"""The same inert reusable adapter contract is available over HTTP and MCP."""
import httpx
import pytest

from modelbuilder.api import create_app
from modelbuilder.mcp_server import OperationGateway, catalog_tools
from modelbuilder.models import Actor
from modelbuilder.service import OperationService


@pytest.mark.asyncio
async def test_local_catalog_http_mcp_parity_and_project_scope(tmp_path):
    service = OperationService(tmp_path / "catalog.sqlite")
    actor = Actor("owner", {"read", "modify"})
    token = "test-only-owner"
    project = service.invoke("project.create", {"slug": "adapters", "model_name": "Local adapters", "idempotency_key": "p"}, actor)
    payload = {"project_id": project["project_id"]}
    app = create_app(service=service, tokens={token: actor})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + token}) as client:
        response = await client.post("/v1/operations/component.list", json=payload)
        assert response.status_code == 200, response.text
        result = response.json()
        gateway = OperationGateway(client)
        assert await gateway.invoke("component.list", payload) == result
        entries = [entry for entry in result["components"] if entry["id"].startswith("modelbuilder.local.")]
        assert len(entries) == 3
        assert "component_list" in {tool.name for tool in catalog_tools(await gateway.catalog())[0]}
        for entry in entries:
            request = {**payload, "component_id": entry["id"]}
            fetched = await client.post("/v1/operations/component.get", json=request)
            assert fetched.status_code == 200, fetched.text
            assert fetched.json() == await gateway.invoke("component.get", request) == entry
            assert "implementation_ref" not in entry
            assert "source_binding" in entry["adapter"]
    service.store.close()
