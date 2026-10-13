# Single-app installation status

The intended end-user install is one app:

- Linux: open the Modelbuilder AppImage
- macOS: drag Modelbuilder from its DMG into Applications
- Windows: run the Modelbuilder user installer

**An unsigned macOS arm64 development DMG has been built and tested on the
Mac build host. There is no published, signed, clean-machine-accepted release.**
That artifact pins runtime `2e60975`; it does not contain later source changes.
Its mounted payload passed strict integrity, CLI/import-location and five managed
service/MCP lifecycle checks. Bundled MPS and MLX checks passed separately.
Native visual testing, signing/notarization and clean-machine acceptance remain
open. The complete evidence archive is preserved on the build host; its latest
Library upload is blocked. Linux AppImage and Windows installers are not built.
See [current verification](../docs/CURRENT_VERIFICATION.md) for exact scope.
Do not present development Python/bootstrap/service commands as an end-user
installation path. Unit tests or a package recipe alone are not installer proof.

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
choose a committed Git checkout or imported source snapshot, explicitly confirm source trust, then
bind the returned project-specific alias using the existing revisioned operation.
Cancelling does not register or trust anything. Registration stores no credential,
does not create Git repositories or commits, and preserves the checkout identity
across restarts. A symlink/replacement is rejected or loses execution trust.

MCP is a mandatory bundled interface. An optional coding assistant is a separate
choice. Each assistant starts its own stdio bridge for one selected project;
its short-lived credential is read-only and project-scoped. Tokens never go into
assistant config files. Mutation/execution requests enter an exact-request queue.
The native owner reviews the project, requester, payload and authoritative linked
material, then approves or denies that one request. Approval uses an internal,
one-use Actor capability; it never upgrades the assistant bearer. Changed project
state, changed review material, expiry, withdrawal and replay fail closed. A
bounded asynchronous Hub capability remains revocable until that exact transfer
ends. One native publication review authorizes a checked internal approval and
start; approval-only operations never imply an upload. Already submitted native
jobs require their ordinary cancellation operation, and transmitted data cannot
be assumed undone. Restart never automatically replays an uncertain operation.

This increment does not grant standing recurring-watch authority. That requires
a separately reviewed policy; a one-shot bridge request fails closed. Vendor
assistant authentication and end-to-end paid model acceptance remain separate.
See [exact request review](REVIEWED_REQUESTS.md) for lifetime and audit details.

These controls constrain requests through the managed MCP/API bridge. They do
not sandbox an installed coding assistant with unrestricted same-user shell or
filesystem access. Owner-only file modes protect against other OS users, not
another process running under the same user ID. Vendor trust and permissions
remain part of the deployment boundary.

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
- For a shallow source repository, `source_bundle_shallow`:
  `{ "path": "source.bundle.shallow", "sha256": "..." }`. Copy the source
  repository's `git rev-parse --git-path shallow` file alongside the bundle;
  do not drop shallow boundaries or fetch/repair history during assembly.
  The sidecar is hash-checked and included in the sealed inventory. The moved
  payload probe actually restores the bundle, checks every real ref and the
  selected commit, and runs `git fsck --full`. First launch uses the same restore
  path; later launches never reset an existing user's checkout.
- `service`: `python` archive description and application `wheel` description
- `backend`: the same pair for targets with a pinned local backend profile
- `hub`: a separate Python archive (no application wheel), required on Linux,
  macOS and Windows x64/arm64. It installs the complete exact target Hub/keyring
  hash lock from the offline wheelhouse. Linux armhf has no reviewed Hub binary
  profile and reports Hub unavailable; there is no architecture fallback.
- Optional `mlx` on Apple Silicon: a separate Python archive, installed from the
  frozen MLX hash-lock. It never modifies the service/backend environment.
- `git`: archive description for self-contained native Git
- Each archive description includes `path`, `sha256`, `source_url`, `executable`
  (relative to the extracted archive), and `notice` (relative to runtime output,
  for example `notices/CPython-LICENSE.txt`). Python archives also declare
  `version`, matching `.python-version` exactly. macOS archives also declare
  `minimum_os`; the builder computes mandatory service/backend/Hub minima independently and stamps
  that base floor in Info.plist. Optional MLX keeps its own minimum OS and is
  not configured or imported on older hosts. The reviewed MLX metal profile
  needs macOS 26; that does not raise the base app minimum to macOS 26.
- Each wheel description includes `path` and `sha256`

Build native application wheels with the existing inert replay builder's explicit
`--target`, using an exported/imported verified source snapshot, for example:

```sh
python scripts/replay_bundle.py wheel /build/verified-source --kind backend \
  --target darwin-arm64 --destination /build/wheels/darwin-arm64
python scripts/replay_bundle.py wheel /build/verified-source --kind service \
  --target darwin-arm64 --destination /build/wheels/darwin-arm64
```

The selected `requirements/profiles/<component>-<target>.lock` must be part of
that snapshot's SHA256 inventory. Metadata keeps the declared direct dependency
names and extras while taking exact versions from that native lock: Apple
Silicon's torch is `2.9.0`; the Linux CPU profile uses `2.9.0+cpu`. The wheel
contains a `modelbuilder-profile.json` receipt with target, variant, lock path,
lock hash and resolved requirements. Keep different targets in separate output
directories. `--backend-variant cuda-cu128` selects an existing reviewed CUDA
profile for standalone backend wheels; the bundled runtime builder still uses
its existing default target locks. Omitting `--target` deliberately preserves
the recipe's exact replay environment and is not native dependency selection.
This inert metadata operation does not establish Mac native acceptance; assembly
still runs the target lock install, `pip check`, and moved-payload probes on Mac.

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


### Native Hub settings

The managed application wires its bundled Hub worker automatically. Public Hub
inspection needs no account. Use **ModelBuilder: Configure project Hub account
and publication folder** from the command palette or the Hub sidebar link.
Select a native publication folder or connect an account with a native password
input. The confirmation explains persistent OS-keyring storage, project scope,
and exact folder identity. Only supported OS-backed keyrings are accepted;
missing, locked or plaintext-fallback stores block setup. Linking an existing
keyring service/username reference is also supported.

A token goes directly from the native host to a one-shot bundled keyring helper
on stdin, never through MCP, HTTP, command arguments, recipes, logs or the
settings database. The app stores only an opaque account reference and a
canonical folder's device/inode identity in private local app state. Each newly
stored credential gets a fresh lookup name. Token validity is not checked with
Hugging Face during setup. A failed or uncertain keyring write is not linked;
if storage succeeds but settings fail, the app reports the partial outcome.
Disconnecting removes the project's app reference, not the OS-keyring item;
the confirmation explains how to remove that retained item in the OS manager.

Changing settings invalidates existing project publication previews and
approvals and requests cancellation of active transfers. It cannot undo files
already transmitted. Exact publication review and service-side grants remain
required. Directory replacement or symlink redirection requires another native
folder review. These controls constrain the managed bridge, not other programs
with unrestricted access under the same OS user.

The Hub lock, interpreter provenance and helper files are sealed into the
runtime inventory. Sealing physically moves the payload, checks the exact full
installed Hub/keyring closure, then launches the actual SDK worker for a local
card inspection with network/implicit authentication disabled. This is separate
from native clean-machine installer acceptance, OS-keyring acceptance and real
provider acceptance, which remain required platform-specific gates.

Native Hub settings also expose a project-scoped per-transfer size limit. The
conservative default remains 16 GiB; explicit native review may select an exact
integer limit up to 1 PiB, using bytes or whole binary units such as `64 GiB` or
`2 TiB`. Another project's limit is unchanged. Raising this ceiling allocates
no disk space and authorizes no transfer. Every operation still checks its exact
planned bytes and available/reserved space at start and retry. Lowering or
changing the setting invalidates old publication previews/approvals and requests
cancellation of active project transfers, with the same reconciliation caveat as
account changes.

### Bundled Trainer and immutable runtime files

Supported packaged backends (Linux x64/arm64 and macOS arm64) include the
reviewed `trainer.lock` overlay in addition to the complete native backend lock.
The builder installs it from the hash-verified offline wheelhouse. Sealing moves
the runtime and verifies both lock identities, exact installed versions and
package locations, including `accelerate` and Transformers Trainer imports.
Other targets report the backend/Trainer capability as unsupported. Recipes can
select the corresponding reviewed environment profile, which records both the
base `lock_file` and `trainer_profile` in source and replay identity. Import and
metadata checks do not substitute for native bundled Trainer/MPS acceptance.

Managed Python launches disable bytecode with `-B` (including isolated `-I`
launches, which ignore Python environment variables). Service/backend mutable
cache and temporary files use private app state outside the sealed payload.
Strict verification rejects added executable or bytecode files; it never makes
exceptions for arbitrary unsealed caches. Native acceptance drivers must also
use `-B` when running the bundled interpreters. The mandatory Hub profile counts
toward the base macOS minimum; the optional MLX minimum remains independent.
