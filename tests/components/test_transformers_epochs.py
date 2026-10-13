"""Real Trainer epoch policy and fresh-process exact-state dropout continuation."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from jsonschema.exceptions import ValidationError
from modelbuilder.backend.components import component_catalog, identity, read_checkpoint
from modelbuilder.backend.components import transformers_epochs as epochs
from test_transformers_training import local as registered_local

local = registered_local


@pytest.fixture
def epoch_local(local):
    from transformers import BertConfig, BertForSequenceClassification
    from safetensors.torch import save_model
    import torch

    assets = local["tmp"] / "assets"
    config = BertConfig.from_json_file(str(assets / "config.json"))
    config.hidden_dropout_prob = 0.25
    config.attention_probs_dropout_prob = 0.2
    torch.manual_seed(10)
    model = BertForSequenceClassification(config)
    config.to_json_file(assets / "config.json", use_diff=False)
    save_model(model, str(assets / "weights.safetensors"))
    parameters = deepcopy(local["parameters"])
    for name in ("max_steps", "checkpoint_interval_steps"):
        parameters.pop(name)
    for key in ("config", "weights"):
        parameters["architecture"]["model"][key]["sha256"] = hashlib.sha256(
            Path(local["context"]["dependencies"][key]).read_bytes()
        ).hexdigest()
    parameters.update(horizon_epochs=4, max_lineage_records=16, lr_scheduler_type="linear", warmup_steps=1)
    parameters["policy"] = {
        "schema_version": "modelbuilder.rules/v1",
        "id": "epoch-options",
        "version": "1",
        "rules": [
            {
                "id": "curve",
                "kind": "convergence/v1",
                "metric": "eval_loss",
                "criterion": "target",
                "direction": "minimize",
                "threshold": 0,
                "max_epochs": 4,
                "window": 2,
                "insufficient_window": "collect_within_bounds",
                "min_epochs": 1,
                "max_steps": 8,
                "next_epoch_steps": 2,
                "step_unit": "optimizer_steps",
                "max_runtime_seconds": 0,
                "next_epoch_seconds": 0,
                "max_cost": 0,
                "next_epoch_cost": 0,
                "cost_unit": "local-free",
                "resource_modes": {
                    "steps": "active",
                    "runtime_seconds": "not_applicable",
                    "cost": "not_applicable",
                },
            }
        ],
    }
    from tests.components.rights_test_support import local_review

    inventory = [
        {
            "path": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }
        for path in [
            *(Path(value) for value in local["context"]["dependencies"].values()),
            local["tmp"] / "data/rows.jsonl",
        ]
    ]
    review, rights = local_review(
        local["project"]["project_id"],
        commit=identity(inventory),
        source_files=inventory,
        repo_id="urn:modelbuilder:test-authored-tiny-transformers-dropout",
    )
    local["policy"], local["rights"] = review, rights
    local["inputs"] = {key: {**value, "rights": rights} for key, value in local["inputs"].items()}
    local["context"]["declared_artifact_rights"] = rights
    inputs = {"train": local["inputs"]["train"], "validation": local["inputs"]["data"]}
    context = deepcopy(local["context"])

    return {**local, "parameters": parameters, "inputs": inputs, "context": context}


CHILD = """import json,sys
from pathlib import Path
from modelbuilder.backend.components.transformers_epochs import train
request=json.loads(Path(sys.argv[1]).read_text())
if any(rule['kind']=='method/v1' for rule in request['parameters']['policy']['rules']):
    from modelbuilder.backend.rule_methods import load_methods
    source=request['context']['source_root']
    request['context']['epoch_methods']=load_methods(request['parameters']['policy'], {'source':request['context']['source'], 'source_root':source, 'trusted_code_roots':[source]})
stop=request.pop('cancel_after_epoch', None)
if stop is not None:
    def ready(**event):
        if event['step']==stop*2: Path(request['context']['cancel_file']).write_text('{}')
    request['context']['checkpoint_ready']=ready
try:
    result=train(**request)
    Path(sys.argv[2]).write_text(json.dumps({'status':'completed','path':str(result['trained'])}))
except InterruptedError as error:
    Path(sys.argv[2]).write_text(json.dumps({'status':'cancelled','message':str(error)}))
"""


def run_child(local, name, *, resume=None, mode="exact_state", cancel_after_epoch=None, parameters=None):
    output = local["tmp"] / name
    request = {
        "inputs": local["inputs"],
        "parameters": parameters or local["parameters"],
        "context": {
            **local["context"],
            "output_dir": str(output),
            "cancel_file": str(local["tmp"] / (name + ".cancel")),
        },
    }
    if resume:
        request["context"].update(resume={"path": str(resume)}, resume_mode=mode)
    if cancel_after_epoch:
        request["cancel_after_epoch"] = cancel_after_epoch
    request_path, result_path = local["tmp"] / (name + ".json"), local["tmp"] / (name + ".result.json")
    request_path.write_text(json.dumps(request))
    script = local["tmp"] / "run_epochs.py"
    script.write_text(CHILD)
    env = {
        **os.environ,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
    }
    result = subprocess.run(
        [sys.executable, str(script), str(request_path), str(result_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
    )
    (local["tmp"] / (name + ".log")).write_text(result.stdout + result.stderr)
    assert result.returncode == 0, (result.stdout + result.stderr)[-12000:]
    return json.loads(result_path.read_text())


def assert_state_equal(a, b):
    import torch
    import numpy as np

    if isinstance(a, torch.Tensor):
        assert isinstance(b, torch.Tensor) and torch.equal(a, b)
    elif isinstance(a, np.ndarray):
        assert isinstance(b, np.ndarray) and np.array_equal(a, b)
    elif isinstance(a, dict):
        assert a.keys() == b.keys()
        for key in a:
            assert_state_equal(a[key], b[key])
    elif isinstance(a, (tuple, list)):
        assert type(a) is type(b) and len(a) == len(b)
        for x, y in zip(a, b):
            assert_state_equal(x, y)
    else:
        assert a == b


def test_epoch_catalog_is_inert_and_v1_is_unchanged():
    catalog = component_catalog()
    assert next(
        row for row in catalog if row["id"] == "modelbuilder.transformers.train" and row["version"] == "1"
    )["capabilities"]["resume_modes"] == ["warm_start"]
    entry = next(
        row for row in catalog if row["id"] == "modelbuilder.transformers.train" and row["version"] == "2"
    )
    assert entry["capabilities"]["resume_modes"] == ["exact_state", "warm_start"]
    assert entry["capabilities"]["devices"] == ["cpu"]


@pytest.mark.parametrize("batch_size", [2, 3])
def test_dropout_uninterrupted_matches_fresh_process_exact_resume(epoch_local, batch_size):
    epoch_local["parameters"]["batch_size"] = batch_size
    import torch
    from safetensors.torch import load_file

    full = Path(run_child(epoch_local, "full")["path"])
    stopped = run_child(epoch_local, "partial", cancel_after_epoch=2)
    assert stopped["status"] == "cancelled"
    parent = epoch_local["tmp"] / "partial/checkpoints/000000000004"
    assert read_checkpoint(parent)["execution"]["completed_epoch"] == 2
    resumed = Path(run_child(epoch_local, "resumed", resume=parent)["path"])
    assert_state_equal(load_file(full / "model.safetensors"), load_file(resumed / "model.safetensors"))
    for name in ("optimizer.pt", "scheduler.pt"):
        assert_state_equal(
            torch.load(full / name, weights_only=True), torch.load(resumed / name, weights_only=True)
        )
    # Native RNG file contains NumPy/Python values. This is test-owned data in a
    # local fixture; product restoration remains Trainer's pinned safe loader.
    assert_state_equal(
        torch.load(full / "rng_state.pth", weights_only=False),
        torch.load(resumed / "rng_state.pth", weights_only=False),
    )
    a = json.loads((full / "epoch-state.json").read_text())
    b = json.loads((resumed / "epoch-state.json").read_text())
    assert a["lineage"] == b["lineage"] and len(a["lineage"]) == 16
    orders = [[row["sample_id"] for row in a["lineage"][start : start + 4]] for start in range(0, 16, 4)]
    assert all(sorted(order) == ["0", "1", "2", "3"] for order in orders)
    assert len({tuple(order) for order in orders}) > 1
    assert a["contract"] == b["contract"] and a["contract"]["scheduler_horizon_steps"] == 8
    assert [row["metrics"] for row in a["history"]] == [row["metrics"] for row in b["history"]]
    assert a["decision"]["outcome"] == b["decision"]["outcome"] == "stop"
    sa = json.loads((full / "trainer_state.json").read_text())
    sb = json.loads((resumed / "trainer_state.json").read_text())
    for key in ("global_step", "epoch", "max_steps", "num_train_epochs"):
        assert sa[key] == sb[key]
    assert_state_equal(sa["log_history"][0]["eval_loss"], sb["log_history"][0]["eval_loss"])


def checkpoint_options(local, name):
    return {**local["context"], "output_dir": str(local["tmp"] / name)}


@pytest.mark.parametrize("metric", ["eval_loss", "eval_accuracy"])
def test_selected_target_stops_at_minimum_and_bounds_stop_without_claiming_convergence(epoch_local, metric):
    selected = deepcopy(epoch_local["parameters"])
    selected["policy"]["rules"][0].update(
        metric=metric,
        threshold=10 if metric == "eval_loss" else 0,
        direction="minimize" if metric == "eval_loss" else "maximize",
        min_epochs=2,
    )
    result = epochs.train(
        inputs=epoch_local["inputs"], parameters=selected, context=checkpoint_options(epoch_local, "target")
    )
    state = json.loads((result["trained"] / "epoch-state.json").read_text())
    assert state["completed_epochs"] == 2 and state["decision"]["reasons"] == ["criterion_met"]
    limited = deepcopy(epoch_local["parameters"])
    limited["policy"]["rules"][0]["max_steps"] = 4
    result = epochs.train(
        inputs=epoch_local["inputs"], parameters=limited, context=checkpoint_options(epoch_local, "bound")
    )
    state = json.loads((result["trained"] / "epoch-state.json").read_text())
    assert state["completed_epochs"] == 2 and state["decision"]["reasons"] == ["resource_bound:max_steps"]
    assert state["contract"]["scheduler_horizon_steps"] == 8


def test_partial_cancellation_publishes_only_completed_epochs(epoch_local, monkeypatch):
    from transformers import Trainer

    original = Trainer.training_step
    cancel = epoch_local["tmp"] / "partial.cancel"
    batches = 0

    def training_step(self, *args, **kwargs):
        nonlocal batches
        value = original(self, *args, **kwargs)
        batches += 1
        if batches == 3:
            cancel.write_text("{}")
        return value

    monkeypatch.setattr(Trainer, "training_step", training_step)
    context = {**checkpoint_options(epoch_local, "partial"), "cancel_file": str(cancel)}
    with pytest.raises(InterruptedError, match="earlier completed"):
        epochs.train(inputs=epoch_local["inputs"], parameters=epoch_local["parameters"], context=context)
    outputs = list((epoch_local["tmp"] / "partial/checkpoints").glob("*/checkpoint.manifest.json"))
    assert len(outputs) == 1 and read_checkpoint(outputs[0].parent)["step"] == 2
    assert batches == 3
    cancel.unlink()
    resumed = epochs.train(
        inputs=epoch_local["inputs"],
        parameters=epoch_local["parameters"],
        context={
            **checkpoint_options(epoch_local, "resume_partial"),
            "resume": {"path": str(outputs[0].parent)},
            "resume_mode": "exact_state",
        },
    )
    assert read_checkpoint(resumed["trained"])["step"] == 8


@pytest.mark.parametrize(
    "case",
    [
        "missing-optimizer",
        "tampered-rng",
        "partial-trainer-state",
        "changed-horizon",
        "changed-seed",
        "changed-policy",
        "changed-cursor",
        "wrong-adapter",
        "stopped-decision",
        "bool-contract",
        "bool-decision",
    ],
)
def test_exact_resume_rejects_tampered_partial_or_changed_contract(epoch_local, case):
    import shutil

    result = run_child(epoch_local, "interrupted", cancel_after_epoch=2)
    assert result["status"] == "cancelled"
    parent = epoch_local["tmp"] / "interrupted/checkpoints/000000000004"
    copied = epoch_local["tmp"] / "candidate"
    shutil.copytree(parent, copied)
    parameters = deepcopy(epoch_local["parameters"])
    manifest = json.loads((copied / "checkpoint.manifest.json").read_text())
    if case == "missing-optimizer":
        (copied / "optimizer.pt").unlink()
    elif case == "tampered-rng":
        (copied / "rng_state.pth").write_bytes(b"tampered")
    elif case == "partial-trainer-state":
        state = json.loads((copied / "trainer_state.json").read_text())
        state["epoch"] = 1.5
        (copied / "trainer_state.json").write_text(json.dumps(state))
    elif case == "changed-horizon":
        parameters["horizon_epochs"] = 5
        parameters["max_lineage_records"] = 20
    elif case == "changed-seed":
        parameters["seed"] += 1
    elif case == "changed-policy":
        parameters["policy"]["rules"][0]["threshold"] = 0.1
    elif case == "wrong-adapter":
        manifest["framework"]["adapter"] = "modelbuilder.transformers.train/v1"
    else:
        state = json.loads((copied / "epoch-state.json").read_text())
        if case == "changed-cursor":
            state["lineage"][0]["sample_id"] = state["lineage"][1]["sample_id"]
        elif case == "bool-contract":
            state["contract"]["parameters"]["warmup_steps"] = True
        elif case == "bool-decision":
            state["decision"]["rules"][0]["selected_options"]["max_cost"] = False
        else:
            state["decision"]["outcome"] = "stop"
        (copied / "epoch-state.json").write_text(json.dumps(state))
    # Even resealing a modified JSON file cannot make partial state or a
    # different scientific decision qualify as an exact continuation boundary.
    if case in {
        "partial-trainer-state",
        "changed-cursor",
        "stopped-decision",
        "wrong-adapter",
        "bool-contract",
        "bool-decision",
    }:
        manifest["assets"] = epochs.base._assets(copied)
        (copied / "checkpoint.manifest.json").write_text(json.dumps(manifest))
    with pytest.raises((ValueError, FileNotFoundError)):
        epochs.train(
            inputs=epoch_local["inputs"],
            parameters=parameters,
            context={
                **checkpoint_options(epoch_local, "denied"),
                "resume": {"path": str(copied)},
                "resume_mode": "exact_state",
            },
        )
    assert not list((epoch_local["tmp"] / "denied").rglob("checkpoint.manifest.json"))


def test_warm_start_resets_horizon_and_is_not_exact_continuation(epoch_local):
    run_child(epoch_local, "first", cancel_after_epoch=2)
    parent = epoch_local["tmp"] / "first/checkpoints/000000000004"
    warm = Path(run_child(epoch_local, "warm", resume=parent, mode="warm_start")["path"])
    exact = Path(run_child(epoch_local, "exact", resume=parent)["path"])
    from safetensors.torch import load_file
    import torch

    a, b = load_file(warm / "model.safetensors"), load_file(exact / "model.safetensors")
    assert any(not torch.equal(a[key], b[key]) for key in a)
    state = json.loads((warm / "epoch-state.json").read_text())
    assert state["completed_epochs"] == 4 and len(state["lineage"]) == 16


@pytest.mark.integration
@pytest.mark.parametrize("parallel", [1, 2])
def test_registered_native_epoch_adapter_evaluates_and_exports_via_existing_worker(epoch_local, parallel):
    from test_transformers_training import (
        test_registered_data_native_zenml_train_evaluate_export as native_pipeline,
    )
    from modelbuilder.backend.components import transformers_training

    source = epoch_local["source"]
    wrapper = source / "adapter.py"
    wrapper.write_text(
        "from modelbuilder.backend.components.transformers_epochs import train\nfrom modelbuilder.backend.components.transformers_training import evaluate, export\n"
    )
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "Reviewed synthetic epoch wrapper",
        ],
        check=True,
    )
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    epoch_local["context"]["source"] = {
        "commit": commit,
        "files": [{"path": "adapter.py", "sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest()}],
    }
    epoch_local["parameters"]["horizon_epochs"] = 3
    epoch_local["parameters"]["policy"]["rules"][0].update(max_epochs=3, max_steps=6)
    epoch_local["catalog_entries"] = [
        epochs.catalog_entries()[0],
        *transformers_training.catalog_entries()[1:],
    ]
    # This existing acceptance helper writes a real pinned recipe, independently
    # reviewed rights, registered portable data and invokes the native worker.
    # It also verifies actual native evaluation and fresh-process exported logits.
    epoch_local["execution"] = {"max_parallel_stages": parallel}
    epoch_local["request_extensions"] = {
        "rule_subject": {
            "project_id": "forged",
            "run_id": "forged",
            "config_digest": "0" * 64,
            "data_digest": "0" * 64,
        }
    }
    native_pipeline(epoch_local, False, True)
    checkpoint = epoch_local["tmp"] / "job/outputs/train/checkpoints/000000000006"
    descriptor = read_checkpoint(checkpoint)
    assert descriptor["framework"]["adapter"] == epochs.ADAPTER
    assert descriptor["resume_modes"] == ["exact_state", "warm_start"]
    state = json.loads((checkpoint / "epoch-state.json").read_text())
    assert state["completed_epochs"] == 3 and len(state["lineage"]) == 12
    assert state["decision"]["outcome"] == "stop"
    assert (
        state["evidence"]["subject"]["run_id"]
        == json.loads((epoch_local["tmp"] / "job/native.json").read_text())["backend_run_id"]
    )


@pytest.mark.parametrize(
    "case",
    [
        "pre-cancelled",
        "stream",
        "device",
        "lineage-capacity",
        "step-projection",
        "first-runtime-bound",
        "missing-validation",
    ],
)
def test_epoch_admission_rejects_incomplete_evidence_and_resource_conflicts(epoch_local, case, monkeypatch):
    from transformers import Trainer

    monkeypatch.setattr(
        Trainer, "train", lambda *a, **k: pytest.fail("Denied admission reached optimization")
    )
    parameters = deepcopy(epoch_local["parameters"])
    inputs = deepcopy(epoch_local["inputs"])
    context = checkpoint_options(epoch_local, "admission-denied")
    if case == "pre-cancelled":
        path = epoch_local["tmp"] / "cancel"
        path.write_text("{}")
        context["cancel_file"] = str(path)
    elif case == "stream":
        inputs["train"]["stream"] = {}
    elif case == "device":
        context["device"] = "mps"
    elif case == "lineage-capacity":
        parameters["max_lineage_records"] = 1
    elif case == "step-projection":
        parameters["policy"]["rules"][0]["next_epoch_steps"] = 1
    elif case == "first-runtime-bound":
        rule = parameters["policy"]["rules"][0]
        rule["resource_modes"]["runtime_seconds"] = "active"
        rule["max_runtime_seconds"] = 1
        rule["next_epoch_seconds"] = 2
    else:
        del inputs["validation"]
    with pytest.raises((ValueError, KeyError, InterruptedError)):
        epochs.train(inputs=inputs, parameters=parameters, context=context)


def test_retry_existing_output_fails_before_replaying_any_optimization(epoch_local, monkeypatch):
    from transformers import Trainer

    result = epochs.train(
        inputs=epoch_local["inputs"],
        parameters=epoch_local["parameters"],
        context=checkpoint_options(epoch_local, "existing"),
    )
    before = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (epoch_local["tmp"] / "existing/checkpoints").rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(Trainer, "train", lambda *a, **k: pytest.fail("Retry replayed optimization"))
    with pytest.raises(ValueError, match="must not replay"):
        epochs.train(
            inputs=epoch_local["inputs"],
            parameters=epoch_local["parameters"],
            context=checkpoint_options(epoch_local, "existing"),
        )
    after = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (epoch_local["tmp"] / "existing/checkpoints").rglob("*")
        if path.is_file()
    }
    assert before == after and read_checkpoint(result["trained"])["step"] == 8


@pytest.mark.parametrize("budget", ["runtime", "steps", "epochs", "cost"])
def test_epoch_zero_exhausted_or_unaccounted_budget_rejects_before_asset_reads(
    epoch_local, budget, monkeypatch
):
    monkeypatch.setattr(
        epochs.base, "_stage", lambda *a, **k: pytest.fail("Exhausted budget reached asset loading")
    )
    p = deepcopy(epoch_local["parameters"])
    rule = p["policy"]["rules"][0]
    if budget == "runtime":
        rule["resource_modes"]["runtime_seconds"] = "active"
        rule["max_runtime_seconds"] = 0
        rule["next_epoch_seconds"] = 0
    elif budget == "steps":
        rule["max_steps"] = 0
        rule["next_epoch_steps"] = 0
    elif budget == "epochs":
        rule["max_epochs"] = 0
    else:
        rule["resource_modes"]["cost"] = "active"
    with pytest.raises((ValueError, ValidationError)):
        epochs.train(
            inputs=epoch_local["inputs"],
            parameters=p,
            context=checkpoint_options(epoch_local, "zero-" + budget),
        )


def test_history_capability_boundary_is_rejected_during_inert_admission(epoch_local, monkeypatch):
    p = deepcopy(epoch_local["parameters"])
    p["horizon_epochs"] = 10000
    p["max_lineage_records"] = 40000
    p["policy"]["rules"][0].update(max_epochs=10000, max_steps=20000)
    assert epochs.epoch_policy.validate_options(p)[0]["max_epochs"] == 10000
    p["horizon_epochs"] = 10001
    p["max_lineage_records"] = 40004
    p["policy"]["rules"][0].update(max_epochs=10001, max_steps=20002)
    monkeypatch.setattr(
        epochs.base, "_stage", lambda *a, **k: pytest.fail("Unsupported history reached asset loading")
    )
    with pytest.raises(ValueError, match="at most 10000"):
        epochs.train(
            inputs=epoch_local["inputs"],
            parameters=p,
            context=checkpoint_options(epoch_local, "long-history"),
        )


def test_actual_plateau_collects_then_stops_on_completed_epoch_metrics(epoch_local):
    p = deepcopy(epoch_local["parameters"])
    p["learning_rate"] = 1e-20
    p["policy"]["rules"][0].update(criterion="plateau", window=3, threshold=0)
    result = epochs.train(
        inputs=epoch_local["inputs"], parameters=p, context=checkpoint_options(epoch_local, "plateau")
    )
    checkpoints = sorted((epoch_local["tmp"] / "plateau/checkpoints").glob("*/epoch-state.json"))
    decisions = [json.loads(path.read_text()) for path in checkpoints]
    assert [row["completed_epochs"] for row in decisions] == [1, 2, 3]
    assert decisions[0]["decision"]["reasons"] == decisions[1]["decision"]["reasons"] == ["collecting_window"]
    assert decisions[-1]["decision"]["reasons"] == ["criterion_met"]
    assert (
        decisions[-1]["history"][0]["metrics"]["eval_loss"]
        == decisions[-1]["history"][-1]["metrics"]["eval_loss"]
    )
    assert read_checkpoint(result["trained"])["step"] == 6


def test_actual_tiny_causal_lm_epoch_training_and_state(epoch_local):
    from transformers import GPT2Config, GPT2LMHeadModel
    from safetensors.torch import save_model
    import torch

    p = deepcopy(epoch_local["parameters"])
    assets = epoch_local["tmp"] / "assets"
    config = GPT2Config(
        vocab_size=10,
        n_embd=8,
        n_layer=1,
        n_head=2,
        n_positions=16,
        n_ctx=16,
        pad_token_id=1,
        bos_token_id=None,
        eos_token_id=None,
        resid_pdrop=0.2,
        embd_pdrop=0.2,
        attn_pdrop=0.2,
    )
    torch.manual_seed(17)
    model = GPT2LMHeadModel(config)
    config.to_json_file(assets / "config.json", use_diff=False)
    save_model(model, str(assets / "weights.safetensors"))
    p["architecture"].update(task="causal-lm", labels=[])
    for key in ("config", "weights"):
        p["architecture"]["model"][key]["sha256"] = hashlib.sha256(
            Path(epoch_local["context"]["dependencies"][key]).read_bytes()
        ).hexdigest()
    p["policy"]["rules"][0]["max_epochs"] = 2
    p["policy"]["rules"][0]["max_steps"] = 4
    result = epochs.train(
        inputs=epoch_local["inputs"], parameters=p, context=checkpoint_options(epoch_local, "causal")
    )
    state = json.loads((result["trained"] / "epoch-state.json").read_text())
    assert state["completed_epochs"] == 2 and all(row["metrics"]["eval_loss"] > 0 for row in state["history"])
    assert read_checkpoint(result["trained"])["state"] == [
        "model",
        "optimizer",
        "scheduler",
        "rng",
        "data_cursor",
    ]


@pytest.mark.parametrize(
    "conflict", ["history", "runtime", "evidence-epochs", "evidence-steps", "reordered-lineage"]
)
def test_resealed_resume_metadata_must_agree_with_native_cursor_and_sampler(
    epoch_local, conflict, monkeypatch
):
    run_child(epoch_local, "consistent", cancel_after_epoch=2)
    parent = epoch_local["tmp"] / "consistent/checkpoints/000000000004"
    state = json.loads((parent / "epoch-state.json").read_text())
    if conflict == "history":
        state["history"][0]["metrics"]["eval_loss"] += 0.1
    elif conflict == "runtime":
        state["runtime_seconds"] = 1
    elif conflict == "evidence-epochs":
        state["evidence"]["facts"]["curve"]["completed_epochs"] = 1
    elif conflict == "evidence-steps":
        state["evidence"]["facts"]["curve"]["steps"] = 2
    else:
        state["lineage"][0], state["lineage"][1] = state["lineage"][1], state["lineage"][0]
    (parent / "epoch-state.json").write_text(json.dumps(state))
    manifest = json.loads((parent / "checkpoint.manifest.json").read_text())
    manifest["assets"] = epochs.base._assets(parent)
    (parent / "checkpoint.manifest.json").write_text(json.dumps(manifest))
    from transformers import Trainer

    monkeypatch.setattr(
        Trainer, "train", lambda *a, **k: pytest.fail("Resealed conflict reached optimization")
    )
    with pytest.raises(ValueError):
        epochs.train(
            inputs=epoch_local["inputs"],
            parameters=epoch_local["parameters"],
            context={
                **checkpoint_options(epoch_local, "denied-conflict"),
                "resume": {"path": str(parent)},
                "resume_mode": "exact_state",
            },
        )


NOISY_METHOD = """def noisy_flat(*, facts, parameters, subject):
    values=facts['values']
    window=parameters['window']
    consecutive=0
    for end in range(window,len(values)+1):
        spread=max(values[end-window:end])-min(values[end-window:end])
        consecutive=consecutive+1 if spread<=parameters['width'] else 0
    done=consecutive>=parameters['patience'] and facts['completed_epochs']>=parameters['min_epochs']
    return {'outcome':'stop' if done else 'continue',
        'reasons':['selected_noisy_flat_patience_met' if done else 'collect_selected_patience'],
        'observed':{'consecutive_windows':consecutive,'values_seen':len(values)}}
"""


@pytest.fixture
def custom_epoch(epoch_local):
    local = epoch_local
    source = local["source"]
    method = source / "noisy_flat.py"
    (source / "adapter.py").write_text(
        "from modelbuilder.backend.components.transformers_epochs import train\nfrom modelbuilder.backend.components.transformers_training import evaluate, export\n"
    )
    method.write_text(NOISY_METHOD)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "Independent noisy flat epoch method",
        ],
        check=True,
    )
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "window": {"type": "integer", "minimum": 2},
            "width": {"type": "number", "minimum": 0},
            "patience": {"type": "integer", "minimum": 1},
            "min_epochs": {"type": "integer", "minimum": 1},
        },
        "required": ["window", "width", "patience", "min_epochs"],
    }
    declaration = {
        "id": "independent.noisy-flat",
        "version": "1",
        "decision_domain": "continuation",
        "implementation_ref": {
            "module": "independent_flat",
            "entrypoint": "noisy_flat",
            "commit": commit,
            "path": method.name,
            "sha256": hashlib.sha256(method.read_bytes()).hexdigest(),
        },
        "parameter_schema": schema,
        "parameter_schema_digest": identity(schema),
    }
    declaration["method_digest"] = identity(declaration)
    local["context"].update(
        source={
            "commit": commit,
            "files": [
                {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for path in sorted(source.glob("*.py"))
            ],
        },
        source_root=str(source),
    )
    p = local["parameters"]
    p["epoch_bounds"] = epochs.epoch_policy.selected_bounds(p)
    p["policy"]["rules"] = [
        {
            "id": "flat",
            "kind": "method/v1",
            "method": declaration,
            "parameters": {"window": 2, "width": 100, "patience": 2, "min_epochs": 3},
        }
    ]
    p["evidence_bindings"] = {"flat": {"values": {"source": "metric_history", "metric": "eval_loss"}}}
    from modelbuilder.models import Actor

    actor = Actor("owner", {"read", "modify"})
    local["service"].invoke(
        "rules.method.register",
        {
            "project_id": local["project"]["project_id"],
            "method": declaration,
            "idempotency_key": "epoch-method",
        },
        actor,
    )
    local["service"].invoke(
        "rules.policy.register",
        {
            "project_id": local["project"]["project_id"],
            "policy": p["policy"],
            "idempotency_key": "epoch-policy",
        },
        actor,
    )
    return local


def combined_options(local):
    p = deepcopy(local["parameters"])
    p["policy"]["rules"].append(
        {"id": "identity", "kind": "identity/v1", "expected": {"status": "completed"}}
    )
    p["evidence_bindings"]["identity"] = {"actual": {"source": "identities"}}
    return p


def test_actual_source_pinned_noisy_patience_method_and_combined_policy(custom_epoch):
    custom = Path(run_child(custom_epoch, "custom")["path"])
    state = json.loads((custom / "epoch-state.json").read_text())
    assert state["completed_epochs"] == 3 and state["decision"]["reasons"] == [
        "selected_noisy_flat_patience_met"
    ]
    assert state["decision"]["rules"][0]["selected_options"]["parameters"]["width"] == 100
    combined = Path(run_child(custom_epoch, "combined", parameters=combined_options(custom_epoch))["path"])
    state = json.loads((combined / "epoch-state.json").read_text())
    assert [row["id"] for row in state["decision"]["rules"]] == ["flat", "identity"]
    assert [row["outcome"] for row in state["decision"]["rules"]] == ["stop", "passed"]
    assert state["decision"]["outcome"] == "stop" and state["completed_epochs"] == 3


def test_custom_source_pinned_fresh_process_resume_preserves_method_history(custom_epoch):
    full = Path(run_child(custom_epoch, "custom_full")["path"])
    assert run_child(custom_epoch, "custom_partial", cancel_after_epoch=2)["status"] == "cancelled"
    parent = custom_epoch["tmp"] / "custom_partial/checkpoints/000000000004"
    resumed = Path(run_child(custom_epoch, "custom_resumed", resume=parent)["path"])
    from safetensors.torch import load_file

    assert_state_equal(load_file(full / "model.safetensors"), load_file(resumed / "model.safetensors"))
    a = json.loads((full / "epoch-state.json").read_text())
    b = json.loads((resumed / "epoch-state.json").read_text())
    assert a["lineage"] == b["lineage"] and [row["metrics"] for row in a["history"]] == [
        row["metrics"] for row in b["history"]
    ]
    assert a["decision"]["reasons"] == b["decision"]["reasons"] == ["selected_noisy_flat_patience_met"]


def test_combined_convergence_and_custom_conflict_is_not_projected_to_one_rule(custom_epoch):
    p = combined_options(custom_epoch)
    convergence = {**deepcopy(epoch_local_policy()), **p["epoch_bounds"]}
    convergence.update(id="curve", metric="eval_loss", criterion="target", threshold=10, min_epochs=1)
    p["policy"]["rules"].append(convergence)
    from modelbuilder.backend.rule_methods import load_methods

    methods = load_methods(
        p["policy"],
        {
            "source": custom_epoch["context"]["source"],
            "source_root": str(custom_epoch["source"]),
            "trusted_code_roots": [str(custom_epoch["source"])],
        },
    )
    with pytest.raises(ValueError, match="conflicting_decisions"):
        epochs.train(
            inputs=custom_epoch["inputs"],
            parameters=p,
            context={**checkpoint_options(custom_epoch, "conflict"), "epoch_methods": methods},
        )
    state = json.loads(
        (custom_epoch["tmp"] / "conflict/checkpoints/000000000002/epoch-state.json").read_text()
    )
    assert state["decision"]["outcome"] == "blocked"
    assert {row["id"] for row in state["decision"]["rules"]} == {"flat", "identity", "curve"}


def epoch_local_policy():
    return {
        "id": "curve",
        "kind": "convergence/v1",
        "metric": "eval_loss",
        "direction": "minimize",
        "criterion": "target",
        "threshold": 0,
        "window": 2,
        "insufficient_window": "collect_within_bounds",
        "min_epochs": 1,
    }


@pytest.mark.parametrize(
    "case",
    [
        "unavailable-metric",
        "unknown-source",
        "overwritten-safety",
        "missing-bounds",
        "undeclared-binding",
        "source-drift",
    ],
)
def test_custom_epoch_evidence_or_source_conflicts_fail_before_optimization(custom_epoch, case, monkeypatch):
    from transformers import Trainer

    monkeypatch.setattr(
        Trainer, "train", lambda *a, **k: pytest.fail("Invalid evidence reached optimization")
    )
    p = deepcopy(custom_epoch["parameters"])
    context = checkpoint_options(custom_epoch, "unsupported")
    if case == "unavailable-metric":
        p["evidence_bindings"]["flat"]["values"]["metric"] = "eval_perplexity"
    elif case == "unknown-source":
        p["evidence_bindings"]["flat"]["values"] = {"source": "invented_data"}
    elif case == "overwritten-safety":
        p["evidence_bindings"]["flat"]["checkpoint_digest"] = {"source": "identities"}
    elif case == "missing-bounds":
        del p["epoch_bounds"]
    elif case == "undeclared-binding":
        p["evidence_bindings"]["ghost"] = {}
    else:
        (custom_epoch["source"] / "noisy_flat.py").write_text(NOISY_METHOD + "\n# changed\n")
    from modelbuilder.backend.rule_methods import load_methods

    with pytest.raises((ValueError, ValidationError)):
        context["epoch_methods"] = load_methods(
            p["policy"],
            {
                "source": custom_epoch["context"]["source"],
                "source_root": str(custom_epoch["source"]),
                "trusted_code_roots": [str(custom_epoch["source"])],
            },
        )
        epochs.train(inputs=custom_epoch["inputs"], parameters=p, context=context)


@pytest.mark.integration
def test_actual_combined_custom_policy_in_isolated_native_trainer(custom_epoch):
    from test_transformers_training import (
        test_registered_data_native_zenml_train_evaluate_export as native_pipeline,
    )
    from modelbuilder.backend.components import transformers_training

    custom_epoch["parameters"] = combined_options(custom_epoch)
    custom_epoch["parameters"]["horizon_epochs"] = 3
    custom_epoch["parameters"]["epoch_bounds"].update(max_epochs=3, max_steps=6)
    custom_epoch["catalog_entries"] = [
        epochs.catalog_entries()[0],
        *transformers_training.catalog_entries()[1:],
    ]
    custom_epoch["execution"] = {"max_parallel_stages": 2}
    native_pipeline(custom_epoch, False, True)
    state = json.loads(
        (custom_epoch["tmp"] / "job/outputs/train/checkpoints/000000000006/epoch-state.json").read_text()
    )
    assert state["decision"]["outcome"] == "stop" and len(state["decision"]["rules"]) == 2
    assert state["evidence"]["facts"]["flat"]["completed_epochs"] == 3
    assert state["evidence"]["facts"]["flat"]["values"] == [
        row["metrics"]["eval_loss"] for row in state["history"]
    ]


@pytest.mark.parametrize(
    "conflict",
    ["runtime", "custom-cursor", "convergence-cursor", "custom-values", "convergence-observations"],
)
def test_resealed_combined_evidence_uses_one_authoritative_history_and_budget(
    custom_epoch, conflict, monkeypatch
):
    p = combined_options(custom_epoch)
    p["epoch_bounds"]["resource_modes"]["runtime_seconds"] = "active"
    p["epoch_bounds"].update(max_runtime_seconds=10000, next_epoch_seconds=1)
    convergence = {**epoch_local_policy(), **deepcopy(p["epoch_bounds"])}
    convergence.update(id="curve", threshold=10, min_epochs=3)
    p["policy"]["rules"].append(convergence)
    assert (
        run_child(custom_epoch, "combined_interrupted", parameters=p, cancel_after_epoch=2)["status"]
        == "cancelled"
    )
    parent = custom_epoch["tmp"] / "combined_interrupted/checkpoints/000000000004"
    state = json.loads((parent / "epoch-state.json").read_text())
    assert state["runtime_seconds"] > 0
    if conflict == "runtime":
        state["runtime_seconds"] = 0
    elif conflict == "custom-cursor":
        state["evidence"]["facts"]["flat"]["completed_epochs"] = 1
    elif conflict == "convergence-cursor":
        state["evidence"]["facts"]["curve"]["steps"] = 2
    elif conflict == "custom-values":
        state["evidence"]["facts"]["flat"]["values"][0] += 0.1
    else:
        state["evidence"]["facts"]["curve"]["observations"][0]["value"] += 0.1
    (parent / "epoch-state.json").write_text(json.dumps(state))
    manifest = json.loads((parent / "checkpoint.manifest.json").read_text())
    manifest["assets"] = epochs.base._assets(parent)
    (parent / "checkpoint.manifest.json").write_text(json.dumps(manifest))
    from transformers import Trainer
    from modelbuilder.backend.rule_methods import load_methods

    monkeypatch.setattr(
        Trainer, "train", lambda *a, **k: pytest.fail("Combined evidence conflict reached optimization")
    )
    methods = load_methods(
        p["policy"],
        {
            "source": custom_epoch["context"]["source"],
            "source_root": str(custom_epoch["source"]),
            "trusted_code_roots": [str(custom_epoch["source"])],
        },
    )
    with pytest.raises(ValueError, match="history/resource/cursor"):
        epochs.train(
            inputs=custom_epoch["inputs"],
            parameters=p,
            context={
                **checkpoint_options(custom_epoch, "combined_denied"),
                "epoch_methods": methods,
                "resume": {"path": str(parent)},
                "resume_mode": "exact_state",
            },
        )


def test_caller_rule_subject_is_rejected_without_native_ownership(epoch_local, monkeypatch):
    from transformers import Trainer

    monkeypatch.setattr(Trainer, "train", lambda *a, **k: pytest.fail("Forged subject reached optimization"))
    with pytest.raises(ValueError, match="Caller-selected rule_subject"):
        epochs.train(
            inputs=epoch_local["inputs"],
            parameters=epoch_local["parameters"],
            context={
                **checkpoint_options(epoch_local, "forged"),
                "rule_subject": {
                    "project_id": "forged",
                    "run_id": "forged",
                    "config_digest": "0" * 64,
                    "data_digest": "0" * 64,
                },
            },
        )


def test_epoch_overlap_requires_unavailable_cross_run_or_lineage_receipts(custom_epoch, monkeypatch):
    p = combined_options(custom_epoch)
    p["policy"]["rules"].append(
        {"id": "disjoint", "kind": "overlap/v1", "scope": "cross_lineage", "max_overlap": 0}
    )
    p["evidence_bindings"]["disjoint"] = {}
    monkeypatch.setattr(
        epochs.base, "_stage", lambda *a, **k: pytest.fail("Unmeasured overlap reached assets")
    )
    with pytest.raises(ValueError, match="cannot obtain independent cross_run/cross_lineage"):
        epochs.train(
            inputs=custom_epoch["inputs"],
            parameters=p,
            context=checkpoint_options(custom_epoch, "unmeasured-overlap"),
        )


def test_epoch_representation_requires_unavailable_registered_stratum_receipts(custom_epoch, monkeypatch):
    p = deepcopy(custom_epoch["parameters"])
    p["policy"]["rules"].append(
        {
            "id": "representation",
            "kind": "representation/v1",
            "allocation": "proportional",
            "min_per_stratum": 1,
            "required_strata": [],
            "max_share_error": 0.1,
        }
    )
    p["evidence_bindings"]["representation"] = {
        "before": {"source": "identities"},
        "after": {"source": "identities"},
    }
    monkeypatch.setattr(
        epochs.base, "_stage", lambda *a, **k: pytest.fail("Unmeasured representation reached assets")
    )
    with pytest.raises(ValueError, match="cannot obtain registered before/after"):
        epochs.train(
            inputs=custom_epoch["inputs"],
            parameters=p,
            context=checkpoint_options(custom_epoch, "unmeasured"),
        )


def test_actual_combined_custom_and_convergence_scientific_rules_agree(custom_epoch):
    p = combined_options(custom_epoch)
    convergence = {**epoch_local_policy(), **deepcopy(p["epoch_bounds"])}
    convergence.update(id="curve", threshold=10, min_epochs=3)
    p["policy"]["rules"].append(convergence)
    result = Path(run_child(custom_epoch, "scientific_combined", parameters=p)["path"])
    state = json.loads((result / "epoch-state.json").read_text())
    assert state["completed_epochs"] == 3 and state["decision"]["outcome"] == "stop"
    assert [(row["id"], row["outcome"]) for row in state["decision"]["rules"]] == [
        ("flat", "stop"),
        ("identity", "passed"),
        ("curve", "stop"),
    ]
    assert (
        state["evidence"]["facts"]["curve"]["observations"][-1]["value"]
        == state["evidence"]["facts"]["flat"]["values"][-1]
    )


@pytest.fixture
def rng_consuming_epoch(custom_epoch):
    local = custom_epoch
    source = local["source"]
    path = source / "noisy_flat.py"
    effect = """import random
import numpy as np
import torch

def disturb_rng():
    random.seed(790);random.random()
    np.random.seed(791);np.random.random(5)
    torch.manual_seed(792);torch.rand(7)

disturb_rng()
"""
    body = NOISY_METHOD.replace("    values=facts['values']", "    disturb_rng()\n    values=facts['values']")
    path.write_text(effect + "\n" + body)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "Deterministic method with deliberate global RNG effects",
        ],
        check=True,
    )
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    declaration = deepcopy(local["parameters"]["policy"]["rules"][0]["method"])
    declaration["version"] = "2"
    declaration["implementation_ref"].update(
        commit=commit, sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )
    declaration["method_digest"] = identity(
        {key: value for key, value in declaration.items() if key != "method_digest"}
    )
    local["parameters"]["policy"]["version"] = "2"
    local["parameters"]["policy"]["rules"][0]["method"] = declaration
    local["context"]["source"] = {
        "commit": commit,
        "files": [
            {"path": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(source.glob("*.py"))
        ],
    }
    from modelbuilder.models import Actor

    actor = Actor("owner", {"read", "modify"})
    local["service"].invoke(
        "rules.method.register",
        {
            "project_id": local["project"]["project_id"],
            "method": declaration,
            "idempotency_key": "rng-method",
        },
        actor,
    )
    local["service"].invoke(
        "rules.policy.register",
        {
            "project_id": local["project"]["project_id"],
            "policy": local["parameters"]["policy"],
            "idempotency_key": "rng-policy",
        },
        actor,
    )
    return local


def test_source_method_import_and_evaluation_restore_loaded_training_rng(rng_consuming_epoch):
    import random
    import numpy as np
    import torch
    from modelbuilder.backend.rule_methods import load_methods, preserve_training_rng

    before = (random.getstate(), np.random.get_state(), torch.get_rng_state().clone())
    methods = load_methods(
        rng_consuming_epoch["parameters"]["policy"],
        {
            "source": rng_consuming_epoch["context"]["source"],
            "source_root": str(rng_consuming_epoch["source"]),
            "trusted_code_roots": [str(rng_consuming_epoch["source"])],
        },
    )
    assert_state_equal(before, (random.getstate(), np.random.get_state(), torch.get_rng_state()))
    with preserve_training_rng():
        result = methods[0].evaluate(
            facts={"values": [0.5, 0.4], "completed_epochs": 2},
            parameters={"window": 2, "width": 100, "patience": 2, "min_epochs": 3},
            subject={},
        )
    assert result["outcome"] == "continue"
    assert_state_equal(before, (random.getstate(), np.random.get_state(), torch.get_rng_state()))
    with pytest.raises(RuntimeError, match="deliberate"), preserve_training_rng():
        random.seed(9)
        np.random.seed(10)
        torch.manual_seed(11)
        raise RuntimeError("deliberate")
    assert_state_equal(before, (random.getstate(), np.random.get_state(), torch.get_rng_state()))


def test_rng_consuming_deterministic_method_preserves_fresh_process_dropout_resume_parity(
    rng_consuming_epoch,
):
    import torch
    from safetensors.torch import load_file

    local = rng_consuming_epoch
    full = Path(run_child(local, "rng_full")["path"])
    assert run_child(local, "rng_partial", cancel_after_epoch=2)["status"] == "cancelled"
    parent = local["tmp"] / "rng_partial/checkpoints/000000000004"
    resumed = Path(run_child(local, "rng_resumed", resume=parent)["path"])
    assert_state_equal(load_file(full / "model.safetensors"), load_file(resumed / "model.safetensors"))
    for name in ("optimizer.pt", "scheduler.pt"):
        assert_state_equal(
            torch.load(full / name, weights_only=True), torch.load(resumed / name, weights_only=True)
        )
    assert_state_equal(
        torch.load(full / "rng_state.pth", weights_only=False),
        torch.load(resumed / "rng_state.pth", weights_only=False),
    )
    a = json.loads((full / "epoch-state.json").read_text())
    b = json.loads((resumed / "epoch-state.json").read_text())
    assert a["lineage"] == b["lineage"] and [row["metrics"] for row in a["history"]] == [
        row["metrics"] for row in b["history"]
    ]
    assert a["decision"]["reasons"] == b["decision"]["reasons"] == ["selected_noisy_flat_patience_met"]
