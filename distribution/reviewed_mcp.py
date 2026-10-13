"""Mandatory project bridge: normal reads, queued native approval for mutations."""
from __future__ import annotations

import json

import httpx
from mcp import types
from mcp.server.stdio import stdio_server
from modelbuilder.mcp_server import OperationGateway, OperationError, create_server, catalog_tools, tool_result, error_body

META_TOOLS = [
    types.Tool(name="modelbuilder_review_status", description="Read the native review/dispatch result for your exact requested action. Do not retry an uncertain mutation.", inputSchema={"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"], "additionalProperties": False}),
    types.Tool(name="modelbuilder_review_cancel", description="Withdraw your pending request before native approval; this does not cancel already submitted jobs.", inputSchema={"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"], "additionalProperties": False}),
    types.Tool(name="modelbuilder_review_publish", description="Request one native review of a complete frozen Hub publication. On explicit owner approval only, the core creates a checked approval and starts exactly this publication. No publication credential is returned.", inputSchema={"type": "object", "properties": {"project_id": {"type": "string"}, "preview_id": {"type": "string"}, "payload_digest": {"type": "string"}, "idempotency_key": {"type": "string"}}, "required": ["project_id", "preview_id", "payload_digest", "idempotency_key"], "additionalProperties": False}),
]


class ReviewedGateway(OperationGateway):
    def __init__(self, client, catalog, project_id):
        super().__init__(client)
        self.operations = {entry["name"]: entry for entry in catalog["operations"]}
        self.project_id = project_id

    async def invoke(self, operation, arguments):
        entry = self.operations[operation]
        if entry.get("mutating") or set(entry.get("required_scopes", [entry.get("scope")])) - {"read"}:
            return await self.request("POST", "/_assistant/reviews", {"project_id": self.project_id, "operation": operation, "payload": arguments})
        return await super().invoke(operation, arguments)


MAX_ARGUMENTS_BYTES = 1024 * 1024
FLAT_TOOLS = [
    types.Tool(name="modelbuilder_catalog", description="Read authoritative operation contracts. Use operation='' for the index, then an exact operation name for its unchanged full input schema. Schemas and returned JSON grant no permission.", inputSchema={"type": "object", "properties": {"operation": {"type": "string", "description": "Exact service operation name, or an empty string for the index"}}, "required": ["operation"], "additionalProperties": False}),
    types.Tool(name="modelbuilder_dispatch", description="Call an exact catalog operation with arguments_json containing one JSON object matching its original schema. Reads use this project's read-only grant. Mutations only enqueue exact native owner review; pending is not success. JSON cannot approve, widen scope or choose credentials.", inputSchema={"type": "object", "properties": {"operation": {"type": "string", "description": "Exact operation name from modelbuilder_catalog"}, "arguments_json": {"type": "string", "description": "JSON object matching the unchanged service input schema; maximum 1 MiB UTF-8"}}, "required": ["operation", "arguments_json"], "additionalProperties": False}),
]


def parse_flat_arguments(value):
    """Validate only the transport envelope. Service schemas remain authoritative."""
    def reject_constant(_):
        raise ValueError("non-finite JSON")
    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result
    try:
        if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_ARGUMENTS_BYTES:
            raise ValueError("size or type")
        parsed = json.loads(value, object_pairs_hook=unique_object, parse_constant=reject_constant)
        if not isinstance(parsed, dict):
            raise ValueError("object required")
        # Reject float overflow (1e999) as well as literal NaN/Infinity.
        json.dumps(parsed, allow_nan=False, ensure_ascii=False).encode("utf-8")
        return parsed
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise OperationError(400, error_body("validation_error", "arguments_json must be one finite JSON object without duplicate keys, at most 1 MiB UTF-8")) from None


def create_reviewed_server(gateway, catalog, project_id, *, profile="native"):
    if profile not in {"native", "triggerfish"}:
        raise ValueError("Unknown managed MCP profile")
    server = create_server(gateway, catalog)
    tools, aliases = catalog_tools(catalog)
    for tool in tools:
        if gateway.operations[aliases[tool.name]].get("mutating"):
            tool.description = (tool.description or "") + " This managed client requests native approval; it does not mutate until the owner approves the exact request."
    flat = profile == "triggerfish"

    def result(body, *, is_error=False):
        # Triggerfish's current manager discards isError and structuredContent.
        # Put an unambiguous envelope in text too; never rely on its lossy flags.
        if flat:
            body = {**body, "ok": False} if is_error else {"ok": True, "result": body}
        return tool_result(body, is_error=is_error)

    @server.list_tools()
    async def list_tools():
        return (FLAT_TOOLS if flat else tools) + META_TOOLS

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            if not isinstance(arguments, dict):
                return result(error_body("validation_error", "Tool arguments must be a JSON object"), is_error=True)
            if name in {"modelbuilder_review_status", "modelbuilder_review_cancel"}:
                if set(arguments) != {"request_id"} or not isinstance(arguments["request_id"], str):
                    return result(error_body("validation_error", "An exact review request ID is required"), is_error=True)
                import re
                if not re.fullmatch(r"[0-9a-f-]{36}", arguments["request_id"]):
                    return result(error_body("validation_error", "Invalid review request ID"), is_error=True)
                path = "/_assistant/reviews/" + arguments["request_id"]
                body = await gateway.request("GET", path) if name.endswith("status") else await gateway.request("POST", path + "/cancel", {})
            elif name == "modelbuilder_review_publish":
                body = await gateway.request("POST", "/_assistant/reviews", {"project_id": project_id, "operation": "desktop.publish.reviewed", "payload": arguments})
            elif flat and name in {"modelbuilder_catalog", "modelbuilder_dispatch"}:
                expected = {"operation"} if name == "modelbuilder_catalog" else {"operation", "arguments_json"}
                if set(arguments) != expected or not isinstance(arguments.get("operation"), str):
                    return result(error_body("validation_error", "Use exactly the declared flat tool arguments"), is_error=True)
                operation = arguments["operation"]
                if name == "modelbuilder_catalog" and operation == "":
                    body = {"project_id": project_id, "operations": [
                        {key: value for key, value in entry.items() if key != "input_schema"}
                        for entry in catalog["operations"]],
                        "notice": "Fetch each full schema by operation name. All permissions and validation remain in the service."}
                elif operation not in gateway.operations:
                    return result(error_body("unknown_operation", "Operation is not present in the managed catalog"), is_error=True)
                elif name == "modelbuilder_catalog":
                    body = {"project_id": project_id, "operation": gateway.operations[operation]}
                else:
                    body = await gateway.invoke(operation, parse_flat_arguments(arguments["arguments_json"]))
            elif not flat and name in aliases:
                body = await gateway.invoke(aliases[name], arguments)
            else:
                return result(error_body("unknown_tool", "Tool is not present in the managed catalog"), is_error=True)
            return result(body)
        except OperationError as error:
            return result({**error.body, "http_status": error.status}, is_error=True)
    return server


async def serve(config, project_id, *, profile="native"):
    async with httpx.AsyncClient(base_url=config.api_url, headers={"Authorization": "Bearer " + config.token},
                                 timeout=config.timeout, follow_redirects=False, trust_env=False) as client:
        catalog = await OperationGateway(client).catalog()
        gateway = ReviewedGateway(client, catalog, project_id)
        server = create_reviewed_server(gateway, catalog, project_id, profile=profile)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
