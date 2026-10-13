"""Actual source/index/operation tests for bounded sampling and eager parity."""
from copy import deepcopy
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
import subprocess
import sys

import pytest

from modelbuilder.dataset import DatasetRegistry, load_portable_split
from modelbuilder import dataset_streaming as streaming
from modelbuilder.evaluation import digest
from modelbuilder.models import ServiceError
from modelbuilder.service import OperationService
from modelbuilder.store import SQLiteStore
from test_streaming import OWNER, write_manifest, rows, register
import test_streaming as streaming_fixtures
from streaming_memory_probe import run_with_clean_parent

setup = streaming_fixtures.setup


def sample(**changes):
    return {"kind": "rows.sample", "version": "1", "split_ids": ["train"], "fields": ["label"],
            "fraction": 0.25, "seed": "固定-seed", "allocation": "proportional", "min_per_stratum": 1,
            "required_strata": [], "max_share_error": 0.1, **changes}


def alive(index):
    with index.connect() as db:
        return [streaming.row_identity(row) for row in db.execute("SELECT * FROM samples WHERE alive=1 ORDER BY seq")]


def compare(registry, pid, manifest):
    _, eager, splits, reports = registry._materialize(pid, {**manifest, "access_mode": "eager"})
    _, index, _, stream_reports = registry._materialize(pid, manifest)
    expected = [{key: row[key] for key in ("sample_id", "source_id", "source_row", "source_content_digest",
                                         "content_digest", "split_id")} for row in eager]
    assert alive(index) == expected
    for before, after in zip(reports, stream_reports):
        if before['declaration']['kind'] == 'rows.sample':
            for key in ('representation', 'selection_digest', 'policy_digest', 'source_counts'):
                assert before[key] == after[key]
            assert after['selected_count'] == len(before['selected_sample_ids'])
            assert after['removed_count'] == len(before['removed_sample_ids'])
    for split in splits:
        actual = list(streaming.indexed_rows(index, split['id']))
        assert [row['sample_id'] for row in actual] == split['sample_ids']
        assert all(row['row'] == next(x['row'] for x in eager if x['sample_id'] == row['sample_id']) for row in actual)
    return index, stream_reports


@pytest.mark.parametrize('fmt', ['jsonl', 'csv', 'parquet'])
@pytest.mark.parametrize('allocation', ['proportional', 'equal'])
def test_shards_splits_unicode_stage_evidence_and_consumption_parity(setup, fmt, allocation):
    if fmt == 'parquet':
        if sys.platform != 'linux':
            pytest.skip('Pinned Parquet containment requires Linux RLIMIT_AS; Mac decoder is intentionally unavailable')
        pytest.importorskip('pyarrow')
    service, pid, root = setup
    a = [{'id': f'é-{i}', 'text': f'  Café {i}\n雪  ', 'label': 0} for i in range(24)]
    a[0]['id'] = 'é-"0\\😀'
    b = [{'id': f'雪-{i}', 'text': f'  Béta {i}  ', 'label': 0 if i < 2 else 1} for i in range(16)]
    transforms = [
        {'kind': 'text.normalize', 'fields': ['text'], 'unicode_form': 'NFD', 'casefold': True},
        sample(fraction=0.5, allocation=allocation, max_share_error=0, required_strata=[{'label': 0}, {'label': 1}]),
        {'kind': 'text.normalize', 'fields': ['text'], 'unicode_form': 'NFC'},
    ]
    manifest = write_manifest(root, [('a', 'train', a), ('b', 'train', b), ('dev', 'validation', rows(4, 'dev'))], fmt, transforms)
    manifest['splits'] = [{'id': 'train', 'role': 'train', 'source_ids': ['a', 'b']}, manifest['splits'][-1]]
    before = {x['path']: (root / x['path']).read_bytes() for x in manifest['sources']}
    index, reports = compare(service._dataset_registry, pid, manifest)
    assert sorted(x['after'] for x in reports[1]['representation'][0]['strata']) == ([7, 13] if allocation == 'proportional' else [10, 10])
    assert {x['path']: (root / x['path']).read_bytes() for x in manifest['sources']} == before
    with index.connect() as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'sample_rank%' OR name='sample_winners'").fetchall()
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='sample_strata'").fetchall()
        assert db.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 44
    record = register(service, pid, manifest)
    data = service._dataset_registry.read_split(pid, record['dataset_id'], 1, 'train', purpose='training')
    tracker = streaming.StreamingConsumption(data)
    selected = list(streaming.iter_stream_rows(data))
    assert len(selected) == 20 and record['lineage']['source_row_count'] == 44
    assert tracker.snapshot()['count'] == 0  # Preparation proves no consumption.
    for row in selected[:3]:
        tracker.add(row)
    assert tracker.snapshot()['count'] == 3


def test_explicit_split_order_and_hash_ties_preserve_membership_order(setup, monkeypatch):
    service, pid, root = setup
    manifest = write_manifest(root, [('source', 'train', rows(20))], transforms=[sample()])
    manifest['splits'] = [{'id': 'train', 'role': 'train', 'sample_ids': [f'train-{i}' for i in reversed(range(20))]}]
    from modelbuilder import rules
    original = digest
    def tied(value):
        return 'a' * 64 if isinstance(value, dict) and set(value) == {'seed', 'sample_id'} else original(value)
    monkeypatch.setattr(streaming, 'digest', tied)
    monkeypatch.setattr(rules, 'digest', tied)
    index, _ = compare(service._dataset_registry, pid, manifest)
    assert [x['sample_id'] for x in alive(index)] == ['train-0', 'train-1', 'train-10', 'train-11', 'train-13']
    assert [x['sample_id'] for x in streaming.indexed_rows(index, 'train')] == ['train-13', 'train-11', 'train-10', 'train-1', 'train-0']


def test_missing_fields_unselected_split_and_normalized_strata(setup):
    service, pid, root = setup
    source = [{'id': f'train-{i}', 'text': ' A ' if i < 20 else 'A', 'label': i % 2} for i in range(40)]
    manifest = write_manifest(root, [('train', 'train', source), ('dev', 'validation', [{'id': 'dev', 'label': 1}])])
    manifest['row_schema']['required'] = ['id', 'label']
    manifest['transformations'] = [sample(fields=['text'], required_strata=[{'text': ' A '}], min_per_stratum=0)]
    index, reports = compare(service._dataset_registry, pid, manifest)
    assert len(reports[0]['representation'][0]['strata']) == 2
    assert any(x['sample_id'] == 'dev' for x in alive(index))
    manifest['splits'].pop()
    manifest['sources'].pop()
    manifest['transformations'].insert(0, {'kind': 'text.normalize', 'fields': ['text'], 'strip': True})
    manifest['transformations'][1]['required_strata'] = [{'text': 'A'}]
    _, reports = compare(service._dataset_registry, pid, manifest)
    assert len(reports[1]['representation'][0]['strata']) == 1


def test_selection_digest_uses_eager_json_escaping_for_every_unicode_id(setup):
    service, pid, root = setup
    ids = ['é-"quoted"', '雪-\\backslash', '😀-𝄞', 'e\u0301-combining']
    source = [{'id': name, 'text': name, 'label': 0} for name in ids]
    manifest = write_manifest(root, [('train', 'train', source)], transforms=[sample(fraction=1, max_share_error=0)])
    _, reports = compare(service._dataset_registry, pid, manifest)
    assert reports[0]['selection_digest'] == digest(sorted(ids))


def test_null_sampling_evidence_is_blocked_in_selected_split(setup):
    service, pid, root = setup
    source = list(rows(10))
    source[0]['label'] = None
    manifest = write_manifest(root, [('train', 'train', source)], transforms=[sample()])
    for mode in ('eager', 'streaming'):
        with pytest.raises(ServiceError) as error:
            service._dataset_registry._materialize(pid, {**manifest, 'access_mode': mode})
        assert error.value.code == 'dataset_sampling_blocked'


def test_distinct_sampling_values_are_erased_before_index_publication(setup):
    service, pid, root = setup
    category = 'transient-category-value-should-be-erased'
    source = [{'id': f'row-{i}', 'text': category, 'label': i % 2} for i in range(20)]
    manifest = write_manifest(root, [('train', 'train', source)], transforms=[sample(fields=['text'])])
    index, reports = compare(service._dataset_registry, pid, manifest)
    assert reports[0]['representation'][0]['strata'][0]['stratum_values'] == {'text': category}
    assert category.encode() not in index.path.read_bytes()
    with index.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM samples WHERE length(f0)=64').fetchone()[0] == 20


@pytest.mark.parametrize('changes, message', [
    ({'fraction': 0.01}, 'minimum'),
    ({'split_ids': ['absent']}, 'unknown or empty'),
    ({'required_strata': [{'label': 7}]}, 'absent'),
    ({'required_strata': [{'wrong': 0}]}, 'exactly'),
    ({'required_strata': [{'label': 0}, {'label': 0}]}, 'unique'),
    ({'fraction': 0.3, 'max_share_error': 0}, 'share_error'),
    ({'fields': ['absent']}, 'missing'),
])
def test_invalid_or_infeasible_policies_fail_with_eager_reason_and_no_index(setup, changes, message):
    service, pid, root = setup
    manifest = write_manifest(root, [('train', 'train', rows(10))], transforms=[sample(**changes)])
    registry = service._dataset_registry
    for mode in ('eager', 'streaming'):
        with pytest.raises(ServiceError, match=message) as error:
            registry._materialize(pid, {**manifest, 'access_mode': mode})
        assert error.value.code == 'dataset_sampling_blocked'
    assert not list(Path(registry._stream_directory).iterdir())


def test_required_minima_construct_quota_and_multiple_sample_stages(setup):
    service, pid, root = setup
    source = [{'id': f'train-{i}', 'text': f'row-{i}', 'label': 0 if i < 39 else 1} for i in range(40)]
    manifest = write_manifest(root, [('train', 'train', source)], transforms=[
        sample(fraction=0.25, min_per_stratum=0, required_strata=[{'label': 1}]),
        sample(fraction=0.5, min_per_stratum=1, max_share_error=0.2)])
    _, reports = compare(service._dataset_registry, pid, manifest)
    assert sorted(x['after'] for x in reports[0]['representation'][0]['strata']) == [1, 9]
    assert reports[1]['output_rows'] == 5


def test_quotas_are_per_selected_split_and_nested_strata_are_canonical(setup):
    service, pid, root = setup
    manifest = write_manifest(root, [('a', 'train', rows(40, 'a')), ('b', 'validation', rows(80, 'b'))],
                              transforms=[sample(split_ids=['a', 'b'])])
    _, reports = compare(service._dataset_registry, pid, manifest)
    assert [(row['split_id'], sum(x['after'] for x in row['strata'])) for row in reports[0]['representation']] == [('a', 10), ('b', 20)]
    nested = [{'id': f'n-{i}', 'text': 'row-' + str(i), 'label': {'category': '雪', 'options': [1, True]}}
              for i in range(20)]
    manifest = write_manifest(root, [('train', 'train', nested)], transforms=[sample(required_strata=[{'label': nested[0]['label']}])])
    manifest['row_schema']['properties']['label'] = {'type': 'object', 'additionalProperties': True}
    compare(service._dataset_registry, pid, manifest)


def test_filters_before_and_after_sample_defer_errors_only_for_surviving_rows(setup):
    service, pid, root = setup
    source = list(rows(40)) + [{'id': 'bad', 'text': '', 'label': None}]
    transforms = [{'kind': 'rows.drop_missing', 'fields': ['text']}, sample(),
                  {'kind': 'rows.deduplicate', 'fields': ['label']}]
    manifest = write_manifest(root, [('train', 'train', source)], transforms=transforms)
    compare(service._dataset_registry, pid, manifest)
    # A downstream malformed row selected out by the sample must not fail.
    from modelbuilder.rules import sample_rows
    source = list(rows(40))
    chosen, _ = sample_rows([{'sample_id': x['id'], 'split_id': 'train', 'row': x} for x in source], sample())
    chosen_ids = {x['sample_id'] for x in chosen}
    dropped = next(x for x in source if x['id'] not in chosen_ids)
    dropped['text'] = None
    manifest['transformations'] = [sample(), {'kind': 'text.normalize', 'fields': ['text']}]
    (root / 'train.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in source))
    manifest['sources'][0]['sha256'] = hashlib.sha256((root / 'train.jsonl').read_bytes()).hexdigest()
    compare(service._dataset_registry, pid, manifest)
    kept = next(x for x in source if x['id'] in chosen_ids)
    kept['text'] = None
    (root / 'train.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in source))
    manifest['sources'][0]['sha256'] = hashlib.sha256((root / 'train.jsonl').read_bytes()).hexdigest()
    for mode in ('eager', 'streaming'):
        with pytest.raises(ServiceError) as error:
            service._dataset_registry._materialize(pid, {**manifest, 'access_mode': mode})
        assert error.value.code == 'dataset_transform_invalid'


def test_duplicate_identity_across_shards_rejected_before_sampling(setup):
    service, pid, root = setup
    manifest = write_manifest(root, [('a', 'train', rows(10)), ('b', 'train', rows(10))], transforms=[sample()])
    manifest['splits'] = [{'id': 'train', 'role': 'train', 'source_ids': ['a', 'b']}]
    with pytest.raises(ServiceError) as error:
        service._dataset_registry._materialize(pid, manifest)
    assert error.value.code == 'dataset_sample_overlap'


@pytest.mark.parametrize('cap,value', [('max_sampling_strata', 1), ('max_report_bytes', 200), ('max_index_bytes', 8192)])
def test_sampling_limits_cold_warm_and_restart(setup, cap, value):
    service, pid, root = setup
    manifest = write_manifest(root, [('train', 'train', rows(100))], transforms=[sample()])
    registry = service._dataset_registry
    record = register(service, pid, manifest)
    registry.streaming_limits[cap] = value
    with pytest.raises(ServiceError) as error:
        registry._materialize(pid, manifest)
    assert error.value.code == 'dataset_limit_exceeded'
    restarted = DatasetRegistry(service.store, project_roots={pid: {'data': root}}, streaming_limits={cap: value})
    with pytest.raises(ServiceError) as error:
        restarted._materialize(pid, manifest)
    assert error.value.code == 'dataset_limit_exceeded'
    cold = DatasetRegistry(None, project_roots={pid: {'data': root}}, streaming_limits={cap: value})
    with pytest.raises(ServiceError) as error:
        cold._materialize(pid, manifest)
    assert error.value.code == 'dataset_limit_exceeded'
    assert not list(Path(cold._stream_directory).iterdir())
    assert record['row_count'] == 25


@pytest.mark.parametrize('fmt', ['jsonl', 'csv'])
@pytest.mark.parametrize('label', ['雪' * 1000, '😀' * 1000], ids=['bmp', 'supplementary'])
@pytest.mark.parametrize('boundary', [-1, 0, 1, 'review-5000'])
def test_unicode_report_budget_matches_cold_warm_and_restart(setup, fmt, label, boundary):
    service, pid, root = setup
    source = [{**row, 'label': label} for row in rows(4)]
    manifest = write_manifest(root, [('train', 'train', source)], fmt, [sample(fraction=0.5)])
    manifest['row_schema']['properties']['label'] = {'type': 'string'}
    record = register(service, pid, manifest)
    registry = service._dataset_registry
    expected = alive(next(iter(registry._stream_indexes.values())))
    report_bytes = len(streaming.encoded(record['transformations']))
    cap = 5000 if boundary == 'review-5000' else report_bytes + boundary
    registry.streaming_limits['max_report_bytes'] = cap
    restarted = DatasetRegistry(service.store, project_roots={pid: {'data': root}}, streaming_limits={'max_report_bytes': cap})
    cold = DatasetRegistry(None, project_roots={pid: {'data': root}}, streaming_limits={'max_report_bytes': cap})
    for mode, reader in [('warm', registry), ('restart', restarted), ('cold', cold)]:
        if cap < report_bytes:
            with pytest.raises(ServiceError) as error:
                reader._materialize(pid, manifest)
            assert error.value.code == 'dataset_limit_exceeded', mode
            if mode == 'cold':
                assert not reader._stream_indexes and not list(Path(reader._stream_directory).iterdir())
        else:
            _, index, _, report = reader._materialize(pid, manifest)
            assert len(index) == 2 and alive(index) == expected, mode
            assert report == record['transformations'] and len(streaming.encoded(report)) == report_bytes, mode


def test_unicode_working_evidence_keeps_early_canonical_report_cap(setup, monkeypatch):
    service, pid, root = setup
    source = [{**row, 'label': str(i) + '雪' * 1000} for i, row in enumerate(rows(4))]
    manifest = write_manifest(root, [('train', 'train', source)], transforms=[sample(fraction=1)])
    manifest['row_schema']['properties']['label'] = {'type': 'string'}
    from modelbuilder import rules
    def forbidden(*args, **kwargs):
        raise AssertionError('Oversized aggregate evidence must fail before quotas/reports are allocated')
    monkeypatch.setattr(rules, 'sampling_quotas', forbidden)
    cold = DatasetRegistry(None, project_roots={pid: {'data': root}}, streaming_limits={'max_report_bytes': 5000})
    with pytest.raises(ServiceError) as error:
        cold._materialize(pid, manifest)
    assert error.value.code == 'dataset_limit_exceeded'
    assert not cold._stream_indexes and not list(Path(cold._stream_directory).iterdir())


def test_physical_index_allocation_is_outside_logical_identity_but_enforced_on_reuse(setup):
    service, pid, root = setup
    manifest = write_manifest(root, [('train', 'train', rows(40))], transforms=[sample()])
    record = register(service, pid, manifest)
    registry = service._dataset_registry
    key, original = next(iter(registry._stream_indexes.items()))
    path = Path(registry._stream_directory) / 'physical-allocation.sqlite3'
    shutil.copyfile(original.path, path)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE allocation_probe (payload BLOB)')
        db.execute('INSERT INTO allocation_probe VALUES (zeroblob(?))', (original.path.stat().st_size + 32768,))
        db.commit()
        db.execute('DROP TABLE allocation_probe')
        db.commit()
        peak = path.stat().st_size
        db.execute('VACUUM')
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert peak > path.stat().st_size
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    destination = path.with_name(sha + '.sqlite3')
    path.rename(destination)
    different = streaming.StreamingIndex(registry, pid, original.manifest, destination, deepcopy(original.summary), sha,
                                        peak_index_bytes=peak)
    assert alive(different) == alive(original)
    changed_record = streaming.version_record(pid, record['dataset_id'], 1, original.manifest, different,
                                             different.summary['splits'], different.summary['transformations'])
    for field in ('content_digest', 'version_id', 'lineage', 'transformations', 'splits'):
        assert changed_record[field] == record[field]
    assert 'peak_index_bytes' not in changed_record['lineage']
    assert changed_record['lineage_index']['peak_index_bytes'] == peak
    assert changed_record['lineage_index']['sha256'] != record['lineage_index']['sha256']
    legacy = streaming.StreamingIndex(registry, pid, original.manifest, destination,
        {**deepcopy(original.summary), 'peak_index_bytes': peak + 4096}, sha)
    legacy_record = streaming.version_record(pid, record['dataset_id'], 1, original.manifest, legacy,
                                            legacy.summary['splits'], legacy.summary['transformations'])
    assert legacy_record['content_digest'] == record['content_digest']
    assert legacy_record['version_id'] == record['version_id']
    assert legacy.peak_index_bytes == peak + 4096 and 'peak_index_bytes' not in legacy.summary
    streaming.enforce_index_caps(registry, different)
    registry._stream_indexes[key] = different
    registry.streaming_limits['max_index_bytes'] = peak - 1
    store = SQLiteStore(root.parent / 'allocation-state.sqlite3')
    try:
        store.put('dataset', record['dataset_id'], pid, changed_record)
        restarted = DatasetRegistry(store, project_roots={pid: {'data': root}}, streaming_limits={'max_index_bytes': peak - 1})
        shutil.copyfile(destination, Path(restarted._stream_directory) / (sha + '.sqlite3'))
        for reader in (registry, restarted):
            with pytest.raises(ServiceError) as error:
                reader._materialize(pid, manifest)
            assert error.value.code == 'dataset_limit_exceeded'
    finally:
        store.close()
    # A missing/smaller allocation observation cannot bypass real file size.
    different.peak_index_bytes = 0
    registry.streaming_limits['max_index_bytes'] = destination.stat().st_size - 1
    with pytest.raises(ServiceError) as error:
        streaming.enforce_index_caps(registry, different)
    assert error.value.code == 'dataset_limit_exceeded'
    # Logical selection evidence remains part of the content/version identity.
    different.summary['transformations'][0]['selection_digest'] = 'a' * 64
    altered = streaming.version_record(pid, record['dataset_id'], 1, original.manifest, different,
                                      different.summary['splits'], different.summary['transformations'])
    assert altered['content_digest'] != record['content_digest'] and altered['version_id'] != record['version_id']


@pytest.mark.parametrize('phase', ['ingestion', 'ranking'])
def test_cancellation_cleans_incomplete_indexes_then_restart_succeeds(setup, monkeypatch, phase):
    service, pid, root = setup
    manifest = write_manifest(root, [('train', 'train', rows(1000))], transforms=[sample()])
    cancel = root.parent / 'cancel.json'
    if phase == 'ingestion':
        original = streaming.source_rows
        def interrupted(*args, **kwargs):
            for i, row in enumerate(original(*args, **kwargs)):
                if i == 10:
                    cancel.touch()
                yield row
        monkeypatch.setattr(streaming, 'source_rows', interrupted)
    else:
        original = streaming.digest
        count = 0
        def interrupted(value):
            nonlocal count
            if isinstance(value, dict) and set(value) == {'seed', 'sample_id'}:
                count += 1
                if count == 100:
                    cancel.touch()
            return original(value)
        monkeypatch.setattr(streaming, 'digest', interrupted)
    registry = service._dataset_registry
    with streaming.compilation_context(str(cancel)), pytest.raises(ServiceError) as error:
        register(service, pid, manifest)
    assert error.value.code == 'dataset_cancelled'
    assert not service.store.list('dataset', pid)
    assert not list(Path(registry._stream_directory).iterdir()) and not registry._stream_indexes
    monkeypatch.undo()
    cancel.unlink()
    record = register(service, pid, manifest)
    restarted = OperationService(service.store.path, dataset_roots={pid: {'data': root}})
    try:
        preview = restarted.invoke('dataset.preview', {'project_id': pid, 'dataset_id': record['dataset_id'],
                                  'revision': 1, 'split_id': 'train', 'purpose': 'training', 'limit': 2}, OWNER)
        assert len(preview['sample_ids']) == 2
    finally:
        restarted.store.close()


def test_portable_compilation_uses_existing_cancel_file(setup):
    service, pid, root = setup
    manifest = write_manifest(root, [('train', 'train', rows(40))], transforms=[sample()])
    record = register(service, pid, manifest)
    ref = service._dataset_registry.portable_reference(pid, record['dataset_id'], 1)
    cancel = root.parent / 'cancel.json'
    cancel.touch()
    with pytest.raises(ServiceError) as error:
        load_portable_split(ref, {'dataset-' + x['sha256']: root / x['path'] for x in manifest['sources']},
                            'train', purpose='training', cancel_file=str(cancel))
    assert error.value.code == 'dataset_cancelled'


def test_warm_index_verification_observes_cancellation_and_keeps_valid_cache(setup, monkeypatch):
    service, pid, root = setup
    manifest = write_manifest(root, [('train', 'train', rows(100))], transforms=[sample()])
    register(service, pid, manifest)
    registry = service._dataset_registry
    index = next(iter(registry._stream_indexes.values()))
    cancel = root.parent / 'cancel.json'
    original = streaming.checked_file_digest
    def interrupted(stream):
        cancel.touch()
        return original(stream)
    monkeypatch.setattr(streaming, 'checked_file_digest', interrupted)
    with streaming.compilation_context(str(cancel)), pytest.raises(ServiceError) as error:
        registry._materialize(pid, manifest)
    assert error.value.code == 'dataset_cancelled'
    assert index.path.exists() and hashlib.sha256(index.path.read_bytes()).hexdigest() == index.sha256
    cancel.unlink()
    monkeypatch.undo()
    assert registry._materialize(pid, manifest)[1] is index


@pytest.mark.asyncio
async def test_http_and_mcp_gateway_share_sampling_result_and_failure(setup):
    import httpx
    from modelbuilder.api import create_app
    from modelbuilder.mcp_server import OperationGateway, OperationError
    service, pid, root = setup
    manifest = write_manifest(root, [('train', 'train', rows(40))], transforms=[sample()])
    app = create_app(service=service, tokens={'token': OWNER})
    async with httpx.AsyncClient(base_url='http://localhost', transport=httpx.ASGITransport(app=app),
                                 headers={'Authorization': 'Bearer token'}) as client:
        gateway = OperationGateway(client)
        request = {'project_id': pid, 'manifest': manifest, 'idempotency_key': 'sampled'}
        result = await gateway.invoke('dataset.register', request)
        assert result == (await client.post('/v1/operations/dataset.register', json=request)).json()
        preview = {'project_id': pid, 'dataset_id': result['dataset_id'], 'revision': 1,
                   'split_id': 'train', 'purpose': 'training', 'offset': 2, 'limit': 3}
        assert await gateway.invoke('dataset.preview', preview) == (await client.post('/v1/operations/dataset.preview', json=preview)).json()
        bad = deepcopy(request)
        bad['idempotency_key'] = 'infeasible'
        bad['manifest']['transformations'][0]['required_strata'] = [{'label': 99}]
        response = await client.post('/v1/operations/dataset.register', json=bad)
        with pytest.raises(OperationError) as error:
            await gateway.invoke('dataset.register', bad)
        assert response.json()['error']['code'] == error.value.body['error']['code'] == 'dataset_sampling_blocked'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_official_mcp_stdio_sampling_matches_http_and_restart(tmp_path, monkeypatch):
    from importlib.util import module_from_spec, spec_from_file_location
    import httpx
    source = Path(__file__).resolve().parents[2] / 'scripts/mcp_smoke.py'
    spec = spec_from_file_location('stream_sampling_mcp_harness', source)
    harness = module_from_spec(spec)
    spec.loader.exec_module(harness)
    root, data = tmp_path / 'service', tmp_path / 'data'
    root.mkdir()
    data.mkdir()
    service = OperationService(root / 'service.sqlite3')
    pid = service.invoke('project.create', {'slug': 'sampling', 'model_name': 'Sampling', 'idempotency_key': 'p'}, OWNER)['project_id']
    service.store.close()
    manifest = write_manifest(data, [('train', 'train', rows(40)), ('dev', 'validation', rows(4, 'dev'))], transforms=[sample()])
    reader = DatasetRegistry(None, project_roots={pid: {'data': data}})
    _, expected, _, reports = reader._materialize(pid, {**manifest, 'access_mode': 'eager'})
    config = tmp_path / 'operator.json'
    config.write_text(json.dumps({'dataset_roots': {pid: {'data': str(data)}}}))
    monkeypatch.setenv('MODELBUILDER_OPERATOR_CONFIG', str(config))
    monkeypatch.setenv('MODELBUILDER_BACKEND', 'none')
    process = harness.ServiceProcess(root)
    process.start()
    try:
        async with process.mcp() as session:
            await session.initialize()
            request = {'project_id': pid, 'manifest': manifest, 'idempotency_key': 'sampled'}
            result = await session.call_tool('dataset_register', request)
            assert not result.isError, result.structuredContent
            record = result.structuredContent
            assert record['transformations'][0]['selection_digest'] == reports[0]['selection_digest']
            async with httpx.AsyncClient(base_url=process.base_url,
                    headers={'Authorization': 'Bearer ' + process.owner}) as client:
                assert record == (await client.post('/v1/operations/dataset.register', json=request)).json()
            process.restart()
            preview = {'project_id': pid, 'dataset_id': record['dataset_id'], 'revision': 1,
                       'split_id': 'train', 'purpose': 'training', 'limit': 100}
            page = await session.call_tool('dataset_preview', preview)
            assert not page.isError, page.structuredContent
            assert page.structuredContent['sample_ids'] == [x['sample_id'] for x in expected if x['split_id'] == 'train']
            bad = deepcopy(request)
            bad['idempotency_key'] = 'blocked'
            bad['manifest']['transformations'][0]['required_strata'] = [{'label': 99}]
            blocked = await session.call_tool('dataset_register', bad)
            assert blocked.isError and blocked.structuredContent['error']['code'] == 'dataset_sampling_blocked'
    finally:
        process.stop()


def test_mac_parquet_sampling_fails_at_verified_containment_boundary(setup):
    if sys.platform == 'linux':
        pytest.skip('Mac-only negative containment check')
    service, pid, root = setup
    path = root / 'train.parquet'
    path.write_bytes(b'PAR1')
    manifest = write_manifest(root, [('train', 'train', rows(1))], transforms=[sample()])
    manifest['sources'][0].update(path=path.name, format='parquet', sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(ServiceError) as error:
        service._dataset_registry._materialize(pid, manifest)
    assert error.value.code == 'capability_unavailable'


@pytest.mark.skipif(os.name == 'nt', reason='Fresh-process RSS evidence uses POSIX getrusage')
def test_above_eager_limits_sampling_has_bounded_resident_memory(tmp_path):
    # A clean intermediary excludes inherited pytest high-water RSS while the
    # measured child's kernel peak retains actual native + Python allocations;
    # the child generates rows directly on disk and never materializes IDs.
    script = Path(__file__).with_name('streaming_sampling_large.py')
    reports = []
    for count in (120_001, 240_001):
        root = tmp_path / str(count)
        root.mkdir()
        result = run_with_clean_parent([sys.executable, str(script), str(root), str(count)],
                                       env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, timeout=120)
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report['source_rows'] == count and report['selected_rows'] == count // 10
        assert report['source_bytes'] > report['eager_file_limit']
        assert report['peak_rss_bytes'] < 192 * 1024**2, report
        reports.append(report)
    assert reports[1]['peak_rss_bytes'] - reports[0]['peak_rss_bytes'] < 16 * 1024**2, reports


@pytest.mark.skipif(os.name == 'nt', reason='RSS evidence uses POSIX getrusage')
def test_rss_probe_excludes_parent_heap_but_keeps_released_child_peak():
    # All pressure is confined to a short-lived parent, not the pytest process.
    # The measured allocation is released before reporting, so current-RSS
    # polling or subtraction of the ancestor's larger peak cannot pass.
    worker = """import json,mmap,resource,sys
size=int(sys.argv[1])
if size:
    memory=mmap.mmap(-1,size)
    for offset in range(0,size,4096): memory[offset]=1
    memory.close()
peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({'peak':peak if sys.platform=='darwin' else peak*1024}))
"""
    parent = """import json,resource,subprocess,sys
sys.path.insert(0,sys.argv[1])
from streaming_memory_probe import run_with_clean_parent
pressure=bytearray(96*1024**2)
for offset in range(0,len(pressure),4096): pressure[offset]=1
worker=sys.argv[2]
direct=subprocess.run([sys.executable,'-c',worker,'0'],capture_output=True,text=True,check=True)
values=[]
for size in (0,48*1024**2):
    result=run_with_clean_parent([sys.executable,'-c',worker,str(size)],timeout=10)
    assert result.returncode==0,result.stderr
    values.append(json.loads(result.stdout)['peak'])
print(json.dumps({'direct':json.loads(direct.stdout)['peak'],'clean':values[0],'released_peak':values[1]}))
"""
    result = subprocess.run([sys.executable, '-c', parent, str(Path(__file__).parent), worker],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    if sys.platform == 'linux':
        assert report['direct'] >= 96 * 1024**2, report  # Reproduce exec contamination.
    assert report['clean'] < 64 * 1024**2, report
    assert report['released_peak'] >= 48 * 1024**2, report
    assert report['released_peak'] - report['clean'] >= 32 * 1024**2, report


@pytest.mark.skipif(os.name == 'nt', reason='RSS fixture containment requires POSIX process groups')
def test_rss_probe_timeout_terminates_measured_worker(tmp_path):
    import time
    import psutil

    pid_file = tmp_path / 'worker.pid'
    worker = "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired):
        run_with_clean_parent([sys.executable, '-c', worker, str(pid_file)], timeout=1)
    assert pid_file.is_file()
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.01)
    else:
        pytest.fail('Timed-out RSS measurement left its worker running')
