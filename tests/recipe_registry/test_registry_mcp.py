"""Independent official-SDK stdio client plus actual loopback HTTP/SQLite.

This proves generic MCP parity, not authenticated Claude/Codex vendor sessions.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

import modelbuilder


class PrivateService:
    def __init__(self, root):
        self.root = root
        self.source_path = str(Path(modelbuilder.__file__).resolve().parent.parent)
        self.publisher = secrets.token_urlsafe(24)
        self.reader = secrets.token_urlsafe(24)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.process = None
        self.log = None

    def start(self):
        env = {**os.environ, "PYTHONPATH": self.source_path, "MODELBUILDER_DB_PATH": str(self.root / "registry.sqlite3"),
               "MODELBUILDER_SOURCE_ROOT": str(self.root), "MODELBUILDER_BACKEND": "none", "MODELBUILDER_HOST": "127.0.0.1",
               "MODELBUILDER_PORT": str(self.port), "MODELBUILDER_TOKENS_JSON": json.dumps({
                   self.publisher: {"id": "registry-mcp-publisher", "scopes": ["read", "modify", "publish"]},
                   self.reader: {"id": "registry-mcp-reader", "scopes": ["read"]},
               })}
        env.pop("MODELBUILDER_TOKEN", None)
        self.log = (self.root / "service.log").open("a")
        self.process = subprocess.Popen([sys.executable, "-m", "modelbuilder.api"], env=env,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        with httpx.Client(timeout=0.3, trust_env=False) as client:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise AssertionError("Registry service failed to start; inspect disposable test service.log")
                try:
                    if client.get(self.url + "/health").status_code == 200:
                        return
                except httpx.RequestError:
                    pass
                time.sleep(0.05)
        raise AssertionError("Registry loopback service startup timed out")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log:
            self.log.close()

    def mcp(self, readonly=False):
        return StdioServerParameters(command=sys.executable, args=["-m", "modelbuilder.mcp_server"],
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": self.source_path,
                 "MODELBUILDER_API_URL": self.url, "MODELBUILDER_TOKEN": self.reader if readonly else self.publisher})


@pytest.mark.asyncio
async def test_generic_mcp_private_registry_matches_http_and_survives_restart(tmp_path):
    service = PrivateService(tmp_path)
    data = b"raise RuntimeError('bundled code must remain inert')\n"
    (tmp_path / "model.py").write_bytes(data)
    for arguments in (["init", "-q"], ["add", "model.py"], ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Pinned inert source"]):
        subprocess.run(["git", "-C", str(tmp_path), *arguments], check=True)
    commit = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    recipe = {
        "schema_version": "0", "recipe_id": "template", "project_id": "template", "revision": 1,
        "name": "Independent MCP fixture", "source": {"repository": "https://example.invalid/source.git",
            "commit": commit, "files": [{"path": "model.py", "sha256": hashlib.sha256(data).hexdigest()}]},
        "nodes": [], "edges": [], "resources": [], "environment": {"python": "3.12", "requirements": []},
        "evaluation": {"threshold": 1.0, "exact_integer": 9007199254740993}, "outputs": [], "external_dependencies": [],
        "licenses": [{"scope": "source", "license": "MIT", "redistribution_allowed": True}],
        "permissions": {"network": False, "execute_on_import": False},
    }

    async def call(session, name, payload):
        response = await session.call_tool(name, payload)
        assert not response.isError, response.model_dump()
        assert response.structuredContent == json.loads(response.content[0].text)
        return response.structuredContent

    try:
        service.start()
        async with stdio_client(service.mcp()) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                catalog = await session.list_tools()
                assert {"recipe_publish_preview", "recipe_publish", "recipe_discover", "recipe_fork"} <= {tool.name for tool in catalog.tools}
                source = await call(session, "project_create", {"slug": "mcp-registry-source", "model_name": "Source",
                    "recipe": recipe, "idempotency_key": "source"})
                await call(session, "policy_update", {"project_id": source["project_id"], "expected_revision": 1,
                    "changes": {"publication_enabled": True}, "idempotency_key": "enable"})
                fields = {"registry_id": "local-private", "project_id": source["project_id"], "recipe_id": source["recipe_id"],
                    "expected_revision": 1, "package_name": "mcp-classifier", "package_version": "1.0.0", "visibility": "private",
                    "metadata": {"title": "MCP classifier", "description": "Independent protocol acceptance", "tags": ["mcp"]}}
                preview = await call(session, "recipe_publish_preview", fields)
                request = {**fields, "review_digest": preview["review_digest"], "confirm_publish": True, "idempotency_key": "publish"}
                publication = await call(session, "recipe_publish", request)
                async with httpx.AsyncClient(base_url=service.url, headers={"Authorization": "Bearer " + service.publisher}, trust_env=False) as client:
                    result = await client.post("/v1/operations/recipe.publish", json=request)
                    assert result.status_code == 200
                    assert result.json() == publication
                identity = {"registry_id": "local-private", "publication_id": publication["publication_id"]}
                inspected = await call(session, "recipe_registry_get", identity)
                assert '"threshold":1.0' in inspected["bundle_json"]
                assert '"exact_integer":9007199254740993' in inspected["bundle_json"]
                assert inspected["import_report"]["execution_allowed"] is False
                fields = {**identity, "target": {"mode": "new_project", "slug": "mcp-registry-fork", "model_name": "Fork"}}
                reviewed = await call(session, "recipe_fork_preview", fields)
                imported = await call(session, "recipe_fork", {**fields, "review_digest": reviewed["review_digest"],
                                      "confirm_import": True, "idempotency_key": "fork"})
                assert imported["project"]["recipe_id"] == imported["recipe_id"]
                assert len((await call(session, "recipe_list", {"project_id": imported["project_id"]}))["recipes"]) == 1
                # Same MCP process reconnects to restarted, durable HTTP service.
                service.stop()
                service.start()
                assert (await call(session, "recipe_discover", {"registry_id": "local-private", "query": "MCP"}))["total"] == 1
                assert await call(session, "recipe_publish", request) == publication
                retained = await call(session, "recipe_registry_imported_bundle", {"project_id": imported["project_id"],
                    "recipe_id": imported["recipe_id"], "revision": imported["revision"]})
                assert retained["bundle_json"] == inspected["bundle_json"]
                async with stdio_client(service.mcp(readonly=True)) as reader_streams:
                    async with ClientSession(*reader_streams) as reader:
                        await reader.initialize()
                        denied = await reader.call_tool("recipe_publish", {**request, "idempotency_key": "guessed-write"})
                        assert denied.isError
                        assert denied.structuredContent["http_status"] == 403
                        assert (await call(reader, "recipe_discover", {"registry_id": "local-private"}))["total"] == 1
    finally:
        service.stop()
