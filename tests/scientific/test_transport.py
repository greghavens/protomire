"""Independent official MCP client and HTTP share scientific contracts/results."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import httpx
import pytest
from .test_policy import policy


@pytest.mark.asyncio
async def test_scientific_http_mcp_parity_restart_and_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELBUILDER_BACKEND", "none")
    spec = spec_from_file_location("scientific_transport_acceptance", Path(__file__).resolve().parents[2] / "scripts/mcp_smoke.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / "state"
    state.mkdir()
    process = module.ServiceProcess(state)
    try:
        process.start()
        async with httpx.AsyncClient(base_url=process.base_url, headers={"Authorization": "Bearer " + process.owner}, trust_env=False) as http:
            async def call(name, body):
                response = await http.post("/v1/operations/" + name, json=body)
                assert response.status_code == 200, response.text
                return response.json()
            project = await call("project.create", {"slug": "scientific-http", "model_name": "Scientific HTTP", "idempotency_key": "project"})
            pid = {"project_id": project["project_id"]}
            async with process.mcp() as session:
                await session.initialize()
                methods = await session.call_tool("scientific_methods", pid)
                assert not methods.isError
                assert methods.structuredContent == await call("scientific.methods", pid)
                registration = {**pid, "policy": policy("paired_normal_known_sd"), "idempotency_key": "policy"}
                created = await session.call_tool("scientific_policy_register", registration)
                assert not created.isError, created.model_dump()
                assert created.structuredContent == await call("scientific.policy.register", registration)
                params = registration["policy"]["rules"][0]["method"]["parameters"]
                sample = {**pid, "method": "normal-known-sd-one-sided/v1", "parameters": params, "alpha": .025, "available_pairs": 10}
                planned = await session.call_tool("scientific_sample_plan", sample)
                assert not planned.isError
                assert planned.structuredContent == await call("scientific.sample.plan", sample)
                assert planned.structuredContent["adequate"] is False
                missing = dict(sample)
                missing["parameters"] = {k:v for k,v in params.items() if k != "detectable_effect"}
                rejected = await session.call_tool("scientific_sample_plan", missing)
                assert rejected.isError and rejected.structuredContent["error"]["code"] == "validation_error"
                digest = created.structuredContent["policy_digest"]
                active = await call("scientific.policy.activate", {**pid, "policy_digest": digest, "expected_revision": project["revision"], "idempotency_key": "activate"})
                assert active["scientific_policy_digest"] == digest
                smoke = {**pid, "recipe_id": project["recipe_id"], "expected_revision": 1,
                    "limits": [{"node_id": "no-declared-budget", "limit_id": "unknown", "unit": "steps", "maximum": 1}]}
                planned_smoke = await session.call_tool("run_smoke_plan", smoke)
                assert not planned_smoke.isError
                assert planned_smoke.structuredContent == await call("run.smoke.plan", smoke)
                assert planned_smoke.structuredContent["supported"] is False
                process.restart()
                read = await session.call_tool("scientific_policy_get", {**pid, "policy_digest": digest})
                assert not read.isError and read.structuredContent["policy"] == registration["policy"]
                assert (await call("project.get", pid))["scientific_policy_digest"] == digest
    finally:
        process.stop()
