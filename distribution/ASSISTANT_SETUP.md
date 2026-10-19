# Optional Claude Code / Codex setup

Modelbuilder remains a complete human-operated native Code OSS workbench. This
opt-in setup connects an existing, independently licensed assistant to the same
operation service through the shipped native MCP module. It does not add a chat
UI, bundle or redistribute vendor binaries, create an account, sign in, issue
credentials, or send a model prompt.

## Managed project integrations

Packaged projects support separate Claude, Codex, OpenCode and Kilo project-local
skills and MCP configurations. Codex, OpenCode and Kilo can share `AGENTS.md`:
each client owns one exact instruction block backed by its applied receipt.
Setup and disconnect preserve user text and the other clients' blocks. Unknown,
unreceipted, duplicated or edited managed markers require conflict review.
Reviewed transactions check both file hashes and the peer receipts under the
project setup lock. Disconnecting one client leaves the others configured.
These files grant no execution authority or vendor authentication.

## Native workbench flow

Use **ModelBuilder: Set Up Optional Coding Tool** (also available from the
Code page). Choose Codex or Claude Code, then one separate action:

1. **Check MCP setup** checks the selected installed service Python and native MCP
   readiness. Missing client, missing environment credential, failed discovery,
   and unverified authenticated behavior remain distinct results.
2. **Configure MCP connection** previews the actual Python/module, service URL,
   destination file, exact new server entry and whether it replaces an existing
   `modelbuilder` entry. Review the plan and explicitly apply it. Close vendor
   clients before applying, then restart them afterward.
3. **Install optional extension** previews the exact vendor, extension ID,
   version, host target, HTTPS source, registry SHA256, installation profile and
   applicable terms. Only **Accept terms and install** starts the download and
   installation. Existing vendor account sign-in remains your own separate step.
4. **View supported versions** shows the pinned choices and installed-client
   discovery. A listed pin is not a claim that every host has passed acceptance.

The feature includes a standalone Python helper in its resources; select the
absolute Python interpreter from the actual installed Modelbuilder service. The
distribution launcher supplies its portable `.py` CLI path and any explicitly
chosen extension/user-data profile. If that metadata is absent, select the real
IDE launcher. The helper does not invent a profile path. An unselected profile
uses the IDE CLI's own default, so use the same launch/installation profile.

## Client configuration and credential boundary

- Codex: `$CODEX_HOME/config.toml`, otherwise `~/.codex/config.toml`, with a
  `[mcp_servers.modelbuilder]` table. `env_vars = ["MODELBUILDER_TOKEN"]` forwards
  the token from the Codex process environment. The service URL is a non-secret
  value under its `env` table. The CLI and IDE extension use the same config.
- Claude Code: the selected local workspace's `.mcp.json`, under
  `mcpServers.modelbuilder`, with `type: stdio`. Its `env` contains
  `MODELBUILDER_TOKEN: "${MODELBUILDER_TOKEN}"`, using the client's supported
  expansion syntax. Claude retains its own workspace/server trust prompts.
- Both launch the chosen interpreter with `-I -m modelbuilder.mcp_server`. The
  helper first verifies that interpreter can locate the installed module; it
  never assumes `.venv/bin/python`, a username, extension version directory, or
  architecture-specific binary path. `-I` excludes workspace/PYTHONPATH code
  injection; the service package must actually be installed in that environment.
- Provide a scoped service credential in the **assistant process environment**
  yourself using your approved secret mechanism, then restart the assistant/IDE
  so it inherits the variable. Environment inheritance is not an OS keychain.
  Launching a GUI from a desktop icon may not inherit terminal variables.
- The helper never accepts a token argument, fetches the IDE's SecretStorage
  value, writes a token to either config, saves vendor credentials, or generates
  a service token. It does not start/install an operation-service daemon.
- No anonymous MCP access is enabled. A missing token stays blocked. Use only the
  service scopes you intend the assistant to have. Connecting the service makes
  those operations available to the assistant; its own approval policy and the
  service's permissions still apply.
- Non-loopback service URLs require HTTPS. Credentials in URLs, query strings,
  fragments, and `${...}` interpolation in the service URL are rejected.

The generated workspace file has machine-specific absolute paths. Review it
before sharing or committing it; no recipe/export contains this configuration.
Vendor authentication and paid inference require the user's independent action.

## CLI contract

Python 3.12 is the service runtime. The helper itself uses only the standard
library (Python 3.11+). Every successful invocation prints one JSON object;
errors print `{ "ok": false, "error": { "code": ..., "message": ... } }` and
exit 2. Arbitrary vendor/stdout/stderr diagnostics and credentials are omitted.

```text
python distribution/assistant_setup.py catalog
python distribution/assistant_setup.py preview-config --client codex --python /absolute/service/python --service-url http://127.0.0.1:8765
python distribution/assistant_setup.py preview-config --client claude --python /absolute/service/python --service-url http://127.0.0.1:8765 --workspace /absolute/model-project
```

A preview returns `plan` and `approval`. To apply it, repeat the **same arguments**
with `configure` instead of `preview-config` and add `--approval <digest>`.
Nothing is applied if the config, interpreter/module path, URL or other approved
plan detail changes. `--config /absolute/file` is available for an explicit
client-supported destination; an arbitrary file is not automatically loaded by
that client. A Codex project-scoped config still requires a trusted project.

```text
python distribution/assistant_setup.py preview-install --client claude --ide-cli /absolute/runtime/bin/modelbuilder-ide.py
python distribution/assistant_setup.py doctor --client codex --python /absolute/service/python --service-url http://127.0.0.1:8765
```

For installation, repeat the same arguments with `install` and
`--approval <digest>` only after reviewing/accepting the preview and linked
terms. Optional `--extensions-dir`, `--user-data-dir`, and exact `--version`
arguments are included in the approved plan. The `.py` launcher runs with the
helper's Python, without shell interpolation. `.cmd`, `.bat`, `.ps1`, and `.sh`
wrappers are rejected: select the generated `.py` launcher or native CLI.

`doctor --probe-client` additionally starts an **already installed** vendor
transport in a disposable workspace and vendor configuration home. It strips
inherited vendor credentials and does not read the user's auth stores, account
history, other MCP servers or hooks. Codex's app-server lists MCP status; Claude's
`mcp list` checks the one disposable configuration. Neither creates a model turn
or calls a tool. `--client-binary` resolves ambiguous installed binaries; otherwise
manifest identity discovery under `--extensions-dir`, then `PATH`, is used.
There is no download fallback for a missing client.

## Safe edits and installation verification

Configuration previews reveal only the new Modelbuilder entry, never unrelated
settings. Apply is bound to the preview SHA256 and source-file SHA256. It uses a
cooperative exclusive lock, checks for concurrent changes immediately before
replacement, retains a timestamped backup, writes/fsyncs a temporary sibling,
and atomically replaces the file. Repeat identical configuration is a no-op.
On POSIX, new configs/backups are mode 0600; on Windows they inherit the selected
private directory's ACL. Keep the configuration directory private.

JSON retains unrelated entries semantically. TOML preserves untouched text and
checks full unrelated parsed content for equality. Conventional named/nested
server tables can be replaced; inline/dotted declarations and ambiguous layouts
that cannot be preserved safely are rejected without changing the source. Use
the client's settings UI for these layouts. Malformed JSON/TOML, duplicate JSON
keys, symlinks, hardlinks, and oversized files are refused. The lock coordinates
Modelbuilder setup instances; external vendor processes do not honor it, so
close them before changing configuration. No general cross-process CAS is claimed.

Install previews resolve the selected **exact** version and native target from
the verified vendor namespace on Open VSX, including its published checksum.
Installation re-resolves the preview and refuses changed metadata. Downloaded
bytes must match that SHA256; the embedded VSIX publisher/name/version/target
must match too. Installation excludes optional extension-pack dependencies.
Afterward the IDE CLI must list that exact extension/version before success is
reported. Downloaded files are temporary, never added to product bundles.
Registry hashes establish consistency with the reviewed source; this is not an
independent binary-signature or supply-chain audit.

A missing artifact is reported as an **exact-version/channel gap**, never an
unsupported OS. No Microsoft Marketplace endpoint or missing-platform URL is
fabricated. No sandbox is weakened, third-party license inferred from CLI source
licensing, or successful authenticated agent call inferred from installation.
Activation on the exact fork, vendor sign-in and model-driven behavior remain
separate acceptance gates. See [the platform audit](ASSISTANT_COMPATIBILITY.md).

## Reproducing evidence

```text
.venv/bin/python -m pytest tests/assistant_setup -q
.venv/bin/python scripts/probe_optional_assistant.py --client codex --output evidence/assistant-setup/native-and-codex-discovery.json
.venv/bin/python scripts/probe_optional_assistant.py --client claude --output evidence/assistant-setup/native-and-claude-absence.json
```

On Windows use the installed environment's `Scripts/python.exe`. The probe starts
a temporary local service with a synthetic **read-only** token, disposable DB,
and no training backend. It runs readiness only, then removes the temporary
service/configuration and restores its process environment. It never installs a
missing client. As of 2026-10-09, the existing Linux x64 Codex CLI 0.159.2 and
native MCP enumerate 61 current tools. Claude is absent in this execution
environment. Exact IDE-extension installation/activation is historical evidence,
not rerun by this check; authenticated model-agent parity remains unverified.

## Official references checked 2026-10-09

- [Codex MCP configuration](https://developers.openai.com/codex/mcp): shared
  config, stdio command/args, `env` and `env_vars`.
- [Codex IDE](https://developers.openai.com/codex/ide) and
  [OpenAI terms](https://openai.com/policies/row-terms-of-use/).
- [Claude MCP configuration](https://code.claude.com/docs/en/mcp): scopes,
  `.mcp.json`, environment expansion and project approval.
- [Claude VS Code integration](https://code.claude.com/docs/en/vs-code): native
  extension/CLI configuration and the official Open VSX installation channel.
- [Claude legal and compliance](https://code.claude.com/docs/en/legal-and-compliance).
