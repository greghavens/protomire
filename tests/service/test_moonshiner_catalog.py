"""Moonshiner discovery shares the inert HTTP/MCP component catalog."""
import httpx
import pytest

from modelbuilder.api import create_app
from modelbuilder.backend.components.moonshiner import catalog_entries
from modelbuilder.mcp_server import OperationGateway
from modelbuilder.models import Actor
from modelbuilder.service import OperationService


@pytest.mark.asyncio
async def test_moonshiner_http_mcp_catalog_parity_without_upstream(tmp_path):
    service = OperationService(tmp_path / "catalog.sqlite")
    actor = Actor("owner", {"read", "modify"})
    project = service.invoke("project.create", {"slug": "moonshiner", "model_name": "Dataset compose",
        "idempotency_key": "project"}, actor)
    payload = {"project_id": project["project_id"]}
    app = create_app(service=service, tokens={"test-only-owner": actor})
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"Authorization": "Bearer test-only-owner"}) as client:
            gateway = OperationGateway(client)
            result = await client.post("/v1/operations/component.list", json=payload)
            assert result.status_code == 200
            assert await gateway.invoke("component.list", payload) == result.json()
            entry = catalog_entries()[0]
            assert entry in result.json()["components"]
            request = {**payload, "component_id": entry["id"]}
            result = await client.post("/v1/operations/component.get", json=request)
            assert result.status_code == 200
            assert result.json() == await gateway.invoke("component.get", request) == entry
            assert "implementation_ref" not in entry
            assert entry["adapter"]["example_source"] == "examples/moonshiner/adapter.py"
    finally:
        service.store.close()
