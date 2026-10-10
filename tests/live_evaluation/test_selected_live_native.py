"""Actual exact-node native run preserves explicitly selected live evaluation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import pytest
from fastapi.testclient import TestClient
from modelbuilder.api import create_app
from modelbuilder.models import Actor
from modelbuilder.service import OperationService
from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.common import read_json

ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(os.environ.get("MODELBUILDER_BACKEND_PYTHON", str(ROOT / ".venv-backend/bin/python")))
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not PYTHON.is_file(), reason="Pinned native backend required")]


@pytest.mark.parametrize("boundary_inputs,trainer_only", [(False, False), (True, False), (False, True)])
def test_selected_training_evaluator_cut_publishes_scores_before_later_training_checkpoint(tmp_path, boundary_inputs, trainer_only):
    source = tmp_path / "fixture-source"
    fixture = source / "src/modelbuilder/backend/fixtures.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes((ROOT / "src/modelbuilder/backend/fixtures.py").read_bytes())
    lock = source / "requirements/backend.lock"
    lock.parent.mkdir()
    lock.write_bytes((ROOT / "requirements/backend.lock").read_bytes())
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Acceptance", "-c", "user.email=acceptance@example.invalid",
                    "commit", "-qm", "Pinned local fixture for selected live test"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    recipe = json.loads((ROOT / "examples/custom-pytorch/recipe.json").read_text())
    recipe.pop("digest", None)
    recipe["source"].update(commit=commit, files=[{"path": str(fixture.relative_to(source)), "sha256": hashlib.sha256(fixture.read_bytes()).hexdigest()}])
    recipe["evaluation"]["live"] = {"enabled": True, "max_concurrency": 1, "timeout_seconds": 30}
    for node in recipe["nodes"]:
        if isinstance(node.get("implementation_ref"), dict):
            node["implementation_ref"]["commit"] = commit
        if node["kind"] == "model.train":
            node["parameters"].update(steps=4 if trainer_only else 12, step_delay=1.2)
    recipe.pop("project_id", None)
    recipe.pop("recipe_id", None)
    backend = ZenMLBackend(tmp_path / "native", python_executable=str(PYTHON), source_root=source)
    service = OperationService(tmp_path / "service.sqlite", backend)
    client = TestClient(create_app(service, {"test-owner": Actor("owner", {"read", "modify", "execute"})}))
    def call(operation, **payload):
        response = client.post("/v1/operations/" + operation, json=payload, headers={"Authorization": "Bearer test-owner"})
        assert response.status_code == 200, response.text
        return response.json()
    project = call("project.create", slug="selected-live", model_name="CPU fixture", recipe=recipe, idempotency_key="project")
    recipe = call("recipe.get", project_id=project["project_id"], recipe_id=project["recipe_id"])
    base = {"project_id": project["project_id"], "recipe_id": project["recipe_id"], "expected_revision": recipe["revision"], "trusted": True}
    nodes, pins = ["data", "initial", "train", "evaluate"], []
    if trainer_only:
        nodes.remove("evaluate")
    if boundary_inputs:
        data_run = call("run.submit", **base, execution={"node_ids": ["data"], "input_bindings": []}, idempotency_key="data-only")
        data_identity = {"project_id": project["project_id"], "operation_id": data_run["operation_id"]}
        data_job = backend.root / "jobs" / data_run["operation_id"]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not read_json(data_job / "state.json").get("worker_reaped"):
            time.sleep(.1)
        assert call("run.status", **data_identity)["status"] == "succeeded"
        artifact = call("run.artifacts", **data_identity, include_binding_descriptors=True)["artifacts"][0]
        descriptor = artifact["binding_descriptor"]
        pins = [{"node_id": node, "input_port": "data", **{key: descriptor[key] for key in
            ("backend_run_id", "source_node_id", "source_port", "artifact_id", "content_sha256")}} for node in ("initial", "train", "evaluate")]
        nodes.remove("data")
    payload = {**base, "execution": {"node_ids": nodes, "input_bindings": pins}}
    plan = call("run.plan", **payload)
    assert plan["executable"], plan["blocked_reasons"]
    submitted = call("run.submit", **payload, idempotency_key="selected-live")
    job = backend.root / "jobs" / submitted["operation_id"]
    identity = {"project_id": project["project_id"], "operation_id": submitted["operation_id"]}
    seen, latest_ready = None, None
    try:
        deadline = time.monotonic() + 100
        if trainer_only:
            while time.monotonic() < deadline and not read_json(job / "state.json").get("worker_reaped"):
                time.sleep(.1)
            final = call("run.status", **identity)
            assert final["status"] == "succeeded", final
            assert set(final["executed_node_ids"]) == set(nodes)
            assert call("run.evaluations.list", **identity)["evaluations"] == []
            assert not (job / "outputs/evaluate").exists()
            assert not (job / "outputs/export").exists()
            assert read_json(job / "request.json")["recipe"] == recipe
            assert call("recipe.get", project_id=project["project_id"], recipe_id=project["recipe_id"]) == recipe
            (tmp_path / "acceptance.json").write_text(json.dumps({"trainer_only": True, "final": final, "hidden_evaluators": False}, indent=2))
            return
        while time.monotonic() < deadline:
            evaluated = call("run.evaluations.list", **identity)
            events = call("run.events.list", **identity, limit=200)["events"]
            completed = [row for row in evaluated["evaluations"] if row["status"] == "completed"]
            ready = [row for row in events if row["kind"] == "checkpoint.ready"]
            if len(completed) >= 2 and ready and max(row["step"] for row in ready) < 12:
                seen, latest_ready = completed, max(row["step"] for row in ready)
                break
            if read_json(job / "state.json").get("worker_reaped"):
                break
            time.sleep(.05)
        assert seen is not None, (evaluated, events, (job / "worker.log").read_text()[-6000:])
        observed_ids = {row["event_id"] for row in events}
        for row in seen:
            assert row["evaluation_records"]["validation_loss"]["treatment"]["checkpoint"] == row["checkpoint_digest"]
        while time.monotonic() < deadline:
            state = read_json(job / "state.json")
            if state.get("worker_reaped"):
                break
            time.sleep(.1)
        final = call("run.status", **identity)
        assert final["status"] == "succeeded", final
        assert set(final["executed_node_ids"]) == set(nodes)
        assert not (job / "outputs/export").exists()
        if boundary_inputs:
            assert not (job / "outputs/data").exists()
        assert len([row for row in events if row["kind"] == "evaluation.completed"]) >= 2
        later = call("run.events.list", **identity, limit=200)["events"]
        assert any(row["kind"] == "checkpoint.ready" and row["step"] > latest_ready and row["event_id"] not in observed_ids for row in later)
        assert read_json(job / "request.json")["recipe"] == recipe
        page = call("run.events.list", **identity, limit=200)
        assert not call("run.events.list", **identity, after=page["next_cursor"], limit=200)["events"]
        assert call("recipe.get", project_id=project["project_id"], recipe_id=project["recipe_id"]) == recipe
        (tmp_path / "acceptance.json").write_text(json.dumps({"boundary_inputs": boundary_inputs, "early_evaluations": seen, "early_events": events, "later_events": later, "final": final}, indent=2))
    finally:
        if not read_json(job / "state.json").get("worker_reaped"):
            call("run.cancel", **identity, idempotency_key="cleanup")
        client.close()
        service.store.close()
