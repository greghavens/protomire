"""Actual low-wire dictionary attacks and unlocked local dataset preparation."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from fastapi.testclient import TestClient

from modelbuilder import parquet_reader
from modelbuilder.api import create_app
from modelbuilder.auth import TokenAuth
from modelbuilder.dataset import DatasetRegistry
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService
from test_registry import make_manifest

if os.environ.get("MODELBUILDER_REQUIRE_PARQUET") == "1":
    import pyarrow as arrow
    import pyarrow.parquet as parquet
else:
    arrow = pytest.importorskip("pyarrow")
    parquet = pytest.importorskip("pyarrow.parquet")

OWNER = Actor("dataset-owner", {"read", "modify", "execute"})


def parse(data, budget=100000):
    return DatasetRegistry(None, max_total_bytes=budget)._parse(data, {"id": "source", "format": "parquet"}, {})


def dictionary_source(nested=False):
    stream = io.BytesIO()
    if nested:
        # Only an 80KB dictionary and 32KB integer indices exist in the writer.
        # Decoding the logical list would require over 655MB of string payload.
        values = arrow.DictionaryArray.from_arrays(arrow.array([0] * 8192, type=arrow.int32()), arrow.array(["x" * 80000]))
        value = arrow.ListArray.from_arrays(arrow.array([0, 8192], type=arrow.int32()), values)
        table = arrow.Table.from_arrays([arrow.array(["one"]), value], names=["id", "text"])
    else:
        table = arrow.table({"id": [str(i) for i in range(64)], "text": ["x" * 80000] * 64})
    parquet.write_table(table, stream, compression="zstd", use_dictionary=True, write_statistics=False, store_schema=False)
    return stream.getvalue()


@pytest.mark.parametrize("nested", [False, True])
def test_real_dictionary_expansion_stays_in_resource_contained_child(nested, monkeypatch):
    raw = dictionary_source(nested)
    metadata = parquet.ParquetFile(io.BytesIO(raw)).metadata
    assert len(raw) < 1000 and sum(metadata.row_group(0).column(i).total_uncompressed_size
                                 for i in range(metadata.num_columns)) < 100000
    monkeypatch.setattr(parquet, "ParquetFile", lambda *a, **k: pytest.fail("parent must never run native Parquet parser"))
    with pytest.raises(ServiceError) as failure:
        parse(raw)
    assert failure.value.code == "dataset_limit_exceeded"


def test_actual_child_enforces_total_address_space_before_decode(tmp_path):
    helper = Path(parquet_reader.__file__).with_name("parquet_decode.py")
    # Use the actual production main(), replace only its pure decode callback.
    # A refused 600MB virtual allocation proves the fixed512MB cap, without
    # allocating the requested memory in the parent or depending on RSS polling.
    program = '''import importlib.util, json, resource, sys
spec=importlib.util.spec_from_file_location("bounded",sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
def decode(*args, **kwargs):
    assert resource.getrlimit(resource.RLIMIT_AS)==(512*1024*1024,512*1024*1024)
    try: bytearray(600*1024*1024)
    except MemoryError: return [{"id":"hard-memory-cap-enforced"}]
    raise AssertionError("OS allowed allocation beyond the promised process cap")
m.decode=decode
raise SystemExit(m.main())
'''
    result = subprocess.run([sys.executable, "-I", "-c", program, str(helper)],
        input=b'{"max_file_bytes":100000,"max_rows":100,"decoded_budget":100000}\nbytes',
        capture_output=True, timeout=10, env={"OPENBLAS_NUM_THREADS": "1"})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["rows"] == [{"id": "hard-memory-cap-enforced"}]


def test_timeout_kills_and_reaps_the_actual_slow_child(tmp_path, monkeypatch):
    slow = tmp_path / "slow.py"
    pid = tmp_path / "pid"
    slow.write_text(f'import os,time\nopen({str(pid)!r},"w").write(str(os.getpid()))\ntime.sleep(30)\n')
    real = parquet_reader.subprocess.run
    def launch(command, **kwargs):
        assert "-I" in command and kwargs["env"] == {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
                                                     "MKL_NUM_THREADS": "1", "ARROW_NUM_THREADS": "1"}
        assert kwargs["stderr"] == subprocess.DEVNULL and hasattr(kwargs["stdout"], "fileno")
        return real([*command[:-1], str(slow)], **kwargs)
    monkeypatch.setattr(parquet_reader.subprocess, "run", launch)
    monkeypatch.setattr(parquet_reader, "WALL_SECONDS", .15)
    with pytest.raises(ServiceError) as failure:
        parse(b"bounded fixture")
    assert failure.value.code == "dataset_decode_timeout"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


def test_output_length_checked_before_parent_json_materialization(tmp_path, monkeypatch):
    writer = tmp_path / "oversize.py"
    writer.write_text('import sys\nsys.stdout.buffer.write(b"x"*400000)\n')
    real = parquet_reader.subprocess.run
    monkeypatch.setattr(parquet_reader.subprocess, "run", lambda command, **kwargs: real([*command[:-1], str(writer)], **kwargs))
    with pytest.raises(ServiceError) as failure:
        parse(b"bytes")
    assert failure.value.code == "dataset_limit_exceeded"


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_optional_platform_refusal_keeps_jsonl_csv_and_ide_service_available(platform, monkeypatch, tmp_path):
    monkeypatch.setattr(parquet_reader.sys, "platform", platform)
    with pytest.raises(ServiceError) as failure:
        parse(b"bytes")
    assert failure.value.code == "capability_unavailable" and "JSONL/CSV" in failure.value.message
    registry = DatasetRegistry(None)
    assert registry._parse(b'{"id":"one"}\n', {"id": "x", "format": "jsonl"}, {}) == [{"id": "one"}]
    assert registry._parse(b'id\none\n', {"id": "x", "format": "csv"}, {}) == [{"id": "one"}]
    service = OperationService(tmp_path / "service.sqlite3")
    try:
        project = service.invoke("project.create", {"slug": "ordinary", "model_name": "Usable IDE", "idempotency_key": "p"}, OWNER)
        assert service.invoke("recipe.get", {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}, OWNER)["revision"] == 1
    finally:
        service.store.close()


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    service = OperationService(tmp_path / "service.sqlite3")
    project = service.invoke("project.create", {"slug": "bounded", "model_name": "Bounded data", "idempotency_key": "project"}, OWNER)
    service.store.close()
    service = OperationService(tmp_path / "service.sqlite3", dataset_roots={project["project_id"]: {"data": str(root)}})
    manifest = make_manifest(root, fmt="parquet")
    yield service, project, manifest, root
    service.store.close()


def test_held_decode_releases_reads_and_another_sqlite_writer(setup, monkeypatch):
    service, project, manifest, _ = setup
    pid = project["project_id"]
    entered, release = threading.Event(), threading.Event()
    original = parquet_reader.read_parquet
    calls = []
    def held(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(parquet_reader, "read_parquet", held)
    writer = OperationService(service.store.path)
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            future = pool.submit(service.invoke, "dataset.register", {"project_id": pid, "manifest": manifest,
                                "idempotency_key": "held"}, OWNER)
            try:
                assert entered.wait(3)
                assert not service.store.connection.in_transaction
                assert pool.submit(service.invoke, "project.get", {"project_id": pid}, OWNER).result(timeout=1)["project_id"] == pid
                assert pool.submit(writer.invoke, "evidence.add", {"project_id": pid, "title": "Concurrent write", "source_identifier": "doi:10.1234/local",
                                   "idempotency_key": "write"}, OWNER).result(timeout=1)["title"] == "Concurrent write"
                assert not future.done()
            finally:
                release.set()
            record = future.result(timeout=5)
        assert len(calls) == 1  # Commit reuses verified rows; no second decode.
        assert service.invoke("dataset.register", {"project_id": pid, "manifest": manifest,
                              "idempotency_key": "held"}, OWNER) == record
        assert len(calls) == 1 and len(service.store.list("dataset", pid)) == 1
    finally:
        release.set()
        writer.store.close()


@pytest.mark.parametrize("change", ["remove", "replace", "downgrade", "project", "source"])
def test_http_preparation_rechecks_exact_grant_project_and_pinned_bytes(setup, monkeypatch, change):
    service, project, manifest, root = setup
    pid = project["project_id"]
    auth = TokenAuth({"first": OWNER, "second": OWNER})
    app = create_app(service, auth)
    original = parquet_reader.read_parquet
    def changed(*args, **kwargs):
        value = original(*args, **kwargs)
        if change == "remove":
            auth._tokens.pop("first", None)
        elif change in {"replace", "downgrade"}:
            auth._tokens["first"] = Actor(OWNER.id, {"read"} if change == "downgrade" else OWNER.scopes)
        elif change == "project":
            current = service.store.get("project", pid)
            service.store.put("project", pid, pid, {**current, "revision": current["revision"] + 1,
                                                   "model_name": "Rebound during decode"}, current["revision"])
        else:
            path = root / manifest["sources"][0]["path"]
            path.write_bytes(path.read_bytes() + b"changed")
        return value
    monkeypatch.setattr(parquet_reader, "read_parquet", changed)
    with TestClient(app) as client:
        response = client.post("/v1/operations/dataset.register", json={"project_id": pid, "manifest": manifest,
                               "idempotency_key": "revoked"}, headers={"Authorization": "Bearer first"})
    expected = "dataset_preparation_changed" if change == "project" else "dataset_checksum_mismatch" if change == "source" else "forbidden"
    assert response.json()["error"]["code"] == expected, response.text
    assert not service.store.list("dataset", pid)
    assert service.store.connection.execute("SELECT COUNT(*) FROM idempotency WHERE key='revoked'").fetchone()[0] == 0
    assert auth.authenticate("Bearer second").id == OWNER.id


def test_decoder_slots_fail_closed_and_recover(monkeypatch):
    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(parquet_reader, "_SLOTS", slots)
    assert slots.acquire(False) and slots.acquire(False)
    with pytest.raises(ServiceError) as failure:
        parse(b"bytes")
    assert failure.value.code == "dataset_decode_busy"
    slots.release()
    slots.release()
    assert parse(dictionary_source(False), budget=10_000_000)[0]["id"] == "0"


@pytest.mark.parametrize("failure", ["set", "verify"])
def test_child_refuses_unavailable_containment_before_parser(monkeypatch, failure):
    from modelbuilder import parquet_decode
    import resource
    def refuse(*args):
        raise OSError("controlled unsupported limit")
    if failure == "set":
        monkeypatch.setattr(resource, "setrlimit", refuse)
    else:
        monkeypatch.setattr(resource, "setrlimit", lambda *args: None)
        monkeypatch.setattr(resource, "getrlimit", lambda *args: (-1, -1))
    monkeypatch.setattr(parquet_decode, "decode", lambda *a, **k: pytest.fail("must refuse before decoding"))
    with pytest.raises(parquet_decode.DecodeError) as refused:
        parquet_decode.main()
    assert refused.value.code == "capability_unavailable"


@pytest.mark.parametrize("action", ["version", "bind", "preview", "validate"])
def test_prepared_dataset_operations_decode_once_outside_writer(setup, monkeypatch, action):
    service, project, manifest, _ = setup
    pid = project["project_id"]
    registered = service.invoke("dataset.register", {"project_id": pid, "manifest": manifest, "idempotency_key": "initial"}, OWNER)
    payload = {"project_id": pid, "dataset_id": registered["dataset_id"]}
    if action == "version":
        payload.update(manifest={**manifest, "description": "next"}, expected_revision=1, idempotency_key="next")
    elif action == "bind":
        payload.update(recipe_id=project["recipe_id"], expected_revision=1, dataset_revision=1,
                       split_id="train", purpose="training", node_id="bound", idempotency_key="bind")
    elif action == "preview":
        payload.update(revision=1, split_id="train", purpose="training")
    else:
        payload.update(revision=1)
    original, calls = parquet_reader.read_parquet, []
    def checked(*args, **kwargs):
        assert not service.store.connection.in_transaction
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(parquet_reader, "read_parquet", checked)
    result = service.invoke("dataset." + action, payload, OWNER)
    assert result and calls == [1]


@pytest.mark.parametrize("mutation", ["recipe", "dataset", "roots", "source_binding"])
def test_prepared_binding_rechecks_all_binding_revisions(setup, monkeypatch, mutation):
    service, project, manifest, _ = setup
    pid = project["project_id"]
    initial = service.invoke("dataset.register", {"project_id": pid, "manifest": manifest, "idempotency_key": "initial"}, OWNER)
    if mutation == "dataset":
        operation = "dataset.version"
        payload = {"project_id": pid, "dataset_id": initial["dataset_id"], "expected_revision": 1,
                   "manifest": {**manifest, "description": "requested"}, "idempotency_key": "stale"}
    else:
        operation = "dataset.bind"
        payload = {"project_id": pid, "dataset_id": initial["dataset_id"], "dataset_revision": 1,
                   "recipe_id": project["recipe_id"], "expected_revision": 1, "split_id": "train",
                   "purpose": "training", "node_id": "bound", "idempotency_key": "stale"}
    original = parquet_reader.read_parquet
    def changed(*args, **kwargs):
        value = original(*args, **kwargs)
        if mutation == "recipe":
            recipe = service._current_recipe(pid)
            service.invoke("recipe.update", {"project_id": pid, "recipe_id": recipe["recipe_id"],
                "recipe": {**recipe, "name": "Concurrent edited recipe"}, "expected_revision": 1,
                "idempotency_key": "concurrent-recipe"}, OWNER)
        elif mutation == "dataset":
            with monkeypatch.context() as local:
                local.setattr(parquet_reader, "read_parquet", original)
                service.invoke("dataset.version", {**payload, "manifest": {**manifest, "description": "concurrent"},
                                                   "idempotency_key": "concurrent-version"}, OWNER)
        elif mutation == "roots":
            service._dataset_registry._roots[pid]["new-approved-root"] = service._dataset_registry._roots[pid]["data"]
        else:
            current = service._get("project", pid, pid)
            service.store.put("project", pid, pid, {**current, "source_binding": {"alias": "changed"},
                                                   "revision": current["revision"] + 1}, current["revision"])
        return value
    monkeypatch.setattr(parquet_reader, "read_parquet", changed)
    with pytest.raises(ServiceError) as refusal:
        service.invoke(operation, payload, OWNER)
    assert refusal.value.code in {"revision_conflict", "dataset_preparation_changed"}
    assert service.store.connection.execute("SELECT COUNT(*) FROM idempotency WHERE key='stale'").fetchone()[0] == 0


def test_concurrent_same_key_binding_replays_one_atomic_receipt(setup, monkeypatch):
    service, project, manifest, _ = setup
    pid = project["project_id"]
    initial = service.invoke("dataset.register", {"project_id": pid, "manifest": manifest, "idempotency_key": "initial"}, OWNER)
    payload = {"project_id": pid, "dataset_id": initial["dataset_id"], "dataset_revision": 1,
               "recipe_id": project["recipe_id"], "expected_revision": 1, "split_id": "train",
               "purpose": "training", "node_id": "bound", "idempotency_key": "same"}
    barrier = threading.Barrier(2)
    original = parquet_reader.read_parquet
    def held(*args, **kwargs):
        barrier.wait(timeout=5)
        return original(*args, **kwargs)
    monkeypatch.setattr(parquet_reader, "read_parquet", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.invoke, "dataset.bind", payload, OWNER) for _ in range(2)]
        values = [future.result(timeout=10) for future in futures]
    assert values[0] == values[1]
    assert service._current_recipe(pid)["revision"] == 2
    assert len(service.store.list("recipe", pid)) == 1
