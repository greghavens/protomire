"""CPU Trainer v2: user-selected epoch policy and verified native continuation.

One full-horizon Trainer call owns optimization, RNG and scheduler restoration.
Only completed eager-dataset epoch boundaries are exact continuation points.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time

from . import PROTOCOL, identity, read_checkpoint, validate_resume
from . import transformers_training as base
from . import transformers_epoch_policy as epoch_policy
from ...rules import POLICY_SCHEMA, evaluate_policy, validate_policy
from ..rule_methods import preserve_training_rng

ADAPTER = "modelbuilder.transformers.train/v2"
PARAMETERS = deepcopy(base.TRAIN_PARAMETERS)
for key in ("max_steps", "checkpoint_interval_steps"):
    PARAMETERS["properties"].pop(key)
    PARAMETERS["required"].remove(key)
PARAMETERS["properties"].update(
    horizon_epochs={"type": "integer", "minimum": 1},
    max_lineage_records={"type": "integer", "minimum": 1},
    policy=POLICY_SCHEMA,
    epoch_bounds=epoch_policy.BOUNDS,
    evidence_bindings=epoch_policy.BINDINGS,
)
PARAMETERS["required"].extend(("horizon_epochs", "max_lineage_records", "policy"))
PARAMETERS["properties"]["gradient_accumulation_steps"] = {"const": 1, "type": "integer"}
_REQUIRED = {
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "adapter.json",
    "optimizer.pt",
    "scheduler.pt",
    "rng_state.pth",
    "trainer_state.json",
    "epoch-state.json",
}


def catalog_entries():
    descriptor = {
        "id": "modelbuilder.transformers.train",
        "version": "2",
        "protocol": PROTOCOL,
        "role": "training",
        "description": "Single-process CPU eager Trainer with completed-epoch policy checks and exact native-state continuation.",
        "parameter_schema": deepcopy(PARAMETERS),
        "capabilities": {
            "devices": ["cpu"],
            "resume_modes": ["exact_state", "warm_start"],
            "checkpoint_events": True,
            "frameworks": ["pytorch", "transformers", "accelerate"],
            "export_formats": [],
        },
    }
    return [
        {
            **descriptor,
            "component_digest": identity(descriptor),
            "inputs": [{"name": "train", "type": "dict"}, {"name": "validation", "type": "dict"}],
            "outputs": [{"name": "trained", "type": "Path"}],
            "adapter": {
                "module": __name__,
                "entrypoint": "train",
                "example_source": "examples/transformers_training/epochs_adapter.py",
                "source_binding": "Pin a reviewed v2 wrapper and the existing Trainer runtime.",
            },
        }
    ]


def _cancelled(context):
    return bool(context.get("cancel_file") and Path(context["cancel_file"]).exists())


def _contract(parameters, train_data, validation_data, steps):
    return {
        "parameters": deepcopy(parameters),
        "train": train_data,
        "validation": validation_data,
        "steps_per_epoch": steps,
        "scheduler_horizon_steps": parameters["horizon_epochs"] * steps,
        "sampler": {
            "algorithm": "accelerate.SeedableRandomSampler",
            "seed": parameters["seed"],
            "epoch_cursor": "completed_epochs",
            "workers": 0,
            "drop_last": False,
        },
    }


def _subject(context, data, parameters):
    if "rule_subject" in context:
        raise ValueError("Caller-selected rule_subject is not a native execution identity")
    if context.get("job_dir"):
        if not isinstance(context.get("epoch_dispatch"), dict) or not isinstance(
            context.get("epoch_native"), dict
        ):
            raise ValueError("Epoch adapter requires platform-owned native execution context")
        from ..epoch_context import verify

        native = verify(context["epoch_dispatch"], context.get("epoch_native"))
        return {
            "project_id": native["project_id"],
            "run_id": native["run_id"],
            "config_digest": native["config_digest"],
            "data_digest": identity(data),
        }
    # Direct local functions are not an admission/permission boundary and cannot
    # claim native run ownership. Their reproducible subject is derived, never supplied.
    return {
        "project_id": "local-component:" + data["train"]["dataset"]["id"],
        "run_id": "local-series:"
        + identity({"source": context["identities"]["source"], "parameters": parameters}),
        "config_digest": identity(parameters),
        "data_digest": identity(data),
    }


def _resume(path, context, contract, sample_digests, methods, metrics):
    descriptor = validate_resume(
        path, "exact_state", context["identities"], {"resume_modes": ["exact_state"]}
    )
    if (
        descriptor.get("framework") != {"adapter": ADAPTER, "packages": base._versions()}
        or {asset["path"] for asset in descriptor["assets"]} != _REQUIRED
        or descriptor.get("training", {}).get("contract_digest") != identity(contract)
    ):
        raise ValueError("Exact epoch checkpoint adapter, assets or original horizon differ")
    saved = json.loads((path / "epoch-state.json").read_text())
    native = json.loads((path / "trainer_state.json").read_text())
    completed = saved.get("completed_epochs")
    if (
        type(completed) is not int
        or not 0 < completed < contract["parameters"]["horizon_epochs"]
        or type(native.get("epoch")) not in (int, float)
        or native.get("epoch") != completed
        or type(native.get("global_step")) is not int
        or native.get("global_step") != completed * contract["steps_per_epoch"]
        or type(native.get("max_steps")) is not int
        or native.get("max_steps") != contract["scheduler_horizon_steps"]
        or descriptor["step"] != native["global_step"]
        or identity(saved.get("contract")) != identity(contract)
        or saved.get("sample_digests") != sample_digests
    ):
        raise ValueError("Exact resume requires unchanged complete native epoch/horizon and options")
    records = saved.get("lineage")
    ids = contract["train"]["split"]["sample_ids"]
    if not isinstance(records, list) or len(records) != completed * len(ids):
        raise ValueError("Checkpoint sampler/data cursor is incomplete")
    # Reuse the declared pinned sampler itself, rather than guessing a cursor or
    # accepting an arbitrary permutation of the registered rows.
    from accelerate.data_loader import SeedableRandomSampler
    import torch

    sampler = SeedableRandomSampler(
        range(len(ids)), data_seed=contract["parameters"]["seed"], generator=torch.Generator(device="cpu")
    )
    expected = []
    for epoch in range(completed):
        sampler.set_epoch(epoch)
        expected.extend(
            {"sample_id": ids[index], "content_digest": sample_digests[index]} for index in sampler
        )
    if identity(records) != identity(expected):
        raise ValueError("Checkpoint row order conflicts with the declared seedable epoch sampler")
    state = _state({asset["path"]: asset["sha256"] for asset in descriptor["assets"]}, completed, contract)
    subject = saved.get("evidence", {}).get("subject")
    history = saved.get("history")
    if not isinstance(history, list) or len(history) != completed:
        raise ValueError("Checkpoint scientific history is incomplete")
    for index, row in enumerate(history, 1):
        measured = [
            entry
            for entry in native["log_history"]
            if entry.get("epoch") == index and all(metric in entry for metric in metrics)
        ]
        if (
            set(row) != {"epoch", "metrics", "checkpoint_digest", "subject_digest"}
            or row["epoch"] != index
            or len(measured) != 1
            or identity(row["metrics"]) != identity({key: measured[0][key] for key in sorted(metrics)})
            or row["subject_digest"] != identity(subject)
        ):
            raise ValueError("Checkpoint scientific history differs from native completed-epoch measurements")
    if history[-1]["checkpoint_digest"] != identity(state):
        raise ValueError("Checkpoint history differs from actual native optimizer/scheduler/RNG state")
    bounds = epoch_policy.selected_bounds(contract["parameters"])
    cursor = {
        "completed_epochs": completed,
        "steps": native["global_step"],
        "runtime_seconds": saved["runtime_seconds"],
        "cost": 0,
    }
    expected_evidence = epoch_policy.evidence(
        contract["parameters"],
        subject,
        history,
        records,
        state,
        cursor,
        descriptor["identities"],
    )
    expected_admission = epoch_policy.admission(
        bounds, completed, native["global_step"], saved["runtime_seconds"]
    )
    if (
        identity(saved["evidence"]) != identity(expected_evidence)
        or identity(saved.get("admission")) != identity(expected_admission)
        or expected_admission["outcome"] != "continue"
        or bounds["resource_modes"]["runtime_seconds"] == "not_applicable"
        and saved["runtime_seconds"] != 0
    ):
        raise ValueError(
            "Checkpoint history/resource/cursor evidence conflicts with authoritative resume state"
        )
    from .rule_gate import gate

    with preserve_training_rng():
        expected_decision = gate(
            inputs={"checkpoint_state": state, "evidence": expected_evidence},
            parameters={"policy": contract["parameters"]["policy"]},
            context={"rule_methods": methods},
        )["result"]
    if (
        identity(saved.get("decision")) != identity(expected_decision)
        or expected_decision["outcome"] != "continue"
    ):
        raise ValueError("Exact resume requires the unchanged allowing combined policy decision")
    return saved


def _state(hashes, completed, contract):
    return {
        "schema_version": "modelbuilder.rule-checkpoint-state/v1",
        "resume_modes": ["exact_state"],
        "state": {
            key: {"kind": "value", "value": value}
            for key, value in {
                "model": hashes["model.safetensors"],
                "optimizer": hashes["optimizer.pt"],
                "scheduler": hashes["scheduler.pt"],
                "rng": hashes["rng_state.pth"],
                "data_cursor": {
                    "completed_epochs": completed,
                    "sampler": contract["sampler"],
                    "data_digest": identity(contract["train"]),
                },
            }.items()
        },
    }


def train(*, inputs, parameters, context):
    base._parameters(parameters, PARAMETERS)
    if any(
        type(parameters[key]) is not int
        for key in (
            "seed",
            "batch_size",
            "gradient_accumulation_steps",
            "warmup_steps",
            "horizon_epochs",
            "max_lineage_records",
        )
    ):
        raise ValueError("Trainer epoch count/seed options require actual integers")
    if set(inputs) != {"train", "validation"}:
        raise ValueError("Epoch training requires explicit registered train and validation inputs")
    versions = base._versions()
    if versions["transformers"] != "4.57.1" or versions["accelerate"] != "1.11.0":
        raise ValueError("Epoch continuation requires pinned Transformers 4.57.1 and Accelerate 1.11.0")
    policy = validate_policy(parameters["policy"])
    bounds, selected_metrics = epoch_policy.validate_options(parameters)
    if (
        context.get("device", "cpu") != "cpu"
        or int(os.environ.get("WORLD_SIZE", "1")) != 1
        or int(os.environ.get("LOCAL_RANK", "-1")) != -1
        or any("stream" in value for value in inputs.values())
    ):
        raise ValueError("Trainer v2 exact epochs require single-process CPU and eager registered datasets")
    if epoch_policy.admission(bounds, 0, 0, 0)["outcome"] != "continue":
        raise ValueError("Epoch-zero active budget is exhausted before optimization")
    methods = context.get("epoch_methods", ())
    if any(rule["kind"] == "method/v1" for rule in policy["rules"]) and not methods:
        raise ValueError("Epoch custom policy requires admitted source-pinned method registrations")
    if _cancelled(context):
        raise InterruptedError("Training cancelled before any epoch")
    from ...artifact_rights import combine_parent_rights

    parents = [
        {"artifact_id": "declared-model-inputs", "rights": context.get("declared_artifact_rights")},
        {
            "artifact_id": "training-data:" + identity(inputs["train"]),
            "rights": inputs["train"].get("rights"),
        },
    ]
    if context.get("resume"):
        parent = read_checkpoint(Path(context["resume"]["path"]))
        parents.append({"artifact_id": "resume:" + identity(parent), "rights": parent.get("rights")})
    context = {**context, "output_artifact_rights": combine_parent_rights(parents)}
    from transformers import Trainer, TrainerCallback, TrainingArguments, default_data_collator
    from accelerate.state import AcceleratorState

    AcceleratorState._reset_state(reset_partial_state=True)
    output = Path(context["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    if (output / "trainer").exists() or (output / "checkpoints").exists():
        raise ValueError("Epoch training output already exists; retries must not replay optimization")
    with (
        base._device_environment("cpu"),
        base._runtime(parameters["seed"]),
        tempfile.TemporaryDirectory(prefix="model-input-", dir=output) as temporary,
    ):
        initial = Path(temporary)
        base._stage(parameters["architecture"], context, initial)
        model, tokenizer, settings = base.loader.load(initial)
        rows, train_data = base._split(inputs["train"], {"train"}, settings)
        valid_rows, validation_data = base._split(inputs["validation"], {"validation"}, settings)
        steps = math.ceil(len(rows) / parameters["batch_size"])
        if (
            bounds["next_epoch_steps"] != steps
            or bounds["max_steps"] < steps
            or len(rows) * parameters["horizon_epochs"] > parameters["max_lineage_records"]
        ):
            raise ValueError(
                "Epoch step projection or explicit lineage capacity differs from the full horizon"
            )
        contract = _contract(parameters, train_data, validation_data, steps)
        subject = _subject(context, {"train": train_data, "validation": validation_data}, parameters)
        features = base._features(rows, tokenizer, settings)
        for item, sample, content in zip(
            features, train_data["split"]["sample_ids"], inputs["train"]["consumption"]["sample_digests"]
        ):
            item["_lineage"] = {"sample_id": sample, "content_digest": content}
        lineage, history, elapsed_before, resume_path = [], [], 0, None
        if context.get("resume"):
            previous = Path(context["resume"]["path"])
            mode = context.get("resume_mode")
            if mode == "exact_state":
                saved = _resume(
                    previous,
                    context,
                    contract,
                    inputs["train"]["consumption"]["sample_digests"],
                    methods,
                    selected_metrics,
                )
                lineage, history, elapsed_before = (
                    saved["lineage"],
                    saved["history"],
                    saved["runtime_seconds"],
                )
                origin = saved["evidence"]["subject"]
                if (
                    origin["project_id"] != subject["project_id"]
                    or origin["data_digest"] != subject["data_digest"]
                ):
                    raise ValueError("Exact continuation series belongs to different project/data")
                subject = origin
                resume_path = str(previous)
            elif mode == "warm_start":
                validate_resume(
                    previous, "warm_start", context["identities"], {"resume_modes": ["warm_start"]}
                )
                base._checkpoint_descriptor(previous)
                from safetensors.torch import load_model

                load_model(model, str(previous / "model.safetensors"), strict=True, device="cpu")
            else:
                raise ValueError("Explicit exact_state or warm_start resume mode required")
        started, final = time.monotonic(), None

        class EpochControl(TrainerCallback):
            def on_step_begin(self, args, state, control, **kwargs):
                if _cancelled(context):
                    raise InterruptedError(
                        "Training cancelled; only earlier completed epochs are exactly resumable"
                    )
                return control

        class EpochTrainer(Trainer):
            def training_step(self, model, inputs, num_items_in_batch=None):
                lineage.extend(inputs.pop("_modelbuilder_lineage"))
                if len(lineage) > parameters["max_lineage_records"]:
                    raise ValueError("Actual lineage exceeds its explicit capacity")
                return super().training_step(model, inputs, num_items_in_batch)

            def _save_checkpoint(self, model, trial):
                nonlocal final
                completed = round(self.state.epoch)
                if (
                    self.state.epoch != completed
                    or self.state.global_step != completed * steps
                    or len(lineage) != completed * len(rows)
                ):
                    raise ValueError("Partial epochs cannot publish exact checkpoints")
                super()._save_checkpoint(model, trial)  # Native optimizer, scheduler, RNG and Trainer state.
                native = Path(self.args.output_dir) / f"checkpoint-{self.state.global_step}"
                destination = output / "checkpoints" / f"{self.state.global_step:012d}"
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise ValueError(
                        "Epoch checkpoint already exists; retry with a new native output directory"
                    )
                pending = Path(tempfile.mkdtemp(prefix=".pending-", dir=destination.parent))
                try:
                    for name in _REQUIRED - {"tokenizer.json", "adapter.json", "epoch-state.json"}:
                        shutil.copyfile(native / name, pending / name)
                    tokenizer.save(str(pending / "tokenizer.json"))
                    base._json(pending / "adapter.json", settings)
                    hashes = {asset["path"]: asset["sha256"] for asset in base._assets(pending)}
                    checkpoint_state = _state(hashes, completed, contract)
                    checkpoint_digest = identity(checkpoint_state)
                    measured = {key: self.state.log_history[-1].get(key) for key in sorted(selected_metrics)}
                    if any(
                        type(value) not in (int, float) or not math.isfinite(value)
                        for value in measured.values()
                    ):
                        raise ValueError("Completed epoch lacks explicitly selected finite held-out metrics")
                    history.append(
                        {
                            "epoch": completed,
                            "metrics": measured,
                            "checkpoint_digest": checkpoint_digest,
                            "subject_digest": identity(subject),
                        }
                    )
                    runtime = (
                        elapsed_before + time.monotonic() - started
                        if bounds["resource_modes"]["runtime_seconds"] == "active"
                        else 0
                    )
                    cursor = {
                        "completed_epochs": completed,
                        "steps": self.state.global_step,
                        "runtime_seconds": runtime,
                        "cost": 0,
                    }
                    evidence = epoch_policy.evidence(
                        parameters,
                        subject,
                        history,
                        lineage,
                        checkpoint_state,
                        cursor,
                        context["identities"],
                    )
                    with preserve_training_rng():
                        decision = evaluate_policy(policy, evidence, extensions=methods)
                        if decision["outcome"] == "continue":
                            from .rule_gate import gate

                            checked = gate(
                                inputs={"checkpoint_state": checkpoint_state, "evidence": evidence},
                                parameters={"policy": policy},
                                context={"rule_methods": methods},
                            )["result"]
                            if identity(checked) != identity(decision):
                                raise ValueError("Epoch method produced a nondeterministic decision")
                    admitted = epoch_policy.admission(bounds, completed, self.state.global_step, runtime)
                    saved = {
                        "completed_epochs": completed,
                        "contract": contract,
                        "lineage": lineage,
                        "sample_digests": inputs["train"]["consumption"]["sample_digests"],
                        "history": history,
                        "runtime_seconds": runtime,
                        "evidence": evidence,
                        "decision": decision,
                        "admission": admitted,
                    }
                    base._json(pending / "epoch-state.json", saved)
                    descriptor = {
                        "schema_version": "modelbuilder.checkpoint/v1",
                        "purpose": "last",
                        "step": self.state.global_step,
                        "identities": context["identities"],
                        "resume_modes": ["exact_state", "warm_start"],
                        "state": ["model", "optimizer", "scheduler", "rng", "data_cursor"],
                        "assets": base._assets(pending),
                        "framework": {"adapter": ADAPTER, "packages": base._versions()},
                        "training": {
                            "trainer": "transformers.Trainer",
                            "contract_digest": identity(contract),
                            "optimizer_state_saved": True,
                        },
                        "data": {
                            **train_data,
                            "iteration": "trainer_seedable_random_sampler",
                            "consumption": {
                                "count": len(lineage),
                                "ordered_lineage_digest": identity(lineage),
                            },
                        },
                        "execution": {
                            "device": "cpu",
                            "dtype": "float32",
                            "completed_epoch": completed,
                            "current_subject": _subject(
                                context, {"train": train_data, "validation": validation_data}, parameters
                            ),
                            "series_subject": subject,
                            "parent_checkpoint_digest": identity(parent) if context.get("resume") else None,
                        },
                        "history": self.state.log_history,
                        "rights": context["output_artifact_rights"],
                    }
                    base._json(pending / "checkpoint.manifest.json", descriptor)
                    read_checkpoint(pending)
                    pending.rename(destination)
                except BaseException:
                    shutil.rmtree(pending, ignore_errors=True)
                    raise
                final = destination
                if decision["outcome"] in {"blocked", "cancelled"}:
                    raise ValueError(
                        "Epoch policy " + decision["outcome"] + ":" + ",".join(decision["reasons"])
                    )
                if admitted["outcome"] == "blocked":
                    raise ValueError("Epoch resource bound exceeded:" + ",".join(admitted["reasons"]))
                if decision["outcome"] == "stop" or admitted["outcome"] == "stop":
                    self.control.should_training_stop = True
                if context.get("checkpoint_ready"):
                    context["checkpoint_ready"](path=final, step=self.state.global_step, purpose="last")
                if _cancelled(context):
                    raise InterruptedError("Training cancelled after a completed exact epoch checkpoint")

        def collate(items):
            result = default_data_collator(
                [{k: v for k, v in item.items() if k != "_lineage"} for item in items]
            )
            if "_lineage" in items[0]:
                result["_modelbuilder_lineage"] = [item["_lineage"] for item in items]
            return result

        def metrics(prediction):
            return {"accuracy": float((prediction.predictions.argmax(-1) == prediction.label_ids).mean())}

        options = {
            key: parameters[key]
            for key in (
                "learning_rate",
                "weight_decay",
                "warmup_steps",
                "lr_scheduler_type",
                "max_grad_norm",
                "gradient_accumulation_steps",
                "seed",
            )
        }
        arguments = TrainingArguments(
            use_cpu=True,
            output_dir=str(output / "trainer"),
            num_train_epochs=parameters["horizon_epochs"],
            data_seed=parameters["seed"],
            per_device_train_batch_size=parameters["batch_size"],
            per_device_eval_batch_size=parameters["batch_size"],
            optim="adamw_torch",
            report_to=[],
            save_strategy="epoch",
            eval_strategy="epoch",
            logging_strategy="no",
            disable_tqdm=True,
            dataloader_num_workers=0,
            dataloader_pin_memory=False,
            push_to_hub=False,
            remove_unused_columns=False,
            accelerator_config={
                "dispatch_batches": False,
                "split_batches": False,
                "even_batches": False,
                "use_seedable_sampler": True,
            },
            **options,
        )
        trainer = EpochTrainer(
            model=model,
            args=arguments,
            train_dataset=features,
            eval_dataset=base._features(valid_rows, tokenizer, settings),
            data_collator=collate,
            compute_metrics=metrics if "eval_accuracy" in selected_metrics else None,
            callbacks=[EpochControl()],
        )
        trainer.train(resume_from_checkpoint=resume_path)
        if final is None:
            raise ValueError("Trainer produced no completed epoch checkpoint")
    return {"trained": final}
