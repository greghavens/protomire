"""Official SDK client -> independent stdio MCP -> separate HTTP service."""
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path

import pytest

from modelbuilder.models import Actor
from modelbuilder.service import OperationService

from test_registry import make_manifest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
async def test_independent_native_mcp_dataset_lifecycle_and_restart(tmp_path, monkeypatch):
    spec = spec_from_file_location("dataset_mcp_harness", ROOT / "scripts/mcp_smoke.py")
    harness = module_from_spec(spec)
    spec.loader.exec_module(harness)
    owner = Actor("setup-owner", {"read", "modify"})
    root = tmp_path / "mcp"
    root.mkdir()
    local = OperationService(root / "service.sqlite3")
    project = local.invoke("project.create", {"slug": "dataset-mcp", "model_name": "Dataset via MCP", "idempotency_key": "project"}, owner)
    pid = project["project_id"]
    local.store.close()
    data = tmp_path / "data"
    data.mkdir()
    manifest = make_manifest(data)
    config = tmp_path / "operator.json"
    config.write_text(json.dumps({"dataset_roots": {pid: {"data": str(data)}}}))
    monkeypatch.setenv("MODELBUILDER_OPERATOR_CONFIG", str(config))
    monkeypatch.setenv("MODELBUILDER_BACKEND", "none")
    process = harness.ServiceProcess(root)
    process.start()
    try:
        async with process.mcp() as session:
            await session.initialize()
            listed = await session.list_tools()
            assert {"dataset_register", "dataset_get", "dataset_version", "dataset_list", "dataset_validate", "dataset_preview"}.issubset({tool.name for tool in listed.tools})
            async def call(name, payload):
                result = await session.call_tool(name, payload)
                assert not result.isError, result.structuredContent
                return result.structuredContent
            request = {"project_id": pid, "manifest": manifest, "idempotency_key": "register"}
            one = await call("dataset_register", request)
            assert await call("dataset_register", request) == one
            assert await call("dataset_get", {"project_id": pid, "dataset_id": one["dataset_id"]}) == one
            changed = deepcopy(manifest)
            changed["description"] = "Reviewed through native MCP"
            two = await call("dataset_version", {"project_id": pid, "dataset_id": one["dataset_id"],
                "manifest": changed, "expected_revision": 1, "idempotency_key": "version"})
            assert two["revision"] == 2 and two["parent_version_id"] == one["version_id"]
            page = await call("dataset_preview", {"project_id": pid, "dataset_id": one["dataset_id"], "revision": 2,
                "split_id": "train", "purpose": "training", "limit": 1})
            assert page["sample_ids"] == ["train-1"] and page["has_more"]
            stale = await session.call_tool("dataset_version", {"project_id": pid, "dataset_id": one["dataset_id"],
                "manifest": manifest, "expected_revision": 1, "idempotency_key": "stale"})
            assert stale.isError and stale.structuredContent["error"]["code"] == "revision_conflict"
            process.restart()
            assert await call("dataset_get", {"project_id": pid, "dataset_id": one["dataset_id"], "revision": 1}) == one
            assert (await call("dataset_list", {"project_id": pid}))["datasets"][0]["revision"] == 2
            assert (await call("dataset_validate", {"project_id": pid, "dataset_id": one["dataset_id"], "revision": 2}))["valid"]
            (data / "weather.jsonl").write_text("changed data")
            invalid = await call("dataset_validate", {"project_id": pid, "dataset_id": one["dataset_id"], "revision": 2})
            assert not invalid["valid"] and invalid["diagnostics"][0]["code"] == "dataset_checksum_mismatch"
        async with process.mcp(readonly=True) as session:
            await session.initialize()
            denied = await session.call_tool("dataset_register", {"project_id": pid, "manifest": manifest, "idempotency_key": "reader"})
            assert denied.isError and denied.structuredContent["error"]["code"] == "forbidden"
    finally:
        process.stop()
