"""Recheck the exact requesting grant before returning unlocked native responses."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

import pytest

from modelbuilder.api import create_app
from modelbuilder.auth import TokenAuth
from modelbuilder.models import Actor, ServiceError
from modelbuilder.recipe import default_recipe
from modelbuilder.service import OperationService


OWNER = Actor("owner", {"read", "modify", "execute"})
READ_OPERATIONS = ("run.logs", "run.artifacts", "run.evaluations.list", "run.events.list")
PRIVATE_RESULT = "private-native-response-fixture"


class HeldNativeResponse:
    """A native read or cancellation finishes while its reply remains held."""

    def __init__(self):
        self.entered, self.release = Event(), Event()
        self.calls, self.cancel_effects, self.produced = [], [], []
        self.service = None
        self.fail_response = False

    def invoke(self, operation, payload, actor):
        assert operation in {*READ_OPERATIONS, "run.cancel"}
        assert not self.service.store.connection.in_transaction
        assert self.service.operation_authorizer(operation, actor, payload["project_id"])
        self.calls.append((operation, deepcopy(payload), actor))
        if operation == "run.cancel":
            self.cancel_effects.append(payload["operation_id"])
            response = {"operation_id": payload["operation_id"], "backend_run_id": payload["backend_run_id"],
                        "status": "cancelled", "worker_alive": False, "detail": PRIVATE_RESULT}
        elif operation == "run.logs":
            response = {"logs": PRIVATE_RESULT, "cursor": "1"}
        elif operation == "run.artifacts":
            response = {"artifacts": [{"artifact_id": PRIVATE_RESULT}]}
        elif operation == "run.evaluations.list":
            response = {"evaluations": [{"evaluation_id": PRIVATE_RESULT}]}
        else:
            response = {"events": [{"message": PRIVATE_RESULT}], "next_cursor": 1}
        self.entered.set()
        if not self.release.wait(10):
            raise RuntimeError("Test did not release native response")
        self.produced.append(response)
        if self.fail_response:
            raise ServiceError("backend_error", PRIVATE_RESULT, {"backend_detail": PRIVATE_RESULT}, 502)
        return response


@pytest.fixture
def authorized_native_response(tmp_path):
    backend = HeldNativeResponse()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    body = default_recipe("template", "template", "training")
    body["nodes"] = [{"id": "train", "kind": "model.train", "implementation_ref": "builtin:model.train",
        "inputs": [], "outputs": [], "parameters": {"steps": 4, "learning_rate": 0.1},
        "environment_ref": "default", "resource_request": {},
        "retry": {"max_attempts": 1, "backoff_seconds": 0}, "timeout": 30,
        "cache_policy": "disabled", "provenance": {}}]
    project = service.invoke("project.create", {"slug": "native-response", "model_name": "Native response",
        "recipe": body, "idempotency_key": "project"}, OWNER)
    recipe = service.store.get("recipe", project["recipe_id"])
    with service.store.transaction():
        service.store.put("operation", "running-operation", project["project_id"], {
            "operation_id": "running-operation", "project_id": project["project_id"],
            "backend_run_id": "native-run", "recipe_id": recipe["recipe_id"],
            "recipe_revision": recipe["revision"], "recipe_digest": recipe["digest"],
            "status": "running", "revision": 1})
    auth = TokenAuth({"request-token": OWNER, "surviving-token": OWNER})
    create_app(service, auth)
    actor = auth.authenticate("Bearer request-token")
    reference = {"project_id": project["project_id"], "operation_id": "running-operation"}
    try:
        yield service, backend, auth, actor, reference
    finally:
        backend.release.set()
        service.store.close()


@pytest.mark.parametrize("operation", [*READ_OPERATIONS, "run.cancel"])
@pytest.mark.parametrize("change", ["remove", "replace"])
@pytest.mark.parametrize("failed_response", [False, True])
def test_unlocked_native_reply_requires_its_exact_original_grant(
        authorized_native_response, operation, change, failed_response):
    service, backend, auth, actor, reference = authorized_native_response
    backend.fail_response = failed_response
    request = {**reference, **({"idempotency_key": "cancel"} if operation == "run.cancel" else {})}
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(service.invoke, operation, request, actor)
        try:
            assert backend.entered.wait(2)
            assert len(backend.calls) == 1
            assert backend.calls[0][2] is actor
            if change == "remove":
                del auth._tokens["request-token"]
            else:
                auth._tokens["request-token"] = Actor("owner", OWNER.scopes)
            surviving = auth.authenticate("Bearer surviving-token")
            assert surviving.id == actor.id
            assert surviving is not actor
            assert auth.authorize_operation(operation, surviving, reference["project_id"])
            assert not auth.authorize_operation(operation, actor, reference["project_id"])
            if operation == "run.cancel":
                # The action was authorized and applied before revocation. Only
                # returning the later private native reply loses authorization.
                assert backend.cancel_effects == [reference["operation_id"]]
            assert not response.done()
        finally:
            backend.release.set()
        with pytest.raises(ServiceError) as error:
            response.result(timeout=2)
        assert error.value.code == "forbidden"
        assert error.value.status_code == 403
        assert PRIVATE_RESULT not in error.value.message
        assert PRIVATE_RESULT not in repr(error.value.details)
    assert len(backend.calls) == len(backend.produced) == 1
    assert PRIVATE_RESULT in repr(backend.produced[0])
    assert backend.cancel_effects == ([reference["operation_id"]] if operation == "run.cancel" else [])
    if operation == "run.cancel":
        assert service.store.get("operation", reference["operation_id"])["status"] == "cancel_requested"


@pytest.mark.parametrize("operation", [*READ_OPERATIONS, "run.cancel"])
def test_unrevoked_native_reply_returns_authorized_result(authorized_native_response, operation):
    service, backend, _, actor, reference = authorized_native_response
    request = {**reference, **({"idempotency_key": "cancel"} if operation == "run.cancel" else {})}
    backend.release.set()
    result = service.invoke(operation, request, actor)
    assert PRIVATE_RESULT in repr(result)
    assert len(backend.calls) == 1
    assert backend.cancel_effects == ([reference["operation_id"]] if operation == "run.cancel" else [])


@pytest.mark.parametrize("operation", [*READ_OPERATIONS, "run.cancel"])
def test_unlocked_native_reply_rechecks_operation_project_ownership(authorized_native_response, operation):
    service, backend, _, actor, reference = authorized_native_response
    request = {**reference, **({"idempotency_key": "cancel"} if operation == "run.cancel" else {})}
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(service.invoke, operation, request, actor)
        try:
            assert backend.entered.wait(2)
            with service.store.transaction():
                record = service.store.get("operation", reference["operation_id"])
                service.store.put("operation", record["operation_id"], "other-project",
                    {**record, "project_id": "other-project", "revision": record["revision"] + 1}, record["revision"])
        finally:
            backend.release.set()
        with pytest.raises(ServiceError) as error:
            response.result(timeout=2)
        assert error.value.code == "not_found"
        assert error.value.status_code == 404
        assert PRIVATE_RESULT not in error.value.message
        assert PRIVATE_RESULT not in repr(error.value.details)
    assert len(backend.calls) == len(backend.produced) == 1
    assert backend.cancel_effects == ([reference["operation_id"]] if operation == "run.cancel" else [])


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
@pytest.mark.parametrize("change", ["remove", "replace"])
@pytest.mark.parametrize("failed_response", [False, True])
def test_revocation_after_launch_fence_withholds_reply_and_keeps_dispatch_state(
        authorized_native_response, monkeypatch, operation, change, failed_response):
    service, backend, auth, actor, reference = authorized_native_response
    prepared, launched = [], []
    project = service.store.get("project", reference["project_id"])
    request = {"project_id": reference["project_id"], "recipe_id": project["recipe_id"],
               "expected_revision": 1, "trusted": True, "idempotency_key": "submit"}

    if operation == "run.resume":
        with service.store.transaction():
            parent = service.store.get("operation", reference["operation_id"])
            service.store.put("operation", parent["operation_id"], parent["project_id"],
                {**parent, "status": "succeeded", "revision": parent["revision"] + 1}, parent["revision"])
        request = {**reference, "expected_recipe_revision": 1, "mode": "warm_start", "steps": 6,
                   "trusted": True, "idempotency_key": "resume"}

    def prepare_execution(requested_operation, payload):
        assert requested_operation == operation
        assert not service.store.connection.in_transaction
        handle = deepcopy(payload)
        prepared.append(handle)
        return handle

    def launch_prepared(handle, *, authorize):
        assert handle is prepared[0]
        authorize()
        assert not service.store.connection.in_transaction
        launched.append(handle["operation_id"])
        backend.entered.set()
        if not backend.release.wait(10):
            raise RuntimeError("Test did not release admitted launch")
        if failed_response:
            raise ServiceError("backend_error", PRIVATE_RESULT, {"backend_detail": PRIVATE_RESULT}, 502)
        return {"operation_id": handle["operation_id"], "project_id": handle["project_id"],
                "backend_run_id": "native-" + handle["operation_id"], "status": "submitted",
                "worker_alive": True, "detail": PRIVATE_RESULT}

    monkeypatch.setattr(backend, "prepare_execution", prepare_execution, raising=False)
    monkeypatch.setattr(backend, "launch_prepared", launch_prepared, raising=False)
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(service.invoke, operation, request, actor)
        try:
            assert backend.entered.wait(2)
            assert len(launched) == 1
            assert service.store.get("dispatch_intent", launched[0])["phase"] == "dispatching"
            if change == "remove":
                del auth._tokens["request-token"]
            else:
                auth._tokens["request-token"] = Actor("owner", OWNER.scopes)
            surviving = auth.authenticate("Bearer surviving-token")
            assert auth.authorize_operation(operation, surviving, reference["project_id"])
            assert not auth.authorize_operation(operation, actor, reference["project_id"])
        finally:
            backend.release.set()
        with pytest.raises(ServiceError) as error:
            response.result(timeout=2)
        assert error.value.code == "forbidden"
        assert error.value.status_code == 403
        assert PRIVATE_RESULT not in error.value.message
        assert PRIVATE_RESULT not in repr(error.value.details)
    record = service.store.get("operation", launched[0])
    assert record["status"] == ("unknown" if failed_response else "submitted")
    assert record["backend_run_id"] == (None if failed_response else "native-" + launched[0])
    assert service.store.get("dispatch_intent", launched[0])["phase"] == (
        "uncertain" if failed_response else "dispatched")
    if operation == "run.resume":
        assert service._current_recipe(reference["project_id"])["revision"] == 2
    replayed = service.invoke(operation, request, surviving)
    assert replayed["operation_id"] == launched[0]
    assert replayed["status"] == record["status"]
    assert len(prepared) == len(launched) == 1
