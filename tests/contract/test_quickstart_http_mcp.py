"""Real HTTP plus independent official SDK MCP, no vendor calls or execution."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import httpx
import pytest


@pytest.mark.asyncio
async def test_project_intent_one_recipe_http_mcp_parity_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELBUILDER_BACKEND", "none")
    spec = spec_from_file_location("quickstart_acceptance", Path(__file__).resolve().parents[2] / "scripts/mcp_smoke.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    service = module.ServiceProcess(tmp_path)
    try:
        service.start()
        async with httpx.AsyncClient(base_url=service.base_url, headers={"Authorization": "Bearer " + service.owner}, trust_env=False) as http:
            async def call(name, payload):
                result = await http.post("/v1/operations/" + name, json=payload)
                assert result.status_code == 200, result.text
                return result.json()
            payload = {"model_name": "Scratch parity", "slug": "scratch-parity", "intent": {"schema_version": "1", "goal": "Build from scratch", "starting_point": "scratch", "assistant": "codex"}, "idempotency_key": "create"}
            async with service.mcp() as session:
                await session.initialize()
                result = await session.call_tool("project_create", payload)
                assert not result.isError
                project = result.structuredContent
                assert project == await call("project.create", payload)
                service.restart()
                assert (await session.call_tool("project_create", payload)).structuredContent == project
                opened = await call("project.open", {"project_id": project["project_id"]})
                assert len(opened["recipes"]) == 1 and not opened["runs"]
                assert opened["recipe"]["recipe_id"] == project["recipe_id"] == project["active_recipe_id"]
                update = {"project_id": project["project_id"], "expected_revision": 1, "changes": {"intent": {**payload["intent"], "starting_point": "research"}}, "idempotency_key": "update"}
                updated = await call("project.update", update)
                assert (await session.call_tool("project_update", update)).structuredContent == updated
                stale = await session.call_tool("project_update", {**update, "idempotency_key": "stale"})
                assert stale.isError and stale.structuredContent["http_status"] == 409
    finally:
        service.stop()
