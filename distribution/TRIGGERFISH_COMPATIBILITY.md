# Triggerfish harness compatibility preparation

Status: ModelBuilder-side transport implemented and tested with real local
managed MCP/HTTP/SQLite. Triggerfish installation, provider authentication,
IDE/CLI activation and actual vendor-agent calls remain **UNVERIFIED**.
Triggerfish is an owner-selected harness alongside Codex and Claude; it is not
bundled, auto-installed, auto-started or connected to an existing daemon.

## Audited upstream contract, 2026-10-13

Default upstream HEAD:
[`571c42b9299ab3006ce96fb541b7f1a23988d3b5`](https://github.com/greghavens/triggerfish/commit/571c42b9299ab3006ce96fb541b7f1a23988d3b5).
Latest published release observed: [v0.7.19](https://github.com/greghavens/triggerfish/releases/tag/v0.7.19),
annotated tag resolving to `d2c7f06d80e5b22cbacb794a2067afd020c8e63a`.
The inspected MCP/config/skills files are unchanged between that release and
HEAD. This source comparison is not binary verification or vendor acceptance.
The release also predates HEAD's Tauri security update; installation acceptance
must review the exact chosen official artifact rather than assume release/HEAD
are interchangeable.

- [MCP executor](https://github.com/greghavens/triggerfish/blob/571c42b9299ab3006ce96fb541b7f1a23988d3b5/src/mcp/executor.ts)
  maps top-level type/description/required/items/enum but loses nested properties,
  `$defs`, unions and `const`. Raw ModelBuilder tools are not represented faithfully.
- [Manager adapter](https://github.com/greghavens/triggerfish/blob/571c42b9299ab3006ce96fb541b7f1a23988d3b5/src/mcp/manager_env.ts)
  preserves text and discards MCP `isError` and `structuredContent` in the agent path.
- [MCP client](https://github.com/greghavens/triggerfish/blob/571c42b9299ab3006ce96fb541b7f1a23988d3b5/src/mcp/client/protocol.ts)
  initializes with `2024-11-05`, sends `notifications/initialized` and uses ordinary
  newline-delimited JSON-RPC stdio tools/list and tools/call.
- [Paths](https://github.com/greghavens/triggerfish/blob/571c42b9299ab3006ce96fb541b7f1a23988d3b5/src/cli/config/paths.ts)
  support `TRIGGERFISH_DATA_DIR` and `<data-root>/triggerfish.yaml`.
  `mcp.servers` is the preferred config map; `mcp_servers` is the legacy fallback.
  [Startup wiring](https://github.com/greghavens/triggerfish/blob/571c42b9299ab3006ce96fb541b7f1a23988d3b5/src/gateway/startup/infra/mcp.ts)
  forwards command/args/env/url/classification/enabled only. It does not forward
  allowedCommands or toolCallTimeoutMs, even though manager-level types have them.
- [Command allowlist](https://github.com/greghavens/triggerfish/blob/571c42b9299ab3006ce96fb541b7f1a23988d3b5/src/mcp/manager_connection.ts)
  permits basenames npx/node/python3/python/deno/uvx. `python3.12` and `python.exe`
  do not pass. Preparation reports a blocked basename, never invents an override,
  changes the interpreter or creates an alias.
- [Skill discovery](https://github.com/greghavens/triggerfish/blob/571c42b9299ab3006ce96fb541b7f1a23988d3b5/src/gateway/startup/infra/subsystems.ts)
  uses `<data-root>/skills` and `<data-root>/workspaces/main/skills`.
  Skill classification defaults to PUBLIC and must be reviewed for actual use.
- [Gateway startup](https://github.com/greghavens/triggerfish/blob/571c42b9299ab3006ce96fb541b7f1a23988d3b5/src/gateway/startup/services/chat_session_network.ts)
  hardcodes port 18789, and [CLI chat](https://github.com/greghavens/triggerfish/blob/571c42b9299ab3006ce96fb541b7f1a23988d3b5/src/channels/cli/chat_connection.ts)
  connects to that same fixed port. Data-root separation is not concurrent
  per-project daemon isolation. Automatic launch/join remains unavailable.

## ModelBuilder-side adapter

The explicit managed command suffix `--mcp-profile triggerfish` selects a flat
compatibility profile. The default native profile is unchanged for Claude/Codex.
Both profiles use the same managed lease, project-limited read-only credential,
authoritative service, ReviewedGateway and exact native owner review queue.

Tools:
- `modelbuilder_catalog(operation)` returns an index for an empty string, or
  the unchanged full service input schema for one exact operation name.
- `modelbuilder_dispatch(operation, arguments_json)` decodes one JSON object and
  sends it to the original service path or review queue. The JSON string is data,
  never policy or approval. Duplicate keys, nonfinite numbers, invalid UTF-8,
  nonobjects and strings over 1 MiB UTF-8 are rejected. Service input validation
  remains authoritative; no permissive replacement schema is introduced.
- Existing review status/cancel/publication tools stay available with flat string
  inputs. There is no assistant approval tool or direct owner endpoint.

All text results have `ok:true,result` or `ok:false,error`, with HTTP status for
service errors. MCP structuredContent and isError remain present too. The text
contract survives the upstream manager's lossy result conversion. Pending review
is not a completed mutation. A repeated idempotency key reconciles the original
request; it does not approve it or create a second grant.

The underlying same-user shell is not a hostile-process sandbox. An unrestricted
vendor shell can access files owned by that OS user; the bridge does not pretend
to isolate owner credentials from such a process.

## Reviewed inactive preparation in the IDE

Choose Triggerfish for a native managed project, then “Prepare inactive project
skills and MCP fragment.” The native preview names all files and activation
warnings; cancellation writes nothing. Applying requires that exact preview's
digest and rejects changed files or a changed project/managed command. A non-executing
AST check reads the managed entrypoint as data and rejects an older runtime
without the literal argparse --mcp-profile declaration before writing files. It
does not execute caller-supplied code or acquire a lease, and does not establish
executable trust. Native sealed-runtime checks remain authoritative.

The source-scoped bundle lives under `.modelbuilder/triggerfish/`:
- `mcp.fragment.yaml`: supported JSON-as-YAML `mcp.servers.modelbuilder` entry,
  exact managed command plus compatibility profile, `enabled:false`, no credentials
  or automatic classification. This is not an active `triggerfish.yaml`.
- `skill-template/modelbuilder-project/SKILL.md` and references: shared project workflow,
  exact existing catalog reference snapshot, and flat-transport usage guidance.
- `SETUP.md`: supported upstream layout and truthful activation/install gates.

Skills are staged under skill-template, outside upstream discovery paths, so
preparation cannot silently install a loadable skill into an existing profile.
No global settings, provider configuration, plugins, daemons or ports are touched.
The owner must separately review official installation, provider/account access,
server and skill classification, single-instance feasibility and actual vendor
acceptance. The preparation action does not authorize those steps. Re-running
unchanged preparation is idempotent; rollback removes only unchanged owned files
and preserves owner edits and separately created vendor data.

The managed entrypoint and reviewed transport must be rebuilt into the sealed
application runtime and its inventory repinned after this source change. An older
runtime does not gain the new profile by installing IDE-side templates alone.

## Verification boundaries

`tests/distribution/test_triggerfish_mcp.py` starts the actual ModelBuilder managed
bridge/service and exercises the observed upstream stdio handshake and flat text
contract with a protocol fixture. It proves ModelBuilder-side interoperability,
not Triggerfish execution. Coverage includes nested JSON and bounds, unchanged
schemas, cross-project rejection, native exact approval, duplicate/replayed and
changed requests, denial, cancellation and stale project revisions.

Project-file tests cover inactive output, no secret/global-config reads or writes,
preview/apply approval, symlinks/conflicts/changed project, idempotency, rollback
and the unsupported interpreter basename gate. Native host tests cover the
preparation flow, cancellation and rejecting install/configure actions for
Triggerfish. Existing native transport and Codex/Claude setup tests remain intact.
