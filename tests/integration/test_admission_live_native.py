"""Native acceptance harness; run only after the coordinated memory window opens.

The trainer, checkpoint coordinator, evaluator, ZenML store and service are real.
Only the second request's environment-validation entry is deliberately held.
No second native trainer is launched: its durable intent is cancelled while held.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
from threading import Event
import time

import pytest

from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend import admission
from modelbuilder.backend.common import read_json
from modelbuilder.models import Actor
from modelbuilder.service import OperationService


ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(os.environ.get("MODELBUILDER_BACKEND_PYTHON", str(ROOT / ".venv-backend/bin/python")))
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not PYTHON.is_file(), reason="Pinned native backend required")]


def test_native_checkpoint_ingestion_during_held_admission(tmp_path, monkeypatch):
    root = ROOT
    python = PYTHON  # Preserve the approved virtualenv invocation path.
    assert python.is_file(), "An existing approved native interpreter is required"
    source = tmp_path / "source"
    fixture = source / "src/modelbuilder/backend/fixtures.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes((root / "src/modelbuilder/backend/fixtures.py").read_bytes())
    lock = source / "requirements/backend.lock"
    lock.parent.mkdir()
    lock.write_bytes((root / "requirements/backend.lock").read_bytes())
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Acceptance", "-c", "user.email=acceptance@example.invalid",
                    "commit", "-qm", "Independent pinned CPU fixture"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    recipe = json.loads((root / "examples/custom-pytorch/recipe.json").read_text())
    for key in ("digest", "project_id", "recipe_id"):
        recipe.pop(key, None)
    recipe["source"].update(commit=commit, files=[{"path": str(fixture.relative_to(source)),
                           "sha256": hashlib.sha256(fixture.read_bytes()).hexdigest()}])
    recipe["evaluation"]["live"] = {"enabled": True, "max_concurrency": 1, "timeout_seconds": 30}
    for node in recipe["nodes"]:
        if isinstance(node.get("implementation_ref"), dict):
            node["implementation_ref"]["commit"] = commit
        if node["kind"] == "model.train":
            node["parameters"].update(steps=30, step_delay=.6)
    backend = ZenMLBackend(tmp_path / "native", python_executable=str(python), source_root=source)
    service = OperationService(tmp_path / "service.sqlite3", backend)
    independent = OperationService(service.store.path, backend)
    actor = Actor("native-admission-owner", {"read", "modify", "execute"})
    def call(operation, **payload):
        return service.invoke(operation, payload, actor)
    project = call("project.create", slug="native-admission", model_name="Live CPU admission",
                   recipe=recipe, idempotency_key="project")
    base = {"project_id": project["project_id"], "recipe_id": project["recipe_id"],
            "expected_revision": 1, "trusted": True,
            "execution": {"node_ids": ["data", "initial", "train", "evaluate"], "input_bindings": []}}
    submitted = call("run.submit", **base, idempotency_key="trainer")
    job = backend.root / "jobs" / submitted["operation_id"]
    identity = {"project_id": project["project_id"], "operation_id": submitted["operation_id"]}
    entered, release = Event(), Event()
    original_verify = admission.verify_environment
    def held_verify(*args, **kwargs):
        assert not service.store.connection.in_transaction
        entered.set()
        assert release.wait(90), "Held admission was not released"
        return original_verify(*args, **kwargs)
    monkeypatch.setattr(admission, "verify_environment", held_verify)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = pool.submit(call, "run.submit", **base, idempotency_key="held-admission")
            try:
                assert entered.wait(10), pending.exception() if pending.done() else "Admission entry not reached"
                started = time.monotonic()
                observations = None
                while time.monotonic() - started < 70:
                    evaluations = pool.submit(call, "run.evaluations.list", **identity).result(timeout=2)
                    events = pool.submit(call, "run.events.list", **identity, limit=200).result(timeout=2)
                    completed = [row for row in evaluations["evaluations"] if row["status"] == "completed"]
                    ready = [row for row in events["events"] if row["kind"] == "checkpoint.ready"]
                    if len(completed) >= 2 and ready and max(row["step"] for row in ready) >= 4:
                        observations = {"completed_steps": [row["step"] for row in completed],
                                        "latest_ready_step": max(row["step"] for row in ready)}
                        break
                    assert not read_json(job / "state.json").get("worker_reaped"), "Trainer ended before live evidence"
                    time.sleep(.05)
                assert observations and observations["latest_ready_step"] < 30
                assert not pending.done() and not read_json(job / "state.json").get("worker_reaped")
                written = pool.submit(independent.invoke, "evidence.add", {"project_id": project["project_id"],
                    "title": "Concurrent checkpoint observation", "source_identifier": "urn:local-native-acceptance",
                    "idempotency_key": "concurrent-writer"}, actor).result(timeout=2)
                assert written["title"] == "Concurrent checkpoint observation"
                receipt = call("run.submit", **base, idempotency_key="held-admission")
                assert receipt["status"] == "pending" and receipt["backend_run_id"] is None
                cancelled = call("run.cancel", project_id=project["project_id"],
                                 operation_id=receipt["operation_id"], idempotency_key="cancel-held")
                assert cancelled["status"] == "cancelled" and cancelled["launch_prevented"] is True
                blocked_job = backend.root / "jobs" / receipt["operation_id"]
                assert not (blocked_job / "spawn_attempt.json").exists()
            finally:
                release.set()
            assert pending.result(timeout=100)["status"] == "cancelled"
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline and not read_json(job / "state.json").get("worker_reaped"):
            time.sleep(.1)
        final = call("run.status", **identity)
        assert final["status"] == "succeeded", final
        assert final["backend_run_id"]
        assert not (blocked_job / "spawn_attempt.json").exists()
        events = call("run.events.list", **identity, limit=200)
        assert any(row["kind"] == "checkpoint.ready" and row["step"] > observations["latest_ready_step"]
                   for row in events["events"])
        assert not call("run.events.list", **identity, after=events["next_cursor"], limit=200)["events"]
    finally:
        release.set()
        if not read_json(job / "state.json").get("worker_reaped"):
            call("run.cancel", **identity, idempotency_key="cleanup")
        independent.store.close()
        service.store.close()
