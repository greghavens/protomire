# Project source workspaces

Each model project owns one stable recipe identity and may bind one approved local source checkout. Source binding is a project setting used by existing Open/Create flows. It does not create another model, another recipe, a model selector, or a second training workflow.

## Operator configuration

`MODELBUILDER_OPERATOR_CONFIG` names a server-owned JSON file. Relative paths below are resolved against that file's directory.

```json
{
  "python_executable": "/opt/modelbuilder/.venv-backend/bin/python",
  "source_roots": {
    "segmentation": {
      "label": "Segmentation source",
      "path": "../segmentation-model",
      "trusted": true,
      "dependency_paths": {"training-data": "../data/segmentation.json"}
    },
    "forecast": {
      "path": "../forecast-model",
      "trusted": true,
      "dependency_paths": {"training-data": "../data/forecast.json"}
    }
  }
}
```

An optional `project_ids` array restricts an alias to existing project IDs. Omit it for aliases that may be selected while creating a project. Aliases are bounded simple identifiers. Paths, dependency paths, labels, project restrictions, and operator trust are not writable through project operations. Each alias must identify the root of a committed local Git checkout; nested folders, missing checkouts, and redirected symlink roots are unavailable. Ordinary linked Git worktrees are supported.

Providing `source_roots`, including an empty map, enables strict binding: an unbound project cannot execute or export source, and there is no fallback to the service checkout, a client filesystem path, a recipe repository URL, or another project. Legacy installations without this setting retain their explicitly configured single-root behavior and expose a `default` alias when a backend repository exists. Operators can migrate existing projects by enabling aliases and binding them; run/history records are not rewritten.

The backend Python environment/native ZenML store remain operator-configured and shared. A bound alias uses its own `dependency_paths` map; identically named dependencies in another alias or the legacy global map are not silently substituted. Existing byte hashes and exact installed-environment checks still apply. This is trusted local execution, not a hostile-code sandbox.

## Shared operations and native IDE contract

All operations use the shared service dispatcher, HTTP `/v1/operations/{operation}`, and the automatically generated native MCP tools.

1. `project.source.list({project_id?})` returns `sources`, each with an approved `alias`, `repository_id`, `workspace_path`, `head_commit`, `operator_trusted`, and `available`. Missing/unreadable sources have `available:false` and a diagnostic. An actor restricted to projects must pass an authorized project ID; global source enumeration is forbidden for that actor. `binding_required` reports strict operator mode.
2. `project.source.status({project_id})` returns the persisted `binding`, `project_revision`, checkout identity/path/HEAD when available, `pinned_source_matches`, and `diagnostics`. It inspects the current recipe's declared Git/file closure without importing source, fetching URLs, installing packages, or launching training. `project.open` includes the same status under `source`.
3. `project.source.bind({project_id, expected_revision, source_alias, source_repository_id, trusted:true, idempotency_key})` updates only the project source binding. `source_repository_id` must match the identity actually displayed to the user. The project revision increments; the recipe identity, recipe revisions, and historical runs are unchanged. A stale project revision or changed displayed checkout identity fails with 409.
4. `project.create` optionally accepts the coupled fields `source_alias`, `source_repository_id`, and `trusted:true`, alongside the existing model/project fields and optional initial recipe. Binding is atomic with project/recipe creation. A default draft does not acquire invented source pins; saving/importing the real recipe remains an inert separate edit of the same recipe.

The IDE should connect these operations to the existing native project open/create flow. Prefer the currently opened workspace folder, matched against the approved `workspace_path` values. If no folder is open, ask the human for a folder with the native picker, then match it to an approved alias. Do not submit the picked filesystem path as an API field. Do not infer approval from an arbitrary folder or recipe URL, and do not write operator configuration from the workbench. An unmatched folder needs operator configuration. Do not open a backend path blindly on a different machine; only use a verified local match, or ask the human to locate their corresponding folder.

Show the selected source identity and the trusted-local-code acknowledgement before binding. A cancelled picker or trust prompt leaves the project unchanged. Do not add a model/source dropdown to the recipe or a separate setup workflow. Existing native Explorer/editor opens the corresponding local workspace. After a conflict, reload the project and source status; never auto-accept a new identity under an old confirmation. Display unavailable/pin-mismatch diagnostics honestly. Source folder editing and inert recipe import do not execute code.

Binding consent does not replace the existing per-run `trusted:true` requirement. Operator `trusted:false` cannot be promoted by a client; execution remains blocked until the operator explicitly changes it. Merely listing/opening/status-checking a project never grants either form of trust.

## Identity, routing, and history

`repository_id` is a host-local checkout-location identity derived from the canonical root and Git-directory filesystem identity. It is stable across commits, but replacing/moving/recloning a checkout requires explicit rebind. It is not a portable scientific provenance identifier; recipe commit/file SHA256 identities remain the reproducibility contract.

Execution creates a request-local backend view with the bound root, trusted-root set, and dependency map. No global `repository`/active-project state is changed. Python SDK/supervisor processes start in the operator-owned backend state directory with safe-path mode; the model checkout is not an implicit import path before source-closure staging. Every view shares the existing native store and job ownership checks. Job requests record their exact source root, approved binding, and source workspace snapshot; application run records preserve the binding and snapshot, including the source path/HEAD observed at admission. Later project rebinding cannot rewrite these records or redirect already submitted work.

Exact resume resolves the parent's recorded binding, validates that the same approved checkout still exists, and executes the parent's immutable source/recipe closure. A current project rebind cannot silently redirect it. Warm start uses the current project's explicitly bound source and current recipe, with the existing component architecture/checkpoint compatibility gates. Removed, revoked, or repointed aliases block further execution/resume. Historical status, logs, artifacts, comparisons, and cancellation use the existing project-owned job/native record, so source removal does not hide or strand historical work. Recipe export/dependency inspection use the project's bound root rather than a globally active checkout. Bound recipe export and private-publication review verify that every exported file matches both its SHA256 manifest and the declared Git commit; matching working-tree hashes alone cannot establish Git provenance.

## Verification

`tests/service/test_project_source_binding.py` covers strict config/factory loading, schema exposure, inert binding/status, persistence/history, stale revision and checkout identity, operator allowlisting, scoped actors, trust, changed/escaping source bytes, alias repin, missing binding, concurrent request-local routing, isolated/hash-checked dependencies, recipe export/resolution, capabilities, and exact/warm-start source selection.

`tests/integration/test_project_source_execution.py` creates two distinct test-only Git repositories outside Modelbuilder, runs independent lightweight affine training components through one service and real native ZenML, verifies learned parameters and native/project IDs, rejects cross-project ownership and undeclared checkout modules shadowing platform/stdlib startup, exactly resumes the original model after rebinding the current project, reopens the service, retains old-source history, and denies resume after source approval removal. No Biff code, private model code, vendor login, cloud provider, or extra training framework is involved.

## Review and apply a source pin

`recipe.source.pin.preview` accepts the current `project_id`, `recipe_id`,
`expected_revision`, `expected_project_revision`, displayed
`source_repository_id`, an explicit full Git `commit`, and selected relative
`paths`. The project must already be bound to an approved checkout. Every selected
file must match that real commit and pass the existing bounded source-content
checks. Include all implementation and environment-lock paths required by the
current recipe. No source URL is fetched, no Git commit is created, and no code
is installed or executed.

The preview returns a digest-bound review of the before/after source closure,
file inventory and proposed immutable recipe revision. `recipe.source.pin`
requires those same fields, the `review_digest`, `confirm_pin:true` and an
`idempotency_key`, with read and modify authority. Apply creates only the next
revision of the existing owned recipe. Other parameters, project binding and
historical recipe/run records are preserved. Changed recipe/project revisions,
checkout identity, selected bytes or review contents fail closed. A successful
retry returns its original receipt without requiring an old checkout to remain
available, while current source-alias authorization is checked again.

The native Code action selects files from the already verified local workspace,
shows the explicit commit, selected files and next revision, and asks before
applying. Unsaved recipe changes must be saved or discarded explicitly first.
Filesystem paths never become client-controlled backend checkout roots. The same
operations and authorization apply through HTTP and native MCP.
