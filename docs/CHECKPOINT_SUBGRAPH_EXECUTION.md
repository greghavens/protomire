# Independent checkpoint evaluation and export

`run.plan` and `run.submit` share the existing API/MCP contract. They now accept
an optional `execution` object over the one canonical recipe:

```json
{
  "project_id": "project-id",
  "recipe_id": "canonical-recipe-id",
  "expected_revision": 4,
  "trusted": true,
  "idempotency_key": "evaluate-benchmark-a",
  "execution": {
    "node_ids": ["benchmark_a", "evaluate_a"],
    "input_bindings": [{
      "node_id": "evaluate_a",
      "input_port": "checkpoint",
      "backend_run_id": "completed-training-run-id",
      "source_node_id": "train",
      "source_port": "checkpoint",
      "artifact_id": "native-artifact-version-id",
      "content_sha256": "64-lowercase-hex-characters"
    }]
  }
}
```

Selection is exact. There is no ancestor expansion: an unselected trainer is
neither compiled nor run. Every required selected input must be supplied by
another selected node or an explicit binding. Conflicting or duplicate sources
are rejected. Omitting `execution` preserves the original full-recipe workflow.
The immutable canonical recipe ID, revision and digest are unchanged; selection
and resolved input provenance are additional native run metadata. `run.resume`
retains its existing explicit training-continuation semantics.

Discover binding descriptors explicitly using `run.artifacts` with
`include_binding_descriptors: true`. Each supported output includes a
`binding_descriptor` containing producer node/port, native artifact ID,
content SHA256, declared type, producer recipe revision/digest, role and model
version. Normal artifact/status polling does not rehash checkpoint bytes.
Only completed producer runs in the same project, exact native output ownership,
matching declared/native types and the existing local native artifact store are
accepted. Project materializers, foreign paths and symlinked storage are denied.
Planning hashes native storage bytes without loading the artifact or importing
producer code. Submission revalidates, then the worker rechecks before passing
ZenML 0.97's public `ArtifactVersionResponse` to the selected step. No
`ExternalArtifact(id=...)`, new registry, checkpoint copying or training loop is
introduced. Local supervision, cancellation, source admission and idempotency
remain in force.

`run.status.execution` records exact selected nodes and resolved input bindings.
`executed_node_ids` comes from native started/completed/failed steps.
`run.status.evaluations` records every native evaluation output separately:
run, node, output port, artifact, metric, value, evaluation digest, immutable
record and checkpoint bindings. `metrics.evaluation_catalog` is the compact UI
catalog. Duplicate metric names are never silently overwritten, even if values
and evidence are identical.

For comparison, `run.compare` accepts `evaluations`, with exactly one selector
for each distinct `run_ids` entry:

```json
{
  "backend_run_id": "evaluation-run-id",
  "node_id": "evaluate_a",
  "output_port": "metrics",
  "artifact_id": "native-evaluation-artifact-id",
  "metric": "validation_loss"
}
```

Selectors must match the requested metric and native output. Missing/mismatched
selectors fail, and ambiguous legacy comparisons have no winner. Scientific
comparison still requires matching pinned dataset/split/protocol/metric
identities. Explicit selectors and evaluation evidence digests enter the
comparison digest. Direct checkpoint-bound evaluations identify the trained
producer model version, not their separate evaluation-run version. Promotion
recomputes the same comparison; evaluation failure or cancellation never
changes the prior training run, its metrics, or a project's chosen model.

A selected training cut retains live checkpoint evaluation when its evaluator is
explicitly selected and the resolver proves one compatible evaluator with shared
selected upstream inputs. No unselected evaluator or ancestor runs. Trainer-only
and evaluator-only cuts have no live sidecars. Boundary-bound evaluator inputs
(including optional calibration inputs) must match one explicit trainer input's
same producer run/node/port, artifact ID and content SHA256. The native backend
revalidates those pinned inputs before loading them; only the trainer's existing
loaded value is forwarded to its live evaluator. No producer is run implicitly.
Missing, conflicting or unmatched inputs and ambiguous selected evaluators fail
admission. The canonical
recipe is never rewritten by this execution view. Recovery-only artifacts and
arbitrary custom materializers remain outside the first binding contract.

Conditional terminal stages and live evaluation are separate bounded paths.
A canonical recipe containing both conditions and enabled live evaluation fails
validation explicitly; selected cuts cannot bypass that restriction or execute
a hidden gate/evaluator. See [native terminal conditions](NATIVE_CONDITIONAL_EXECUTION.md).

An export-only run owns its own native version and package. Saving that export
uses the export run/version ID; it does not relabel the package as owned by the
previous training version. Its provenance includes the actual native execution
selection and selected checkpoint artifact/identity. An accepted checkpoint
evaluation decision can independently promote the trained producer version.

## Integration and verification

This feature is integrated in an isolated copy of the source/dataset/release
union. Regenerate immutable source manifests against the actual source commit
under the normal release process before publication. The dataset
resolver must filter import nodes using `execution.node_ids`; admission uses an
inert selected-node/resource view but retains the full canonical source pins.
Use the release follow-on selector UI/service patch for decision persistence,
promotion forwarding and explicit choices. Export retrieval should verify the
actual native step input artifact, including a reused producer artifact.

Tests cover one native training execution, saved-checkpoint evaluation on two
pinned benchmarks, explicit compatible and incompatible comparisons, native
export without further training, failed/cancelled evaluation isolation,
wrong-project/producer/type/hash binding rejection, read-only planning and
selection-aware idempotency. Shipping recipe source closure tests require a
fresh immutable source pin after integration and are not satisfied by this
unpublished staging tree.

## Hardened native binding reads

Native artifact identity reads use nofollow descriptor-relative traversal,
regular-file checks and before/after mutation checks. Traversal is limited to
100,000 entries and 128 directory levels; per-file and total bytes are limited
to 16 GiB. Unsupported secure directory traversal fails closed. This is a
bounded admission-time identity check, not a lock against later storage edits;
submission and the worker revalidate. Native POSIX acceptance is separate from
Windows support and no cross-platform runtime acceptance is implied.

## Combined staged verification

- Service/contract/security/source-closure selection: 1,165 passed, 19 skipped,
  19 deselected with the optional backend interpreter explicitly unavailable.
  Source closure used a real isolated source snapshot; no temporary source
  commit or example pin is part of the deliverable.
- Frontend type checks and 243 unit/host tests passed, including explicit
  evaluator choices, selector-only late responses and invalidation of a shown
  comparison when its evaluator catalog changes. Real HTTP/external-MCP parity
  also passed. These are not browser/native GUI acceptance.
- Real train-once native subgraph, failed/cancelled evaluation isolation and
  existing release lifecycle plus evaluator-selection regression passed
  (9 tests). Native-binding security tests pass under both service and native
  interpreters (46 cases).
- The authored source-bound, immutable dataset-to-training-to-independent
  evaluation-to-promotion/export chain passed, including verified package
  retrieval and fresh-process reload with zero prediction difference. The
  trainer executes once; downstream exports retain their own native ownership.

The CI runner includes both native subgraph and combined dataset/release tests.
The final runtime source closure adds `execution.py` and
`backend/native_inputs.py`, increasing the source inventory from 63 to 65 files.
Raw environment evidence is excluded, and the new publication requires its own
remote CI.
