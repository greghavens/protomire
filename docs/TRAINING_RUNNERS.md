# Existing training frameworks through pinned runners

Modelbuilder orchestrates existing training code. It does not introduce an optimizer loop, replace a framework's Trainer, or assume an NLP architecture. A source-owned CNN, regressor, diffusion model, JAX function, Keras model, or custom CLI harness can use the same `component.run` ABI. The existing Transformers and MLX integrations remain the preferred reusable adapters for their documented tasks.

`modelbuilder.runner/v1` adds an optional, explicit interpreter/environment boundary to that ABI. ZenML remains the only step scheduler, artifact registry and retry owner. The existing native attempt supervisor owns cancellation, deadlines and child cleanup. New framework families do not need a platform source edit: supply a trusted, source-pinned wrapper, an exact installed environment receipt and a feature-specific component declaration.

An extension contract is not universal framework acceptance. Matching package metadata proves neither that imports work nor that a training kernel ran. See the operation matrix below.

## Operator-owned environments

Runner admission does not implicitly install packages, resolve dependency conflicts, download models or provision compute. Those are separate managed actions requiring their own explicit authorization, reviewed dependencies and applicable budget controls. No remote installation, model download or cloud action was performed for this acceptance; the additional Accelerate smoke used an independently provisioned temporary environment from existing local hash-locked package caches. The runner itself constructs only a fixed platform command; trusted source-owned code retains its existing execution authority.

The configuration below documents the low-level registration contract. A managed app setup flow can supply the same approved interpreter alias and receipt without requiring users to edit operator JSON. Environment setup UI is separate from this increment and must not infer source trust or spending approval from a framework selection.

Capture a receipt from an existing interpreter:

```sh
python -m modelbuilder.backend.runner --python /approved/venv/bin/python --output runner-environment.json
```

This runs standard-library metadata inspection in isolated Python mode. The destination must be new; an existing file is never overwritten. The printed SHA256 identifies the exact JSON bytes. The receipt contains:

- CPython patch version, operating system and machine architecture
- Interpreter executable SHA256
- The complete, normalized installed distribution/version inventory, including the small ABI dependencies `jsonschema`, `psutil` and `pydantic`

Duplicate distribution metadata, malformed versions and missing ABI dependencies are rejected. The receipt is a metadata/binary identity, not an installed-wheel integrity attestation. Use an independently reviewed hash lock to provision the environment. Wheels, drivers, shared libraries, editable installs, framework bugs and hostile code are outside this receipt's proof. No sandbox or numerical determinism guarantee is implied.

Register the existing interpreter and approved receipt digest in the operator configuration:

```json
{
  "runner_environments": {
    "my-training-env": {
      "python_executable": "/approved/venv/bin/python",
      "profile_sha256": "<SHA256 printed by the capture command>"
    }
  },
  "dependency_paths": {
    "my-training-env": "/approved/receipts/runner-environment.json"
  }
}
```

Relative operator paths are relative to the operator configuration file. Interpreter symlinks are preserved so selecting a venv never silently selects its base environment. Recipes cannot set these executable paths or approve a new profile. Changing installed packages, interpreter bytes, OS, architecture or Python version requires a new receipt and explicit operator registration, followed by an immutable recipe revision.

The recipe declares an ordinary SHA256-pinned resource with `kind: "environment"`, `id: "my-training-env"`, the same `sha256`, an inert URL such as `urn:sha256:<digest>`, and the appropriate license. The node's existing `environment_ref` selects that ID. The default ZenML environment still owns orchestration and native materialization; independent component environments need not contain ZenML or Torch.

Planning validates the exact receipt against installed metadata and checks the source trust/commit/closure and all declared dependencies. It performs no source imports, training or writes to the run journal. Submission repeats admission. The native child rechecks receipt bytes, actual environment, stack versions, dependencies and source closure before importing the wrapper. A plan is not a promise that a future changed environment will execute.

## Versioned adapter descriptor

A source-bound component may add this `runner` field. This example names the versions actually exercised by the existing CPU Trainer tests; it is not a recommendation to upgrade or a promise that every model works with them.

```json
{
  "protocol": "modelbuilder.runner/v1",
  "stack": [
    {"id": "pytorch", "category": "framework", "distribution": "torch", "version": "2.9.0+cpu"},
    {"id": "transformers", "category": "training-library", "distribution": "transformers", "version": "4.57.1"},
    {"id": "accelerate", "category": "launcher", "distribution": "accelerate", "version": "1.11.0"}
  ],
  "launcher": {"kind": "python", "processes": 1},
  "contracts": {
    "model": "source-owned-native",
    "dataset": "native-component-inputs",
    "checkpoint": "modelbuilder.checkpoint/v1",
    "evaluation": "modelbuilder.evaluation/v1"
  }
}
```

The stack is extensible rather than a framework allowlist. Each stack distribution must match the selected receipt exactly. Multiple libraries can share a distribution only when their pins agree. Stack IDs are unique. Categories distinguish tensor frameworks, higher-level training libraries, fine-tuning libraries, launchers and distributed strategies. For example, PEFT configures trainable adapters; it is not itself a process launcher. FSDP is a distributed strategy in PyTorch, not an independently installed training framework.

A launcher dependency does not authorize its CLI. v1 invokes one platform-owned Python process with a fixed argument vector and `-I -B`; there is no shell interpolation, arbitrary recipe module, install command, or interpreter fallback. The trusted source wrapper can call its existing harness using its own pinned code. Such code retains the backend user's privileges and must honor the declared CPU and process policy. CPU declarations and installed metadata do not prove what a trusted harness actually executed. Network I/O is not independently sandboxed here; source-owned code may perform I/O within its available authority. Native deadlines/cancellation bound owned attempts but do not reserve memory/CPU or authorize provider spend. This is a pinned environment identity and process-lifecycle boundary, not hostile-code confinement or a hardware reservation.

## Model, dataset, checkpoint and evaluation contracts

- Models remain native source-owned code and files. A model architecture, loss, initialization-from-scratch, optimizer, and training algorithm belong to the wrapper's pinned source closure or exact installed libraries. No pretrained weights, tokenizer, text column or tensor layout is required.
- Inputs are the normal native typed component artifacts. Use registered train/evaluation split artifacts where appropriate; preserve their sample/revision/content identities. File inputs are content-hashed and dictionary inputs are canonically bound into checkpoint data identity. An input hash alone does not prove absence of train/test leakage: the harness must use the declared splits correctly.
- Training must return at least one `Path` output with the existing verified `checkpoint.manifest.json`. It supplies actual `context["identities"]`, native asset hashes, step, purpose and supported resume modes. No arbitrary framework objects are unpickled by the platform.
- v1 permits explicitly declared weights warm start or no resume. It rejects `exact_state` even if an upstream framework has a restore API. Neither a Transformers model save, a PEFT adapter nor a Lightning weights-only checkpoint proves optimizer/RNG/scheduler/data-cursor continuity. CPU bit-for-bit fixture tests are not portable hardware determinism guarantees.
- Evaluation must return at least one metrics dictionary with valid immutable `modelbuilder.evaluation/v1` records. Missing dataset, split, protocol, metric, treatment or digest evidence is rejected rather than synthesized. Downstream evaluation can run in a different registered environment; its inputs remain the connected native artifacts.
- Export retains the existing native manifest, selected checkpoint digest, asset hashes, source/dependency identity and explicit fresh-loader parity evidence. Format support is per adapter. A source route does not make every upstream exporter available.
- Logs and final checkpoint artifacts use existing native execution telemetry. v1 rejects cross-environment live evaluation; it never invents intermediate checkpoints. Existing default-environment live-evaluation integrations are unchanged.

See [source-bound components](source-bound-components.md), [Transformers Trainer](TRANSFORMERS_TRAINER.md), and [local placement limits](LOCAL_DEVICE_PLACEMENT.md).

## Feature/version/platform matrix

These rows describe this repository's evidence, not all features advertised upstream. All newly executed training here used Linux x86-64 CPU, CPython 3.12.14, no network model fetch, no GPU and no paid compute. Optional uninstalled frameworks were not installed merely to fill a matrix.

| Stack | Kind | Train / evaluate | Generate / export | Resume | Platform and lifecycle evidence |
|---|---|---|---|---|---|
| PyTorch 2.9.0+cpu | Tensor framework | Source-owned scratch linear regression executed across native runner environments; existing CNN/GRU component tests remain | Existing source-owned native export route; no universal export | Runner weights warm start tested; runner exact state rejected | Linux x86-64 CPU; distinct child interpreters, native artifacts and process-tree cancellation tested |
| Transformers 4.57.1 + Accelerate 1.11.0 | Trainer library + execution helper | Existing offline classification and causal-LM Trainer adapter tests; custom losses/harnesses via source ABI | Existing task-specific generation/inference and safetensors export with fresh-process parity | Existing adapter weights warm start; no exact-state claim | CPU tests executed; CUDA/MPS declarations require existing reviewed placement profiles and separate hardware acceptance |
| Accelerate 1.11.0 | Execution helper / launcher | Actual standalone `Accelerator.prepare`, accumulation and backward smoke made two CPU AdamW updates on a scratch MLP through the runner | Verified native safetensors checkpoint; no universal exporter or generation promise | Weights-only checkpoint; exact state rejected | Separate environment built offline from existing reviewed hash locks; one process and cleanup verified. Launcher CLI/distributed execution unverified |
| TRL | Post-training library | Source-wrapper route for a specifically pinned trainer/task, such as SFT or DPO; not installed/tested here | Framework-specific wrapper required | No exact-state claim | No accepted version/platform row yet; RL rollouts or vLLM workers need their own lifecycle/resource acceptance |
| PEFT | Fine-tuning library | Source-wrapper composition with a trainer; not installed/tested here | Adapter weights differ from a complete base model; wrapper must pin base model and adapter config | Adapter save alone is weights only | No accepted version/platform row yet |
| Diffusers | Diffusion models/training scripts | Source-owned training script/Accelerate API route; not installed/tested here | Image generation and pipeline export require task-specific wrappers/tests | No inferred exact resume | No accepted version/platform row yet |
| sentence-transformers | Embedding training library | Source wrapper around its Trainer/loss/evaluator; not installed/tested here | Embedding inference/export requires corresponding wrapper | No inferred exact resume | No accepted version/platform row yet |
| Lightning | Trainer/library | Source-owned `LightningModule` and `Trainer.fit`; not installed/tested here | Task-specific predict/export wrappers | Its checkpoint API does not automatically prove this contract | CPU single-process source route; distributed strategies unverified |
| Axolotl | Configuration-driven training application | Source wrapper around reviewed pinned configuration/entrypoint; not installed/tested here | Its inference/merge functions require separate wrappers | No inferred exact resume | CLI defaults/model downloads/distributed behavior must be bounded by wrapper; no accepted row |
| torchtune | Recipe-driven training library | Source wrapper around an installed pinned upstream recipe; not installed/tested here | Task-specific adapter required | No inferred exact resume | Single-device recipes still need acceptance; distributed recipes not admitted |
| DeepSpeed / FSDP | Distributed engine / PyTorch strategy | Not admitted by runner v1 | Not admitted by runner v1 | No sharded-state continuity claim | No multi-process, multi-node or accelerator acceptance; source descriptor rejects distributed strategy |
| JAX / Flax | Tensor framework / model-training library | Extensible CPU source route; not installed/tested here | Native inference/export wrapper required | RNG, optimizer and checkpoint format owned by harness | No TPU, GPU, multi-host or accepted version row |
| TensorFlow / Keras | Tensor framework / training API | Extensible CPU source route around `Model.fit` or custom loop; not installed/tested here | SavedModel/Keras export and prediction need wrappers/tests | Model save is not an exact-resume proof | No accepted version/platform row |
| MLX | Apple tensor framework | Existing dedicated reviewed bridge and source ABI, unchanged | Existing adapter-specific operations, unchanged | Existing bridge limits apply | Linux boundary tests only in this pass; requires separate macOS ARM64 interpreter and hardware acceptance |

The machine-readable equivalent is returned under `execution.targets.capabilities.runner_compatibility`. There is deliberately no universal `supported: true` flag. Existing catalog entries retain their task-specific declarations. Framework smoke results must be recorded with exact environment receipt, source, operation, dataset/model fixture, device, checkpoint mode and cleanup evidence before extending accepted claims.

## Local verification

`tests/components/test_runner_contract.py` covers inert descriptors, operator receipt binding, full environment drift, unsupported launchers/platforms/devices, exact-resume rejection, fixed commands, before-import source/dependency rechecks, actual framework-neutral child execution, invalid evaluation evidence and owned descendant cancellation.

`tests/components/test_runner_native.py` starts a real ZenML graph through the service. A source-owned PyTorch scratch model trains in one existing interpreter; data and held-out evaluation run in another, without importing Torch. It verifies native artifact IDs, checkpoint hashes/identities, parameter changes, interpreter PIDs, cleanup and explicit warm-start lineage. Set `MODELBUILDER_RUNNER_TEST_PYTHON` to an already prepared CPU backend interpreter when it is outside the checkout. The test does not install or invoke remote compute.

`tests/components/test_runner_accelerate.py` executes the actual single-process Accelerate API on a source-owned scratch model, outside Transformers Trainer. It verifies two optimizer updates, changed weights, a native safetensors checkpoint and cleanup. This test is selected by the existing native `cpu-components` group; it is not run as a light unit test. Acceptance used a separate environment provisioned offline from the already reviewed `backend-linux-x64.lock` and `trainer.lock`, leaving the configured backend environment unchanged.

## Upstream contract research

Official upstream sources were inspected on 2026-10-13. They motivate wrapper boundaries, not tested-version claims:

- [Transformers Trainer 4.57.1](https://huggingface.co/docs/transformers/v4.57.1/en/main_classes/trainer): Trainer owns optimization, task-specific metrics and its checkpoint API.
- [TRL](https://huggingface.co/docs/trl/en/index) and [PEFT](https://huggingface.co/docs/peft/en/index): post-training algorithms and parameter-efficient adapters compose with existing models/trainers. [PEFT checkpoint format](https://huggingface.co/docs/peft/developer_guides/checkpoint) distinguishes adapter state from the base model.
- [Accelerate launch](https://huggingface.co/docs/accelerate/en/basic_tutorials/launch) and [Diffusers training](https://huggingface.co/docs/diffusers/en/training/overview): launch configuration and task-specific training scripts are separate concerns from a model library.
- [Sentence Transformers training](https://sbert.net/docs/sentence_transformer/training_overview.html): training integrates model, data, loss, arguments and evaluator.
- [Lightning Trainer source documentation](https://github.com/Lightning-AI/pytorch-lightning/blob/master/docs/source-pytorch/common/trainer.rst): user-owned modules integrate with fit/validate/test and explicit accelerator/strategy controls.
- [Axolotl quickstart](https://docs.axolotl.ai/docs/getting-started.html) and [torchtune first fine-tune](https://docs.pytorch.org/torchtune/main/tutorials/first_finetune_tutorial.html): configurations/recipes drive upstream training. Their download and distributed defaults must not be silently inherited by a generic adapter.
- [DeepSpeed getting started](https://www.deepspeed.ai/getting-started/) and [Transformers distributed integration](https://huggingface.co/docs/transformers/accelerate): engines and sharding/launcher settings require explicit distributed lifecycle acceptance.
- [Flax quick start](https://flax.readthedocs.io/en/latest/quick_start.html) and [TensorFlow training/evaluation](https://www.tensorflow.org/guide/keras/training_with_built_in_methods): framework-native state, models and custom objectives remain outside the platform's ABI implementation.

## Declared-input rights admission

Execution authority is a separate gate from source trust, provider access, downloads, and environment identity. The service attaches an immutable project policy snapshot to a request-local backend view through `with_execution_rights(project_id=..., policies=..., policy_store_path=...)`. Recipes and operation callers cannot provide this authority or choose its database path.

Known training, evaluation, and export roles request `train`, `evaluate`, and `export`. A generic asset-using component must declare `node.provenance.intended_use`; a declaration conflicting with a known role is rejected. These are operational actions, not a complete legal purpose. Governed inputs also require `recipe.extensions.usage_context` with explicit purpose, commercial status, deployment, and conditions exactly matching every authoritative reviewed policy. Model/dataset/checkpoint inputs from local sources have the same requirements as Hub inputs. Labels, access tokens, source trust, and download receipts do not grant permission.

The native request binds selected source/resource bytes, requested actions, context, policy revisions, and service-store identity. Admission checks selected registered inputs and the owned native reader's immutable input-rights projections; continuation checks its actual checkpoint. Before launch and source import, the worker opens the captured service store read-only and requires the same project-owned policy bodies and revisions. Changed, revoked, missing, mismatched, or unresolved policies block. In-memory review alone cannot authorize native asset execution because the worker cannot recheck it. Asset-bearing cached steps are currently rejected; fresh dispatch is required.

A scratch/utility run with no declared model, dataset, or checkpoint inputs records `no_declared_external_asset_inputs`. This is not an assertion that all possible inputs are legally cleared. For later asset consumption, generated outputs must carry source-authored local-origin/parent provenance and explicit reviewed lineage. The source receives a safe `rights_admission` decision and, when explicitly declared, `declared_artifact_rights`; the platform does not invent output copyright ownership or licenses. The existing trust boundary still permits source code to perform undeclared I/O. This gate inspects declared/connected inputs and does not claim an I/O sandbox or automatic mid-run revocation cancellation.

`tests/components/test_execution_rights.py` runs in both the light component selection and the aggregate native `cpu-components` group. It checks exact asset/action/context matching, payload spoof rejection, immutable authority, missing/revoked/stale policy revisions, store replacement, before-import rejection, native binding projections, and promotion/export SDK receipt rechecks. The native scratch test now supplies explicit local-origin review evidence and verifies checkpoint rights while preserving cross-environment training, evaluation, cleanup, and warm start.

The current generic profile remains CPython 3.12 on Linux CPU with one Python process. A Python 3.11 project environment, including the separately audited Biff environment, is not yet admitted or runtime-tested by this route.
