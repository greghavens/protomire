# Native MCP acceptance

Run `.venv/bin/python scripts/mcp_smoke.py --output evidence/mcp` from the repository root. The command creates a disposable SQLite database and two random in-memory test credentials. It launches the actual HTTP application service and standalone MCP adapter in separate OS processes. The official MCP SDK client in the harness communicates through real stdio pipes; it never imports the application handlers. The database and credentials are removed at completion.

The checked-in `summary.json`, `external-stdio-transcript.json`, `smoke.log`, and subprocess logs are produced by that command, not fabricated fixtures. Request headers and credentials are excluded. `pytest.log` records the focused adapter and external-client suite.

## Verified

- Discovery and schemas generated from the authenticated HTTP catalog
- Project and recipe creation; recipe read, update, validation, clone, export, and import
- Exact normalized IDs, revisions, digests, and successful resource bodies across HTTP and MCP
- Idempotent mutation retry
- Stale write rejected with HTTP 409 preserved in MCP structured content
- Actual concurrent HTTP/MCP updates: one succeeds, one conflicts, no lost update
- Every cataloged non-read operation denied with HTTP 403 for the read-only credential; project-scoped denials remain in the shared audit history
- Unknown tool, unknown argument, unsupported recipe version, and tampered import rejected
- All created IDs and final representations survive SIGKILL and restart of the HTTP service while the same MCP process/client remain alive
- No IDE process is launched or needed by the harness

## Boundaries

This is real generic MCP protocol interoperability. It does not establish installed Claude Code or Codex interoperability, real GUI interaction, actual job restart/reconciliation, or clean-environment training replay. The HTTP calls exercise the IDE's operation contract but are not GUI automation. Export/import uses a safe, non-executable draft recipe with unresolved source/license information, not a reproducible trained-model bundle. Execution on import remains false.

The server advertises required scopes and forwards every known operation to the service. The service, not discovery filtering or SDK-side validation, enforces read-only permissions and audits denied operations. This prevents hidden business logic and preserves the same structured errors as the HTTP client.

## Configuration

Start the application service separately. Set `MODELBUILDER_API_URL` (default `http://127.0.0.1:8765`) and `MODELBUILDER_TOKEN` in the MCP process environment, then run `modelbuilder-mcp` or `python -m modelbuilder.mcp_server`. Credentials are never command-line arguments. Non-loopback service URLs require HTTPS; redirects are not followed. `MODELBUILDER_TIMEOUT` defaults to 30 seconds. The adapter never automatically retries a mutation. Restart the MCP process after changing the service's operation catalog.
