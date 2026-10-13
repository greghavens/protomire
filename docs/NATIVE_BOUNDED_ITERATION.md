# Explicit bounded native iteration

A recipe can repeat an explicitly declared group of its existing typed nodes.
This addresses G06 using ZenML 0.97 public dynamic step calls, synchronous by default. ZenML
owns every actual step, run, artifact and model-version ID. There is no custom
scheduler, expression evaluator, generated training loop, or
second run registry. One project continues to own one model and one recipe ID.
The [opt-in stage bound](NATIVE_PARALLEL_EXECUTION.md) allows independent body
stages to overlap; iteration barriers and typed feedback remain sequential.

## Canonical declaration

```json
"bounded_iterations": [{
  "id": "refine",
  "node_ids": ["update", "measure", "stop"],
  "max_iterations": 4,
  "state": [{
    "source_node": "update", "source_port": "state",
    "target_node": "update", "target_port": "previous"
  }],
  "until": {"source_node": "stop", "source_port": "done", "equals": true}
}]
```

Each block runs its acyclic body between one and `max_iterations` times.
`until` is optional, post-body, and reads an actual declared boolean native
artifact. Integer/string truthiness is rejected. Bounds are 1–1000; one recipe
can declare at most 10000 native invocations. Blocks are disjoint, not nested.
These are explicit supported boundaries, not acceptance of arbitrary loops.
The old node fields `loop`, `repeat`, and `iterations` remain rejected.

The first invocation obtains each state input from its ordinary outside edge or
an exact selected-subgraph binding. An optional input may be absent initially.
Subsequent invocations pass the exact prior iteration's source artifact as that
native typed input. A required initial input needs a declared outside edge;
input/output types must match exactly. Feedback never reads a latest-name alias,
loads a value into a replacement artifact, or silently carries an older value
when the current producer was skipped. Missing state or early-exit outputs fail
the run. Other outside inputs remain unchanged across the block.

The ordinary graph remains acyclic. Collapsing a block into one atomic unit must
also remain acyclic; a dependency cannot leave the block and re-enter it.
Subgraph selection must include the whole declared block or none of it. It does
not add ancestors. Outside consumers receive only the final iteration's outputs.
A final-iteration branch skip propagates normally even if that node produced an
artifact in an earlier iteration. Every earlier native artifact remains visible
and may be selected explicitly in a later run.

## Training and warm starts

Training and evaluation can both be in a block. Established source-bound
components or built-in stages perform training; the iteration adapter never
implements optimizer/epoch logic. Without an explicit warm-start feedback edge,
repeated training calls are fresh experiments.

A checkpoint feedback edge can additionally declare `"resume_mode":"warm_start"`.
It must connect a training checkpoint Path to one declared training input, and
the receiving component must support warm start. That input can be optional and
absent on iteration one. On later iterations the exact preceding checkpoint is
both the native input dependency and the established training context's
`resume.path`. Model weights are restored according to that component's contract;
optimizer, scheduler, RNG and data cursor are not claimed to continue exactly.
The built-in trainer uses its `initial` checkpoint input. Native step metadata
records the source checkpoint UUID and invocation ancestry. Outputs and recovery
checkpoints use separate per-invocation directories.

`run.resume` with `warm_start` and an exact checkpoint UUID restarts the selected
bounded recipe at iteration one. The external parent checkpoint applies only to
its canonical trainer's first invocation; explicit feedback owns later calls.
The native run records its parent run, checkpoint and invocation. Multiple
training checkpoints require an explicit UUID: `model.checkpoint.get` never
chooses an arbitrary last invocation. All previous invocation checkpoint IDs
remain selectable.

`exact_state` loop continuation is explicitly unavailable: there is no preserved
loop cursor plus complete carried-state resume contract. It is rejected before
launch, including attempts to exact-resume a repeated checkpoint outside a block.
Existing exact-state continuation for non-iterative runs remains unchanged.
Repeated trainers currently require final native evaluations: live sidecars are
rejected for their blocks because their event identity does not yet distinguish
trainer invocations. Ordinary conditional live evaluation remains supported.

## Identity, status and editor

Each repeated native invocation has a deterministic unique ID. Its native
`config.extra` stores `canonical_node_id`, `invocation_id`, `iteration_block_id`,
`iteration`, and `max_iterations`. Readers verify these against the immutable
recipe declaration and the actual native step name. Original canonical node IDs
remain the public node IDs; invocation identity is additional, never a renamed
recipe node. Normal non-iterative step names remain unchanged.

`run.status` includes `invocation_statuses` and `bounded_iterations`. Actual
StepRun status is authoritative. Recorded branch skips create no fabricated
native step. Canonical `node_statuses` shows the latest started body iteration,
so a previous successful call cannot impersonate a pending or skipped call in a
later iteration. An iteration is completed only after its mandatory early-exit
decision and, when continuing, all next-state artifacts have been validated.
A skipped required control producer fails the run and leaves that iteration
incomplete, even when every body node has completed or been legitimately skipped. `executed_node_ids` can include a node whose final invocation
was skipped because an earlier invocation really executed.

Artifacts and evaluations expose both canonical node ID and actual invocation
ID/index. Existing exact selectors still identify run, canonical node, output
port, artifact UUID, and metric. Repeated metrics remain separate; status does
not flatten them into a guessed latest value. Checkpoint verification follows
the actual evaluator invocation's input artifact to its exact native producer.
Native timeout hooks resolve canonical settings through validated invocation
metadata. Timeout/cancel/failure recovery preserves the actual invocation and
partial checkpoint identity after confirmed process termination.

The same schema is exported to the native JSON editor and consumed by HTTP/MCP.
The recipe workbench has a canonical bounded-iteration JSON panel with
stale-source protection, restored unsaved drafts, explicit node deletion guards,
graph membership markers, and observed run summaries. It does not maintain a
second graph or validator. Save/validate still use the shared service.

Iteration nodes must currently declare `cache_policy:"disabled"`. Per-invocation
cache reuse has not been verified. Unrelated ordinary value-stage cache behavior
is unchanged. The [canonical stage setting](NATIVE_PARALLEL_STAGES.md) controls
bounded independent body-stage concurrency. Hostile-code isolation,
nested blocks, exact loop cursor resume, and repeated-trainer live sidecars are
not claimed by this implementation.

## Verification

Task-scoped tests are `tests/iterations/`, the editor's `iterations.test.cjs`, and
existing recipe/condition/deadline/lifecycle regressions. Actual native evidence
is in `evidence/tests/native-iterations/`. Focused results do not establish a
clean stable-union aggregate, installed-wheel replay, native GUI, other OS/GPU,
remote/provider or hostile-code acceptance. Those require their separate gates.

### Focused verification (2026-10-12)

Twelve distinct native acceptance cases passed in focused runs: hard bound,
boolean early exit, failed invocation, timeout, train/evaluate warm-start
feedback plus exact checkpoint selection and continuation, second-iteration
cancellation/recovery, final-iteration skip propagation, an earlier-iteration
selected input cut, official MCP/HTTP/schema parity, repeated export
list/inspect/package, absent mandatory until output, and absent required next
state. They inspect actual native StepRun/ArtifactVersion IDs, not synthetic
results. Existing non-iterative native warm-start/exact-state regression also
passed. Individual reports and limitations are recorded in
`evidence/tests/native-iterations/verification-summary.json`.

An earlier combined run had a test-fixture timing failure: cancellation arrived
after a second legitimate optimizer step, while the assertion expected step one.
The cancellation fixture now pauses deterministically after the first ready
checkpoint of iteration two; that focused case passed without weakening the
checkpoint-step assertion. The final complete native queue remains a separate
acceptance gate. Neither focused passes nor this document claim full-product
acceptance, native GUI acceptance, or stable-union cache acceptance.
