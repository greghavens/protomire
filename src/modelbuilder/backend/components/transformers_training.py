"""Reusable offline Transformers Trainer adapters, inert until trusted execution.

Transformers/Accelerate own optimization. These functions adapt pinned native
models, registered split dictionaries and ordinary ZenML file/value artifacts.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile

from jsonschema import Draft202012Validator

from . import PROTOCOL, identity, read_checkpoint, validate_resume
from . import transformers_loader as loader
from ...family_coverage import FAMILY_POLICY_SCHEMA

VERSION = "1"
_TASKS = ["sequence-classification", "causal-lm"]
_STRING = {"type": "string", "minLength": 1}
_INTEGER = {"type": "integer", "minimum": 1}


def _schema(properties):
    return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}


_REF = _schema({"dependency_id": _STRING, "sha256": {"type": "string", "minLength": 64, "maxLength": 64}})
LIMITS = _schema({"max_parameters": _INTEGER, "max_asset_bytes": _INTEGER,
                  "max_rows": _INTEGER, "max_tokenized_tokens": _INTEGER})
ARCHITECTURE = _schema({
    "task": {"enum": _TASKS}, "model": _schema({"config": _REF, "weights": _REF, "tokenizer": _REF}),
    "text_field": _STRING, "label_field": _STRING,
    "labels": {"type": "array", "uniqueItems": True, "items": _STRING},
    "max_length": {"type": "integer", "minimum": 2}, "truncate": {"type": "boolean"},
    "pad_token_id": {"type": "integer", "minimum": 0}, "limits": LIMITS,
})
TRAIN_PARAMETERS = _schema({
    "architecture": ARCHITECTURE, "seed": {"type": "integer", "minimum": 0, "maximum": 2147483647},
    "max_steps": _INTEGER, "batch_size": _INTEGER, "gradient_accumulation_steps": _INTEGER,
    "learning_rate": {"type": "number", "exclusiveMinimum": 0},
    "weight_decay": {"type": "number", "minimum": 0}, "warmup_steps": {"type": "integer", "minimum": 0},
    "lr_scheduler_type": {"enum": ["linear", "cosine", "constant", "constant_with_warmup"]},
    "max_grad_norm": {"type": "number", "exclusiveMinimum": 0}, "checkpoint_interval_steps": _INTEGER,
})
EVALUATE_PARAMETERS = _schema({"batch_size": _INTEGER,
    "metrics": {"type": "array", "minItems": 1, "uniqueItems": True,
                "items": {"enum": ["accuracy", "cross_entropy", "perplexity"]}}})
EVALUATE_PARAMETERS["properties"]["family_policy"] = deepcopy(FAMILY_POLICY_SCHEMA)

EXPORT_PARAMETERS = _schema({"parity_texts": {"type": "array", "minItems": 1, "maxItems": 32, "items": _STRING},
    "atol": {"type": "number", "minimum": 0}, "verification_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600}})


def catalog_entries():
    entries = []
    for name, role, schema, inputs, outputs in [
        ("train", "training", TRAIN_PARAMETERS, [("train", "dict")], [("trained", "Path")]),
        ("evaluate", "evaluation", EVALUATE_PARAMETERS, [("trained", "Path"), ("data", "dict")], [("metrics", "dict")]),
        ("export", "export", EXPORT_PARAMETERS, [("trained", "Path")], [("exported", "Path")]),
    ]:
        descriptor = {"id": "modelbuilder.transformers." + name, "version": VERSION, "protocol": PROTOCOL,
            "role": role, "description": {
                "train": "Offline Transformers Trainer for user-bound classification or causal-LM models and registered train data.",
                "evaluate": "Held-out classification or causal-LM metrics with exact dataset/checkpoint evidence.",
                "export": "Local safetensors/config/tokenizer package with a standalone loader and fresh-process logits parity.",
            }[name], "parameter_schema": deepcopy(schema), "capabilities": {
                "devices": ["cpu", "cuda", "mps"] if name == "train" else ["cpu"],
                "resume_modes": ["warm_start"] if name == "train" else [],
                "checkpoint_events": name == "train", "frameworks": ["pytorch", "transformers", "accelerate"],
                "export_formats": ["transformers-safetensors"] if name == "export" else []}}
        if name == "train":
            descriptor["capabilities"]["smoke_budget"] = {"schema": "modelbuilder.smoke-budget/v1", "limits": [
                {"id": "optimizer_steps", "unit": "optimizer_steps", "parameter_path": ["max_steps"], "semantics": "absolute_integer_cap", "minimum": 1}]}
        ports = [{"name": name, "type": kind} for name, kind in inputs]
        if name == "train":
            ports.append({"name": "validation", "type": "dict", "required": False})
        entries.append({**descriptor, "inputs": ports,
            "outputs": [{"name": name, "type": kind} for name, kind in outputs],
            "adapter": {"module": __name__, "entrypoint": name,
                "source_binding": "Pin the wrapper and select the reviewed Trainer environment profile; supported packaged backends include this overlay.",
                "example_source": "examples/transformers_training/adapter.py"}, "component_digest": identity(descriptor)})
    return entries


def _parameters(parameters, schema):
    Draft202012Validator(schema).validate(parameters)
    json.dumps(parameters, allow_nan=False)


def _versions():
    names = ("torch", "transformers", "accelerate", "tokenizers", "safetensors")
    try:
        return {name: importlib.metadata.version(name) for name in names}
    except importlib.metadata.PackageNotFoundError as error:
        raise ValueError("The selected backend requires the pinned Trainer overlay; select its reviewed environment profile") from error


@contextmanager
def _runtime(seed, device="cpu"):
    import numpy as np
    import torch
    python_state, numpy_state = random.getstate(), np.random.get_state()
    threads, deterministic = torch.get_num_threads(), torch.are_deterministic_algorithms_enabled()
    warn = torch.is_deterministic_algorithms_warn_only_enabled()
    try:
        torch.set_num_threads(1)
        torch.use_deterministic_algorithms(True)
        cuda_devices = [int(device.split(":")[1])] if device.startswith("cuda:") else []
        with torch.random.fork_rng(devices=cuda_devices):
            random.seed(seed)
            np.random.seed(seed)
            torch.manual_seed(seed)
            yield
    finally:
        random.setstate(python_state)
        np.random.set_state(numpy_state)
        torch.set_num_threads(threads)
        torch.use_deterministic_algorithms(deterministic, warn_only=warn)


@contextmanager
def _device_environment(device):
    updates = {"ACCELERATE_TORCH_DEVICE": device, "ACCELERATE_USE_CPU": str(device == "cpu").lower(),
               "PYTORCH_ENABLE_MPS_FALLBACK": "0"}
    if device.startswith("cuda:"):
        updates["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
    previous = {key: os.environ.get(key) for key in updates}
    try:
        os.environ.update(updates)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _training_arguments(device, **kwargs):
    """Keep Trainer's public device selection from auto-selecting CUDA0/DataParallel."""
    import torch
    from accelerate.state import AcceleratorState, PartialState
    from transformers import TrainingArguments
    if importlib.metadata.version("transformers") != "4.57.1" or importlib.metadata.version("accelerate") != "1.11.0":
        raise ValueError("Explicit Trainer device selection requires Transformers 4.57.1 and Accelerate 1.11.0")
    # Mirror the pinned Trainer's own singleton lifecycle, but do not invoke its
    # default-device setup: that touches CUDA0 even when another ordinal is selected.
    AcceleratorState._reset_state(reset_partial_state=True)
    state = PartialState(cpu=device == "cpu")
    if not _same_device(state.device, device) or state.num_processes != 1:
        raise ValueError("Accelerate state differs from the explicitly selected single device")

    class SelectedDeviceArguments(TrainingArguments):
        @property
        def _setup_devices(self):
            # Trainer 4.57.1 also reads this pinned initialization hook directly
            # after creating its Accelerator. Reuse the already selected state.
            return self.device

        @property
        def device(self):
            self.distributed_state = state
            self.local_rank = state.local_process_index
            selected = torch.device(device)
            if selected.type == "cuda":
                torch.cuda.set_device(selected)
            return selected

        @property
        def n_gpu(self):
            return 1 if device.startswith("cuda:") else 0

    return SelectedDeviceArguments(use_cpu=device == "cpu", **kwargs)


def _same_device(actual, selected):
    import torch
    actual, selected = torch.device(actual), torch.device(selected)
    return actual.type == selected.type and (actual.index == selected.index if selected.type == "cuda" else actual.index in (None, 0))


def _json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, allow_nan=False))


def _hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _assets(path):
    return [{"path": str(item.relative_to(path)), "sha256": _hash(item)}
            for item in sorted(path.rglob("*")) if item.is_file() and item.name != "checkpoint.manifest.json"]


def _copy_dependency(reference, target, context, limit):
    path = context.get("dependencies", {}).get(reference["dependency_id"])
    if not path:
        raise ValueError("Model asset must be an admitted dependency: " + reference["dependency_id"])
    source = Path(path)
    if any(item.is_symlink() for item in (source, *source.parents)):
        raise ValueError("Symlinked model assets are unsupported")
    with source.open("rb") as reader, target.open("xb") as writer:
        info = os.fstat(reader.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Model asset exceeds the recipe's declared resource limit")
        total, digest = 0, hashlib.sha256()
        while chunk := reader.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise ValueError("Model asset grew beyond the recipe's resource limit")
            digest.update(chunk)
            writer.write(chunk)
    if digest.hexdigest() != reference["sha256"]:
        raise ValueError("Model asset differs from the admitted SHA256")


def _stage(architecture, context, directory):
    for kind, name in [("config", "config.json"), ("weights", "model.safetensors"), ("tokenizer", "tokenizer.json")]:
        _copy_dependency(architecture["model"][kind], directory / name, context, architecture["limits"]["max_asset_bytes"])
    settings = {key: deepcopy(value) for key, value in architecture.items() if key != "model"}
    _json(directory / "adapter.json", settings)


def _split(data, roles, settings):
    from ...dataset import CONSUMPTION_SCHEMA
    if not isinstance(data, dict) or data.get("split", {}).get("role") not in roles:
        raise ValueError("Registered dataset split has the wrong purpose for this component")
    rows, split, lineage = data.get("rows"), data["split"], data.get("consumption", {})
    ids = split.get("sample_ids", [])
    expected_purpose = "training" if roles == {"train"} else "evaluation"
    if (not isinstance(rows, list) or not rows or len(rows) > settings["limits"]["max_rows"]
            or len(ids) != len(rows) or len(set(ids)) != len(ids)
            or lineage.get("schema") != CONSUMPTION_SCHEMA or lineage.get("purpose") != expected_purpose
            or lineage.get("sample_ids") != ids or lineage.get("dataset") != data.get("dataset")
            or lineage.get("split") != split or not lineage.get("version_id")
            or len(lineage.get("sample_digests", [])) != len(rows)
            or lineage.get("digest") != identity({key: value for key, value in lineage.items() if key != "digest"})):
        raise ValueError("Dataset lacks consistent immutable registered row lineage or exceeds its resource limit")
    if len(rows) * settings["max_length"] > settings["limits"]["max_tokenized_tokens"]:
        raise ValueError("Tokenized dataset exceeds the recipe's declared resource limit")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("Registered dataset rows must be objects")
        choices = [identity({key: value for key, value in row.items() if key != column})
                   for column, value in row.items() if value == ids[index]]
        if lineage["sample_digests"][index] not in choices:
            raise ValueError("Dataset row differs from its registered content hash")
    family_metadata = {}
    if split["role"] == "train":
        from .transformers_family_exposure import validate_eager_envelope
        family_metadata = validate_eager_envelope(data, rows)
    return rows, {"dataset": deepcopy(data["dataset"]), "split": deepcopy(split),
                  "version_id": lineage["version_id"], "consumption_digest": lineage["digest"],
                  **family_metadata}


def _features(rows, tokenizer, settings):
    if any(row.get("label_policy") == "final-assistant-content-only" for row in rows):
        raise ValueError("Final-assistant target metadata requires a compatible conversation formatter and token-mask validation; the plain-text Trainer cannot consume it")
    features = loader.encode([row.get(settings["text_field"]) for row in rows], tokenizer, settings)
    for feature, row in zip(features, rows):
        if settings["task"] == "causal-lm":
            feature["labels"] = [token if keep else -100 for token, keep in zip(feature["input_ids"], feature["attention_mask"])]
        else:
            label = row.get(settings["label_field"])
            if isinstance(label, str) and label in settings["labels"]:
                label = settings["labels"].index(label)
            if type(label) is not int or not 0 <= label < len(settings["labels"]):
                raise ValueError("Classification labels must match the ordered label names or zero-based class IDs")
            feature["labels"] = label
    return features


def _bounded_evaluation_data(data, settings):
    if "stream" not in data:
        return data
    from ...dataset_streaming import materialize_stream_split
    if data["stream"].get("purpose") != "evaluation" or data.get("split", {}).get("role") not in {"validation", "test"}:
        raise ValueError("Streaming evaluation requires a registered validation or test split")
    maximum = min(settings["limits"]["max_rows"], settings["limits"]["max_tokenized_tokens"] // settings["max_length"])
    if maximum < 1:
        raise ValueError("Evaluation token resource budget cannot hold one row")
    return materialize_stream_split(data, max_rows=maximum, max_bytes=settings["limits"]["max_asset_bytes"])


def _training_inputs(data, tokenizer, settings, exposure=None):
    """Use framework datasets; observe lineage at Trainer.training_step, not prefetch."""
    from torch.utils.data import IterableDataset
    if "stream" in data:
        from ...dataset_streaming import StreamingConsumption, iter_stream_rows, validate_stream_data
        stable = validate_stream_data(data, purpose="training")
        if stable["split"]["sample_count"] > settings["limits"]["max_rows"]:
            raise ValueError("Streaming dataset exceeds the recipe's declared row resource limit")
        consumed = StreamingConsumption(data)

        class RegisteredStream(IterableDataset):
            def __iter__(self):
                iterator = iter_stream_rows(data)
                try:
                    for item in iterator:
                        yield {**_features([item["row"]], tokenizer, settings)[0], "_lineage": item,
                               **({"_family": exposure.row(item["row"], item["sample_id"])} if exposure else {})}
                finally:
                    iterator.close()

        return (RegisteredStream(), consumed.add,
                lambda: {**stable, "iteration": "sequential_stream", "consumption": consumed.snapshot()})
    rows, stable = _split(data, {"train"}, settings)
    features = _features(rows, tokenizer, settings)
    for feature, item, content in zip(features, stable["split"]["sample_ids"], data["consumption"]["sample_digests"]):
        feature["_lineage"] = {"sample_id": item, "content_digest": content}
    if exposure:
        for feature, row, sample in zip(features, rows, stable["split"]["sample_ids"]):
            feature["_family"] = exposure.row(row, sample)
    chain, count = hashlib.sha256(), 0

    def observe(item):
        nonlocal count
        value = json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
        chain.update(len(value).to_bytes(8, "big") + value)
        count += 1

    def snapshot():
        return {**stable, "iteration": "trainer_random_sampler", "consumption": {
            "count": count, "ordered_lineage_digest": chain.hexdigest(),
            "digest_algorithm": "sha256-length-prefixed-canonical-json/v1",
            "observation": "Trainer.training_step input rows; includes repeated epochs"}}
    return features, observe, snapshot


def _snapshot(model, tokenizer, settings, state, context, data_identity, output):
    destination = output / "checkpoints" / f"{state.global_step:012d}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("Checkpoint destination already exists; use a new native run output directory")
    staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=destination.parent))
    try:
        # A single ordinary safetensors file is the v1 portable model contract.
        model.save_pretrained(staging, safe_serialization=True, max_shard_size="1000GB")
        tokenizer.save(str(staging / "tokenizer.json"))
        _json(staging / "adapter.json", settings)
        state.save_to_json(str(staging / "trainer-state.json"))
        if "family_exposure" in data_identity:
            _json(staging / "family-exposure.json", data_identity["family_exposure"])
        descriptor = {"schema_version": "modelbuilder.checkpoint/v1", "purpose": "last", "step": state.global_step,
            "identities": context["identities"], "resume_modes": ["warm_start"], "state": ["model"],
            "assets": _assets(staging), "framework": {"adapter": "modelbuilder.transformers.train/v1", "packages": _versions()},
            "data": data_identity, "training": {"trainer": "transformers.Trainer", "optimizer_state_saved": False},
            "execution": {"device": str(next(model.parameters()).device), "dtype": "float32",
                          "hardware_acceptance": "cpu_regression_tested" if context.get("device", "cpu") == "cpu" else "not_verified"},
            "history": state.log_history, "rights": context["output_artifact_rights"]}
        _json(staging / "checkpoint.manifest.json", descriptor)
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if context.get("checkpoint_ready"):
        context["checkpoint_ready"](path=destination, step=state.global_step, purpose="last")
    return destination


def train(*, inputs, parameters, context):
    _parameters(parameters, TRAIN_PARAMETERS)
    _versions()
    device = context.get("device", "cpu")
    from ..placement import DEVICE_PATTERN, runtime_device_identity
    if not isinstance(device, str) or not re.fullmatch(DEVICE_PATTERN, device):
        raise ValueError("Trainer requires cpu, mps, or an explicit cuda:<visible ordinal>")
    runtime_device_identity(device)  # Real runtime availability, never CPU fallback.
    if int(os.environ.get("WORLD_SIZE", "1")) != 1 or int(os.environ.get("LOCAL_RANK", "-1")) != -1:
        raise ValueError("Distributed Trainer execution requires a separately configured source-owned adapter")
    if ("stream" in inputs["train"] and parameters["batch_size"] * (parameters["gradient_accumulation_steps"] + 1)
            * parameters["architecture"]["max_length"] > parameters["architecture"]["limits"]["max_tokenized_tokens"]):
        raise ValueError("Streaming batch/accumulation/lookahead window exceeds the declared token resource limit")
    if context.get("resume") and context.get("resume_mode") != "warm_start":
        raise ValueError("Trainer v1 supports explicit model-only warm start, not exact continuation")
    from ...artifact_rights import combine_parent_rights
    parents = [{"artifact_id": "declared-model-inputs", "rights": context.get("declared_artifact_rights")},
               {"artifact_id": "training-data:" + identity(inputs["train"]), "rights": inputs["train"].get("rights")}]
    if context.get("resume"):
        parent = read_checkpoint(Path(context["resume"]["path"]))
        parents.append({"artifact_id": "warm-start:" + identity(parent), "rights": parent.get("rights")})
    context = {**context, "output_artifact_rights": combine_parent_rights(parents)}
    from transformers import Trainer, TrainerCallback, default_data_collator
    output = Path(context["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    with _device_environment(device), _runtime(parameters["seed"], device), tempfile.TemporaryDirectory(prefix="model-input-", dir=output) as temporary:
        initial = Path(temporary)
        _stage(parameters["architecture"], context, initial)
        model, tokenizer, settings = loader.load(initial)
        if context.get("resume"):
            previous = Path(context["resume"]["path"])
            validate_resume(previous, "warm_start", context["identities"], {"resume_modes": ["warm_start"]})
            _checkpoint_descriptor(previous)
            from safetensors.torch import load_model
            load_model(model, str(previous / "model.safetensors"), strict=True, device="cpu")
        from . import transformers_family_exposure as family_tracking
        exposure = family_tracking.Exposure(inputs["train"]["family_policy"]) if "family_policy" in inputs["train"] else None
        features, observe, source_identity = _training_inputs(inputs["train"], tokenizer, settings, exposure)

        def data_identity():
            return {**source_identity(), **({"family_policy": exposure.policy,
                     "family_exposure": exposure.snapshot(),
                     **({"final_dataset_family_coverage": deepcopy(inputs["train"]["final_dataset_family_coverage"])}
                        if "final_dataset_family_coverage" in inputs["train"] else {})} if exposure else {})}
        if "validation" in inputs:
            # Shared only with the native live evaluator. Trainer never sees
            # these held-out rows in its optimization dataset.
            _split(_bounded_evaluation_data(inputs["validation"], settings), {"validation"}, settings)
        final = None

        class Publish(TrainerCallback):
            def on_step_end(self, args, state, control, **kwargs):
                nonlocal final
                cancelled = bool(context.get("cancel_file") and Path(context["cancel_file"]).exists())
                if state.global_step % parameters["checkpoint_interval_steps"] == 0 or state.global_step == parameters["max_steps"] or cancelled:
                    final = _snapshot(kwargs["model"], tokenizer, settings, state, context, data_identity(), output)
                if cancelled:
                    raise InterruptedError("Training cancelled after preserving a warm-start checkpoint" +
                                           ("; family exposure " + exposure.snapshot()["assessment"]["status"] if exposure else ""))
                return control

        class ObservedTrainer(Trainer):
            def training_step(self, model, inputs, num_items_in_batch=None):
                for item in inputs.pop("_modelbuilder_lineage"):
                    observe(item)
                family_rows = inputs.pop("_modelbuilder_family", None)
                labels = inputs["labels"] if exposure else None
                loss = super().training_step(model, inputs, num_items_in_batch)
                if exposure:
                    exposure.stage(family_rows, labels, settings["task"], loss)
                return loss

        def collate(features):
            return {**default_data_collator([{key: value for key, value in feature.items() if key not in {"_lineage", "_family"}}
                                            for feature in features]),
                    "_modelbuilder_lineage": [feature["_lineage"] for feature in features],
                    **({"_modelbuilder_family": [feature["_family"] for feature in features]} if exposure else {})}

        options = {key: parameters[key] for key in ("max_steps", "learning_rate", "weight_decay", "warmup_steps",
                    "lr_scheduler_type", "max_grad_norm", "gradient_accumulation_steps", "seed")}
        arguments = _training_arguments(device, output_dir=str(output / "trainer"),
            per_device_train_batch_size=parameters["batch_size"], optim="adamw_torch", report_to=[],
            save_strategy="no", logging_strategy="steps", logging_steps=1, disable_tqdm=True,
            dataloader_num_workers=0, dataloader_pin_memory=False, push_to_hub=False,
            accelerator_config={"dispatch_batches": False, "split_batches": False, "even_batches": False},
            remove_unused_columns=False, **options)
        trainer = ObservedTrainer(model=model, args=arguments, train_dataset=features,
                                  data_collator=collate, callbacks=[Publish()])
        if (not _same_device(arguments.device, device) or not _same_device(trainer.accelerator.device, device)
                or trainer.accelerator.num_processes != 1
                or not _same_device(next(model.parameters()).device, device)):
            raise ValueError("Trainer/Accelerate placement differs from the explicitly selected device; fallback is disabled")
        if exposure:
            family_tracking.validate_runtime(trainer, model)
            trainer.add_callback(family_tracking.callback(exposure, lambda: trainer))
        trainer.train()
        if final is None:
            raise ValueError("Trainer produced no completed update/checkpoint")
    return {"trained": final}


def _checkpoint_descriptor(path):
    descriptor = read_checkpoint(path)
    if descriptor.get("framework", {}).get("adapter") not in {"modelbuilder.transformers.train/v1", "modelbuilder.transformers.train/v2"}:
        raise ValueError("Checkpoint was not produced by the selected Transformers adapter")
    required = {"config.json", "model.safetensors", "tokenizer.json", "adapter.json"}
    if not required.issubset({asset["path"] for asset in descriptor["assets"]}):
        raise ValueError("Checkpoint lacks hash-verified Transformers assets")
    settings = json.loads((path / "adapter.json").read_text())
    schema = deepcopy(ARCHITECTURE)
    schema["properties"].pop("model")
    schema["required"].remove("model")
    _parameters(settings, schema)
    return descriptor


def _loaded_checkpoint(path):
    return _checkpoint_descriptor(path), loader.load(path)


def evaluate(*, inputs, parameters, context):
    _parameters(parameters, EVALUATE_PARAMETERS)
    versions = _versions()
    import math
    import torch
    from transformers import Trainer, TrainingArguments, default_data_collator
    from ...evaluation import make_evaluation_record
    output = Path(context["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    with _device_environment("cpu"), _runtime(0):
        checkpoint, (model, tokenizer, settings) = _loaded_checkpoint(inputs["trained"])
        task = settings["task"]
        supported = {"cross_entropy", "accuracy"} if task == "sequence-classification" else {"cross_entropy", "perplexity"}
        if not set(parameters["metrics"]).issubset(supported):
            raise ValueError("Requested metric is incompatible with the checkpoint task")
        data = _bounded_evaluation_data(inputs["data"], settings)
        rows, data_identity = _split(data, {"validation", "test"}, settings)
        features = _features(rows, tokenizer, settings)
        def reduce_logits(logits, labels):
            if isinstance(logits, tuple):
                logits = logits[0]
            if task == "sequence-classification":
                loss = torch.nn.functional.cross_entropy(logits, labels, reduction="none")
                return torch.stack((loss, (logits.argmax(-1) == labels).float()), -1)
            shifted, targets = logits[:, :-1, :].contiguous(), labels[:, 1:].contiguous()
            losses = torch.nn.functional.cross_entropy(shifted.transpose(1, 2), targets, reduction="none", ignore_index=-100)
            return torch.stack((losses.sum(-1), (targets != -100).sum(-1)), -1)
        args = TrainingArguments(output_dir=str(output / "trainer-evaluation"), use_cpu=True,
            per_device_eval_batch_size=parameters["batch_size"], report_to=[], disable_tqdm=True,
            dataloader_num_workers=0, dataloader_pin_memory=False)
        trainer = Trainer(model=model, args=args, data_collator=default_data_collator, preprocess_logits_for_metrics=reduce_logits)
        measured = trainer.predict(features).predictions.tolist()
        from ...family_evaluation import measured_metrics, family_slices, family_metric_id, evaluation_cohort_identity
        from ...family_coverage import canonical_policy_identity
        if len(measured) != len(rows):
            raise ValueError("Evaluation predictions do not exactly cover registered rows")
        values = measured_metrics(measured, task)
        if "perplexity" in parameters["metrics"]:
            values["perplexity"] = math.exp(values["cross_entropy"])
        records = {}
        policy = parameters.get("family_policy")
        slices = [(None, list(range(len(rows))), None)]
        family_report = None
        if policy:
            groups = family_slices(rows, data_identity["split"]["sample_ids"], policy)
            family_report = {"policy_id": canonical_policy_identity(policy), "families": {}}
            slices.extend((family["id"], groups[family["id"]], family) for family in policy["families"])
        for family_id, indexes, family in slices:
            sample_ids = [data_identity["split"]["sample_ids"][index] for index in indexes]
            contributions = [measured[index] for index in indexes]
            if family:
                sufficient = len(indexes) >= family["min_eval_support"]
                family_report["families"][family_id] = {"support": len(indexes),
                    "minimum_support": family["min_eval_support"], "status": "available" if sufficient else "unavailable",
                    "cohort_digest": identity(sample_ids), "metric_ids": {metric: family_metric_id(family_id, metric) for metric in parameters["metrics"]}}
                if not sufficient:
                    continue  # Explicit unavailable report; native scientific gate sees missing evidence and blocks.
            sliced_values = measured_metrics(contributions, task)
            if "perplexity" in parameters["metrics"]:
                sliced_values["perplexity"] = math.exp(sliced_values["cross_entropy"])
            for metric in parameters["metrics"]:
                name = family_metric_id(family_id, metric) if family else metric
                predictions = [{"item_id": item, "negative_log_likelihood_sum": row[0], "token_count": int(row[1])}
                               if task == "causal-lm" else {"item_id": item, "value": row[0 if metric == "cross_entropy" else 1]}
                               for item, row in zip(sample_ids, contributions)]
                configuration = {"task": task, "batch_size": parameters["batch_size"],
                    "evaluation_cohort": evaluation_cohort_identity(data_identity["dataset"], data_identity["split"])}
                if family:
                    configuration["family"] = {"policy_id": canonical_policy_identity(policy), "family_id": family_id,
                        "minimum_support": family["min_eval_support"], "cohort_digest": identity(sample_ids)}
                records[name] = make_evaluation_record(dataset=data_identity["dataset"], split={**data_identity["split"], "sample_ids": sample_ids},
                    protocol={"implementation": "modelbuilder.transformers.evaluate", "version": VERSION,
                        "configuration": configuration,
                        "preprocessing": {key: settings[key] for key in ("text_field", "label_field", "labels", "max_length", "truncate", "pad_token_id")}, "seed": 0},
                    metric={"name": name, "implementation": "torch.nn.functional.cross_entropy" if metric != "accuracy" else "torch.argmax.eq.mean",
                        "version": versions["torch"], "parameters": {"exponentiate": metric == "perplexity"},
                        "aggregation": ("exp_token_weighted_mean" if metric == "perplexity" else "token_weighted_mean") if task == "causal-lm" else "mean",
                        "direction": "maximize" if metric == "accuracy" else "minimize"},
                    treatment={"model": checkpoint["identities"], "checkpoint": identity(checkpoint), "calibration": None, "reference": None},
                    value=sliced_values[metric], predictions=predictions,
                    provenance={"packages": versions, "consumption": data_identity, "source": context.get("source", {})})
                values[name] = sliced_values[metric]
    result = {key: record["value"] for key, record in records.items()}
    result["evaluation_records"] = records
    if family_report is not None:
        result["family_evaluation"] = family_report
    return {"metrics": result}


def export(*, inputs, parameters, context):
    _parameters(parameters, EXPORT_PARAMETERS)
    versions = _versions()
    import torch
    output = Path(context["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    with _runtime(0):
        checkpoint, (model, tokenizer, settings) = _loaded_checkpoint(inputs["trained"])
        for name in ("config.json", "model.safetensors", "tokenizer.json", "adapter.json"):
            shutil.copy2(inputs["trained"] / name, output / name)
        shutil.copy2(Path(loader.__file__), output / "loader.py")
        _json(output / "parity-input.json", {"texts": parameters["parity_texts"]})
        features = loader.encode(parameters["parity_texts"], tokenizer, settings)
        with torch.inference_mode():
            expected = model(**{key: torch.tensor([row[key] for row in features]) for key in features[0]}).logits
        environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
        environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", OMP_NUM_THREADS="1")
        subprocess.run([sys.executable, "-I", "-B", str(output / "loader.py"), str(output)], check=True,
                       capture_output=True, text=True, timeout=parameters["verification_timeout_seconds"], env=environment)
        actual_path = output / "parity-actual.pt"
        actual = torch.load(actual_path, map_location="cpu", weights_only=True)
        torch.testing.assert_close(actual, expected, atol=parameters["atol"], rtol=0)
        verification = {"passed": True, "atol": parameters["atol"], "rtol": 0,
            "max_abs_difference": float((actual - expected).abs().max()), "fresh_process": True,
            "sample_count": len(features), "device": "cpu", "dtype": "float32"}
        actual_path.unlink()
        _json(output / "verification.json", verification)
        descriptor = {"schema_version": "modelbuilder.export/v1", "selected_checkpoint": identity(checkpoint),
            "loader": {"path": "loader.py", "entrypoint": "predict", "format": "transformers-safetensors"},
            "signature": {"input": "list[str]", "output": "float32 logits", "task": settings["task"], "preprocessing": settings},
            "dependencies": versions, "source": context.get("source", checkpoint["identities"]["source"]),
            "verification": verification, "assets": _assets(output)}
        descriptor["rights"] = checkpoint.get("rights")
        _json(output / "export.manifest.json", descriptor)
    return {"exported": output}
