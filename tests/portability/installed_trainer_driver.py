"""Standalone installed CPU/eager Trainer v2 service acceptance, never a runner.

Invoked only by reproduce_installed_acceptance.py's explicit additional-driver
hook. The service interpreter never imports Torch. This same immutable script
has two backend-only helper modes for authored assets and native state checks.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import traceback
from uuid import uuid4

DRIVER_ID = "trainer-static-service-v1"
CONTEXT_SCHEMA = "modelbuilder.installed-acceptance-context/v1"
RESULT_SCHEMA = "modelbuilder.installed-acceptance-driver-result/v1"
TERMS = """Synthetic fixture terms:
These exact tiny randomly initialized model bytes, seven invented weather rows,
and synchronization adapter are authored solely for this offline acceptance.
The fixture author permits copying, local training, evaluation and export of
these exact fixture bytes for this isolated regression. No pretrained weights,
upstream dataset, user content or broader third-party rights are approved.
"""
USAGE = {"purpose": "Self-authored installed Trainer acceptance", "commercial": False,
         "deployment": "isolated offline fixture", "conditions": ["No upstream data or pretrained weights"]}
# This fixture wraps only the existing adapter callback. sys.setprofile observes
# entry without assigning/patching Trainer.train, AdamW.step, or any runtime API.
WRAPPER = '''from pathlib import Path
import sys
import time
from modelbuilder.backend.components.transformers_epochs import train as epoch_train
from modelbuilder.backend.components.transformers_training import evaluate, export

def train(*, inputs, parameters, context):
    from transformers import Trainer
    import torch
    job = Path(context['job_dir'])
    pause = (job.parents[2] / 'pause-control').read_text() == 'pause'
    original = context.get('checkpoint_ready')
    def ready(**event):
        if original:
            original(**event)
        if event['step'] == 4 and not context.get('resume') and pause:
            (job / 'boundary-waiting').write_text('completed epoch 2')
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and not Path(context['cancel_file']).exists():
                time.sleep(.05)
            if not Path(context['cancel_file']).exists():
                raise RuntimeError('Test boundary synchronization expired')
    train_code, step_code = Trainer.train.__code__, torch.optim.AdamW.step.__code__
    previous = sys.getprofile()
    if previous is not None:
        raise RuntimeError('Acceptance requires no preexisting Python profiler')
    def observed(frame, event, arg):
        if event == 'call' and frame.f_code in (train_code, step_code):
            instance = frame.f_locals.get('self')
            # Pinned optimizer decorators accept *args; 'self' is not a local
            # until after the call event. Observe its original first argument.
            if instance is None:
                positional = frame.f_locals.get('args', ())
                instance = positional[0] if positional else None
            if frame.f_code is train_code and isinstance(instance, Trainer):
                with (job / 'trainer-entered').open('a') as stream:
                    stream.write('actual Trainer.train entered\\n')
            if frame.f_code is step_code and isinstance(instance, torch.optim.AdamW):
                with (job / 'optimizer-entered').open('a') as stream:
                    stream.write('actual AdamW.step entered\\n')
        return observed
    sys.setprofile(observed)
    try:
        return epoch_train(inputs=inputs, parameters=parameters,
                           context={**context, 'checkpoint_ready': ready})
    finally:
        sys.setprofile(previous)
'''


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def dump(path, value):
    path = Path(path)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def inventory(root):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        require(not path.is_symlink(), "Fixture inventory contains a symlink")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = {"sha256": sha(path), "size": path.stat().st_size}
    return result


def installed_identity(expected_python):
    import modelbuilder
    require(sys.flags.isolated == 1 and sys.dont_write_bytecode, "Installed calls require -I -B")
    require(os.path.abspath(sys.executable) == os.path.abspath(expected_python), "Wrong supplied interpreter")
    prefix = Path(sys.prefix).resolve()
    require(prefix != Path(sys.base_prefix).resolve(), "An independent installed environment is required")
    require(prefix == Path(expected_python).parent.parent.resolve(), "Interpreter does not own the selected environment")
    require(Path(modelbuilder.__file__).resolve().is_relative_to(prefix), "Modelbuilder is outside this environment")
    require(not os.environ.get("PYTHONPATH"), "PYTHONPATH is forbidden")
    require(all(path and Path(path).is_absolute() for path in sys.path), "Relative import path")
    return {"executable": sys.executable, "prefix": sys.prefix, "module": modelbuilder.__file__, "sys_path": sys.path}



def check_helper_identity(identity, service_identity, backend_python):
    require(Path(identity["prefix"]).resolve() == Path(backend_python).parent.parent.resolve(), "Wrong backend prefix")
    check_distinct_environments(service_identity, identity)
    require(os.path.abspath(identity["executable"]) == os.path.abspath(backend_python), "Wrong backend interpreter")
    require(Path(identity["module"]).resolve().is_relative_to(Path(identity["prefix"]).resolve()), "Backend module outside environment")


def validate_context(context, context_path, output, *, script=None):
    require(context.get("schema") == CONTEXT_SCHEMA and context.get("driver_id") == DRIVER_ID, "Wrong driver context")
    require(context.get("role") in {"origin", "replay"}, "Unknown installed role")
    require(isinstance(context.get("runtime_commit"), str) and len(context["runtime_commit"]) == 40, "Missing runtime commit")
    require(type(context.get("timeout_seconds")) is int and context["timeout_seconds"] >= 360, "Insufficient driver budget")
    work = Path(context["driver_work"])
    require(work.is_absolute() and work.is_dir() and not work.is_symlink(), "Fresh absolute driver work required")
    require(not any(work.iterdir()), "Driver work is not empty; never reuse earlier acceptance")
    require(Path(output).absolute() == work / "result.json" and not Path(output).exists(), "Wrong/new output path")
    require(not work.resolve().is_relative_to(Path(context["source_root"]).resolve()), "Work overlaps immutable source")
    require(Path(context_path).is_file(), "Missing original context")
    require(sha(script or __file__) == context["driver_sha256"], "Driver bytes differ from selected hash")
    for key in ("service_python", "backend_python"):
        require(Path(context[key]).is_absolute() and Path(context[key]).is_file(), "Missing supplied interpreter")
    require(context["service_python"] != context["backend_python"], "Service/backend roles are not independent")
    return work


def asset_helper(request):
    """Backend only: author all assets locally, no model/download lookup."""
    identity = installed_identity(request["backend_python"])
    import torch
    from transformers import BertConfig, BertForSequenceClassification
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from safetensors.torch import save_model
    torch.set_num_threads(1)
    directory = Path(request["assets"])
    require(directory.is_dir() and not any(directory.iterdir()), "Assets must be new")
    vocab = ["[UNK]", "[PAD]", "sun", "rain", "warm", "cold", "sky", "cloud", "dark", "light"]
    tokenizer = Tokenizer(WordLevel({word: i for i, word in enumerate(vocab)}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.save(str(directory / "tokenizer.json"))
    torch.manual_seed(10)
    config = BertConfig(vocab_size=10, hidden_size=8, num_hidden_layers=1, num_attention_heads=2,
                        intermediate_size=12, max_position_embeddings=16, num_labels=2, pad_token_id=1,
                        hidden_dropout_prob=.25, attention_probs_dropout_prob=.2)
    model = BertForSequenceClassification(config)
    config.to_json_file(directory / "config.json", use_diff=False)
    save_model(model, str(directory / "weights.safetensors"))
    return {"status": "PASS", "identity": identity, "dropout": [.25, .2], "files": inventory(directory)}


def state_equal(a, b):
    import torch
    import numpy as np
    if isinstance(a, torch.Tensor):
        require(isinstance(b, torch.Tensor) and a.dtype == b.dtype and a.shape == b.shape and torch.equal(a, b), "Native tensor differs")
    elif isinstance(a, np.ndarray):
        require(isinstance(b, np.ndarray) and a.dtype == b.dtype and np.array_equal(a, b), "Native NumPy state differs")
    elif isinstance(a, dict):
        require(isinstance(b, dict) and a.keys() == b.keys(), "Native state keys differ")
        for key in a:
            state_equal(a[key], b[key])
    elif isinstance(a, (list, tuple)):
        require(type(a) is type(b) and len(a) == len(b), "Native sequence differs")
        for x, y in zip(a, b):
            state_equal(x, y)
    else:
        require(type(a) is type(b) and a == b, "Native scalar differs")


def check_distinct_environments(service, backend):
    require(Path(service["prefix"]).resolve() != Path(backend["prefix"]).resolve(), "Service/backend environments alias")


def check_model_changed(initial, trained):
    import torch
    require(initial.keys() == trained.keys(), "Model parameter keys differ from initialization")
    require(any(not torch.equal(value, trained[key]) for key, value in initial.items()), "Training left every authored weight unchanged")


def check_checkpoint_scope(descriptor, step, completed):
    require(descriptor["step"] == step and descriptor["execution"]["completed_epoch"] == completed, "Checkpoint epoch/step differs")
    require(descriptor["execution"]["device"] == "cpu" and descriptor["execution"]["dtype"] == "float32", "Unsupported checkpoint device/dtype")
    require(descriptor["state"] == ["model", "optimizer", "scheduler", "rng", "data_cursor"], "Incomplete native checkpoint state")


def check_epoch_json(a, b, parent):
    require(a["lineage"] == b["lineage"] and len(a["lineage"]) == 16, "Consumed lineage differs")
    orders = [[item["sample_id"] for item in a["lineage"][i:i + 4]] for i in range(0, 16, 4)]
    require(all(sorted(order) == ["0", "1", "2", "3"] for order in orders), "Epoch membership differs")
    require(len({tuple(order) for order in orders}) > 1, "Epoch sampler did not change order")
    require(a["contract"] == b["contract"] and b["contract"]["scheduler_horizon_steps"] == 8, "Full scheduler horizon differs")
    require(a["completed_epochs"] == b["completed_epochs"] == 4, "Final epoch differs")
    require([row["epoch"] for row in a["history"]] == [1, 2, 3, 4], "Full history epochs differ")
    require([row["epoch"] for row in b["history"]] == [1, 2, 3, 4], "Resumed history epochs differ")
    require([row["metrics"] for row in a["history"]] == [row["metrics"] for row in b["history"]], "Held-out history differs")
    require(a["decision"]["outcome"] == b["decision"]["outcome"] == "stop", "Bounded final decision differs")
    require(parent["completed_epochs"] == 2 and parent["lineage"] == b["lineage"][:8], "Parent cursor differs")
    require(parent["history"] == b["history"][:2], "Resumption rewrote original epoch evidence")
    require(parent["contract"] == b["contract"], "Resumption changed original contract")
    return {"lineage_and_epoch_membership_equal": True, "epoch_sampler_changes_order": True,
            "full_horizon_and_complete_history_equal": True, "parent_history_prefix_immutable": True,
            "heldout_epoch_metrics_equal": True, "bounded_final_stop": True}


def state_helper(request):
    """Backend only; trusted fixture checkpoints are hash-bound before loading."""
    identity = installed_identity(request["backend_python"])
    import torch
    from safetensors.torch import load_file
    paths = {name: Path(request[name]) for name in ("full", "resumed", "parent")}
    owner = Path(request["driver_work"]).resolve()
    for name, path in paths.items():
        require(path.resolve().is_relative_to(owner) and not path.is_symlink(), "Checkpoint escaped fixture")
        require(inventory(path) == request["inventories"][name], "Checkpoint changed before native comparison")
    a, b = paths["full"], paths["resumed"]
    initial = Path(request["initial_weights"])
    require(initial.resolve().is_relative_to(owner) and not initial.is_symlink() and sha(initial) == request["initial_sha256"], "Initial weights changed or escaped fixture")
    model_a, model_b = load_file(a / "model.safetensors"), load_file(b / "model.safetensors")
    state_equal(model_a, model_b)
    check_model_changed(load_file(initial), model_a)
    for name in ("optimizer.pt", "scheduler.pt"):
        state_equal(torch.load(a / name, weights_only=True), torch.load(b / name, weights_only=True))
    # Native RNG includes NumPy/Python objects: only these just-generated,
    # confined, inventoried fixture checkpoints are deserialized in this helper.
    state_equal(torch.load(a / "rng_state.pth", weights_only=False), torch.load(b / "rng_state.pth", weights_only=False))
    checks = check_epoch_json(read(a / "epoch-state.json"), read(b / "epoch-state.json"), read(paths["parent"] / "epoch-state.json"))
    sa, sb = read(a / "trainer_state.json"), read(b / "trainer_state.json")
    for key, expected in (("global_step", 8), ("epoch", 4.0), ("max_steps", 8), ("num_train_epochs", 4)):
        require(sa[key] == sb[key] == expected, "Native Trainer cursor/horizon differs: " + key)
    # Per-process wall-clock throughput and final train aggregate are not exact
    # continuation state. Compare every saved scientific evaluation entry.
    def evaluations(state):
        return [{k: v for k, v in row.items() if k in {"eval_loss", "eval_accuracy", "epoch", "step"}}
                for row in state["log_history"] if "eval_loss" in row]
    require(evaluations(sa) == evaluations(sb) and len(evaluations(sa)) == 4, "Native evaluation history differs")
    checks.update(model_tensors_equal=True, actual_model_optimization_observed=True, optimizer_equal=True, scheduler_equal=True,
                  python_numpy_torch_rng_equal=True, native_trainer_cursor_and_history_equal=True)
    return {"status": "PASS", "identity": identity, "assertions": checks, "inventories": request["inventories"]}


def parameters(resources):
    lookup = {row["metadata"]["path"]: row for row in resources}
    refs = {key: {"dependency_id": lookup[name]["id"], "sha256": lookup[name]["sha256"]}
            for key, name in (("config", "config.json"), ("weights", "weights.safetensors"), ("tokenizer", "tokenizer.json"))}
    return {"architecture": {"task": "sequence-classification", "model": refs, "text_field": "text", "label_field": "label",
            "labels": ["clear", "wet"], "max_length": 8, "truncate": False, "pad_token_id": 1,
            "limits": {"max_parameters": 100000, "max_asset_bytes": 1048576, "max_rows": 100, "max_tokenized_tokens": 1000}},
        "seed": 123, "batch_size": 2, "gradient_accumulation_steps": 1, "learning_rate": .005,
        "weight_decay": .01, "warmup_steps": 1, "lr_scheduler_type": "linear", "max_grad_norm": 1.,
        "horizon_epochs": 4, "max_lineage_records": 16,
        "policy": {"schema_version": "modelbuilder.rules/v1", "id": "epoch-options", "version": "1", "rules": [{
            "id": "curve", "kind": "convergence/v1", "metric": "eval_loss", "criterion": "target", "direction": "minimize",
            "threshold": 0, "max_epochs": 4, "window": 2, "insufficient_window": "collect_within_bounds", "min_epochs": 1,
            "max_steps": 8, "next_epoch_steps": 2, "step_unit": "optimizer_steps", "max_runtime_seconds": 0,
            "next_epoch_seconds": 0, "max_cost": 0, "next_epoch_cost": 0, "cost_unit": "local-free",
            "resource_modes": {"steps": "active", "runtime_seconds": "not_applicable", "cost": "not_applicable"}}]}}


def write_fixture_files(work):
    source, data, assets = (work / name for name in ("source", "data", "assets"))
    for directory in (source, data, assets):
        directory.mkdir()
    (source / "adapter.py").write_text(WRAPPER)
    (source / "TERMS.txt").write_text(TERMS)
    (data / "TERMS.txt").write_text(TERMS)
    rows = [{"id": str(i), "text": text, "label": i % 2} for i, text in enumerate(
        ["sun warm", "rain cold", "sky light", "cloud dark", "sun sky", "rain cloud", "cold dark"])]
    (data / "rows.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    manifest = {"name": "Owned weather", "benchmark": "independent-weather-v1", "license": "unknown",
        "provenance": {"source": "independently authored synthetic acceptance"},
        "sources": [{"id": "data", "root_id": "data", "path": "rows.jsonl", "format": "jsonl", "sha256": sha(data / "rows.jsonl")}],
        "id_column": "id", "row_schema": {"type": "object", "required": ["id", "text", "label"], "additionalProperties": False,
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "label": {"type": "integer"}}},
        "splits": [{"id": name, "role": role, "sample_ids": ids} for name, role, ids in
            [("train", "train", ["0", "1", "2", "3"]), ("dev", "validation", ["4", "5"]), ("test", "test", ["6"])]]}
    dump(work / "dataset-manifest.json", manifest)
    return source, data, assets, manifest


def stopped(row):
    return row.get("status") in {"succeeded", "failed", "cancelled"} and all(
        row.get(name) is False for name in ("worker_alive", "supervisor_alive", "process_group_alive"))


class Driver:
    def __init__(self, context, context_path, output):
        self.context, self.context_path, self.output = context, Path(context_path), Path(output)
        self.work = validate_context(context, context_path, output)
        self.service_identity = installed_identity(context["service_python"])
        self.deadline = time.monotonic() + context["timeout_seconds"] - 120
        self.service = self.backend = self.actor = self.project = None
        self.transcript = self.work / "operations.jsonl"
        self.env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
        self.env.update(HOME=str(self.work / "home"), PYTHONDONTWRITEBYTECODE="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                        OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", TOKENIZERS_PARALLELISM="false",
                        ZENML_ANALYTICS_OPT_IN="false", WANDB_MODE="disabled")
        Path(self.env["HOME"]).mkdir()

    def event(self, row):
        with self.transcript.open("a") as stream:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def call(self, name, payload=None, *, actor=None):
        from modelbuilder.models import OPERATIONS
        body = {**({"project_id": self.project["project_id"]} if self.project else {}), **(payload or {})}
        if OPERATIONS[name].mutating:
            body.setdefault("idempotency_key", str(uuid4()))
        self.event({"operation": name, "phase": "dispatch", "payload": body, "actor": (actor or self.actor).id})
        try:
            result = self.service.invoke(name, body, actor or self.actor)
        except BaseException as error:
            self.event({"operation": name, "phase": "error", "error": str(error), "code": getattr(error, "code", None)})
            raise
        self.event({"operation": name, "phase": "result", "result": result})
        return result

    def command(self, name, args, *, timeout=90):
        budget = min(timeout, self.deadline - time.monotonic())
        require(budget > 0, "Driver execution deadline exceeded")
        with (self.work / (name + ".log")).open("x") as log:
            result = subprocess.run([str(value) for value in args], cwd=self.work, env=self.env,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=budget)
        require(result.returncode == 0, "Helper failed; inspect " + name + ".log")

    def helper(self, name, payload):
        request, output = self.work / (name + "-request.json"), self.work / (name + "-result.json")
        dump(request, {**payload, "backend_python": self.context["backend_python"]})
        self.command(name, [self.context["backend_python"], "-I", "-B", Path(__file__).absolute(),
                            "--backend-mode", name, "--request", request, "--output", output])
        result = read(output)
        require(result["status"] == "PASS", "Backend helper did not pass")
        check_helper_identity(result["identity"], self.service_identity, self.context["backend_python"])
        return result

    def review(self, alias, kind, files, **extra):
        evidence = self.call("hub.license.local.inspect", {"source_alias": alias, "repo_type": kind,
            "files": sorted({*files, "TERMS.txt"}), "evidence_document_paths": ["TERMS.txt"],
            "max_bytes": 8 * 1024 * 1024, "declared_license": "unknown", **extra})
        require(any(row["path"] == "TERMS.txt" and row["text"] == TERMS for row in evidence["evidence"]["documents"]), "Terms differ")
        policy = self.call("hub.license.review", {"evidence_id": evidence["evidence_id"], "evidence_digest": evidence["evidence_digest"],
            "license_document_paths": ["TERMS.txt"], "permitted_uses": ["download", "train", "evaluate", "export"],
            "intended_context": "Exact self-authored fixture bytes only", "required_files": [], "required_attributions": [],
            "allowed_destination_licenses": [], "gated_terms_acknowledged": False,
            "reviewed_usage_context": USAGE, "confirm_review": True})
        return evidence, policy

    def prepare(self):
        from modelbuilder.backend import ZenMLBackend
        from modelbuilder.models import Actor
        from modelbuilder.service import OperationService
        from modelbuilder.backend.components import transformers_epochs, transformers_training
        self.source, data, assets, manifest = write_fixture_files(self.work)
        self.helper("assets", {"assets": str(assets)})
        (assets / "TERMS.txt").write_text(TERMS)
        for name, args in (("init", ["init", "-q"]), ("add", ["add", "."]), ("commit", ["-c", "user.name=Synthetic Fixture",
                           "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Immutable installed Trainer synchronization fixture"])):
            self.command("source-" + name, ["git", "-C", self.source, *args])
        commit = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], env=self.env, text=True).strip()
        source_identity = {"repository": "urn:sha256:" + sha(self.source / "adapter.py"), "commit": commit,
                           "files": [{"path": name, "sha256": sha(self.source / name)} for name in ("adapter.py", "TERMS.txt")]}
        self.source_before = inventory(self.source)
        self.assets_before, self.data_before = inventory(assets), inventory(data)
        self.data_dir = self.work / "state"
        self.data_dir.mkdir()
        (self.data_dir / "pause-control").write_text("continue")
        self.backend = ZenMLBackend(self.data_dir, python_executable=self.context["backend_python"], source_root=self.source,
                                    trusted_code_roots=[str(self.source)])
        self.actor = Actor("synthetic-fixture-owner", frozenset({"read", "modify", "execute", "publish"}))
        config = {"db_path": self.data_dir / "application.sqlite3", "backend": self.backend, "source_root": self.source,
                  "source_roots": {"owned": {"path": str(self.source), "trusted": True}}, "hub_providers": {}}
        self.service = OperationService(**config)
        sources = self.call("project.source.list")["sources"]
        repository = next(row for row in sources if row["alias"] == "owned")
        self.project = self.call("project.create", {"slug": "trainer-installed", "model_name": "Owned dropout Trainer",
            "source_alias": "owned", "source_repository_id": repository["repository_id"], "trusted": True})
        self.service.hub.close()
        self.service.store.close()
        pid = self.project["project_id"]
        self.service = OperationService(**config, dataset_roots={pid: {"data": str(data)}},
            hub_config={"publication_roots": {pid: {"source": str(self.source), "data": str(data), "assets": str(assets)}}})
        self.recipe = self.call("recipe.get", {"recipe_id": self.project["recipe_id"]})
        evidence, model_policy = self.review("assets", "model", ["config.json", "weights.safetensors", "tokenizer.json"])
        asset = self.call("hub.local.register", {"evidence_id": evidence["evidence_id"], "license_policy_id": model_policy["policy_id"]})
        self.recipe = self.call("hub.asset.bind", {"asset_id": asset["asset_id"], "expected_revision": self.recipe["revision"], "usage_context": USAGE})
        registered = self.call("dataset.register", {"manifest": manifest})
        _, data_policy = self.review("data", "dataset", ["rows.jsonl"])
        reviewed = self.call("hub.dataset.policy.bind", {"dataset_id": registered["dataset_id"], "dataset_revision": registered["revision"],
            "source_policies": {"data": data_policy["policy_id"]}, "intended_use": "train", "usage_context": USAGE})
        self.parameters = parameters(asset["resources"])
        entries = [transformers_epochs.catalog_entries()[0], *transformers_training.catalog_entries()[1:]]
        values = [self.parameters, {"batch_size": 2, "metrics": ["accuracy", "cross_entropy"]},
                  {"parity_texts": ["sun sky"], "atol": 1e-7, "verification_timeout_seconds": 60}]
        nodes = []
        for name, entry, params in zip(("train", "evaluate", "export"), entries, values):
            nodes.append({"id": name, "kind": "component.run", "component": {key: entry[key] for key in
                ("id", "version", "protocol", "role", "description", "parameter_schema", "capabilities")},
                "inputs": entry["inputs"], "outputs": entry["outputs"], "parameters": deepcopy(params),
                "cache_policy": "disabled", "timeout": 120, "retry": {"max_attempts": 1, "backoff_seconds": 0},
                "resource_request": {"gpu": 0}, "environment_ref": "default", "provenance": {},
                "implementation_ref": {**source_identity["files"][0], "module": "installed_service_epochs", "entrypoint": name, "commit": commit}})
        self.recipe.update(source=source_identity, nodes=nodes, edges=[
            {"source_node": "train", "source_port": "trained", "target_node": name, "target_port": "trained"} for name in ("evaluate", "export")],
            environment={"python": "3.12", "requirements": ["zenml==0.97.0"]},
            evaluation={"live": {"enabled": True, "max_concurrency": 1, "timeout_seconds": 60}})
        self.recipe = self.call("recipe.update", {"recipe_id": self.recipe["recipe_id"], "recipe": self.recipe, "expected_revision": self.recipe["revision"]})
        _, source_policy = self.review("source", "source", ["adapter.py"], source_repository_id=repository["repository_id"], source_commit=commit)
        self.recipe = self.call("hub.source.policy.bind", {"recipe_id": self.recipe["recipe_id"], "expected_revision": self.recipe["revision"],
            "expected_project_revision": self.project["revision"], "license_policy_id": source_policy["policy_id"], "intended_use": "train", "usage_context": USAGE})["recipe"]
        for split, purpose, node, target, port in (("train", "training", "training_data", "train", "train"),
                ("dev", "evaluation", "validation_data", "train", "validation"), ("dev", "evaluation", "validation_data", "evaluate", "data")):
            self.recipe = self.call("dataset.bind", {"recipe_id": self.recipe["recipe_id"], "expected_revision": self.recipe["revision"],
                "dataset_id": reviewed["dataset_id"], "dataset_revision": reviewed["dataset_revision"], "split_id": split,
                "purpose": purpose, "node_id": node, "target_node": target, "target_port": port})["recipe"]
        require(len(self.recipe["nodes"]) == 5 and len(self.recipe["edges"]) == 5, "Expected static five-node DAG")
        self.policies = {row["policy_id"] for row in (model_policy, data_policy, source_policy)}
        require(len(self.policies) == 3, "Independent public reviews required")
        dump(self.work / "fixture-provenance.json", {"source": source_identity, "source_inventory": self.source_before,
             "assets": self.assets_before, "data": self.data_before, "policies": sorted(self.policies),
             "parameters": self.parameters, "runtime_commit": self.context["runtime_commit"], "service_identity": self.service_identity,
             "wrapper_scope": "Immutable test callback synchronization and read-only Python profile entry observations; no runtime method reassignment"})
        dump(self.work / "recipe-original.json", self.recipe)

    def wait(self, operation_id, expected="succeeded"):
        until = min(self.deadline, time.monotonic() + 180)
        while time.monotonic() < until:
            row = self.call("run.status", {"operation_id": operation_id})
            if stopped(row):
                require(row["status"] == expected, "Unexpected native terminal status: " + str(row))
                return row
            time.sleep(.2)
        raise AssertionError("Native service operation exceeded bounded deadline")

    def checkpoint(self, row):
        result = self.call("model.checkpoint.get", {"operation_id": row["operation_id"], "checkpoint_kind": "last"})
        require(Path(result["path"]).resolve().is_relative_to(self.work.resolve()), "Checkpoint escaped owned work")
        return result

    def deny(self, payload, code, actor=None):
        from modelbuilder.models import ServiceError
        before = sorted(path.name for path in (self.backend.root / "jobs").iterdir())
        try:
            self.call("run.resume", payload, actor=actor)
        except ServiceError as error:
            require(error.code == code, "Unexpected permission gate: " + error.code)
        else:
            raise AssertionError("Unsafe resume unexpectedly admitted: " + code)
        require(sorted(path.name for path in (self.backend.root / "jobs").iterdir()) == before, "Denied request created native job")

    def execute(self):
        from modelbuilder.backend.components import read_checkpoint, read_export
        from modelbuilder.models import Actor
        scope = {"recipe_id": self.recipe["recipe_id"]}
        def submit(key):
            return self.call("run.submit", {**scope, "expected_revision": self.recipe["revision"], "trusted": True, "idempotency_key": key})
        full = self.wait(submit("uninterrupted")["operation_id"])
        full_ref = self.checkpoint(full)
        (self.data_dir / "pause-control").write_text("pause")
        interrupted = submit("interrupt")
        job = self.backend.root / "jobs" / interrupted["operation_id"]
        until = min(self.deadline, time.monotonic() + 120)
        while time.monotonic() < until and not (job / "boundary-waiting").is_file():
            time.sleep(.05)
        require((job / "boundary-waiting").is_file(), "Missing completed epoch synchronization; see native logs")
        self.call("run.cancel", {"operation_id": interrupted["operation_id"], "idempotency_key": "cancel-boundary"})
        parent = self.wait(interrupted["operation_id"], "cancelled")
        (self.data_dir / "pause-control").write_text("continue")
        recovered = self.checkpoint(parent)
        recovered_descriptor = read_checkpoint(Path(recovered["path"]))
        check_checkpoint_scope(recovered_descriptor, 4, 2)
        require("exact_state" in recovered["resume_modes"], "Checkpoint lacks exact continuation")
        resume = {"operation_id": parent["operation_id"], "checkpoint_kind": "last", "checkpoint_ref": recovered["artifact_id"],
                  "mode": "exact_state", "trusted": True, "expected_recipe_revision": self.recipe["revision"], "idempotency_key": "exact-resume"}
        parent_before = (job / "request.json").read_bytes()
        (self.work / "parent-request-before.json").write_bytes(parent_before)
        self.deny({**resume, "idempotency_key": "read-only-resume"}, "forbidden", Actor(self.actor.id, {"read"}))
        self.deny({**resume, "trusted": False, "idempotency_key": "untrusted-resume"}, "untrusted_execution_blocked")
        edited = deepcopy(self.recipe)
        next(node for node in edited["nodes"] if node["id"] == "train")["parameters"]["learning_rate"] = .009
        current = self.call("recipe.update", {**scope, "recipe": edited, "expected_revision": self.recipe["revision"]})
        resume["expected_recipe_revision"] = current["revision"]
        resumed = self.wait(self.call("run.resume", resume)["operation_id"])
        final = self.checkpoint(resumed)
        a, b, p = Path(full_ref["path"]), Path(final["path"]), Path(recovered["path"])
        check_checkpoint_scope(read_checkpoint(a), 8, 4)
        check_checkpoint_scope(read_checkpoint(b), 8, 4)
        checked = self.helper("state", {"full": str(a), "resumed": str(b), "parent": str(p), "driver_work": str(self.work),
                             "initial_weights": str(self.work / "assets/weights.safetensors"),
                             "initial_sha256": self.assets_before["weights.safetensors"]["sha256"],
                             "inventories": {name: inventory(path) for name, path in (("full", a), ("resumed", b), ("parent", p))}})
        require(resumed["resume_mode"] == "exact_state" and resumed["parent_run_id"] == parent["backend_run_id"], "Wrong continuation lineage")
        require(full["metrics"]["cross_entropy"] == resumed["metrics"]["cross_entropy"], "Final held-out metric differs")
        require((job / "request.json").read_bytes() == parent_before, "Parent request mutated")
        executed = read(self.backend.root / "jobs" / resumed["operation_id"] / "request.json")
        actual = next(node["parameters"] for node in executed["recipe"]["nodes"] if node["id"] == "train")
        require(actual == self.parameters and actual["learning_rate"] == .005 and actual["horizon_epochs"] == 4, "Current edits replaced parent contract")
        saved = read(b / "epoch-state.json")
        require(saved["evidence"]["subject"]["run_id"] == parent["backend_run_id"], "Origin evidence subject differs")
        require(read_checkpoint(b)["execution"]["current_subject"]["run_id"] == resumed["backend_run_id"], "Current subject is not verified child")
        from modelbuilder.evaluation import validate_evaluation_record
        for metric in ("accuracy", "cross_entropy"):
            left, right = (row["metrics"]["evaluation_records"][metric] for row in (full, resumed))
            for record, selected in ((left, full_ref), (right, final)):
                require(not validate_evaluation_record(record), "Invalid final evaluation evidence")
                require(record["split"]["sample_ids"] == ["4", "5"], "Evaluation did not consume held-out rows")
                require(record["treatment"]["checkpoint"] == selected["sha256"], "Evaluation selected another checkpoint")
            for key in ("value", "predictions", "dataset", "split", "protocol", "metric"):
                require(left[key] == right[key], "Final item-addressed evaluation differs: " + key)
        exports = []
        for row, selected in ((full, full_ref), (resumed, final)):
            directory = self.backend.root / "jobs" / row["operation_id"] / "outputs/export"
            descriptor = read_export(directory)
            require(descriptor["verification"]["fresh_process"] and descriptor["verification"]["max_abs_difference"] == 0, "Fresh export prediction parity failed")
            require(descriptor["selected_checkpoint"] == selected["sha256"], "Export selected another checkpoint")
            require({item["license_policy_id"] for item in descriptor["rights"]["sources"]} == self.policies and not descriptor["rights"]["unresolved"], "Export rights do not match three actual reviews")
            artifacts = self.call("run.artifacts", {"backend_run_id": row["backend_run_id"]})["artifacts"]
            artifact = next(item for item in artifacts if item["node_id"] == "export" and item["port"] == "exported")
            identity = {"backend_run_id": row["backend_run_id"], "artifact_id": artifact["artifact_id"]}
            inspected = self.call("model.export.inspect", identity)
            package = self.call("model.export.package", {**identity, "expected_export_digest": inspected["export_digest"]})
            require(bool(package["sha256"]), "Missing packaged export digest")
            exports.append(sha(directory / "model.safetensors"))
        require(exports[0] == exports[1], "Selected exported weights differ")
        negatives = self.negative_checkpoints(resume, recovered)
        require((job / "request.json").read_bytes() == parent_before, "Negative gates mutated parent request")
        for row, steps in ((full, 8), (parent, 4), (resumed, 4)):
            directory = self.backend.root / "jobs" / row["operation_id"]
            require((directory / "trainer-entered").is_file() and (directory / "optimizer-entered").is_file(), "Entry observation was not live")
            require(len((directory / "trainer-entered").read_text().splitlines()) == 1, "Trainer entered more than once")
            require(len((directory / "optimizer-entered").read_text().splitlines()) == steps, "Observed optimizer step count differs")
        assertions = {**checked["assertions"], "permission_gates_before_native_job": True, "exact_continuation_label_and_subjects": True,
            "cpu_float32_scope_verified": True, "observed_optimizer_step_counts_exact": True,
            "selected_evaluation_and_export_equal": True, "fresh_process_export_verification": True, "parent_request_immutable": True,
            "parent_contract_retained_over_current_edits": True, "three_public_synthetic_reviews": True, "unsafe_checkpoint_gates_before_optimization": True, "cpu_float32_complete_native_checkpoints": True}
        dump(self.work / "scientific-receipt.json", {"schema": "modelbuilder.installed-trainer-scientific/v1", "status": "PASS",
            "scope": "Static five-node CPU float32 eager Trainer v2; no streaming continuation", "assertions": assertions,
            "runs": {"full": full, "parent": parent, "resumed": resumed}, "negative_receipts": negatives,
            "checkpoints": {"full": full_ref, "parent": recovered, "resumed": final}, "export_weight_hashes": exports})
        return assertions

    def negative_checkpoints(self, resume, recovered):
        from modelbuilder.backend.components import transformers_training
        archive = Path(recovered["uri"]) / "data.tar.gz"
        require(archive.resolve().is_relative_to(self.backend.root.resolve()) and not archive.is_symlink(), "Unsafe native archive path")
        original = archive.read_bytes()
        backup = self.work / "original-parent-data.tar.gz"
        backup.write_bytes(original)
        results = []
        try:
            for case in ("partial-trainer-state", "changed-contract", "missing-optimizer"):
                invalid = self.work / case
                shutil.copytree(Path(recovered["path"]), invalid)
                if case == "partial-trainer-state":
                    saved = read(invalid / "trainer_state.json")
                    saved["epoch"] = 1.5
                    (invalid / "trainer_state.json").write_text(json.dumps(saved))
                elif case == "changed-contract":
                    saved = read(invalid / "epoch-state.json")
                    saved["contract"]["parameters"]["horizon_epochs"] = 5
                    (invalid / "epoch-state.json").write_text(json.dumps(saved))
                else:
                    (invalid / "optimizer.pt").unlink()
                manifest = read(invalid / "checkpoint.manifest.json")
                manifest["assets"] = transformers_training._assets(invalid)
                (invalid / "checkpoint.manifest.json").write_text(json.dumps(manifest))
                with tarfile.open(archive, "w:gz") as tar:
                    tar.add(invalid, arcname=".")
                current = self.call("recipe.get", {"recipe_id": self.recipe["recipe_id"]})
                candidate = {**resume, "expected_recipe_revision": current["revision"], "idempotency_key": "reject-" + case}
                rejection = ("Exact epoch checkpoint adapter, assets or original horizon differ" if case == "missing-optimizer"
                             else "Exact resume requires unchanged complete native epoch/horizon and options")
                failed = self.wait(self.call("run.resume", candidate)["operation_id"], "failed")
                logs = self.call("run.logs", {"operation_id": failed["operation_id"]})
                require("ValueError: " + rejection in json.dumps(logs), "Wrong negative checkpoint failure")
                failed_job = self.backend.root / "jobs" / failed["operation_id"]
                require(not (failed_job / "trainer-entered").exists() and not (failed_job / "optimizer-entered").exists(), "Unsafe state reached Trainer/optimizer")
                require(not list((failed_job / "outputs/train/checkpoints").glob("*/checkpoint.manifest.json")), "Unsafe state published checkpoint")
                results.append({"case": case, "expected_rejection": rejection, "status": failed, "trainer_entered": False, "optimizer_entered": False})
                archive.write_bytes(original)
        finally:
            archive.write_bytes(original)
        require(archive.read_bytes() == backup.read_bytes(), "Parent archive restoration failed")
        return results

    def cleanup(self):
        """Every owned job, including submits interrupted before a result, is reconciled."""
        errors, records = [], []
        deadline = time.monotonic() + 110
        if self.backend is None or self.service is None or self.project is None:
            return {"status": "PASS", "jobs": records, "errors": errors}
        for job in sorted((self.backend.root / "jobs").glob("*")):
            try:
                require(job.is_dir() and not job.is_symlink() and job.resolve().is_relative_to(self.work.resolve()), "Job outside driver")
                request = read(job / "request.json")
                require(request["operation_id"] == job.name and Path(request["job_dir"]).resolve() == job.resolve(), "Job owner differs")
                require(request["project_id"] == self.project["project_id"], "Foreign project job")
                row = self.call("run.status", {"operation_id": job.name})
                if not stopped(row):
                    self.call("run.cancel", {"operation_id": job.name, "idempotency_key": "driver-cleanup-" + job.name})
                while not stopped(row):
                    require(time.monotonic() < deadline, "Owned native cleanup timeout")
                    time.sleep(.1)
                    row = self.call("run.status", {"operation_id": job.name})
                state = read(job / "state.json")
                require(state.get("termination", {}).get("confirmed") is True, "Termination unconfirmed")
                records.append({"job": str(job), "status": row, "request": request, "state": state})
            except BaseException as error:
                errors.append({"job": str(job), "error": str(error)})
        return {"status": "FAIL" if errors else "PASS", "jobs": records, "errors": errors}

    def run(self):
        failure = None
        assertions = None
        try:
            self.prepare()
            assertions = self.execute()
            require(inventory(self.source) == self.source_before, "Committed test source changed")
            require(inventory(self.work / "assets") == self.assets_before, "Authored model assets changed")
            require(inventory(self.work / "data") == self.data_before, "Registered dataset bytes changed")
            require("torch" not in sys.modules and "transformers" not in sys.modules, "Service imported training stack")
        except BaseException as error:
            failure = {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
        finally:
            try:
                cleanup = self.cleanup()
            except BaseException as error:
                cleanup = {"status": "FAIL", "error": str(error)}
            if self.service is not None:
                for name, resource in (("hub", self.service.hub), ("store", self.service.store)):
                    try:
                        resource.close()
                    except BaseException as error:
                        cleanup["status"] = "FAIL"
                        cleanup.setdefault("errors", []).append({"resource": name, "error": str(error)})
            dump(self.work / "cleanup.json", cleanup)
        if failure is not None or cleanup["status"] != "PASS":
            dump(self.work / "failure.json", {"failure": failure, "cleanup": cleanup})
            raise AssertionError("Installed Trainer acceptance failed; original failure and cleanup retained")
        assertions.update(fixture_source_and_assets_immutable=True, service_backend_interpreters_isolated=True, owned_native_cleanup_confirmed=True)
        # Seal durable file inventories as evidence; raw originals remain in work.
        dump(self.work / "final-file-inventory.json", inventory(self.work))
        evidence = [{"path": str(path), "sha256": sha(path)} for path in sorted(self.work.glob("*.json"))]
        evidence.append({"path": str(self.transcript), "sha256": sha(self.transcript)})
        result = {"schema": RESULT_SCHEMA, "status": "PASS", "driver_id": DRIVER_ID, "role": self.context["role"],
                  "runtime_commit": self.context["runtime_commit"], "context_sha256": sha(self.context_path),
                  "assertions": assertions, "worker_termination_confirmed": True, "evidence": evidence}
        dump(self.output, result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acceptance-context", type=Path)
    parser.add_argument("--backend-mode", choices=("assets", "state"))
    parser.add_argument("--request", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(sys.flags.isolated == 1 and sys.dont_write_bytecode, "Invoke only with -I -B")
    if args.backend_mode:
        require(args.request is not None and args.acceptance_context is None, "Invalid backend helper arguments")
        operation = asset_helper if args.backend_mode == "assets" else state_helper
        dump(args.output, operation(read(args.request)))
    else:
        require(args.acceptance_context is not None and args.request is None, "Missing harness context")
        Driver(read(args.acceptance_context), args.acceptance_context, args.output).run()


if __name__ == "__main__":
    main()
