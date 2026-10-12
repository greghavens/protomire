"""General inference reuses existing HTTP/MCP catalog and authoring operations."""
import httpx
import pytest

from modelbuilder.api import create_app
from modelbuilder.backend.components.transformers_inference import catalog_entries
from modelbuilder.mcp_server import OperationGateway
from modelbuilder.models import Actor
from modelbuilder.service import OperationService


@pytest.mark.asyncio
async def test_general_inference_http_mcp_catalog_parity(tmp_path):
    service = OperationService(tmp_path / "catalog.sqlite")
    actor = Actor("owner", {"read", "modify"})
    project = service.invoke("project.create", {"slug": "general-inference", "model_name": "Inference",
        "idempotency_key": "project"}, actor)
    payload = {"project_id": project["project_id"]}
    app = create_app(service=service, tokens={"test-only-owner": actor})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"Authorization": "Bearer test-only-owner"}) as client:
        gateway = OperationGateway(client)
        listed = await client.post("/v1/operations/component.list", json=payload)
        assert listed.status_code == 200
        assert await gateway.invoke("component.list", payload) == listed.json()
        for entry in catalog_entries():
            assert entry in listed.json()["components"]
            request = {**payload, "component_id": entry["id"]}
            response = await client.post("/v1/operations/component.get", json=request)
            assert response.status_code == 200
            assert response.json() == await gateway.invoke("component.get", request) == entry
            assert "implementation_ref" not in entry
            assert entry["adapter"]["example_source"] == "examples/transformers_inference/adapter.py"
    service.store.close()
