"""Fast transport/packaging assertions. These are not the real T19 CPU run."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
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
