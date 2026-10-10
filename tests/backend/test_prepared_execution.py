"""Prepared admission and process ambiguity contracts, with no native ML work."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from modelbuilder.backend import BackendError, ZenMLBackend
from modelbuilder.backend import adapter, supervisor
from modelbuilder.backend.common import read_json, write_json


@pytest.fixture
def prepared_backend(tmp_path, monkeypatch):
    backend = ZenMLBackend(tmp_path, python_executable=sys.executable, source_root=tmp_path)
    calls = []

    def validate(payload):
        calls.append("validation")
        payload["execution"] = {"node_ids": ["node"], "input_bindings": []}
        payload["resolved_input_bindings"] = []

    def placement(backend, payload, *, allocate=False):
        assert allocate
        calls.append("allocation")
        return {"device": "cpu"}

    def rpc(operation, payload, **kwargs):
        calls.append(operation)
        return {} if operation == "backend.validate" else {"status": "submitted", "backend_run_id": None}

    monkeypatch.setattr(backend, "_validate_execution", validate)
    monkeypatch.setattr("modelbuilder.backend.placement.validate_placement", placement)
    monkeypatch.setattr(backend, "_rpc", rpc)
    payload = {"project_id": "project", "operation_id": str(uuid4()), "idempotency_key": "submit-once",
               "recipe": {"recipe_id": "recipe", "revision": 1, "nodes": [{"id": "node", "kind": "component.run", "inputs": [], "outputs": []}], "environment": {}}}
    return backend, payload, calls


def completed_dummy(monkeypatch, starts):
    def launch(command, **kwargs):
        job = Path(command[-2])
        assert read_json(job / "spawn_attempt.json")["operation_id"] == job.name
        assert kwargs["pass_fds"] == (int(command[-1]),)
        with (job / "launch.lock").open("a") as lock:
            with pytest.raises(BlockingIOError):
                adapter.fcntl.flock(lock, adapter.fcntl.LOCK_EX | adapter.fcntl.LOCK_NB)
        starts.append((command, kwargs))
        write_json(job / "native.json", {"backend_run_id": "dummy-native"})
        return SimpleNamespace(pid=123)
    monkeypatch.setattr(adapter.subprocess, "Popen", launch)


def test_preparation_is_immutable_and_launch_never_repeats_preflight(prepared_backend, monkeypatch):
    backend, payload, calls = prepared_backend
    original = deepcopy(payload)
    monkeypatch.setenv("TZ", "snapshot-only")
    prepared = backend.prepare_execution("run.submit", payload)
    assert calls == ["validation", "allocation", "backend.validate"]
    assert not (backend.root / "jobs").exists()
    assert payload == original
    assert prepared.operation_id == payload["operation_id"]
    assert prepared.project_id == payload["project_id"]
    with pytest.raises(FrozenInstanceError):
        prepared.project_id = "other"
    payload["recipe"]["revision"] = 99
    monkeypatch.setenv("TZ", "changed")
    starts = []
    completed_dummy(monkeypatch, starts)
    def deny_preflight(*args, **kwargs):
        pytest.fail("Launch repeated preparation")
    monkeypatch.setattr(backend, "_validate_execution", deny_preflight)
    monkeypatch.setattr(backend, "_rpc", deny_preflight)
    monkeypatch.setattr("modelbuilder.backend.placement.validate_placement", deny_preflight)
    authorized = []
    result = backend.launch_prepared(prepared, authorize=lambda: authorized.append(True))
    assert result["backend_run_id"] == "dummy-native"
    assert len(starts) == len(authorized) == 1
    request = read_json(backend.root / "jobs" / prepared.operation_id / "request.json")
    assert request["recipe"]["revision"] == 1
    assert starts[0][1]["env"]["TZ"] == "snapshot-only"


def test_only_internal_same_backend_handle_can_launch(prepared_backend, tmp_path):
    backend, payload, calls = prepared_backend
    prepared = backend.prepare_execution("run.submit", payload)
    with pytest.raises(BackendError, match="internal execution preparation"):
        backend.launch_prepared(payload)
    other = ZenMLBackend(tmp_path / "other", python_executable=sys.executable)
    with pytest.raises(BackendError, match="internal execution preparation"):
        other.launch_prepared(prepared)
    assert not (backend.root / "jobs").exists()


def test_final_authorization_precedes_intent_and_can_retry_unattempted_job(prepared_backend, monkeypatch):
    backend, payload, calls = prepared_backend
    prepared = backend.prepare_execution("run.submit", payload)
    job = backend.root / "jobs" / prepared.operation_id
    starts = []
    completed_dummy(monkeypatch, starts)
    def deny():
        assert not (job / "spawn_attempt.json").exists()
        raise BackendError("forbidden", "Authority revoked")
    with pytest.raises(BackendError, match="Authority revoked"):
        backend.launch_prepared(prepared, authorize=deny)
    assert not starts and not (job / "spawn_attempt.json").exists()
    assert backend.launch_prepared(prepared)["backend_run_id"] == "dummy-native"
    assert len(starts) == 1


def test_initialization_lock_wait_is_bounded(prepared_backend):
    backend, payload, calls = prepared_backend
    backend.initialization_lock_timeout_seconds = 0.04
    with (backend.root / "initialize.lock").open("a") as lock:
        adapter.fcntl.flock(lock, adapter.fcntl.LOCK_EX)
        before = time.monotonic()
        with pytest.raises(BackendError, match="initialization is busy") as failed:
            backend.prepare_execution("run.submit", payload)
        assert time.monotonic() - before < 0.5
    assert failed.value.code == "backend_timeout"
    assert failed.value.details["phase"] == "initialization_lock"
    assert calls == ["validation", "allocation"]
    assert not (backend.root / "jobs").exists()


@pytest.mark.parametrize("crash", [OSError("Popen failed"), SystemExit("service died before PID recording")])
def test_attempt_without_ack_or_pid_is_never_blindly_respawned(prepared_backend, monkeypatch, crash):
    backend, payload, calls = prepared_backend
    prepared = backend.prepare_execution("run.submit", payload)
    job = backend.root / "jobs" / prepared.operation_id
    starts = []
    def launch(*args, **kwargs):
        assert read_json(job / "spawn_attempt.json")["attempt_id"]
        starts.append(True)
        raise crash
    monkeypatch.setattr(adapter.subprocess, "Popen", launch)
    with pytest.raises((BackendError, SystemExit)):
        backend.launch_prepared(prepared)
    result = backend.launch_prepared(prepared, authorize=lambda: pytest.fail("Reconcile must not authorize a spawn"))
    assert len(starts) == 1
    assert result["status"] == "unknown" and result["retryable"] is False
    assert result["reconciliation_required"] is True
    assert backend.invoke("run.status", payload)["status"] == "unknown"
    # Later native observation recovers the existing run, without a new process.
    monkeypatch.setattr(backend, "_rpc", lambda *args: {"status": "running", "backend_run_id": "existing-native"})
    assert backend.launch_prepared(prepared)["backend_run_id"] == "existing-native"
    assert len(starts) == 1


def test_legacy_ambiguous_start_is_also_reconcile_only(prepared_backend, monkeypatch):
    backend, payload, calls = prepared_backend
    prepared = backend.prepare_execution("run.submit", payload)
    job = backend.root / "jobs" / prepared.operation_id
    write_json(job / "request.json", {**json.loads(prepared._request_json), "submitted_at": "old"})
    write_json(job / "state.json", {"supervisor_state": "starting"})
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *a, **k: pytest.fail("Legacy ambiguity respawned"))
    assert backend.launch_prepared(prepared)["status"] == "unknown"


@pytest.mark.parametrize("record", ["spawn_attempt.json", "supervisor_ack.json", "state.json", "native.json"])
def test_lost_request_with_launch_evidence_cannot_start_another_worker(prepared_backend, monkeypatch, record):
    backend, payload, calls = prepared_backend
    prepared = backend.prepare_execution("run.submit", payload)
    job = backend.root / "jobs" / prepared.operation_id
    write_json(job / record, {"test": "existing evidence"})
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *a, **k: pytest.fail("Lost request respawned"))
    with pytest.raises(BackendError) as failed:
        backend.launch_prepared(prepared)
    assert failed.value.code == "submission_uncertain"


def test_resume_materialization_and_validation_happen_only_during_preparation(prepared_backend, monkeypatch, tmp_path):
    backend, payload, calls = prepared_backend
    parent = {**deepcopy(payload), "submitted_at": "parent-time"}
    monkeypatch.setattr(backend, "_job", lambda request: (tmp_path / "parent", parent))
    def rpc(operation, request, **kwargs):
        calls.append(operation)
        if operation == "run.status":
            return {"status": "succeeded", "worker_alive": False}
        if operation == "model.checkpoint.get":
            return {"backend_run_id": "parent-native", "artifact_id": "checkpoint", "path": "/dummy/checkpoint"}
        assert operation == "backend.validate"
        return {}
    monkeypatch.setattr(backend, "_rpc", rpc)
    payload["parent_operation_id"] = "parent"
    prepared = backend.prepare_execution("run.resume", payload)
    assert calls == ["run.status", "model.checkpoint.get", "validation", "allocation", "backend.validate"]
    materialized = json.loads(prepared._request_json)
    assert materialized["parent_run_id"] == "parent-native"
    assert materialized["parent_checkpoint_ref"] == "checkpoint"
    assert materialized["resume_path"] == "/dummy/checkpoint"
    assert "submitted_at" not in materialized
    monkeypatch.setattr(backend, "_rpc", lambda *a, **k: pytest.fail("Repeated resume preflight"))
    monkeypatch.setattr(backend, "_job", lambda *a, **k: pytest.fail("Repeated parent materialization"))
    starts = []
    completed_dummy(monkeypatch, starts)
    assert backend.launch_prepared(prepared)["status"] == "submitted"


def test_supervisor_ack_is_durable_before_tracking_or_worker_start(tmp_path, monkeypatch):
    job = tmp_path / "job"
    write_json(job / "spawn_attempt.json", {"attempt_id": "attempt"})
    write_json(job / "request.json", {"submitted_at": "test"})
    class StopBeforeWorker(Exception):
        pass
    def tracking(job):
        ack = read_json(job / "supervisor_ack.json")
        assert ack["attempt_id"] == "attempt"
        assert ack["supervisor_pid"] == os.getpid()
        assert ack["supervisor_identity"]
        assert read_json(job / "state.json")["supervisor_state"] == "starting"
        raise StopBeforeWorker()
    monkeypatch.setitem(sys.modules, "modelbuilder.backend.tracking_bridge",
                        SimpleNamespace(LiveTracking=tracking, training_environment=lambda: {}))
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *a, **k: pytest.fail("Worker started before acknowledgment"))
    with pytest.raises(StopBeforeWorker):
        supervisor.main(job)


@pytest.mark.skipif(os.name != "posix", reason="POSIX inherited file lease")
def test_inherited_lease_and_intent_survive_lost_launch_reply(prepared_backend, monkeypatch):
    """Only a locally authored sleeping Python dummy runs, never a native worker."""
    backend, payload, calls = prepared_backend
    prepared = backend.prepare_execution("run.submit", payload)
    children = []
    popen = subprocess.Popen
    def launch(command, **kwargs):
        child = popen([sys.executable, "-c", "import time; time.sleep(30)"],
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      pass_fds=kwargs["pass_fds"])
        children.append(child)
        raise SystemExit("Lost service before durable child PID")
    monkeypatch.setattr(adapter.subprocess, "Popen", launch)
    try:
        with pytest.raises(SystemExit):
            backend.launch_prepared(prepared)
        assert len(children) == 1 and children[0].poll() is None
        assert backend.launch_prepared(prepared)["status"] == "unknown"
        assert len(children) == 1
        children[0].terminate()
        children[0].wait(timeout=5)
        assert backend.launch_prepared(prepared)["status"] == "unknown"
        assert len(children) == 1
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_initialization_child_keeps_lease_when_preparing_service_disappears(prepared_backend, monkeypatch):
    """Bootstrap ownership survives the service; only a sleeping dummy is spawned."""
    backend, payload, calls = prepared_backend
    # Restore the production RPC envelope, while replacing its SDK child with a dummy.
    monkeypatch.setattr(backend, "_rpc", ZenMLBackend._rpc.__get__(backend))
    backend.initialization_lock_timeout_seconds = 0.04
    children, runs = [], []
    popen = subprocess.Popen
    def run(command, **kwargs):
        runs.append(command)
        assert kwargs["timeout"] == 90
        assert len(kwargs["pass_fds"]) == 1
        if len(runs) == 1:
            child = popen([sys.executable, "-c", "import time; time.sleep(30)"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          pass_fds=kwargs["pass_fds"])
            children.append(child)
            raise SystemExit("Preparing service disappeared")
        request = read_json(Path(command[-1]))
        write_json(Path(request["result"]), {"validated": True})
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(adapter.subprocess, "run", run)
    try:
        with pytest.raises(SystemExit):
            backend.prepare_execution("run.submit", payload)
        with pytest.raises(BackendError, match="initialization is busy"):
            backend.prepare_execution("run.submit", payload)
        assert len(runs) == 1
        children[0].terminate()
        children[0].wait(timeout=5)
        assert backend.prepare_execution("run.submit", payload).operation_id == payload["operation_id"]
        assert len(runs) == 2
        assert not (backend.root / "jobs").exists()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_cancel_without_confirmed_worker_remains_uncertain(prepared_backend, monkeypatch):
    backend, payload, calls = prepared_backend
    prepared = backend.prepare_execution("run.submit", payload)
    job = backend.root / "jobs" / prepared.operation_id
    request = {**json.loads(prepared._request_json), "submitted_at": "attempted"}
    write_json(job / "request.json", request)
    write_json(job / "spawn_attempt.json", {"attempt_id": "lost"})
    clock = iter([0, 13])
    monkeypatch.setattr(adapter.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(adapter.os, "killpg", lambda *args: pytest.fail("No owned PID exists"))
    result = backend.cancel(job, request)
    assert result["status"] == "unknown"
    assert result["cancellation_acknowledged"] is False
    assert (job / "cancel.json").exists()


def test_different_resume_checkpoint_cannot_relabel_existing_attempt(prepared_backend, monkeypatch):
    backend, payload, calls = prepared_backend
    payload.update(parent_run_id="parent", parent_checkpoint_ref="checkpoint-one", resume_mode="warm_start")
    prepared = backend.prepare_execution("run.submit", payload)
    job = backend.root / "jobs" / prepared.operation_id
    write_json(job / "request.json", {**json.loads(prepared._request_json), "submitted_at": "attempted"})
    write_json(job / "spawn_attempt.json", {"attempt_id": "lost"})
    payload["parent_checkpoint_ref"] = "checkpoint-two"
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *a, **k: pytest.fail("Conflicting continuation launched"))
    with pytest.raises(BackendError, match="conflicting request"):
        backend.prepare_execution("run.submit", payload)
    request = read_json(job / "request.json")
    request["parent_checkpoint_ref"] = "concurrently-created-checkpoint"
    write_json(job / "request.json", request)
    with pytest.raises(BackendError, match="conflicting request"):
        backend.launch_prepared(prepared)


@pytest.mark.parametrize("changed", [
    {"tracking": {"mode": "offline", "entity": "fixture", "project": "metrics"}},
    {"tracking_online_consent": True},
    {"max_runtime_seconds": 601},
])
def test_legacy_request_rejects_changed_tracking_consent_or_deadline(prepared_backend, monkeypatch, changed):
    backend, payload, calls = prepared_backend
    original = backend.prepare_execution("run.submit", payload)
    changed_handle = backend.prepare_execution("run.submit", {**payload, **changed})
    job = backend.root / "jobs" / original.operation_id
    legacy = {**json.loads(original._request_json), "submitted_at": "legacy"}
    assert all(field not in legacy for field in ("tracking", "tracking_online_consent", "max_runtime_seconds"))
    write_json(job / "request.json", legacy)
    write_json(job / "native.json", {"backend_run_id": "original-native-fixture"})
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *a, **k: pytest.fail("Changed identity launched"))
    with pytest.raises(BackendError, match="conflicting request"):
        backend.prepare_execution("run.submit", {**payload, **changed})
    with pytest.raises(BackendError, match="conflicting request"):
        backend.launch_prepared(changed_handle)
    assert read_json(job / "request.json") == legacy
    assert read_json(job / "native.json")["backend_run_id"] == "original-native-fixture"


@pytest.mark.parametrize("tracking", [None, {}, {"mode": "disabled"}, {
    "mode": "disabled", "entity": None, "project": None, "credential_env": "MODELBUILDER_WANDB_API_KEY",
    "metric_names": [], "artifact_policy": "none",
}])
def test_legacy_missing_settings_equal_explicit_effective_defaults(prepared_backend, monkeypatch, tracking):
    backend, payload, calls = prepared_backend
    original = backend.prepare_execution("run.submit", payload)
    job = backend.root / "jobs" / original.operation_id
    legacy = {**json.loads(original._request_json), "submitted_at": "legacy"}
    if tracking is None:
        legacy["tracking"] = None  # Legacy null and absent configurations mean disabled.
    write_json(job / "request.json", legacy)
    write_json(job / "native.json", {"backend_run_id": "original-native-fixture"})
    defaults = {**payload, "tracking": tracking, "tracking_online_consent": False, "max_runtime_seconds": 600.0}
    if tracking is None:
        defaults.pop("tracking")
    prepared = backend.prepare_execution("run.submit", defaults)
    monkeypatch.setattr(backend, "_rpc", lambda *a, **k: {
        "status": "running", "backend_run_id": "original-native-fixture"})
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *a, **k: pytest.fail("Legacy retry respawned"))
    assert backend.launch_prepared(prepared)["backend_run_id"] == "original-native-fixture"
    assert read_json(job / "request.json") == legacy


@pytest.mark.parametrize("field,value", [
    ("mode", "offline"), ("entity", "different"), ("project", "different"),
    ("metric_names", ["loss", "accuracy"]), ("artifact_policy", "metrics_snapshot"),
])
def test_tracking_identity_preserves_destination_and_sharing_contract(prepared_backend, field, value):
    backend, payload, calls = prepared_backend
    config = {"mode": "online", "entity": "fixture", "project": "metrics", "metric_names": ["loss"]}
    payload.update(tracking=config, tracking_online_consent=True)
    original = backend.prepare_execution("run.submit", payload)
    job = backend.root / "jobs" / original.operation_id
    write_json(job / "request.json", {**json.loads(original._request_json), "submitted_at": "legacy"})
    changed = {**payload, "tracking": {**config, field: value}}
    with pytest.raises(BackendError, match="conflicting request"):
        backend.prepare_execution("run.submit", changed)
