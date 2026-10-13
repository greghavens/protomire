# Model and dataset Hub workflow

## Scope and boundaries

`hub.*` is part of the existing authoritative operation catalog. HTTP, native MCP,
VS Code SCM, the repository view, and native dialogs use the same operations.
A project still has one model and one current recipe. Downloading files never
selects a base model or changes an initializer. Scratch initialization remains
available. `hub.asset.bind` explicitly adds inert hash-pinned resources to that
same recipe; a compatible trusted component must consume them. Downloaded code,
pickle files, dataset scripts and remote custom code are never executed.

The first production adapter is `huggingface`. Other providers must explicitly
implement `HubProvider`, including repository validation and immutable revision
validation. The common service does not force other providers to use HF's
owner/name IDs or Git SHA-1 commits. No generic unknown-provider escape is enabled.
HF-specific revision/identifier constraints are conditional in
`schemas/hub-asset.v1.schema.json`.

## Isolated runtime and credentials

Operator configuration accepts a separate `hub` object:

- `root`: owned service asset/snapshot directory; defaults beside the database
- `python_executable`: interpreter in a dedicated reviewed Hub environment
- `accounts`: opaque aliases mapped to `keyring_service`, `username`, and optional
  permitted `project_ids`; never credential values
- `publication_roots`: `{project_id: {source_alias: approved_directory}}`
- `max_bytes`: artifact byte cap; unchanged 16 GiB default, explicitly configurable
  up to 1 PiB (2**50 bytes, exactly representable in native JSON/JavaScript)
- `max_concurrent`: one through four workers, default two per service process

The version-specific API boundary uses `huggingface-hub==0.36.2`. Its dependency
input is `requirements/hub.in`, separate from service/backend lock profiles.
Complete production/test locks are available for six native targets; see
[Hub runtime profiles](HUB_RUNTIME_PROFILES.md) for separate environment setup,
hash enforcement and the required socket-blocked SDK CI route.
Authenticated operations additionally need `keyring==25.6.0` and a working secure
OS keyring. The worker only allows native macOS, Windows, Secret Service or KWallet
backends, rejecting plaintext/fallback stores. Provisioning or changing these
credentials is separate explicit secure setup. `modelbuilder.configureHub` uses
a native password input and explicit persistent-storage confirmation, then sends
the entered value only on stdin to the bundled credential helper. Locked, absent
or non-OS-backed stores hard-block; there is no plaintext fallback. Uncertain
writes are reported without linking the account. No login command or automatic
account creation is provided. This native setup is not exposed to the assistant
bearer, MCP, recipes or webviews.
The subprocess receives only the alias's lookup coordinates; the token remains
inside the isolated worker. Anonymous access explicitly uses `token=False`.
Inherited HF tokens, implicit login files and endpoint settings are disabled.
SDK stdout/stderr are not exposed and upstream exception text is never returned.

The SDK owns HTTP, resumable HTTP retries, repository commits and upload protocols.
It runs via an explicit interpreter and Python `-I`, not by importing the training
or service environment. HTTP requests and responses have bounded time/byte limits,
TLS stays enabled, Xet is disabled for a reviewable requests-based byte boundary,
and process deadlines/cancellation terminate the worker. No vendor compute API is
called. No real token, login, model-weight download, publication, repository
creation or charge was exercised during this implementation.

## Download lifecycle

1. Search model/dataset metadata and inspect a repository. HF resolves a branch
   or tag to a 40-hex immutable commit and lists bounded exact sizes and LFS
   SHA256 or Git blob SHA-1 identities. Private/missing and gated-access states
   are explicit. Gated terms must be resolved on the provider website.
2. Create a durable exact-file plan with a byte budget, captured license evidence
   and a current separately reviewed source/context policy. Starting requires
   the exact plan digest; a caller acknowledgement alone cannot grant rights.
3. Each file uses a newly and exclusively created 0700 SDK attempt directory.
   The SDK boundary rejects nonempty, linked, reparse or special-file staging.
   It never reuses a caller-provided SDK cache. Streamed bytes are bounded; final
   data is copied through safe handles, hashed, fsynced and atomically installed.
   Only approved names appear in the final asset directory, without SDK metadata
   or symlinks. Peak storage includes a temporary copy plus verified bytes;
   admission reserves up to twice the planned bytes plus bounded metadata space.
   OS-fenced reservations coordinate copies across processes sharing the Hub root
   and release on completion/cancel/failure; dead-process reservations are reclaimed.
   Free disk is rechecked before every unfinished file. These are conservative
   reservations, not filesystem quotas against other applications.
4. SQLite tracks file-level progress, attempts and cancellation. No asset is
   registered until every selected file verifies. OS locks fence duplicate
   workers and distinguish a live transfer from an interrupted service process.
5. Explicit retry preserves and rechecks completed verified files. Interrupted
   partial files restart in fresh SDK attempts; durable cross-process resume is
   file-level, not a claim of byte-level checkpointing. SDK range retry inside
   an active attempt remains available. This also prevents poisoned SDK cache
   metadata from forcing repeated hash failures with no new HTTP request.

Dataset assets expose a project-scoped root alias to the existing dataset
manifest/registration flow. Splits, row schema and transformation provenance must
still be explicitly declared; a Hub download is not proof of training consumption.
Resource descriptors resolve through a request-local backend dependency view and
are reverified before execution, preserving existing source-pin admission.

## Publication lifecycle and authority

- `hub.repository.compare` compares approved local files to one immutable remote
  revision, reporting added, modified, unchanged and remote-only files. It never
  deletes files. A SCM staged selection is local UI state only.
- `hub.publish.preview` snapshots exactly selected files and returns hashes,
  destination/provider/type, visibility, license, parent revision, additions/
  replacements/unchanged entries, no-deletion semantics and creation effects.
  Staged workflows send the inspected remote revision, and a moved revision
  requires refreshing the comparison before previewing again.
- A new repository requires a selected README with matching license metadata.
  Existing repositories can retain the exact current card license without
  reuploading README. Selected README metadata is parsed locally by a safe YAML
  loader rejecting duplicate keys, and agrees with the actual pinned HF ModelCard
  parser. Quoted duplicate keys and multiline duplicate licenses are rejected.
- `hub.publish.approve` requires the separate `publish` scope. Ordinary assistant
  read/modify/execute grants cannot mint approval receipts. The receipt binds
  project, publisher actor, exact payload digest and preview, expires in 15
  minutes, and is consumed once. It proves an authorized publisher grant, not
  independent proof that a human reviewed it.
- `hub.publish.start` additionally requires `execute`; current grant authority is
  rechecked before dispatch. Frozen files and destination visibility/parent are
  rechecked. The SDK uses `parent_commit`; no automatic visibility changes,
  deletion, account creation or force push occurs. Explicit new-repository
  creation is included in the approved payload.
- Completion verifies exact resulting immutable commit, destination visibility,
  remote card license and selected file hashes/sizes. Cancellation or an uncertain
  error after transmission produces `needs_reconciliation`, never a false
  cancelled/successful result. Reconciliation reads the unique commit marker and
  verifies that commit; it never submits again. A stopped publication cannot use
  the download retry operation.
- Failed previews remove derived snapshots immediately. Cancelled native preview
  dialogs discard their snapshots. Unused previews expire after 24 hours and old
  derived snapshots are removed when new previews are prepared. Receipts/history
  remain, as do the original source files. A crash between snapshot creation and
  registration can leave an orphan private directory; no automatic broad directory
  purge is implemented.

Provider visibility is an externally mutable repository setting. Its other owners
may change it independently; this adapter does not promise to lock those owners
out. A verification mismatch is reported rather than claiming the approved state.

## Native Git/HF layout and integration

Source Git keeps code, tests, recipe/history references, research and small
manifests. Large training data, Parquet files, models and checkpoints stay under
ignored artifact directories or Hub repositories. `hub.asset.manifest` and
`hub.publish.receipt` return immutable credential-free JSON references suitable
for `manifests/models/` or `manifests/datasets/`, with canonical file hashes.
No operation silently adds those bytes to source Git or commits/pushes Git.

`HubSourceControl(projectId)` implements a native VS Code Source Control provider
with working and staged resource groups. `HubRepositoryTree` supplies the companion
repository/revision/remote-file view. `openHubWorkflow(client, projectId, guard)`
returns an inspected repository comparison for `setComparison(result)`.
`stage(path)` and `unstage(path)` update only UI staging.
`publishStagedHubFiles(client, projectId, scm.stagedSelection(), guard)` opens the
exact preview, then the publisher approval confirmation. It never publishes on
save, file change, repository refresh or staging. Commands require a guard that
rejects project/connection changes while a native dialog is open.

The workbench registers these commands and the companion repository view:

- `modelbuilder.openHub` opens the workflow and projects comparisons into SCM/view
- `modelbuilder.hubStage`, `modelbuilder.hubUnstage` stage or unstage a relative path
- `modelbuilder.hubPublishStaged` opens the exact selected-file publication flow
- `modelbuilder.hubRepository` shows the selected repository, revision and files
- project/connection changes clear and dispose prior SCM selection and tree state

The host instantiates `HubController` and guards project/connection identity before
and after dialogs. Managed owner approval rechecks that guard immediately before
dispatch. Repeated publication clicks cannot mint overlapping approvals.

This is a Hub repository/transfer workflow, not broad Git SCM equivalence: it does
not implement branches/merge/rebase, remote deletion, automatic pulls, or conflict
resolution beyond immutable-parent rejection and explicit refresh.

## Security boundary and outstanding acceptance

Service-owned generated directories are private and never accepted from API callers;
SDK attempts are empty, exclusive and short-lived. Existing links and special files
are rejected, final imports use no-follow safe handles, and the supported threat
boundary covers untrusted repository paths, files and API callers. The SDK itself
uses path-based filesystem APIs. This is not an OS sandbox against a malicious
process with the same service OS identity that can discover and replace private
paths during a transfer. Strong isolation from a compromised service identity
requires an OS sandbox/separate identity. Post-download hash checks are not claimed
to prevent arbitrary same-identity concurrent writes inside SDK internals.

Focused deterministic tests exercise actual SDK API calls via fake HTTP and tiny
files, plus service/API/MCP and native UI/SCM mocks. They do not establish real
provider acceptance, secure keyring availability, production authenticated upload,
or native Mac/Windows UI acceptance. Independent security review was interrupted
and is incomplete. Full worker and test hash-locks now include keyring and all
platform dependencies. Six target wheel closures have verified hashes and
dependency metadata; only Linux x64 has fresh offline installation and actual-SDK
execution evidence. No service/backend environment is expanded by the Hub worker.
The combined application still requires integrated regression, native target
acceptance and a deliberately refreshed runtime/source identity.

Pinned upstream API references:
- https://huggingface.co/docs/huggingface_hub/v0.36.0/en/package_reference/hf_api
- https://huggingface.co/docs/huggingface_hub/v0.36.0/en/guides/download
- https://github.com/huggingface/huggingface_hub/tree/v0.36.2

## License evidence and declared-input rights

Download is a reviewed acquisition, not permission inferred from a label. First
capture immutable evidence with `hub.license.inspect`, or use
`hub.license.local.inspect` on exact files from an approved local alias. Evidence
retains the full captured UTF-8 card/license/notice text, source URLs, file hashes,
upstream commit and card metadata. At most 32 documents, 1 MiB each and 4 MiB total
are captured. Unknown, custom/unrecognized and ambiguous labels remain distinct.
Uncaptured external terms or non-UTF-8/oversized terms need a separately reviewed
intake extension; they are not silently accepted. A reviewer must explicitly name
which captured documents contain the applicable terms. File naming and labels do
not establish legal meaning.

`hub.license.review` requires a separately authorized `publish` grant. It records
an exact source/evidence-bound decision with operational `permitted_uses`, actual
term document paths, reviewer rationale, preserved file hashes, exact attribution
text, allowed destination licenses and independent gated-terms acknowledgment.
The policy's structured `reviewed_usage_context` must exactly match the recipe's
`extensions.usage_context`: `{purpose, commercial, deployment, conditions}`.
No goal prose, license identifier, access token or account access is interpreted
as permission. This is a recorded authorized decision, not legal advice or proof
of authorship/ownership. The native editor opens evidence and an editable review
JSON; empty context and missing actual term paths cannot authorize acquisition.

The service resolves current project policy rows internally; API/recipe policy
maps are not authority. Direct registered model/dataset/checkpoint bytes must
belong to the policy's captured source file closure. For ordinary Git files that
initially expose Git blob hashes, successful verified acquisition adds SHA256
materialization evidence under a new policy revision without changing permissions.
Native dispatch must recheck policy revision/revocation using the runner's captured
service store guard. Policy revocation preserves immutable history and blocks
future admission. All registered inputs are governed, including local ones. Pure
scratch/utility work with no applicable declared asset inputs records that narrow
fact, not a fictional upstream rights approval. Trusted source can perform
undeclared I/O; this gate is not an OS/network sandbox.

`hub.local.register` snapshots reviewed local files into the verified asset store
without assuming copyright ownership. Dataset bindings derived from exact verified
asset roots preserve all input rights; unknown sources remain unresolved. Export
and publication must retain multi-parent lineage, satisfy the reviewed notice and
attribution requirements, and match the intended usage context. Publication also
checks each upstream policy's allowed destination licenses. Target card metadata
alone is insufficient. The entire rights/policy/admission/context disclosure is
bound into the exact publication approval.

Managed native integration injects a `HubReview` callback. License review/revocation
use exact one-shot native grants; publication uses `desktop.publish.reviewed` for
one native disclosure and compound approve/start. The ordinary assistant bearer
never receives broad publisher scope. External service connections retain the
explicit scope-separated HTTP/MCP contract.

## Host and configuration handoff

`HubController` owns per-project SCM/tree lifecycle, result projection, stale
project/connection/revision rejection, stage/unstage and publication. The workbench
connects it to project open/close/switch and the New Project Hub action. Managed
review reuses the existing `nativeReviews` controller and its one-shot grants.

Service operator `hub` configuration keys are `root`, `python_executable`,
`accounts`, `publication_roots`, `max_bytes` and `max_concurrent` (1–4). Account
entries contain only keyring lookup names and optional project IDs. Complete
separate Hub runtime locks are provided; secure account setup and actual native
keyring availability remain acceptance gates. Empty account/publication-root
configuration stays fail-closed. Public Hub metadata access needs no account.

The managed distribution seals a dedicated `hub_python` with the exact native
profile and probes its relocated package closure and an offline SDK card operation.
`modelbuilder.configureHub` binds project-specific account lookup references and
canonical publication-folder identity in private `desktop_hub_settings` records
inside the managed state’s `modelbuilder.sqlite3`. The owner-only desktop endpoints
never accept a caller-selected interpreter. Ambient operator settings cannot
replace the sealed worker. Settings changes invalidate pending previews/approvals
and request cancellation; staging does not grant access to a new folder or account.
Credential setup uses fresh project-specific keyring lookup names and does not
validate the token with the provider. Disconnecting a project reference does not
remove the separately stored OS-keyring item.

No token creation, login, real credential storage, vendor weight transfer or
publication was performed as implementation acceptance. The setup and failure
paths are tested with deterministic boundaries; actual secure OS-keyring and
provider acceptance remain pending.

## Large transfer budgets

An authorized operator can raise `hub.max_bytes`; the managed desktop records a
separately reviewed project-specific cap. Existing settings retain the 16 GiB
default. Neither setting starts a transfer. Every download plan and publication
preview still binds exact files, total bytes and its own explicit byte ceiling;
start/retry rechecks the current project cap. Lowering a managed cap invalidates
pending reviews and requests cancellation. Values must be exact positive integers
no larger than 1 PiB; file inventories and native display sums use the same bound.

A 75 GiB inventory can therefore be planned under an explicitly configured larger
cap without allocating or downloading those bytes. Space admission checks the
actual filesystem and accounts for concurrent Hub reservations. Publication and
local-registration snapshots reserve their copy space before writing; failed
snapshots and partial verification files are cleaned up. Verified download files
remain available for explicit file-level resume.

Hashing/copying and downloaded stream chunks remain bounded to 1 MiB. Metadata,
error responses and nonstreaming HTTP remain independently capped at 16 MiB, even
when an artifact budget is much larger. The supported SDK handles large streamed
LFS uploads. If a provider instead requests a large regular/base64 upload, the
file-object guard fails before buffering it; it never allocates the whole file
into JSON. The 1 PiB safety ceiling is a numeric/contract limit, not a claim that
this workload size, transfer duration or upstream repository limit was accepted.
Tests simulate large inventories and disk states using tiny files only.
