"""Independent official SDK/stdio client for the synthetic PDF HTTP harness."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    request = json.loads(sys.argv[1])
    raw = Path(request["fixture_path"]).read_bytes()
    async with stdio_client(StdioServerParameters(command=sys.executable, args=["-m", "modelbuilder.mcp_server"],
                                                   env=dict(os.environ))) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            async def call(name, payload):
                result = await session.call_tool(name, payload)
                if result.isError:
                    raise RuntimeError("Synthetic document MCP call failed: " + name)
                return result.structuredContent or json.loads(result.content[0].text)
            capabilities = await call("research_document_capabilities", {"project_id": request["project_id"]})
            assert capabilities["available"] is True
            imported = await call("research_document_import", {
                "project_id": request["project_id"], "evidence_id": request["evidence_id"],
                "expected_evidence_revision": request["evidence_revision"], "filename": "fixture.pdf",
                "media_type": "application/pdf", "content_base64": base64.b64encode(raw).decode(),
                "expected_sha256": hashlib.sha256(raw).hexdigest(), "license": "Self-authored fixture; authorized extraction",
                "rights_basis": "self_authored", "rights_confirmed": True, "idempotency_key": str(uuid4())})
            assert imported["status"] == "succeeded"
            identity = {"project_id": request["project_id"], "text_id": imported["text_id"]}
            body = await call("research_text_get", identity)
            found = await call("research_text_search", {**identity, "query": "Limitations"})
            assert found["matches"][0]["pages"][0]["page_number"] == 2
            assert body["provenance"]["raw_sha256"] == hashlib.sha256(raw).hexdigest()
            print(json.dumps({"text_id": imported["text_id"], "text_sha256": body["sha256"],
                              "raw_sha256": body["provenance"]["raw_sha256"],
                              "matched_page_sha256": found["matches"][0]["pages"][0]["sha256"],
                              "status": "PASS", "claim_verified": body["claim_verified"]}))


if __name__ == "__main__":
    asyncio.run(main())
