"""Transport unit tests. Real external protocol acceptance is a separate suite."""
from __future__ import annotations

import json

import httpx
import pytest

from modelbuilder.mcp_server import (
    Configuration,
    ConfigurationError,
    OperationError,
    OperationGateway,
    catalog_tools,
    create_server,
    tool_result,
)


@pytest.mark.parametrize("url", [
    "file:///tmp/api", "http://user:password@localhost:8765", "http://example.com:8765",
    "http://127.0.0.1:8765?token=hidden", "http://127.0.0.1:8765/#hidden", "http://localhost:bad",
])
def test_reject_unsafe_configuration_without_leaking_url(url):
    with pytest.raises(ConfigurationError) as result:
        Configuration(api_url=url, token="do-not-log")
    assert url not in str(result.value)
    assert "do-not-log" not in str(result.value)


def test_configuration_requires_explicit_identity(monkeypatch):
    monkeypatch.delenv("MODELBUILDER_TOKEN", raising=False)
    with pytest.raises(ConfigurationError, match="credential"):
        Configuration.from_environment()
    assert "a-secret-token" not in repr(Configuration(token="a-secret-token"))


@pytest.mark.parametrize("timeout", [0, -1, 301, float("nan"), float("inf")])
def test_timeout_is_finite_and_bounded(timeout):
    with pytest.raises(ConfigurationError):
        Configuration(token="test", timeout=timeout)


def test_tool_names_and_schemas_are_generated_from_service():
    schema = {"type": "object", "additionalProperties": False, "properties": {"recipe_id": {"type": "string"}}}
    tools, aliases = catalog_tools({"operations": [
        {"name": "recipe.get", "input_schema": schema, "scope": "read", "description": "Get recipe"},
        {"name": "model.version.promote", "input_schema": schema, "scope": "modify"},
    ]})
    assert aliases == {"recipe_get": "recipe.get", "model_version_promote": "model.version.promote"}
    assert tools[0].inputSchema == schema
    assert tools[0].annotations.readOnlyHint is True
    assert tools[1].annotations.readOnlyHint is False


@pytest.mark.parametrize("catalog", [
    {}, {"operations": [{}]}, {"operations": [{"name": "../../secrets", "input_schema": {}}]},
    {"operations": [{"name": "a.b_c", "input_schema": {}}, {"name": "a_b.c", "input_schema": {}}]},
])
def test_bad_catalog_fails_closed(catalog):
    with pytest.raises(ConfigurationError):
        catalog_tools(catalog)


async def test_gateway_preserves_request_response_and_authentication():
    received = []
    recipe = {"recipe_id": "same-id", "revision": 3, "digest": "same-digest"}

    def responder(request):
        received.append(request)
        return httpx.Response(200, json=recipe)

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8765", headers={"Authorization": "Bearer ephemeral"},
        transport=httpx.MockTransport(responder),
    ) as client:
        result = await OperationGateway(client).invoke("recipe.update", {"expected_revision": 2})
    assert result == recipe
    assert received[0].url.path == "/v1/operations/recipe.update"
    assert received[0].headers["Authorization"] == "Bearer ephemeral"
    assert json.loads(received[0].content) == {"expected_revision": 2}
    encoded = tool_result(result)
    assert encoded.structuredContent == recipe
    assert json.loads(encoded.content[0].text) == recipe
    assert not encoded.isError


async def test_gateway_preserves_409_without_retrying():
    seen = []
    body = {"error": {"code": "revision_conflict", "message": "Changed", "details": {"current_revision": 4}}}

    def responder(request):
        seen.append(request)
        return httpx.Response(409, json=body)

    async with httpx.AsyncClient(base_url="http://localhost", transport=httpx.MockTransport(responder)) as client:
        with pytest.raises(OperationError) as result:
            await OperationGateway(client).invoke("recipe.update", {})
    assert result.value.status == 409
    assert result.value.body == body
    assert len(seen) == 1


@pytest.mark.parametrize("status,body,code", [
    (200, "not-json", "invalid_service_response"),
    (200, "[]", "invalid_service_response"),
    (500, '{"debug":"do-not-forward"}', "service_error"),
    (302, '{"debug":"redirect"}', "service_error"),
])
async def test_unstructured_upstream_errors_are_not_exposed(status, body, code):
    async with httpx.AsyncClient(
        base_url="http://localhost", transport=httpx.MockTransport(lambda _: httpx.Response(status, text=body)),
    ) as client:
        with pytest.raises(OperationError) as result:
            await OperationGateway(client).catalog()
    assert result.value.body["error"]["code"] == code
    assert "do-not-forward" not in json.dumps(result.value.body)


async def test_connection_failure_does_not_include_exception_secrets():
    def responder(request):
        raise httpx.ConnectError("Authorization: Bearer do-not-log", request=request)

    async with httpx.AsyncClient(base_url="http://localhost", transport=httpx.MockTransport(responder)) as client:
        with pytest.raises(OperationError) as result:
            await OperationGateway(client).catalog()
    assert result.value.status == 503
    assert "do-not-log" not in json.dumps(result.value.body)


def test_adapter_has_no_application_imports():
    # Structural regression check: adapters cannot depend on domain internals.
    import ast
    import inspect
    import modelbuilder.mcp_server as module
    tree = ast.parse(inspect.getsource(module))
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(name and name.startswith("modelbuilder.") for name in modules)
    server = create_server(None, {"operations": []})
    assert server.name == "modelbuilder"


@pytest.mark.parametrize("operation,payload", [
    ("recipe.create", {"project_id": "project", "expected_revision": 4, "recipe": {"name": "Built recipe"},
                       "idempotency_key": "author"}),
    ("recipe.import", {"project_id": "project", "expected_revision": 4, "bundle": {"recipe": {}},
                       "idempotency_key": "import"}),
    ("recipe.clone", {"project_id": "project", "recipe_id": "stable-recipe", "expected_revision": 4,
                      "source_revision": 2, "idempotency_key": "restore"}),
])
async def test_author_import_restore_preserve_shared_revision_contract(operation, payload):
    """The adapter must not invent identities or strip service concurrency checks."""
    received = []
    revision = {"project_id": "project", "recipe_id": "stable-recipe", "revision": 5, "digest": "digest-r5"}

    def responder(request):
        received.append(request)
        return httpx.Response(200, json=revision)

    async with httpx.AsyncClient(base_url="http://localhost", transport=httpx.MockTransport(responder)) as client:
        result = await OperationGateway(client).invoke(operation, payload)
    assert len(received) == 1
    assert received[0].url.path == "/v1/operations/" + operation
    assert json.loads(received[0].content) == payload
    assert result == revision
    assert tool_result(result).structuredContent == revision
