# ModelBuilder permanent workbench

This is the first-party built-in feature module for the dedicated Code - OSS distribution, not a temporary third-party extension or a chatbot. The File menu uses a narrowly documented permanent workbench contribution and one composition import because Code - OSS 1.105 does not expose top-level File menu placement to extensions. All behavior remains in this module; no other editor-core changes are required. It is bundled by the distribution build; the VSIX is an additional local test/install artifact, not a Marketplace publication.

## Build and test

Node 22 or newer is recommended; Node 20+ is supported by the feature runtime. Run `npm ci`, `npm test`, and `npm run package`. The authoritative recipe JSON Schema is copied from `../../schemas/recipe.v0.schema.json` at build time. Source business validation, scopes, revision checks, digest normalization, execution and research semantics belong only to the Python operation service.

Use **ModelBuilder: Configure Service Connection** to set the loopback service URL and scoped bearer credential. The token is stored in Code - OSS SecretStorage. It is never placed in settings, recipe JSON, the webview, browser fetches, or logs. Remote URLs require HTTPS; HTTP redirects are rejected. Workspace trust is required before connecting a credential or starting/cancelling/resuming execution.

Create or open a model project at the landing screen. The open workspace and native Model Explorer are fixed to that one model, with Research, Code, Data, Recipe, Runs, and Versions. There is no in-workspace project/model picker or multi-recipe browser. Its single recipe opens automatically for authoring; importing a verified bundle creates a revision of that same recipe, retaining previous run references. Use **File → Import Recipe…** or **File → Export Recipe…** with the editor's Open/Save file dialogs. There are no recipe transfer buttons or forms in the workspace. The host reads/writes exact UTF-8 bundle JSON through the shared lossless transport without reserializing numbers. Export captures server bytes before the Save dialog and labels the captured revision; import retains the revision read before the Open dialog. Dirty graph/source or native JSON drafts block both actions, and a project change during either dialog aborts it. Close Project returns to the landing screen with unsaved changes protected. The workbench supports typed SVG graph authoring, pan/zoom, node/edge edits, arbitrary Python references and parameter JSON, full canonical JSON, and a schema-backed native Monaco source editor. Save uses the exact previously read revision. A 409 preserves the draft and displays the service diff and both documents. Explicit reload requires confirmation before discarding a dirty draft. Invalid JSON blocks graphical mutations; stale node parameter panels cannot overwrite newer source edits. Unsaved JSON and node-panel drafts are locally recovered after closing/reopening the workbench, retaining their original service revision for conflict checks. In-flight poll results cannot replace a newer navigation or save.

Research, claims, hypotheses, decisions, model lineage and compute forms are generated from `/v1/catalog` input schemas. Only explicit allowlisted operations are bridged from the webview. Project identity is enforced by the host and authorization by the service. The webview CSP denies network connections, inline scripts and command URIs; untrusted content is rendered as text. Run panes show application correlation IDs separately from authoritative backend IDs, reconciled state, actual logs and artifact references. A stop request never locally changes a run to cancelled.

## Actual Code - OSS GUI acceptance

Run `test/guiParity.cjs` through the repository's normal development-window harness (`--extensionDevelopmentPath=tests/gui`, `MODELBUILDER_GUI_SMOKE_RUN=1`, `MODELBUILDER_GUI_SUITE=workbench`), with its repository-owned workspace trusted using the normal UI and `--remote-debugging-port=9222` bound to loopback. Do not use upstream `--extensionTestsPath`: that mode discards persisted workspace trust and does not represent normal installed-extension behavior. Set `MODELBUILDER_GUI_PYTHON` to the project's `.venv/bin/python`, `MODELBUILDER_GUI_NODE` if Node is not on PATH, and `MODELBUILDER_GUI_EVIDENCE` to an output directory. The runner starts an isolated real service with an ephemeral token, invokes the real extension/native editor, and drives the actual Electron webview over CDP, including out-of-process iframe targets when needed. The ephemeral SecretStorage credential is deleted afterward. An unavailable OS keyring must be fixed on the host; the test never enables basic/unencrypted storage. `test/mcpEdit.py` is a separate official MCP SDK client over stdio. It checks the actual project landing, GUI creation/open/close, fixed model/recipe identity, build/import paths, graph authoring, typed Python preservation, GUI/MCP digest parity in both directions, conflicts, and invalid-source preservation. This test does not count as passing until run against the real built editor; unit tests are not GUI evidence.

No paid target, public registry, public recipe publishing, assistant binary, or custom training runtime is included here. First-party licensing remains UNLICENSED pending the repository owner's decision; dependencies retain their respective licenses.

For the separate real execution acceptance, select `MODELBUILDER_GUI_SUITE=execution` in the same normal-window harness. `test/guiRun.cjs` loads the shipping custom CPU recipe unchanged, starts the real ZenML adapter with `MODELBUILDER_BACKEND_PYTHON` (default `.venv-backend/bin/python`), and submits once through the visible Run button and normal confirmation. The driver checks the reconciled native UUID, terminal state, metrics, model version, logs and artifact IDs in the actual run pane. It retains runtime artifacts under ignored `.runtime/gui-execution-*`; no fixture training is substituted with a mock. The workbench suite selects the supported Code - OSS simple file dialog for deterministic automation of the real File commands; normal product settings use the operating-system file dialogs. Both GUI suites temporarily choose the supported custom confirmation-dialog presentation in their dedicated test profile, restore the previous preferences afterward, and keep workspace trust and Electron sandboxing enabled.

## Live checkpoint evaluation

In Recipe, **Evaluate checkpoints during training** opts the saved recipe into bounded live evaluation. Configure 1–4 evaluator processes and the timeout; built-in fixture training also exposes the checkpoint interval in training steps. Other harnesses declare their own checkpoint timing. Apply changes to the draft and save the revision before running. Enabling live evaluation requires the recipe's connected evaluator; this UI never invents an evaluation protocol or automatically enables external tracking.

Runs shows checkpoint step/identity, queued/running/completed/failed/canceled states, metrics and immutable provenance during active training. The service's `run.evaluations.list` supplies compatible previous-checkpoint comparisons; the UI never derives compatibility from matching metric names. Trend pairs cannot bridge incompatible evidence. Unavailable streams and legacy runs without live evidence remain explicit.

**Evaluation notifications** configures native IDE result, improvement, regression and failure messages, a metric filter, minimum absolute change and cooldown (10–3600 seconds). Preferences and durable event cursors are saved in the IDE workspace. Notifications apply to the open project, continue when its workbench is hidden, group bursts, silently baseline existing history on first observation and do not replay events after reconnect. Improvements and regressions require a compatible server comparison and declared metric direction. Closing the project stops observation; disposing the extension clears its polling timer. Notification data is not transmitted outside the IDE.

Optional project-level Weights & Biases settings are disabled by default. Offline mode remains local. Online execution displays its exact entity/project, allowlisted metrics and optional sanitized metrics snapshot in the native confirmation, then pins that disclosed configuration into the run request. API keys never enter the form or webview; they belong in the operator's environment. Source, dataset and checkpoint uploads are unavailable. Tracking status is separate from canonical run/evaluation results.

`npm test` covers the renderer projection, notification state machine, host concurrency/persistence/disposal, typed live settings and the hypothesis acceptance metric form default. These are unit/host-mock tests, not native GUI acceptance. `npm run test:browser` drives the actual compiled webview in sandboxed Chromium with simulated service messages and writes a scoped report; it does not verify native toast delivery or real training. Native end-to-end acceptance still uses the normal-window Code - OSS harness above. A browser launch denied by the execution environment records BLOCKED rather than PASS.

## Optional coding tools

Code → **Set up optional coding tool…** (also a native ModelBuilder command) exposes the bundled setup helper for independent Codex or Claude Code clients. Choose an absolute service Python interpreter, then inspect supported versions, check MCP readiness, review a configuration change, or install an exact pinned extension. Configuration and installation each require a native review/confirmation; canceling writes nothing. The launcher supplies the real IDE/profile locations, and unspecified profiles remain under the native IDE's own defaults. A service token must be provisioned in the external client's environment by the user. The IDE's SecretStorage credential is never read or copied by setup. Installing a tool does not verify its activation, sign in, or invoke paid inference.

## Research review and versioned data

Research now exposes bounded saved watches with explicit refresh, immutable new/changed citation findings, licensed source-text import, and staged recipe proposals. Forms use the service catalog, with saved-hypothesis and citation pickers; exact quote/text-ID/offset JSON remains available. Proposal cards retain source snapshots, diff, review and rejected history. Approval and application are distinct actions. Application requires a separate native confirmation, the loaded recipe revision and a clean draft; it never starts training. New draft edits arriving during application remain preserved for conflict resolution. Watch configuration does not start an undisclosed scheduler.

Data lists immutable dataset versions, row counts, source/content identities, rights, splits and transformation reports. Manifest fields use the shared bundled dataset schema and retain canonical JSON. Registration and new versions reference operator-approved roots; the UI never chooses arbitrary absolute data paths. Role-correct previews pin the selected dataset revision and paginate bounded rows. Preview is explicitly separate from actual model consumption. Use split in recipe calls the shared revision-checked `dataset.bind` operation, adds or replaces the chosen dataset import node, and can connect an existing dict input after native review. Dirty drafts are preserved and must be saved or reloaded before binding. The service pins the portable manifest, rights and source hashes, resolves only approved project roots into a request-local execution context, and stores reference metadata only. Dataset files are never uploaded or bundled; foreign imported references require explicit local registration and rebinding. Native Windows source reads and unverified Parquet support remain service-reported capabilities.

## Typed source-bound workspace controls

The node panel reads its fields from canonical `parameter_schema` or
`component.parameter_schema`. Enum, boolean, numeric, string and grouped fields
edit only touched values; unknown parameters and existing harness declarations
remain intact. Arrays and unsupported schema forms retain JSON authoring.
Incomplete typed input and the exact JSON escape-hatch text are saved with the
local draft. Applying the node checks its original identity and rejects a
concurrent JSON/schema change rather than rebasing or overwriting either draft.
The service remains the authoritative schema validator.

Opening/creating a model project uses the existing native source folder, or a
native folder picker when none is open. The host matches that local folder to
operator-approved `project.source.list` entries and sends only the alias and
returned repository identity token. Binding requires a distinct trusted-local-
code acknowledgement and never grants native workspace trust or operator trust.
The Code tab shows source pin diagnostics and opens only a verified local match.
Run confirmation and source/dependency admission remain separate.

Private registry discovery/publication is available only through the native File
menu. Discovery reviews a publication and its exact proposed diff before
importing a new revision of the current model's one recipe. Publication checks
token permission, separately confirms a required policy change, opens the exact
source/license/dependency review in the native editor, and asks for dependency
and final publication consent. Cancellation, stale reviews and concurrent drafts
never silently approve new content. The local-private registry does not provide
public hosting, install dependencies or execute imported code.

Compute has read-only remote provider, reservation and billing-history
diagnostics. Separate execution, cleanup, billing and evidence states remain
visible; exact integer-string money is never rounded through JavaScript Number.
Remote execution and spending enforcement remain disabled.

`npm test` covers frontend modules and mocked native-host interactions.
`npm run test:workspace-http` additionally starts a real isolated loopback service
and tests the compiled host helpers against typed source binding, canonical
recipe edits/conflicts, registry permissions/import identity and cost diagnostics.
It uses only a temporary test source checkout and does not launch a run.
`MODELBUILDER_SERVICE_ROOT` and `MODELBUILDER_HTTP_PYTHONPATH` can point this runner
to an isolated integrated service checkout. Native dialogs and the DOM are
mocked in this test, so it is not native GUI acceptance.
`npm run test:workspace-browser` exercises actual Chromium DOM with simulated host
messages. Browser and native Code - OSS acceptance are separate required gates;
launch failures must be reported as blocked, never converted to a GUI pass.

### Author a source-bound component

In Recipe, choose `component.run` from the existing Component menu and use
**Add node** with a unique node ID. This creates an explicitly incomplete,
inert `modelbuilder.component/v1` draft. In its existing node panel, enter the
component ID, version and role, select a Python file already pinned in
`recipe.source.files`, and enter the module and entrypoint. The panel copies
that file's declared SHA256 and the recipe commit; it never guesses either.
Declare the parameter schema, actual capabilities, and typed input/output
ports. At least one supported output is required. Empty capability arrays
claim no capabilities. Apply refreshes the existing typed parameter controls;
then Validate and Save use the authoritative service. Completeness is not
execution admission: source trust and each run remain separate checks.

Unapplied component fields, including incomplete JSON, recover with the node
panel. A changed canonical node or source closure blocks stale Apply rather
than silently rebinding the draft. Unknown fields are retained, and a matching
legacy node-level parameter schema stays synchronized when explicitly edited.

`npm run test:component-http` creates a NEW node through the same exported
Add-node and panel-control paths, then validates/saves it against an isolated
real HTTP service. It makes no registry or provider calls. Set
`MODELBUILDER_COMPONENT_NATIVE=1` and `MODELBUILDER_BACKEND_PYTHON` to opt into a
native CPU run of the newly authored, source-pinned independent test harness.
`MODELBUILDER_COMPONENT_REPORT` saves the scoped report;
`MODELBUILDER_COMPONENT_KEEP=1` retains the isolated run evidence. These tests
use a DOM test double and do not establish browser or native GUI acceptance.

### Review and pin committed source

In **Code → Review and pin source files…**, choose an explicit full Git commit (the displayed native input defaults to the bound checkout's HEAD), then choose files under the already-open, verified source workspace. Required recipe implementation and environment lock paths are added to the review inventory. The native review lists repository identity, full commit, relative paths and SHA-256 hashes, and the exact current/next recipe revisions. Only **Pin reviewed source** applies the preview digest as the next revision of the same recipe.

Pinning is inert: it never commits Git changes, executes source, installs dependencies, or starts a run. Unsaved recipe/native-source edits, files outside the bound workspace, symlinks, dirty committed files, changed source identity, and stale project/recipe revisions fail closed. A conflict requires a fresh review; there is no automatic rebase or retry. Native dialogs are covered by test doubles and the compiled host path has isolated real-HTTP coverage (`npm run test:source-pin-http`); this is not native GUI acceptance.
