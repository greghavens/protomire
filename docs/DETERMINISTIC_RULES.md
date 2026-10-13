# User-selected deterministic safeguards

Rules extend the existing versioned component, dataset, typed condition and
bounded-iteration contracts. They introduce no expression evaluator, action
scheduler, optimizer, chatbot or second execution registry. Policies are inert
JSON with explicit options. Registration and preview do not grant execution,
source trust, rights, provider access or spending authority.

`rules.policy.register` saves an immutable `modelbuilder.rules/v1` policy under
its canonical SHA256. Changing any options under the same logical ID/version
conflicts; select a new version. Identical requests reuse the ordinary service
idempotency receipt. `rules.policy.get` reads it. `rules.evaluate` previews its
deterministic result from supplied facts. These are the same project-scoped,
audited operations for HTTP, the native workbench and standalone MCP. Register
requires `modify`; get/evaluate require `read`. Preview cannot launch work and
does not turn a caller's verification label into independently verified facts.

For execution, insert `modelbuilder.rules.gate` from the registered component
catalog and pin a reviewed source wrapper, such as `examples/rules/adapter.py`.
The component receives native evidence and checkpoint artifacts, and contains
the exact policy body in its immutable parameters. Existing source admission,
permissions, rights, deadlines, retries and cancellation remain authoritative.
All rule gates must disable cache. The native runtime recomputes the decision
against copies of original parameters/inputs and compares canonical digests,
not Python equality. It seals the platform-computed result using source,
component and runtime provenance captured before calling detached wrapper context.
It also verifies the application project,
native run and executed recipe digest, and seals the result with native input
UUIDs/content hashes and component/source identities. A fabricated wrapper
result, changed checkpoint, stale subject or another project's artifact fails.
Reviewed code remains responsible for the honesty of its measurements; this is
not a sandbox or proof of scientific validity.

## Policy vocabulary and evidence

Every policy declares `schema_version`, `id`, `version` and uniquely identified
rules. JSON schemas validate the complete supported options; unknown rule kinds
and expressions are rejected. The generic `method/v1` rule selects a separately
registered source method without adding a core-engine variant. Registered source-bound components can produce
new task-specific evidence and actions through the existing ABI. They are not
imported or selected by an operation payload's executable expression.

| Rule kind | Explicit options and versioned semantics |
| --- | --- |
| `convergence/v1` | Metric, minimize/maximize direction, target/plateau criterion, threshold, window, insufficient-window behavior, minimum/maximum epochs, step/time/cost bounds, and next-epoch resource increments. |
| `representation/v1` | Proportional-to-original or equal-per-stratum shares, minimum retained count, maximum absolute share error. Compares actual before/after counts; percentage reduction alone is not a failure. |
| `identity/v1` | Selected expected scalar configuration/identity fields. Exact canonical value comparison rejects a boolean substituted for a numeric option. Unselected fields are outside this rule's scope. |
| `overlap/v1` | Explicit cross-run or cross-lineage scope, maximum overlap, and digest identifying the checked identities. It never interprets repeated exposure within an epoch/run as cross-run overlap. A source-owned checker supplies measured facts. |

Evidence uses `modelbuilder.rule-evidence/v1` with `subject` (application
`project_id`, native `run_id`, executed recipe `config_digest`, `data_digest`),
`source`, `verification: "verified"` and `facts` keyed by the exact rule IDs.
Missing, unknown, failed, nonfinite, wrong-type, mixed-subject, unordered or stale
evidence cannot authorize continuation. Every metric observation identifies its
epoch, metric, value, checkpoint digest and canonical subject digest. A present
checkpoint alone does not prove a completed epoch: status must be `completed`.

Decisions use `modelbuilder.rule-decision/v1` and report `passed`, `continue`,
`stop`, `blocked` or `cancelled`, stable reason codes, observed values, every
selected rule option, policy/evidence digests and their own canonical digest.
Rational arithmetic makes comparisons and quota allocation independent of the
process's floating-point reduction order or Decimal context. Rational observed
shares/improvements serialize as exact strings, e.g. `3/5`.

Missing evidence blocks. Conflicting stop/continue rules block. Cancellation is
explicit. The native component raises on blocked/cancelled evidence, so an
unknown result cannot become `false` and inadvertently mean "run again".
It emits `done: true` only for an explicit stop decision, and `allowed: true`
only for passed/continue. Stops caused by a budget say `resource_bound:...`,
not convergence. Already exceeded bounds report `resource_bound_exceeded:...`.

## Convergence and execution capability

Target compares the selected metric to its selected threshold in the selected
direction. Plateau v1 requires a contiguous selected window whose every adjacent
directed improvement is between zero and the selected threshold. A degrading
curve is not declared converged. This is one explicit definition a user can
select; it is not a universal recommendation about loss or training practice.
All options are required rather than inferred from prose or research. Counts
and indexing options require actual JSON integers; integer-valued floats are
rejected consistently at registration. Supplied metric histories must be
contiguous, including while collecting a window. A recent contiguous history
may begin after earlier unavailable records, but sparse supplied histories never
count missing observations as evidence. Resource modes explicitly distinguish
active counters from `not_applicable` zero counters (e.g. free local cost). An
active bound at equality is exhausted even when the next-epoch projection is
zero. Selected step/cost units must match the measured resource-unit map; epochs
mean complete dataset passes and time is in seconds. Next-epoch increments are admission
projections, not hard runtime or spending guarantees; supervisor deadlines and
independent spending gates retain those separate responsibilities.

`insufficient_window: "block"` refuses an incomplete window.
`"collect_within_bounds"` permits explicitly selected collection of additional
valid observations, subject to the same continuation capability and resource
bounds. It does not permit missing metric fields or inconsistent identities.
Minimum epochs can postpone a satisfied criterion; a maximum/resource bound
still wins over that minimum. Continuation requires an explicit supported
`exact_state` fact. The native gate verifies either an actual connected native
checkpoint Path using the established checkpoint manifest/asset verifier, or
an actual connected JSON `modelbuilder.rule-checkpoint-state/v1` artifact
with explicit resume modes and typed model, optimizer, scheduler, RNG and
data-cursor state. Null, missing and empty effective states are rejected.
Stateless optimizer/scheduler/RNG entries may explicitly declare `kind: no_op`
with a nonempty reason; model and cursor must contain effective values. Its digest must match the measured epoch.
A weights-only checkpoint cannot authorize exact continuation.

The harness owns actual epoch execution and state restoration. Built-in
Transformers max-step adapters, runner/v1 and native training warm-start feedback
have not gained exact epoch continuation in this change. They must report
unsupported continuation instead of labeling fresh/warm-start training another
epoch. The native integration fixture exercises supported numerical-state
feedback in an independent, source-pinned synthetic gradient harness: one
complete two-example dataset exposure per epoch, real weight updates, actual
native carried-state/decision artifacts, target stopping and bounded stopping.
This proves the supported mechanism, not model quality or broad adapter support.

Use the gate's `done` boolean as an existing bounded block's `until` output.
Hard `max_iterations` and supervisor deadlines remain independent limits.
Retries execute the same immutable invocation/inputs; no hidden action is
launched by an evaluation. Repeated submission IDs reuse existing native work.
Exact whole-loop resume still has the existing explicit unsupported contract.

For a mandatory pre-action guard, set the action's `required_guards` to the
single gate node ID and set its ordinary condition to that guard's
`allowed == true`. Put multiple predicates in that gate's policy. The guard and
its evidence producers must be unconditional. Exact-node execution cannot omit
the guard or substitute saved input bindings for it. A guard denial fails its
native step; an explicit stop legitimately skips the conditioned action.
An explicit `rules.policy.activate` operation additionally pins this policy
on the project, with optimistic project revision fencing, selected operation
names and selected native node roles. Registration stays inert. Updates/imports
cannot remove this project authority: admission requires matching native guards
for every selected node in the activated role scope. `rules.policy.deactivate`
is a separate audited revision-fenced operation. The worker rechecks the durable
store identity and exact activation snapshot before dispatch. Existing scientific
activation, rights and permissions remain independent. Standalone export-package and promotion operations accept an explicit
`rule_gate` selector (native run/node/result artifact/content SHA256). The
inspector verifies a completed trusted native gate, the exact active policy
revision/digest, original source/evidence/native-input identities, actual boolean
allow output and target descriptor. The gate must consume an actual `target`
Path artifact: an export for packaging, or the single selected checkpoint for
promotion. The target must belong to the exact native model version/action
artifact, and promotion retains its existing scientific comparison, rights and
prepared-evidence fences. The saved custom method result is inspected without
executing plugin code under read/package authority. Missing, stale, foreign,
failed and substituted decisions block. Continuation policies, ambiguous
promotion checkpoint selection and unsupported target descriptor kinds remain
explicitly unsupported; they are not converted into predicate approvals.

## Dataset sampling

An eager dataset manifest can declare this deterministic transformation:

```json
{
  "kind": "rows.sample",
  "version": "1",
  "split_ids": ["train"],
  "fields": ["category"],
  "fraction": 0.1,
  "seed": "sample-1",
  "allocation": "proportional",
  "min_per_stratum": 1,
  "max_share_error": 0.01,
  "required_strata": []
}
```

The exact requested split total is floor(fraction × original rows). Each split
is allocated independently using bounded largest-remainder targets, constructive
minimum counts and available capacities. The pure `plan_quotas` function performs rational bounded water filling,
then largest-remainder integer reconciliation; ties use canonical stratum
SHA256 order. Its cost depends on the number of strata, not selected row count. Feasible minima are satisfied
before representation validation. For example, 98 A / 2 B sampled to 10 rows
with minimum one and share-error tolerance 0.1 yields 9 A / 1 B. SHA256(seed, sample ID) ranks rows within each
stratum. Returned rows retain original order, other splits retain every row,
and source bytes remain unchanged. Proportional and equal allocation are
distinct choices: 600 A / 400 B at 10% yields 60/40 proportionally or 50/50
equally. Strata, seed and tolerances are user-selected; no source/category names
or universal shrinkage thresholds are hardcoded.

The operation records readable stratum values, before/after counts and shares,
per-source counts, selected/removed sample IDs, selection SHA256 and declaration
SHA256 in existing transformation/version lineage. Minimum coverage and share
tolerance are validated before committing a version or importing it. An empty
result, missing field, declared required-stratum absence, infeasible minimum or
share tolerance outside available capacity fails explicitly. `required_strata`
is a user-selected array of complete selected-field value objects, checked
before allocation in every targeted split and retained with a constructive
minimum of at least one for declared required categories. An empty declaration records
`coverage_status: unspecified`; observed strata alone never prove expected
category coverage. There is no silent backfill, replacement
sampling, altered quota or unrequested change to other splits. Existing
consumption receipts still distinguish prepared samples from actual exposure.

Streaming sampling currently fails closed with
`dataset_sampling_streaming_unsupported` during manifest validation. Other
streaming transforms remain supported. The eager implementation is bounded by
the existing eager registry limits; it does not materialize an unbounded stream.

## Verification scope

Tests under `tests/rules` cover deterministic decisions, typed option parity,
scoped overlap, metric windows, all explicit bounds, malformed/nonfinite facts,
conflicts, cancellation, actual checkpoint identity, exact percentage allocation,
equal allocation, tied quotas, infeasible coverage, source immutability, registry
integration, immutable versions/retries, HTTP/MCP schemas/permissions, independent
official stdio MCP/restart, and actual native bounded optimization/artifacts.
Existing condition/iteration/dataset/component/scientific tests remain relevant.
GUI, installed-wheel replay, final shipping runtime/source pin, larger streaming
sampling and broader training-adapter continuation remain separate gates.

## Source method extensions

`rules.methods` lists inert built-in and project-registered declarations.
`rules.method.register` requires modify authority and records method ID/version,
source entrypoint/commit/path/SHA256, decision domain, parameter schema/schema
digest, and complete method digest. Identity or schema changes require a new
version. Declarations cannot replace a built-in identity, contain executable
expressions, or retrieve schema references. Policy registration requires its
exact project-registered custom method declaration.

A `method/v1` rule contains `method` (that complete declaration) and
`parameters` validated by its pinned schema. Built-ins and custom methods share
the existing `scientific.Method` registry contract. A custom predicate emits
passed/blocked; a continuation technique emits continue/stop/blocked/cancelled.
The registered source callable receives keyword facts, parameters and subject,
and returns exactly outcome, reasons and observed. Core JSON/digest and outcome
validation is mandatory. This allows explicitly selected noisy-flat, patience
or other techniques without changing core engine logic or replacing plateau v1.

The control plane never imports a method from an operation payload. Built-in
read-only preview remains available; custom preview refuses with
`custom_rule_execution_required`, even for a caller who also has execute scope.
Use the normal trusted `run.submit` gate path for a custom native evaluation.
The runtime independently loads method callables from the already admitted
recipe source closure, requires matching commit/file SHA/schema identity, and
recomputes the wrapper result with those registrations before sealing. Native
attestation records the exact method declarations alongside native input IDs,
producer identity and immutable source/runtime snapshots. Pure deterministic
behavior remains an obligation of reviewed source; trusted Python execution is
not an I/O sandbox.

All rule native, custom-native, standalone-action and independent stdio acceptance files are in
both canonical serial and bounded native-queue service selections. Their service
interpreter requirement is retained; they are excluded from backend partitions.
A collection regression checks completeness and no duplicate coverage.

Built-in best/last checkpoint promotion requires the gate’s explicit `target_checkpoint_kind` option. Its native target attestation records the owning verified manifest digest together with the selection kind, asset path and asset SHA256. Promotion verifies that exact selection and manifest against the measured evaluator input. Omitting the option binds the entire manifest and does not authorize a selected-asset action. Even equal best/last asset hashes do not erase their distinct selected kinds.
