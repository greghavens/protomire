"""Real native ZenML graph crossing operator-selected interpreter environments."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.components import read_checkpoint
from modelbuilder.backend.runner import inspect_environment
from modelbuilder.models import Actor
from modelbuilder.service import OperationService
from rights_test_support import local_source_review, USAGE_CONTEXT
from modelbuilder.artifact_rights import combine_parent_rights

ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(os.environ.get("MODELBUILDER_RUNNER_TEST_PYTHON", ROOT / ".venv-backend/bin/python"))
pytestmark = [pytest.mark.integration, pytest.mark.skipif(sys.platform != "linux", reason="Linux lifecycle acceptance"),
              pytest.mark.skipif(not PYTHON.is_file(), reason="Existing CPU backend interpreter required")]


def runner_node(name, role, alias, inputs, outputs, reference, profile):
    distribution = "torch" if role == "training" else "jsonschema"
    return {"id": name, "kind": "component.run", "implementation_ref": {**reference, "entrypoint": name},
        "inputs": inputs, "outputs": outputs, "parameters": {}, "environment_ref": alias,
        "resource_request": {"gpu": 0}, "retry": {"max_attempts": 1, "backoff_seconds": 0},
        "timeout": 60, "cache_policy": "disabled", "provenance": {},
        "component": {"id": "independent." + name, "version": "1", "protocol": "modelbuilder.component/v1",
            "role": role, "parameter_schema": {"type": "object", "additionalProperties": False},
            "capabilities": {"frameworks": ["pytorch"] if role == "training" else [], "devices": ["cpu"],
                             "resume_modes": ["warm_start"] if role == "training" else [], "export_formats": []},
            "runner": {"protocol": "modelbuilder.runner/v1",
                "stack": [{"id": distribution, "category": "framework" if role == "training" else "training-library",
                           "distribution": distribution, "version": profile["packages"][distribution]}],
                "launcher": {"kind": "python", "processes": 1},
                "contracts": {"model": "source-owned-native", "dataset": "native-component-inputs",
                              "checkpoint": "modelbuilder.checkpoint/v1", "evaluation": "modelbuilder.evaluation/v1"}}}}


def test_native_scratch_training_crosses_environments_and_preserves_artifacts(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "harness.py").write_bytes(Path(__file__).with_name("runner_training_harness.py").read_bytes())
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Independent scratch source"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    digest = hashlib.sha256((source / "harness.py").read_bytes()).hexdigest()
    reference = {"path": "harness.py", "sha256": digest, "module": "scratch_harness", "commit": commit}
    registered, paths, resources, profiles = {}, {}, [], {}
    for alias, python in [("tensor", str(PYTHON)), ("plain", sys.executable)]:
        profile = inspect_environment(python)
        path = tmp_path / (alias + ".json")
        path.write_text(json.dumps(profile, sort_keys=True))
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        registered[alias] = {"python_executable": python, "profile_sha256": sha}
        profiles[alias] = profile
        paths[alias] = str(path)
        resources.append({"id": alias, "kind": "environment", "url": "urn:sha256:" + sha,
                          "sha256": sha, "license": "test-only"})
    assert "torch" in profiles["tensor"]["packages"]
    assert "torch" not in profiles["plain"]["packages"], "Run in the service interpreter to prove dependency separation"
    data, trained, metrics = {"name": "data", "type": "dict"}, {"name": "trained", "type": "Path"}, {"name": "metrics", "type": "dict"}
    nodes = [runner_node("data", "dataset", "plain", [], [data], reference, profiles["plain"]),
             runner_node("train", "training", "tensor", [data], [trained], reference, profiles["tensor"]),
             runner_node("evaluate", "evaluation", "plain", [data, trained], [metrics], reference, profiles["plain"])]
    source_contract = {"repository": "urn:sha256:" + digest, "commit": commit,
                       "files": [{"path": "harness.py", "sha256": digest}]}
    _, declared_rights = local_source_review(source, source_contract)
    nodes[0]["provenance"]["intended_use"] = "train"
    recipe = {"schema_version": "0", "recipe_id": "scratch", "project_id": "pending", "revision": 1,
        "name": "Scratch regression via existing framework", "source": source_contract,
        "nodes": nodes, "edges": [
            {"source_node": "data", "source_port": "data", "target_node": target, "target_port": "data"} for target in ("train", "evaluate")]
            + [{"source_node": "train", "source_port": "trained", "target_node": "evaluate", "target_port": "trained"}],
        "resources": resources, "external_dependencies": [], "environment": {"python": "3.12", "requirements": ["zenml==0.97.0"]},
        "evaluation": {}, "outputs": [],
        "extensions": {"artifact_rights": declared_rights, "usage_context": USAGE_CONTEXT}}
    backend = ZenMLBackend(tmp_path / "state", python_executable=str(PYTHON), source_root=source,
                           trusted_code_roots=[str(source)], dependency_paths=paths, runner_environments=registered)
    service = OperationService(tmp_path / "service.sqlite3", backend=backend, source_root=source)
    actor = Actor("owner", {"read", "modify", "execute"})
    try:
        project = service.invoke("project.create", {"slug": "runner-scratch", "model_name": "User scratch model",
            "recipe": recipe, "idempotency_key": "create"}, actor)
        policy, _ = local_source_review(source, source_contract, project["project_id"])
        service.store.put("hub_license_policy", policy["policy_id"], project["project_id"], policy)
        service.backend = backend.with_execution_rights(project_id=project["project_id"],
            policies={policy["policy_id"]: policy}, policy_store_path=service.store.path)
        request = {"project_id": project["project_id"], "recipe_id": project["recipe_id"], "trusted": True}
        before = sorted(str(path) for path in source.rglob("*"))
        plan = service.invoke("run.plan", request, actor)
        assert plan["executable"], plan
        assert sorted(str(path) for path in source.rglob("*")) == before
        assert not (backend.root / "jobs").exists()
        submitted = service.invoke("run.submit", {**request, "expected_revision": 1, "idempotency_key": "run"}, actor)
        operation = {"project_id": project["project_id"], "operation_id": submitted["operation_id"]}
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = service.invoke("run.status", operation, actor)
            if status["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(.1)
        job = backend.root / "jobs" / submitted["operation_id"]
        assert status["status"] == "succeeded", (status, (job / "worker.log").read_text()[-12000:])
        assert status["backend_run_id"]
        checkpoint = read_checkpoint(job / "outputs/train")
        assert checkpoint["rights"] == combine_parent_rights([{
            "artifact_id": "recipe-declared-origin", "rights": declared_rights}])
        assert "rights_admission" in checkpoint["identities"]
        assert checkpoint["resume_modes"] == ["warm_start"] and checkpoint["state"] == ["model"]
        weights = json.loads((job / "outputs/train/weights.json").read_text())
        assert weights["changed"] > 0 and weights["python"] == str(PYTHON)
        owners = [json.loads(path.read_text()) for path in (job / "processes").glob("*/owner.json")]
        assert len(owners) == 3 and len({row["pid"] for row in owners}) == 3
        assert all(row["reaped"] and row["exit_code"] == 0 and not row["descendant_cleanup"]["remaining_pids"] for row in owners)
        artifacts = service.invoke("run.artifacts", operation, actor)["artifacts"]
        assert len(artifacts) >= 3 and all(item["artifact_id"] for item in artifacts)
        assert status["metrics"]["parameter_change"] > 0
        assert status["metrics"]["torch_imported"] is False
        assert status["metrics"]["python"] == sys.executable
        # Receipt/environment differences are part of immutable execution data,
        # never rewritten into a global platform dependency environment.
        journal = json.loads((job / "request.json").read_text())
        assert journal["runner_environments"]["train"]["environment_ref"] == "tensor"
        assert journal["runner_environments"]["evaluate"]["environment_ref"] == "plain"
        resumed = service.invoke("run.resume", {**operation, "mode": "warm_start", "trusted": True,
            "expected_recipe_revision": 1, "idempotency_key": "warm-start"}, actor)
        resume_operation = {**operation, "operation_id": resumed["operation_id"]}
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            resumed_status = service.invoke("run.status", resume_operation, actor)
            if resumed_status["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(.1)
        assert resumed_status["status"] == "succeeded", resumed_status
        resumed_job = backend.root / "jobs" / resumed["operation_id"]
        warmed = json.loads((resumed_job / "outputs/train/weights.json").read_text())
        assert warmed["weights"] != weights["weights"]
        assert json.loads((resumed_job / "request.json").read_text())["parent_run_id"] == status["backend_run_id"]
        changed = deepcopy(recipe)
        changed["nodes"][1]["component"]["capabilities"]["resume_modes"] = ["exact_state"]
        from modelbuilder.backend.components import validate_component
        with pytest.raises(ValueError, match="exact-state"):
            validate_component(changed["nodes"][1])
    finally:
        service.store.close()
