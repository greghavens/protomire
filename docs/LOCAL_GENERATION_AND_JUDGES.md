# Local model-driven generation and judges, version 1

## What is implemented

Three reusable adapters are shipped in `modelbuilder.backend.components.local_inference` and discoverable through the existing project-scoped `component.list` / `component.get` operations, HTTP, MCP, and Recipe → Browse reusable components:

| Component | Native inputs | Output | Implemented behavior |
| --- | --- | --- | --- |
| `modelbuilder.local.generate` | `prompts`, `validation`, `test` dictionaries from registered dataset splits | `candidates` dictionary | Actual seeded GPT-2 causal language-model sampling using installed Transformers/PyTorch |
| `modelbuilder.local.judge` | `candidates` dictionary | `judgments` dictionary | Separate BERT single-label sequence-classification model, explicit label mapping, class probabilities and configurable ranking |
| `modelbuilder.local.prepare-dataset` | `candidates`, `judgments` dictionaries | `prepared` native `Path` artifact | Exact reviewed IDs to JSONL, train-only registry manifest and full provenance sidecar |

These are ordinary `component.run` nodes compiled into existing native ZenML steps. There is no new training framework, orchestration scheduler, data host, chat agent or run registry. No optimizer step occurs in any adapter. A judge is a fallible model prediction, not ground truth, a verified quality metric, an approval, or a promotion decision. Judge output deliberately uses the `utility` role and `judgments` port, not the canonical evaluation `metrics` port; it cannot silently become a scientific comparison/promotion metric.

The shipped wrapper example is `examples/local_generation/adapter.py`. Its functions call the implemented reusable library, rather than asking each project to reimplement inference. The catalog can be inspected without importing Torch, Transformers, model weights or user code.

## Explicit adapter scope

Version 1 supports only:

- CPU float32 inference, deterministic Torch operations, one Torch thread.
- GPT-2 `GPT2LMHeadModel` for generation and `BertForSequenceClassification` for judges.
- A local configuration JSON, local `tokenizer.json` readable by the installed Rust Tokenizers library, and one local safetensors file for each model.
- At most 20 million model parameters, 256 MiB of weights per model, 128 generated candidates, 8 samples per prompt and 64 generated tokens per candidate. Configuration dimensions, configuration/tokenizer bytes and context length are separately bounded.
- Sampling with explicit seed, prompt prefix, temperature and top-k. Every parameter is required and additional parameters are rejected.
- Single-label classification with two through 32 classifier labels, with explicit ordered label names, score label, and highest-first or lowest-first ranking.
- A fixed finite-JSON row schema with `id`, `parent_id`, the complete `prompt`, complete generated `text`, `generated_token_ids`, `sample_index`, and the exact `sampling_seed`.

Unsupported options fail rather than silently adapting: other architectures, AutoModel/Hub IDs, URLs, GPU/device maps, quantized models, remote Python code, pickle/PyTorch `.bin` checkpoints, sharded checkpoints, arbitrary decoding options, silent truncation (including persisted tokenizer truncation settings), multimodal inputs, missing splits, and unavailable dependency assets. The loader instantiates explicit installed model classes and loads safetensors from a private byte-verified snapshot; it never calls `from_pretrained`, imports model-supplied code, or downloads assets. Even an accepted local config with `auto_map` or `quantization_config` is refused.

The supported scope is a first useful library slice, not an extensive standard-ML catalog. A configurable prompt prefix can request a counterfactual or difficult continuation, and the separate classifier can rank candidates for review. There is no claim that a tiny/random model creates scientifically useful counterfactuals, successful adversarial attacks, factual examples, or reliably difficult examples. General adversarial-search loops, semantic decontamination, generative/open-ended LLM judges, arbitrary language-model architectures and calibrated scoring are not implemented here.

## Use the existing source and dependency admission

1. Make the ordinary adapter wrapper available in the project's own source repository using the normal code workflow. Pin its real existing Git commit and file SHA256 through the Code/source workflow. Modelbuilder does not create a source commit or fabricate a hash for a library selection.
2. In Recipe, browse reusable components and choose **Add declaration to draft** with a unique node ID. This inserts a visibly incomplete `component.run` declaration into the same recipe. It does not save, execute, register data or download a model. Choose a pinned wrapper file, its actual module, and `generate`, `judge`, or `prepare_dataset` in the existing node panel. Supply all required parameters, then validate and save a new revision.
3. Declare each config, tokenizer and safetensors file as a SHA256-pinned resource/external dependency in the recipe. Resolve them using the existing operator-owned dependency mapping. Model parameters refer to each asset as `{"dependency_id": "your-declared-id", "sha256": "the-actual-64-character-sha256"}`; these are dependency IDs, never browser paths or model-hub names. Set `adapter` to `gpt2-causal-lm` or `bert-sequence-classifier` as applicable. Required bytes are rechecked both by normal component execution and the local adapter.
4. Register and bind prompt/validation/test splits using the existing dataset operations. Generation accepts only train prompts and protected validation/test splits from the same project and benchmark. The native dictionary contract preserves exact dataset revision, content hash, split IDs, ordered sample identities, row digests and consumption identity.
5. Connect three dataset import outputs to the generator's matching inputs; connect `candidates` to the judge. Configure a separate judge weight file and explicit ordered labels. Preview admission and explicitly run the saved recipe through the ordinary trusted-local execution action.

The catalog includes reusable declarations and any current source-bound node declarations. If multiple entries share a component ID, `component.get` requires a node ID to inspect a particular node; `component.list` retains the unbound library template. All source trust, dependency checks and regular run authorization continue to apply. This is trusted local code, not a sandbox against hostile Python wrappers.

## Reproducibility and isolation

Each accepted candidate contains its exact prompt, parent identity, generated token IDs and stable content-derived ID. The artifact includes the model config/weights/tokenizer dependency pins, source closure, actual package versions, actual runtime-source identity, deterministic CPU settings, exact input split identities and ordered sample-ID digests, all generation options and seed derivation policy. Each prompt/sample receives a SHA256-derived seed. Model construction and inference preserve the caller's Torch RNG and deterministic/thread settings.

Repeatability is tested for the same pinned runtime, assets and inputs; byte-for-byte outputs across different CPUs or library versions are not promised. Runtime/source/environment identities make those changes visible rather than asserting universal determinism.

Train, validation and test sample IDs must be disjoint. Train prompts are checked against protected text before inference. Generated full text is checked against validation/test text using NFKC, casefold and whitespace normalization. Matching candidates are withheld and recorded with a rejection reason. Only protected identities/digests are retained in candidate provenance; protected texts are not copied into generated rows. This is exact normalized-text isolation. It is not semantic contamination detection and cannot detect whether an externally trained model previously saw benchmark data.

The judge validates candidate integrity/schema, performs separate model inference and seals predictions against the exact candidate digest, judge asset digest and label/ranking configuration. Probabilities are uncalibrated model outputs. The ranked list is a review aid; no threshold or ranking admits examples automatically.

## Explicit review and normal dataset versioning

Generation/judging alone produce native artifacts only. Inspect the actual candidates and judgments first. A later explicitly configured `prepare-dataset` node requires:

- Exact candidate and judgment artifact digests that were reviewed.
- A nonempty explicit `approved_ids` list and a review note.
- A dataset name, user-declared license, approved root alias and a new synthetic benchmark identity distinct from the protected source benchmark.

Because these first adapters are deterministic within the same pinned environment, a reviewed recipe revision can rerun generation/judging and add preparation. Stale digests, changed predictions, unknown IDs, schema additions or a reused protected benchmark identity fail closed. The acceptance test executes this second real native pipeline after inspecting the first pipeline's saved artifacts.

Preparation writes content-addressed JSONL plus `dataset.manifest.json` and `review.provenance.json` beneath its own native output directory. The rows preserve `judge_predicted_label` and `judge_score`; no field claims these predictions are true labels. The full sidecar preserves generator/source/seed/prompt/judge/review evidence. Reinvocation does not overwrite different prepared bytes.

An operator must make the prepared files available through an approved dataset-root alias, without changing the declared bytes. The user then explicitly calls normal `dataset.register` or `dataset.version` with the prepared manifest. This creates the ordinary immutable project-scoped dataset revision; registration itself does not start training. A further explicit `dataset.bind` and normal reviewed run would be needed for any actual training. The manifest has one `train` split, so registry purpose gates refuse it as a validation/test evaluation dataset. Existing protected datasets are unchanged. A later user-created merge/relabeling of datasets remains that user's separate reviewed workflow, not an automatic effect of generation.

## Tests and acceptance limits

`tests/components/test_local_inference.py` creates tiny independent GPT-2 and BERT models through mature library constructors, saves safetensors locally, and creates an independently authored registered dataset. No private/pretrained model download, vendor session, credential, external inference, paid API, or training job is needed.

Coverage includes actual seeded generation, a different seed changing output, repeatable judge probabilities, changed judge weights changing predictions, complete model/source/data provenance, no network calls from the adapters, protected ID/text separation and generated-overlap refusal, unsupported/oversized/remote-code/pickle/symlink/tampered asset refusals, stale review refusals, immutable registry versioning and train/evaluation purpose isolation. The integration executes real dataset imports → generation → judge in native ZenML, reloads its actual native artifacts in another process, then runs a second explicitly reviewed revision with preparation and registers the resulting train-only artifact.

The service test checks shared HTTP/MCP catalog parity; frontend tests check inert library insertion, safe text rendering, duplicate/stale recipe refusal and preservation of source-binding requirements. A further acceptance starts from an arbitrary one-file project repository, exercises the compiled library-insertion and source-panel transforms, then uses real HTTP source bind/pin, recipe save, dataset bind, admission and native execution. The test proves the incomplete preset is rejected, the pinned one-line wrapper executes the packaged implementation, no product source tree or source commit is generated by the UI, and later source drift still blocks admission. Compilation and these DOM/helper tests are not a new native Code-OSS GUI acceptance. GPU, external providers, scientific sample quality, sandboxing and arbitrary architectures remain outside this adapter's verified scope.
