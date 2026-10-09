"""Independent adversarial boundary checks; no foreign code or paid jobs run.

The contract doubles below exercise rejection/correlation semantics only. They
are never evidence that the real ZenML execution gates have passed.
"""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from modelbuilder.backend import adapter as backend_module
from modelbuilder.backend.adapter import BackendError, ZenMLBackend
from modelbuilder.models import Actor, ServiceError
from modelbuilder.recipe import default_recipe, export_bundle, import_bundle
from modelbuilder.service import OperationService


OWNER = Actor("audit-owner", {"read", "modify", "execute"})


def small_recipe():
    result = default_recipe("audit-project", "audit-recipe")
    result["nodes"] = [{
        "id": "data", "kind": "dataset.synthetic",
        "implementation_ref": "builtin:dataset.synthetic", "inputs": [],
        "outputs": [{"name": "data", "type": "dict"}],
        "parameters": {"fixture": "custom", "count": 40},
        "environment_ref": "default", "resource_request": {"cpu": 1, "memory_mb": 128},
        "retry": {"max_attempts": 1, "backoff_seconds": 0},
        "timeout": 30, "cache_policy": "disabled", "provenance": {},
    }]
    return result


def source_bound_recipe(backend, tmp_path):
    """Trusted inert fixture snapshot, like an extracted bundle without .git."""
    source_root = tmp_path / "trusted-source"
    source_root.mkdir()
    content = b"def fixture_marker():\n    return 42\n"
    (source_root / "fixture.py").write_bytes(content)
    runtime_content = Path(backend_module.__file__).with_name("fixtures.py").read_bytes()
    runtime_relative = "src/modelbuilder/backend/fixtures.py"
    (source_root / runtime_relative).parent.mkdir(parents=True)
    (source_root / runtime_relative).write_bytes(runtime_content)
    backend.repository = source_root
    body = small_recipe()
    body["source"].update(repository="https://example.invalid/audit-fixture.git", commit="a" * 40,
                          files=[{"path": "fixture.py", "sha256": hashlib.sha256(content).hexdigest()},
                                 {"path": runtime_relative, "sha256": hashlib.sha256(runtime_content).hexdigest()}])
    return body


class BoundaryBackend:
    """Deliberately configurable, nonexecuting contract probe."""

    def __init__(self):
        self.calls = []
        self.status = "running"
        self.run_id = "native-audit-run"

    def invoke(self, operation, payload, actor):
        self.calls.append((operation, deepcopy(payload)))
        return {"operation_id": payload.get("operation_id"),
                "backend_run_id": self.run_id,
                "status": "submitted" if operation in {"run.submit", "run.resume"} else self.status}


@pytest.fixture
def service(tmp_path):
    instance = OperationService(tmp_path / "audit.sqlite3", backend=BoundaryBackend())
    yield instance
    instance.store.close()


def create_project(service, slug="audit"):
    return service.invoke("project.create", {"slug": slug, "model_name": slug,
                          "idempotency_key": f"project-{slug}"}, OWNER)


def create_run(service):
    project = create_project(service)
    recipe = service.invoke("recipe.create", {"project_id": project["project_id"],
                            "recipe": small_recipe(), "idempotency_key": "recipe"}, OWNER)
    payload = {"project_id": project["project_id"], "recipe_id": recipe["recipe_id"],
               "expected_revision": 1, "trusted": True, "idempotency_key": "submit"}
    run = service.invoke("run.submit", payload, OWNER)
    return project, recipe, run, payload


def test_resume_respects_revoked_execution_target(service):
    project, _, run, _ = create_run(service)
    service.invoke("policy.update", {"project_id": project["project_id"], "expected_revision": 1,
                   "changes": {"allowed_targets": []}, "idempotency_key": "disable"}, OWNER)
    service.backend.calls.clear()
    with pytest.raises(ServiceError):
        service.invoke("run.resume", {"project_id": project["project_id"],
                       "operation_id": run["operation_id"], "mode": "exact_state", "steps": 5,
                       "trusted": True, "idempotency_key": "resume"}, OWNER)
    assert service.backend.calls == []


@pytest.mark.parametrize("invalid", ["complete", "success", "billing_stopped", "", None, 1])
def test_unknown_backend_states_never_become_authoritative(service, invalid):
    project, _, run, _ = create_run(service)
    service.backend.status = invalid
    try:
        result = service.invoke("run.status", {"project_id": project["project_id"],
                                "operation_id": run["operation_id"]}, OWNER)
    except ServiceError as error:
        assert error.code == "backend_contract_error"
    else:
        assert result["status"] == "unknown"
    stored = service.store.get("operation", run["operation_id"])
    assert stored["status"] in {"submitted", "unknown"}


def test_backend_cannot_replace_known_native_run_identity(service):
    project, _, run, _ = create_run(service)
    service.backend.run_id = "foreign-native-run"
    with pytest.raises(ServiceError) as error:
        service.invoke("run.status", {"project_id": project["project_id"],
                       "operation_id": run["operation_id"]}, OWNER)
    assert error.value.code == "backend_contract_error"
    assert service.store.get("operation", run["operation_id"])["backend_run_id"] == run["backend_run_id"]


def test_submit_retry_after_reconciliation_still_starts_once(service):
    project, _, run, payload = create_run(service)
    service.invoke("run.status", {"project_id": project["project_id"],
                   "operation_id": run["operation_id"]}, OWNER)
    assert service.invoke("run.submit", {**payload, "request_id": "retry-request"}, OWNER) == run
    assert sum(operation == "run.submit" for operation, _ in service.backend.calls) == 1


@pytest.mark.parametrize("parameter", ["access_token", "token", "api_key", "X-Amz-Signature", "%61ccess_token"])
def test_credential_query_urls_never_persist_in_project_metadata(service, parameter):
    marker = "synthetic-audit-marker"
    with pytest.raises(ServiceError):
        service.invoke("project.create", {"slug": "credential-url", "model_name": "audit",
                       "git_remote": f"https://example.invalid/repo?{parameter}={marker}",
                       "idempotency_key": "credential-url"}, OWNER)
    assert marker not in str(service.store.list("project"))


@pytest.mark.parametrize("operation", ["run.plan", "run.submit", "run.resume"])
def test_execution_parameter_overrides_cannot_be_silently_ignored(service, operation):
    project, recipe, run, _ = create_run(service)
    payload = {"project_id": project["project_id"], "parameters": {"steps": 999}}
    if operation == "run.resume":
        payload.update(operation_id=run["operation_id"], trusted=True, mode="exact_state", steps=5)
    else:
        payload["recipe_id"] = recipe["recipe_id"]
    if operation != "run.plan":
        payload["idempotency_key"] = "hidden-override"
    if operation == "run.submit":
        payload.update(trusted=True, expected_revision=1)
    service.backend.calls.clear()
    with pytest.raises(ServiceError):
        service.invoke(operation, payload, OWNER)
    assert service.backend.calls == []


@pytest.mark.parametrize("operation", ["run.status", "run.logs", "run.cancel", "run.artifacts",
                                      "run.resume", "model.checkpoint.get"])
def test_foreign_operation_ids_rejected_before_backend(service, operation):
    _, _, run, _ = create_run(service)
    other = create_project(service, "other")
    scoped = Actor("scoped", {"read", "modify", "execute"}, {other["project_id"]})
    payload = {"project_id": other["project_id"], "operation_id": run["operation_id"]}
    if operation in {"run.cancel", "run.resume"}:
        payload["idempotency_key"] = operation
    if operation == "run.resume":
        payload.update(trusted=True, mode="exact_state", steps=5)
    service.backend.calls.clear()
    with pytest.raises(ServiceError) as error:
        service.invoke(operation, payload, scoped)
    assert error.value.code == "not_found"
    assert service.backend.calls == []


def test_plan_requires_no_execution_consent_or_process(tmp_path, monkeypatch):
    backend = ZenMLBackend(tmp_path)
    body = source_bound_recipe(backend, tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("Planning must never launch a process")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    result = backend.invoke("run.plan", {"project_id": "audit-project", "recipe": body})
    assert result["target"] == "local-cpu"
    assert not (backend.root / "jobs").exists()


@pytest.mark.parametrize("variable", ["MODELBUILDER_TOKEN", "MODELBUILDER_TOKENS_JSON",
                                     "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY"])
def test_worker_environment_excludes_control_plane_and_provider_tokens(tmp_path, monkeypatch, variable):
    # This is a synthetic marker, never a real credential.
    marker = "synthetic-audit-marker-42"
    monkeypatch.setenv(variable, marker)
    environment = ZenMLBackend(tmp_path)._env()
    assert variable not in environment
    assert marker not in str(environment)


def test_untrusted_python_blocked_without_import_or_process(tmp_path, monkeypatch):
    backend = ZenMLBackend(tmp_path)
    body = source_bound_recipe(backend, tmp_path)
    body["nodes"][0].update(kind="code.python", implementation_ref={
        "module": "audit_fixture", "entrypoint": "main", "commit": "a" * 40,
        "path": "audit_fixture.py", "sha256": "b" * 64})
    def forbidden(*args, **kwargs):
        raise AssertionError("Untrusted Python must not execute")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    with pytest.raises(BackendError) as error:
        backend._validate_execution({"trusted": True, "recipe": body})
    assert error.value.code == "untrusted_execution_blocked"


@pytest.mark.parametrize("mutation", ["missing_source", "private_dependency", "gpu_requirement"])
def test_unavailable_requirements_rejected_before_execution(tmp_path, mutation):
    backend = ZenMLBackend(tmp_path)
    body = source_bound_recipe(backend, tmp_path)
    if mutation == "missing_source":
        body["source"]["files"] = [{"path": "definitely-missing-audit-source.py", "sha256": "b" * 64}]
    elif mutation == "private_dependency":
        body["external_dependencies"] = [{"id": "private-dataset", "kind": "dataset",
            "url": "https://example.invalid/private-data", "sha256": "b" * 64,
            "license": "MIT", "private": True, "required": True}]
    else:
        body["nodes"][0]["resource_request"]["gpu"] = 1
    with pytest.raises(BackendError):
        backend._validate_execution({"trusted": True, "recipe": body})


@pytest.mark.parametrize("kl_change", [{"reference_model_ref": "unconnected-initial"},
                                      {"applies_to": "hidden_features"}])
def test_unimplemented_kl_reference_or_scope_cannot_be_silently_ignored(tmp_path, kl_change):
    backend = ZenMLBackend(tmp_path)
    body = source_bound_recipe(backend, tmp_path)
    initial = deepcopy(body["nodes"][0])
    initial.update(id="initial", kind="model.initialize", implementation_ref="builtin:model.initialize",
                   inputs=[{"name": "data", "type": "dict"}],
                   outputs=[{"name": "initial", "type": "Path"}], parameters={})
    train = deepcopy(initial)
    train.update(id="train", kind="model.train", implementation_ref="builtin:model.train",
                 inputs=[{"name": "data", "type": "dict"}, {"name": "initial", "type": "Path"}],
                 outputs=[{"name": "checkpoint", "type": "Path"}], parameters={"steps": 3, "kl": {
                     "enabled": True, "coefficient": 0.1, "direction": "reference_to_model",
                     "reference_model_ref": "initial", "applies_to": "logits", "frozen": True}})
    body["nodes"] += [initial, {**deepcopy(initial), "id": "unconnected-initial"}, train]
    body["edges"] = [{"source_node": "data", "source_port": "data", "target_node": node,
                      "target_port": "data"} for node in ["initial", "unconnected-initial", "train"]]
    body["edges"].append({"source_node": "initial", "source_port": "initial",
                          "target_node": "train", "target_port": "initial"})
    assert backend._validate_execution({"trusted": True, "recipe": body}) == body
    train["parameters"]["kl"].update(kl_change)
    with pytest.raises(BackendError) as error:
        backend._validate_execution({"trusted": True, "recipe": body})
    assert error.value.code == "capability_unavailable"


def test_imported_unresolved_dependency_never_claims_execution_ready():
    body = small_recipe()
    body["external_dependencies"] = [{"id": "private-dataset", "kind": "dataset",
        "url": "https://example.invalid/private-data", "sha256": "b" * 64,
        "license": "MIT", "private": True, "required": True}]
    result = import_bundle(export_bundle(body))
    assert result["execution_allowed"] is False
    assert result["ready"] is False
    assert any(item["reason"] == "private_reference" for item in result["unresolved_dependencies"])


def test_resume_rejects_foreign_checkpoint_identifier(tmp_path, monkeypatch):
    backend = ZenMLBackend(tmp_path)
    job = tmp_path / str(uuid4())
    parent = {"project_id": "audit-project", "recipe": small_recipe()}
    monkeypatch.setattr(backend, "_job", lambda payload: (job, parent))
    monkeypatch.setattr(backend, "_rpc", lambda operation, payload: {
        "backend_run_id": "parent-native-run", "artifact_id": "actual-parent-checkpoint", "path": str(job)})
    submitted = []
    monkeypatch.setattr(backend, "submit", lambda payload: submitted.append(payload) or {"status": "submitted"})
    with pytest.raises(BackendError):
        backend.resume({"project_id": "audit-project", "operation_id": str(uuid4()),
                        "checkpoint_id": "foreign-checkpoint", "trusted": True,
                        "mode": "exact_state", "steps": 5})
    assert submitted == []


def test_live_worker_cannot_be_cancelled_even_with_conflicting_ack(service):
    project, _, run, _ = create_run(service)
    service.backend.invoke = lambda operation, payload, actor: {
        "operation_id": run["operation_id"], "backend_run_id": run["backend_run_id"],
        "status": "cancelled", "confirmed": True, "worker_alive": True,
    }
    with pytest.raises(ServiceError) as error:
        service.invoke("run.cancel", {"project_id": project["project_id"],
                       "operation_id": run["operation_id"], "idempotency_key": "cancel-live"}, OWNER)
    assert error.value.code == "backend_contract_error"
    assert service.store.get("operation", run["operation_id"])["status"] != "cancelled"


def test_failed_process_start_cannot_leave_permanently_submitted_journal(tmp_path, monkeypatch):
    backend = ZenMLBackend(tmp_path)
    body = source_bound_recipe(backend, tmp_path)
    payload = {"project_id": "audit-project", "operation_id": str(uuid4()),
               "recipe": body, "trusted": True, "idempotency_key": "start-once"}
    starts = []
    def launch(*args, **kwargs):
        starts.append(args)
        if len(starts) == 1:
            raise OSError("Synthetic launch failure before creating a process")
        return SimpleNamespace(pid=999999999)
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(backend, "_rpc", lambda operation, request: {"status": "submitted"})
    with pytest.raises((OSError, BackendError)):
        backend.submit(payload)
    try:
        retry = backend.submit(payload)
    except BackendError:
        return  # An explicit failed/blocked launch is truthful; endless submitted is not.
    assert len(starts) == 2 or retry["status"] in {"failed", "unknown"}


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="Linux supervisor lifecycle contract")
def test_cancellation_waits_for_owned_process_group_not_just_leader(tmp_path, monkeypatch):
    """Only locally authored short-lived Python fixtures are started and killed.

    A child ignoring SIGTERM exposes a real orphan-worker race. The finally
    block kills only this test's fresh session/process group, even on failure.
    The SDK seam is inert because this tests supervisor process ownership only.
    """
    from modelbuilder.backend import supervisor
    from modelbuilder.backend.common import alive, process_identity, read_json, write_json

    job = tmp_path / "job"
    job.mkdir()
    pid_file = tmp_path / "child-pid"
    child = tmp_path / "child.py"
    child.write_text("import os, signal, time\nfrom pathlib import Path\n"
                     "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                     f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                     "time.sleep(20)\n")
    worker = tmp_path / "worker.py"
    worker.write_text("import subprocess, sys, time\n"
                      f"subprocess.Popen([sys.executable, {str(child)!r}])\n"
                      "time.sleep(20)\n")
    write_json(job / "request.json", {"max_runtime_seconds": 15})
    original_popen = subprocess.Popen
    processes = []
    def controlled_popen(command, **kwargs):
        assert command[1:3] == ["-m", "modelbuilder.backend.worker"]
        process = original_popen([sys.executable, str(worker)], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", controlled_popen)
    monkeypatch.setitem(sys.modules, "modelbuilder.backend.sdk", SimpleNamespace(reconcile=lambda path: None))
    errors = []
    def supervise():
        try:
            supervisor.main(job)
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=supervise, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists(), "Trusted fixture child failed to start"
        pid = int(pid_file.read_text())
        identity = process_identity(pid)
        assert alive(pid, identity)
        write_json(job / "cancel.json", {"requested_at": "audit"})
        thread.join(timeout=9)
        assert not thread.is_alive(), "Supervisor failed to finish cancellation"
        assert not errors
        assert read_json(job / "state.json")["worker_reaped"] is True
        assert not alive(pid, identity), "Supervisor reported finished while its child was still running"
    finally:
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        thread.join(timeout=6)


@pytest.mark.integration
def test_native_sdk_cancellation_mapping_requires_positive_proof(tmp_path):
    """Exercise actual pinned SDK mapping code with controlled observation seams.

    These are state-mapping regressions, not evidence of live ZenML cancellation.
    Live process-group behavior is independently covered above and native
    training/cancellation acceptance belongs to test_native_lifecycle.py.
    """
    backend = ZenMLBackend(tmp_path)
    if not Path(backend.python).is_file():
        pytest.skip("Pinned native SDK environment is required for mapping regressions")
    script = r'''
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from modelbuilder.backend import sdk
from modelbuilder.backend.common import write_json
from zenml.enums import ExecutionStatus

root = Path(sys.argv[1])
violations = []

def check(condition, message):
    if not condition:
        violations.append(message)

def fixture(name, native=ExecutionStatus.FAILED, termination=None, state=None, cancel=False):
    job = root / name
    job.mkdir()
    write_json(job / "request.json", {"project_id": "mapping-audit", "submitted_at": "audit",
                                     "recipe": {"nodes": []}})
    write_json(job / "state.json", state or {})
    if cancel:
        write_json(job / "cancel.json", {"requested_at": "audit"})
    run = SimpleNamespace(status=native, id="native-audit-run", model_version=None,
                          exception_info="preserved-native-failure" if native == ExecutionStatus.FAILED else None,
                          run_metadata={"local_supervisor_termination": termination} if termination else {}, steps={})
    client = SimpleNamespace(zen_store=SimpleNamespace(update_run=lambda *a, **k: None),
                             get_pipeline_run=lambda *a, **k: run)
    sdk.native_run = lambda job: (client, run)
    sdk.log_metadata = lambda metadata, **kwargs: run.run_metadata.update(metadata)
    sdk.alive = lambda *args: False
    sdk.process_group_state = lambda *args: False
    return job, run

# A PID with no saved birth identity is not proof that a worker stopped.
job, run = fixture("missing-identity", state={"worker_pid": 999999999}, cancel=True)
sdk._reconcile(job)
proof = sdk.metadata_values(run).get("local_supervisor_termination", {})
check(not (proof.get("reason") == "cancelled" and proof.get("confirmed")),
      "A recorded PID without identity was promoted to confirmed native cancellation")

proof = {"reason": "cancelled", "confirmed": True, "process_group_terminated": True,
         "worker_pid": 999999999, "confirmed_at": "audit"}
verified_state = {"worker_pid": 999999999, "worker_identity": "synthetic-original-birth",
                  "worker_reaped": True, "process_group_terminated": True}
job, run = fixture("completed-wins", native=ExecutionStatus.COMPLETED, termination=proof,
                   state=verified_state)
sdk.reconcile = lambda job: run
result = sdk.status(job)
check(result["status"] == "succeeded" and result["backend_status"] == "completed",
      "Stale cancellation metadata overrode authoritative completed state")

job, run = fixture("unproven-group", termination={**proof, "process_group_terminated": False},
                   state=verified_state)
sdk.reconcile = lambda job: run
check(sdk.status(job)["status"] != "cancelled", "Incomplete native proof was treated as confirmed cancellation")

job, run = fixture("nonboolean-proof", termination={**proof, "confirmed": "false"}, state=verified_state)
sdk.reconcile = lambda job: run
check(sdk.status(job)["status"] != "cancelled", "Truthy nonboolean metadata was accepted as confirmation")

job, run = fixture("native-enum-alone", native=ExecutionStatus.CANCELLED, state={"worker_pid": 999999999})
sdk.reconcile = lambda job: run
check(sdk.status(job)["status"] != "cancelled", "Native enum bypassed required local termination proof")

job, run = fixture("live-group", termination=proof, state={"worker_pid": 999999999}, cancel=True)
sdk.reconcile = lambda job: run
sdk.process_group_state = lambda *args: True
result = sdk.status(job)
check(result["status"] == "cancel_requested" and result["worker_alive"] is True,
      "Live process group was acknowledged as cancelled")
check(result["backend_status"] == "failed" and result["error"] == "preserved-native-failure",
      "Native failure status or exception was hidden")

job, run = fixture("unobservable-worker", termination=proof, state={"worker_pid": 999999999,
                   "worker_identity": "synthetic-original-birth"}, cancel=True)
sdk.reconcile = lambda job: run
sdk.process_group_state = lambda *args: None
result = sdk.status(job)
check(result["status"] == "cancel_requested" and result["worker_alive"] is None,
      "Unobservable process group was conflated with confirmed termination")

job, run = fixture("ordinary-failure", state={"worker_pid": 999999999, "worker_reaped": True,
                                             "process_group_terminated": True})
sdk.reconcile = lambda job: run
result = sdk.status(job)
check(result["status"] == "failed" and result["backend_status"] == "failed" and result["error"],
      "An ordinary native failure was relabeled or suppressed")

job, run = fixture("late-cancel-after-failure", termination={**proof, "reason": "worker_failure"},
                   state=verified_state, cancel=True)
sdk._reconcile(job)
check(sdk.metadata_values(run)["local_supervisor_termination"]["reason"] == "worker_failure",
      "Late cancellation rewrote previously confirmed native worker-failure evidence")
sdk.reconcile = lambda job: run
check(sdk.status(job)["status"] == "failed", "Late cancel relabeled an established terminal failure")

job, run = fixture("confirmed-cancel", termination=proof, state=verified_state, cancel=True)
sdk.reconcile = lambda job: run
result = sdk.status(job)
check(result["status"] == "cancelled" and result["backend_status"] == "failed"
      and result["worker_alive"] is False and result["error"] == "preserved-native-failure",
      "Verified cancellation did not preserve authoritative native failure details")
print(json.dumps({"violations": violations}))
'''
    result = subprocess.run([backend.python, "-c", script, str(tmp_path)],
                            cwd=backend.repository, env=backend._env(), text=True,
                            capture_output=True, timeout=45)
    assert result.returncode == 0, result.stderr
    import json
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["violations"] == []


@pytest.mark.parametrize("uncertainty", ["different_runtime", "permission_denied", "unobservable_group"])
def test_termination_proof_fails_closed_for_unobservable_runtime(monkeypatch, uncertainty):
    from modelbuilder.backend import common
    state = {"worker_pid": 999999999, "worker_identity": "synthetic-original-birth",
             "runtime_identity": "original-runtime"}
    monkeypatch.setattr(common, "runtime_identity", lambda: "other-runtime" if uncertainty == "different_runtime"
                        else "original-runtime")
    monkeypatch.setattr(common, "process_group_state", lambda group: None if uncertainty == "unobservable_group"
                        else False)
    def cannot_observe(*args, **kwargs):
        if uncertainty == "permission_denied":
            raise PermissionError("Synthetic unreadable process observation")
        raise FileNotFoundError("Synthetic absent process observation")
    monkeypatch.setattr(Path, "read_text", cannot_observe)
    assert common.termination_confirmed(state) is False
