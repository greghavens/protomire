"""An independent official MCP client pins the same recipe through real HTTP."""
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import subprocess

import httpx
import pytest


@pytest.mark.asyncio
async def test_reviewed_source_pin_http_mcp_parity_and_restart(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    code = source / "harness.py"
    code.write_text("raise RuntimeError('source pin must be inert')\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Fixture"]):
        subprocess.run(["git", "-C", str(source), *args], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    config = tmp_path / "operator.json"
    config.write_text(json.dumps({"source_roots": {"approved": {"path": str(source), "trusted": False}}}))
    monkeypatch.setenv("MODELBUILDER_OPERATOR_CONFIG", str(config))
    monkeypatch.setenv("MODELBUILDER_BACKEND", "none")
    spec = spec_from_file_location("source_pin_acceptance", Path(__file__).resolve().parents[2] / "scripts/mcp_smoke.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / "state"
    state.mkdir()
    service = module.ServiceProcess(state)
    try:
        service.start()
        async with httpx.AsyncClient(base_url=service.base_url, headers={"Authorization": "Bearer " + service.owner}, trust_env=False) as http:
            async def call(name, body):
                result = await http.post("/v1/operations/" + name, json=body)
                assert result.status_code == 200, result.text
                return result.json()
            listing = await call("project.source.list", {})
            identity = listing["sources"][0]
            project = await call("project.create", {"slug": "pin-http", "model_name": "Pin HTTP",
                "source_alias": "approved", "source_repository_id": identity["repository_id"], "trusted": True,
                "idempotency_key": "project"})
            fields = {"project_id": project["project_id"], "recipe_id": project["recipe_id"], "expected_revision": 1,
                "expected_project_revision": 1, "source_repository_id": identity["repository_id"],
                "commit": commit, "paths": ["harness.py"]}
            preview = await call("recipe.source.pin.preview", fields)
            payload = {**fields, "review_digest": preview["review_digest"], "confirm_pin": True, "idempotency_key": "pin"}
            async with service.mcp() as session:
                await session.initialize()
                result = await session.call_tool("recipe_source_pin", payload)
                assert not result.isError, result.model_dump()
                pinned = result.structuredContent
                assert pinned == await call("recipe.source.pin", payload)
                service.restart()
                repeated = await session.call_tool("recipe_source_pin", payload)
                assert not repeated.isError and repeated.structuredContent == pinned
                assert pinned["recipe"]["revision"] == 2 and pinned["execution_allowed"] is False
                assert (await call("run.list", {"project_id": project["project_id"]}))["runs"] == []
                stale = await session.call_tool("recipe_source_pin", {**payload, "idempotency_key": "stale"})
                assert stale.isError and stale.structuredContent["http_status"] == 409
    finally:
        service.stop()
