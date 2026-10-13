# Single-app installation status

The intended end-user install is one app:

- Linux: open the Modelbuilder AppImage
- macOS: drag Modelbuilder from its DMG into Applications
- Windows: run the Modelbuilder user installer

**These installers are not yet built or available.** The current checkout is a
source development build. Do not present its Python/bootstrap/service commands
as an end-user installation path. A successful unit test or package recipe does
not establish a working native installer.

The new package path embeds the operation service, its CPython environment,
mandatory MCP bridge, verified native dependency resources, Git and an initial
source bundle. Opening the packaged workbench obtains a private local core
session automatically. No user-installed Python, manually started HTTP server,
manual port selection, or pasted service token is part of that path.

The native feature owns a lease; multiple windows and MCP consumers share the
same authenticated loopback service. Closing one client releases only its lease.
The last client releases the service after a short grace period. Application
state remains in private app storage. Submitted backend jobs retain their
existing durable supervised lifetime and are not cancelled by closing the IDE.
A restart creates a new session credential. No daemon is registered at login,
no global shell profile is changed, and no listener binds off-host.

A selected project source folder is admitted only through the native owner flow:
choose an existing committed Git checkout, explicitly confirm source trust, then
bind the returned project-specific alias using the existing revisioned operation.
Cancelling does not register or trust anything. Registration stores no credential,
does not create Git repositories or commits, and preserves the checkout identity
across restarts. A symlink/replacement is rejected or loses execution trust.

MCP is a mandatory bundled interface. An optional coding assistant is a separate
choice. Each assistant starts its own stdio bridge for one selected project;
its short-lived credential is read-only and project-scoped. Tokens never go into
assistant config files. Assistant mutation/execution is deliberately unavailable
until service-enforced human approval grants exist. This is not equivalent to
the IDE's owner grant or to authenticated vendor-assistant acceptance.

Windows native local training remains unsupported by the current backend.
The Windows installer recipe and storage DACL implementation require real
Windows acceptance; an `.exe` wrapper does not make POSIX execution portable.
macOS Intel and Linux ARM32 also lack the pinned local backend dependency
profiles. A native installer must disclose those capabilities accurately.

# Release engineering (not end-user instructions)

`build_ide.py` remains the source of the pinned Code OSS build. The single-app
orchestrator reuses its `vscode-<platform>-<architecture>-min` task and native
application output; it does not build a second editor or fork more editor core.

1. `python distribution/single_app.py plan --target linux-x64` describes the
   native input/output paths and explicitly reports `not_built`.
2. `python distribution/single_app.py build-editor --target linux-x64` invokes
   the existing pinned native build/package driver.
3. `python distribution/build_runtime.py --inputs /build/reviewed-inputs.json
   --output /build/runtime` builds a self-contained runtime from locally cached,
   SHA256-reviewed CPython and Git archives, a source Git bundle and inert
   application wheels. It installs dependencies only from an offline wheelhouse,
   with the existing exact target hash-locks, `--require-hashes`, and
   `--only-binary=:all:`. It enforces the exact repository Python patch pin for both runtimes, keeps
   separate archive provenance, and moves the whole payload before interpreter
   and Git helper probes. External venv/system-Python/Git dependencies fail. It never installs on the end user's machine.
4. `python distribution/single_app.py stage --app /build/VSCode-linux-x64
   --runtime /build/runtime --output /build/staged --target linux-x64` embeds the
   sealed runtime in the permanent native feature. Missing integration, missing
   resources, extra payload files, changed hashes and wrong targets fail closed.
5. `python distribution/single_app.py package --app /build/staged
   --output /build/artifacts --target linux-x64 --appimagetool /tools/appimagetool
   --appimagetool-sha256 REVIEWED_HASH` creates an unsigned private AppImage using
   a separately reviewed tool. macOS invokes `hdiutil create` and `hdiutil verify`.
   Windows invokes the exact pinned upstream `build/win32/code.iss` with its
   pinned Inno compiler and Modelbuilder-specific identity, as a user installer.

Every actual build runs on its native host. A passed moved-payload probe is not proof that every dynamic library or optional
subprocess can run on a clean machine. Native dependency closure stays an explicit
release gate. Artifact names derive the application version from the verified wheel
and product metadata, independently of the upstream editor version.

Native packaging and a clean-machine
launch/close/restart/persistence/MCP smoke test are separate gates. Mac native
packaging has not been run here; Windows has not been run here. Linux's existing
upstream npm-cache/network blocker and missing reviewed relocatable runtime
inputs still prevent an actual artifact in this environment.

Builder input JSON uses these fields (paths are relative to the JSON file):

- `target`, `source_commit`, `wheelhouse`, `notices`
- `source_bundle`: `{ "path": "source.bundle", "sha256": "..." }`
- `service`: `python` archive description and application `wheel` description
- `backend`: the same pair for targets with a pinned local backend profile
- Optional `mlx` on Apple Silicon: a separate Python archive, installed from the
  frozen MLX hash-lock. It never modifies the service/backend environment.
- `git`: archive description for self-contained native Git
- Each archive description includes `path`, `sha256`, `source_url`, `executable`
  (relative to the extracted archive), and `notice` (relative to runtime output,
  for example `notices/CPython-LICENSE.txt`). Python archives also declare
  `version`, matching `.python-version` exactly. macOS archives also declare
  `minimum_os`; the builder computes mandatory service/backend minima independently and stamps
  that base floor in Info.plist. Optional MLX keeps its own minimum OS and is
  not configured or imported on older hosts. The reviewed MLX metal profile
  needs macOS 26; that does not raise the base app minimum to macOS 26.
- Each wheel description includes `path` and `sha256`

Only reviewed official artifact sources belong in those provenance fields.
Git is not redistributed under the product's own license: preserve its notices
and satisfy its applicable corresponding-source/source-offer obligations before
any distribution. Python and every bundled dependency need their own preserved
license records. Recorded URLs/hashes alone are not a legal compliance review.
The product license is undecided. No signed release, public release, updater,
hosted build or public artifact upload is configured by these scripts.

For existing source-checkout development, the previous manual service connection
remains available. `MODELBUILDER_DEVELOPMENT=1` is an explicit launcher-level
escape hatch for engineers; it is not an end-user first-run requirement.
