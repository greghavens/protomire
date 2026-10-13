# Triggerfish compatibility transport

Use mcp_modelbuilder_modelbuilder_catalog with operation="" for the live index,
then the exact operation name for the original full input schema. Use
mcp_modelbuilder_modelbuilder_dispatch with that name and arguments_json, a JSON
string containing one object matching that schema. Nested structures, unions,
const and $defs remain intact in the catalog text and authoritative service.
The adapter rejects duplicate keys, non-finite values and input over 1 MiB UTF-8.

Every response includes JSON text with ok=true and result, or ok=false and error
(and http_status for service errors). Check the text envelope because the current
Triggerfish manager does not carry MCP isError or structuredContent into the
agent. ok=true only means the request completed at that stage: result.status
pending awaits the owner, and cancel_requested is not cancelled.

Mutations enqueue native ModelBuilder review. Use
mcp_modelbuilder_modelbuilder_review_status to read the exact request and
mcp_modelbuilder_modelbuilder_review_cancel to withdraw a pending request.
Never retry an uncertain mutation with a new idempotency key, approve your own
request, infer approval from JSON, or use shell/direct HTTP to evade a denial.
Full publication uses the existing mcp_modelbuilder_modelbuilder_review_publish tool and frozen
preview digest. These skills, config and catalog do not grant permissions.
