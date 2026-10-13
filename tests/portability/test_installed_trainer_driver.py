"""Lightweight contract tests: no model imports, dependencies installed, or jobs."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("installed_trainer_driver", Path(__file__).with_name("installed_trainer_driver.py"))
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


def context(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    for name in ("service", "backend"):
        (tmp_path / name).write_text("inert executable placeholder")
    selected = {"schema": driver.CONTEXT_SCHEMA, "driver_id": driver.DRIVER_ID, "role": "origin", "runtime_commit": "a" * 40,
                "timeout_seconds": 600, "driver_work": str(work), "source_root": str(source),
                "service_python": str(tmp_path / "service"), "backend_python": str(tmp_path / "backend"),
                "driver_sha256": driver.sha(driver.__file__)}
    path = tmp_path / "context.json"
    driver.dump(path, selected)
    return selected, path, work / "result.json"


def test_fresh_context(tmp_path):
    value, path, output = context(tmp_path)
    assert driver.validate_context(value, path, output) == output.parent


@pytest.mark.parametrize("change", ["schema", "driver_id", "role", "runtime_commit", "timeout_seconds", "driver_sha256", "same_python", "source_overlap", "used_work"])
def test_context_rejects_wrong_or_reused_identity(tmp_path, change):
    value, path, output = context(tmp_path)
    if change == "same_python":
        value["backend_python"] = value["service_python"]
    elif change == "source_overlap":
        value["source_root"] = str(output.parent)
    elif change == "used_work":
        (output.parent / "previous").write_text("never overwrite")
    else:
        value[change] = 1 if change == "timeout_seconds" else "wrong"
    with pytest.raises(AssertionError):
        driver.validate_context(value, path, output)


def histories():
    contract = {"scheduler_horizon_steps": 8, "parameters": {"horizon_epochs": 4}}
    orders = [(0, 1, 2, 3), (3, 2, 1, 0), (1, 0, 3, 2), (2, 3, 0, 1)]
    full = {"lineage": [{"sample_id": str(i), "content_digest": str(i) * 64} for order in orders for i in order],
            "contract": contract, "completed_epochs": 4, "history": [{"epoch": i, "metrics": {"eval_loss": 1 / i}} for i in range(1, 5)],
            "decision": {"outcome": "stop"}}
    resumed = deepcopy(full)
    parent = {**deepcopy(full), "completed_epochs": 2, "history": deepcopy(full["history"][:2]), "lineage": deepcopy(full["lineage"][:8])}
    return full, resumed, parent


def test_complete_epoch_scientific_contract():
    checks = driver.check_epoch_json(*histories())
    assert checks and all(checks.values())


@pytest.mark.parametrize("change", ["row", "content", "history", "horizon", "completed", "decision", "parent_history", "parent_cursor", "membership", "constant_order"])
def test_scientific_drift_is_rejected(change):
    full, resumed, parent = histories()
    if change == "row":
        resumed["lineage"][1]["sample_id"] = "9"
    elif change == "content":
        resumed["lineage"][1]["content_digest"] = "other"
    elif change == "history":
        resumed["history"][2]["metrics"]["eval_loss"] += .01
    elif change == "horizon":
        resumed["contract"]["scheduler_horizon_steps"] = 4
    elif change == "completed":
        resumed["completed_epochs"] = 3
    elif change == "decision":
        resumed["decision"]["outcome"] = "continue"
    elif change == "parent_history":
        parent["history"][0]["metrics"]["eval_loss"] = 99
    elif change == "parent_cursor":
        parent["completed_epochs"] = 1
    elif change == "membership":
        full["lineage"][0]["sample_id"] = resumed["lineage"][0]["sample_id"] = "9"
    elif change == "constant_order":
        full["lineage"] = full["lineage"][:4] * 4
        resumed["lineage"] = deepcopy(full["lineage"])
    with pytest.raises(AssertionError):
        driver.check_epoch_json(full, resumed, parent)


def test_stopped_requires_all_explicit_native_liveness_fields():
    row = {"status": "succeeded", "worker_alive": False, "supervisor_alive": False, "process_group_alive": False}
    assert driver.stopped(row)
    for key in row:
        changed = dict(row)
        changed.pop(key)
        assert not driver.stopped(changed)
    assert not driver.stopped({**row, "process_group_alive": True})


def bare_driver(tmp_path):
    obj = driver.Driver.__new__(driver.Driver)
    obj.work = tmp_path
    obj.context = {"backend_python": sys.executable, "runtime_commit": "a" * 40}
    obj.service_identity = {"scope": "inert contract test"}
    obj.service = obj.backend = obj.actor = obj.project = None
    obj.deadline = time.monotonic() + 120
    obj.transcript = tmp_path / "operations.jsonl"
    obj.env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "TZ"}}
    obj.env["HOME"] = str(tmp_path / "home")
    return obj


def test_public_rights_construct_exact_five_nodes_without_native_execution(tmp_path):
    obj = bare_driver(tmp_path)
    def inert_assets(name, request):
        assert name == "assets"
        for name in ("config.json", "tokenizer.json", "weights.safetensors"):
            (Path(request["assets"]) / name).write_text("inert rights contract bytes; never model-loaded")
        return {"status": "PASS"}
    obj.helper = inert_assets
    try:
        obj.prepare()
        nodes = {row["id"]: row for row in obj.recipe["nodes"]}
        assert len(nodes) == 5 and len(obj.recipe["edges"]) == 5
        assert nodes["train"]["parameters"]["horizon_epochs"] == 4
        assert nodes["train"]["component"]["version"] == "2"
        assert {row["source_node"] for row in obj.recipe["edges"]} == {"training_data", "validation_data", "train"}
        policies = obj.call("hub.license.policy.list")["policies"]
        assert len(policies) == 3 and {row["policy_id"] for row in policies} == obj.policies
        assert all(row["reviewed_by"] == obj.actor.id for row in policies)
        operations = [json.loads(line)["operation"] for line in obj.transcript.read_text().splitlines()]
        assert operations.count("hub.license.review") == 6  # dispatch plus result
        assert "run.submit" not in operations and not (obj.backend.root / "jobs").exists()
        assert "torch" not in sys.modules and "transformers" not in sys.modules
        assert driver.inventory(obj.source) == obj.source_before
        assert obj.cleanup() == {"status": "PASS", "jobs": [], "errors": []}
    finally:
        if obj.service is not None:
            obj.service.hub.close()
            obj.service.store.close()


@pytest.mark.parametrize("decorated_optimizer", [False, True])
def test_wrapper_observes_calls_without_patching_and_restores_profiler(tmp_path, monkeypatch, decorated_optimizer):
    job = tmp_path / "state/backend/jobs/job"
    job.mkdir(parents=True)
    (tmp_path / "state/pause-control").write_text("continue")
    class Trainer:
        def train(self):
            return "trained"
    class AdamW:
        def step(self):
            return "stepped"
    if decorated_optimizer:
        original = AdamW.step
        def wrapped(*args, **kwargs):
            return original(*args, **kwargs)
        AdamW.step = wrapped
    train_original, step_original = Trainer.train, AdamW.step
    def unrelated(args):
        return args
    def underlying(**request):
        assert unrelated(object()) is not None
        assert request["parameters"] == {"unchanged": True}
        Trainer().train()
        AdamW().step()
        raise RuntimeError("deliberate helper failure")
    modules = {name: ModuleType(name) for name in ("torch", "transformers", "modelbuilder.backend.components.transformers_epochs", "modelbuilder.backend.components.transformers_training")}
    modules["torch"].optim = SimpleNamespace(AdamW=AdamW)
    modules["transformers"].Trainer = Trainer
    modules["modelbuilder.backend.components.transformers_epochs"].train = underlying
    modules["modelbuilder.backend.components.transformers_training"].evaluate = lambda **kw: None
    modules["modelbuilder.backend.components.transformers_training"].export = lambda **kw: None
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    namespace = {}
    exec(compile(driver.WRAPPER, "immutable-fixture-adapter.py", "exec"), namespace)
    previous = sys.getprofile()
    try:
        sys.setprofile(None)
        with pytest.raises(RuntimeError, match="deliberate"):
            namespace["train"](inputs={}, parameters={"unchanged": True}, context={"job_dir": str(job)})
        assert sys.getprofile() is None
        assert Trainer.train is train_original and AdamW.step is step_original
        assert (job / "trainer-entered").is_file() and (job / "optimizer-entered").is_file()
    finally:
        sys.setprofile(previous)


def test_cleanup_continues_after_independent_job_failure(tmp_path):
    obj = bare_driver(tmp_path)
    obj.backend = SimpleNamespace(root=tmp_path / "state/backend")
    obj.project = {"project_id": "p"}
    obj.service = object()
    jobs = obj.backend.root / "jobs"
    for name in ("bad", "good"):
        job = jobs / name
        job.mkdir(parents=True)
        driver.dump(job / "request.json", {"operation_id": name, "job_dir": str(job), "project_id": "p"})
        driver.dump(job / "state.json", {"termination": {"confirmed": True}})
    observed = []
    def call(name, payload):
        observed.append((name, payload["operation_id"]))
        if payload["operation_id"] == "bad":
            raise ValueError("bad status")
        return {"status": "succeeded", "worker_alive": False, "supervisor_alive": False, "process_group_alive": False}
    obj.call = call
    result = obj.cleanup()
    assert result["status"] == "FAIL" and len(result["errors"]) == 1 and len(result["jobs"]) == 1
    assert observed == [("run.status", "bad"), ("run.status", "good")]


def test_cleanup_cancels_supervisor_only_job(tmp_path):
    obj = bare_driver(tmp_path)
    obj.backend = SimpleNamespace(root=tmp_path / "state/backend")
    obj.project = {"project_id": "p"}
    obj.service = object()
    job = obj.backend.root / "jobs/job"
    job.mkdir(parents=True)
    driver.dump(job / "request.json", {"operation_id": "job", "job_dir": str(job), "project_id": "p"})
    driver.dump(job / "state.json", {"termination": {"confirmed": True}})
    calls = []
    def call(name, payload):
        calls.append(name)
        return {"status": "cancelled", "worker_alive": False, "supervisor_alive": len(calls) == 1, "process_group_alive": False}
    obj.call = call
    result = obj.cleanup()
    assert result["status"] == "PASS"
    assert calls == ["run.status", "run.cancel", "run.status"]


def test_run_retains_original_failure_and_attempts_cleanup(tmp_path):
    obj = bare_driver(tmp_path)
    def prepare():
        raise ValueError("original setup failure")
    obj.prepare = prepare
    obj.cleanup = lambda: {"status": "FAIL", "errors": ["cleanup diagnostic"]}
    with pytest.raises(AssertionError, match="acceptance failed"):
        obj.run()
    evidence = driver.read(tmp_path / "failure.json")
    assert evidence["failure"]["message"] == "original setup failure"
    assert evidence["cleanup"]["status"] == "FAIL"
    assert not (tmp_path / "result.json").exists()


def test_inventory_rejects_symlinks(tmp_path):
    (tmp_path / "file").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "file")
    with pytest.raises(AssertionError, match="symlink"):
        driver.inventory(tmp_path)


def test_interpreter_aliases_do_not_count_as_independent(tmp_path):
    real = tmp_path / "venv"
    real.mkdir()
    alias = tmp_path / "other"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(AssertionError, match="alias"):
        driver.check_distinct_environments({"prefix": str(real)}, {"prefix": str(alias)})
    driver.check_distinct_environments({"prefix": str(real)}, {"prefix": str(tmp_path / "independent")})


def test_model_noop_is_rejected_without_loading_any_model(monkeypatch):
    module = ModuleType("torch")
    module.equal = lambda a, b: a == b
    monkeypatch.setitem(sys.modules, "torch", module)
    with pytest.raises(AssertionError, match="unchanged"):
        driver.check_model_changed({"w": [1, 2]}, {"w": [1, 2]})
    driver.check_model_changed({"w": [1, 2]}, {"w": [1, 3]})


@pytest.mark.parametrize("change", [None, "step", "completed_epoch", "device", "dtype", "state"])
def test_cpu_float32_complete_checkpoint_scope(change):
    value = {"step": 8, "execution": {"completed_epoch": 4, "device": "cpu", "dtype": "float32"},
             "state": ["model", "optimizer", "scheduler", "rng", "data_cursor"]}
    if change is None:
        driver.check_checkpoint_scope(value, 8, 4)
        return
    if change in {"step", "state"}:
        value[change] = [] if change == "state" else 4
    else:
        value["execution"][change] = "wrong"
    with pytest.raises(AssertionError):
        driver.check_checkpoint_scope(value, 8, 4)


def test_helper_identity_requires_distinct_resolved_prefixes(tmp_path):
    service, backend = tmp_path / "service", tmp_path / "backend"
    service.mkdir()
    backend.mkdir()
    python = backend / "bin/python"
    value = {"prefix": str(backend), "executable": str(python), "module": str(backend / "site-packages/modelbuilder/__init__.py")}
    driver.check_helper_identity(value, {"prefix": str(service)}, str(python))
    alias = tmp_path / "alias"
    alias.symlink_to(backend, target_is_directory=True)
    with pytest.raises(AssertionError, match="alias"):
        driver.check_helper_identity(value, {"prefix": str(alias)}, str(python))
    with pytest.raises(AssertionError, match="Wrong backend prefix"):
        driver.check_helper_identity({**value, "prefix": str(service)}, {"prefix": str(service)}, str(python))
    with pytest.raises(AssertionError, match="outside environment"):
        driver.check_helper_identity({**value, "module": str(service / "source.py")}, {"prefix": str(service)}, str(python))


@pytest.mark.parametrize("changed", [False, True])
def test_backend_state_contract_checks_weight_change_and_all_native_files(tmp_path, monkeypatch, changed):
    full, resumed, parent = histories()
    paths = {}
    for name, epoch in (("full", full), ("resumed", resumed), ("parent", parent)):
        path = tmp_path / name
        path.mkdir()
        paths[name] = str(path)
        driver.dump(path / "epoch-state.json", epoch)
        driver.dump(path / "trainer_state.json", {"global_step": 8, "epoch": 4.0, "max_steps": 8,
                    "num_train_epochs": 4, "log_history": [{"eval_loss": 1 / i, "epoch": i, "step": i * 2} for i in range(1, 5)]})
        for file in ("model.safetensors", "optimizer.pt", "scheduler.pt", "rng_state.pth"):
            (path / file).write_text("inert fixture payload")
    initial = tmp_path / "initial.safetensors"
    initial.write_text("inert initial bytes")
    request = {**paths, "driver_work": str(tmp_path), "backend_python": "unused", "initial_weights": str(initial),
               "initial_sha256": driver.sha(initial), "inventories": {key: driver.inventory(Path(path)) for key, path in paths.items()}}
    loads, comparisons = [], []
    torch = ModuleType("torch")
    torch.equal = lambda a, b: a == b
    def load(path, *, weights_only):
        loads.append((Path(path).name, weights_only))
        return {"state": Path(path).name}
    torch.load = load
    tensors = ModuleType("safetensors.torch")
    tensors.load_file = lambda path: {"weight": 0 if Path(path) == initial or not changed else 1}
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "safetensors.torch", tensors)
    monkeypatch.setattr(driver, "installed_identity", lambda python: {"scope": "mocked"})
    monkeypatch.setattr(driver, "state_equal", lambda a, b: comparisons.append((a, b)))
    if not changed:
        with pytest.raises(AssertionError, match="unchanged"):
            driver.state_helper(request)
        assert not loads
    else:
        result = driver.state_helper(request)
        assert result["assertions"]["actual_model_optimization_observed"]
        assert loads == [("optimizer.pt", True)] * 2 + [("scheduler.pt", True)] * 2 + [("rng_state.pth", False)] * 2
        assert len(comparisons) == 4 and all(a == b for a, b in comparisons)
