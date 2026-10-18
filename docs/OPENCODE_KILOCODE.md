# OpenCode and Kilo Code project integration

The built-in project setup supports current documented project-local skill and local-STDIO MCP formats:

| Harness | MCP config | Skills |
|---|---|---|
| OpenCode | `opencode.json`, `mcp.modelbuilder` | `.opencode/skills/` |
| Kilo Code | `.kilo/kilo.json`, `mcp.modelbuilder` | `.kilo/skills/` |

Both use `type: local`, a command array, `enabled: true`, and a bounded discovery timeout. Setup accepts only the native project-scoped managed command. No owner token, provider credentials, global config, permission allowlist, sign-in or paid inference is written. Existing unrelated settings and servers are preserved. Duplicate/malformed JSON and alternate/legacy config locations block setup for explicit precedence review; JSONC is not rewritten or silently shadowed.

The same project workflow and repository-migration skill files are packaged for Codex, Claude, OpenCode and Kilo. Triggerfish receives the same migration package in its existing inactive compatibility bundle; its upstream activation gates are unchanged. Managed preview, exact-change approval, receipts, conflict detection and rollback remain common to all paths.

Validation includes temporary-file setup/rollback, preservation of unrelated settings, and actual cold-start of the generated commands against ModelBuilder's MCP server after the IDE lease and service have closed. **This does not establish upstream vendor skill discovery, extension installation, sign-in, model-provider behavior, or Flatpak host-terminal launching.** OpenCode/Kilo extension installation is intentionally not offered until a vendor version and distribution path are verified. Project setup requires a native managed project.

First project open offers an explanation of independent MCP availability. Dismissal is remembered; no configuration is written by the notice. The existing setup flow shows and approves the concrete changes. “Disconnect managed project integration” removes only unchanged managed entries/skills/instruction blocks, preserves user edits, and requires restarting the assistant to close an existing process.

Verified format references (2026-10-18): [OpenCode MCP](https://docs.opencode.ai/docs/mcp-servers/), [OpenCode skills](https://docs.opencode.ai/docs/skills/), [Kilo MCP](https://kilo.ai/docs/automate/mcp/using-in-kilo-code), [Kilo skills](https://kilo.ai/docs/customize/skills).
