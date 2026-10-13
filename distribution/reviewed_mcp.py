"""Mandatory project bridge: normal reads, queued native approval for mutations."""
from __future__ import annotations

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


async def serve(config, project_id):
    async with httpx.AsyncClient(base_url=config.api_url, headers={"Authorization": "Bearer " + config.token},
                                 timeout=config.timeout, follow_redirects=False, trust_env=False) as client:
        catalog = await OperationGateway(client).catalog()
        gateway = ReviewedGateway(client, catalog, project_id)
        server = create_server(gateway, catalog)
        tools, aliases = catalog_tools(catalog)
        for tool in tools:
            if gateway.operations[aliases[tool.name]].get("mutating"):
                tool.description = (tool.description or "") + " This managed client requests native approval; it does not mutate until the owner approves the exact request."
        @server.list_tools()
        async def list_tools():
            return tools + META_TOOLS
        @server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            try:
                if not isinstance(arguments, dict):
                    return tool_result(error_body("validation_error", "Tool arguments must be a JSON object"), is_error=True)
                if name in {"modelbuilder_review_status", "modelbuilder_review_cancel"}:
                    if set(arguments) != {"request_id"} or not isinstance(arguments["request_id"], str):
                        return tool_result(error_body("validation_error", "An exact review request ID is required"), is_error=True)
                    import re
                    if not re.fullmatch(r"[0-9a-f-]{36}", arguments["request_id"]):
                        return tool_result(error_body("validation_error", "Invalid review request ID"), is_error=True)
                    path = "/_assistant/reviews/" + arguments["request_id"]
                    result = await gateway.request("GET", path) if name.endswith("status") else await gateway.request("POST", path + "/cancel", {})
                elif name == "modelbuilder_review_publish":
                    result = await gateway.request("POST", "/_assistant/reviews", {"project_id": project_id, "operation": "desktop.publish.reviewed", "payload": arguments})
                elif name in aliases:
                    result = await gateway.invoke(aliases[name], arguments)
                else:
                    return tool_result(error_body("unknown_tool", "Tool is not present in the managed catalog"), is_error=True)
                return tool_result(result)
            except OperationError as error:
                return tool_result({**error.body, "http_status": error.status}, is_error=True)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
