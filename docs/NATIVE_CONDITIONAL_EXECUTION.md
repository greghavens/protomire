# Native terminal conditions on the canonical recipe

This is a bounded adapter implementation of the original conditional-pipeline
requirement (handoff §5.2 / evaluation G05). It uses the same recipe ID, revision,
typed nodes, editor and HTTP/MCP operations. ZenML 0.97's public dynamic pipeline
executes selected stages sequentially. There is no additional scheduler, run or
artifact registry, training loop, expression language or alternative workflow.

## Authoring and validation

A terminal node may declare an explicit boolean control dependency:

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

Only globally terminal stages can be conditional: their outputs cannot feed
another data edge or another condition. Downstream merges and nested conditions
are rejected with an actionable message. `loop`, `repeat` and `iterations` node
settings are rejected explicitly. A recipe containing conditions and enabled
live evaluation is rejected because the current sidecar path cannot honor the
gate. General loops, independent-stage concurrency and conditional live
sidecars remain unimplemented product requirements; native SDK probes are not
claims of their integration.

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
`native_run_metadata`. `eligible` means the gate passed, not that the stage
finished successfully. Its actual native step status remains authoritative.
`run.status` exposes these decisions, `skipped_node_ids`, and a merged
`node_statuses` view. An absent or pending stage is never inferred to be skipped.
A skipped stage has no output artifact, checkpoint or evaluation record.

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

Automated acceptance includes strict schema/selection/loop/downstream/live
rejections; editor preservation/stale-choice/graph/status tests; actual native
true/false runs with a false branch module that fails if imported; actual gate
cache reuse; supervisor timeout cancellation; and native selected-checkpoint
conditional evaluation/comparison checks. Exact commands and terminal results
are supplied in the separate patch's verification evidence. This document does
not claim full aggregate installed-wheel, native GUI, remote CI, other OS/GPU,
hostile-code or remote/provider acceptance. This follow-on is rebased onto the selected-cut resume, evaluation-attribution
and phased-promotion baseline. Live selected cuts additionally preserve early
evaluations only when the trainer and compatible evaluator are both explicitly
selected; boundary data must match the same pinned native input. This does not
relax the conditions-plus-live rejection. Final source pins and installed source
inventory still require regeneration and replay on the final union.

Primary SDK references inspected:

- https://docs.zenml.io/concepts/steps_and_pipelines/dynamic_pipelines
- https://docs.zenml.io/concepts/steps_and_pipelines/advanced_features
- Installed ZenML 0.97 `execution/pipeline/dynamic/compilation.py`,
  `execution/pipeline/dynamic/runner.py`, `config/cache_policy.py`,
  `orchestrators/cache_utils.py` and `orchestrators/local/local_orchestrator.py`.

The pinned package behavior and actual native tests are the authority; current
online documentation may describe later versions.
