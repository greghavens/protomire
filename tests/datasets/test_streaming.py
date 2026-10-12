"""Real sharded sources, metadata-only indexes, restart, and bounded random pages."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from modelbuilder.dataset import DatasetRegistry, load_portable_split
from modelbuilder.dataset_streaming import StreamingConsumption, iter_stream_rows, validate_stream_data
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService

OWNER = Actor("streaming-owner", {"read", "modify", "execute"})


def write_manifest(root, shards, fmt="jsonl", transforms=()):
    sources, splits = [], []
    for name, role, rows in shards:
        path = root / (name + "." + fmt)
        if fmt == "csv":
            import csv
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["id", "text", "label"])
                writer.writeheader()
                writer.writerows(rows)
        elif fmt == "parquet":
            import pyarrow as pa
            import pyarrow.parquet as pq
            pq.write_table(pa.Table.from_pylist(list(rows)), path, row_group_size=100, write_page_checksum=True)
        else:
            with path.open("w") as stream:
                for row in rows:
                    stream.write(json.dumps(row) + "\n")
        with path.open("rb") as stream:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
        sources.append({"id": name, "root_id": "data", "path": path.name, "format": fmt, "sha256": sha})
        splits.append({"id": name, "role": role, "source_ids": [name]})
    return {"name": "Real streaming data", "benchmark": "stream-v1", "license": "CC0-1.0",
            "provenance": {"source": "operator-local user data"}, "access_mode": "streaming", "sources": sources,
            "id_column": "id", "row_schema": {"type": "object", "additionalProperties": False,
                "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "label": {"type": "integer"}},
                "required": ["id", "text", "label"]}, "splits": splits, "transformations": list(transforms)}


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    path = tmp_path / "state.sqlite3"
    service = OperationService(path)
    project = service.invoke("project.create", {"slug": "stream", "model_name": "Stream", "idempotency_key": "p"}, OWNER)
    pid = project["project_id"]
    service.store.close()
    service = OperationService(path, dataset_roots={pid: {"data": root}})
    yield service, pid, root
    service.store.close()


def rows(count, prefix="train", width=30):
    for i in range(count):
        yield {"id": f"{prefix}-{i}", "text": f"{prefix}-{i} " + "x" * width, "label": i % 2}


def register(service, pid, manifest):
    return service.invoke("dataset.register", {"project_id": pid, "manifest": manifest, "idempotency_key": "register"}, OWNER)


@pytest.mark.parametrize("fmt", ["jsonl", "csv", "parquet"])
def test_streaming_portable_rows_and_actual_consumption(setup, fmt):
    if fmt == "parquet":
        pytest.importorskip("pyarrow")
    service, pid, root = setup
    manifest = write_manifest(root, [("train", "train", rows(220)), ("test", "test", rows(3, "test"))], fmt)
    record = register(service, pid, manifest)
    assert record["row_count"] == 223 and "sample_index" not in record
    registry = service._dataset_registry
    data = registry.read_split(pid, record["dataset_id"], 1, "train", purpose="training")
    assert "rows" not in data and "sample_ids" not in data["split"]
    validate_stream_data(data)
    tracker = StreamingConsumption(data)
    observed = []
    for item in iter_stream_rows(data):
        tracker.add(item)
        observed.append(item["sample_id"])
    assert observed == [f"train-{i}" for i in range(220)]
    lineage = tracker.snapshot()
    assert lineage["count"] == 220 and lineage["ordered_lineage_digest"] == record["splits"][0]["lineage_digest"]
    assert str(root) not in json.dumps(lineage)
    for offset in (0, 99, 103, 218, 500):
        page = registry.preview(pid, record["dataset_id"], 1, "train", purpose="training", offset=offset, limit=3)
        assert page["sample_ids"] == observed[offset:offset + 3]
    paths, guards = registry.execution_sources(pid, record["dataset_id"], 1)
    reference = registry.portable_reference(pid, record["dataset_id"], 1)
    assert str(root) not in json.dumps(reference)
    portable = load_portable_split(reference, paths, "train", purpose="training", source_guards=guards)
    assert portable["split"] == data["split"]
    with pytest.raises(ServiceError, match="purpose"):
        registry.read_split(pid, record["dataset_id"], 1, "test", purpose="training")


def test_large_sources_and_restart_preview_never_decode_full_split(setup, monkeypatch):
    service, pid, root = setup
    # Bigger than both old byte caps and the old 100,000-row ceiling, using a
    # generator. The measured code is not helped by preloaded test rows.
    manifest = write_manifest(root, [("train", "train", rows(110_001, width=300))])
    assert (root / "train.jsonl").stat().st_size > 32 * 1024**2
    import psutil
    import threading
    process = psutil.Process()
    before, peak, stop = process.memory_info().rss, [0], threading.Event()
    def sample_rss():
        while not stop.wait(.005):
            peak[0] = max(peak[0], process.memory_info().rss)
    sampler = threading.Thread(target=sample_rss, daemon=True)
    sampler.start()
    try:
        record = register(service, pid, manifest)
    finally:
        stop.set()
        sampler.join()
    peak[0] = max(peak[0], process.memory_info().rss)
    assert peak[0] - before < 48 * 1024**2, f"Streaming allocated {(peak[0]-before)/1024**2:.1f} MiB"
    assert record["row_count"] == 110_001 and len(json.dumps(record)) < 12_000
    from modelbuilder import dataset_streaming
    def forbidden(*args, **kwargs):
        raise AssertionError("A warm or restarted page must use the lineage index, not parse the source")
    monkeypatch.setattr(dataset_streaming, "source_rows", forbidden)
    registry = service._dataset_registry
    page = registry.preview(pid, record["dataset_id"], 1, "train", purpose="training", offset=109_999, limit=2)
    assert page["sample_ids"] == ["train-109999", "train-110000"]
    restarted = DatasetRegistry(service.store, project_roots={pid: {"data": root}})
    assert restarted.preview(pid, record["dataset_id"], 1, "train", purpose="training", offset=80_000, limit=1)["sample_ids"] == ["train-80000"]
    assert restarted.validate(pid, dataset_id=record["dataset_id"], revision=1)["valid"]
    # Indexes contain source/row/split hashes and locators, never private bodies.
    for path in Path(registry._stream_directory).glob("*.sqlite3"):
        assert (b"x" * 300) not in path.read_bytes()


def test_source_and_index_tamper_fail_closed(setup):
    service, pid, root = setup
    manifest = write_manifest(root, [("train", "train", rows(10))])
    record = register(service, pid, manifest)
    registry = service._dataset_registry
    path = root / "train.jsonl"
    original = path.read_bytes()
    path.write_bytes(original.replace(b"train-9", b"train-X"))
    with pytest.raises(ServiceError) as error:
        registry.preview(pid, record["dataset_id"], 1, "train", purpose="training", limit=1)
    assert error.value.code == "dataset_checksum_mismatch"
    path.write_bytes(original)
    index = next(iter(registry._stream_indexes.values()))
    with index.path.open("r+b") as stream:
        stream.seek(4096)
        stream.write(b"changed")
    with pytest.raises(ServiceError) as error:
        registry.preview(pid, record["dataset_id"], 1, "train", purpose="training", limit=1)
    assert error.value.code == "dataset_identity_mismatch"


def test_transforms_split_order_and_decontamination(setup):
    service, pid, root = setup
    samples = [
        {"id": "a", "text": "Keep", "label": 1}, {"id": "b", "text": " KEEP ", "label": 1},
        {"id": "c", "text": "held out", "label": 0}, {"id": "drop", "text": None, "label": 0},
        {"id": "d", "text": "Other", "label": 0}]
    manifest = write_manifest(root, [("train", "train", samples), ("test", "test", [{"id": "test", "text": "held out", "label": 0}])],
        transforms=[{"kind": "rows.drop_missing", "fields": ["text"]},
                    {"kind": "text.normalize", "fields": ["text"], "casefold": True},
                    {"kind": "rows.deduplicate", "fields": ["text"]}, {"kind": "rows.decontaminate", "fields": ["text"]}])
    manifest["splits"][0] = {"id": "train", "role": "train", "sample_ids": ["d", "b", "c", "drop", "a"]}
    record = register(service, pid, manifest)
    page = service._dataset_registry.preview(pid, record["dataset_id"], 1, "train", purpose="training")
    assert page["sample_ids"] == ["d", "a"] and record["row_count"] == 3
    assert [report["removed_count"] for report in record["transformations"]] == [1, 0, 1, 1]


def test_caps_and_duplicate_ids_are_enforced(setup):
    service, pid, root = setup
    manifest = write_manifest(root, [("train", "train", rows(10))])
    registry = DatasetRegistry(service.store, project_roots={pid: {"data": root}}, streaming_limits={"max_rows": 4})
    assert not registry.validate(pid, manifest)["valid"]
    manifest = write_manifest(root, [("train", "train", list(rows(2)) + list(rows(2)))])
    with pytest.raises(ServiceError) as error:
        service._dataset_registry.register(pid, manifest)
    assert error.value.code == "dataset_sample_overlap"
    with pytest.raises(ValueError):
        DatasetRegistry(service.store, streaming_limits={"max_rows": True})


def test_csv_multiline_is_one_indexed_record(setup):
    service, pid, root = setup
    manifest = write_manifest(root, [("train", "train", [{"id": "a", "text": "hello\nworld,\"quoted\"", "label": 1},
                                                            {"id": "b", "text": "next", "label": 0}])], "csv")
    record = register(service, pid, manifest)
    page = service._dataset_registry.preview(pid, record["dataset_id"], 1, "train", purpose="training", limit=1)
    assert page["rows"][0]["text"] == 'hello\nworld,"quoted"'
    assert page["total"] == 2


def test_stream_descriptor_tampering_is_rejected(setup):
    service, pid, root = setup
    record = register(service, pid, write_manifest(root, [("train", "train", rows(2))]))
    data = service._dataset_registry.read_split(pid, record["dataset_id"], 1, "train", purpose="training")
    changed = deepcopy(data)
    changed["split"]["lineage_digest"] = "0" * 64
    with pytest.raises(ServiceError):
        list(iter_stream_rows(changed))


def test_shards_share_one_split_without_unbounded_ids(setup):
    service, pid, root = setup
    manifest = write_manifest(root, [("first", "train", rows(12, "a")), ("second", "train", rows(14, "b"))])
    manifest["splits"] = [{"id": "train", "role": "train", "source_ids": ["first", "second"]}]
    record = register(service, pid, manifest)
    data = service._dataset_registry.read_split(pid, record["dataset_id"], 1, "train", purpose="training")
    consumed = list(iter_stream_rows(data))
    assert len(consumed) == 26 and consumed[12]["source_id"] == "second"
    assert consumed[12]["source_row"] == 1
    assert [row["sample_id"] for row in consumed] == [f"a-{i}" for i in range(12)] + [f"b-{i}" for i in range(14)]
    tracker = StreamingConsumption(data)
    with pytest.raises(ServiceError) as error:
        tracker.add({**consumed[0], "content_digest": "0" * 64})
    assert error.value.code == "dataset_consumption_invalid"
    assert tracker.snapshot()["count"] == 0


def test_nonfinite_rows_and_concurrent_source_changes_are_rejected(setup, monkeypatch):
    service, pid, root = setup
    manifest = write_manifest(root, [("train", "train", [{"id": "bad", "text": "x", "label": float("nan")}])])
    result = service._dataset_registry.validate(pid, manifest)
    assert not result["valid"] and result["diagnostics"][0]["code"] == "dataset_source_invalid"
    manifest = write_manifest(root, [("train", "train", rows(10))])
    from modelbuilder import dataset_streaming
    original = dataset_streaming.source_rows
    def racing(*args, **kwargs):
        for item in original(*args, **kwargs):
            yield item
        target = root / "train.jsonl"
        target.write_bytes(target.read_bytes().replace(b"train-9", b"train-X"))
    monkeypatch.setattr(dataset_streaming, "source_rows", racing)
    result = service._dataset_registry.validate(pid, manifest)
    assert not result["valid"] and result["diagnostics"][0]["code"] in {"dataset_checksum_mismatch", "dataset_source_changed"}


def test_historical_restart_pages_and_tighter_caps(setup, monkeypatch):
    service, pid, root = setup
    manifest = write_manifest(root, [("train", "train", rows(5))])
    record = register(service, pid, manifest)
    second = deepcopy(manifest)
    second["description"] = "Next immutable version"
    service._dataset_registry.version(pid, record["dataset_id"], 1, second)
    registry = DatasetRegistry(service.store, project_roots={pid: {"data": root}})
    from modelbuilder import dataset_streaming
    def forbidden(*args, **kwargs):
        raise AssertionError("Historical page must use its pinned on-disk metadata index")
    monkeypatch.setattr(dataset_streaming, "source_rows", forbidden)
    assert registry.preview(pid, record["dataset_id"], 1, "train", purpose="training")["total"] == 5
    limited = DatasetRegistry(service.store, project_roots={pid: {"data": root}}, streaming_limits={"max_rows": 4})
    assert limited.validate(pid, dataset_id=record["dataset_id"], revision=1)["diagnostics"][0]["code"] == "dataset_limit_exceeded"


def test_streaming_hashes_are_outside_writer_transaction(setup, monkeypatch):
    service, pid, root = setup
    from modelbuilder import dataset_streaming
    original = dataset_streaming.verify_source
    calls = []
    def guarded(*args, **kwargs):
        assert not service.store.connection.in_transaction, "Large source hashing must not hold the writer lock"
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(dataset_streaming, "verify_source", guarded)
    record = register(service, pid, write_manifest(root, [("train", "train", rows(20))]))
    request = {"project_id": pid, "dataset_id": record["dataset_id"], "revision": 1,
               "split_id": "train", "purpose": "training", "offset": 15, "limit": 2}
    assert service.invoke("dataset.preview", request, OWNER)["sample_ids"] == ["train-15", "train-16"]
    assert calls


def test_page_query_seeks_index_instead_of_offset_scanning(setup):
    service, pid, root = setup
    record = register(service, pid, write_manifest(root, [("train", "train", rows(25))]))
    index = next(iter(service._dataset_registry._stream_indexes.values()))
    with index.connect() as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT s.* FROM pages p JOIN samples s ON s.seq=p.seq WHERE p.split_id=? AND p.ordinal>=? ORDER BY p.ordinal LIMIT ?", ("train", 23, 1)).fetchall()
        text = " ".join(row[3] for row in plan)
        assert "SEARCH p USING PRIMARY KEY" in text
        assert "SEARCH s USING INTEGER PRIMARY KEY" in text
    assert service._dataset_registry.preview(pid, record["dataset_id"], 1, "train", purpose="training", offset=23, limit=1)["sample_ids"] == ["train-23"]


def test_small_evaluation_split_bridge_keeps_same_version_and_exact_ids(setup):
    service, pid, root = setup
    record = register(service, pid, write_manifest(root, [("train", "train", rows(100)), ("dev", "validation", rows(4, "dev"))]))
    registry = service._dataset_registry
    train = registry.read_split(pid, record["dataset_id"], 1, "train", purpose="training")
    dev = registry.read_split(pid, record["dataset_id"], 1, "dev", purpose="evaluation")
    from modelbuilder.dataset_streaming import materialize_stream_split, _resolve_stream
    _, training_index = _resolve_stream(train)
    eager = materialize_stream_split(dev, max_rows=4, max_bytes=4096)
    _, evaluation_index = _resolve_stream(dev)
    assert training_index is evaluation_index  # No recompile of a large training split for evaluation.
    assert eager["dataset"] == train["dataset"]
    assert eager["consumption"]["version_id"] == record["version_id"]
    assert eager["split"] == {"id": "dev", "role": "validation", "sample_ids": [f"dev-{i}" for i in range(4)]}
    assert eager["consumption"]["sample_ids"] == eager["split"]["sample_ids"]
    assert len(eager["consumption"]["sample_digests"]) == 4
    assert "stream" not in eager and str(root) not in json.dumps(eager)


def test_split_bridge_preflights_rows_and_decoded_bytes_before_reading(setup, monkeypatch):
    service, pid, root = setup
    record = register(service, pid, write_manifest(root, [("dev", "validation", rows(5, "dev"))]))
    data = service._dataset_registry.read_split(pid, record["dataset_id"], 1, "dev", purpose="evaluation")
    from modelbuilder import dataset_streaming
    def forbidden(*args, **kwargs):
        raise AssertionError("Over-budget split must fail before resolving/decoding its source")
    monkeypatch.setattr(dataset_streaming, "_resolve_stream", forbidden)
    for budget in ({"max_rows": 4, "max_bytes": 10000}, {"max_rows": 5, "max_bytes": 10}):
        with pytest.raises(ServiceError) as error:
            dataset_streaming.materialize_stream_split(data, **budget)
        assert error.value.code == "dataset_limit_exceeded"


@pytest.mark.skipif(__import__('os').name == 'nt', reason='POSIX descriptor boundary')
@pytest.mark.parametrize("change", ["symlink", "fifo", "root"])
def test_streaming_sources_keep_descriptor_root_safety(setup, change):
    import os
    service, pid, root = setup
    manifest = write_manifest(root, [("train", "train", rows(3))])
    source = root / "train.jsonl"
    if change == "root":
        root.rename(root.with_name("previous-data"))
        root.mkdir()
    else:
        backup = root.with_name("outside.jsonl")
        backup.write_bytes(source.read_bytes())
        source.unlink()
        if change == "symlink":
            source.symlink_to(backup)
        else:
            os.mkfifo(source)
    result = service._dataset_registry.validate(pid, manifest)
    assert not result["valid"]
    assert result["diagnostics"][0]["code"] == {"symlink": "dataset_source_unavailable", "fifo": "dataset_source_invalid", "root": "dataset_root_changed"}[change]


def test_streaming_binding_and_plan_use_same_public_operation_contract(setup, monkeypatch):
    service, pid, root = setup
    from test_binding import InspectBackend
    from modelbuilder import dataset_streaming
    backend = InspectBackend(root.parent / "runtime")
    service.backend = backend
    original = dataset_streaming.verify_source
    def guarded(*args, **kwargs):
        assert not service.store.connection.in_transaction
        return original(*args, **kwargs)
    monkeypatch.setattr(dataset_streaming, "verify_source", guarded)
    record = register(service, pid, write_manifest(root, [("train", "train", rows(8))]))
    recipe = service.invoke("project.open", {"project_id": pid}, OWNER)["recipe"]
    bound = service.invoke("dataset.bind", {"project_id": pid, "recipe_id": recipe["recipe_id"],
        "expected_revision": recipe["revision"], "dataset_id": record["dataset_id"], "dataset_revision": 1,
        "split_id": "train", "purpose": "training", "node_id": "data", "idempotency_key": "bind"}, OWNER)
    assert not bound["execution_started"] and bound["binding"]["split"]["sample_count"] == 8
    assert str(root) not in json.dumps(bound)
    plan = service.invoke("run.plan", {"project_id": pid, "recipe_id": recipe["recipe_id"]}, OWNER)
    assert plan["executable"], plan
    _, payload, paths, guards = backend.calls[-1]
    importer = payload["recipe"]["nodes"][0]
    reference = json.loads(Path(paths[importer["parameters"]["reference_id"]]).read_text())
    data = load_portable_split(reference, paths, "train", purpose="training", source_guards=guards)
    assert len(list(iter_stream_rows(data))) == 8
    assert not list(backend.root.rglob("*.jsonl"))


def test_operator_caps_reach_service_and_reject_unknown_settings(tmp_path, monkeypatch):
    from modelbuilder.api import create_app
    from modelbuilder.backend.operator_config import load_operator_config
    config = tmp_path / "operator.json"
    config.write_text(json.dumps({"dataset_limits": {"max_rows": 42, "max_row_bytes": 2048}}))
    assert load_operator_config(config)["dataset_limits"]["max_rows"] == 42
    monkeypatch.setenv("MODELBUILDER_OPERATOR_CONFIG", str(config))
    monkeypatch.setenv("MODELBUILDER_DB_PATH", str(tmp_path / "configured.sqlite3"))
    monkeypatch.setenv("MODELBUILDER_BACKEND", "none")
    app = create_app(tokens={})
    try:
        assert app.state.service._dataset_registry.streaming_limits["max_row_bytes"] == 2048
    finally:
        app.state.service.store.close()
    config.write_text(json.dumps({"dataset_limits": {"disable_checksums": True}}))
    with pytest.raises(ValueError):
        load_operator_config(config)
