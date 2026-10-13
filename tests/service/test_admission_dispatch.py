"""Admission/dispatch race contracts with controlled, nonexecuting backends.

The gates below represent slow preflight and the last pre-spawn boundary. No
native models, runtime probes, subprocess workers, or publication run here.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from threading import Event, Lock

import pytest

from modelbuilder.api import create_app
from modelbuilder.auth import TokenAuth
from modelbuilder.models import Actor, ServiceError
from modelbuilder.recipe import default_recipe
from modelbuilder.service import OperationService


OWNER = Actor("owner", {"read", "modify", "execute"})


class InterruptedDispatch(BaseException):
    """Simulate process loss without running or killing an actual worker."""


class ControlledBackend:
    """Only an authorize callback followed by launch bookkeeping can 'spawn'."""

    def __init__(self, *, block_at="prepare", crash_at=None):
        self.block_at, self.crash_at = block_at, crash_at
        self.entered, self.release = Event(), Event()
        self.guard = Lock()
        self.prepared, self.launch_attempts, self.launched, self.calls = [], [], [], []
        self.service = None

    def _gate(self, phase):
        assert not self.service.store.connection.in_transaction
        if self.block_at == phase:
            self.entered.set()
            if not self.release.wait(10):
                raise RuntimeError("Test did not release admission preparation")
        if self.crash_at == phase:
            raise InterruptedDispatch(phase)

    def prepare_execution(self, operation, payload):
        assert operation in {"run.submit", "run.resume"}
        handle = {"operation": operation, "payload": deepcopy(payload)}
        with self.guard:
            self.prepared.append(handle)
        self._gate("prepare")
        return handle

    def launch_prepared(self, handle, *, authorize):
        assert any(handle is candidate for candidate in self.prepared)
        with self.guard:
            self.launch_attempts.append(handle)
        self._gate("authorize")
        authorize()
        self._gate("authorized")
        with self.guard:
            self.launched.append(handle)
        self._gate("launched")
        payload = handle["payload"]
        return {"operation_id": payload["operation_id"], "project_id": payload["project_id"],
                "backend_run_id": "native-" + payload["operation_id"], "status": "submitted",
                "worker_alive": True}

    def invoke(self, operation, payload, actor):
        with self.guard:
            self.calls.append((operation, deepcopy(payload)))
        if operation == "run.plan":
            self._gate("prepare")
            return {"project_id": payload["project_id"], "executable": True, "blocked_reasons": []}
        if operation in {"run.status", "run.cancel"}:
            self._gate(operation)
            native_id = payload.get("backend_run_id")
            if native_id is None:
                launched = any(handle["payload"]["operation_id"] == payload["operation_id"]
                               for handle in self.launched)
                if not launched:
                    raise ServiceError("not_found", "No native worker exists", status_code=404)
                native_id = "native-" + payload["operation_id"]
            return {"operation_id": payload["operation_id"], "project_id": payload["project_id"],
                    "backend_run_id": native_id,
                    "status": "succeeded" if operation == "run.status" else "cancel_requested",
                    "confirmed": False}
        if operation == "run.evaluations.list":
            return {"evaluations": []}
        if operation == "run.events.list":
            return {"events": []}
        if operation == "run.logs":
            return {"logs": []}
        if operation == "run.artifacts":
            return {"artifacts": []}
        raise AssertionError(f"Unexpected monolithic backend call: {operation}")


def seed(service, operation):
    """Use the same minimal built-in trainer and stored-run fixture as service tests."""
    body = default_recipe("template", "template", "training")
    body["nodes"] = [{"id": "train", "kind": "model.train", "implementation_ref": "builtin:model.train",
        "inputs": [], "outputs": [], "parameters": {"steps": 4, "learning_rate": 0.1},
        "environment_ref": "default", "resource_request": {},
        "retry": {"max_attempts": 1, "backoff_seconds": 0}, "timeout": 30,
        "cache_policy": "disabled", "provenance": {}}]
    project = service.invoke("project.create", {"slug": "admission", "model_name": "Admission",
        "recipe": body, "idempotency_key": "project"}, OWNER)
    recipe = service.store.get("recipe", project["recipe_id"])
    with service.store.transaction():
        service.store.put("operation", "parent-operation", project["project_id"], {
            "operation_id": "parent-operation", "project_id": project["project_id"],
            "backend_run_id": "parent-native-run", "recipe_id": recipe["recipe_id"],
            "recipe_revision": recipe["revision"], "recipe_digest": recipe["digest"],
            "status": "succeeded", "revision": 1})
    if operation == "run.resume":
        return {"project_id": project["project_id"], "operation_id": "parent-operation",
            "expected_recipe_revision": 1, "mode": "warm_start", "steps": 6,
            "trusted": True, "idempotency_key": "admit"}
    payload = {"project_id": project["project_id"], "recipe_id": project["recipe_id"],
               "expected_revision": 1, "trusted": True}
    if operation == "run.submit":
        payload["idempotency_key"] = "admit"
    return payload


def pending_record(service, project_id):
    records = [record for record in service.store.list("operation", project_id)
               if record["operation_id"] != "parent-operation"]
    assert len(records) == 1
    assert records[0]["status"] == "pending"
    assert records[0].get("backend_run_id") is None
    return records[0]


@pytest.mark.parametrize("operation", ["run.plan", "run.submit", "run.resume"])
def test_each_admission_path_receives_request_local_rights_authority(tmp_path, operation):
    """Nonexecuting routing proof: submit/resume must not bypass the Hub resolver."""
    resolutions = []

    class RightsAwareBackend(ControlledBackend):
        def with_execution_rights(self, *, project_id, policies, policy_store_path):
            scoped = copy(self)
            scoped.rights_authority = (project_id, deepcopy(policies), policy_store_path)
            resolutions.append(scoped.rights_authority)
            return scoped

        def check_authority(self, payload):
            assert self.rights_authority == (payload["project_id"], {}, self.service.store.path)

        def prepare_execution(self, operation, payload):
            self.check_authority(payload)
            return super().prepare_execution(operation, payload)

        def invoke(self, operation, payload, actor):
            if operation == "run.plan":
                self.check_authority(payload)
            return super().invoke(operation, payload, actor)

    backend = RightsAwareBackend(block_at=None)
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    try:
        payload = seed(service, operation)
        result = service.invoke(operation, payload, OWNER)
        assert result["project_id"] == payload["project_id"]
        assert resolutions == [(payload["project_id"], {}, service.store.path)]
        assert not hasattr(backend, "rights_authority")
    finally:
        service.store.close()


@pytest.mark.parametrize("operation", ["run.plan", "run.submit", "run.resume"])
@pytest.mark.parametrize("separate_connection", [False, True])
def test_slow_admission_allows_live_polls_and_unrelated_writer(tmp_path, operation, separate_connection):
    backend = ControlledBackend()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, operation)
    polling = OperationService(tmp_path / "service.sqlite3", backend=backend) if separate_connection else service
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            admission = pool.submit(service.invoke, operation, payload, OWNER)
            try:
                assert backend.entered.wait(2)
                assert not service.store.connection.in_transaction
                if operation != "run.plan":
                    record = pending_record(polling, payload["project_id"])
                    assert record["recipe_id"] == service._current_recipe(payload["project_id"])["recipe_id"]
                for poll_operation in ("run.status", "run.evaluations.list", "run.events.list", "run.logs"):
                    poll = pool.submit(polling.invoke, poll_operation,
                        {"project_id": payload["project_id"], "operation_id": "parent-operation"}, OWNER)
                    assert isinstance(poll.result(timeout=2), dict)
                writer = pool.submit(polling.invoke, "project.create",
                    {"slug": "unrelated", "model_name": "Other", "idempotency_key": "unrelated"}, OWNER)
                assert writer.result(timeout=2)["slug"] == "unrelated"
                assert not admission.done()
            finally:
                backend.release.set()
            result = admission.result(timeout=2)
            if operation == "run.plan":
                assert result["executable"]
                assert backend.prepared == backend.launched == []
                assert len(service.store.list("operation", payload["project_id"])) == 1
            else:
                assert result["status"] == "submitted"
                assert len(backend.prepared) == len(backend.launched) == 1
    finally:
        if separate_connection:
            polling.store.close()
        service.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
@pytest.mark.parametrize("separate_connection", [False, True])
def test_duplicate_pending_admission_reuses_receipt_and_conflicts_fast(tmp_path, operation, separate_connection):
    backend = ControlledBackend()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, operation)
    retrying = OperationService(tmp_path / "service.sqlite3", backend=backend) if separate_connection else service
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            admission = pool.submit(service.invoke, operation, payload, OWNER)
            try:
                assert backend.entered.wait(2)
                record = pending_record(retrying, payload["project_id"])
                retry = pool.submit(retrying.invoke, operation, {**payload, "request_id": "new-request"}, OWNER)
                receipt = retry.result(timeout=2)
                assert receipt["operation_id"] == record["operation_id"]
                assert receipt["status"] == "pending"
                assert retrying.invoke(operation, payload, OWNER) == receipt
                changed = {**payload, **({"steps": 7} if operation == "run.resume" else {"trusted": False})}
                conflict = pool.submit(retrying.invoke, operation, changed, OWNER)
                with pytest.raises(ServiceError) as error:
                    conflict.result(timeout=2)
                assert error.value.status_code == 409
                assert len(backend.prepared) == 1
                assert backend.launched == []
                assert not admission.done()
            finally:
                backend.release.set()
            result = admission.result(timeout=2)
            assert result["operation_id"] == receipt["operation_id"]
            assert result["status"] == "submitted"
            assert retrying.invoke(operation, payload, OWNER) == result
            assert len(backend.prepared) == len(backend.launched) == 1
    finally:
        if separate_connection:
            retrying.store.close()
        service.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
@pytest.mark.parametrize("block_at", ["prepare", "authorize"])
@pytest.mark.parametrize("change", ["remove", "replace", "scopes", "project"])
def test_dispatch_rechecks_exact_authenticated_grant(tmp_path, operation, block_at, change):
    backend = ControlledBackend(block_at=block_at)
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, operation)
    auth = TokenAuth({"request-token": OWNER, "other-token": OWNER})
    create_app(service, auth)
    actor = auth.authenticate("Bearer request-token")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            admission = pool.submit(service.invoke, operation, payload, actor)
            try:
                assert backend.entered.wait(2)
                if change == "remove":
                    del auth._tokens["request-token"]
                elif change == "replace":
                    auth._tokens["request-token"] = Actor("owner", OWNER.scopes)
                elif change == "scopes":
                    auth._tokens["request-token"] = Actor("owner", {"read"})
                else:
                    auth._tokens["request-token"] = Actor("owner", OWNER.scopes, {"different-project"})
                assert auth.authorize_operation(operation, auth.authenticate("Bearer other-token"), payload["project_id"])
            finally:
                backend.release.set()
            with pytest.raises(ServiceError) as error:
                admission.result(timeout=2)
            assert error.value.code == "forbidden"
            assert backend.launched == []
            if operation == "run.resume":
                assert service._current_recipe(payload["project_id"])["revision"] == 1
    finally:
        service.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
def test_pending_receipt_and_intent_commit_before_dataset_preparation(tmp_path, monkeypatch, operation):
    from modelbuilder import dataset_preparation

    backend = ControlledBackend(block_at=None)
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, operation)
    observer = OperationService(tmp_path / "service.sqlite3", backend=backend)
    entered, release = Event(), Event()
    original = dataset_preparation.prepare

    def held_prepare(*args, **kwargs):
        assert not service.store.connection.in_transaction
        entered.set()
        if not release.wait(10):
            raise RuntimeError("Test did not release dataset preparation")
        return original(*args, **kwargs)

    monkeypatch.setattr(dataset_preparation, "prepare", held_prepare)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            admission = pool.submit(service.invoke, operation, payload, OWNER)
            try:
                assert entered.wait(2)
                record = pending_record(observer, payload["project_id"])
                intent = observer.store.get("dispatch_intent", record["operation_id"])
                assert intent["phase"] == "preparing"
                assert intent["operation"] == operation
                assert intent["actor_id"] == OWNER.id
                assert intent["restart_policy"] == "reconcile_only_no_implicit_launch"
                assert intent["cancel_requested"] is False
                retry = pool.submit(observer.invoke, operation, payload, OWNER)
                assert retry.result(timeout=2)["operation_id"] == record["operation_id"]
                assert backend.prepared == backend.launched == []
            finally:
                release.set()
            assert admission.result(timeout=2)["status"] == "submitted"
    finally:
        observer.store.close()
        service.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
@pytest.mark.parametrize("block_at", ["prepare", "authorize"])
def test_cancel_pending_admission_prevents_launch(tmp_path, operation, block_at):
    backend = ControlledBackend(block_at=block_at)
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, operation)
    cancelling = OperationService(tmp_path / "service.sqlite3", backend=backend)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            admission = pool.submit(service.invoke, operation, payload, OWNER)
            try:
                assert backend.entered.wait(2)
                record = pending_record(cancelling, payload["project_id"])
                cancel_payload = {"project_id": payload["project_id"],
                                  "operation_id": record["operation_id"], "idempotency_key": "cancel"}
                cancel = pool.submit(cancelling.invoke, "run.cancel", cancel_payload, OWNER)
                cancelled = cancel.result(timeout=2)
                assert cancelled["status"] == "cancelled"
                assert cancelling.store.get("dispatch_intent", record["operation_id"])["phase"] == "cancelled"
                assert cancelling.invoke("run.cancel", cancel_payload, OWNER) == cancelled
                assert not admission.done()
            finally:
                backend.release.set()
            assert admission.result(timeout=2)["status"] == "cancelled"
            assert backend.launched == []
            assert all(op != "run.cancel" for op, _ in backend.calls)
            assert cancelling.invoke(operation, payload, OWNER)["status"] == "cancelled"
            status = cancelling.invoke("run.status", {"project_id": payload["project_id"],
                "operation_id": record["operation_id"]}, OWNER)
            assert status["status"] == "cancelled"
            if operation == "run.resume":
                assert service._current_recipe(payload["project_id"])["revision"] == 1
    finally:
        cancelling.store.close()
        service.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
@pytest.mark.parametrize("crash_at,phase,launched", [
    ("prepare", "preparing", 0), ("authorized", "dispatching", 0), ("launched", "dispatching", 1),
])
def test_interrupted_admission_retries_and_restarts_never_respawn(tmp_path, operation, crash_at, phase, launched):
    backend = ControlledBackend(block_at=None, crash_at=crash_at)
    path = tmp_path / "service.sqlite3"
    service = OperationService(path, backend=backend)
    backend.service = service
    payload = seed(service, operation)
    try:
        with pytest.raises(InterruptedDispatch):
            service.invoke(operation, payload, OWNER)
        records = [record for record in service.store.list("operation", payload["project_id"])
                   if record["operation_id"] != "parent-operation"]
        assert len(records) == 1
        operation_id = records[0]["operation_id"]
        intent = service.store.get("dispatch_intent", operation_id)
        assert intent["phase"] == phase
        receipt = service.invoke(operation, payload, OWNER)
        assert receipt["operation_id"] == operation_id
        assert len(backend.prepared) == 1
        assert len(backend.launched) == launched
    finally:
        service.store.close()
    restarted = OperationService(path, backend=backend)
    backend.service = restarted
    try:
        assert restarted.invoke(operation, payload, OWNER)["operation_id"] == operation_id
        assert restarted.store.get("dispatch_intent", operation_id) == intent
        assert len(backend.prepared) == 1
        assert len(backend.launched) == launched
    finally:
        restarted.store.close()


def test_pending_retry_bypasses_saturated_preparation_capacity(tmp_path):
    from modelbuilder.run_preparation import _PREPARERS

    backend = ControlledBackend()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, "run.submit")
    acquired = 0
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            admission = pool.submit(service.invoke, "run.submit", payload, OWNER)
            try:
                assert backend.entered.wait(2)
                while _PREPARERS.acquire(blocking=False):
                    acquired += 1
                retry = pool.submit(service.invoke, "run.submit", payload, OWNER)
                assert retry.result(timeout=2)["status"] == "pending"
                conflict = pool.submit(service.invoke, "run.submit", {**payload, "trusted": False}, OWNER)
                with pytest.raises(ServiceError) as error:
                    conflict.result(timeout=2)
                assert error.value.status_code == 409
                assert len(backend.prepared) == 1
            finally:
                while acquired:
                    _PREPARERS.release()
                    acquired -= 1
                backend.release.set()
            assert admission.result(timeout=2)["status"] == "submitted"
    finally:
        service.store.close()


def submit_in_process(path, operation, payload, entered, release, outcomes):
    """A fresh interpreter/connection must observe another process's reservation."""
    backend = ControlledBackend()
    backend.entered, backend.release = entered, release
    service = OperationService(path, backend=backend)
    backend.service = service
    try:
        result = service.invoke(operation, payload, OWNER)
        outcomes.put({"result": result, "prepared": len(backend.prepared), "launched": len(backend.launched)})
    except BaseException as error:
        outcomes.put({"error": type(error).__name__, "message": str(error)})
    finally:
        service.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
def test_separate_process_retry_cannot_adopt_live_reservation(tmp_path, operation):
    from multiprocessing import get_context

    path = tmp_path / "service.sqlite3"
    service = OperationService(path)
    payload = seed(service, operation)
    context = get_context("spawn")
    entered, release, retry_entered = context.Event(), context.Event(), context.Event()
    first_results, retry_results = context.Queue(), context.Queue()
    first = context.Process(target=submit_in_process,
                            args=(path, operation, payload, entered, release, first_results))
    retry = context.Process(target=submit_in_process,
                            args=(path, operation, payload, retry_entered, release, retry_results))
    started = []
    try:
        first.start()
        started.append(first)
        assert entered.wait(5)
        record = pending_record(service, payload["project_id"])
        retry.start()
        started.append(retry)
        replayed = retry_results.get(timeout=5)
        assert "error" not in replayed, replayed
        assert replayed["result"]["operation_id"] == record["operation_id"]
        assert replayed["result"]["status"] == "pending"
        assert replayed["prepared"] == replayed["launched"] == 0
        retry.join(timeout=5)
        assert retry.exitcode == 0
        assert first.is_alive()
        with pytest.raises(ServiceError) as error:
            service.invoke(operation, {**payload, "trusted": False}, OWNER)
        assert error.value.status_code == 409
        release.set()
        submitted = first_results.get(timeout=5)
        assert "error" not in submitted, submitted
        assert submitted["result"]["operation_id"] == record["operation_id"]
        assert submitted["result"]["status"] == "submitted"
        assert submitted["prepared"] == submitted["launched"] == 1
        first.join(timeout=5)
        assert first.exitcode == 0
        if operation == "run.resume":
            assert service._current_recipe(payload["project_id"])["revision"] == 2
    finally:
        release.set()
        for process in started:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        first_results.close()
        retry_results.close()
        service.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
def test_pending_polls_are_truthful_and_do_not_consult_native_backend(tmp_path, operation):
    backend = ControlledBackend()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, operation)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            admission = pool.submit(service.invoke, operation, payload, OWNER)
            try:
                assert backend.entered.wait(2)
                record = pending_record(service, payload["project_id"])
                reference = {"project_id": payload["project_id"], "operation_id": record["operation_id"]}
                status = pool.submit(service.invoke, "run.status", reference, OWNER).result(timeout=2)
                assert status["status"] == "pending"
                assert status.get("backend_run_id") is None
                for poll_operation, field in (("run.evaluations.list", "evaluations"),
                                              ("run.events.list", "events"),
                                              ("run.logs", "logs"), ("run.artifacts", "artifacts")):
                    result = pool.submit(service.invoke, poll_operation, reference, OWNER).result(timeout=2)
                    assert not result[field]
                assert backend.calls == []
                assert backend.launched == []
                assert not admission.done()
            finally:
                backend.release.set()
            assert admission.result(timeout=2)["status"] == "submitted"
    finally:
        service.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
@pytest.mark.parametrize("block_at", ["prepare", "authorize"])
@pytest.mark.parametrize("change", ["project", "policy", "recipe"])
def test_dispatch_rechecks_authoritative_inputs_at_both_boundaries(tmp_path, operation, block_at, change):
    backend = ControlledBackend(block_at=block_at)
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, operation)
    project_id = payload["project_id"]
    changing = OperationService(tmp_path / "service.sqlite3", backend=backend)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            admission = pool.submit(service.invoke, operation, payload, OWNER)
            try:
                assert backend.entered.wait(2)
                if change == "recipe":
                    current = changing._current_recipe(project_id)
                    changed = deepcopy(current)
                    changed["nodes"][0]["parameters"]["learning_rate"] = 0.02
                    mutation_operation = "recipe.update"
                    mutation_payload = {"project_id": project_id, "recipe_id": current["recipe_id"],
                        "expected_revision": current["revision"], "recipe": changed, "idempotency_key": "change"}
                else:
                    mutation_operation = change + ".update"
                    mutation_payload = {"project_id": project_id, "expected_revision": 1,
                        "changes": ({"model_name": "Changed"} if change == "project" else
                                    {"max_local_runtime_seconds": 60}), "idempotency_key": "change"}
                mutation = pool.submit(changing.invoke, mutation_operation, mutation_payload, OWNER)
                assert isinstance(mutation.result(timeout=2), dict)
                assert not admission.done()
            finally:
                backend.release.set()
            with pytest.raises(ServiceError) as error:
                admission.result(timeout=2)
            assert error.value.status_code == 409
            assert backend.launched == []
            if operation == "run.resume" and change != "recipe":
                assert service._current_recipe(project_id)["revision"] == 1
    finally:
        changing.store.close()
        service.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
@pytest.mark.parametrize("block_at", ["authorized", "launched"])
def test_cancellation_after_dispatch_fence_reaches_launched_worker(tmp_path, operation, block_at):
    backend = ControlledBackend(block_at=block_at)
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, operation)
    cancelling = OperationService(tmp_path / "service.sqlite3", backend=backend)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            admission = pool.submit(service.invoke, operation, payload, OWNER)
            try:
                assert backend.entered.wait(2)
                record = pending_record(cancelling, payload["project_id"])
                assert cancelling.store.get("dispatch_intent", record["operation_id"])["phase"] == "dispatching"
                cancel = pool.submit(cancelling.invoke, "run.cancel", {"project_id": payload["project_id"],
                    "operation_id": record["operation_id"], "idempotency_key": "cancel"}, OWNER)
                assert cancel.result(timeout=2)["status"] == "cancel_requested"
                assert cancelling.store.get("dispatch_intent", record["operation_id"])["cancel_requested"]
                assert not admission.done()
            finally:
                backend.release.set()
            submitted = admission.result(timeout=2)
            assert submitted["operation_id"] == record["operation_id"]
            assert submitted["backend_run_id"] == "native-" + record["operation_id"]
            assert submitted["status"] == "cancel_requested"
            assert len(backend.prepared) == len(backend.launched) == 1
            cancellations = [request for op, request in backend.calls if op == "run.cancel"]
            assert len(cancellations) == 2
            assert cancellations[-1]["backend_run_id"] == submitted["backend_run_id"]
            assert cancelling.invoke(operation, payload, OWNER)["status"] == "cancel_requested"
    finally:
        cancelling.store.close()
        service.store.close()


@pytest.mark.parametrize("operation", ["run.status", "run.cancel"])
@pytest.mark.parametrize("separate_connection", [False, True])
def test_slow_native_status_or_cancel_does_not_hold_sqlite_writer(tmp_path, operation, separate_connection):
    backend = ControlledBackend(block_at=operation)
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, "run.submit")
    with service.store.transaction():
        parent = service.store.get("operation", "parent-operation")
        service.store.put("operation", parent["operation_id"], payload["project_id"],
                          {**parent, "status": "running", "revision": 2}, parent["revision"])
    writing = OperationService(tmp_path / "service.sqlite3", backend=backend) if separate_connection else service
    request = {"project_id": payload["project_id"], "operation_id": "parent-operation"}
    if operation == "run.cancel":
        request["idempotency_key"] = "cancel"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            native_call = pool.submit(service.invoke, operation, request, OWNER)
            try:
                assert backend.entered.wait(2)
                assert not service.store.connection.in_transaction
                writer = pool.submit(writing.invoke, "project.create", {"slug": "during-native-io",
                    "model_name": "Other", "idempotency_key": "during-native-io"}, OWNER)
                assert writer.result(timeout=2)["slug"] == "during-native-io"
                assert not native_call.done()
            finally:
                backend.release.set()
            result = native_call.result(timeout=2)
            assert result["status"] == ("succeeded" if operation == "run.status" else "cancel_requested")
    finally:
        if separate_connection:
            writing.store.close()
        service.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
def test_final_correlation_write_failure_reconciles_same_native_run_after_restart(tmp_path, monkeypatch, operation):
    backend = ControlledBackend(block_at=None)
    path = tmp_path / "service.sqlite3"
    service = OperationService(path, backend=backend)
    backend.service = service
    payload = seed(service, operation)
    original_put = service.store.put
    faulted = []

    def fail_first_correlation(kind, resource_id, project_id, body, expected_revision=None):
        if kind == "operation" and body.get("backend_run_id") and body.get("status") == "submitted" and not faulted:
            faulted.append(resource_id)
            raise OSError("Controlled final correlation persistence failure")
        return original_put(kind, resource_id, project_id, body, expected_revision)

    monkeypatch.setattr(service.store, "put", fail_first_correlation)
    try:
        with pytest.raises(ServiceError) as error:
            service.invoke(operation, payload, OWNER)
        assert error.value.code == "backend_error"
        assert len(faulted) == len(backend.launched) == 1
        operation_id = faulted[0]
        record = service.store.get("operation", operation_id)
        assert record["status"] == "unknown"
        assert record["backend_run_id"] is None
        assert service.store.get("dispatch_intent", operation_id)["phase"] == "uncertain"
        assert service.invoke(operation, payload, OWNER)["operation_id"] == operation_id
        assert len(backend.prepared) == len(backend.launched) == 1
    finally:
        service.store.close()
    restarted = OperationService(path, backend=backend)
    backend.service = restarted
    try:
        receipt = restarted.invoke(operation, payload, OWNER)
        assert receipt["operation_id"] == operation_id
        assert receipt["status"] == "unknown"
        reconciled = restarted.invoke("run.status", {"project_id": payload["project_id"],
            "operation_id": operation_id}, OWNER)
        assert reconciled["operation_id"] == operation_id
        assert reconciled["backend_run_id"] == "native-" + operation_id
        assert reconciled["status"] == "succeeded"
        assert restarted.invoke(operation, payload, OWNER)["backend_run_id"] == reconciled["backend_run_id"]
        assert len(backend.prepared) == len(backend.launched) == 1
    finally:
        restarted.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
def test_pending_research_reservation_blocks_same_global_run_key(tmp_path, operation):
    backend = ControlledBackend(block_at=None)
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, operation)
    try:
        with service.store.transaction():
            service.store.connection.execute("INSERT INTO research_requests VALUES(?,?,?,?,?,?,?)",
                (OWNER.id, payload["idempotency_key"], "research.watch.refresh_due", "research-payload-hash",
                 "research-owner", "2999-01-01T00:00:00+00:00", "{}"))
        with pytest.raises(ServiceError) as error:
            service.invoke(operation, payload, OWNER)
        assert error.value.code == "idempotency_conflict"
        assert error.value.status_code == 409
        assert backend.prepared == backend.launched == []
        assert len(service.store.list("operation", payload["project_id"])) == 1
        assert service.store.list("dispatch_intent", payload["project_id"]) == []
    finally:
        service.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
def test_pending_run_reservation_blocks_same_global_research_key(tmp_path, operation):
    backend = ControlledBackend()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, operation)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            admission = pool.submit(service.invoke, operation, payload, OWNER)
            try:
                assert backend.entered.wait(2)
                refresh = pool.submit(service.invoke, "research.watch.refresh_due",
                    {"project_id": payload["project_id"], "idempotency_key": payload["idempotency_key"]}, OWNER)
                with pytest.raises(ServiceError) as error:
                    refresh.result(timeout=2)
                assert error.value.code == "idempotency_conflict"
                assert error.value.status_code == 409
                assert service.store.connection.execute("SELECT COUNT(*) FROM research_requests").fetchone()[0] == 0
                assert not admission.done()
            finally:
                backend.release.set()
            assert admission.result(timeout=2)["status"] == "submitted"
            assert len(backend.prepared) == len(backend.launched) == 1
    finally:
        service.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
@pytest.mark.parametrize("launch_error", [False, True])
def test_late_launch_response_cannot_regress_reconciled_terminal_state(
        tmp_path, monkeypatch, operation, terminal, launch_error):
    backend = ControlledBackend(block_at="launched")
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, operation)
    original_invoke, original_launch = backend.invoke, backend.launch_prepared

    def observe_terminal(op, request, actor):
        if op == ("run.cancel" if terminal == "cancelled" else "run.status"):
            return {"operation_id": request["operation_id"], "project_id": request["project_id"],
                    "backend_run_id": "native-" + request["operation_id"],
                    "status": terminal, "worker_alive": False}
        return original_invoke(op, request, actor)

    def finish_launch(handle, *, authorize):
        result = original_launch(handle, authorize=authorize)
        if launch_error:
            raise RuntimeError("Controlled late launch response failure")
        return result

    monkeypatch.setattr(backend, "invoke", observe_terminal)
    monkeypatch.setattr(backend, "launch_prepared", finish_launch)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            admission = pool.submit(service.invoke, operation, payload, OWNER)
            try:
                assert backend.entered.wait(2)
                record = pending_record(service, payload["project_id"])
                reference = {"project_id": payload["project_id"], "operation_id": record["operation_id"]}
                if terminal == "cancelled":
                    reference["idempotency_key"] = "cancel"
                observation = pool.submit(service.invoke,
                    "run.cancel" if terminal == "cancelled" else "run.status", reference, OWNER)
                assert observation.result(timeout=2)["status"] == terminal
                assert not admission.done()
            finally:
                backend.release.set()
            if launch_error:
                with pytest.raises(ServiceError) as error:
                    admission.result(timeout=2)
                assert error.value.code == "backend_error"
            else:
                assert admission.result(timeout=2)["status"] == terminal
            latest = service.store.get("operation", record["operation_id"])
            assert latest["status"] == terminal
            assert latest["backend_run_id"] == "native-" + record["operation_id"]
            assert service.invoke(operation, payload, OWNER)["status"] == terminal
            assert len(backend.prepared) == len(backend.launched) == 1
    finally:
        service.store.close()


@pytest.mark.parametrize("operation", ["run.status", "run.cancel"])
@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
def test_late_missing_native_response_returns_newer_terminal_correlation(tmp_path, monkeypatch, operation, terminal):
    backend = ControlledBackend(block_at=None, crash_at="authorized")
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, "run.submit")
    entered, release = Event(), Event()
    first_call, guard = [], Lock()

    def native_response(op, request, actor):
        with guard:
            hold = not first_call
            if hold:
                first_call.append(op)
        if hold:
            assert op == operation
            assert not service.store.connection.in_transaction
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Test did not release the missing native response")
            raise ServiceError("not_found", "Controlled missing native result", status_code=404)
        return {"operation_id": request["operation_id"], "project_id": request["project_id"],
                "backend_run_id": "native-" + request["operation_id"],
                "status": terminal, "worker_alive": False}

    try:
        with pytest.raises(InterruptedDispatch):
            service.invoke("run.submit", payload, OWNER)
        receipt = service.invoke("run.submit", payload, OWNER)
        reference = {"project_id": payload["project_id"], "operation_id": receipt["operation_id"]}
        request = {**reference, **({"idempotency_key": "first-cancel"} if operation == "run.cancel" else {})}
        monkeypatch.setattr(backend, "invoke", native_response)
        with ThreadPoolExecutor(max_workers=2) as pool:
            stale = pool.submit(service.invoke, operation, request, OWNER)
            try:
                assert entered.wait(2)
                observed = service.invoke("run.status", reference, OWNER)
                assert observed["status"] == terminal
                assert not stale.done()
            finally:
                release.set()
            assert stale.result(timeout=2)["status"] == terminal
            assert service.store.get("operation", receipt["operation_id"])["status"] == terminal
            assert backend.launched == []
    finally:
        service.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
def test_replayed_receipt_does_not_mix_stale_worker_liveness_with_confirmed_cancellation(tmp_path, monkeypatch, operation):
    backend = ControlledBackend(block_at=None)
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, operation)
    original_invoke = backend.invoke

    def confirmed_cancel(op, request, actor):
        if op == "run.cancel":
            return {"operation_id": request["operation_id"], "backend_run_id": request["backend_run_id"],
                    "status": "cancelled", "worker_alive": False, "confirmed": True}
        return original_invoke(op, request, actor)

    monkeypatch.setattr(backend, "invoke", confirmed_cancel)
    try:
        submitted = service.invoke(operation, payload, OWNER)
        assert submitted["worker_alive"] is True
        cancelled = service.invoke("run.cancel", {"project_id": payload["project_id"],
            "operation_id": submitted["operation_id"], "idempotency_key": "cancel"}, OWNER)
        assert cancelled["status"] == "cancelled"
        assert cancelled["worker_alive"] is False
        replayed = service.invoke(operation, payload, OWNER)
        assert replayed["status"] == "cancelled"
        assert replayed.get("worker_alive") is not True
        assert len(backend.prepared) == len(backend.launched) == 1
    finally:
        service.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
def test_existing_native_attempt_read_failure_without_authorize_stays_uncertain(tmp_path, monkeypatch, operation):
    backend = ControlledBackend(block_at=None)
    path = tmp_path / "service.sqlite3"
    service = OperationService(path, backend=backend)
    backend.service = service
    payload = seed(service, operation)
    attempted_reads = []

    def existing_attempt_read(handle, *, authorize):
        # Native launch evidence may already exist. Reconciliation is allowed to
        # return without requesting a fresh launch authorization, but its failure
        # does not prove the original worker never started.
        attempted_reads.append(handle)
        raise ServiceError("backend_timeout", "Controlled existing-attempt status timeout", status_code=504)

    monkeypatch.setattr(backend, "launch_prepared", existing_attempt_read)
    try:
        with pytest.raises(ServiceError) as error:
            service.invoke(operation, payload, OWNER)
        assert error.value.code == "backend_timeout"
        receipt = service.invoke(operation, payload, OWNER)
        operation_id = receipt["operation_id"]
        assert receipt["status"] == "unknown"
        assert receipt["dispatch_state"] == "uncertain"
        assert service.store.get("dispatch_intent", operation_id)["phase"] == "uncertain"
        assert len(backend.prepared) == len(attempted_reads) == 1
        assert backend.launched == []
    finally:
        service.store.close()
    restarted = OperationService(path, backend=backend)
    backend.service = restarted
    try:
        replayed = restarted.invoke(operation, payload, OWNER)
        assert replayed["operation_id"] == operation_id
        assert replayed["status"] == "unknown"
        assert len(backend.prepared) == len(attempted_reads) == 1
        assert backend.launched == []
    finally:
        restarted.store.close()


@pytest.mark.parametrize("separate_connection", [False, True])
def test_distinct_resume_keys_share_one_revision_fence_and_only_one_launch(tmp_path, monkeypatch, separate_connection):
    backend = ControlledBackend()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, "run.resume")
    competing = OperationService(tmp_path / "service.sqlite3", backend=backend) if separate_connection else service
    both_prepared = Event()
    original_gate = backend._gate

    def observe_both_preparations(phase):
        if phase == "prepare":
            with backend.guard:
                if len(backend.prepared) == 2:
                    both_prepared.set()
        return original_gate(phase)

    monkeypatch.setattr(backend, "_gate", observe_both_preparations)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.invoke, "run.resume", payload, OWNER)
            try:
                assert backend.entered.wait(2)
                second = pool.submit(competing.invoke, "run.resume", {**payload, "idempotency_key": "other-resume"}, OWNER)
                assert both_prepared.wait(2)
                assert service._current_recipe(payload["project_id"])["revision"] == 1
                pending = [record for record in service.store.list("operation", payload["project_id"])
                           if record["operation_id"] != "parent-operation"]
                assert len(pending) == 2
                assert all(record["status"] == "pending" for record in pending)
                assert backend.launched == []
            finally:
                backend.release.set()
            results, errors = [], []
            for future in (first, second):
                try:
                    results.append(future.result(timeout=2))
                except ServiceError as error:
                    errors.append(error)
            assert len(results) == len(errors) == 1
            assert results[0]["status"] == "submitted"
            assert errors[0].code == "revision_conflict"
            assert len(backend.prepared) == 2
            assert len(backend.launched) == 1
            assert service._current_recipe(payload["project_id"])["revision"] == 2
            assert {intent["phase"] for intent in service.store.list("dispatch_intent", payload["project_id"])} == {
                "dispatched", "failed"}
    finally:
        if separate_connection:
            competing.store.close()
        service.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
def test_uncertain_backend_reply_is_durable_and_reconciles_without_respawn(tmp_path, operation):
    class UncertainBackend(ControlledBackend):
        def launch_prepared(self, handle, *, authorize):
            result = super().launch_prepared(handle, authorize=authorize)
            return {**result, "backend_run_id": None, "status": "unknown", "worker_alive": False,
                    "launch_state": "uncertain", "reconciliation_required": True}

    backend = UncertainBackend(block_at=None)
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, operation)
    try:
        result = service.invoke(operation, payload, OWNER)
        operation_id = result["operation_id"]
        assert result["backend_run_id"] is None
        assert result["status"] == "unknown"
        assert result["dispatch_state"] == "uncertain"
        assert service.store.get("dispatch_intent", operation_id)["phase"] == "uncertain"
        replayed = service.invoke(operation, payload, OWNER)
        assert replayed["status"] == "unknown" and len(backend.launched) == 1
        status = service.invoke("run.status", {"project_id": payload["project_id"],
                                "operation_id": operation_id}, OWNER)
        assert status["status"] == "succeeded"
        assert status["dispatch_state"] == "dispatched"
        assert service.store.get("dispatch_intent", operation_id)["phase"] == "dispatched"
        assert len(backend.launched) == 1
    finally:
        service.store.close()
