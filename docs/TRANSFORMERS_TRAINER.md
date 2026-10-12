# Reusable Transformers training, evaluation and export

Three standard library components extend the existing typed recipe catalog:

- `modelbuilder.transformers.train`: installed Hugging Face `Trainer` runs optimization, batching, gradient accumulation, AdamW and the selected scheduler on a pinned user model and a registered train split.
- `modelbuilder.transformers.evaluate`: evaluates the selected checkpoint on registered validation/test data, recording immutable item-addressed accuracy, cross-entropy or perplexity evidence.
- `modelbuilder.transformers.export`: exports a local safetensors/config/tokenizer package and an independent Python loader, with actual fresh-process logits comparison against the selected checkpoint.

These are ordinary source-pinned `component.run` nodes compiled by the existing ZenML adapter. One project still owns one model and one evolving recipe. There are no alternate run stores, new training framework, source-import selectors or separate workflows. Catalog inspection imports no Torch, Transformers or model code. `examples/transformers_training/adapter.py` is the ordinary one-line project wrapper; it delegates to these reusable implementations rather than asking each project to implement an optimizer loop.

## Reproducible installation

Use the existing optional backend profile:

```sh
python3.12 scripts/bootstrap.py --backend --trainer
# Or extend only an existing backend:
python3.12 scripts/bootstrap.py --backend-only --trainer --skip-model-download
```

The versioned, hash-locked `requirements/profiles/trainer.lock` adds `accelerate==1.11.0`. Transformers 4.57.1, Torch 2.9.0, Tokenizers and safetensors already belong to the existing backend profiles. Installation is wheel-only, hash-required and no-dependencies, then checks the full backend dependency closure. It never upgrades the frozen base profile or adds model libraries to the service/MCP environment. `--trainer` requires a backend flag. A later backend sync without `--trainer` removes the optional package. Missing Accelerate fails execution with an explicit optional-profile diagnostic; a catalog entry is not evidence of installation.

Regenerate the pure-Python overlay with pinned uv:

```sh
uv pip compile requirements/trainer.in --no-deps --generate-hashes --only-binary :all: -o requirements/profiles/trainer.lock
```

The existing source/dependency/environment admission remains mandatory. Declare the actual exact installed framework requirements in the recipe, including `accelerate==1.11.0`. No installation occurs during planning or execution. Native runtime verification in this change is Linux x64 CPU; dependency metadata on another target is not an execution pass.

## User-bound models and data

`architecture.task` explicitly selects `sequence-classification` or `causal-lm`. The installed Transformers `AutoConfig` / `AutoModelForSequenceClassification` / `AutoModelForCausalLM` mapping selects the architecture from the pinned local configuration. There is no hard-coded BERT/GPT-2 model family or fixture parameter ceiling. Tests use independently created BERT, DistilBERT, GPT-2 and Llama models; installed-class compatibility beyond those tests remains subject to each model's supported text input/loss interface.

Model assets are three already-admitted local resource dependencies: full `config.json`, one safetensors weights file, and Rust Tokenizers `tokenizer.json`. Each parameter reference includes `dependency_id` and its exact SHA256. The wrapper snapshots and rehashes the admitted bytes before loading. Models are constructed from installed code only; it does not retrieve Hub assets, call `from_pretrained`, execute remote Python, load pickles, or install packages. Quantized configurations and `auto_map` custom code are rejected. Encoder-decoder, multimodal, sharded-checkpoint, PEFT and custom-loss tasks continue to use the existing source-owned component ABI. Unsupported installed architecture/optional dependencies fail visibly.

The implemented adapter honors the recipe’s explicit single-process CPU, CUDA visible ordinal or MPS selection in float32, with one Torch CPU thread. It preconfigures Accelerate state and uses a version-pinned device-initialization bridge to prevent Trainer from touching CUDA0 or enabling DataParallel, and verifies that the Trainer, Accelerate and model tensors all use the selected device. Missing hardware, an incompatible pinned runtime or unsupported deterministic operations fail without CPU fallback. Only CPU training was hardware-verified here; CUDA/MPS execution, cancellation, memory behavior and numerical quality remain real-hardware acceptance gates. CPU evaluation and exports remain independent of training placement.

Training has one required native dictionary input, `train`, and an optional `validation` input for shared native live evaluation. Bind the user's registered train split through the ordinary dataset binding controls. The registry's dataset revision, source bytes, ordered sample IDs, row hashes and split role are checked before preprocessing. Validation/test data cannot be passed to the trainer. `text_field` and `label_field` select actual user columns. Classification uses an explicit ordered `labels` list matching the model's head; rows may contain one of those names or its integer index. Causal-LM rows use text and model-derived next-token labels, with attention-mask padding excluded from loss. Classification labels are empty for causal LM.

`max_length`, `truncate` and `pad_token_id` are explicit. Persisted tokenizer truncation/padding defaults are cleared. Overlong text fails unless the recipe explicitly allows truncation; context length and vocabulary/padding ranges are checked. Classification is single-label, two or more classes. Requested regression/multilabel behavior is not silently substituted.

The required `architecture.limits` controls user-declared resource budgets:

```json
{
  "max_parameters": 250000000,
  "max_asset_bytes": 2000000000,
  "max_rows": 50000,
  "max_tokenized_tokens": 25600000
}
```

Those numbers are examples, not platform defaults or hard limits. A meta-tensor construction counts parameters before allocating CPU weights. These checks complement existing operator admission and process resource limits; they do not guarantee that a given model/training state will fit available RAM. Increase a budget only for an appropriately provisioned backend. Eager preprocessing stores the registered split’s tokenized features within the declared row/token budget. Streaming train splits instead use the registry’s verified, manifest-backed iterator inside a framework `IterableDataset`, with one-row preprocessing, sequential source order and no in-memory corpus. The explicit final `max_steps` supports finite-budget streaming. Row limits still apply to the selected split; `max_tokenized_tokens` bounds eager materialization, or the configured batch/gradient-accumulation/lookahead window for streaming. A small `Trainer.training_step` hook observes exact delivered IDs/content hashes and delegates the complete optimization step to the framework. It records compact ordered consumption evidence at completed checkpoints, excluding DataLoader/Accelerate lookahead and retaining repeated epochs.

Required training parameters outside `architecture` are `seed`, `max_steps`, `batch_size`, `gradient_accumulation_steps`, `learning_rate`, `weight_decay`, `warmup_steps`, `lr_scheduler_type`, `max_grad_norm` and `checkpoint_interval_steps`. Schedulers are installed Trainer `linear`, `cosine`, `constant` or `constant_with_warmup`; no bespoke optimizer loop is implemented. Parameters not in the versioned strict schema are rejected. Logging integrations and Hub publication are disabled; existing Modelbuilder tracking remains the authorized execution integration.

## Checkpoints, cancellation and warm start

At completed optimizer-step boundaries, the Trainer callback saves normal safetensors/config/tokenizer state plus its JSON progress into a new immutable checkpoint directory. A complete manifest is published only after every asset has closed. The existing `checkpoint_ready` callback enables native live-evaluation scheduling and durable recovery. Cancellation is checked at optimizer-step boundaries and saves the current warm-start checkpoint before raising an interrupted operation; the regular supervisor retains its native/effective cancellation distinction.

Version 1 deliberately advertises only `warm_start`. It saves model state, not complete optimizer, RNG, scheduler and dataloader state, and does not claim bitwise exact continuation. `run.resume` with a compatible checkpoint and explicit warm-start mode uses the same architecture, restores its model weights and creates a new Trainer optimizer/scheduler/budget. A changed learning rate or final budget belongs to the new immutable recipe revision. `exact_state` fails explicitly. Checkpoint `data` identifies the registered input split and records actual batch-boundary row count/ordered content evidence. It does not claim that the complete split or its manifest order was consumed by a shuffled, finite-budget training run. Connect a registered validation split to both the trainer’s optional `validation` input and evaluator’s `data` input for the existing native live-evaluation mechanism; held-out rows never enter the Trainer optimization dataset.

## Evaluation and scientific identity

Connect `trained` plus a registered validation/test dictionary `data` to the evaluator. Choose `batch_size` and an explicit nonempty metric list. Classification supports `accuracy` and `cross_entropy`; causal LM supports token-weighted `cross_entropy` and `perplexity`. Incompatible metrics fail. Padding positions do not contribute to causal loss. Batch logits are reduced before accumulation, avoiding a full dataset-by-vocabulary prediction array.

Every metric records actual dataset revision/content/benchmark, split role/ordered sample IDs, explicit preprocessing, implementation/package version, aggregation/direction, per-item evidence and the canonical connected checkpoint descriptor SHA256. Causal perplexity is the exponential of token-weighted mean negative log likelihood; it is not the mean of per-item perplexities. Comparison and promotion use the existing immutable evaluation compatibility checks, and no train metric implicitly becomes held-out selection evidence.

The current evaluator requires bounded, item-addressed evaluation evidence. A small validation/test split from the same streaming dataset is materialized within the explicit row/token budgets and `max_asset_bytes` decoded-byte budget, retaining its original dataset/version, complete sample IDs and content hashes. It does not materialize the full training corpus or re-register the held-out split. Oversized held-out splits fail before decoding; truly unbounded evaluation requires a future compact evidence protocol rather than invented or omitted sample IDs. Metrics and tiny test losses are correctness evidence, not scientific model-quality guarantees or evidence of train/test semantic decontamination.

## Portable local exports

An export receives the explicitly connected `trained` checkpoint and requires `parity_texts`, nonnegative `atol`, and `verification_timeout_seconds`. It copies safetensors, full config, tokenizer and explicit preprocessing; writes `loader.py`; and starts an isolated Python process with offline flags and no project `PYTHONPATH`. The loader imports only ordinary Torch/Transformers/Tokenizers/safetensors, never Modelbuilder. Actual CPU logits must match the selected checkpoint at `rtol=0` and the requested tolerance. The manifest binds every asset, the exact selected checkpoint, task/signature, actual dependency versions and verification result.

After installing the recorded dependencies, consumers can import the exported `loader.py` and call `load(directory)` for the model/tokenizer/settings or `predict(directory, texts)` for logits. The selected checkpoint and input texts, not a guessed latest/best weight file, determine the verification. Fresh-process parity is tested for the recorded runtime and samples; it is not universal cross-device or cross-version equivalence. Exports stay local and do not publish weights or accept license agreements.

## Executed tests

```sh
MODELBUILDER_REQUIRE_TRAINER=1 PYTHONPATH=src .venv-backend/bin/python -m pytest tests/components/test_transformers_training.py
.venv/bin/python -m pytest tests/portability/test_trainer_profile.py
```

An additional required-hardware gate is `MODELBUILDER_REQUIRE_TRAINER_DEVICE=cuda:0 MODELBUILDER_REQUIRE_TRAINER=1 PYTHONPATH=src .venv-backend-cuda/bin/python -m pytest tests/components/test_transformers_training.py -k required_accelerator` after installing the CUDA profile with `--trainer`. Use `mps` on the pinned macOS backend. Setting the selected device makes missing hardware a failure; an unset device skips that separate gate. This environment had no accelerator, so that gate is unverified.

The explicit requirement variable turns a missing optional model dependency into failure; ordinary service-only tests can skip model execution. Tests build independent user weights and registered datasets, verify actual parameter changes across several installed architectures, compute held-out evidence, preserve cancellation/warm-start snapshots, reject invalid inputs/resource budgets, and reload local exports in a fresh process. The native integration executes dataset imports → Trainer → evaluation/export as actual ZenML steps and rereads native metric artifacts in another process. No model download, paid API, hosted CI, external tracking upload, or scientific performance claim is needed.

Primary API references: [pinned Transformers Trainer](https://huggingface.co/docs/transformers/v4.57.1/en/main_classes/trainer), [pinned Auto classes](https://huggingface.co/docs/transformers/v4.57.1/en/model_doc/auto), and the installed public APIs at those pinned versions.
