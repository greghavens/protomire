"""Fast transport/packaging assertions. These are not the real T19 CPU run."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from modelbuilder.recipe import RecipeValidationError, default_recipe, pin_source_files

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("replay_bundle", ROOT / "scripts/replay_bundle.py")
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


@pytest.mark.parametrize("fixture", ["custom-pytorch", "pretrained-small"])
def test_shipping_recipe_has_complete_verified_source_closure(fixture):
    recipe = json.loads((ROOT / "examples" / fixture / "recipe.json").read_text())
    inventory = replay.source_inventory(ROOT)
    assert {item["path"] for item in recipe["source"]["files"]} == set(inventory)
    pinned = pin_source_files(recipe, ROOT, inventory, recipe["source"]["commit"])
    assert pinned == recipe


@pytest.fixture
def snapshot(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    files = {
        "src/modelbuilder/__init__.py": b'__version__ = "0.1.0"\n',
        "src/modelbuilder/backend/__init__.py": b"",
        "src/modelbuilder/backend/placement.py": b"DEVICE = 'cpu'\n",
        "src/modelbuilder/evaluation.py": b"SCHEMA = 'test'\n",
        "src/modelbuilder/tracking.py": b"ENABLED = False\n",
        "src/modelbuilder/recipe.py": b"VERSION = 0\n",
        "src/modelbuilder/backend/adapter.py": b'raise RuntimeError("must not execute during import or wheel build")\n',
        "src/modelbuilder/service.py": b'raise RuntimeError("must not execute")\n',
        "pyproject.toml": b'[project]\nversion = "0.1.0"\ndependencies = ["jsonschema==4.23.0"]\n',
    }
    recipe = default_recipe("project", "recipe")
    recipe["source"]["commit"] = "a" * 40  # Test identity only, never evidence of a source revision.
    recipe["source"]["files"] = []
    recipe["environment"]["requirements"] = ["zenml[server]==0.97.0"]
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        recipe["source"]["files"].append({"path": name, "sha256": hashlib.sha256(content).hexdigest()})
    (root / "recipe.json").write_text(json.dumps(recipe))
    return root


def test_service_wheel_is_deterministic_inert_and_complete(snapshot, tmp_path):
    first = replay.build_wheel(snapshot, tmp_path / "first", "service")
    second = replay.build_wheel(snapshot, tmp_path / "second", "service")
    assert first["sha256"] == second["sha256"]
    assert first["build_hooks_executed"] is False
    with zipfile.ZipFile(first["wheel"]) as wheel:
        assert "modelbuilder/service.py" in wheel.namelist()
        assert "modelbuilder/backend/adapter.py" in wheel.namelist()
        assert wheel.read("modelbuilder/backend/placement.py") == b"DEVICE = 'cpu'\n"
        metadata = wheel.read("modelbuilder-0.1.0.dist-info/METADATA").decode()
        assert "Requires-Dist: jsonschema==4.23.0" in metadata
        assert "zenml" not in metadata
        assert all(not name.endswith(".pth") for name in wheel.namelist())


def test_runtime_wheel_separates_dependency_metadata(snapshot, tmp_path):
    result = replay.build_wheel(snapshot, tmp_path / "wheels", "backend")
    with zipfile.ZipFile(result["wheel"]) as wheel:
        assert "modelbuilder/service.py" not in wheel.namelist()
        for dependency in ["evaluation.py", "tracking.py", "recipe.py"]:
            assert "modelbuilder/" + dependency in wheel.namelist()
        assert "modelbuilder/backend/adapter.py" in wheel.namelist()
        assert wheel.read("modelbuilder/backend/placement.py") == b"DEVICE = 'cpu'\n"
        metadata = wheel.read("modelbuilder_runtime-0.1.0.dist-info/METADATA").decode()
        assert "Requires-Dist: zenml[server]==0.97.0" in metadata
        assert "jsonschema" not in metadata


def test_tampered_source_cannot_become_a_wheel(snapshot, tmp_path):
    (snapshot / "src/modelbuilder/backend/adapter.py").write_text("changed = True\n")
    with pytest.raises(RecipeValidationError, match="Source bytes"):
        replay.build_wheel(snapshot, tmp_path / "wheel", "backend")


def test_execution_requires_separate_explicit_trust(tmp_path):
    with pytest.raises(ValueError, match="trust-repository-source"):
        replay.run_recipe(tmp_path / "source", tmp_path / "data", Path("/not-used"), tmp_path / "output.json")
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("cancel_fails", [False, True])
def test_timeout_uses_valid_cancel_contract_and_always_records_evidence(snapshot, tmp_path, monkeypatch, cancel_fails):
    """Real operation validation with a fake process boundary; no runtime claim."""
    import modelbuilder.backend
    calls = []

    class BoundaryBackend:
        def prepare_execution(self, operation, payload):
            return operation, payload

        def launch_prepared(self, prepared, authorize=None):
            if authorize:
                authorize()
            return self.invoke(*prepared, None)

        def invoke(self, operation, payload, actor):
            calls.append((operation, payload))
            if operation == "run.cancel" and cancel_fails:
                raise RuntimeError("Simulated transport loss")
            return {"operation_id": payload["operation_id"], "backend_run_id": "native-contract-test",
                    "status": "cancel_requested" if operation == "run.cancel" else "running", "worker_alive": True}

    monkeypatch.setattr(modelbuilder.backend, "ZenMLBackend", lambda *args, **kwargs: BoundaryBackend())
    output = tmp_path / "timeout.json"
    with pytest.raises(TimeoutError, match="timed out"):
        replay.run_recipe(snapshot, tmp_path / "data", Path("/unused-python"), output, trusted=True, timeout=0)
    evidence = json.loads(output.read_text())
    assert evidence["status"] == "FAIL" and evidence["reason"] == "timeout"
    cancellation = next(payload for operation, payload in calls if operation == "run.cancel")
    assert cancellation["idempotency_key"] == "portable:timeout-cancel"
    if cancel_fails:
        assert evidence["termination_confirmed"] is False and evidence["cancellation_error"]
    else:
        assert evidence["cancellation"]["status"] == "cancel_requested"


def result(root):
    identity = {"prefix": root + "/env", "python": "3.12.14", "distributions": {"modelbuilder": "0.1.0"},
                "pythonpath": None, "modules": {"modelbuilder": root + "/env/lib/modelbuilder/__init__.py"},
                "sys_path": [root + "/env/lib"], "pth_files": {},
                "store": {name: root + "/" + name for name in
                          ("stack_id", "artifact_store_id", "artifact_store_path", "metadata_url", "zenml_config_path")}}
    return {"status": "PASS", "source_commit": "a" * 40, "transport_recipe_digest": "b" * 64,
            "source_root": root + "/source", "data_dir": root + "/data", "service_identity": copy.deepcopy(identity),
            "backend_identity": copy.deepcopy(identity), "output_sha256": {"train/checkpoint.pt": "c" * 64},
            "recipe_identity": {"project_recipe_id": "owned", "imported_recipe_id": "owned", "listed_recipe_ids": ["owned"],
                                "initial_revision": 1, "imported_revision": 2, "run_recipe_revision": 2,
                                "original_revision_unchanged": True, "run_recipe_id": "owned",
                                "imported_recipe_digest": "digest", "run_recipe_digest": "digest",
                                "native_executed_recipe_digest": "digest"},
            "run": {"metrics": {"dataset_sha256": "d" * 64, "validation_loss": 0.2}, "backend_run_id": root}}


def test_independent_identical_replay_passes():
    assert replay.compare_results(result("/origin"), result("/replay"))["status"] == "PASS"


def catalog_result(root):
    record = result(root)
    row = {"artifact_id": root + "/artifact", "backend_run_id": root,
           "model_version_ref": root + "/version", "node_id": "evaluate",
           "output_port": "metrics", "metric": "validation_loss",
           "evaluation_digest": "e" * 64, "value": 0.2}
    record["run"]["metrics"]["evaluation_catalog"] = [copy.deepcopy(row)]
    record["run"]["evaluations"] = [{**row, "record": {"digest": "e" * 64}}]
    record["run"]["model_version_ref"] = row["model_version_ref"]
    record["run"]["artifacts"] = [{"artifact_id": row["artifact_id"], "backend_run_id": root,
                                  "node_id": "evaluate", "port": "metrics"}]
    return record


def test_replay_catalog_compares_measurements_with_independent_native_references():
    origin, repeated = catalog_result("/origin"), catalog_result("/replay")
    before = copy.deepcopy((origin, repeated))
    comparison = replay.compare_results(origin, repeated)
    assert comparison["status"] == "PASS"
    assert comparison["checks"]["metrics_equal"]
    assert comparison["checks"]["origin_evaluation_catalog_owned"]
    assert comparison["checks"]["replay_evaluation_catalog_owned"]
    assert (origin, repeated) == before


def pending_catalog_result(root):
    record = catalog_result(root)
    for row in (record["run"]["metrics"]["evaluation_catalog"][0], record["run"]["evaluations"][0]):
        row["model_version_ref"] = None
        row["checkpoint_verification"] = {"verified": False, "code": "checkpoint_verification_pending",
            "message": "Checkpoint identity and ownership are verified on explicit comparison or promotion"}
    return record


def test_replay_pending_checkpoint_attribution_is_preserved_without_inventing_a_version():
    origin, repeated = pending_catalog_result("/origin"), pending_catalog_result("/replay")
    assert replay.compare_results(origin, repeated)["status"] == "PASS"
    repeated["run"]["metrics"]["evaluation_catalog"][0]["model_version_ref"] = "/replay/version"
    repeated["run"]["evaluations"][0]["model_version_ref"] = "/replay/version"
    assert replay.compare_results(origin, repeated)["status"] == "FAIL"


def test_replay_checkpoint_verification_diagnostics_remain_meaningful():
    origin, repeated = pending_catalog_result("/origin"), pending_catalog_result("/replay")
    for row in (repeated["run"]["metrics"]["evaluation_catalog"][0], repeated["run"]["evaluations"][0]):
        row["checkpoint_verification"]["code"] = "checkpoint_identity_mismatch"
    comparison = replay.compare_results(origin, repeated)
    assert comparison["checks"]["metrics_equal"] is False
    assert comparison["status"] == "FAIL"


def test_replay_unexplained_null_model_version_is_rejected():
    origin, repeated = catalog_result("/origin"), catalog_result("/replay")
    for row in (repeated["run"]["metrics"]["evaluation_catalog"][0], repeated["run"]["evaluations"][0]):
        row["model_version_ref"] = None
    assert replay.compare_results(origin, repeated)["status"] == "FAIL"


@pytest.mark.parametrize("field,value", [("evaluation_digest", "f" * 64), ("value", 0.3),
                                       ("metric", "other"), ("node_id", "other"),
                                       ("output_port", "other"), ("future_semantic_field", "changed")])
def test_replay_catalog_still_rejects_semantic_drift(field, value):
    origin, repeated = catalog_result("/origin"), catalog_result("/replay")
    repeated["run"]["metrics"]["evaluation_catalog"][0][field] = value
    comparison = replay.compare_results(origin, repeated)
    assert comparison["status"] == "FAIL"
    assert comparison["checks"]["metrics_equal"] is False


@pytest.mark.parametrize("field", ["artifact_id", "backend_run_id", "model_version_ref"])
def test_replay_catalog_rejects_unowned_native_reference(field):
    origin, repeated = catalog_result("/origin"), catalog_result("/replay")
    repeated["run"]["metrics"]["evaluation_catalog"][0][field] = origin["run"]["metrics"]["evaluation_catalog"][0][field]
    comparison = replay.compare_results(origin, repeated)
    assert comparison["status"] == "FAIL"
    assert comparison["checks"]["metrics_equal"] is True
    assert comparison["checks"]["replay_evaluation_catalog_owned"] is False


@pytest.mark.parametrize("catalog", [None, {}, [None], [{}]])
def test_replay_catalog_rejects_malformed_evidence(catalog):
    origin, repeated = catalog_result("/origin"), catalog_result("/replay")
    repeated["run"]["metrics"]["evaluation_catalog"] = catalog
    assert replay.compare_results(origin, repeated)["status"] == "FAIL"


def test_replay_catalog_rejects_duplicate_or_missing_native_artifact():
    origin, repeated = catalog_result("/origin"), catalog_result("/replay")
    repeated["run"]["artifacts"].append(copy.deepcopy(repeated["run"]["artifacts"][0]))
    assert replay.compare_results(origin, repeated)["status"] == "FAIL"
    repeated["run"]["artifacts"] = []
    assert replay.compare_results(origin, repeated)["status"] == "FAIL"
    repeated = catalog_result("/replay")
    repeated["run"]["metrics"]["evaluation_catalog"] *= 2
    assert replay.compare_results(origin, repeated)["status"] == "FAIL"


@pytest.mark.parametrize("field", ["artifact_id", "model_version_ref"])
def test_replay_catalog_rejects_shared_native_ids_even_with_matching_local_evidence(field):
    origin, repeated = catalog_result("/origin"), catalog_result("/replay")
    value = origin["run"]["metrics"]["evaluation_catalog"][0][field]
    repeated["run"]["metrics"]["evaluation_catalog"][0][field] = value
    repeated["run"]["evaluations"][0][field] = value
    if field == "artifact_id":
        repeated["run"]["artifacts"][0][field] = value
    else:
        repeated["run"][field] = value
    comparison = replay.compare_results(origin, repeated)
    assert comparison["checks"]["replay_evaluation_catalog_owned"] is True
    assert comparison["checks"]["independent_catalog_references"] is False
    assert comparison["status"] == "FAIL"


def test_replay_catalog_rejects_model_version_not_owned_by_run_or_checkpoint():
    origin, repeated = catalog_result("/origin"), catalog_result("/replay")
    repeated["run"]["model_version_ref"] = "another-version"
    assert replay.compare_results(origin, repeated)["status"] == "FAIL"


@pytest.mark.parametrize("section", ["record", "checkpoint_bindings"])
def test_replay_catalog_keeps_complete_record_and_checkpoint_binding_semantics(section):
    origin, repeated = catalog_result("/origin"), catalog_result("/replay")
    if section == "record":
        repeated["run"]["evaluations"][0][section]["treatment"] = {"checkpoint": "changed"}
    else:
        repeated["run"]["evaluations"][0][section] = [{
            "producer_model_version_ref": repeated["run"]["model_version_ref"],
            "content_sha256": "changed", "source_port": "checkpoint", "input_port": "candidate"}]
    comparison = replay.compare_results(origin, repeated)
    assert comparison["checks"]["metrics_equal"] is True
    assert comparison["checks"]["evaluation_records_equal"] is False
    assert comparison["status"] == "FAIL"


def test_same_environment_is_not_clean_replay():
    same = result("/origin")
    comparison = replay.compare_results(same, same)
    assert comparison["status"] == "FAIL"
    assert comparison["checks"]["backend_identity_independent_prefix"] is False


@pytest.mark.parametrize("field,value", [("imported_recipe_id", "new-independent-recipe"),
                                       ("listed_recipe_ids", ["owned", "another"]),
                                       ("original_revision_unchanged", False), ("imported_revision", 1),
                                       ("run_recipe_id", "another"), ("run_recipe_revision", 1),
                                       ("native_executed_recipe_digest", "different")])
def test_replay_rejects_recipe_identity_or_revision_drift(field, value):
    origin, repeated = result("/origin"), result("/replay")
    repeated["recipe_identity"][field] = value
    comparison = replay.compare_results(origin, repeated)
    assert comparison["status"] == "FAIL"
    assert comparison["checks"]["replay_single_owned_recipe_identity"] is False


@pytest.mark.parametrize("defect", ["source_path", "shared_pth", "module_path", "pythonpath", "metrics", "hashes", "store"])
def test_replay_evidence_rejects_contamination_or_drift(defect):
    origin, repeated = result("/origin"), result("/replay")
    identity = repeated["backend_identity"]
    if defect == "source_path":
        identity["sys_path"].append("/origin/source/src")
    elif defect == "shared_pth":
        identity["pth_files"]["source.pth"] = "/origin/source/src\n"
    elif defect == "module_path":
        identity["modules"]["modelbuilder"] = "/origin/source/src/modelbuilder/__init__.py"
    elif defect == "pythonpath":
        identity["pythonpath"] = "/another/source"
    elif defect == "metrics":
        repeated["run"]["metrics"]["validation_loss"] = 0.3
    elif defect == "hashes":
        repeated["output_sha256"]["train/checkpoint.pt"] = "e" * 64
    elif defect == "store":
        identity["store"]["metadata_url"] = origin["backend_identity"]["store"]["metadata_url"]
    assert replay.compare_results(origin, repeated)["status"] == "FAIL"


@pytest.mark.parametrize("kind", ["backend", "service"])
def test_runtime_wheel_contains_integrated_module_closure_and_imports_without_source_checkout(tmp_path, kind):
    source = tmp_path / "snapshot"
    source.mkdir()
    shutil.copytree(ROOT / "src/modelbuilder", source / "src/modelbuilder", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    recipe = default_recipe("fixture-project", "fixture-recipe", "Runtime wheel closure")
    # This is an inert verified transport snapshot, not evidence of Git ancestry.
    recipe["source"]["commit"] = "a" * 40
    recipe["source"]["files"] = [{"path": str(path.relative_to(source)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in sorted(source.rglob("*.py"))] + [{"path": "pyproject.toml",
        "sha256": hashlib.sha256((source / "pyproject.toml").read_bytes()).hexdigest()}]
    recipe["environment"]["requirements"] = ["zenml[server]==0.97.0"]
    (source / "recipe.json").write_text(json.dumps(recipe))
    wheel = replay.build_wheel(source, tmp_path / "wheels", kind)
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel["wheel"]) as archive:
        for path in ["platform_file_access.py", "source_workspace.py", "recipe_registry_contract.py",
                     "remote/__init__.py", "remote/contracts.py", "remote/operation_contract.py",
                     "source_pinning_contract.py", "export_packages.py", "backend/native_exports.py",
                     "execution.py", "conditions.py", "backend/conditional_worker.py", "compute_discovery.py", "backend/native_inputs.py",
                     "parquet_reader.py", "parquet_decode.py",
                     "backend/evaluation_binding.py", "backend/promotion.py"]:
            assert "modelbuilder/" + path in archive.namelist()
        for path in ("service.py", "dataset_preparation.py", "research_scheduler.py", "literature_fetch.py"):
            assert ("modelbuilder/" + path in archive.namelist()) == (kind == "service")
        archive.extractall(installed)
    sample = tmp_path / "sample.parquet"
    if os.environ.get("MODELBUILDER_REQUIRE_PARQUET") == "1" or importlib.util.find_spec("pyarrow"):
        import pyarrow as arrow
        import pyarrow.parquet as parquet
        parquet.write_table(arrow.table({"id": ["installed"], "text": ["bounded decoder"]}), sample)
    probe = """
import importlib, importlib.util, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
for name in ('modelbuilder.dataset', 'modelbuilder.models', 'modelbuilder.source_workspace',
             'modelbuilder.backend.admission', 'modelbuilder.backend.operator_config',
             'modelbuilder.export_packages', 'modelbuilder.backend.native_exports',
             'modelbuilder.execution', 'modelbuilder.conditions', 'modelbuilder.backend.native_inputs',
             'modelbuilder.parquet_reader',
             'modelbuilder.backend.evaluation_binding', 'modelbuilder.backend.promotion',
             'modelbuilder.remote.operation_contract', 'modelbuilder.remote.provider'):
    module = importlib.import_module(name)
    assert pathlib.Path(module.__file__).resolve().is_relative_to(root), name
if sys.argv[2] == 'service':
    import json, subprocess
    for name in ('modelbuilder.service', 'modelbuilder.api', 'modelbuilder.research_scheduler', 'modelbuilder.literature_fetch'):
        module = importlib.import_module(name)
        assert pathlib.Path(module.__file__).resolve().is_relative_to(root), name
    helper = root / 'modelbuilder/literature_fetch.py'
    # Invalid input proves the standalone installed helper launches without network.
    child = subprocess.run([sys.executable, '-I', str(helper)], input=json.dumps({'query':'','limit':1}),
                           text=True, capture_output=True, timeout=5)
    assert child.returncode == 1 and child.stdout == ''
else:
    assert importlib.util.find_spec('modelbuilder.service') is None
sample = pathlib.Path(sys.argv[3])
if sample.exists():
    from modelbuilder.parquet_reader import read_parquet
    assert read_parquet(sample.read_bytes(), max_file_bytes=100000, max_rows=100, decoded_budget=100000) == [
        {'id': 'installed', 'text': 'bounded decoder'}]
from modelbuilder.remote.provider import diagnostics
assert diagnostics()['remote_execution_enabled'] is False
print('Isolated runtime wheel import closure passed')
"""
    # Import closure is service-safe and must not require the optional native runtime.
    result = subprocess.run([sys.executable, "-I", "-c", probe, str(installed), kind, str(sample)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Isolated runtime wheel import closure passed" in result.stdout
