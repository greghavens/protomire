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


## Platform/channel audit (2026-10-09)

Three different gates must remain separate: vendor runtime support, availability
of a particular version in an authorized installation channel, and acceptance
of that package on this exact Modelbuilder distribution. Availability is not
installation, activation, authentication, or a successful model-agent tool call.

The official [Codex Windows documentation](https://developers.openai.com/codex/windows)
explicitly supports the CLI and IDE extension on Windows. The
[Codex IDE guide](https://developers.openai.com/codex/ide) documents installation
in compatible editors. The [Claude requirements](https://code.claude.com/docs/en/setup)
describe macOS, Windows and Linux x64/ARM64; its
[IDE guide](https://code.claude.com/docs/en/vs-code) explains that the extension
bundles its own CLI. Native Windows sandbox capabilities differ from WSL and
must be assessed independently; this project does not weaken sandbox settings.
Neither a general vendor OS statement nor CLI source licensing grants rights
to redistribute an IDE binary.

Exact-version Open VSX metadata was read for Codex **26.5908.31748** and Claude
Code **2.1.295**. The registry returned these platform packages:

| Target | Codex pinned Open VSX package | Claude pinned Open VSX package | Exact Modelbuilder acceptance |
|---|---|---|---|
| Linux x64, glibc | Listed | Listed | Prior activation/discovery passed; authenticated agent calls blocked |
| Linux ARM64, glibc | Listed | Listed | Not run |
| macOS Intel | Listed | Listed | Not run |
| macOS Apple Silicon | Listed | Listed | Not run |
| Windows x64 | Not listed for this pin/channel | Listed | Not run |
| Windows ARM64 | Not listed for this pin/channel | Listed | Not run |
| Linux ARM32 (`linux-armhf`) | Not listed | Not listed | Not run; no pinned assistant package |
| Alpine x64 / ARM64 | Listed | Listed | Not run; native desktop build is a separate gate |
| Browser/web | Not listed | Not listed | Native extension host and stdio cannot run in a browser-only host |

Missing Windows packages in this specific Codex Open VSX version are an
**installation-channel gap**, not a claim that Codex does not support Windows.
An owner-supplied official, appropriately licensed VSIX can be installed through
the generic IDE CLI. Its version, checksum, embedded target, licensing, and
exact-fork activation must be reviewed before calling it supported. Do not add
Microsoft Marketplace endpoints to Code OSS, assume a newer package is
compatible, or manufacture a URL for a missing package. A Windows ARM64-specific
Codex extension binary has not been inspected in this audit.

The precise returned URLs are retained in
[`assistant-package-availability.json`](../evidence/platforms/assistant-package-availability.json).
The additional assistant packages were not downloaded or executed in this audit.
Their native binaries have not been exercised; only the prior Linux x64 bundled
transports have runtime evidence. Browser-based Codex/Claude products and remote
extension hosts are separate products/deployment paths, not proof that this
native desktop distribution runs as a web extension.

## Explicit installation and MCP setup boundary

The optional production setup helper now lives in `distribution/assistant_setup.py`
and is included in the native feature's resources. Its user-invoked IDE action
and CLI keep install, configuration and discovery separate. Nothing is bundled,
automatically installed at startup, or configured with a persisted credential.
See [the setup guide](ASSISTANT_SETUP.md) for exact commands and security limits.

- Install previews show vendor, extension ID, exact version, native target,
  official Open VSX source, SHA256 and terms. A second explicit user action accepts
  those terms and installs the approved package; embedded identity/target and
  exact post-install version are checked. No Marketplace rights are assumed.
- Configuration previews select the actual installed service Python/module,
  approved service URL and client config path. Apply preserves unrelated entries,
  backs up the original, checks its digest and replaces it atomically. Codex uses
  its documented `env_vars` token allow-list; Claude uses `${MODELBUILDER_TOKEN}`
  expansion in a project `.mcp.json`. Neither writes a token or reads IDE secrets.
- The user supplies a scoped token in the assistant's environment and starts the
  operation service separately. HTTPS is required away from loopback. Vendor
  account sign-in and paid inference are separate user actions.
- Readiness checks distinguish interpreter/module availability, missing token,
  native tool discovery, installed vendor discovery, and authenticated behavior.
  Optional vendor transport probes use isolated homes and no model prompts.

Current 2026-10-09 evidence in
[`native-and-codex-discovery.json`](../evidence/assistant-setup/native-and-codex-discovery.json)
shows the already-installed **Codex CLI 0.159.2** and native MCP each enumerating
**61 tools** using a disposable local read-only token. It is not a rerun of the
historical IDE extension, a vendor account session, or model-agent acceptance.
Claude is absent in the current execution environment, recorded in
[`native-and-claude-absence.json`](../evidence/assistant-setup/native-and-claude-absence.json).
No assistant package was downloaded/installed in this implementation check.

The earlier `tests/gui/probe_assistant_mcp.py` remains a historical exact-fork
acceptance probe, not the production registration path. It discovers binaries
by manifest identity and supports explicit paths without fixed versions or
architecture directories. Windows uses native `.exe`/`Scripts/python.exe`
layouts. Other platforms still require real native GUI/vendor acceptance;
offline path/config tests do not promote them to runtime PASS.

## Platform-selected development extensions

`python distribution/install_extensions.py` now detects the native target and
selects an exact pinned artifact from `extension_manifest/extensions.lock.json`.
The default manifest contains only the previously audited MIT Python, Jupyter,
basedpyright and Python Debugger packages; it does not include vendor assistants.
Python Debugger has separate SHA256-pinned artifacts for all seven native desktop
targets. Each archive's checksum, embedded publisher/name/version and VSIX target
are checked before installation. Cache names include target to prevent one
architecture reusing another's file. The other three pins use their explicit
universal packages. This does not imply every feature works on every host.

Configuration is explicit:

- `--target` or `MODELBUILDER_TARGET` selects a native target; default detects host.
- `--download-only --target darwin-arm64` can prepare another target's artifacts.
- Native installation rejects a target that differs from the host.
- `--manifest` selects a reviewed schema-version-2 manifest; unknown or unpinned
  targets fail before any download or installation. No fallback to `latest`.
- `--cli`, `--cache-dir`, `--user-data-dir`, and `--extensions-dir` override paths.
- `MODELBUILDER_RUNTIME_DIR` and `MODELBUILDER_BUILD_CACHE` share build defaults.
- The default CLI is `<runtime>/bin/modelbuilder-ide.py`, invoked with the current
  Python interpreter; paths are passed as separate arguments, not shell strings.
- Extension-pack dependencies remain excluded to avoid silently installing
  optional proprietary components. This installer neither provisions credentials
  nor changes vendor MCP settings.

All seven debugpy VSIX archives were downloaded for static integrity/identity
inspection during this change; the results are retained in
[`debugpy-artifacts.json`](../evidence/platforms/debugpy-artifacts.json).
Offline selector/security tests do not replace native GUI, debugger, notebook,
OS keyring, authentication, or end-to-end agent acceptance on each target.
