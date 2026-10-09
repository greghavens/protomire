# Modelbuilder Code OSS development distribution

This is a source-built, branded **development distribution**, with the permanent
model lifecycle feature installed as a built-in module. It is not a patched
Microsoft Visual Studio Code binary, an installer for Microsoft's Marketplace,
or a published third-party extension product. Production signing, automatic
updates, security maintenance and macOS/Windows packaging are not implemented.

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
- Only tracked `product.json` is changed. `apply_overlay.py` and
  `verify_build.py` reject other tracked upstream differences. Source notices
  remain intact. No core patches are allowed by this increment.
- No proprietary assistants, Pylance, Remote Development or downloaded upstream
  Marketplace built-ins are bundled. No Marketplace endpoint is configured.
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
