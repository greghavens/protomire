#!/usr/bin/env python3
"""Real external MCP/stdio acceptance against a separate HTTP service process.

Run: .venv/bin/python scripts/mcp_smoke.py --output evidence/mcp
The client imports the official MCP SDK, never modelbuilder's Python handlers.
Only disposable randomly generated test credentials are used, and none are
included in the trace. This is generic MCP acceptance, NOT Claude/Codex proof.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


class ServiceProcess:
    def __init__(self, root: Path):
        self.root = root
        self.owner = secrets.token_urlsafe(32)
        self.reader = secrets.token_urlsafe(32)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.process = None
        self.log_file = None
        self.generation = 0

    def start(self):
        self.generation += 1
        env = os.environ.copy()
        env.update({
            "PYTHONPATH": str(ROOT / "src"),
            "MODELBUILDER_DB_PATH": str(self.root / "service.sqlite3"),
            "MODELBUILDER_TOKENS_JSON": json.dumps({
                self.owner: {"id": "mcp-acceptance-owner", "scopes": ["read", "modify", "execute"]},
                self.reader: {"id": "mcp-acceptance-reader", "scopes": ["read"]},
            }),
            "MODELBUILDER_HOST": "127.0.0.1", "MODELBUILDER_PORT": str(self.port),
        })
        env.pop("MODELBUILDER_TOKEN", None)
        self.log_file = (self.root / f"service-{self.generation}.log").open("w")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "modelbuilder.api"],
            cwd=ROOT, env=env, stdout=self.log_file, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 30
        with httpx.Client(trust_env=False, timeout=0.3) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"HTTP service exited at startup; see service-{self.generation}.log")
                try:
                    response = client.get(self.base_url + "/health")
                    if response.status_code == 200:
                        return
                except httpx.RequestError:
                    pass
                time.sleep(0.05)
        raise RuntimeError("HTTP service did not become healthy within 30 seconds")

    def stop(self, *, crash=False):
        if self.process is not None and self.process.poll() is None:
            self.process.kill() if crash else self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self.log_file is not None:
            self.log_file.close()

    def restart(self):
        self.stop(crash=True)
        self.start()

    @asynccontextmanager
    async def mcp(self, *, readonly=False):
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "modelbuilder.mcp_server"], cwd=str(ROOT),
            env={
                "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src"),
                "PYTHONUNBUFFERED": "1", "MODELBUILDER_API_URL": self.base_url,
                "MODELBUILDER_TOKEN": self.reader if readonly else self.owner,
            },
        )
        log = self.root / ("mcp-reader.stderr.log" if readonly else "mcp-owner.stderr.log")
        with log.open("a") as stderr:
            async with stdio_client(parameters, errlog=stderr) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    yield session


def sample_value(schema: dict, key: str, project_id: str, recipe_id: str):
    if key == "project_id":
        return project_id
    if key == "recipe_id":
        return recipe_id
    if key == "idempotency_key":
        return "denied-" + secrets.token_hex(8)
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type", "object")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "string")
    if kind == "object":
        return {}
    if kind == "array":
        return ["00000000-0000-0000-0000-000000000001"] * schema.get("minItems", 1)
    if kind == "integer":
        return schema.get("minimum", 1)
    if kind == "number":
        return 0
    if kind == "boolean":
        return False
    return "mcp-denial-probe"


async def acceptance(service: ServiceProcess, evidence: dict):
    transcript = evidence["transcript"]

    def record(transport, request, response):
        transcript.append({"transport": transport, "request": request, "response": response})

    async def call(session, name, arguments, *, error_status=None):
        result = await session.call_tool(name, arguments)
        decoded = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        record("mcp-stdio", {"method": "tools/call", "params": {"name": name, "arguments": arguments}}, decoded)
        assert result.isError is (error_status is not None), (name, decoded)
        body = result.structuredContent
        assert isinstance(body, dict), (name, decoded)
        assert json.loads(result.content[0].text) == body
        if error_status is not None:
            assert body["http_status"] == error_status, (name, body)
        return body

    async with httpx.AsyncClient(
        base_url=service.base_url, headers={"Authorization": f"Bearer {service.owner}"}, trust_env=False,
    ) as http:
        async def operation(name, payload, expected_status=200):
            response = await http.post(f"/v1/operations/{name}", json=payload)
            record("http", {"method": "POST", "path": f"/v1/operations/{name}", "json": payload},
                   {"status": response.status_code, "body": response.json()})
            assert response.status_code == expected_status, response.text
            return response.json()

        async with httpx.AsyncClient(base_url=service.base_url, trust_env=False) as anonymous:
            for headers in ({}, {"Authorization": "Bearer invalid-test-credential"}):
                response = await anonymous.get("/v1/catalog", headers=headers)
                # The trace intentionally omits request headers, including test tokens.
                record("http", {"method": "GET", "path": "/v1/catalog", "identity": "invalid" if headers else "missing"},
                       {"status": response.status_code, "body": response.json()})
                assert response.status_code == 401

        response = await http.get("/v1/catalog")
        assert response.status_code == 200, response.text
        catalog = response.json()
        record("http", {"method": "GET", "path": "/v1/catalog"}, {"status": 200, "body": catalog})
        async with service.mcp() as session:
            initialization = await session.initialize()
            record("mcp-stdio", {"method": "initialize"}, initialization.model_dump(mode="json", by_alias=True))
            listed = await session.list_tools()
            record("mcp-stdio", {"method": "tools/list"}, listed.model_dump(mode="json", by_alias=True))
            discovered = {tool.name: tool for tool in listed.tools}
            expected = {entry["name"].replace(".", "_"): entry for entry in catalog["operations"]}
            assert set(discovered) == set(expected)
            for name, tool in discovered.items():
                assert tool.inputSchema == expected[name]["input_schema"]
            resources = await session.list_resources()
            record("mcp-stdio", {"method": "resources/list"}, resources.model_dump(mode="json", by_alias=True))
            resource = await session.read_resource("modelbuilder://catalog")
            record("mcp-stdio", {"method": "resources/read", "params": {"uri": "modelbuilder://catalog"}},
                   resource.model_dump(mode="json", by_alias=True))
            assert json.loads(resource.contents[0].text) == catalog

            project = await call(session, "project_create", {
                "slug": "headless-mcp-acceptance", "model_name": "Protocol fixture model",
                "idempotency_key": "mcp-project-create",
            })
            project_id = project["project_id"]
            assert await operation("project.get", {"project_id": project_id}) == project
            created = await call(session, "recipe_create", {
                "project_id": project_id, "name": "MCP-created recipe", "idempotency_key": "mcp-recipe-create",
            })
            duplicate = await call(session, "recipe_create", {
                "project_id": project_id, "name": "MCP-created recipe", "idempotency_key": "mcp-recipe-create",
            })
            assert duplicate == created
            recipe_id = created["recipe_id"]
            identity = {"project_id": project_id, "recipe_id": recipe_id}
            assert await operation("recipe.get", identity) == created
            assert await call(session, "recipe_get", identity) == created
            validation = await call(session, "recipe_validate", identity)
            assert validation.get("valid") is True, validation

            changed = deepcopy(created)
            changed["name"] = "Changed by external MCP client"
            updated = await call(session, "recipe_update", {
                **identity, "recipe": changed, "expected_revision": created["revision"],
                "idempotency_key": "mcp-recipe-update",
            })
            assert updated["recipe_id"] == recipe_id
            assert updated["revision"] == created["revision"] + 1
            assert updated["digest"] != created["digest"]
            assert await operation("recipe.get", identity) == updated
            conflict = await call(session, "recipe_update", {
                **identity, "recipe": changed, "expected_revision": created["revision"],
                "idempotency_key": "mcp-stale-write",
            }, error_status=409)
            assert conflict["error"]["details"]
            assert await operation("recipe.get", identity) == updated

            source_edit = deepcopy(updated)
            source_edit["name"] = "Changed through the IDE HTTP contract"
            api_updated = await operation("recipe.update", {
                **identity, "recipe": source_edit, "expected_revision": updated["revision"],
                "idempotency_key": "api-source-edit",
            })
            assert await call(session, "recipe_get", identity) == api_updated

            # Two real transports race the same precondition. Exactly one must
            # succeed, and the loser must receive the service's 409 with diff.
            race_mcp = {**identity, "recipe": {**api_updated, "name": "Concurrent MCP edit"},
                        "expected_revision": api_updated["revision"], "idempotency_key": "race-mcp"}
            race_api = {**identity, "recipe": {**api_updated, "name": "Concurrent HTTP edit"},
                        "expected_revision": api_updated["revision"], "idempotency_key": "race-http"}
            api_race_result, mcp_race_result = await asyncio.gather(
                http.post("/v1/operations/recipe.update", json=race_api),
                session.call_tool("recipe_update", race_mcp),
            )
            api_race_body = api_race_result.json()
            mcp_race_body = mcp_race_result.structuredContent
            record("http", {"method": "POST", "path": "/v1/operations/recipe.update", "json": race_api},
                   {"status": api_race_result.status_code, "body": api_race_body})
            record("mcp-stdio", {"method": "tools/call", "params": {"name": "recipe_update", "arguments": race_mcp}},
                   mcp_race_result.model_dump(mode="json", by_alias=True, exclude_none=True))
            mcp_race_status = mcp_race_body.get("http_status", 200)
            assert sorted([api_race_result.status_code, mcp_race_status]) == [200, 409]
            if api_race_result.status_code == 200:
                winner, loser = api_race_body, mcp_race_body
            else:
                winner, loser = mcp_race_body, api_race_body
            assert winner["revision"] == api_updated["revision"] + 1
            assert loser["error"]["details"]["current_revision"] == winner["revision"]
            assert loser["error"]["details"]["diff"]
            api_updated = winner
            assert await call(session, "recipe_get", identity) == api_updated
            assert await operation("recipe.get", identity) == api_updated

            unknown_schema = {**api_updated, "schema_version": "unsupported-version"}
            await call(session, "recipe_validate", {"project_id": project_id, "recipe": unknown_schema}, error_status=400)
            await call(session, "recipe_get", {**identity, "unexpected_argument": True}, error_status=400)
            clone = await call(session, "recipe_clone", {
                **identity, "name": "External MCP clone", "expected_revision": api_updated["revision"],
                "idempotency_key": "mcp-clone",
            })
            assert clone["recipe_id"] != recipe_id
            assert clone["project_id"] == project_id
            bundle = await call(session, "recipe_export", identity)
            tampered = deepcopy(bundle)
            tampered["recipe"]["name"] = "Tampered without recomputing bundle digest"
            await call(session, "recipe_import", {
                "project_id": project_id, "bundle": tampered, "idempotency_key": "mcp-tampered-import",
            }, error_status=400)
            imported = await call(session, "recipe_import", {
                "project_id": project_id, "bundle": bundle,
                "idempotency_key": "mcp-import", "name": "External MCP imported recipe",
            })
            assert imported["recipe_id"] not in {recipe_id, clone["recipe_id"]}
            assert imported["import_report"]["execution_allowed"] is False
            imported = {key: value for key, value in imported.items() if key != "import_report"}
            assert await call(session, "recipe_get", {"project_id": project_id, "recipe_id": imported["recipe_id"]}) == imported
            assert await call(session, "recipe_validate", {"project_id": project_id, "recipe_id": imported["recipe_id"]})

            async with service.mcp(readonly=True) as reader:
                await reader.initialize()
                assert await call(reader, "recipe_get", identity) == api_updated
                denied_operations = []
                for entry in catalog["operations"]:
                    if entry["scope"] == "read":
                        continue
                    schema = entry["input_schema"]
                    payload = {key: sample_value(schema["properties"][key], key, project_id, recipe_id)
                               for key in schema.get("required", [])}
                    if "project_id" in schema["properties"]:
                        payload["project_id"] = project_id
                    await call(reader, entry["name"].replace(".", "_"), payload, error_status=403)
                    denied_operations.append(entry["name"])
                guessed = await reader.call_tool("recipe_publish", {**identity})
                record("mcp-stdio", {"method": "tools/call", "params": {"name": "recipe_publish", "arguments": identity}},
                       guessed.model_dump(mode="json", by_alias=True))
                assert guessed.isError and guessed.structuredContent["error"]["code"] == "unknown_tool"
                assert await call(reader, "recipe_get", identity) == api_updated

            # SIGKILL the HTTP service; preserve its on-disk database and port.
            # The independent MCP process remains alive across this restart.
            service.restart()
            after_restart = await call(session, "recipe_get", identity)
            assert after_restart == api_updated
            assert await call(session, "project_get", {"project_id": project_id}) == project
            assert await call(session, "recipe_get", {"project_id": project_id, "recipe_id": clone["recipe_id"]}) == clone
            assert await call(session, "recipe_get", {"project_id": project_id, "recipe_id": imported["recipe_id"]}) == imported
            history = await call(session, "project_history", {"project_id": project_id})
            denied_audits = {event["operation"] for event in history["events"]
                            if event["outcome"] == "denied" and event["actor_id"] == "mcp-acceptance-reader"}
            # project.create has no project identity yet and is audited globally.
            assert set(denied_operations) - {"project.create"} <= denied_audits
            evidence["summary"] = {
                "status": "PASS", "tool_count": len(discovered),
                "project_id": project_id, "recipe_id": recipe_id, "clone_recipe_id": clone["recipe_id"],
                "imported_recipe_id": imported["recipe_id"], "revision": api_updated["revision"],
                "digest": api_updated["digest"], "read_only_denied_operations": denied_operations,
                "http_status_conflict": conflict["http_status"], "restart_method": "SIGKILL then same SQLite store",
                "same_live_mcp_process_across_service_restart": True,
                "api_mcp_full_resource_equality": True, "idempotency_verified": True,
                "concurrent_http_mcp_conflict": True, "tampered_import_rejected": True,
                "unknown_schema_rejected": True, "unknown_arguments_rejected_by_shared_service": True,
                "catalog_schema_equality": True, "denied_writes_audited": True, "history_returned": bool(history),
                "headless": "Harness starts only the service and MCP subprocess; no IDE is launched or required.",
                "gui_parity": "HTTP contract equality only. Real GUI actions require separate GUI evidence.",
                "claude_codex": "UNVERIFIED by this generic-client test; separate authenticated client acceptance required.",
            }


async def run_smoke(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0], "mcp_sdk": version("mcp"),
        "command": ".venv/bin/python scripts/mcp_smoke.py --output evidence/mcp",
        "client_boundary": "Official MCP ClientSession in this process communicates via stdio with a separate server process.",
        "credentials": "Fresh test-only credentials are generated in memory and omitted from evidence.",
        "transcript": [],
    }
    with tempfile.TemporaryDirectory(prefix="modelbuilder-mcp-") as folder:
        service = ServiceProcess(Path(folder))
        try:
            service.start()
            await acceptance(service, evidence)
        except BaseException as error:
            evidence["summary"] = {"status": "FAIL", "error_type": type(error).__name__, "message": str(error)}
            raise
        finally:
            service.stop()
            evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
            serialized = json.dumps(evidence, indent=2, sort_keys=True)
            for credential in (service.owner, service.reader):
                serialized = serialized.replace(credential, "[REDACTED]")
            (output / "external-stdio-transcript.json").write_text(serialized + "\n")
            (output / "summary.json").write_text(json.dumps(evidence.get("summary", {}), indent=2, sort_keys=True) + "\n")
            for log in service.root.glob("*.log"):
                content = log.read_text()
                for credential in (service.owner, service.reader):
                    content = content.replace(credential, "[REDACTED]")
                (output / log.name).write_text(content)
    return evidence["summary"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evidence" / "mcp")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_smoke(args.output)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
