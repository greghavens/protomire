# Private recipe registry

## Implemented boundary

The registry stores immutable, inert recipe bundles in the service's existing
SQLite database. It is a private, local recipe registry, not a public marketplace
service. There is no configured public registry, network upload/download,
account creation, billing, code installation or model execution in this module.
An unconfigured registry identifier returns `capability_unavailable`.

Git remains the source-code authority. For source-bound projects, export and publication review prove the exported closure against the declared local Git commit as well as the manifest hashes. Recipes contain pinned dataset/model
references; their bytes stay at their external repositories. Bundles accept the
existing bounded UTF-8 source/configuration file inventory. Explicit bundled
dataset, model, checkpoint and runtime-artifact references are rejected. Bundled
references may name only source/environment text. File suffix/content checks
and declared kinds cannot establish semantic truth about arbitrary text.

One project owns one model and one stable editable recipe. Importing a registry
publication either creates a new immutable revision of that recipe or creates a
new project with exactly one recipe before its workspace opens. Nothing creates
a second active recipe, recipe tab, workspace selector or apply-recipe action.

## Integration interfaces

`recipe_registry_contract.operation_specs(_spec)` returns eight shared operation
specifications. Register them in `models.OPERATIONS`. All GUI/HTTP/MCP calls go
through `OperationService.invoke`; there is no alternate transport or auth path.

`RecipeRegistry(service).dispatch(operation, payload, actor)` handles only names
in `REGISTRY_OPERATIONS`. Dispatch this before the generic `recipe.*` branch.

`RecipeRegistry(service).authorize_cached_result(operation, payload, actor,
result)` must run before returning a cached `recipe.fork` result. This prevents
a narrowed project grant from recovering content through an idempotent replay.
Both source and target access are checked. The imported original bundle has its
own target-scoped reader, which remains accessible when source access is removed.

`authorize_publication_policy_change(payload, actor)` runs before `policy.update`.
Only actors with both `modify` and `publish` can enable publication. The existing
`publication_enabled` project flag defaults to false and enables only this
implemented private destination. Disabling it needs the existing modify scope.
The flag is no longer in the permanently-unavailable tuple; remote and
untrusted execution remain fail-closed. These service hooks share the existing
precise audit targets and generated HTTP/MCP contract.

No SQLite schema migration is necessary. The existing resource/history store
holds `recipe_registry_package`, `recipe_publication`, and
`recipe_import_snapshot`. All writes occur in the host's existing transaction,
audit and idempotency path. A package name is bound to its original source
project; another project cannot publish a new version under that name.

## Exact request schemas

The canonical schemas are in `recipe_registry_contract.py` and the standalone
`schemas/recipe-registry.operations.v0.schema.json`. The adjacent catalog JSON
includes descriptions/scopes. The standalone-schema test checks exact equality
with the shared catalog. Regenerate the full HTTP/OpenAPI/MCP artifacts with
`scripts/export_contracts.py` after integrating.

All mutations require an `idempotency_key`; all operations accept `request_id`.
Unknown payload fields are rejected. The only supported `registry_id` is
`local-private`.

1. `recipe.registry.list` (`read`): no required fields. Returns actual private
   SQLite capability, audience, supported license declarations, policy gate,
   and `public_registry_integration: "not_configured"`.
2. `recipe.discover` (`read`): `registry_id`; optional `query`, `tags`, `limit`
   (1–100), `offset`. Returns authorized publication metadata, total and next
   offset. Metadata includes exact source, immutable digests, license and
   dependency declarations, actor, version, timestamp, and readiness warnings.
   It never includes bundled file contents or reads an external URL.
3. `recipe.registry.get` (`read`): `registry_id`, `publication_id`. Revalidates
   metadata integrity and the entire bundle; returns metadata, `import_report`
   and lossless `bundle_json`. Do not parse/reserialize that text in JavaScript.
4. `recipe.publish.preview` (`read`): `registry_id`, `project_id`, `recipe_id`,
   `expected_revision`, `package_name`, `package_version`, `visibility: "private"`,
   and `metadata: {title, description, tags}`. Returns `{review, review_digest,
   can_publish, confirmation_required, execution_allowed: false}`. It does not
   persist anything. Package versions are three numeric segments with an
   optional bounded prerelease suffix. Metadata tags are lower-case identifiers.
5. `recipe.publish` (`read` + `publish`): the exact preview fields plus
   `review_digest` and `confirm_publish: true`; requires project publication
   policy enabled. If review reports unresolved/private dependencies, also
   requires `acknowledge_unresolved_dependencies: true`. Returns immutable
   publication metadata and never replaces an existing package version.
6. `recipe.fork.preview` (`read`): `registry_id`, `publication_id`, `target`.
   Existing target: `{mode: "existing_project", project_id, expected_revision}`.
   New target: `{mode: "new_project", slug, model_name, description?}`. Returns
   `{review, review_digest, can_import, confirmation_required,
   execution_allowed: false}`. Existing-project review includes exact diff,
   current/next recipe revision and unchanged recipe ID. New-project review
   makes clear the destination identity will be allocated on confirmation.
7. `recipe.fork` (`read` + `modify`): the exact fork-preview fields plus
   `review_digest` and `confirm_import: true`; unresolved dependencies require
   the same acknowledgement. Returns `{project_id, recipe_id, revision, recipe,
   project, publication_id, import_report, target_mode, execution_allowed:false}`.
8. `recipe.registry.imported_bundle` (`read`): `project_id`, `recipe_id`,
   `revision`. Returns the target-scoped retained snapshot with original
   `bundle_json`, target digest and origin. The original bundle contains the
   publisher's recipe identity, not a fabricated export of the revised target.

## Native File-menu UI contract

The registry adds no workspace recipe selectors. The native host exposes
registry discovery from the native File menu or project-opening surface. Keep
existing File-menu recipe import/export intact.

Publication flow:

1. Let the user select the local private registry and package/version metadata.
2. Inspect project policy. If disabled, offer an explicit, separately explained
   policy change. Call `policy.update` only on confirmation, with expected policy
   revision and a new idempotency key; it requires modify+publish scopes. A
   publish-capable token must already have been explicitly configured.
3. Call `recipe.publish.preview`. Show destination, private audience, exact
   package/version, recipe revision/digests, all bundled filenames, source,
   license declarations and unresolved/private references. State no public
   upload and no legal/trust verification.
4. Require explicit confirmation of that review; separately acknowledge any
   unresolved dependencies. Cancel closes the flow without mutation. Submit
   precisely the reviewed fields/digest with `confirm_publish: true`. A stale
   review/revision never retries by silently adopting new content.

Discovery/import flow:

1. Browse metadata outside the current workspace, via File-menu discovery or
   the pre-workspace project-opening surface. Read selected publication using
   `recipe.registry.get`; use its raw bundle JSON for source inspection/export.
2. Choose a new project before opening its workspace, or the existing project's
   next recipe revision. Call `recipe.fork.preview` with explicit target.
3. Show implementation/dependencies, original provenance, exact target and
   diff. Explicit confirmation uses `confirm_import: true`, the exact review
   digest and applicable dependency acknowledgement. Cancel has no effect.
4. Open the returned project or refresh the same canonical recipe. Do not start
   training, install dependencies, write source files, enable execution trust,
   or create a second recipe selector. Handle permission/409 conflicts visibly.

No native GUI integration/visual acceptance is claimed by these backend tests.

## Integrity, privacy and rights

Existing export/import validation checks canonical recipe SHA256, bundle
inventory SHA256 and every source file hash. Publication metadata has its own
digest and includes the review digest. Every read/fork verifies stored content.
Digests are integrity checks, not publisher signatures or Git authenticity.

The audience is authenticated actors with read scope and access to the source
project. Project-scoped actors see only their allowed publications; they cannot
fork into unauthorized projects or create new projects. After a successful
fork, the target retains a verified original-source snapshot under its own
scope. Discovery never bypasses source access through filters or pagination.

Source content requires an explicit source-license declaration and affirmative
redistribution permission. The conservative supported declaration policy is
MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC and CC0-1.0. This is not a legal
compatibility engine or rights verification. Unsupported bundled licenses fail
closed. External unsupported/private dependencies remain pinned references and
explicit unresolved/license-review warnings; they are never fetched or granted
access automatically. Catalog prose uses the existing recipe credential and
absolute-host-path scan, in addition to service secret-field/URL rejection.

Import readiness describes verified bytes inside the bundle. Source files are
not materialized into a workspace. A retained original bundle can be inspected
or exported through the explicit reader; execution still needs the existing
separate source/dependency resolution, trust, isolation and admission flow.

## Validation and remaining gates

The tests use real temporary filesystem SQLite stores, real concurrent service
instances, FastAPI HTTP requests, and an independent official-MCP-SDK stdio
client against a separate loopback HTTP service. They cover durable restart,
lossless JSON floats/large integers, immutable versions/package ownership,
metadata/source tampering, source/bundled license checks, mixed-case URL schemes,
explicit bundled data/weights refusal, unresolved private dependencies, policy
gates, read-only/project isolation, cached replay after access revocation,
content-bound confirmations, cancelled previews, conflict rollback, retained
source snapshots, exact audit targets, and one recipe per project.

These tests do not establish public registry integration, authenticated
Claude/Codex vendor sessions, native GUI behavior, hosted service security,
legal clearance or safe execution of arbitrary published code. Public
publication, third-party account setup, model/dataset hosting and payments are
not implemented or performed.
