"""Offline generation and classifier judging using installed Transformers Auto classes.

The catalog is inert. Trusted, source-pinned components adapt native framework
inference over admitted assets, with recipe-owned resource budgets. Predictions
never become scientific evaluation metrics, true labels, or training approvals.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import tempfile

from jsonschema import Draft202012Validator

from . import PROTOCOL, identity
from . import local_inference as legacy
from . import transformers_loader as loader
from .transformers_training import _stage, _split

VERSION = "1"
CANDIDATES = "modelbuilder.generated-candidates/v2"
JUDGMENTS = "modelbuilder.model-judgments/v2"
_POSITIVE = {"type": "integer", "minimum": 1}
_TEXT = {"type": "string", "minLength": 1}
_HASH = {"type": "string", "minLength": 64, "maxLength": 64}


def _schema(properties):
    return {"type": "object", "additionalProperties": False,
            "required": list(properties), "properties": properties}


_REF = _schema({"dependency_id": _TEXT, "sha256": _HASH})
MODEL = _schema({name: _REF for name in ("config", "weights", "tokenizer")})
LIMITS = _schema({name: _POSITIVE for name in ("max_parameters", "max_asset_bytes", "max_rows",
    "max_tokenized_tokens", "max_text_chars", "max_artifact_bytes")})
_COMMON = {"model": MODEL, "limits": LIMITS, "max_length": _POSITIVE,
           "pad_token_id": {"type": "integer", "minimum": 0}}
GENERATION_PARAMETERS = _schema({**_COMMON,
    "seed": {"type": "integer", "minimum": 0, "maximum": 2147483647},
    "text_field": _TEXT, "prompt_prefix": {"type": "string"},
    "max_new_tokens": _POSITIVE, "samples_per_prompt": _POSITIVE,
    "temperature": {"type": "number", "exclusiveMinimum": 0}, "top_k": _POSITIVE,
    "eos_token_ids": {"type": "array", "uniqueItems": True,
                      "items": {"type": "integer", "minimum": 0}},
})
JUDGE_PARAMETERS = _schema({**_COMMON,
    "labels": {"type": "array", "minItems": 2, "uniqueItems": True, "items": _TEXT},
    "score_label": _TEXT, "rank_direction": {"enum": ["highest_first", "lowest_first"]},
    "batch_size": _POSITIVE,
})
PREPARE_PARAMETERS = _schema({
    **{key: deepcopy(value) for key, value in legacy.PREPARE_PARAMETERS["properties"].items()
       if key != "approved_ids"},
    "approved_ids": {"type": "array", "minItems": 1, "uniqueItems": True, "items": _TEXT},
    "limits": _schema({name: _POSITIVE for name in ("max_rows", "max_artifact_bytes", "max_text_chars")}),
})


def catalog_entries():
    entries = []
    for name, role, schema, inputs, outputs, description in [
        ("generate", "dataset", GENERATION_PARAMETERS, ["prompts", "validation", "test"], [("candidates", "dict")],
         "Offline installed Transformers causal-LM sampling with explicit resource budgets; candidates only."),
        ("judge", "utility", JUDGE_PARAMETERS, ["candidates"], [("judgments", "dict")],
         "Separate installed Transformers sequence-classifier predictions and ranking, never ground truth."),
        ("prepare-dataset", "dataset", PREPARE_PARAMETERS, ["candidates", "judgments"], [("prepared", "Path")],
         "Explicit reviewed candidate selection to train-only JSONL and manifest; no automatic registration."),
    ]:
        descriptor = {"id": "modelbuilder.transformers." + name, "version": VERSION, "protocol": PROTOCOL,
            "role": role, "description": description, "parameter_schema": deepcopy(schema),
            "capabilities": {"devices": ["cpu"], "resume_modes": [], "export_formats": [],
                             "frameworks": ["pytorch", "transformers"]}}
        entries.append({**descriptor, "inputs": [{"name": name, "type": "dict"} for name in inputs],
            "outputs": [{"name": name, "type": kind} for name, kind in outputs],
            "adapter": {"module": __name__, "entrypoint": name.replace("-", "_"),
                "source_binding": "Pin a Python wrapper in your project's trusted source closure.",
                "example_source": "examples/transformers_inference/adapter.py"},
            "component_digest": identity(descriptor)})
    return entries


def _parameters(parameters, schema, context):
    legacy._parameters(parameters, schema)
    if context.get("device", "cpu") != "cpu":
        raise ValueError("Transformers inference currently verifies CPU float32 only")


def _settings(parameters, task):
    return {"task": task, "model": parameters["model"], "labels": parameters.get("labels", []),
            "max_length": parameters["max_length"], "pad_token_id": parameters["pad_token_id"],
            "truncate": False, "limits": parameters["limits"]}


def _runtime(context):
    return {**legacy._runtime(context), "adapter_family": "modelbuilder.transformers.inference/v1"}


def _model(parameters, context, task):
    settings = _settings(parameters, task)
    with tempfile.TemporaryDirectory(prefix="modelbuilder-inference-") as temporary:
        _stage(settings, context, Path(temporary))
        model, tokenizer, settings = loader.load(temporary)
    model_identity = {"task": task, "model_type": model.config.model_type,
                      "assets": deepcopy(parameters["model"]), "digest": identity(parameters["model"])}
    return model, tokenizer, settings, model_identity


def _text(value, limits):
    if not isinstance(value, str) or not value or len(value) > limits["max_text_chars"]:
        raise ValueError("Inference text is empty or exceeds the recipe's declared character limit")
    return value


def _cancel(context):
    if context.get("cancel_file") and Path(context["cancel_file"]).exists():
        raise InterruptedError("Local inference cancelled")


def _artifact(value, schema, limits):
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise ValueError("Unsupported inference artifact schema")
    if len(json.dumps(value, allow_nan=False).encode()) > limits["max_artifact_bytes"]:
        raise ValueError("Inference artifact exceeds the recipe's declared byte limit")
    if value.get("digest") != identity({key: item for key, item in value.items() if key != "digest"}):
        raise ValueError("Inference artifact integrity mismatch")
    return value


def _row_schema(parameters):
    text = {"type": "string", "minLength": 1, "maxLength": parameters["limits"]["max_text_chars"]}
    return _schema({"id": _TEXT, "parent_id": _TEXT, "prompt": text, "text": text,
        "generated_token_ids": {"type": "array", "minItems": 1, "maxItems": parameters["max_new_tokens"],
                                "items": {"type": "integer", "minimum": 0}},
        "sample_index": {"type": "integer", "minimum": 0, "maximum": parameters["samples_per_prompt"] - 1},
        "sampling_seed": {"type": "integer", "minimum": 0, "maximum": 4294967295}})


def _candidate_rows(value, limits):
    # Both old artifacts and new budgeted artifacts can be explicitly reviewed
    # with this family. No v1 adapter, schema or digest is changed.
    schema = value.get("schema") if isinstance(value, dict) else None
    _artifact(value, schema if schema in (CANDIDATES, legacy.CANDIDATES) else CANDIDATES, limits)
    rows = value.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= limits["max_rows"]:
        raise ValueError("Candidate rows exceed the recipe's declared row limit")
    if schema == legacy.CANDIDATES:
        legacy._candidate_rows(value)
        expected_schema = legacy.ROW_SCHEMA
    else:
        parameters = value.get("provenance", {}).get("parameters", {})
        legacy._parameters(parameters, GENERATION_PARAMETERS)
        expected_schema = _row_schema(parameters)
    if value.get("status") != "candidates_only" or value.get("row_schema") != expected_schema:
        raise ValueError("Unsupported candidate row schema or status")
    validator = Draft202012Validator(expected_schema)
    seen = set()
    for row in rows:
        validator.validate(row)
        _text(row["text"], limits)
        _text(row["prompt"], limits)
        if row["id"] in seen:
            raise ValueError("Duplicate generated identity")
        seen.add(row["id"])
    return rows


def _protected_splits(inputs, parameters):
    if set(inputs) != {"prompts", "validation", "test"}:
        raise ValueError("Generation requires prompts, validation and test registered splits")
    settings = _settings(parameters, "causal-lm")
    splits = {}
    for key, role in (("prompts", "train"), ("validation", "validation"), ("test", "test")):
        rows, stable = _split(inputs[key], {role}, settings)
        for row in rows:
            _text(row.get(parameters["text_field"]), parameters["limits"])
        # Protected texts and sample IDs are never copied into output provenance.
        splits[key] = {"dataset": stable["dataset"], "version_id": stable["version_id"],
            "consumption_digest": stable["consumption_digest"],
            "split": {"id": stable["split"]["id"], "role": role, "sample_count": len(rows),
                      "sample_ids_digest": identity(stable["split"]["sample_ids"])}}
    projects = {value["consumption"].get("project_id") for value in inputs.values()}
    benchmarks = {value["dataset"].get("benchmark") for value in splits.values()}
    if len(projects) != 1 or None in projects or len(benchmarks) != 1 or None in benchmarks:
        raise ValueError("Generation splits must belong to the same project and benchmark")
    groups = [set(inputs[key]["split"]["sample_ids"]) for key in inputs]
    if any(a & b for index, a in enumerate(groups) for b in groups[index + 1:]):
        raise ValueError("Generation/protected split identities overlap")
    field = parameters["text_field"]
    protected = {legacy._normalized(row[field]) for key in ("validation", "test") for row in inputs[key]["rows"]}
    if any(legacy._normalized(row[field]) in protected
           or legacy._normalized(_text(parameters["prompt_prefix"] + row[field], parameters["limits"])) in protected
           for row in inputs["prompts"]["rows"]):
        raise ValueError("Training prompts overlap protected benchmark content")
    return splits, protected


def generate(*, inputs, parameters, context):
    """Framework causal-LM sampling with exact-text held-out overlap rejection."""
    _parameters(parameters, GENERATION_PARAMETERS, context)
    splits, protected = _protected_splits(inputs, parameters)
    count = len(inputs["prompts"]["rows"]) * parameters["samples_per_prompt"]
    if count > parameters["limits"]["max_rows"]:
        raise ValueError("Generation exceeds the recipe's declared row limit")
    if count * parameters["max_length"] > parameters["limits"]["max_tokenized_tokens"]:
        raise ValueError("Generation exceeds the recipe's declared token budget")
    if parameters["max_new_tokens"] >= parameters["max_length"]:
        raise ValueError("Generation needs room for a prompt within max_length")
    _cancel(context)
    import torch
    from transformers import GenerationConfig
    rows, rejected = [], []
    row_schema = _row_schema(parameters)
    with legacy._deterministic(parameters["seed"]):
        model, tokenizer, _, model_identity = _model(parameters, context, "causal-lm")
        vocab = model.get_input_embeddings().num_embeddings
        if parameters["top_k"] > vocab or any(token >= vocab for token in parameters["eos_token_ids"]):
            raise ValueError("Sampling settings exceed the model vocabulary")
        settings = GenerationConfig(max_new_tokens=parameters["max_new_tokens"], do_sample=True,
            temperature=parameters["temperature"], top_k=parameters["top_k"], top_p=1.0, num_beams=1,
            use_cache=True, pad_token_id=parameters["pad_token_id"],
            eos_token_id=parameters["eos_token_ids"] or None, bos_token_id=None)
        # Transformers otherwise falls back to model-owned generation defaults,
        # including EOS and beam/forced-token options not present in the recipe.
        model.generation_config = deepcopy(settings)
        for parent_id, source_row in zip(inputs["prompts"]["split"]["sample_ids"], inputs["prompts"]["rows"]):
            prompt = _text(parameters["prompt_prefix"] + source_row[parameters["text_field"]], parameters["limits"])
            tokens = tokenizer.encode(prompt, add_special_tokens=True).ids
            if not tokens or len(tokens) + parameters["max_new_tokens"] > parameters["max_length"]:
                raise ValueError("Prompt does not fit the declared context; silent truncation is forbidden")
            for sample in range(parameters["samples_per_prompt"]):
                _cancel(context)
                sample_seed = int(identity({"seed": parameters["seed"], "parent": parent_id, "sample": sample})[:8], 16)
                torch.random.default_generator.manual_seed(sample_seed)
                batch = torch.tensor([tokens], dtype=torch.long, device="cpu")
                with torch.inference_mode():
                    prediction = model.generate(input_ids=batch, attention_mask=torch.ones_like(batch),
                                                generation_config=settings, use_model_defaults=False)[0].tolist()
                new_tokens = prediction[len(tokens):]
                text = _text(tokenizer.decode(prediction, skip_special_tokens=False), parameters["limits"])
                row_id = "generated-" + identity({"model": model_identity, "splits": splits,
                    "parameters": parameters, "parent": parent_id, "sample": sample, "tokens": new_tokens})
                if legacy._normalized(text) in protected:
                    rejected.append({"id": row_id, "parent_id": parent_id, "sample_index": sample,
                                     "sampling_seed": sample_seed, "reason": "protected_content_overlap"})
                    continue
                row = {"id": row_id, "parent_id": parent_id, "prompt": prompt, "text": text,
                       "generated_token_ids": new_tokens, "sample_index": sample, "sampling_seed": sample_seed}
                Draft202012Validator(row_schema).validate(row)
                rows.append(row)
    if not rows:
        raise ValueError("Generation produced no schema-valid isolated candidates")
    value = legacy._seal({"schema": CANDIDATES, "status": "candidates_only", "row_schema": row_schema,
        "rows": rows, "rejected": rejected, "provenance": {"generator": model_identity,
            "parameters": deepcopy(parameters), "splits": splits, "runtime": _runtime(context),
            "seed_policy": "sha256(seed,parent_id,sample_index)[0:8] per sample",
            "protection": "NFKC/casefold/whitespace exact-text and sample-ID isolation; not semantic decontamination"}})
    return {"candidates": _artifact(value, CANDIDATES, parameters["limits"])}


def judge(*, inputs, parameters, context):
    """Separate sequence classifier inference; ranking is a review aid only."""
    _parameters(parameters, JUDGE_PARAMETERS, context)
    if set(inputs) != {"candidates"}:
        raise ValueError("Judge requires only the candidate artifact")
    candidates = inputs["candidates"]
    rows = _candidate_rows(candidates, parameters["limits"])
    if len(rows) * parameters["max_length"] > parameters["limits"]["max_tokenized_tokens"]:
        raise ValueError("Judging exceeds the recipe's declared token budget")
    if parameters["score_label"] not in parameters["labels"]:
        raise ValueError("score_label must be in the configured label mapping")
    if parameters["model"]["weights"]["sha256"] == candidates["provenance"]["generator"]["assets"]["weights"]["sha256"]:
        raise ValueError("Judge must use separate model weights")
    _cancel(context)
    import torch
    predictions = []
    with legacy._deterministic(0):
        model, tokenizer, settings, model_identity = _model(parameters, context, "sequence-classification")
        for offset in range(0, len(rows), parameters["batch_size"]):
            _cancel(context)
            batch = rows[offset:offset + parameters["batch_size"]]
            features = loader.encode([row["text"] for row in batch], tokenizer, settings)
            with torch.inference_mode():
                logits = model(**{key: torch.tensor([row[key] for row in features], device="cpu")
                                  for key in features[0]}).logits
                if tuple(logits.shape) != (len(batch), len(parameters["labels"])):
                    raise ValueError("Judge output shape differs from the explicit label mapping")
                probabilities = torch.softmax(logits, dim=-1).tolist()
            for row, scores in zip(batch, probabilities):
                if not all(math.isfinite(score) for score in scores):
                    raise ValueError("Judge returned nonfinite probabilities")
                predicted = max(range(len(scores)), key=scores.__getitem__)
                predictions.append({"id": row["id"], "prediction_kind": "model_prediction",
                    "predicted_label": parameters["labels"][predicted],
                    "probabilities": dict(zip(parameters["labels"], scores)),
                    "score": scores[parameters["labels"].index(parameters["score_label"])]})
    ranked = sorted(predictions, key=lambda row: ((-1 if parameters["rank_direction"] == "highest_first" else 1)
                                                * row["score"], row["id"]))
    value = legacy._seal({"schema": JUDGMENTS, "candidate_digest": candidates["digest"],
        "status": "model_predictions_not_ground_truth", "predictions": predictions,
        "ranked_ids": [row["id"] for row in ranked], "provenance": {"judge": model_identity,
            "parameters": deepcopy(parameters), "runtime": _runtime(context)}})
    return {"judgments": _artifact(value, JUDGMENTS, parameters["limits"])}


def prepare_dataset(*, inputs, parameters, context):
    """Write an explicitly reviewed train-only proposal without registry effects."""
    _parameters(parameters, PREPARE_PARAMETERS, context)
    if set(inputs) != {"candidates", "judgments"}:
        raise ValueError("Dataset preparation requires candidates and their judgments")
    candidates, judgments = inputs["candidates"], inputs["judgments"]
    rows = _candidate_rows(candidates, parameters["limits"])
    _artifact(judgments, JUDGMENTS, parameters["limits"])
    if (parameters["candidate_digest"] != candidates["digest"]
            or parameters["judgment_digest"] != judgments["digest"]
            or judgments.get("candidate_digest") != candidates["digest"]):
        raise ValueError("Reviewed candidate/judgment identities changed")
    original = {value["dataset"]["benchmark"] for value in candidates["provenance"]["splits"].values()}
    if parameters["benchmark"] in original:
        raise ValueError("Generated training data needs a separate benchmark identity")
    approved = parameters["approved_ids"]
    lookup = {row["id"]: row for row in rows}
    predictions = judgments.get("predictions", [])
    if (len(approved) > parameters["limits"]["max_rows"] or not set(approved) <= lookup.keys()
            or judgments.get("status") != "model_predictions_not_ground_truth"
            or len(predictions) != len(rows) or {row.get("id") for row in predictions} != lookup.keys()
            or any(row.get("prediction_kind") != "model_prediction" for row in predictions)):
        raise ValueError("Review must select exact candidates with explicit model predictions")
    predictions = {row["id"]: row for row in predictions}
    selected = [{**lookup[row_id], "judge_predicted_label": predictions[row_id]["predicted_label"],
                 "judge_score": predictions[row_id]["score"], "candidate_digest": candidates["digest"],
                 "judgment_digest": judgments["digest"]} for row_id in approved]
    schema = deepcopy(candidates["row_schema"])
    schema["properties"].update(judge_predicted_label=_TEXT,
        judge_score={"type": "number", "minimum": 0, "maximum": 1},
        candidate_digest={"type": "string", "minLength": 64, "maxLength": 64},
        judgment_digest={"type": "string", "minLength": 64, "maxLength": 64})
    schema["required"].extend(["judge_predicted_label", "judge_score", "candidate_digest", "judgment_digest"])
    for row in selected:
        Draft202012Validator(schema).validate(row)
    content = "".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in selected).encode()
    filename = "reviewed-" + hashlib.sha256(content).hexdigest() + ".jsonl"
    manifest = {"name": parameters["name"], "benchmark": parameters["benchmark"], "license": parameters["license"],
        "description": "Reviewed synthetic training rows. Judge outputs are model predictions, not true labels.",
        "provenance": {"source": "modelbuilder.transformers.prepare-dataset/v1", "source_revision": candidates["digest"],
                       "description": json.dumps({"judgments": judgments["digest"], "review": parameters["review_note"]})},
        "sources": [{"id": "reviewed", "root_id": parameters["root_id"], "path": filename,
                     "format": "jsonl", "sha256": hashlib.sha256(content).hexdigest()}],
        "id_column": "id", "row_schema": schema,
        "splits": [{"id": "train", "role": "train", "source_ids": ["reviewed"]}], "transformations": []}
    from ...dataset import validate_manifest
    validate_manifest(manifest)
    files = {filename: content, "dataset.manifest.json": json.dumps(manifest, sort_keys=True, indent=2).encode(),
        "review.provenance.json": json.dumps({"candidates": candidates, "judgments": judgments,
            "review": parameters, "registration_started": False, "training_started": False},
            sort_keys=True, indent=2, allow_nan=False).encode()}
    if sum(len(data) for data in files.values()) > parameters["limits"]["max_artifact_bytes"]:
        raise ValueError("Prepared artifact exceeds the recipe's declared byte limit")
    output = Path(context["output_dir"])
    if any(path.is_symlink() for path in (output, *output.parents)):
        raise ValueError("Prepared artifact directory cannot be symlinked")
    output.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        path = output / name
        if path.is_symlink() or (path.exists() and path.read_bytes() != data):
            raise ValueError("Prepared artifact already contains different bytes")
    for name, data in files.items():
        (output / name).write_bytes(data)
    return {"prepared": output}
