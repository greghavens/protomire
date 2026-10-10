# ZenML backend boundary and tested mismatch

The backend runs ZenML 0.97.0 in a separate interpreter. The application and MCP service never import ZenML or Torch. Native `pipeline`, `step`, `Model`, `Client`, materializers, `save_artifact`, `log_metadata`, and public `BaseZenStore` methods own the real pipeline runs, artifact versions and model-version links. There is no MLflow/ClearML or competing model registry.

## Actual recipe execution

The compiler topologically orders canonical nodes and passes each declared edge's native ZenML output into the target step. `dataset.synthetic`, bounded content-addressed `dataset.import`, `dataset.validate`, `model.initialize`, `model.train`, `model.evaluate`, `model.checkpoint`, `model.export`, `model.compare`, and explicitly operator-trusted Python callables have executable dispatch boundaries. The two examples are ordinary model code; they are not a general training framework.

Custom Python imports require both user execution consent and a separately configured trusted source root. A recipe's `trusted: true` flag alone cannot authorize arbitrary code. There is no verified untrusted-code sandbox in this slice; such execution fails closed. Local process supervision is not described as untrusted isolation.

Source files, Git commit bytes when a Git checkout is available, resolved dependency hashes, installed workload source, Python version and exact package versions are checked before launch. Unsupported/private unresolved dependencies and GPU requirements on the CPU target are rejected. Worker environments exclude application tokens, provider credentials and inherited Python paths. Installed runtime wheels import normally without a cross-environment `PYTHONPATH`.

## Cancellation: important ZenML 0.97 static-pipeline limitation

ZenML's static pipeline state computation derives `failed` from an interrupted failed step. Passing `ExecutionStatus.CANCELLED` to the public store does not yield a native cancelled static pipeline, and finished runs cannot be changed to another terminal state. The adapter does not patch that computation or falsely label the native record.

The explicit local supervisor:

1. Receives a cancellation request, lets a fixture atomically save its complete state, then escalates SIGTERM/SIGKILL as needed.
2. Waits for the worker and every runnable member of its owned process group to terminate, including children after their leader exits.
3. Preserves the real ZenML terminal status (`backend_status: failed` for an interrupted static run).
4. Writes the verified termination facts into that native run's metadata with the public `log_metadata` API.
5. Returns effective operation `status: cancelled` only when those native metadata facts confirm cancellation and the owned process group is absent. `backend_status` remains visible separately. A request alone remains `cancel_requested`.

A worker SIGKILL without a cancellation request is a failure, never cancellation. Termination is checked using PID birth identity, not a bare PID. If a supervisor dies while its original worker remains alive, a restarted service can regain control only after verifying the original worker identity. There is no claim that a malicious trusted program escaping into a new OS session is safely isolated; untrusted code remains disabled.

The application can restart without killing a detached supervisor. A per-job launch lock inherited by supervisor and worker fences launch ownership. A durable spawn-attempt marker precedes process creation; matching retries reconcile the existing intent instead of relaunching it. Missing process/native metadata, including after an interrupted or failed creation attempt, does not prove that no training launched. Uncertain attempts remain inspectable and are never automatically respawned. Native store initialization is serialized with bounded lock acquisition outside the service writer transaction. See [run admission and dispatch](admission-dispatch.md) for the exact authorization, cancellation and restart boundaries; there is no cross-store exactly-once guarantee.

## Checkpoint and model lineage

Training saves model/reference tensors, optimizer, scheduler, Torch RNG, Python RNG, minibatch permutation, cursor and epoch after each update. Exact continuation checks architecture, consumed dataset identity and the entire training contract before restoring them. The exponential schedule is horizon-independent. A requested learning-rate/batch/KL change must use the separate warm-start mode.

A killed or cancelled training step cannot return its normal output. After verified termination, the supervisor registers its atomic recovery checkpoint with native `save_artifact`, associates it with the native model version, and records its UUID on the native parent run. A continuation records the actual parent run UUID and checkpoint artifact UUID; a new immutable revision/digest of the same project recipe identifies the changed final step count.

Selection compares actual validation artifacts from completed native runs and verifies that the promoted model version wins the declared metric/direction. Held-out metrics are not used implicitly. Parent links and the selection decision are stored on the native model version.

## Reproduction and evidence

- `scripts/bootstrap_backend.sh`: hashed independent backend lock; CPU Torch index explicit
- `.venv-backend/bin/python -m pytest tests/integration/test_cpu_fixtures.py`: deterministic standalone model checks
- `.venv-backend/bin/python -m pytest tests/integration/test_native_lifecycle.py`: real DAG/artifact/model, negative/failure, cancellation, crash, checkpoint, pretrained selection and parent lineage checks
- `tests/service/test_real_backend.py`: same workflow through shared authorized operations
- `tests/integration/test_mcp_training.py` (when present): native MCP/service process-restart evidence
- `evidence/backend/`: actual IDs, metrics, logs and JUnit outputs

Initial failures remain documented as failures rather than being counted as passes. The evaluated 0.96.4 source checkout is not this runtime: all adapter acceptance uses the installed 0.97.0 release. This is a conditional go for the trusted local CPU vertical slice, not approval for untrusted recipes, GPU/cloud spending or production deployment.

### Cancellation before a native run exists

A trusted Python module can be interrupted during import, before ZenML creates a pipeline run. The supervisor now durably records its wait result, original worker identity, runtime identity and positively empty process group. In that case, status returns `cancelled` with `backend_run_id: null`, `backend_status: null`, and `termination_record_source: supervisor_journal`. It never invents a native run. Missing or uncertain process evidence remains pending/unknown. A prior observed worker failure cannot be reclassified by a late cancellation request.

Continuation is rejected while the parent worker is running, while cancellation is pending, or while process state is unknown. Once termination is reconciled, continuation still requires a real native checkpoint; a pre-pipeline cancellation has none and is rejected clearly.

## Bounded native value-stage caching

Explicitly cache-enabled JSON-value source stages can reuse completed native
outputs after a downstream failure or cancellation, with exact immutable
execution identity and original native producer provenance. Training,
evaluation, export, Path artifacts and resource/environment isolation remain
outside this cache capability. See [the supported contract and limitations](NATIVE_PIPELINE_CACHE.md).

## Explicit terminal conditions

[Native terminal conditions](NATIVE_CONDITIONAL_EXECUTION.md) add typed boolean
control dependencies to the same canonical recipe and editor. Selected gates
are explicit; native metadata records skipped stages without invented outputs.
This bounded sequential path does not implement loops, conditional downstream
merges, conditional live sidecars, independent-stage concurrency or per-stage
isolation/resources.
