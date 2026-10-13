"""Two separate, committed model repositories execute through one service and native ZenML store."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import subprocess
import time
import uuid

import pytest
from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.common import read_json
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService
from tests.components.rights_test_support import USAGE_CONTEXT, local_source_review

ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(os.environ.get("MODELBUILDER_BACKEND_PYTHON", str(ROOT / ".venv-backend/bin/python")))
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not PYTHON.is_file(), reason="Pinned backend required")]
OWNER = Actor("owner", {"read", "modify", "execute"})
HARNESS = '''from pathlib import Path
from copy import deepcopy
import hashlib
import json

SLOPE = __SLOPE__

def train(*, inputs, parameters, context):
    weight, bias, start = 0.0, 0.0, 0
    if context.get("resume"):
        saved = json.loads((Path(context["resume"]["path"]) / "state.json").read_text())
        weight, bias = saved["weight"], saved["bias"]
        start = saved["step"] if context["resume_mode"] == "exact_state" else 0
    rows = [(x, SLOPE * x + 1.0) for x in (-2., -1., 0., 1., 2.)]
    for step in range(start, parameters["updates"]):
        residuals = [(weight * x + bias - y, x) for x, y in rows]
        weight -= .1 * 2 * sum(r * x for r, x in residuals) / len(rows)
        bias -= .1 * 2 * sum(r for r, x in residuals) / len(rows)
    loss = sum((weight * x + bias - y) ** 2 for x, y in rows) / len(rows)
    target = Path(context["output_dir"])
    target.mkdir(parents=True, exist_ok=True)
    state = {"weight": weight, "bias": bias, "slope": SLOPE, "step": parameters["updates"]}
    (target / "state.json").write_text(json.dumps(state))
    descriptor = {"schema_version": "modelbuilder.checkpoint/v1", "purpose": "last", "step": state["step"],
        "identities": context["identities"], "resume_modes": ["exact_state", "warm_start"],
        "state": ["model", "optimizer", "scheduler", "rng", "data_cursor"],
        "assets": [{"path": "state.json", "sha256": hashlib.sha256((target / "state.json").read_bytes()).hexdigest()}],
        "metrics": {"loss": loss, "weight": weight}, "rights": context["declared_artifact_rights"]}
    (target / "checkpoint.manifest.json").write_text(json.dumps(descriptor))
    return {"trained": target}
'''


def source_recipe(tmp_path, alias, slope):
    source = tmp_path / alias
    source.mkdir()
    (source / "harness.py").write_text(HARNESS.replace("__SLOPE__", str(slope)))
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Acceptance", "-c", "user.email=acceptance@example.invalid", "commit", "-qm", "Independent affine model"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    digest = hashlib.sha256((source / "harness.py").read_bytes()).hexdigest()
    node = {"id": "train", "kind": "component.run", "implementation_ref": {"module": "harness", "entrypoint": "train",
        "path": "harness.py", "commit": commit, "sha256": digest}, "inputs": [], "outputs": [{"name": "trained", "type": "Path"}],
        "parameters": {"architecture": {"kind": "affine"}, "updates": 120}, "environment_ref": "default", "resource_request": {"gpu": 0},
        "cache_policy": "disabled", "timeout": 30, "retry": {"max_attempts": 1, "backoff_seconds": 0}, "provenance": {},
        "component": {"id": "independent.affine", "version": "1", "protocol": "modelbuilder.component/v1", "role": "training",
            "parameter_schema": {"type": "object"}, "capabilities": {"resume_modes": ["exact_state", "warm_start"],
            "checkpoint_events": False, "export_formats": [], "frameworks": ["python"]}}}
    recipe = {"schema_version": "0", "recipe_id": str(uuid.uuid4()), "project_id": str(uuid.uuid4()), "revision": 1, "name": alias,
        "source": {"repository": "https://example.invalid/" + alias, "commit": commit, "files": [{"path": "harness.py", "sha256": digest}]},
        "nodes": [node], "edges": [], "resources": [], "external_dependencies": [], "environment": {"python": "3.12", "requirements": ["zenml==0.97.0"]},
        "evaluation": {}, "outputs": []}
    _, envelope = local_source_review(source, recipe["source"], recipe["project_id"], policy_id="test-source-" + alias)
    recipe["extensions"] = {"usage_context": deepcopy(USAGE_CONTEXT), "artifact_rights": envelope}
    return source, recipe


def register_source_review(service, project, source, recipe):
    policy, _ = local_source_review(source, recipe["source"], project["project_id"], policy_id="test-source-" + recipe["name"])
    service.store.put("hub_license_policy", policy["policy_id"], project["project_id"], policy)


def wait(service, project, run):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        status = service.invoke("run.status", {"project_id": project["project_id"], "operation_id": run["operation_id"]}, OWNER)
        if status["status"] in {"succeeded", "failed", "cancelled"}:
            assert status["status"] == "succeeded", status
            assert status["backend_run_id"]
            return status
        time.sleep(.25)
    raise AssertionError(status)


def test_two_source_bound_models_native_execution_history_and_resume(tmp_path):
    sources = {alias: source_recipe(tmp_path, alias, slope) for alias, slope in (("ascending", 3), ("descending", -2))}
    roots = {alias: {"path": str(source), "trusted": True} for alias, (source, _) in sources.items()}
    # Undeclared checkout modules must not shadow runtime startup, before source
    # closure staging has a chance to exclude undeclared siblings.
    for source, _ in sources.values():
        (source / "modelbuilder.py").write_text("raise RuntimeError('undeclared platform shadow loaded')")
        (source / "json.py").write_text("raise RuntimeError('undeclared stdlib shadow loaded')")
    backend = ZenMLBackend(tmp_path / "state", python_executable=str(PYTHON))
    service = OperationService(tmp_path / "service.db", backend=backend, source_roots=roots)
    projects, runs = {}, {}
    baseline = backend.repository
    for alias, (source, recipe) in sources.items():
        identity = service.source_workspaces.inspect(alias)
        unpinned = deepcopy(recipe)
        unpinned["source"].update(commit="0" * 40, files=[])
        for node in unpinned["nodes"]:
            node["implementation_ref"]["commit"] = "0" * 40
        project = service.invoke("project.create", {"slug": alias, "model_name": alias, "source_alias": alias,
            "source_repository_id": identity["repository_id"], "trusted": True, "recipe": unpinned, "idempotency_key": alias}, OWNER)
        register_source_review(service, project, source, recipe)
        projects[alias] = project
        fields = {"project_id": project["project_id"], "recipe_id": project["recipe_id"], "expected_revision": 1,
            "expected_project_revision": 1, "source_repository_id": identity["repository_id"],
            "commit": recipe["source"]["commit"], "paths": ["harness.py"]}
        original = service.invoke("recipe.get", {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}, OWNER)
        reviewed = service.invoke("recipe.source.pin.preview", fields, OWNER)
        pinned = service.invoke("recipe.source.pin", {**fields, "review_digest": reviewed["review_digest"],
            "confirm_pin": True, "idempotency_key": alias + "-pin"}, OWNER)
        assert pinned["revision"] == 2 and pinned["execution_allowed"] is False
        assert service.store.list("operation", project["project_id"]) == []
        assert service.invoke("recipe.get", {"project_id": project["project_id"], "recipe_id": project["recipe_id"], "revision": 1}, OWNER) == original
        plan = service.invoke("run.plan", {"project_id": project["project_id"], "recipe_id": project["recipe_id"], "trusted": True}, OWNER)
        assert plan["executable"], plan
        runs[alias] = service.invoke("run.submit", {"project_id": project["project_id"], "recipe_id": project["recipe_id"],
            "expected_revision": 2, "trusted": True, "idempotency_key": alias + "-run"}, OWNER)
        assert backend.repository == baseline
    for alias in sources:
        result = wait(service, projects[alias], runs[alias])
        job = backend.root / "jobs" / result["operation_id"]
        request = read_json(job / "request.json")
        model = read_json(job / "outputs/train/state.json")
        assert abs(model["weight"] - model["slope"]) < 1e-9
        assert abs(model["bias"] - 1) < 1e-9
        assert request["source_root"] == str(sources[alias][0])
        assert request["source_binding"] == projects[alias]["source_binding"]
        assert result["source_workspace"]["workspace_path"] == str(sources[alias][0])
        assert read_json(job / "native.json")["backend_run_id"] == result["backend_run_id"]
        runs[alias] = result
    assert projects["ascending"]["project_id"] != projects["descending"]["project_id"]
    assert runs["ascending"]["backend_run_id"] != runs["descending"]["backend_run_id"]
    with pytest.raises(ServiceError) as error:
        service.invoke("run.status", {"project_id": projects["descending"]["project_id"], "operation_id": runs["ascending"]["operation_id"]}, OWNER)
    assert error.value.code == "not_found"
    with pytest.raises(Exception) as error:
        backend._job({"project_id": projects["descending"]["project_id"], "operation_id": runs["ascending"]["operation_id"]})
    assert error.value.code == "not_found"
    old = projects["ascending"]
    other = service.source_workspaces.inspect("descending")
    rebound = service.invoke("project.source.bind", {"project_id": old["project_id"], "expected_revision": old["revision"],
        "source_alias": "descending", "source_repository_id": other["repository_id"], "trusted": True, "idempotency_key": "rebind"}, OWNER)
    resume = service.invoke("run.resume", {"project_id": old["project_id"], "operation_id": runs["ascending"]["operation_id"],
        "expected_recipe_revision": 2, "mode": "exact_state", "trusted": True, "idempotency_key": "resume"}, OWNER)
    resumed = wait(service, rebound, resume)
    request = read_json(backend.root / "jobs" / resumed["operation_id"] / "request.json")
    assert request["source_root"] == str(sources["ascending"][0])
    assert resumed["source_binding"] == old["source_binding"]
    assert read_json(backend.root / "jobs" / resumed["operation_id"] / "outputs/train/state.json")["slope"] == 3
    service.store.close()
    reopened = OperationService(tmp_path / "service.db", backend=backend, source_roots=roots)
    assert reopened.invoke("project.get", {"project_id": old["project_id"]}, OWNER)["source_binding"] == rebound["source_binding"]
    reopened.source_workspaces.aliases.pop("ascending")
    history = reopened.invoke("run.status", {"project_id": old["project_id"], "operation_id": runs["ascending"]["operation_id"]}, OWNER)
    assert history["status"] == "succeeded" and history["source_binding"] == old["source_binding"]
    with pytest.raises(ServiceError) as error:
        reopened.invoke("run.resume", {"project_id": old["project_id"], "operation_id": runs["ascending"]["operation_id"],
            "expected_recipe_revision": 3, "mode": "exact_state", "trusted": True, "idempotency_key": "revoked-resume"}, OWNER)
    assert error.value.code == "source_not_allowed"
    reopened.store.close()
