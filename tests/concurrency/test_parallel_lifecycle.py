"""Existing audited framework/fixture lifecycles through native parallel bodies."""
import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip('torch')
pytest.importorskip('zenml')

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration


def harness(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('live', [False, True])
def test_source_component_training_evaluation_export_and_live(tmp_path, monkeypatch, live):
    module = harness('tests/components/test_native_components.py', 'parallel_components')
    original = module.write_json
    def write(path, value):
        if path.name == 'request.json':
            value['recipe']['execution'] = {'max_parallel_stages': 2}
        return original(path, value)
    monkeypatch.setattr(module, 'write_json', write)
    module.test_native_independent_harness(tmp_path, 'image', 3, live)
    records = [json.loads(path.read_text()) for path in (tmp_path / 'job/processes').glob('*/owner.json')]
    assert len(records) == 3 and all(record['reaped'] for record in records)


def test_builtin_lifecycle_uses_process_bodies_and_native_lineage(tmp_path):
    from modelbuilder.backend import ZenMLBackend
    module = harness('tests/integration/test_native_lifecycle.py', 'parallel_fixture')
    backend = ZenMLBackend(tmp_path)
    body = module.recipe(steps=4)
    body['execution'] = {'max_parallel_stages': 2}
    request = module.submit(backend, body)
    result = module.wait(backend, request, predicate=lambda current: current['status'] in {'succeeded', 'failed', 'cancelled'} and not current['worker_alive'])
    assert result['status'] == 'succeeded', result
    assert set(result['node_statuses']) == {'data', 'initial', 'train', 'evaluate', 'export'}
    records = [json.loads(path.read_text()) for path in (backend.root / 'jobs' / request['operation_id'] / 'processes').glob('*/owner.json')]
    assert len(records) == 5 and all(record['reaped'] for record in records)
    assert all(record['descendant_cleanup']['remaining_pids'] == [] for record in records)
