# Bring an existing model or training harness

Modelbuilder runs source-bound components as native ZenML steps. Components wrap your existing PyTorch, Transformers, Lightning, Accelerate, or other installed framework code. The platform does not provide or replace your optimizer loop. A tiny wrapper around a current Python or CLI harness is sufficient; no Modelbuilder source edit is needed.

## Source admission

Configure an operator-owned source root and explicitly trust it. A recipe records its source Git commit, every source file's SHA256, exact Python/package requirements, and resolved resource/dependency hashes. The configured checkout must match the commit and entire declared source closure. Opening/importing a recipe only validates data. Planning checks the same admission gates as submission and never imports model code, installs dependencies, or starts training. Runtime rechecks source and dependency bytes before loading a component. It stages only declared source files into a private execution snapshot, removes normal imports from the original checkout and previous source snapshots, and supplies that snapshot as the wrapper source root. An undeclared sibling is not implicitly available just because it exists in the checkout.

This is trusted local execution, not a sandbox for hostile code. Components can use the powers of the backend process. Untrusted code remains blocked. A changed dependency, unresolved source, unavailable environment, unsupported resource request, or undeclared live-checkpoint capability is a planning/submission failure.

## Versioned component declaration

A `component.run` node has the normal pinned Python `implementation_ref` and a `component` object:

```json
{
  "id": "my-repository.training",
  "version": "1",
  "protocol": "modelbuilder.component/v1",
  "role": "training",
  "parameter_schema": {
    "type": "object",
    "properties": {"learning_rate": {"type": "number", "minimum": 0}},
    "required": ["learning_rate"],
    "additionalProperties": false
  },
  "capabilities": {
    "frameworks": ["pytorch"],
    "resume_modes": ["exact_state", "warm_start"],
    "export_formats": [],
    "checkpoint_events": true
  }
}
```

Roles are `model`, `dataset`, `training`, `evaluation`, `export`, and `utility`. All capabilities are explicit; an empty resume list means no resume. The catalog is derived from the same immutable recipe, keeping one model/project and one evolving recipe identity. `component.list`, `component.get`, and `component.validate` expose the declarations through the common service/API/MCP contract. The versioned `modelbuilder.examples` fixture family remains available for regression examples.

Parameter schemas support bounded primitive/object/array/composition controls. References and regular expressions are intentionally unsupported; schemas cannot retrieve remote or local files. Native ports support `dict`, `Path`, `str`, `int`, `float`, and `bool`; the `DatasetArtifact`, `CheckpointArtifact`, `EvaluationArtifact`, and `ExportArtifact` JSON aliases are also available. File artifacts remain framework-native files/directories managed by ZenML; the platform never unpickles arbitrary user objects.

## Wrapper ABI

```python
from pathlib import Path

def run(*, inputs: dict, parameters: dict, context: dict) -> dict:
    destination = Path(context["output_dir"])
    # Invoke the existing harness with its normal model/loss/optimizer configuration.
    # Write its native artifacts and the appropriate descriptor under destination.
    return {"trained": destination}
```

The keys and types returned must exactly match the node's declared outputs. `inputs` are the actual connected native artifacts. `parameters` are the immutable canonical recipe values. `context` supplies the output directory, source/dependency identities, resolved dependency paths, cancel file, and an explicit resume descriptor when resuming. It also supplies the selected `device`, `placement` metadata and effective `resume_modes`. Training components must declare supported device families and honor this context; omitted device capabilities mean CPU only. These declarations and placement probes do not prove that trusted component code used the selected device. Training-state identities include architecture/configuration, source closure, dependency closure, training configuration, environment, and data input manifests. Only trusted source is imported, after revalidation, inside the step worker.

A model factory, custom loss, or optimizer may be configured by the wrapper and imported from its own pinned source closure. Third-party library calls remain in that wrapper or the existing harness. There is no mandatory architecture, task, tensor layout, loss, training algorithm, checkpoint filename, or repository template.

## Checkpoints and continuation

A training `Path` output is a directory with `checkpoint.manifest.json`:

```json
{
  "schema_version": "modelbuilder.checkpoint/v1",
  "purpose": "last",
  "step": 12,
  "identities": {"architecture": "...", "source": "...", "dependencies": "...", "training": "...", "environment": "...", "data": "..."},
  "resume_modes": ["exact_state"],
  "state": ["model", "optimizer", "scheduler", "rng", "data_cursor"],
  "assets": [{"path": "my-native-state.bin", "sha256": "..."}]
}
```

Use the execution `context["identities"]` exactly. Asset names are yours; every asset is relative and hash verified. Symlink/escape/missing/tampered assets are refused. `exact_state` requires declared optimizer, scheduler, RNG and data-cursor state plus model state; your framework/harness remains responsible for saving/restoring those values. Weights-only artifacts must not claim exact continuation. Declared state is necessary but cannot override effective `context["resume_modes"]`: accelerator exact-state continuation is rejected, including migration from an accelerator checkpoint to CPU. Only explicitly supported warm start remains available there; see [local placement and continuation limits](LOCAL_DEVICE_PLACEMENT.md). `warm_start` checks architecture compatibility and can begin an explicitly changed stage without representing it as exact continuation.

Generic exact resume uses the parent's unchanged canonical training parameters, including its original final budget. It does not guess that your harness uses a parameter called `steps`. To change a stage, edit the current recipe and explicitly choose warm start. Generic run.resume rejects a fixture-style steps override. The service retains a new immutable recipe revision and explicit parent checkpoint identity.

## Evaluation and live checkpoints

An evaluator returns a metrics dictionary embedding `evaluation_records`, one immutable record per metric. Records identify the actual dataset revision/content, split/sample IDs, protocol, metric implementation/configuration/direction, model/checkpoint/calibration/reference, and optional per-item predictions. See `modelbuilder.evaluation.make_evaluation_record`; emitting the equivalent JSON does not require importing Modelbuilder. Missing identities are unknown, not synthesized. Comparison/promotion uses compatibility checks.

For concurrent evaluation set `recipe.evaluation.live` to `{"enabled": true, "max_concurrency": 1, "timeout_seconds": 300}`. Connect exactly one downstream evaluator whose additional inputs are shared with training. Declare `checkpoint_events: true` and call `context["checkpoint_ready"](path=checkpoint_directory, step=step, purpose="last")` only after complete native state and manifest are atomically published. The coordinator synchronously verifies/snapshots the immutable checkpoint and evaluates it in a bounded child process while training continues. Record `treatment.checkpoint` as the canonical descriptor SHA256 (also supplied as `context["checkpoint_digest"]` to live evaluators). Cancellation reaps child work and preserves completed records. No callback means no invented checkpoint progress.

For the versioned fixture examples, `checkpoint_interval_steps` controls publication cadence (1–100000, default 1); native recovery state is still saved every completed update. Generic harnesses declare their own cadence controls in their parameter schema.

## Local export

An export `Path` output has an `export.manifest.json`, format-specific loader, dependency/source identity, input/output signature, selected checkpoint digest, asset hashes, and recorded reload/prediction parity tolerance/result. The selected digest must match a connected checkpoint descriptor. The platform validates assets and selection rather than assuming latest weights or `best.pt`. The harness owns its framework-specific export and reload check; a fresh process can execute its loader without Modelbuilder. Export remains local; publication requires separate authorization.

## Executed acceptance and limits

`tests/components/test_native_components.py` creates a separate committed source repository from independent test-only code. It trains residual convolution segmentation models at two widths using BCE+Dice, and a variable-length GRU forecaster with quantile loss, AdamW and StepLR. It checks actual parameter changes, native ZenML artifacts, evaluation, local export, and fresh-process exact prediction parity. Additional tests compare complete optimizer/scheduler/RNG/cursor state after interrupted continuation and verify a changed custom loss changes weights. Service-level tests exercise source admission, cancellation, descriptor recovery, exact resume and export using the same evolving recipe. Live tests require completed evaluation before the final checkpoint publication.

These are small CPU extensibility/correctness tests, not scientific quality claims. They do not establish hostile-code isolation, paid-cloud execution, arbitrary dependency installation, or universal framework compatibility. Component declarations describe the adapter's implemented capabilities; framework-specific correctness still needs its own tests.

## Reusable local generation and judge adapters

The catalog also ships `modelbuilder.local.generate`, `modelbuilder.local.judge`, and `modelbuilder.local.prepare-dataset`. These are real bounded Transformers adapters for pinned local GPT-2 generation, separate BERT classification predictions and explicitly reviewed train-only dataset preparation. Recipe → Browse reusable components inserts a declaration into the same recipe draft; the ordinary real source pin, dependency configuration and trusted run gates remain mandatory. See [Local generation and judges](LOCAL_GENERATION_AND_JUDGES.md) for exact supported scope, provenance, isolation, review and tests. Generated data and judgments do not automatically enter training or promotion.

## Reusable standard training adapters

`modelbuilder.transformers.train`, `.evaluate`, and `.export` wrap the installed Hugging Face Trainer, task-specific AutoModel classes, safetensors and existing native ZenML operations. User-bound configuration, weights, tokenizer, registered eager or streaming train splits, explicit task/metric choices and resource budgets replace fixture-specific assumptions. Their project wrapper is one import, while the framework continues to own optimization. See [Transformers Trainer](TRANSFORMERS_TRAINER.md) for the optional reproducible backend profile, implemented CPU/CUDA/MPS selection, CPU verification evidence, warm-start limits, exact held-out identities and standalone export loader. Existing arbitrary source-owned models/losses/harnesses remain supported through the same component ABI.
