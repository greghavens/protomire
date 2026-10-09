# Exact-fork assistant compatibility, 2026-10-09

Tested editor: source-built Modelbuilder IDE Dev / Code OSS 1.105.0,
`03c265b1adee71ac88f833e065f7bb956b60550a`, Linux x64.

## Observed results

| Gate | Codex | Claude Code |
|---|---|---|
| Exact version | openai.chatgpt 26.5908.31748 | Anthropic.claude-code 2.1.295 |
| Open VSX SHA256 verified, separately installed | PASS | PASS |
| Actual Extension Host activation / registered commands | PASS | PASS |
| Actual editor-tab account sign-in UI | PASS | PASS |
| Bundled vendor transport connects to native MCP | PASS: app-server enumerates 48 tools and catalog resource | PASS: `mcp list` reports Connected |
| Direct vendor transport tool call | BLOCKED: local thread sandbox initialization failed before a call | Not attempted without an authenticated model session |
| Authenticated model-agent CRUD/run/error/permission parity | BLOCKED | BLOCKED |

The authentication screens require the user's own vendor account or API/provider
credentials. No sign-in, OAuth grant, credential retrieval, paid inference, or
model prompt was performed. The successful MCP transport probes used an isolated
local operation service with a disposable synthetic token and temporary vendor
configuration homes, all cleaned up afterward. They are not generic-client
results, but are also not a substitute for authenticated agent behavior.

Codex's extra direct-call probe could not create a local ephemeral thread because
its sandbox helper failed opening
`/tmp/codex-bwrap-synthetic-mount-targets-1000/lock` (read-only filesystem).
No sandbox disabling, security-policy change, or alternate bypass was attempted.

Compatibility warnings on this pinned editor include Codex's missing
`languageModelProxy` proposal and Claude's unsupported `contribSecondarySidebar`
proposal. Both editor tabs rendered; the warnings are not a claim of complete
compatibility. Vendor webview font CSP warnings were not bypassed.

## Licensing and installation boundary

These VSIX files and their installed binaries are local test/user installations,
not product bundles or redistribution artifacts. No explicit legal acceptance
prompt was displayed by either CLI installation.

- Codex archive `LICENSE.md` points to the
  [OpenAI Terms of Use](https://openai.com/policies/row-terms-of-use/), which apply
  through use. Codex CLI source licensing does not grant rights to redistribute
  the IDE extension binary.
- Claude's package license states use is governed by the agreements listed in
  [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance).
  The applicable consumer/commercial terms and product-preinstallation conditions
  must be reviewed before distribution. The binary was not modified.

## Evidence

`evidence/gui/distribution/assistant-install-results.json` records exact artifact
URLs, hashes and CLI status. `assistant-activation-results.json` records actual
Extension Host activation and limitations. `assistant-mcp-discovery.json` contains
the bundled clients' real MCP transport results, schema inventory and sandbox
failure. `codex-claude-auth-required.jpg` is an unedited native desktop capture
showing both installed vendor sign-in screens on this exact editor.
