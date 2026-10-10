"""Real optional PyArrow bytes, portable provenance, and fail-closed admission.

Every binary is generated locally in pytest's disposable directory. No downloaded
or user dataset, network resolver, training run, or synthetic Arrow stand-in.
"""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
import random
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

if os.environ.get("MODELBUILDER_REQUIRE_PARQUET") == "1":
    import pyarrow as arrow
    import pyarrow.parquet as parquet
else:
    arrow = pytest.importorskip("pyarrow", reason="Install the pinned optional profile with --parquet")
    parquet = pytest.importorskip("pyarrow.parquet")

from modelbuilder.api import create_app  # noqa: E402
from modelbuilder.backend.adapter import ZenMLBackend  # noqa: E402
from modelbuilder.backend.admission import BackendError, validate_dataset_import  # noqa: E402
from modelbuilder.dataset import DatasetRegistry, PYARROW_REQUIREMENT, load_portable_split  # noqa: E402
from modelbuilder.dataset_binding import reference_bytes, reference_dependency, resolve_backend  # noqa: E402
from modelbuilder.mcp_server import OperationGateway  # noqa: E402
from modelbuilder.models import Actor, ServiceError  # noqa: E402
from modelbuilder.service import OperationService  # noqa: E402

OWNER = Actor("parquet-owner", {"read", "modify", "execute"})


def write_manifest(root, *, rows=None, name="sequences", metadata=None, **options):
    rng = random.Random(17)
    if rows is None:
        rows = [{"id": f"sequence-{index:02d}", "tokens": [rng.randrange(100) for _ in range(1 + index % 13)],
                 "text": f"  Sequence {index}  ", "label": index % 2} for index in range(48)]
    table = arrow.Table.from_pylist(rows)
    if metadata:
        table = table.replace_schema_metadata(metadata)
    path = root / f"{name}.parquet"
    parquet.write_table(table, path, compression="zstd", row_group_size=8,
                        write_page_checksum=True, **options)
    manifest = {
        "name": "Seeded variable-length sequences", "benchmark": "local-sequences-v1", "license": "CC0-1.0",
        "provenance": {"source": "locally generated synthetic sequences", "source_revision": "generator-v1-seed-17"},
        "sources": [{"id": "sequences", "root_id": "data", "path": path.name, "format": "parquet",
                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}],
        "id_column": "id", "row_schema": {"type": "object", "additionalProperties": False,
            "properties": {"id": {"type": "string"}, "tokens": {"type": "array", "items": {"type": "integer"}},
                           "text": {"type": "string"}, "label": {"type": "integer"}},
            "required": ["id", "tokens", "text", "label"]},
        "splits": [{"id": "train", "role": "train", "sample_ids": [row["id"] for row in rows[:32]][::-1]},
                   {"id": "dev", "role": "validation", "sample_ids": [row["id"] for row in rows[32:40]]},
                   {"id": "test", "role": "test", "sample_ids": [row["id"] for row in rows[40:]]}],
    }
    (root / f"{name}.schema.json").write_text(json.dumps(manifest["row_schema"], sort_keys=True))
    (root / f"{name}.splits.json").write_text(json.dumps(manifest["splits"], sort_keys=True))
    return manifest, rows


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    service = OperationService(tmp_path / "service.sqlite3")
    project = service.invoke("project.create", {"slug": "parquet", "model_name": "Sequences",
                             "idempotency_key": "project"}, OWNER)
    service.store.close()
    service = OperationService(tmp_path / "service.sqlite3", dataset_roots={project["project_id"]: {"data": str(root)}})
    yield service, service._dataset_registry, project, root
    service.store.close()


def test_actual_sequences_roundtrip_versions_split_schema_and_relocated_reader(setup, tmp_path):
    _, registry, project, root = setup
    pid = project["project_id"]
    manifest, rows = write_manifest(root)
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    registered = registry.register(pid, manifest)
    assert registered["row_count"] == 48
    rerun, _ = write_manifest(root, name="rerun")
    assert rerun["sources"][0]["sha256"] == manifest["sources"][0]["sha256"]
    second_registration = registry.register(pid, rerun)
    assert second_registration["content_digest"] == registered["content_digest"]
    changed = deepcopy(manifest)
    changed["transformations"] = [{"kind": "text.normalize", "fields": ["text"], "casefold": True}]
    newer = registry.version(pid, registered["dataset_id"], 1, changed)
    assert newer["parent_version_id"] == registered["version_id"]
    assert newer["content_digest"] != registered["content_digest"]
    assert registry.get(pid, registered["dataset_id"], 1) == registered
    assert all((root / name).read_bytes() == content for name, content in before.items())
    assert registered["manifest"]["row_schema"] == json.loads(before["sequences.schema.json"])
    assert registered["manifest"]["splits"] == json.loads(before["sequences.splits.json"])
    original = registry.read_split(pid, registered["dataset_id"], 1, "train", purpose="training")
    normalized = registry.read_split(pid, registered["dataset_id"], 2, "train", purpose="training")
    assert original["rows"] == rows[:32][::-1]
    assert normalized["rows"][0]["text"] == "sequence 31"
    assert normalized["consumption"]["sample_ids"] == changed["splits"][0]["sample_ids"]
    reference = registry.portable_reference(pid, registered["dataset_id"], 2)
    relocated = tmp_path / "relocated.parquet"
    relocated.write_bytes(before["sequences.parquet"])
    paths = {reference["dependencies"][0]["id"]: str(relocated)}
    assert load_portable_split(reference, paths, "train", purpose="training", expected_project_id=pid) == normalized
    assert reference["manifest"]["provenance"] == manifest["provenance"]
    assert reference["dependencies"][0]["sha256"] == manifest["sources"][0]["sha256"]
    assert str(root) not in json.dumps(reference)


@pytest.mark.parametrize("mutation,code", [("overlap", "dataset_split_overlap"),
    ("schema", "dataset_row_invalid"), ("checksum", "dataset_checksum_mismatch"),
    ("content_overlap", "dataset_content_overlap")])
def test_real_parquet_identity_and_schema_refusals(setup, mutation, code):
    _, registry, project, root = setup
    manifest, rows = write_manifest(root)
    if mutation == "overlap":
        manifest["splits"][1]["sample_ids"].append(manifest["splits"][0]["sample_ids"][0])
    elif mutation == "schema":
        manifest["row_schema"]["properties"]["tokens"]["items"]["type"] = "string"
    elif mutation == "checksum":
        manifest["sources"][0]["sha256"] = "0" * 64
    else:
        rows[32] = {**rows[0], "id": rows[32]["id"]}
        manifest, _ = write_manifest(root, rows=rows)
    result = registry.validate(project["project_id"], manifest)
    assert result["diagnostics"][0]["code"] == code
    assert registry.list(project["project_id"]) == {"datasets": []}


@pytest.mark.parametrize("value", [b"binary", datetime(2026, 1, 1), Decimal("1.25"), float("nan")])
def test_nonportable_arrow_values_rejected_without_coercion(setup, value):
    _, registry, project, root = setup
    manifest, rows = write_manifest(root)
    rows[0]["label"] = value
    for row in rows[1:]:
        row["label"] = value
    manifest, _ = write_manifest(root, rows=rows)
    assert registry.validate(project["project_id"], manifest)["diagnostics"][0]["code"] == "dataset_source_invalid"


def test_corrupt_parquet_returns_structured_diagnostic_not_decoder_exception(setup):
    _, registry, project, root = setup
    manifest, _ = write_manifest(root)
    file = root / "sequences.parquet"
    file.write_bytes(file.read_bytes()[:100])
    manifest["sources"][0]["sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
    result = registry.validate(project["project_id"], manifest)
    assert result["diagnostics"][0]["code"] == "dataset_source_invalid"
    assert str(root) not in json.dumps(result)


def test_rows_and_compression_expansion_rejected_before_batches(setup, monkeypatch):
    service, _, project, root = setup
    manifest, rows = write_manifest(root)
    rows = [{**row, "text": "x" * 8192 + row["id"]} for row in rows]
    manifest, _ = write_manifest(root, rows=rows, use_dictionary=False)
    assert (root / "sequences.parquet").stat().st_size < 24000
    monkeypatch.setattr(parquet.ParquetFile, "iter_batches", lambda *a, **k: pytest.fail("preflight must block decode"))
    for limits in ({"max_rows": 47}, {"max_total_bytes": 24000}):
        registry = DatasetRegistry(service.store, project_roots=service.dataset_roots, **limits)
        assert registry.validate(project["project_id"], manifest)["diagnostics"][0]["code"] == "dataset_limit_exceeded"


def test_dictionary_expansion_and_metadata_allocation_are_bounded(setup):
    service, registry, project, root = setup
    manifest, rows = write_manifest(root)
    rows = [{**row, "text": "x" * 4096} for row in rows]
    manifest, _ = write_manifest(root, rows=rows)
    small = DatasetRegistry(service.store, project_roots=service.dataset_roots, max_total_bytes=40000)
    assert small.validate(project["project_id"], manifest)["diagnostics"][0]["code"] == "dataset_limit_exceeded"
    manifest, _ = write_manifest(root, metadata={b"oversized": b"x" * (1024 * 1024 + 1)})
    assert registry.validate(project["project_id"], manifest)["diagnostics"][0]["code"] == "dataset_source_invalid"


def test_symlink_and_tampered_sources_never_redirect_pinned_bytes(setup):
    _, registry, project, root = setup
    manifest, _ = write_manifest(root)
    record = registry.register(project["project_id"], manifest)
    path = root / "sequences.parquet"
    moved = root / "moved.parquet"
    path.rename(moved)
    try:
        path.symlink_to(moved)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Host lacks existing symlink permission")
        raise
    with pytest.raises(ServiceError) as failure:
        registry.read_split(project["project_id"], record["dataset_id"], 1, "train", purpose="training")
    assert failure.value.code == "dataset_source_unavailable"
    path.unlink()
    path.write_bytes(moved.read_bytes() + b"tampering")
    assert registry.validate(project["project_id"], dataset_id=record["dataset_id"])["diagnostics"][0]["code"] == "dataset_checksum_mismatch"
    assert registry.get(project["project_id"], record["dataset_id"]) == record


async def test_http_mcp_binding_real_admission_and_worker_local_dependency(setup, tmp_path):
    service, registry, project, root = setup
    manifest, rows = write_manifest(root)
    pid = project["project_id"]
    # A tiny independent source anchor, not copied platform code or a dataset.
    source = tmp_path / "source"
    source.mkdir()
    (source / "project.txt").write_text("Independent Parquet dataset admission fixture\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "project.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "Dataset source anchor"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    recipe = service.invoke("recipe.get", {"project_id": pid, "recipe_id": project["recipe_id"]}, OWNER)
    recipe["source"] = {"repository": "urn:sha256:" + "1" * 64, "commit": commit,
                        "files": [{"path": "project.txt", "sha256": hashlib.sha256((source / "project.txt").read_bytes()).hexdigest()}]}
    current = service.invoke("recipe.update", {"project_id": pid, "recipe_id": project["recipe_id"], "recipe": recipe,
                             "expected_revision": 1, "idempotency_key": "source"}, OWNER)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service, tokens={"test": OWNER})),
                                base_url="http://localhost", headers={"Authorization": "Bearer test"}) as client:
        gateway = OperationGateway(client)
        record = await gateway.invoke("dataset.register", {"project_id": pid, "manifest": manifest, "idempotency_key": "data"})
        assert await gateway.invoke("dataset.get", {"project_id": pid, "dataset_id": record["dataset_id"]}) == record
        binding = await gateway.invoke("dataset.bind", {"project_id": pid, "recipe_id": project["recipe_id"],
            "expected_revision": current["revision"], "dataset_id": record["dataset_id"], "dataset_revision": 1,
            "split_id": "train", "purpose": "training", "node_id": "train", "idempotency_key": "bind"})
    assert PYARROW_REQUIREMENT in binding["recipe"]["environment"]["requirements"]
    assert binding["binding"]["split"]["sample_ids"] == manifest["splits"][0]["sample_ids"]
    assert not binding["execution_started"]
    backend = ZenMLBackend(tmp_path / "state", python_executable=sys.executable, source_root=source)
    payload = {"project_id": pid, "recipe": binding["recipe"], "trusted": True}
    prepared = resolve_backend(service, backend, payload)
    plan = prepared.invoke("run.plan", payload)
    assert plan["executable"], plan
    node = binding["recipe"]["nodes"][0]
    reference = registry.portable_reference(pid, record["dataset_id"], 1)
    assert load_portable_split(reference, prepared.dependency_paths, "train", purpose="training",
        source_guards=prepared.dataset_source_guards)["rows"] == rows[:32][::-1]
    assert not list(backend.root.rglob("*.parquet"))
    assert not (backend.root / "jobs").exists()
    # A genuinely isolated interpreter without PyArrow must fail admission.
    bare = tmp_path / "without-parquet"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(bare)], check=True)
    prepared.python = str(bare / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    with pytest.raises(BackendError) as failure:
        validate_dataset_import(prepared, payload, node)
    assert failure.value.code in {"dependency_unavailable", "capability_unavailable"}


def test_admission_checks_pinned_adapter_in_worker_even_if_recipe_omits_it(setup, tmp_path, monkeypatch):
    _, registry, project, root = setup
    manifest, _ = write_manifest(root)
    record = registry.register(project["project_id"], manifest)
    reference = registry.portable_reference(project["project_id"], record["dataset_id"], 1)
    dependency = reference_dependency(reference)
    path = tmp_path / "reference.json"
    path.write_bytes(reference_bytes(reference))
    backend = SimpleNamespace(repository=tmp_path, python=sys.executable,
        dependency_paths={dependency["id"]: str(path), reference["dependencies"][0]["id"]: str(root / "sequences.parquet")})
    payload = {"project_id": project["project_id"], "recipe": {"external_dependencies": [dependency, *reference["dependencies"]]}}
    node = {"id": "data", "parameters": {"reference_id": dependency["id"], "split_id": "train", "purpose": "training"}}
    validate_dataset_import(backend, payload, node)
    from modelbuilder import parquet_reader
    for platform in ("darwin", "win32"):
        monkeypatch.setattr(parquet_reader.sys, "platform", platform)
        with pytest.raises(BackendError) as failure:
            validate_dataset_import(backend, payload, node)
        assert failure.value.code == "capability_unavailable"
        assert "JSONL/CSV" in failure.value.message


def test_multi_source_decoded_budget_is_shared(setup):
    service, _, project, root = setup
    manifest, rows = write_manifest(root)
    rows = [{**row, "text": "x" * 256} for row in rows]
    manifest, _ = write_manifest(root, rows=rows)
    other, _ = write_manifest(root, name="second", rows=[{**row, "id": "second-" + row["id"]} for row in rows])
    other["sources"][0]["id"] = "second"
    manifest["sources"] += other["sources"]
    manifest["splits"] = [{"id": "all", "role": "train", "source_ids": ["sequences", "second"]}]
    limit = 28000
    assert sum((root / source["path"]).stat().st_size for source in manifest["sources"]) < limit
    registry = DatasetRegistry(service.store, project_roots=service.dataset_roots, max_total_bytes=limit)
    assert registry.validate(project["project_id"], manifest)["diagnostics"][0]["code"] == "dataset_limit_exceeded"


def test_column_file_references_and_duplicate_fields_are_rejected_before_pages(setup, monkeypatch):
    _, registry, project, root = setup
    manifest, _ = write_manifest(root)
    file = root / "sequences.parquet"
    metadata = parquet.read_metadata(file)
    metadata.set_file_path("https://example.invalid/unapproved.parquet")
    metadata.write_metadata_file(file)
    manifest["sources"][0]["sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
    monkeypatch.setattr(parquet.ParquetFile, "iter_batches", lambda *a, **k: pytest.fail("must not read pages"))
    assert registry.validate(project["project_id"], manifest)["diagnostics"][0]["code"] == "dataset_source_invalid"
    duplicate = arrow.Table.from_arrays([arrow.array(["one"]), arrow.array(["two"])], names=["id", "id"])
    parquet.write_table(duplicate, file)
    manifest["sources"][0]["sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
    assert registry.validate(project["project_id"], manifest)["diagnostics"][0]["code"] == "dataset_source_invalid"


def test_real_parquet_explicit_dedup_preserves_removed_sample_lineage(setup):
    _, registry, project, root = setup
    _, rows = write_manifest(root)
    rows[1] = {**rows[0], "id": rows[1]["id"]}
    manifest, _ = write_manifest(root, rows=rows)
    manifest["transformations"] = [{"kind": "rows.deduplicate"}]
    record = registry.register(project["project_id"], manifest)
    assert record["row_count"] == 47
    assert record["transformations"][0]["removed_sample_ids"] == ["sequence-01"]
    assert "sequence-01" not in record["splits"][0]["sample_ids"]


def test_incorrect_adapter_version_fails_closed_before_reading_pages(setup, monkeypatch):
    _, registry, project, root = setup
    manifest, _ = write_manifest(root)
    from modelbuilder import parquet_reader
    monkeypatch.setattr(parquet_reader.importlib.metadata, "version", lambda _name: "0.0.0")
    assert registry.validate(project["project_id"], manifest)["diagnostics"][0]["code"] == "dependency_mismatch"


@pytest.mark.parametrize("pin,conflict", [("PyArrow==25.0.0", False), ("pyarrow==24.0.0", True)])
def test_binding_respects_existing_adapter_pin_and_rolls_back_conflicts(setup, pin, conflict):
    service, registry, project, root = setup
    manifest, _ = write_manifest(root)
    pid = project["project_id"]
    record = registry.register(pid, manifest)
    recipe = service.invoke("recipe.get", {"project_id": pid, "recipe_id": project["recipe_id"]}, OWNER)
    recipe["environment"]["requirements"] = [pin]
    current = service.invoke("recipe.update", {"project_id": pid, "recipe_id": project["recipe_id"],
                             "expected_revision": 1, "recipe": recipe, "idempotency_key": "pin"}, OWNER)
    payload = {"project_id": pid, "recipe_id": project["recipe_id"], "expected_revision": current["revision"],
               "dataset_id": record["dataset_id"], "dataset_revision": 1, "split_id": "train", "purpose": "training",
               "node_id": "train", "idempotency_key": "bind"}
    if conflict:
        with pytest.raises(ServiceError) as failure:
            service.invoke("dataset.bind", payload, OWNER)
        assert failure.value.code == "dataset_binding_conflict"
        assert service.invoke("recipe.get", {"project_id": pid, "recipe_id": project["recipe_id"]}, OWNER) == current
    else:
        bound = service.invoke("dataset.bind", payload, OWNER)
        assert bound["recipe"]["environment"]["requirements"] == [pin]
