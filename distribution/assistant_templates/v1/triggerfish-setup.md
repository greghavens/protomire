# Triggerfish preparation, inactive

This bundle is prepared for one ModelBuilder project. It does not install or
launch Triggerfish, load a skill into a running vendor, or configure a provider.
Vendor acceptance remains UNVERIFIED. No credentials are stored here.

Inspected upstream: greghavens/triggerfish default HEAD
571c42b9299ab3006ce96fb541b7f1a23988d3b5, observed 2026-10-13; latest published
release v0.7.19 (commit d2c7f06d80e5b22cbacb794a2067afd020c8e63a). Source audit is not acceptance of an installed release.

Supported upstream paths:
- TRIGGERFISH_DATA_DIR selects a data root; triggerfish.yaml lives there.
- mcp.servers is the preferred map (mcp_servers is the legacy fallback).
- Skills load from <data-root>/skills and <data-root>/workspaces/main/skills.
- Stdio uses command and args; classification must be explicitly chosen by the
  owner. Omitting it denies tools. enabled:false prevents startup.

mcp.fragment.yaml contains an inactive, credential-free supported configuration
fragment. It is deliberately not named triggerfish.yaml and is not a complete
provider config. The sibling skill-template directory is deliberately outside the supported
skill-discovery paths. It cannot be auto-loaded by ordinary upstream startup,
even if the owner already uses this directory as a data root. Moving its content
into <data-root>/skills requires a separate reviewed activation step. Shared ModelBuilder
workflow and unchanged operation schemas are retained in the skill references.
The skill classification default is PUBLIC upstream; the owner must review the
appropriate skill ceiling together with server classification before activation.

Activation remains blocked for automatic IDE use. Upstream hardcodes gateway and
CLI port 18789 (and Tidepool uses its own fixed port). TRIGGERFISH_DATA_DIR isolates
files, not ports or same-user OS credentials. There is no verified project/port
CLI override. Do not join another daemon, stop one, change a global config or
plugin, or invent a flag to bypass this constraint. A separately reviewed single
instance may be evaluated by the owner, with provider/account setup and exact
binary/version provenance; ModelBuilder does not automate that step.

The upstream stdio allowlist matches only basenames npx, node, python3, python,
deno and uvx. Current config startup drops allowedCommands, so python3.12 and
python.exe are blocked. Do not add unsupported config keys, rename an executable,
or substitute another interpreter to claim compatibility.

ModelBuilder's bridge uses the mandatory managed core, one-project read-only
credential and existing exact native owner review queue. It does not create
broader service permissions. A local vendor with an unrestricted same-user shell
can read local files; this transport is not a hostile same-user process sandbox.
Keep service/vendor secrets out of the project and use only supported native
secret handling. Do not commit private runtime/provider state into source control.

Rollback through ModelBuilder removes only these unchanged managed files. Owner
edits and any separately created vendor data or credentials are preserved.
