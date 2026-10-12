# General local generation and model judges

The ordinary reusable component catalog now includes three source-bound inference adapters:

- `modelbuilder.transformers.generate`: installed Transformers `AutoModelForCausalLM.generate` samples actual local model continuations from registered train prompts.
- `modelbuilder.transformers.judge`: a separate installed `AutoModelForSequenceClassification` produces explicit class probabilities and a ranking for review.
- `modelbuilder.transformers.prepare-dataset`: an explicitly reviewed selection becomes a local train-only JSONL/manifest proposal, without registration, binding or training.

These extend the existing Recipe → Browse reusable components authoring controls and shared `component.list`, `component.get` and `component.validate` HTTP/MCP operations. They are ordinary `component.run` declarations with source/dependency admission and native ZenML execution. There is no additional model selector, workflow, training framework, orchestration service or result store. Catalog inspection imports no model libraries and executes no user code. Pin the small wrapper at `examples/transformers_inference/adapter.py` in the project's real trusted source closure, fill the declaration's parameters and ports, and validate/save the normal recipe revision before execution.

The original `modelbuilder.local.*` adapters, their v1 schemas, implementation behavior and bounded GPT-2/BERT compatibility remain intact. New candidates use `modelbuilder.generated-candidates/v2`; new judgments use `modelbuilder.model-judgments/v2`. The general judge can also read valid original v1 candidate artifacts. The new preparation adapter accepts v1 or v2 candidates paired with new v2 judgments, while the original preparation path remains unchanged.

## Models and execution

A model is three local, already-admitted resource dependencies, each with its actual `dependency_id` and SHA256:

```json
{
  "config": {"dependency_id": "generator-config", "sha256": "<actual SHA256>"},
  "weights": {"dependency_id": "generator-weights", "sha256": "<actual SHA256>"},
  "tokenizer": {"dependency_id": "generator-tokenizer", "sha256": "<actual SHA256>"}
}
```

The shared Transformers loader constructs a compatible architecture using installed `AutoConfig`, `AutoModelForCausalLM` or `AutoModelForSequenceClassification` mappings. It is not restricted to GPT-2 or BERT. Actual CPU tests cover GPT-2 and Llama generation, and BERT and DistilBERT classifier judging; compatibility of other installed architectures depends on their standard text generation/classification interface and installed optional dependencies. Unsupported combinations fail visibly.

The implementation snapshots and rehashes assets before loading: full configuration JSON, one safetensors weights file, and a Rust Tokenizers `tokenizer.json`. No Hub download, `from_pretrained`, arbitrary Python import, `trust_remote_code`, pickle load, external inference request or paid API is used. Symlinked assets, changed bytes, `auto_map`, quantized configurations, encoder-decoder task substitutions and incompatible label heads are rejected. Sharded checkpoints, multimodal tasks and custom/generative judge protocols can use the existing user-owned component interface; this reusable family does not claim those interfaces are implemented.

Execution is single-process CPU float32 with eager attention, one Torch thread and deterministic Torch algorithms. Invocation rejects a non-CPU device. CUDA/MPS inference is not hardware-verified or advertised by these entries. Model initialization and inference preserve the caller's Torch RNG, thread count and deterministic settings. Repeatability applies to identical pinned assets, inputs, options and runtime, not universal cross-device or cross-version numerical equivalence.

Generation requires explicit `seed`, `text_field`, `prompt_prefix`, `max_new_tokens`, `samples_per_prompt`, `temperature`, `top_k`, `eos_token_ids`, `max_length`, `pad_token_id`, `model` and `limits`. The framework performs seeded top-k sampling with one beam. Each parent/sample derives a stable seed. An empty `eos_token_ids` disables early EOS termination; an explicit list can stop a sample earlier. Model-owned generation defaults cannot override the recorded decoding settings or the explicit EOS choice. Tokenizer persistence defaults cannot silently pad or truncate. Prompt plus the maximum new tokens must fit `max_length`, and `max_length` must fit the model's declared context when that configuration exposes a context limit.

Judging requires explicit `labels`, `score_label`, `rank_direction`, `batch_size`, `max_length`, `pad_token_id`, `model` and `limits`. Ordered labels must match a single-label classifier's output head. Batched inference yields uncalibrated probabilities, a `predicted_label`, configured probability score and deterministic score/ID ordering. The judge must use different weight bytes from the generator. Model predictions are fallible and are never represented as true labels, successful adversarial attacks, calibrated quality guarantees or human approvals.

## Recipe-owned resource budgets

There are no embedded 20-million-parameter, 256-MiB, 128-candidate, eight-sample or 64-token production ceilings in this family. Every inference invocation must declare positive resource budgets:

```json
{
  "max_parameters": 500000000,
  "max_asset_bytes": 3000000000,
  "max_rows": 20000,
  "max_tokenized_tokens": 20480000,
  "max_text_chars": 65536,
  "max_artifact_bytes": 100000000
}
```

These values illustrate the contract; they are not defaults, machine-capacity recommendations or verified model-scale claims. Limits are part of the immutable recipe/provenance. The shared loader counts model parameters with meta tensors before allocating real model weights. `max_asset_bytes` bounds each admitted model asset. `max_rows` bounds each input split and total generated/ judged rows. `max_tokenized_tokens` bounds each eager split's row count times `max_length`, and generated/judged rows times `max_length`. `max_text_chars` bounds prompt, input and generated text. `max_artifact_bytes` bounds complete serialized candidate/judgment artifacts, including provenance. The judge's own budgets apply independently when consuming a candidate artifact.

Increase budgets only for appropriately provisioned execution. These admission checks complement operator/process limits and do not predict activation memory, KV-cache memory, inference duration or whether a model will fit available RAM. Inputs/outputs are bounded eager native dictionaries; this adapter is not an unbounded generation stream. Explicit cancellation is checked before loading and between generated samples/judge batches; existing worker timeouts/supervision bound the native step.

## Held-out boundaries and evidence

Generation requires three registered dictionary inputs: train `prompts`, protected `validation` and protected `test`. Every split must have valid immutable dataset/consumption lineage and the correct train/evaluation purpose. All belong to one project/benchmark. Sample IDs must be disjoint, and modified rows fail their registered content hashes.

Train prompt texts are checked against validation/test content before inference. Generated full texts are checked again after real generation. NFKC/casefold/whitespace exact matches are withheld and recorded as rejections. Protected texts and literal protected sample IDs are not copied into generated provenance. This is exact normalized-text isolation, not semantic decontamination or evidence that an external model's pretraining never encountered the benchmark.

Each accepted row records parent ID, exact prompt, generated full text, generated token IDs, sample index and exact derived seed. The artifact binds all generator asset pins, options/resource budgets, registered split/revision/consumption identities, source closure, execution identities and actual framework/runtime versions. Judgment artifacts seal predictions to the exact candidate digest, separate model identity, label/ranking configuration and runtime.

The judge retains the `utility` role and `judgments` output rather than the scientific evaluation `metrics` port. Therefore it does not silently enter held-out comparison/promotion. Use the separate checkpoint evaluator for canonical scientific metrics against known registered held-out labels. Ranking never admits generated examples automatically.

## Explicit reviewed preparation

A later preparation invocation needs the exact reviewed candidate and judgment digests, a nonempty explicit `approved_ids` list, review note, dataset name, declared license, approved dataset-root alias, distinct synthetic benchmark identity, and its own `max_rows`, `max_text_chars` and `max_artifact_bytes` limits. A stale digest, unknown selection, inconsistent predictions or the source benchmark identity fails.

Preparation writes content-addressed JSONL, `dataset.manifest.json` and `review.provenance.json` into the native step's output directory. The byte limit applies to their combined size. Paths cannot be symlinked, and different existing bytes are not overwritten. Rows use `judge_predicted_label` and `judge_score`; no field claims ground truth. The manifest has only a train split. Normal explicit `dataset.register`/`dataset.version`, binding and authorized run operations are separate subsequent decisions; preparation itself triggers none of them. The registry rejects this train-only proposal for validation/test evaluation use.

## Verification

```sh
MODELBUILDER_REQUIRE_LOCAL_INFERENCE=1 PYTHONPATH=src .venv-backend/bin/python -m pytest tests/components/test_transformers_inference.py
PYTHONPATH=src .venv/bin/python -m pytest tests/service/test_transformers_inference_catalog.py
```

Tests create independent local safetensors models through installed framework constructors. They run genuine seeded generation, repeatable batched probabilities, changed-seed output, changed-weight scores, 65-token output, nine samples per prompt and 130 candidates beyond the original adapter limits. They cover multiple model families, explicit resource/context/label limits, byte integrity, remote-code/pickle/symlink refusals, no-network operation, EOS behavior, cancellation, registered held-out isolation, generated overlap rejection, exact review, train-only registration and inert source-bound authoring. Native integrations execute registered dataset imports → generation → judge and reload real ZenML artifacts in another process. HTTP/MCP catalog results are identical. Tiny CPU models test general adapter correctness; they are not quality, accelerator or large-model performance claims.

API reference: [installed Transformers 4.57.1 Auto classes](https://huggingface.co/docs/transformers/v4.57.1/en/model_doc/auto) and [generation configuration](https://huggingface.co/docs/transformers/v4.57.1/en/main_classes/text_generation).
