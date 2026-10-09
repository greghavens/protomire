# Modelbuilder Code OSS development distribution

This is a source-built, branded **development distribution**, with the permanent
model lifecycle feature installed as a built-in module. It is not a Microsoft
Visual Studio Code binary, an installer for Microsoft's Marketplace,
or a published third-party extension product. Production signing, automatic
updates, security maintenance and macOS/Windows packaging are not implemented.
The current product has one documented composition import for its permanent
native File-menu contribution; see [the exact exception](FILE_MENU_EXCEPTION.md).
The original zero-core-patch build is historical evidence, not the current count.

## Reproduce (Linux x64)

1. Install upstream native prerequisites from your operating system's official
   repositories: a C/C++ compiler, make, pkg-config, Python, X11/xkbfile, libsecret
   and Kerberos development packages; GTK3/NSS/GBM/ALSA runtime libraries.
2. Run `./scripts/build_ide.sh build`. It downloads the checksum-pinned Node
   22.19.0 toolchain locally, verifies Code OSS tag **1.105.0**, commit
   `03c265b1adee71ac88f833e065f7bb956b60550a`, preserves upstream lockfiles and
   invokes `npm ci`, `npm run compile` and `npm run electron`. The feature is
   compiled separately, then copied into `extensions/modelbuilder-workbench`.
3. Run `.runtime/bin/modelbuilder-ide /path/to/your/project` from a desktop
   terminal. This starts the branded upstream Electron development distribution.
4. `python distribution/install_extensions.py` verifies pinned VSIX SHA256s and
   installs Python, Jupyter, basedpyright and Python Debugger from Open VSX. The
   upstream `--do-not-include-pack-dependencies` flag prevents automatically
   installing Python's optional proprietary Pylance package. Automatic updates
   must stay off in acceptance profiles to preserve the tested versions.
5. Install `ipykernel==6.30.1` into a Python environment and run
   `MODELBUILDER_GUI_PYTHON=/absolute/path/to/python ./scripts/smoke_ide.sh` from
   the desktop. This runs real Extension Host tests and retains results under
   `evidence/gui/distribution/`. Notebook kernel selection/workspace trust can
   require the regular IDE UI on a new profile.
6. Run `./scripts/smoke_ide.sh --workbench` for the real graph/source/external-MCP
   parity and conflict-recovery suite. After `./scripts/bootstrap.sh --backend`,
   `./scripts/smoke_ide.sh --execution` submits the shipping pinned CPU recipe
   through the actual Runs UI and its normal confirmation. It records the real
   backend UUID, reconciled state, logs, metrics, model version and artifact IDs.

`build_ide.sh prepare` verifies/downloads the source+Node and applies metadata
without compiling. `compile` reuses an existing dependency installation.
`package` additionally attempts upstream `vscode-linux-x64-min`; a development
build passing does not assert that separate production packaging passed.

On this Debian environment missing development headers were supplied without
mutating the OS by `distribution/prepare_native_sysroot.sh`. It downloads signed
official packages to `.cache/apt`, extracts only into `.cache/sysroot`, records
package hashes, and uses pkg-config's supported sysroot facility. This is a
build prerequisite accommodation, not a change to Electron's security sandbox.
Hosts with installed prerequisites do not need it. Native package versions are
recorded in evidence; their general helper follows the host's official APT
repositories and is not an independently pinned portable OS image.

## Integrity and boundaries

- `code_oss_pin.json` fixes source, Node/npm and Electron versions. This is a
  known stable API baseline, not the latest Code OSS security release.
- Tracked upstream changes are `product.json` plus exactly one import in
  `workbench.common.main.ts`; one product File-menu module is added.
  `apply_overlay.py` and `verify_build.py` enforce exact bytes against the pinned
  upstream composition baseline and reject other source changes. Source notices
  remain intact. This is the documented File-menu blocker exception, not a
  general permission to modify editor internals.
- No proprietary assistants, Pylance, Remote Development or downloaded upstream
  Marketplace built-ins are bundled. No Marketplace endpoint is configured.
- The permanent built-in module contributes `chat.disableAIFeatures=true`,
  `workbench.startupEditor=none` and
  `workbench.secondarySideBar.defaultVisibility=hidden`. Activation closes the
  auxiliary bar using a supported command when the no-AI default is enabled.
  This is the product default, not a screenshot-only manual change.
- The operation service, not the extension, owns all business state. Closing
  the IDE does not stop the native MCP service or change resource identity.
- Node 22.19.0 is the upstream build toolchain. The repository's service and
  workbench CI uses its own separately pinned Node/Python versions.
- `tests/gui/test_distribution.py` checks packaging policy only. Its success
  is **not** a GUI acceptance pass. Only actual `smoke.cjs` results and captured
  GUI observations establish runtime compatibility.
- No script disables Electron sandboxing, Linux user namespace policy, TLS
  verification, workspace trust, or OS security. Such host restrictions are
  reported as blockers.

## Extension licensing

The lock manifest records each exact Open VSX artifact URL, version, checksum,
declared license and separately downloaded license hash. License texts are
retained beside it; archive-contained notices remain in installed VSIX files.
Python and Jupyter are MIT components; this does not grant rights to Pylance or
every extension recommended by their extension packs. Debugpy is explicitly
pinned to **linux-x64** rather than accepting a registry default platform.
basedpyright's official guide supports Open VSX and non-Microsoft forks.

Codex CLI source licensing must not be applied to the Codex IDE binary.
Claude's published product-preinstallation conditions are not blanket rights
to redistribute a modified assistant. User installation and authenticated
exact-fork MCP verification are separate gates, never inferred from a generic
MCP client test. No paid inference or new authentication is performed here.

## Observed acceptance (2026-10-09)

The source build compiled with zero errors and launched on the supported cloud
desktop with Electron sandboxing intact. The final real editor smoke passed all
eight checks, including actual basedpyright diagnostics, integrated terminal,
Git, a debugpy breakpoint/variable evaluation (`answer=42`), and Jupyter execution
(`42`). `evidence/gui/distribution/smoke-results.json` is authoritative; screenshots
now capture the actual Electron workbench rather than an unrelated foreground
terminal. Initial attempts and the interrupted repeat are retained separately.
The original workbench passed graph/MCP checks and GUI-submitted ZenML CPU
execution, but its multi-model/recipe-selector UX was subsequently rejected.
`evidence/gui/workbench`, `final-caption`, and `execution` are historical proof,
not acceptance of the corrected one-project/one-model/one-recipe design.
Corrected landing, fixed-workspace and execution results are recorded separately
under `evidence/gui/single-model` and `evidence/gui/single-model-execution`.
The later `evidence/gui/single-model-roundtrip` suite passes 17 actual DOM checks
plus native Monaco saving, including the full five-node recipe import/export,
fixed identity, raw bundle preservation and tamper rejection. The initial failed
numeric round-trip probe remains recorded under `single-model-import`.
Portable bundles must use `recipe.export` with `format=json` and pass its
`bundle_json` string unchanged to import. The integrity algorithm is unchanged;
parsing and reserializing bundle contents in JavaScript can alter number syntax.
Corrected-domain training is a separate gate: consult the terminal status in
`single-model-execution`, not historical execution screenshots. The
manual `gui.yml` workflow reproduces these suites on a sandbox-capable Xvfb host;
local success does not assert that the remote workflow has run.

The full initial `npm ci` was interrupted by a registry HTTP 403 late in upstream
`test/mcp` dependencies. The missing final self-host extension dependencies were
installed separately before successful compilation. A pristine unattended
bootstrap and the separate production packaging target remain unverified.

Separate user-installed, checksum-verified Codex 26.5908.31748 and Claude Code
2.1.295 VSIXs activated on the exact fork and displayed normal account sign-in
screens. Their own bundled transports were probed with an isolated disposable
local service: Claude reported MCP Connected; Codex enumerated all 48 native MCP
tools. These artifacts are not bundled or redistributed by this product.
Authenticated model-agent CRUD/run/error/permission parity remains blocked on
vendor authentication/inference authorization. No credentials or paid inference
were used. See `assistant-activation-results.json`, `assistant-install-results.json`
and `assistant-mcp-discovery.json` for the precise evidence and limits.

The vendor installers presented no explicit acceptance prompt. Codex's archive
license links [OpenAI Terms of Use](https://openai.com/policies/row-terms-of-use/),
which apply to its use. Claude's license links its
[legal and compliance terms](https://code.claude.com/docs/en/legal-and-compliance).
This installation/use does not establish redistribution rights. Compatibility
warnings include Codex's unavailable `languageModelProxy` proposal and Claude's
unsupported secondary-sidebar proposal on Code OSS 1.105.0; editor tabs still
rendered. No API/security bypass or vendor binary modification was used.

The cloud desktop does not provide a working OS encryption keyring; the IDE
displayed that warning during ephemeral test-token storage. The production
credential-storage deployment gate is therefore not established by these tests.

Primary sources checked for this increment:
- https://github.com/microsoft/vscode/wiki/How-to-Contribute
- https://github.com/microsoft/vscode/tree/03c265b1adee71ac88f833e065f7bb956b60550a
- https://open-vsx.org/extension/ms-python/python
- https://open-vsx.org/extension/ms-toolsai/jupyter
- https://open-vsx.org/extension/ms-python/debugpy
- https://docs.basedpyright.com/latest/installation/ides/
- https://learn.chatgpt.com/docs/codex/ide
- https://code.claude.com/docs/en/vs-code
- https://code.claude.com/docs/en/legal-and-compliance
