# Scientific workflows and native checkpoint derivation

These contracts extend the source-pinned component ABI and native ZenML execution.
There is one project, one continuing model and one canonical recipe identity. No
second trainer, scheduler, model registry, provider call or publication path is
introduced. The same operation catalog serves HTTP, native MCP and IDE actions.

## Derived checkpoints

A `component.run` with role `derivation` owns a new native output and the current
native model version. It does not impersonate a training step. Its Path output
uses `modelbuilder.checkpoint/v2` and `modelbuilder.checkpoint-derivation/v1`:

- Exact native parent artifact IDs, project IDs, storage SHA256s and semantic
  checkpoint digests, keyed by the actual input ports
- Pinned component digest, source entrypoint and configuration
- The actual native producer run, invocation and model version
- An inference-only artifact: step 0, model state only, no optimizer/scheduler/RNG
  continuation, no exact-state or warm-start promise
- Explicit invalidation of inherited calibration

The runtime checks this contract against actual connected inputs. Explicit
comparison, lineage inspection and promotion re-read native parent/output bytes
and follow cross-run lineage. Foreign or unknown parents, changed content,
ambiguous/cached ownership and unsupported materializers fail closed. Promotion
preparation includes ancestor native runs and their storage witnesses, including
ancestors outside the directly compared runs.

`modelbuilder.checkpoint.blend` is a verified implementation, not a fixed catalog
of transformations. Pin the example wrapper in the project source closure. It
accepts two checkpoints with `weight_layout.format = safetensors-state-dict/v1`.
The layout names one declared safetensors weight asset, explicit auxiliary assets
and a model signature. Both parents must have the same architecture identity,
layout/signature, exact tensor keys, shapes and dtypes. Auxiliary bytes must match.
Nonfloating buffers must match exactly. Floating tensors must be finite. A caller
specifies alpha in [0,1] and asset/element bounds. Computation is on CPU with
float64 intermediates and the original output dtype. Endpoints and identical
entries are preserved. Assets are hash-verified; pickle is never deserialized.

Other reviewed source-bound components can implement architecture-aware merging,
quantization, inference configuration changes or calibration, under the same
lineage contract. The bundled blend deliberately does not support LoRA/adapter
merging, quantized tensors, sharded weights, architecture conversion, or restored
optimizer state. Equal architecture identifiers are a declared compatibility
contract, not proof that arbitrary user code honors an architecture.

Select a parent's actual best/selected checkpoint using `model.checkpoint.get`,
then bind its exact native artifact descriptor. A derivation can be evaluated by
the same source-bound evaluation component that understands its declared layout.
`model.checkpoint.get` supports purpose `inference` for these artifacts.
`model.version.lineage` exposes verified `checkpoint_derivations`.

## Versioned scientific policy

`scientific.policy.register` creates an immutable content-addressed project policy.
Registration does not activate it. `scientific.policy.activate` uses the current
project revision and requires that policy for accepted decisions and promotion.
There are no product- or model-specific metric thresholds.

A `modelbuilder.scientific-policy/v1` names:

- Stable policy ID/version and purpose
- Every metric, direction and minimum oriented improvement
- Each method ID/version, implementation identity and parameters/assumptions
- Allowed model treatment variations, while dataset, cohort, protocol and metric
  definitions must remain identical
- Family error rate, Bonferroni rule allocation and fail-closed unknown behavior

`run.compare` already binds immutable executed recipe revisions and configuration
differences. Add `scientific_policy_digest` and `scientific_evaluations` selecting
actual baseline/candidate run, node, output, artifact and metric for every rule.
All metrics on a side must evaluate the same checkpoint and actual model owner.
Missing/ambiguous evaluations, incompatible cohorts, unverified checkpoints and
unsupported methods cannot pass. A primary-metric improvement cannot compensate
for a failed retention rule. A failed or unknown gate produces no winner.

The result includes policy/evidence digests, actual selectors, cohort identities,
method/threshold/uncertainty details and per-rule outcomes. Accepted decisions
bind both the comparison digest and scientific result digest. Promotion rechecks
the current required policy and native evidence; hand-entered acceptance cannot
replace a required failed/absent gate. Scientific acceptance never authorizes
spending or publication.

### Verified methods and sample planning

`scientific.methods` returns inert method schemas.

- `scalar_threshold/1` is descriptive only. It claims neither significance nor
  sample adequacy.
- `paired_normal_known_sd/1` is a one-sided paired-mean z test under explicitly
  supplied independent-normal-difference and known-population-SD assumptions.
  The variance source, minimum detectable effect beyond the null threshold,
  target prospective power and alternative are required. This method is not a
  general substitute for a paired t test, bootstrap, clustered analysis or a
  binomial test.

`scientific.sample.plan` uses those explicit design inputs. There is no universal
minimum N. It reports required pairs and prospective design power, not observed
post-hoc power. Test gating also requires the declared prospective sample size.
Multiplicity is controlled by Bonferroni within one pre-specified comparison of the declared rules; it does not account for repeated/adaptive reuse of a holdout. No family-power claim is made. Python's
`statistics.NormalDist` supplies inverse-normal quantiles and `math.erfc` supplies
stable tails. Numerical range failures are explicit, including unrepresentable
standard errors. Tests compare with an independent SciPy oracle, including tiny
alpha values. These are conditional calculations, not independent verification
that the user's population/variance assumptions are true.

### New methods without a product release

A reviewed source-bound component may supply `Method(id, version,
implementation, parameter_schema, evaluate)` objects to `scientific_gate.gate`.
Methods cannot replace existing identities or overwrite evidence/policy fields.
The policy pins an `evaluator` containing the component digest and full source
implementation reference. Use component role `scientific_gate`, with native
baseline/candidate evaluation dictionary inputs and typed `passed`/`result`
outputs. The core runtime validates the typed report against the exact input
records and seals the actual native producer/input identities. This same bool
feeds existing native branch or continuation conditions.

For comparison/promotion, `scientific_gate` selects the completed native result
artifact by run/node/port/artifact and storage SHA256. The verifier requires an
explicitly trusted completed execution, exact policy/evaluator/source pin,
unchanged native inputs, exact record/cohort/checkpoint linkage and internally
consistent rule/aggregate outcomes. It verifies the executed result instead of
importing or reimplementing arbitrary custom methods in the control plane. A
caller-provided `passed: true` is never an accepted input. Reviewing scientific
correctness of custom code remains the project's responsibility, just as for a
custom trainer or evaluator.

## Declared-budget smoke runs

Components can declare `capabilities.smoke_budget` with version
`modelbuilder.smoke-budget/v1`. Each limit identifies an exact nested integer
parameter path, unit, minimum and `absolute_integer_cap` semantics. The bundled
Transformers Trainer advertises its existing absolute `max_steps` budget. A new
component can expose a new budget path without a core product edit.

- `run.smoke.plan` returns an inert draft and exact changes using only declared
  semantics. Unsupported budgets return a clear diagnostic and no draft.
- `recipe.smoke.prepare` saves that draft as the next immutable revision of the
  same recipe, preserving the parent revision/digest, limit choices and budget
  contract digests. It does not execute.
- Use the ordinary `run.plan` and `run.submit` with that saved revision. There is
  no invisible execution-time hyperparameter override or guessed learning rate.

Smoke execution is explicitly non-production. Checkpoint ownership propagates
non-production status through native bindings and derivations, and promotion
refuses smoke checkpoints/derived descendants. A smoke run is a functional
check, not statistical or production-quality acceptance. Budget declarations
must be honored by the reviewed harness; the generic platform cannot prove the
semantics of arbitrary source code. Complex budget transformations that cannot
be expressed as an absolute integer cap require a reviewed adapter; no automatic
epoch/batch/token conversion is guessed.

## Verification boundary

Independent CPU fixtures exercise actual training, multi-parent native blending,
evaluation, configuration comparison, policy selection/promotion, smoke ancestor
blocking, and changed-ancestor rejection. Unit/oracle tests cover the numerical,
tensor, identity and missing-evidence boundaries. Real HTTP and official MCP
clients exercise shared schemas/results and restart persistence. The native UI
module and typed action tests are supplied separately for host integration.
These checks do not constitute accelerator/model-scale acceptance or native GUI
interaction acceptance. No private project code, assets or thresholds are used.

## Upstream rights and notices

Derived checkpoints retain every parent's exact native artifact/checkpoint identity
and full rights envelope in `parent_rights_evidence`. Their `rights` envelope is
a deterministic union of all source/evidence/review references, unresolved items
and parent-envelope digests. An absent or malformed parent's evidence remains
unresolved; the destination recipe's license label never replaces it.

Promotion and export packaging use the shared rights-policy registry and the
runner's internal, project-scoped authority receipt. Policies are resolved from
the authoritative service store, not accepted from operation payloads. Promotion
captures the current canonical recipe's structured usage context together with
the policy snapshot in its prepare/apply guard. Export uses the actual immutable
exporting recipe's context. Every source must match its reviewed evidence,
permitted intended use, exact context and current policy revision. Missing,
unreviewed, revoked or stale evidence blocks artifact use. The explicit test
fixture binds a locally authored notice and test-only reviewed policy; local
origin or an empty source list does not imply copyright permission.

Export manifests must preserve the selected checkpoint's complete rights
lineage. Packaging checks the reviewed required notice files and attributions;
the transfer receipt binds those policy revisions. Each subsequent package chunk
rechecks current authority and the original review before returning bytes. Raw
metadata inspection remains available without treating it as an export grant.
The Hub workflow owns immutable original license/notice evidence and publication
rules. These checks record and enforce reviewed constraints; they do not infer
legal compatibility, authorship or legal advice. Undeclared I/O performed by
trusted arbitrary source code is not a sandboxed rights-enforcement guarantee.

The rights integration depends on the shared `hub_rights` validator and
`backend.execution_rights` authority contract. They are owned by the Hub/runner
implementation, not duplicated scientific-policy engines.
