"""Bounded, offline CPU generation/judging adapters over installed Transformers.

This module is inert until a trusted source-bound wrapper invokes an adapter.
Weights are safetensors only. No model hub, dynamic imports, pickle or remote code.
Generated examples and model predictions are never ground truth or automatically
registered, bound to training, promoted, or published.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import unicodedata

from jsonschema import Draft202012Validator

from ...generated_artifacts import attach_lineage, validate_lineage
from . import PROTOCOL, identity

VERSION = "1"
CANDIDATES = "modelbuilder.generated-candidates/v1"
JUDGMENTS = "modelbuilder.model-judgments/v1"
MAX_ASSET_BYTES = 256 * 1024 * 1024
MAX_PARAMETERS = 20_000_000
MAX_ROWS = 128
_TEXT = {"type": "string", "minLength": 1, "maxLength": 8192}
_HASH = {"type": "string", "minLength": 64, "maxLength": 64}
_REF = {"type": "object", "additionalProperties": False, "required": ["dependency_id", "sha256"],
        "properties": {"dependency_id": {"type": "string", "minLength": 1}, "sha256": _HASH}}
_MODEL = {"type": "object", "additionalProperties": False,
          "required": ["adapter", "config", "weights", "tokenizer"],
          "properties": {"adapter": {"enum": ["gpt2-causal-lm", "bert-sequence-classifier"]},
                         "config": _REF, "weights": _REF, "tokenizer": _REF}}
ROW_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["id", "parent_id", "prompt", "text", "generated_token_ids", "sample_index", "sampling_seed"],
    "properties": {"id": _TEXT, "parent_id": _TEXT, "prompt": _TEXT, "text": _TEXT,
        "sample_index": {"type": "integer", "minimum": 0, "maximum": 7},
        "sampling_seed": {"type": "integer", "minimum": 0, "maximum": 4294967295},
        "generated_token_ids": {"type": "array", "minItems": 1, "maxItems": 64,
                                "items": {"type": "integer", "minimum": 0}}}}


def _schema(properties, required=None):
    return {"type": "object", "additionalProperties": False,
            "required": list(properties) if required is None else required, "properties": properties}


GENERATION_PARAMETERS = _schema({
    "model": _MODEL, "seed": {"type": "integer", "minimum": 0, "maximum": 2147483647},
    "text_field": {"type": "string", "minLength": 1, "maxLength": 200},
    "prompt_prefix": {"type": "string", "maxLength": 2048},
    "max_new_tokens": {"type": "integer", "minimum": 1, "maximum": 64},
    "samples_per_prompt": {"type": "integer", "minimum": 1, "maximum": 8},
    "temperature": {"type": "number", "minimum": 0.01, "maximum": 5},
    "top_k": {"type": "integer", "minimum": 1, "maximum": 1024},
})
GENERATION_PARAMETERS["properties"]["model"] = deepcopy(_MODEL)
GENERATION_PARAMETERS["properties"]["model"]["properties"]["adapter"] = {"enum": ["gpt2-causal-lm"]}
JUDGE_PARAMETERS = _schema({
    "model": _MODEL, "labels": {"type": "array", "minItems": 2, "maxItems": 32,
                               "uniqueItems": True, "items": {"type": "string", "minLength": 1, "maxLength": 200}},
    "score_label": {"type": "string", "minLength": 1, "maxLength": 200}, "rank_direction": {"enum": ["highest_first", "lowest_first"]},
})
JUDGE_PARAMETERS["properties"]["model"] = deepcopy(_MODEL)
JUDGE_PARAMETERS["properties"]["model"]["properties"]["adapter"] = {"enum": ["bert-sequence-classifier"]}
PREPARE_PARAMETERS = _schema({
    "candidate_digest": _HASH, "judgment_digest": _HASH,
    "approved_ids": {"type": "array", "minItems": 1, "maxItems": MAX_ROWS, "uniqueItems": True,
                     "items": _TEXT},
    "name": {"type": "string", "minLength": 1, "maxLength": 200},
    "benchmark": {"type": "string", "minLength": 1, "maxLength": 200},
    "license": {"type": "string", "minLength": 1, "maxLength": 2048},
    "root_id": {"type": "string", "minLength": 1, "maxLength": 200},
    "review_note": {"type": "string", "minLength": 1, "maxLength": 2048},
})


def catalog_entries():
    """Return reusable declarations, never fabricate a user's source pin."""
    entries = []
    for suffix, role, description, schema, inputs, outputs, entrypoint in [
        ("generate", "dataset", "Offline GPT-2 CPU sampling from a pinned local model; candidates only.",
         GENERATION_PARAMETERS, ["prompts", "validation", "test"], [("candidates", "dict")], "generate"),
        ("judge", "utility", "Separate local BERT classifier predictions, not ground truth or quality guarantees.",
         JUDGE_PARAMETERS, ["candidates"], [("judgments", "dict")], "judge"),
        ("prepare-dataset", "dataset", "Explicit reviewed selection to train-only JSONL/manifest; no registration or training.",
         PREPARE_PARAMETERS, ["candidates", "judgments"], [("prepared", "Path")], "prepare_dataset"),
    ]:
        descriptor = {"id": "modelbuilder.local." + suffix, "version": VERSION, "protocol": PROTOCOL,
            "role": role, "description": description, "parameter_schema": deepcopy(schema),
            "capabilities": {"resume_modes": [], "export_formats": [], "frameworks": ["pytorch", "transformers"]}}
        entries.append({**descriptor, "inputs": [{"name": name, "type": "dict"} for name in inputs],
            "outputs": [{"name": name, "type": kind} for name, kind in outputs],
            "adapter": {"module": __name__, "entrypoint": entrypoint,
                "source_binding": "Pin a Python wrapper in your project's trusted source closure.",
                "example_source": "examples/local_generation/adapter.py"},
            "component_digest": identity(descriptor)})
    return entries


def _parameters(parameters, schema):
    # Validation happens at invocation too, even if a user changes the node's schema.
    Draft202012Validator(schema).validate(parameters)
    json.dumps(parameters, allow_nan=False)


def _finite_json(data):
    def reject(value):
        raise ValueError("Only finite JSON assets are supported")
    value = json.loads(data, parse_constant=reject)
    json.dumps(value, allow_nan=False)
    return value


def _asset(reference, context, max_bytes):
    if not re.fullmatch(r"[a-f0-9]{64}", reference["sha256"]):
        raise ValueError("Model asset requires a SHA256 pin")
    raw = context.get("dependencies", {}).get(reference["dependency_id"])
    if not isinstance(raw, str):
        raise ValueError("Model asset is not an admitted dependency")
    path = Path(raw)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Symlinked model assets are unsupported")
    with path.open("rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError("Model asset is not a bounded regular file")
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes or hashlib.sha256(data).hexdigest() != reference["sha256"]:
        raise ValueError("Model asset differs from its pinned bytes")
    return data


@contextmanager
def _deterministic(seed):
    import torch
    previous = torch.are_deterministic_algorithms_enabled()
    warn = torch.is_deterministic_algorithms_warn_only_enabled()
    threads = torch.get_num_threads()
    try:
        torch.set_num_threads(1)
        torch.use_deterministic_algorithms(True)
        with torch.random.fork_rng(devices=[]):
            torch.random.default_generator.manual_seed(seed)
            yield
    finally:
        torch.use_deterministic_algorithms(previous, warn_only=warn)
        torch.set_num_threads(threads)


def _model(spec, context, expected):
    if spec["adapter"] != expected:
        raise ValueError("Unsupported model adapter for this operation")
    config = _finite_json(_asset(spec["config"], context, 1024 * 1024))
    tokenizer_bytes = _asset(spec["tokenizer"], context, 4 * 1024 * 1024)
    weights = _asset(spec["weights"], context, MAX_ASSET_BYTES)
    if not isinstance(config, dict) or any(key in config for key in ("auto_map", "quantization_config")):
        raise ValueError("Custom code and quantized model configurations are unsupported")
    import torch
    from tokenizers import Tokenizer
    from safetensors.torch import load_model
    from transformers import BertConfig, BertForSequenceClassification, GPT2Config, GPT2LMHeadModel
    is_generator = expected == "gpt2-causal-lm"
    if config.get("model_type") != ("gpt2" if is_generator else "bert"):
        raise ValueError("Configuration architecture does not match the explicit adapter")
    cls, model_cls = (GPT2Config, GPT2LMHeadModel) if is_generator else (BertConfig, BertForSequenceClassification)
    model_config = cls.from_dict(config)
    if (model_config.is_encoder_decoder or model_config.add_cross_attention
            or (not is_generator and model_config.is_decoder)):
        raise ValueError("Encoder-decoder and cross-attention variants are unsupported")
    # Validate before allocation, including products. No arbitrary size config gets instantiated.
    width = model_config.n_embd if is_generator else model_config.hidden_size
    layers = model_config.n_layer if is_generator else model_config.num_hidden_layers
    heads = model_config.n_head if is_generator else model_config.num_attention_heads
    positions = model_config.n_positions if is_generator else model_config.max_position_embeddings
    inner = (model_config.n_inner or 4 * width) if is_generator else model_config.intermediate_size
    vocab = model_config.vocab_size
    dimensions = [width, layers, heads, positions, inner, vocab]
    if (any(type(x) is not int or x < 1 for x in dimensions) or width % heads
            or width > 1024 or layers > 12 or positions > 4096 or inner > 4096 or vocab > 65536
            or vocab * width + layers * (4 * width * width + 2 * width * inner) > MAX_PARAMETERS):
        raise ValueError("Model exceeds the bounded CPU adapter dimensions")
    if not is_generator and (not 2 <= model_config.num_labels <= 32
            or model_config.problem_type not in (None, "single_label_classification")):
        raise ValueError("Judge supports single-label classification with 2 through 32 labels")
    # Parsing tokenizer.json cannot import Python or retrieve a tokenizer from a hub.
    tokenizer = Tokenizer.from_str(tokenizer_bytes.decode("utf-8"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    if (tokenizer.get_vocab_size(with_added_tokens=True) > vocab
            or any(token_id >= vocab or token_id < 0 for token_id in tokenizer.get_vocab().values())):
        raise ValueError("Tokenizer vocabulary exceeds the pinned model vocabulary")
    # Explicit eager CPU inference is the supported deterministic implementation.
    model_config._attn_implementation = "eager"
    with torch.device("cpu"):
        model = model_cls(model_config).to(device="cpu", dtype=torch.float32)
    if sum(p.numel() for p in model.parameters()) > MAX_PARAMETERS:
        raise ValueError("Model exceeds the bounded CPU parameter count")
    with tempfile.TemporaryDirectory(prefix="modelbuilder-local-weights-") as directory:
        snapshot = Path(directory) / "model.safetensors"
        snapshot.write_bytes(weights)
        load_model(model, str(snapshot), strict=True, device="cpu")
    model.eval()
    return model, tokenizer, {"adapter": expected, "assets": deepcopy(spec), "digest": identity(spec)}, positions


def _normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _split(data, role, text_field):
    if not isinstance(data, dict) or data.get("split", {}).get("role") != role:
        raise ValueError("Expected a registered " + role + " split")
    rows, ids = data.get("rows"), data["split"].get("sample_ids")
    lineage = data.get("consumption", {})
    if (not isinstance(rows, list) or not 1 <= len(rows) <= 100000 or not isinstance(ids, list)
            or len(ids) != len(rows) or len(set(ids)) != len(ids)
            or not isinstance(lineage.get("sample_digests"), list) or len(lineage["sample_digests"]) != len(rows)
            or lineage.get("sample_ids") != ids or lineage.get("dataset") != data.get("dataset")
            or lineage.get("split") != data["split"] or not lineage.get("version_id")
            or lineage.get("digest") != identity({key: value for key, value in lineage.items() if key != "digest"})):
        raise ValueError("Dataset split lacks consistent immutable row lineage")
    for index, row in enumerate(rows):
        if (not isinstance(row, dict) or not isinstance(row.get(text_field), str)
                or not 1 <= len(row[text_field]) <= 8192):
            raise ValueError("Dataset adapter needs bounded text rows")
        # Registry content digests exclude its arbitrary identity column; supplied
        # row order is bound by the registry's ordered sample identities.
        digest_options = [identity({key: value for key, value in row.items() if key != field})
                          for field, value in row.items() if value == ids[index]]
        if lineage.get("sample_digests", [])[index] not in digest_options:
            raise ValueError("Dataset rows differ from their registered consumption hashes")
    return {"dataset": deepcopy(data["dataset"]),
            "split": {"id": data["split"]["id"], "role": role, "sample_count": len(ids), "sample_ids_digest": identity(ids)},
            "version_id": lineage["version_id"], "consumption_digest": lineage["digest"]}


def _seal(value):
    return {**value, "digest": identity(value)}


def _artifact(value, schema):
    if (not isinstance(value, dict) or value.get("schema") != schema
            or value.get("digest") != identity({key: item for key, item in value.items() if key != "digest"})):
        raise ValueError("Candidate/judgment artifact integrity mismatch")
    if len(json.dumps(value, allow_nan=False).encode()) > 8 * 1024 * 1024:
        raise ValueError("Inference artifact exceeds the size bound")
    return value


def _candidate_rows(value):
    _artifact(value, CANDIDATES)
    if value.get("row_schema") != ROW_SCHEMA or not 1 <= len(value.get("rows", [])) <= MAX_ROWS:
        raise ValueError("Unsupported generated row schema or count")
    validator = Draft202012Validator(ROW_SCHEMA)
    seen = set()
    for row in value["rows"]:
        validator.validate(row)
        if row["id"] in seen:
            raise ValueError("Duplicate generated identity")
        seen.add(row["id"])
    return value["rows"]


def _runtime(context):
    return {"component_version": VERSION, "runtime_source": context.get("environment", {}).get("modelbuilder_runtime_source"),
            "source": deepcopy(context.get("source", {})), "execution_identities": deepcopy(context.get("identities", {})),
            "packages": {name: importlib.metadata.version(name) for name in ("torch", "transformers", "tokenizers", "safetensors")},
            "device": "cpu", "dtype": "float32", "threads": 1, "deterministic_algorithms": True}


def generate(*, inputs, parameters, context):
    """Sample actual causal-LM outputs, withholding protected overlaps as rejections."""
    _parameters(parameters, GENERATION_PARAMETERS)
    if set(inputs) != {"prompts", "validation", "test"}:
        raise ValueError("Generation requires prompts, validation and test registered splits")
    field = parameters["text_field"]
    splits = {key: _split(inputs[key], role, field) for key, role in
              (("prompts", "train"), ("validation", "validation"), ("test", "test"))}
    projects = {inputs[key]["consumption"].get("project_id") for key in inputs}
    benchmarks = {value["dataset"].get("benchmark") for value in splits.values()}
    if len(projects) != 1 or None in projects or len(benchmarks) != 1 or None in benchmarks:
        raise ValueError("Generation splits must belong to the same project and benchmark")
    groups = [set(inputs[key]["split"]["sample_ids"]) for key in inputs]
    if any(a & b for index, a in enumerate(groups) for b in groups[index + 1:]):
        raise ValueError("Generation/protected split identities overlap")
    protected = {_normalized(row[field]) for key in ("validation", "test") for row in inputs[key]["rows"]}
    if any(_normalized(row[field]) in protected for row in inputs["prompts"]["rows"]):
        raise ValueError("Training prompts overlap protected benchmark content")
    if len(inputs["prompts"]["rows"]) * parameters["samples_per_prompt"] > MAX_ROWS:
        raise ValueError("Generation request exceeds the candidate bound")
    import torch
    rows, rejected = [], []
    with _deterministic(parameters["seed"]):
        model, tokenizer, model_identity, positions = _model(parameters["model"], context, "gpt2-causal-lm")
        if parameters["top_k"] > model.config.vocab_size:
            raise ValueError("top_k exceeds model vocabulary")
        for parent_id, source_row in zip(inputs["prompts"]["split"]["sample_ids"], inputs["prompts"]["rows"]):
            prompt = parameters["prompt_prefix"] + source_row[field]
            token_ids = tokenizer.encode(prompt, add_special_tokens=True).ids
            if not token_ids or len(token_ids) + parameters["max_new_tokens"] > positions:
                raise ValueError("Prompt does not fit the model context; silent truncation is forbidden")
            for sample in range(parameters["samples_per_prompt"]):
                sample_seed = int(identity({"seed": parameters["seed"], "parent": parent_id, "sample": sample})[:8], 16)
                torch.random.default_generator.manual_seed(sample_seed)
                tokens = torch.tensor([token_ids], dtype=torch.long, device="cpu")
                from transformers import GenerationConfig
                settings = GenerationConfig(max_new_tokens=parameters["max_new_tokens"], do_sample=True,
                    temperature=parameters["temperature"], top_k=parameters["top_k"], top_p=1.0,
                    num_beams=1, use_cache=True, pad_token_id=0, eos_token_id=None, bos_token_id=None)
                with torch.inference_mode():
                    prediction = model.generate(input_ids=tokens, attention_mask=torch.ones_like(tokens),
                                                generation_config=settings)[0].tolist()
                new_tokens = prediction[len(token_ids):]
                text = tokenizer.decode(prediction, skip_special_tokens=False)
                row_id = "generated-" + identity({"model": model_identity, "splits": splits,
                    "parameters": parameters, "parent": parent_id, "sample": sample, "tokens": new_tokens})
                if _normalized(text) in protected:
                    rejected.append({"id": row_id, "parent_id": parent_id, "sample_index": sample,
                                     "sampling_seed": sample_seed, "reason": "protected_content_overlap"})
                    continue
                row = {"id": row_id, "parent_id": parent_id, "prompt": prompt, "text": text,
                       "generated_token_ids": new_tokens, "sample_index": sample, "sampling_seed": sample_seed}
                Draft202012Validator(ROW_SCHEMA).validate(row)
                rows.append(row)
    if not rows:
        raise ValueError("Generation produced no schema-valid isolated candidates")
    value = attach_lineage({"schema": CANDIDATES, "status": "candidates_only", "row_schema": deepcopy(ROW_SCHEMA),
        "rows": rows, "rejected": rejected, "provenance": {"generator": model_identity,
            "parameters": deepcopy(parameters), "splits": splits, "runtime": _runtime(context),
            "seed_policy": "sha256(seed,parent_id,sample_index)[0:8] per sample",
            "protection": "NFKC/casefold/whitespace exact-text and sample-ID isolation; not semantic decontamination"}}, inputs, context)
    _artifact(value, CANDIDATES)
    return {"candidates": value}


def judge(*, inputs, parameters, context):
    """A separate classifier returns predictions and a ranking, never true labels."""
    _parameters(parameters, JUDGE_PARAMETERS)
    if set(inputs) != {"candidates"}:
        raise ValueError("Judge requires only the candidate artifact")
    candidates = inputs["candidates"]
    rows = _candidate_rows(candidates)
    validate_lineage(candidates)
    if parameters["score_label"] not in parameters["labels"]:
        raise ValueError("score_label must be in the configured label mapping")
    if parameters["model"]["weights"]["sha256"] == candidates["provenance"]["generator"]["assets"]["weights"]["sha256"]:
        raise ValueError("Judge must use separate model weights")
    import torch
    predictions = []
    with _deterministic(0):
        model, tokenizer, model_identity, positions = _model(parameters["model"], context, "bert-sequence-classifier")
        if len(parameters["labels"]) != model.config.num_labels:
            raise ValueError("Explicit label mapping must exactly match the judge output dimension")
        for row in rows:
            token_ids = tokenizer.encode(row["text"], add_special_tokens=True).ids
            if not token_ids or len(token_ids) > positions:
                raise ValueError("Candidate does not fit judge context; silent truncation is forbidden")
            tokens = torch.tensor([token_ids], dtype=torch.long, device="cpu")
            with torch.inference_mode():
                logits = model(input_ids=tokens, attention_mask=torch.ones_like(tokens)).logits[0]
                probabilities = torch.softmax(logits, dim=-1).tolist()
            if not all(math.isfinite(value) for value in probabilities):
                raise ValueError("Judge returned nonfinite probabilities")
            predicted = max(range(len(probabilities)), key=probabilities.__getitem__)
            predictions.append({"id": row["id"], "prediction_kind": "model_prediction",
                "predicted_label": parameters["labels"][predicted],
                "probabilities": dict(zip(parameters["labels"], probabilities)),
                "score": probabilities[parameters["labels"].index(parameters["score_label"])]})
    ranked = sorted(predictions, key=lambda row: ((-1 if parameters["rank_direction"] == "highest_first" else 1) * row["score"], row["id"]))
    result = attach_lineage({"schema": JUDGMENTS, "candidate_digest": candidates["digest"],
        "status": "model_predictions_not_ground_truth", "predictions": predictions,
        "ranked_ids": [row["id"] for row in ranked], "provenance": {"judge": model_identity,
            "parameters": deepcopy(parameters), "runtime": _runtime(context)}}, inputs, context)
    _artifact(result, JUDGMENTS)
    return {"judgments": result}


def prepare_dataset(*, inputs, parameters, context):
    """Create a reviewed train-only manifest proposal, with no registry side effects."""
    _parameters(parameters, PREPARE_PARAMETERS)
    if set(inputs) != {"candidates", "judgments"}:
        raise ValueError("Dataset preparation requires candidates and their judgments")
    candidates, judgments = inputs["candidates"], inputs["judgments"]
    rows = _candidate_rows(candidates)
    _artifact(judgments, JUDGMENTS)
    if (parameters["candidate_digest"] != candidates["digest"]
            or parameters["judgment_digest"] != judgments["digest"]
            or judgments.get("candidate_digest") != candidates["digest"]):
        raise ValueError("Reviewed candidate/judgment identities changed")
    original = {value["dataset"]["benchmark"] for value in candidates["provenance"]["splits"].values()}
    if parameters["benchmark"] in original:
        raise ValueError("Generated training data needs a separate benchmark identity")
    approved = set(parameters["approved_ids"])
    lookup = {row["id"]: row for row in rows}
    predictions = judgments.get("predictions", [])
    if (not approved <= lookup.keys() or len(predictions) != len(rows)
            or {row.get("id") for row in predictions} != lookup.keys()
            or any(row.get("prediction_kind") != "model_prediction" for row in predictions)):
        raise ValueError("Review must select exact candidates with explicit model predictions")
    validate_lineage(candidates)
    validate_lineage(judgments)
    predictions = {row["id"]: row for row in predictions}
    selected = [{**lookup[row_id], "judge_predicted_label": predictions[row_id]["predicted_label"],
                 "judge_score": predictions[row_id]["score"], "candidate_digest": candidates["digest"],
                 "judgment_digest": judgments["digest"]} for row_id in parameters["approved_ids"]]
    schema = deepcopy(ROW_SCHEMA)
    schema["properties"].update(judge_predicted_label=_TEXT, judge_score={"type": "number", "minimum": 0, "maximum": 1},
                                candidate_digest=_HASH, judgment_digest=_HASH)
    schema["required"].extend(["judge_predicted_label", "judge_score", "candidate_digest", "judgment_digest"])
    for row in selected:
        Draft202012Validator(schema).validate(row)
    output = Path(context["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in selected).encode()
    filename = "reviewed-" + hashlib.sha256(content).hexdigest() + ".jsonl"
    manifest = {"name": parameters["name"], "benchmark": parameters["benchmark"], "license": parameters["license"],
        "description": "Reviewed synthetic training rows. Judge outputs are model predictions, not true labels.",
        "provenance": {"source": "modelbuilder.local.prepare-dataset/v1", "source_revision": candidates["digest"],
                       "description": json.dumps({"judgments": judgments["digest"], "review": parameters["review_note"]})},
        "sources": [{"id": "reviewed", "root_id": parameters["root_id"], "path": filename,
                     "format": "jsonl", "sha256": hashlib.sha256(content).hexdigest()}],
        "id_column": "id", "row_schema": schema,
        "splits": [{"id": "train", "role": "train", "source_ids": ["reviewed"]}], "transformations": []}
    manifest = attach_lineage(manifest, inputs, context)
    from ...dataset import validate_manifest
    validate_manifest(manifest)
    # Immutable content-addressed rows plus a full provenance sidecar. Never overwrite
    # different bytes on repeated invocation, and never consume a raw host path parameter.
    for name, data in {filename: content, "dataset.manifest.json": json.dumps(manifest, sort_keys=True, indent=2).encode(),
        "review.provenance.json": json.dumps({"candidates": candidates, "judgments": judgments,
            "review": parameters, "registration_started": False, "training_started": False},
            sort_keys=True, indent=2, allow_nan=False).encode()}.items():
        path = output / name
        if path.is_symlink() or (path.exists() and path.read_bytes() != data):
            raise ValueError("Prepared artifact already contains different bytes")
        path.write_bytes(data)
    return {"prepared": output}
