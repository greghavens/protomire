"""HTTP/MCP admission parity, using real transports and an inert gated backend.

The official MCP SDK runs over in-memory streams, and its OperationGateway calls
our HTTP ASGI app. No native backend process or model is executed.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from threading import Event

import httpx
from jsonschema import Draft202012Validator
from mcp.shared.memory import create_connected_server_and_client_session
import pytest

from modelbuilder.api import create_app
from modelbuilder.auth import TokenAuth
from modelbuilder.mcp_server import OperationGateway, create_server
from modelbuilder.models import Actor
from modelbuilder.recipe import default_recipe
from modelbuilder.service import OperationService


OWNER = Actor("transport-owner", {"read", "modify", "execute"})
RUN_SCHEMA = json.loads((Path(__file__).resolve().parents[2] / "schemas/run-status.v0.schema.json").read_text())


class HeldAdmission:
    def __init__(self):
        self.entered, self.release = Event(), Event()
        self.preparations, self.launches = [], []
        self.service = None

    def prepare_execution(self, operation, payload):
        assert not self.service.store.connection.in_transaction
        handle = (operation, deepcopy(payload))
        self.preparations.append(handle)
        self.entered.set()
        if not self.release.wait(10):
            raise RuntimeError("Controlled transport preparation was not released")
        return handle

    def launch_prepared(self, handle, *, authorize):
        authorize()
        self.launches.append(handle)
        raise AssertionError("Cancelled or revoked admission reached launch")

    def invoke(self, operation, payload, actor):
        raise AssertionError("A pending correlation must not call a native backend")


def seed(service, operation):
    recipe = default_recipe("template", "template", "transport-admission")
    recipe["nodes"] = [{"id": "train", "kind": "model.train", "implementation_ref": "builtin:model.train",
        "inputs": [], "outputs": [], "parameters": {"steps": 4, "learning_rate": 0.1},
        "environment_ref": "default", "resource_request": {},
        "retry": {"max_attempts": 1, "backoff_seconds": 0}, "timeout": 30,
        "cache_policy": "disabled", "provenance": {}}]
    project = service.invoke("project.create", {"slug": "transport", "model_name": "Transport",
        "recipe": recipe, "idempotency_key": "project"}, OWNER)
    if operation == "run.resume":
        recipe = service.store.get("recipe", project["recipe_id"])
        # A declared historical fixture, never an identity synthesized for pending work.
        with service.store.transaction():
            service.store.put("operation", "fixture-parent", project["project_id"], {
                "operation_id": "fixture-parent", "project_id": project["project_id"],
                "backend_run_id": "fixture-native-parent", "recipe_id": recipe["recipe_id"],
                "recipe_revision": 1, "recipe_digest": recipe["digest"], "status": "succeeded", "revision": 1})
        return {"project_id": project["project_id"], "operation_id": "fixture-parent",
                "expected_recipe_revision": 1, "mode": "warm_start", "steps": 6,
                "trusted": True, "idempotency_key": "one-admission"}
    return {"project_id": project["project_id"], "recipe_id": project["recipe_id"],
            "expected_revision": 1, "trusted": True, "idempotency_key": "one-admission"}


def assert_receipt(receipt, status):
    Draft202012Validator(RUN_SCHEMA).validate(receipt)
    assert receipt["status"] == status
    assert "backend_run_id" in receipt and receipt["backend_run_id"] is None
    assert receipt["operation_id"]


@asynccontextmanager
async def transports(app):
    """Exercise SDK framing/serialization as well as the HTTP gateway and auth."""
    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.ASGITransport(app=app),
                          headers={"Authorization": "Bearer initiating-token"}) as original,
        httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.ASGITransport(app=app),
                          headers={"Authorization": "Bearer surviving-token"}) as observer,
    ):
        catalog = (await original.get("/v1/catalog")).json()
        async with (
            create_connected_server_and_client_session(create_server(OperationGateway(original), catalog),
                                                       read_timeout_seconds=timedelta(seconds=5)) as original_mcp,
            create_connected_server_and_client_session(create_server(OperationGateway(observer), catalog),
                                                       read_timeout_seconds=timedelta(seconds=5)) as observer_mcp,
        ):
            listed = {tool.name: tool for tool in (await original_mcp.list_tools()).tools}
            for entry in catalog["operations"]:
                if entry["name"] in {"run.submit", "run.resume", "run.cancel", "run.status"}:
                    assert listed[entry["name"].replace(".", "_")].inputSchema == entry["input_schema"]

            async def invoke(transport, operation, payload, *, surviving=False):
                if transport == "http":
                    client = observer if surviving else original
                    response = await client.post("/v1/operations/" + operation, json=payload)
                    return response.status_code, response.json()
                session = observer_mcp if surviving else original_mcp
                result = await session.call_tool(operation.replace(".", "_"), payload)
                assert json.loads(result.content[0].text) == result.structuredContent
                body = deepcopy(result.structuredContent)
                status = body.pop("http_status") if result.isError else 200
                return status, body

            yield invoke


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
@pytest.mark.parametrize("initiating_transport", ["http", "mcp"])
async def test_pending_retry_status_and_cancel_match_across_transports(tmp_path, operation, initiating_transport):
    backend = HeldAdmission()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, operation)
    auth = TokenAuth({"initiating-token": OWNER, "surviving-token": OWNER})
    app = create_app(service, auth)
    try:
        async with transports(app) as call:
            admitted = asyncio.create_task(call(initiating_transport, operation, payload))
            try:
                assert await asyncio.to_thread(backend.entered.wait, 2)
                code, pending_http = await call("http", operation, payload, surviving=True)
                assert code == 200
                assert_receipt(pending_http, "pending")
                code, pending_mcp = await call("mcp", operation, {**payload, "request_id": "retried"}, surviving=True)
                assert code == 200 and pending_mcp == pending_http
                run = {"project_id": payload["project_id"], "operation_id": pending_http["operation_id"]}
                for transport in ("http", "mcp"):
                    code, status = await call(transport, "run.status", run, surviving=True)
                    assert code == 200 and status == pending_http
                    assert_receipt(status, "pending")
                assert len(backend.preparations) == 1 and not backend.launches
                cancel = {**run, "idempotency_key": "one-cancel"}
                code, cancelled = await call("mcp" if initiating_transport == "http" else "http",
                                             "run.cancel", cancel, surviving=True)
                assert code == 200
                assert_receipt(cancelled, "cancelled")
                for transport in ("http", "mcp"):
                    code, repeated = await call(transport, "run.cancel", cancel, surviving=True)
                    assert code == 200 and repeated == cancelled
                    code, status = await call(transport, "run.status", run, surviving=True)
                    assert code == 200 and status == cancelled
                assert not admitted.done()
            finally:
                backend.release.set()
                outcome = await asyncio.wait_for(admitted, timeout=3)
            assert outcome == (200, cancelled)
            assert not backend.launches and len(backend.preparations) == 1
            for transport in ("http", "mcp"):
                assert await call(transport, operation, payload, surviving=True) == (200, cancelled)
            assert service._current_recipe(payload["project_id"])["revision"] == 1
    finally:
        backend.release.set()
        service.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
@pytest.mark.parametrize("initiating_transport", ["http", "mcp"])
@pytest.mark.parametrize("change", ["remove", "replace"])
async def test_exact_transport_grant_cannot_be_revived_by_same_actor_token(tmp_path, operation, initiating_transport, change):
    backend = HeldAdmission()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed(service, operation)
    auth = TokenAuth({"initiating-token": OWNER, "surviving-token": OWNER})
    app = create_app(service, auth)
    original_grant = auth.authenticate("Bearer initiating-token")
    try:
        async with transports(app) as call:
            admitted = asyncio.create_task(call(initiating_transport, operation, payload))
            try:
                assert await asyncio.to_thread(backend.entered.wait, 2)
                code, pending = await call("http", operation, payload, surviving=True)
                assert code == 200
                assert_receipt(pending, "pending")
                if change == "remove":
                    del auth._tokens["initiating-token"]
                else:
                    auth._tokens["initiating-token"] = Actor(OWNER.id, OWNER.scopes)
                assert not auth.authorize_operation(operation, original_grant, payload["project_id"])
                assert auth.authorize_operation(operation, auth.authenticate("Bearer surviving-token"), payload["project_id"])
            finally:
                backend.release.set()
                code, error = await asyncio.wait_for(admitted, timeout=3)
            assert code == 403 and error["error"]["code"] == "forbidden"
            assert not backend.launches and len(backend.preparations) == 1
            run = {"project_id": payload["project_id"], "operation_id": pending["operation_id"]}
            for transport in ("http", "mcp"):
                code, failed = await call(transport, "run.status", run, surviving=True)
                assert code == 200
                assert_receipt(failed, "failed")
                assert failed["errors"][0]["code"] == "forbidden"
                assert await call(transport, operation, payload, surviving=True) == (200, failed)
            assert not backend.launches and len(backend.preparations) == 1
            assert service._current_recipe(payload["project_id"])["revision"] == 1
    finally:
        backend.release.set()
        service.store.close()
