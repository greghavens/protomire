# Native branches, joins and gated live evaluation

This is a bounded adapter implementation of the original conditional-pipeline
requirement (handoff §5.2 / evaluation G05). It uses the same recipe ID, revision,
typed nodes, editor and HTTP/MCP operations. ZenML 0.97's public dynamic pipeline
executes selected stages sequentially. There is no additional scheduler, run or
artifact registry, training loop, expression language or alternative workflow.

## Authoring and validation

A node may declare an explicit boolean control dependency:

```json
"condition": {
  "source_node": "quality_gate",
  "source_port": "allowed",
  "equals": true
}
```

`quality_gate.allowed` must be a declared `bool` output. The output must contain
an actual boolean; integers, strings and generic truthiness are rejected. The
condition is part of the canonical recipe and its digest. Both data edges and
control dependencies affect graph ordering. A condition does not change data
ports or manufacture optional outputs.

The existing node panel offers an explicit gate/value selector, preserves
unrelated node fields, draws the control dependency as a dashed graph edge and
shows recorded skips in run inspection. The panel refuses stale/removed gate
choices. A gate used by another node cannot be silently deleted. JSON/HTTP/MCP
use the same exported service-owned schema and validation.

Conditions can feed downstream data consumers and nested boolean gates. A false
condition skips the node. A required input or control gate from a recorded skipped
node skips its consumer transitively. Failed or pending producers are never
classified as skipped. Data and control cycles remain invalid.

Joins use the existing typed input declaration, without a new node kind:

```json
"inputs": [
  {"name": "left", "type": "dict", "required": false},
  {"name": "right", "type": "dict", "required": false}
]
```

Each port still has exactly one producer. A skipped optional producer is omitted
from the native step signature and call, rather than replaced by a fabricated
artifact or `None` input. A `code.python` join needs callable defaults for omitted
arguments; a `component.run` join receives only the available keys in `inputs`.
The callable decides what to do when one, several, or no branches survive. A
missing Python default, bad return type, or deliberate rejection is an actual
native step failure. Native typed input IDs retain the original producer lineage.
Built-in lifecycle stages are not optional-join adapters. Parameters cannot
shadow declared Python input ports.

`loop`, `repeat` and `iterations` node settings are still rejected explicitly.
Explicit typed bounded iteration is described in [NATIVE_BOUNDED_ITERATION.md](NATIVE_BOUNDED_ITERATION.md). Arbitrary loops and independent-stage concurrency remain unsupported; native SDK probes are not claims of their integration.

## Conditional live checkpoints

Enabled live evaluation can coexist with branches. A skipped trainer cannot
start a sidecar. A conditioned evaluator's gate must already be an explicit
ancestor of its trainer through the canonical data/control graph. The same
project-owned bool artifact is checked before training starts and again before
final evaluation; a false or skipped gate disables that evaluator's sidecar.
Skipped shared required inputs also disable the sidecar. Omitted optional inputs
remain absent from the sidecar's component input dictionary.

A gate produced by the trainer, by a later stage, or by an independent unordered
stage cannot control live evaluation. Admission rejects it as
`unsupported_conditional_live_gate`, explaining that the gate must precede
training or live mode must be disabled. The adapter adds no hidden gate,
dependency, ancestor or extra run. Existing bounded checkpoint snapshots,
evaluator child processes, event identities, cleanup and cancellation continue
to own live observations; ZenML owns the enclosing run and final artifacts.

## Exact selections and native outcomes

A selected conditional stage requires its gate in `execution.node_ids` explicitly.
The adapter never adds or executes an unselected gate or ancestor. Existing
selected-subgraph input bindings still reference exact native artifact IDs,
producer run/node/port, project ownership and stored-content SHA256. These are
revalidated through the same admission and worker binding path. False branches
do not relax admission: all selected source and bindings must still be valid.

Unconditional selections retain the existing static ZenML path. A selection
containing a condition uses the public dynamic pipeline. It calls stages
synchronously; no `.submit()` or parallel threads are enabled. The gate artifact
is checked for same project and allowed native boolean datatype/materializer,
then loaded and compared exactly. A false target's project module is not
imported and the target is not invoked.

ZenML does not create a StepRun for a branch that is never invoked. The adapter
does not fabricate one. It records `conditional_execution` on the actual native
pipeline run with the gate artifact ID, boolean value and value SHA256, exact
condition, decision (`eligible` or `skipped`) and `status_source` of
`native_run_metadata`. Propagated skips record the exact `blocked_by` data/control
dependencies and `reason: upstream_skipped`, rooted in a verified native gate
artifact decision. `eligible` means the gate passed, not that the stage
finished successfully. Its actual native step status remains authoritative.
`run.status` exposes these decisions, `skipped_node_ids`, and a merged
`node_statuses` view. An absent or pending stage is never inferred to be skipped.
A skipped stage has no output artifact, checkpoint or evaluation record.
`conditional_inputs` records omitted optional edges and actual consumed native
input IDs; status verifies these against the corresponding native step inputs.

## Cache, provenance and supervision

The bounded value-cache policy in [NATIVE_PIPELINE_CACHE.md](NATIVE_PIPELINE_CACHE.md)
continues to apply; training/evaluation/export/checkpoint stages remain uncached.
Full recipe/condition identity, source/data/environment, selection/bindings and
resume ancestry remain in the application SHA256 cache key. True and false
branches can reuse an eligible boolean gate only under that same immutable key.
Actual cache hits retain native original producer and artifact provenance.

One ZenML dynamic-specific adaptation is necessary: the SDK materializes normal
step parameters into fresh external artifacts. The native cache policy ignores
only `spec` and declared non-input parameters whose complete values are already
covered by the application key. It never ignores declared typed input ports or
their exact native artifact IDs. Source/runtime identity now explicitly includes
`conditions.py` and `execution.py`, as well as all backend modules. Changes to
control semantics cannot silently reuse an older runtime cache.

Existing native start/end hooks, supervisor deadlines, cancellation reconciliation,
immutable recipe provenance and checkpoint binding checks are used by both
execution paths. No resource reservation or per-stage environment/isolation is
promised. The local ZenML runtime silently downgrades isolated dynamic steps to
inline, established by installed-source inspection and an actual same-PID probe.
The adapter therefore remains trusted local sequential execution.

Scientific comparisons still require the existing evaluator/checkpoint/dataset
contracts and selectors. Skipped branches provide no observation to compare.
Cache reuse does not turn repeated data into independent scientific evidence.
Ordinary run inspection keeps evaluator measurements visible with pending
checkpoint verification and no asserted model owner. Explicit comparison and
phased promotion verify the actual selected native checkpoint and its original
producer; an evaluation-only run version cannot be promoted as that model.

## Acceptance and remaining gates

Automated acceptance includes strict schema/selection/loop/cycle and unavailable
live-gate rejections; editor preservation/stale-choice/graph/status tests; actual native
true/false runs with a false branch module that fails if imported; actual gate
cache reuse; supervisor timeout cancellation; and native selected-checkpoint
conditional evaluation/comparison checks; nested true/false branches, Python and
component joins, empty joins, transitive skips, native failures, exact selected
cuts, restored status and native input-ID inspection. Exact commands and terminal results
are supplied in the separate patch's verification evidence. This document does
not claim full aggregate installed-wheel, native GUI, remote CI, other OS/GPU,
hostile-code or remote/provider acceptance. This follow-on is rebased onto the selected-cut resume, evaluation-attribution
and phased-promotion baseline. Live selected cuts additionally preserve early
evaluations only when the trainer and compatible evaluator are both explicitly
selected; boundary data must match the same pinned native input. Conditional live evaluation additionally requires its gate to precede the trainer
and honors that gate before launching the first evaluator child. Final source pins and installed source
inventory still require regeneration and replay on the final union.

Primary SDK references inspected:

- https://docs.zenml.io/concepts/steps_and_pipelines/dynamic_pipelines
- https://docs.zenml.io/concepts/steps_and_pipelines/advanced_features
- Installed ZenML 0.97 `execution/pipeline/dynamic/compilation.py`,
  `execution/pipeline/dynamic/runner.py`, `config/cache_policy.py`,
  `orchestrators/cache_utils.py` and `orchestrators/local/local_orchestrator.py`.

The pinned package behavior and actual native tests are the authority; current
online documentation may describe later versions.

### Branch/join and conditional-live verification (2026-10-12)

Task-scoped evidence is under `evidence/tests/native-branches/`:

- `native.xml`: seven real ZenML branch/join cases passed, including Python and
  component joins, nested/required skip chains, deliberate exception/type failures,
  empty joins, exact selected cuts and restored native identity/input inspection.
- `live.xml`: four real CPU cases passed. A permitted evaluator published scores
  before a later training checkpoint. False evaluator and trainer gates produced
  no live checkpoint/evaluation records. Cancellation retained completed scores
  and left no queued/running evaluator children.
- `regression.xml`: native MCP/HTTP parity, deadline termination, explicit cancel
  and false-gate cache reuse passed. The true-gate cache assertion failed during
  concurrent runtime-source edits: the same immutable recipe produced different
  runtime-derived cache keys. The assertion remains unchanged. The exact keys are
  retained in `concurrent-cache-key-change.json`; stable-union replay is required.
- 89 recipe/selection/condition contract tests and six editor condition tests
  passed, along with TypeScript checks/build and targeted Python lint.

These are focused tests, not a claim of a clean full-product aggregate or native
GUI acceptance. Generated contract drift from the concurrent union still needs
regeneration and final verification by the integrating task.
