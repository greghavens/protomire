"""Standalone MCP transport over the authoritative application operation API.

No store, recipe, permission, or backend implementation is imported here. Run
``modelbuilder-mcp`` with MODELBUILDER_API_URL and MODELBUILDER_TOKEN in its
process environment. Standard output is reserved exclusively for MCP frames.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.server.lowlevel.helper_types import ReadResourceContents


class ConfigurationError(ValueError):
    """Safe-to-display configuration error (never contains a credential)."""


@dataclass(frozen=True)
class Configuration:
    api_url: str = "http://127.0.0.1:8765"
    token: str = field(default="", repr=False)
    timeout: float = 30.0

    def __post_init__(self) -> None:
        try:
            url = urlsplit(self.api_url)
            port = url.port
        except ValueError:
            raise ConfigurationError("MODELBUILDER_API_URL must be a valid HTTP(S) base URL") from None
        del port
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ConfigurationError("MODELBUILDER_API_URL must not contain credentials, query, or fragment")
        if url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ConfigurationError("Non-loopback operation services require HTTPS")
        if not self.token.strip():
            raise ConfigurationError("MODELBUILDER_TOKEN must contain an operation-service credential")
        if not math.isfinite(self.timeout) or self.timeout <= 0 or self.timeout > 300:
            raise ConfigurationError("MODELBUILDER_TIMEOUT must be greater than zero and at most 300 seconds")

    @classmethod
    def from_environment(cls) -> Configuration:
        try:
            timeout = float(os.environ.get("MODELBUILDER_TIMEOUT", "30"))
        except ValueError:
            raise ConfigurationError("MODELBUILDER_TIMEOUT must be a number of seconds") from None
        return cls(
            api_url=os.environ.get("MODELBUILDER_API_URL", "http://127.0.0.1:8765").rstrip("/"),
            token=os.environ.get("MODELBUILDER_TOKEN", ""),
            timeout=timeout,
        )


class OperationError(Exception):
    """Transported service error, with the original HTTP status and JSON body."""

    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__("Operation service rejected the request")
        self.status = status
        self.body = body


def error_body(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": {}}}


class OperationGateway:
    """Thin, retry-free HTTP client; all business decisions belong to the service."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def request(self, method: str, path: str, payload: dict | None = None) -> dict[str, Any]:
        try:
            response = await self.client.request(method, path, json=payload)
        except httpx.RequestError:
            # Do not serialize exceptions: they can contain request headers/URLs.
            raise OperationError(503, error_body("service_unavailable", "Operation service is unreachable")) from None
        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError):
            raise OperationError(502, error_body("invalid_service_response", "Operation service returned invalid JSON")) from None
        if not isinstance(body, dict):
            raise OperationError(502, error_body("invalid_service_response", "Operation service returned a non-object"))
        if not response.is_success:
            # Only documented service errors are passed through. A proxy error
            # page or arbitrary upstream diagnostic must not expose credentials.
            if not isinstance(body.get("error"), dict):
                body = error_body("service_error", "Operation service rejected the request")
            raise OperationError(response.status_code, body)
        return body

    async def catalog(self) -> dict[str, Any]:
        return await self.request("GET", "/v1/catalog")

    async def invoke(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", f"/v1/operations/{operation}", arguments)


def tool_result(body: dict[str, Any], *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(body, sort_keys=True, ensure_ascii=False))],
        structuredContent=body,
        isError=is_error,
    )


def catalog_tools(catalog: dict[str, Any]) -> tuple[list[types.Tool], dict[str, str]]:
    """Generate names and schemas; never reproduce application input validation."""
    entries = catalog.get("operations")
    if not isinstance(entries, list):
        raise ConfigurationError("Operation service catalog must contain an operations array")
    tools: list[types.Tool] = []
    aliases: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigurationError("Operation service catalog entry must be an object")
        name = entry.get("name")
        schema = entry.get("input_schema")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", name):
            raise ConfigurationError("Operation service catalog contains an invalid operation name")
        alias = name.replace(".", "_")
        if alias in aliases:
            raise ConfigurationError("Operation service catalog contains colliding MCP tool aliases")
        if not isinstance(schema, dict):
            raise ConfigurationError("Operation service catalog contains an invalid input schema")
        aliases[alias] = name
        tools.append(types.Tool(
            name=alias,
            description=entry.get("description", name),
            inputSchema=schema,
            annotations=types.ToolAnnotations(
                title=name,
                readOnlyHint=entry.get("scope") == "read",
                destructiveHint=entry.get("scope") != "read",
                openWorldHint=True,
            ),
        ))
    return tools, aliases


def create_server(gateway: OperationGateway, catalog: dict[str, Any]) -> Server:
    tools, aliases = catalog_tools(catalog)
    server = Server(
        "modelbuilder", version="0.1.0",
        instructions=(
            "Model lifecycle operations share the IDE's authoritative operation service. "
            "Use expected_revision for updates; on http_status 409 read the current resource before retrying. "
            "Execution submission returns correlation/backend IDs, not proof of training completion. "
            "Do not treat cancel_requested as cancelled. Credentials are process environment configuration."
        ),
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return tools

    # Deliberately turn off SDK-side JSON Schema validation. The shared service
    # validates and audits every attempted known operation, including denied
    # writes. This preserves identical error contracts for IDE/API/MCP callers.
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        operation = aliases.get(name)
        if operation is None:
            return tool_result(error_body("unknown_tool", "Tool is not present in the service catalog"), is_error=True)
        try:
            return tool_result(await gateway.invoke(operation, arguments))
        except OperationError as error:
            return tool_result({**error.body, "http_status": error.status}, is_error=True)

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return [types.Resource(
            uri="modelbuilder://catalog", name="Operation catalog",
            description="Shared service operation names, schemas, and required scopes.",
            mimeType="application/json",
        )]

    @server.read_resource()
    async def read_resource(uri: Any) -> list[ReadResourceContents]:
        if str(uri) != "modelbuilder://catalog":
            raise ValueError("Unknown resource")
        return [ReadResourceContents(content=json.dumps(catalog, sort_keys=True), mime_type="application/json")]

    return server


async def serve(config: Configuration) -> None:
    async with httpx.AsyncClient(
        base_url=config.api_url,
        headers={"Authorization": f"Bearer {config.token}"},
        timeout=config.timeout,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        gateway = OperationGateway(client)
        catalog = await gateway.catalog()
        server = create_server(gateway, catalog)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    try:
        asyncio.run(serve(Configuration.from_environment()))
    except (ConfigurationError, OperationError) as error:
        # Human-readable, sanitized diagnostics go only to stderr.
        if isinstance(error, OperationError):
            print(f"modelbuilder-mcp: service startup failed (HTTP {error.status})", file=sys.stderr)
        else:
            print(f"modelbuilder-mcp: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
