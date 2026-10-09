"""Real file adapters and independent text data, no platform ML fixtures."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

from modelbuilder.api import create_app
from modelbuilder.dataset import (DatasetRegistry, MANIFEST_SCHEMA, consumption, evaluation_identity,
                                  load_portable_split, validate_manifest)
from modelbuilder.evaluation import compare_records, make_evaluation_record
from modelbuilder.mcp_server import OperationGateway, catalog_tools
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService

OWNER = Actor("dataset-owner", {"read", "modify", "execute"})


def make_manifest(root, *, suffix="", rows=None, fmt="jsonl"):
    rows = rows or [
        {"id": "train-1", "text": "  sunny day  ", "label": 1},
        {"id": "train-2", "text": "cold rain", "label": 0},
        {"id": "dev-1", "text": "warm afternoon", "label": 1},
        {"id": "heldout-1", "text": "winter snow", "label": 0},
    ]
    target = root / ("weather" + suffix + "." + fmt)
    if fmt == "csv":
        import csv
        with target.open("w", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=["id", "text", "label"])
            writer.writeheader()
            writer.writerows(rows)
    else:
        target.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return {
        "name": "Independent weather text", "benchmark": "weather-v1", "license": "CC0-1.0",
        "provenance": {"source": "locally authored test examples", "source_revision": "1"},
        "sources": [{"id": "weather", "root_id": "data", "path": target.name,
                     "format": fmt, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}],
        "id_column": "id", "row_schema": {"type": "object", "additionalProperties": False,
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "label": {"type": "integer"}},
            "required": ["id", "text", "label"]},
        "splits": [{"id": "train", "role": "train", "sample_ids": [row["id"] for row in rows if row["id"].startswith("train")]},
                   {"id": "dev", "role": "validation", "sample_ids": [row["id"] for row in rows if row["id"].startswith("dev")]},
                   {"id": "heldout", "role": "test", "sample_ids": [row["id"] for row in rows if row["id"].startswith("heldout")]}],
        "transformations": [{"kind": "text.normalize", "fields": ["text"], "strip": True, "casefold": True}],
    }


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    service = OperationService(tmp_path / "store.sqlite3")
    project = service.invoke("project.create", {"slug": "dataset", "model_name": "Weather model", "idempotency_key": "project"}, OWNER)
    pid = project["project_id"]
    service.store.close()
    service = OperationService(tmp_path / "store.sqlite3", dataset_roots={pid: {"data": str(root)}})
    registry = DatasetRegistry(service.store, project_roots=service.dataset_roots)
    yield service, registry, pid, root
    service.store.close()


@pytest.mark.parametrize("fmt", ["csv", "jsonl"])
def test_actual_files_versions_transforms_and_consumed_identity(setup, fmt):
    service, registry, pid, root = setup
    manifest = make_manifest(root, fmt=fmt)
    registered = registry.register(pid, manifest)
    assert registered["revision"] == 1 and registered["row_count"] == 4
    assert "sunny day" not in json.dumps(registered)  # metadata, not a data-hosting store
    train = registry.read_split(pid, registered["dataset_id"], 1, "train", purpose="training")
    assert train["rows"][0]["text"] == "sunny day"
    assert [row["id"] for row in train["rows"]] == train["consumption"]["sample_ids"] == ["train-1", "train-2"]
    assert not set(train["split"]["sample_ids"]) & {"dev-1", "heldout-1"}
    exact = consumption(registered, "train", ["train-2", "train-1", "train-2"], purpose="training")
    assert exact["count"] == 3 and exact["unique_count"] == 2
    changed = deepcopy(manifest)
    target = root / ("weather-v2." + fmt)
    target.write_bytes((root / manifest["sources"][0]["path"]).read_bytes().replace(b"winter snow", b"autumn wind"))
    changed["sources"][0].update(path=target.name, sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    second = registry.version(pid, registered["dataset_id"], 1, changed)
    assert second["revision"] == 2 and second["parent_version_id"] == registered["version_id"]
    assert second["content_digest"] != registered["content_digest"]
    assert registry.get(pid, registered["dataset_id"], 1) == registered
    assert registry.validate(pid, dataset_id=registered["dataset_id"], revision=1)["valid"]
    assert registry.validate(pid, dataset_id=registered["dataset_id"], revision=2)["valid"]
    assert registry.list(pid)["datasets"][0]["revision"] == 2
    assert "sample_index" not in registry.list(pid)["datasets"][0]
    portable = registry.portable_reference(pid, registered["dataset_id"], 2)
    assert portable["dependencies"][0]["sha256"] == changed["sources"][0]["sha256"]
    assert str(root) not in json.dumps(portable)
    with pytest.raises(ServiceError) as conflict:
        registry.version(pid, registered["dataset_id"], 1, changed)
    assert conflict.value.code == "revision_conflict"
    assert len(service.store.resource_versions("dataset", registered["dataset_id"])) == 2


def test_dataset_change_cannot_be_ranked_as_same_benchmark(setup):
    _, registry, pid, root = setup
    manifest = make_manifest(root)
    one = registry.register(pid, manifest)
    changed = deepcopy(manifest)
    changed["transformations"][0]["casefold"] = False
    two = registry.version(pid, one["dataset_id"], 1, changed)
    records = []
    for index, version in enumerate([one, two]):
        evidence = make_evaluation_record(**evaluation_identity(version, "dev"),
            protocol={"implementation": "independent-weather-evaluate", "version": "1", "configuration": {}, "preprocessing": {}, "seed": 1},
            metric={"name": "accuracy", "implementation": "python.equality", "version": "1", "parameters": {}, "aggregation": "mean", "direction": "maximize"},
            treatment={"model": f"model-{index}", "checkpoint": f"checkpoint-{index}", "calibration": None, "reference": None},
            value=.5 + index * .5, predictions=[{"item_id": "dev-1", "value": .5 + index * .5}])
        records.append({"backend_run_id": f"run-{index}", "status": "succeeded",
                        "metrics": {"accuracy": evidence["value"], "evaluation_records": {"accuracy": evidence}}})
    compared = compare_records(records, "accuracy", "maximize")
    assert not compared["compatible"] and compared["selected_run_id"] is None


@pytest.mark.parametrize("split,purpose,ids", [
    ("dev", "training", ["dev-1"]), ("heldout", "training", ["heldout-1"]),
    ("train", "evaluation", ["train-1"]), ("dev", "calibration", ["dev-1"]),
    ("train", "training", ["heldout-1"]), ("train", "training", []),
])
def test_split_role_and_consumption_refusals(setup, split, purpose, ids):
    _, registry, pid, root = setup
    record = registry.register(pid, make_manifest(root))
    with pytest.raises(ServiceError):
        consumption(record, split, ids, purpose=purpose)


def test_duplicate_ids_split_overlap_content_overlap_and_decontamination(setup):
    _, registry, pid, root = setup
    manifest = make_manifest(root)
    overlap = deepcopy(manifest)
    overlap["splits"][1]["sample_ids"].append("train-1")
    assert registry.validate(pid, overlap)["diagnostics"][0]["code"] == "dataset_split_overlap"
    rows = [json.loads(line) for line in (root / "weather.jsonl").read_text().splitlines()]
    rows[0]["text"] = "WARM AFTERNOON"
    manifest = make_manifest(root, rows=rows)
    assert registry.validate(pid, manifest)["diagnostics"][0]["code"] == "dataset_content_overlap"
    manifest["transformations"].append({"kind": "rows.decontaminate", "fields": ["text"]})
    clean = registry.register(pid, manifest)
    assert clean["row_count"] == 3
    assert clean["transformations"][-1]["removed_sample_ids"] == ["train-1"]
    assert clean["splits"][0]["sample_ids"] == ["train-2"]
    rows[0]["id"] = "train-2"
    manifest = make_manifest(root, rows=rows)
    # Its duplicated sample_ids declaration is rejected even before data reads.
    assert not registry.validate(pid, manifest)["valid"]


def test_dedup_drop_missing_select_and_row_order(setup):
    _, registry, pid, root = setup
    rows = [{"id": "train-1", "text": "sun", "label": 1, "unused": "x"},
            {"id": "train-2", "text": "sun", "label": 1, "unused": "y"},
            {"id": "train-3", "text": "", "label": 0, "unused": "z"},
            {"id": "dev-1", "text": "snow", "label": 0, "unused": "x"},
            {"id": "heldout-1", "text": "rain", "label": 0, "unused": "x"}]
    manifest = make_manifest(root, rows=rows)
    manifest["transformations"] = [{"kind": "rows.drop_missing", "fields": ["text"]},
                                   {"kind": "columns.select", "fields": ["id", "text", "label"]},
                                   {"kind": "rows.deduplicate", "fields": ["text"]}]
    record = registry.register(pid, manifest)
    assert record["row_count"] == 3
    assert record["transformations"][0]["removed_sample_ids"] == ["train-3"]
    assert record["transformations"][2]["removed_sample_ids"] == ["train-2"]
    assert all("source_content_digest" in item for item in record["sample_index"])


def test_tampering_is_refused_for_validate_and_read_without_mutating_version(setup):
    _, registry, pid, root = setup
    record = registry.register(pid, make_manifest(root))
    (root / "weather.jsonl").write_text("tampered")
    validation = registry.validate(pid, dataset_id=record["dataset_id"], revision=1)
    assert not validation["valid"] and validation["diagnostics"][0]["code"] == "dataset_checksum_mismatch"
    with pytest.raises(ServiceError) as error:
        registry.read_split(pid, record["dataset_id"], 1, "train", purpose="training")
    assert error.value.code == "dataset_checksum_mismatch"
    assert registry.get(pid, record["dataset_id"]) == record


@pytest.mark.parametrize("path", ["/etc/passwd", "../secret", "nested/../../secret", "C:/secret", "C:\\secret", "./weather.jsonl", ".", "a//b", "a/\x00b"])
def test_path_traversal_and_absolute_paths_refused(setup, path):
    _, registry, pid, root = setup
    manifest = make_manifest(root)
    manifest["sources"][0]["path"] = path
    result = registry.validate(pid, manifest)
    assert not result["valid"] and result["diagnostics"][0]["code"] == "dataset_path_invalid"


@pytest.mark.parametrize("intermediate", [False, True])
def test_symlink_sources_and_directories_fail_closed(setup, tmp_path, intermediate):
    _, registry, pid, root = setup
    manifest = make_manifest(root)
    if intermediate:
        (root / "linkdir").symlink_to(root, target_is_directory=True)
        manifest["sources"][0]["path"] = "linkdir/weather.jsonl"
    else:
        (root / "link.jsonl").symlink_to(root / "weather.jsonl")
        manifest["sources"][0]["path"] = "link.jsonl"
    result = registry.validate(pid, manifest)
    assert result["diagnostics"][0]["code"] == "dataset_source_unavailable"
    assert str(tmp_path) not in json.dumps(result)


def test_root_replacement_is_detected(setup):
    _, registry, pid, root = setup
    manifest = make_manifest(root)
    root.rename(root.with_name("old-data"))
    root.mkdir()
    make_manifest(root)
    result = registry.validate(pid, manifest)
    assert result["diagnostics"][0]["code"] == "dataset_root_changed"


def test_read_mutation_and_fifo_fail_without_hanging(setup, monkeypatch):
    _, registry, pid, root = setup
    manifest = make_manifest(root)
    original_read = os.read
    modified = False
    def changed(fd, size):
        nonlocal modified
        result = original_read(fd, size)
        if not modified:
            modified = True
            with (root / "weather.jsonl").open("a") as stream:
                stream.write("\n")
        return result
    monkeypatch.setattr(os, "read", changed)
    assert registry.validate(pid, manifest)["diagnostics"][0]["code"] == "dataset_source_changed"
    monkeypatch.setattr(os, "read", original_read)
    fifo = root / "fifo"
    os.mkfifo(fifo)
    manifest["sources"][0]["path"] = "fifo"
    assert registry.validate(pid, manifest)["diagnostics"][0]["code"] == "dataset_source_invalid"


def test_bounds_schema_code_and_parquet_capability_fail_closed(setup, monkeypatch):
    service, registry, pid, root = setup
    manifest = make_manifest(root)
    limited = DatasetRegistry(service.store, project_roots=service.dataset_roots, max_file_bytes=12)
    assert limited.validate(pid, manifest)["diagnostics"][0]["code"] == "dataset_limit_exceeded"
    limited = DatasetRegistry(service.store, project_roots=service.dataset_roots, max_rows=2)
    assert limited.validate(pid, manifest)["diagnostics"][0]["code"] == "dataset_limit_exceeded"
    bad = deepcopy(manifest)
    bad["row_schema"]["$ref"] = "file:///etc/passwd"
    assert registry.validate(pid, bad)["diagnostics"][0]["code"] == "dataset_schema_unsupported"
    bad = deepcopy(manifest)
    bad["transformations"] = [{"kind": "python", "code": "import os"}]
    assert registry.validate(pid, bad)["diagnostics"][0]["code"] == "dataset_manifest_invalid"
    bad = deepcopy(manifest)
    bad["sources"][0]["format"] = "parquet"
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    assert registry.validate(pid, bad)["diagnostics"][0]["code"] == "capability_unavailable"


def test_project_isolation_hides_paths_manifests_and_data(setup):
    service, registry, pid, root = setup
    record = registry.register(pid, make_manifest(root))
    other = service.invoke("project.create", {"slug": "other", "model_name": "Other", "idempotency_key": "other"}, OWNER)["project_id"]
    for call in [lambda: registry.get(other, record["dataset_id"]),
                 lambda: registry.read_split(other, record["dataset_id"], 1, "train", purpose="training")]:
        with pytest.raises(ServiceError) as error:
            call()
        assert error.value.status_code == 404
        assert str(root) not in str(error.value)
    assert registry.list(other) == {"datasets": []}
    assert registry.validate(other, make_manifest(root))["diagnostics"][0]["code"] == "dataset_root_unavailable"


async def test_shared_service_http_mcp_gateway_and_idempotency(setup):
    service, _, pid, root = setup
    actor = Actor("scoped", {"read", "modify"}, {pid})
    app = create_app(service, tokens={"test-only": actor})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                                headers={"Authorization": "Bearer test-only"}) as client:
        gateway = OperationGateway(client)
        tools, aliases = catalog_tools(await gateway.catalog())
        assert aliases["dataset_register"] == "dataset.register"
        assert next(tool for tool in tools if tool.name == "dataset_register").annotations.readOnlyHint is False
        payload = {"project_id": pid, "manifest": make_manifest(root), "idempotency_key": "dataset"}
        registered = await gateway.invoke("dataset.register", payload)
        assert await gateway.invoke("dataset.register", payload) == registered
        assert service.invoke("dataset.get", {"project_id": pid, "dataset_id": registered["dataset_id"]}, actor) == registered
        changed = deepcopy(payload["manifest"])
        changed["description"] = "Reviewed data sources"
        updated = await gateway.invoke("dataset.version", {"project_id": pid, "dataset_id": registered["dataset_id"],
            "expected_revision": 1, "manifest": changed, "idempotency_key": "dataset-version"})
        assert updated["revision"] == 2
        assert (await gateway.invoke("dataset.list", {"project_id": pid}))["datasets"][0]["revision"] == 2
        assert (await gateway.invoke("dataset.validate", {"project_id": pid, "dataset_id": registered["dataset_id"], "revision": 1}))["valid"]
        assert (await gateway.invoke("dataset.get", {"project_id": pid, "dataset_id": registered["dataset_id"], "revision": 1})) == registered
    events = service.invoke("project.history", {"project_id": pid}, OWNER)["events"]
    assert events[0]["operation"] == "dataset.version" and events[0]["target_id"] == registered["dataset_id"]


def test_schema_file_matches_contract():
    path = Path(__file__).resolve().parents[2] / "schemas/dataset.v1.schema.json"
    assert json.loads(path.read_text()) == MANIFEST_SCHEMA
    with pytest.raises(ServiceError):
        validate_manifest({})


def test_portable_version_loads_without_database_or_original_root(setup, tmp_path):
    _, registry, pid, root = setup
    record = registry.register(pid, make_manifest(root))
    reference = registry.portable_reference(pid, record["dataset_id"], 1)
    relocated = tmp_path / "elsewhere"
    relocated.mkdir()
    file = relocated / "immutable.data"
    file.write_bytes((root / "weather.jsonl").read_bytes())
    paths = {reference["dependencies"][0]["id"]: str(file)}
    # The portable loader has no registry/store argument and original aliases
    # are not resolvable in this independent directory.
    loaded = load_portable_split(reference, paths, "train", purpose="training", expected_project_id=pid)
    assert loaded == registry.read_split(pid, record["dataset_id"], 1, "train", purpose="training")
    assert loaded["rows"][0]["text"] == "sunny day"
    tampered = deepcopy(reference)
    tampered["version_id"] = "0" * 64
    with pytest.raises(ServiceError) as error:
        load_portable_split(tampered, paths, "train", purpose="training", expected_project_id=pid)
    assert error.value.code == "dataset_identity_mismatch"
    with pytest.raises(ServiceError) as error:
        load_portable_split(reference, paths, "train", purpose="training", expected_project_id="another-project")
    assert error.value.code == "dataset_reference_invalid"
    with pytest.raises(ServiceError) as error:
        load_portable_split(reference, {}, "train", purpose="training", expected_project_id=pid)
    assert error.value.code == "dataset_dependency_unavailable"
    file.write_text("changed")
    with pytest.raises(ServiceError) as error:
        load_portable_split(reference, paths, "train", purpose="training", expected_project_id=pid)
    assert error.value.code == "dataset_checksum_mismatch"


def test_preview_bounds_and_service_endpoint(setup):
    service, registry, pid, root = setup
    record = registry.register(pid, make_manifest(root))
    first = registry.preview(pid, record["dataset_id"], 1, "train", purpose="training", limit=1)
    assert first["total"] == 2 and first["has_more"]
    assert first["sample_ids"] == ["train-1"] and "consumption" not in first
    assert "sample_ids" not in first["split"]
    page = service.invoke("dataset.preview", {"project_id": pid, "dataset_id": record["dataset_id"], "revision": 1,
                           "split_id": "train", "purpose": "training", "offset": 1, "limit": 1}, OWNER)
    assert page["sample_ids"] == ["train-2"] and not page["has_more"]
    with pytest.raises(ServiceError):
        registry.preview(pid, record["dataset_id"], 1, "train", purpose="training", limit=101)


def test_explicit_item_order_and_whole_source_split_selectors(setup):
    _, registry, pid, root = setup
    manifest = make_manifest(root)
    manifest["splits"][0]["sample_ids"].reverse()
    record = registry.register(pid, manifest)
    train = registry.read_split(pid, record["dataset_id"], 1, "train", purpose="training")
    assert [row["id"] for row in train["rows"]] == ["train-2", "train-1"]
    manifest["splits"] = [{"id": "train", "role": "train", "source_ids": ["weather"]}]
    record = registry.register(pid, manifest)
    assert record["splits"][0]["sample_ids"] == ["train-1", "train-2", "dev-1", "heldout-1"]


def test_durable_versions_on_restart_and_read_without_local_roots(setup):
    service, registry, pid, root = setup
    record = registry.register(pid, make_manifest(root))
    reopened = OperationService(service.store.path)
    try:
        assert reopened.invoke("dataset.get", {"project_id": pid, "dataset_id": record["dataset_id"]}, OWNER) == record
        result = reopened.invoke("dataset.validate", {"project_id": pid, "dataset_id": record["dataset_id"]}, OWNER)
        assert not result["valid"] and result["diagnostics"][0]["code"] == "dataset_root_unavailable"
    finally:
        reopened.store.close()


@pytest.mark.parametrize("fmt", ["jsonl", "csv"])
def test_row_bound_stops_before_decoding_extra_rows(setup, fmt):
    service, _, pid, root = setup
    manifest = make_manifest(root, fmt=fmt)
    path = root / manifest["sources"][0]["path"]
    if fmt == "jsonl":
        lines = path.read_text().splitlines()
        path.write_text("\n".join(lines[:2] + ["not valid JSON"]) + "\n")
    else:
        lines = path.read_text().splitlines()
        path.write_text("\n".join(lines[:3] + ["bad,row,not-a-number"]) + "\n")
    manifest["sources"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    limited = DatasetRegistry(service.store, project_roots=service.dataset_roots, max_rows=2)
    result = limited.validate(pid, manifest)
    assert result["diagnostics"][0]["code"] == "dataset_limit_exceeded"


def test_dataset_admission_matches_worker_validation(setup):
    from modelbuilder.backend.admission import BackendError, validate_dataset_import
    _, registry, pid, root = setup
    record = registry.register(pid, make_manifest(root))
    reference = registry.portable_reference(pid, record["dataset_id"], 1)
    path = root / "reference.json"
    path.write_text(json.dumps(reference))
    declarations = [{"id": "reference", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}, *reference["dependencies"]]
    paths = {"reference": str(path), reference["dependencies"][0]["id"]: str(root / "weather.jsonl")}
    backend = SimpleNamespace(dependency_paths=paths)
    payload = {"project_id": pid, "recipe": {"external_dependencies": declarations}}
    node = {"id": "data", "parameters": {"reference_id": "reference", "split_id": "train", "purpose": "training"}}
    validate_dataset_import(backend, payload, node)
    with pytest.raises(BackendError) as error:
        validate_dataset_import(backend, payload, {**node, "parameters": {}})
    assert error.value.code == "validation_error"
    with pytest.raises(BackendError) as error:
        validate_dataset_import(backend, payload, {**node, "parameters": {**node["parameters"], "split_id": "heldout"}})
    assert error.value.code == "dataset_split_purpose_invalid"
    with pytest.raises(BackendError) as error:
        validate_dataset_import(backend, {**payload, "project_id": "other"}, node)
    assert error.value.code == "dataset_reference_invalid"
    with pytest.raises(BackendError) as error:
        validate_dataset_import(backend, {**payload, "recipe": {"external_dependencies": declarations[:1]}}, node)
    assert error.value.code == "dataset_dependency_unavailable"
    (root / "weather.jsonl").write_text("changed")
    with pytest.raises(BackendError) as error:
        validate_dataset_import(backend, payload, node)
    assert error.value.code == "dataset_checksum_mismatch"
