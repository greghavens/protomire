"""Real Linux CPU inventory via HTTP and an independent official MCP stdio client.

No GPU, Windows/macOS, or accelerator execution acceptance is inferred here.
"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import httpx
import pytest


@pytest.mark.asyncio
async def test_execution_targets_hardware_http_mcp_readonly_parity(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELBUILDER_BACKEND", "zenml")
    monkeypatch.setenv("MODELBUILDER_BACKEND_PYTHON", str(tmp_path / "missing-backend"))
    monkeypatch.delenv("MODELBUILDER_OPERATOR_CONFIG", raising=False)
    spec = spec_from_file_location("compute_acceptance", Path(__file__).resolve().parents[2] / "scripts/mcp_smoke.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    service = module.ServiceProcess(tmp_path)
    try:
        service.start()
        async with httpx.AsyncClient(base_url=service.base_url, headers={"Authorization": "Bearer " + service.owner}, trust_env=False) as http:
            created = await http.post("/v1/operations/project.create", json={"slug": "compute-inventory", "model_name": "One model", "idempotency_key": "project"})
            assert created.status_code == 200, created.text
            project = created.json()
            payload = {"project_id": project["project_id"]}
            first = (await http.post("/v1/operations/execution.targets", json=payload)).json()
            async with service.mcp(readonly=True) as session:
                await session.initialize()
                result = await session.call_tool("execution_targets", payload)
                assert not result.isError, result.model_dump()
                mcp = result.structuredContent
            assert mcp["hardware"]["freshness"]["cached"]
            for response in (first, mcp):
                assert response["hardware"]["scope"] == "operation_service_host"
                assert response["hardware"]["cpu"]["logical_cores"] > 0
                assert response["hardware"]["memory"]["total_bytes"] > 0
                assert [row["id"] for row in response["targets"]] == ["local-cpu"]
                assert response["targets"][0]["available"] is False
                assert not response["hardware"]["resource_control"]["gpu_execution"]
            first["hardware"].pop("freshness")
            mcp["hardware"].pop("freshness")
            assert first == mcp
            # Discovery does not create runs or mutate the one model/recipe.
            assert (await http.post("/v1/operations/run.list", json=payload)).json() == {"runs": []}
            fetched = (await http.post("/v1/operations/project.get", json=payload)).json()
            assert fetched["recipe_id"] == project["recipe_id"] and fetched["revision"] == project["revision"]
    finally:
        service.stop()
