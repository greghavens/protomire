"""Inert/local contracts only: no native models, installs, or fixture commits."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("installed_streaming_driver", Path(__file__).with_name("installed_streaming_driver.py"))
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


def context(tmp_path):
    work, source = tmp_path / "work", tmp_path / "source"
    work.mkdir()
    source.mkdir()
    for name in ("service", "backend"):
        (tmp_path / name).write_text("inert interpreter")
    selected = {"schema": driver.CONTEXT_SCHEMA, "driver_id": driver.DRIVER_ID, "role": "origin", "runtime_commit": "a" * 40,
        "timeout_seconds": 600, "driver_work": str(work), "source_root": str(source), "service_python": str(tmp_path / "service"),
        "backend_python": str(tmp_path / "backend"), "driver_sha256": driver.sha(driver.__file__)}
    path = tmp_path / "context.json"
    driver.dump(path, selected)
    return selected, path, work / "result.json"


def test_context(tmp_path):
    c, p, o = context(tmp_path)
    assert driver.validate_context(c, p, o) == o.parent


@pytest.mark.parametrize("change", ["schema", "driver_id", "role", "runtime_commit", "timeout_seconds", "driver_sha256", "same_python", "source_overlap", "used_work"])
def test_context_negative(tmp_path, change):
    c, p, o = context(tmp_path)
    if change == "same_python":
        c["backend_python"] = c["service_python"]
    elif change == "source_overlap":
        c["source_root"] = str(o.parent)
    elif change == "used_work":
        (o.parent / "old").touch()
    else:
        c[change] = 1 if change == "timeout_seconds" else "wrong"
    with pytest.raises(AssertionError):
        driver.validate_context(c, p, o)


def bare(tmp_path):
    obj = driver.Driver.__new__(driver.Driver)
    obj.work, obj.context_path, obj.output = tmp_path, tmp_path / "context.json", tmp_path / "result.json"
    obj.context = {"backend_python": sys.executable, "runtime_commit": "a" * 40}
    obj.service_identity = {"prefix": str(tmp_path / "service")}
    obj.service = obj.backend = obj.actor = obj.project = None
    obj.deadline = time.monotonic() + 120
    obj.transcript = tmp_path / "operations.jsonl"
    obj.env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "TZ"}}
    return obj


@pytest.mark.parametrize("fmt", ["csv", "jsonl"])
@pytest.mark.parametrize("mode", ["proportional", "equal"])
def test_tiny_real_reader_matches_independent_selection_and_consumption(tmp_path, fmt, mode):
    from modelbuilder.dataset import DatasetRegistry
    from modelbuilder.dataset_streaming import StreamingConsumption, iter_stream_rows
    from modelbuilder.service import OperationService
    from modelbuilder.models import Actor
    manifests = driver.write_data(tmp_path / "data", 24)
    oracle = driver.independent_oracle(24)[mode]
    service = OperationService(tmp_path / "application.sqlite3")
    actor = Actor("inert-fixture", {"read", "modify"})
    project = service.invoke("project.create", {"slug": "tiny", "model_name": "tiny", "idempotency_key": "p"}, actor)
    pid = project["project_id"]
    registry = DatasetRegistry(service.store, project_roots={pid: {fmt: tmp_path / "data" / fmt}})
    try:
        spec = driver.sample_policy(mode, 24)
        record = registry.register(pid, {**manifests[fmt], "transformations": [spec]})
        page = registry.preview(pid, record["dataset_id"], 1, "train", purpose="training", limit=100)
        driver.check_selection(record, page, oracle, spec)
        data = registry.read_split(pid, record["dataset_id"], 1, "train", purpose="training")
        tracker = StreamingConsumption(data)
        try:
            assert tracker.snapshot() == driver.expected_consumption(data, [])
            items = list(iter_stream_rows(data))
            assert [{k: v for k, v in row.items() if k != "row"} for row in items] == oracle["selected"]
            for row in items[:4]:
                tracker.add(row)
            assert tracker.snapshot() == driver.expected_consumption(data, oracle["selected"][:4])
            assert tracker.snapshot()["count"] == 4 < len(items)
        finally:
            tracker.close()
    finally:
        service.hub.close()
        service.store.close()


@pytest.mark.parametrize("change", ["selected", "digest", "policy", "quota", "source_cap", "source_rows", "selected_content", "full_lineage"])
def test_scientific_selection_drift_is_rejected(change):
    oracle = driver.independent_oracle(24)["proportional"]
    spec = driver.sample_policy("proportional", 24)
    report = {"selection_digest": oracle["selection_digest"], "policy_digest": driver.canonical(spec), "selected_count": 12,
        "representation": [{"strata": [{"stratum_values": {"label": i}, "before": oracle["before"][i], "after": oracle["after"][i]} for i in (0, 1)]}],
        "source_counts": [{"source_id": "train-source", "before": 24, "after": 12}]}
    record = {"lineage": {"source_row_count": 26}, "row_count": 14, "transformations": [report]}
    page = {"sample_ids": list(oracle["selected_ids"]), "rows": [driver.row_for(item["source_row"] - 1) for item in oracle["selected"]],
            "split": {"lineage_digest": driver.lineage_digest(oracle["selected"])}}
    driver.check_selection(record, page, oracle, spec)
    if change == "selected":
        page["sample_ids"].reverse()
    elif change == "digest":
        report["selection_digest"] = "wrong"
    elif change == "policy":
        report["policy_digest"] = "wrong"
    elif change == "quota":
        report["representation"][0]["strata"][0]["after"] -= 1
    elif change == "source_cap":
        report["source_counts"][0]["after"] -= 1
    elif change == "selected_content":
        page["rows"][-1]["text"] = "wrong"
    elif change == "full_lineage":
        page["split"]["lineage_digest"] = "wrong"
    else:
        record["lineage"]["source_row_count"] -= 1
    with pytest.raises(AssertionError):
        driver.check_selection(record, page, oracle, spec)


@pytest.mark.parametrize("change", [None, "prefetch", "exact_state", "adapter", "step", "device"])
def test_checkpoint_scope_and_actual_consumption(change):
    expected = {"count": 4, "ordered_lineage_digest": "a" * 64}
    descriptor = {"step": 2, "framework": {"adapter": "modelbuilder.transformers.train/v1"},
        "execution": {"device": "cpu", "dtype": "float32"}, "data": {"iteration": "sequential_stream", "consumption": deepcopy(expected)},
        "state": ["model"], "resume_modes": ["warm_start"]}
    if change is None:
        driver.check_checkpoint(descriptor, expected, 2)
        return
    if change == "prefetch":
        descriptor["data"]["consumption"]["count"] = 6
    elif change == "exact_state":
        descriptor["resume_modes"].append("exact_state")
    elif change == "adapter":
        descriptor["framework"]["adapter"] = "modelbuilder.transformers.train/v2"
    elif change == "step":
        descriptor["step"] = 3
    else:
        descriptor["execution"]["device"] = "cuda:0"
    with pytest.raises(AssertionError):
        driver.check_checkpoint(descriptor, expected, 2)


def test_callback_cancel_stops_before_another_optimization_step(tmp_path, monkeypatch):
    job = tmp_path / "state/backend/jobs/job"
    job.mkdir(parents=True)
    (tmp_path / "state/pause-control").write_text("pause")
    cancel = job / "cancel.json"
    calls = []
    def production(**kw):
        calls.append("native_step1")
        kw["context"]["checkpoint_ready"](step=1, path=job / "checkpoint", purpose="last")
        calls.append("UNSAFE_step2")
    def published(**kw):
        calls.append("native_checkpoint_published")
        cancel.touch()
    module = ModuleType("modelbuilder.backend.components.transformers_training")
    module.train, module.evaluate, module.export = production, lambda **kw: None, lambda **kw: None
    monkeypatch.setitem(sys.modules, module.__name__, module)
    namespace = {}
    exec(compile(driver.WRAPPER, "owned-fixture.py", "exec"), namespace)
    with pytest.raises(InterruptedError, match="Public cancellation"):
        namespace["train"](inputs={}, parameters={}, context={"job_dir": str(job), "cancel_file": str(cancel), "checkpoint_ready": published})
    assert calls == ["native_step1", "native_checkpoint_published"]
    assert (job / "boundary-waiting").is_file()
    assert module.train is production


def test_callback_unpaused_is_transparent(tmp_path, monkeypatch):
    job = tmp_path / "state/backend/jobs/job"
    job.mkdir(parents=True)
    (tmp_path / "state/pause-control").write_text("continue")
    seen = []
    def production(**kw):
        kw["context"]["checkpoint_ready"](step=1)
        return {"trained": "unchanged-delegate-result"}
    module = ModuleType("modelbuilder.backend.components.transformers_training")
    module.train, module.evaluate, module.export = production, lambda **kw: None, lambda **kw: None
    monkeypatch.setitem(sys.modules, module.__name__, module)
    namespace = {}
    exec(compile(driver.WRAPPER, "owned-fixture.py", "exec"), namespace)
    result = namespace["train"](inputs={}, parameters={}, context={"job_dir": str(job), "checkpoint_ready": lambda **kw: seen.append(kw)})
    assert result == {"trained": "unchanged-delegate-result"} and seen == [{"step": 1}]
    assert not (job / "boundary-waiting").exists()


def test_public_data_rights_are_real_and_no_job_is_submitted(tmp_path):
    from modelbuilder.service import OperationService
    from modelbuilder.models import Actor
    obj = bare(tmp_path)
    manifests = driver.write_data(tmp_path / "data", 24)
    obj.actor = Actor("authored-owner", {"read", "modify", "execute", "publish"})
    obj.service = OperationService(tmp_path / "application.sqlite3")
    obj.project = obj.call("project.create", {"slug": "rights", "model_name": "rights"})
    pid = obj.project["project_id"]
    obj.service.hub.close()
    obj.service.store.close()
    obj.service = OperationService(tmp_path / "application.sqlite3", dataset_roots={pid: {"jsonl": tmp_path / "data/jsonl"}},
        hub_config={"publication_roots": {pid: {"jsonl": str(tmp_path / "data/jsonl")}}}, hub_providers={})
    try:
        _, policy = obj.review("jsonl", "dataset", ["train.jsonl", "dev.jsonl"])
        record = obj.call("dataset.register", {"manifest": {**manifests["jsonl"], "transformations": [driver.sample_policy("proportional", 24)]}})
        bound = obj.call("hub.dataset.policy.bind", {"dataset_id": record["dataset_id"], "dataset_revision": 1,
            "source_policies": {"train-source": policy["policy_id"], "dev-source": policy["policy_id"]},
            "intended_use": "train", "usage_context": driver.USAGE})
        assert not bound["rights"]["unresolved"]
        assert policy["reviewed_by"] == obj.actor.id
        operations = [json.loads(line)["operation"] for line in obj.transcript.read_text().splitlines()]
        assert "hub.license.local.inspect" in operations and "hub.license.review" in operations
        assert "run.submit" not in operations
    finally:
        obj.service.hub.close()
        obj.service.store.close()


def test_cleanup_attempts_remaining_jobs_after_an_independent_failure(tmp_path):
    obj = bare(tmp_path)
    obj.backend = SimpleNamespace(root=tmp_path / "state/backend")
    obj.project = {"project_id": "p"}
    obj.service = object()
    for name in ("bad", "good"):
        job = obj.backend.root / "jobs" / name
        job.mkdir(parents=True)
        driver.dump(job / "request.json", {"operation_id": name, "job_dir": str(job), "project_id": "p"})
        driver.dump(job / "state.json", {"termination": {"confirmed": True}})
    calls = []
    def call(name, payload):
        calls.append((name, payload["operation_id"]))
        if payload["operation_id"] == "bad":
            raise ValueError("independent failure")
        return {"status": "succeeded", "worker_alive": False, "supervisor_alive": False, "process_group_alive": False}
    obj.call = call
    result = obj.cleanup()
    assert result["status"] == "FAIL" and len(result["jobs"]) == 1 and len(result["errors"]) == 1
    assert calls == [("run.status", "bad"), ("run.status", "good")]


def test_cleanup_cancels_supervisor_only_job(tmp_path):
    obj = bare(tmp_path)
    obj.backend = SimpleNamespace(root=tmp_path / "state/backend")
    obj.project = {"project_id": "p"}
    obj.service = object()
    job = obj.backend.root / "jobs/j"
    job.mkdir(parents=True)
    driver.dump(job / "request.json", {"operation_id": "j", "job_dir": str(job), "project_id": "p"})
    driver.dump(job / "state.json", {"termination": {"confirmed": True}})
    calls = []
    def call(name, payload):
        calls.append(name)
        return {"status": "cancelled", "worker_alive": False, "supervisor_alive": len(calls) == 1, "process_group_alive": False}
    obj.call = call
    assert obj.cleanup()["status"] == "PASS"
    assert calls == ["run.status", "run.cancel", "run.status"]


def test_original_failure_is_preserved_when_cleanup_also_fails(tmp_path):
    obj = bare(tmp_path)
    obj.prepare = lambda: (_ for _ in ()).throw(ValueError("original failure"))
    obj.cleanup = lambda: {"status": "FAIL", "errors": ["cleanup failure"]}
    with pytest.raises(AssertionError, match="acceptance failed"):
        obj.run()
    evidence = driver.read(tmp_path / "failure.json")
    assert evidence["failure"]["message"] == "original failure" and evidence["cleanup"]["status"] == "FAIL"
    assert not obj.output.exists()


def test_helper_names_are_case_scoped_and_always_isolated(tmp_path, monkeypatch):
    obj = bare(tmp_path)
    calls = []
    def command(name, args, **kw):
        calls.append((name, args))
        driver.dump(Path(args[-1]), {"status": "PASS", "identity": {}})
    obj.command = command
    monkeypatch.setattr(driver, "check_helper_identity", lambda *args: None)
    obj.helper("state", {"case": "jsonl-proportional"})
    obj.helper("state", {"case": "csv-equal"})
    assert [name for name, args in calls] == ["state-jsonl-proportional", "state-csv-equal"]
    assert all(args[1:3] == ["-I", "-B"] and args[args.index("--backend-mode") + 1] == "state" for _, args in calls)


def test_interpreter_aliases_fail(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(AssertionError, match="alias"):
        driver.check_distinct_environments({"prefix": str(real)}, {"prefix": str(alias)})


def test_controller_confines_home_tmp_and_drops_inherited_configuration(tmp_path, monkeypatch):
    c, p, o = context(tmp_path)
    monkeypatch.setattr(driver.os, "environ", {"PATH": "/usr/bin", "PYTHONPATH": "/untrusted", "PROVIDER_SECRET": "inert"})
    monkeypatch.setattr(driver.tempfile, "tempdir", None)
    monkeypatch.setattr(driver, "installed_identity", lambda path: {"prefix": str(tmp_path / "service")})
    obj = driver.Driver(c, p, o)
    assert obj.env["TMPDIR"] == str(o.parent / "tmp") and Path(obj.env["TMPDIR"]).is_dir()
    assert obj.env["HOME"] == str(o.parent / "home") and Path(obj.env["HOME"]).is_dir()
    assert "PROVIDER_SECRET" not in obj.env and "PYTHONPATH" not in obj.env
    assert obj.env["HF_HUB_OFFLINE"] == "1" and obj.env["OMP_NUM_THREADS"] == "1"
    assert driver.os.environ == obj.env


@pytest.mark.parametrize("changed", [False, True])
def test_state_helper_checks_real_change_with_inert_tensor_library(tmp_path, monkeypatch, changed):
    class Tensor:
        shape = (1,)
        dtype = "float32"
        def __init__(self, value):
            self.value = value
    initial, trained = tmp_path / "initial", tmp_path / "trained"
    initial.write_text("original inert bytes")
    trained.write_text("trained inert bytes")
    request = {"backend_python": "inert", "driver_work": str(tmp_path), "initial": str(initial), "trained": str(trained),
        "initial_sha256": driver.sha(initial), "trained_sha256": driver.sha(trained)}
    torch = ModuleType("torch")
    torch.float32 = "float32"
    torch.equal = lambda a, b: a.value == b.value
    torch.isfinite = lambda value: SimpleNamespace(all=lambda: True)
    tensors = ModuleType("safetensors.torch")
    tensors.load_file = lambda path, device: {"w": Tensor(1 if Path(path) == trained and changed else 0)}
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "safetensors.torch", tensors)
    monkeypatch.setattr(driver, "installed_identity", lambda path: {"scope": "mocked"})
    if changed:
        assert driver.state_helper(request)["assertions"]["native_weights_changed_and_finite"] is True
    else:
        with pytest.raises(AssertionError, match="unchanged"):
            driver.state_helper(request)


def test_configuration_reuses_genuine_source_policy_across_cases(tmp_path, monkeypatch):
    from modelbuilder.backend.components import validate_component
    obj = bare(tmp_path)
    oracle = driver.independent_oracle(24)
    obj.oracle = oracle
    obj.project = {"project_id": "p", "recipe_id": "r", "revision": 1}
    obj.repository = {"repository_id": "repo"}
    obj.source_identity = {"repository": "urn:sha256:" + "a" * 64, "commit": "b" * 40,
                          "files": [{"path": "adapter.py", "sha256": "a" * 64}]}
    obj.source_policy = None
    obj.model_policy = {"policy_id": "model-policy"}
    obj.data_policies = {fmt: {"policy_id": fmt + "-policy"} for fmt in ("jsonl", "csv")}
    obj.resources = [{"id": name, "sha256": "c" * 64, "metadata": {"path": path}}
                     for name, path in (("config", "config.json"), ("weights", "weights.safetensors"), ("tokenizer", "tokenizer.json"))]
    obj.parameters = driver.parameters(obj.resources)
    obj.records = {"jsonl-proportional": {"dataset_id": "jsonl", "revision": 1, "manifest": {"sources": [{"id": "s"}]}},
                   "csv-equal": {"dataset_id": "csv", "revision": 1, "manifest": {"sources": [{"id": "s"}]}}}
    current = {"recipe_id": "r", "revision": 1, "extensions": {"usage_context": driver.USAGE}}
    reviews, bindings = [], []
    obj.review = lambda *args, **kw: (reviews.append((args, kw)), {"policy_id": "source-policy"})
    obj.preview = lambda record: {}
    monkeypatch.setattr(driver, "check_selection", lambda *args: None)
    obj.service = SimpleNamespace(_dataset_registry=SimpleNamespace(read_split=lambda *args, **kwargs: {"inert": True}))
    def call(name, payload):
        nonlocal current
        if name == "hub.dataset.policy.bind":
            return {"dataset_id": payload["dataset_id"], "dataset_revision": 1}
        if name == "dataset.get":
            return next(row for row in obj.records.values() if row["dataset_id"] == payload["dataset_id"])
        if name == "recipe.get":
            return deepcopy(current)
        if name == "recipe.update":
            current = deepcopy(payload["recipe"])
            current["revision"] += 1
            for node in current["nodes"]:
                validate_component(node)
            return deepcopy(current)
        if name == "hub.source.policy.bind":
            bindings.append(payload)
            current["extensions"]["artifact_rights"] = {"sources": [{"license_policy_id": payload["license_policy_id"]}]}
            current["revision"] += 1
            return {"recipe": deepcopy(current)}
        if name == "dataset.bind":
            current["nodes"].append({"id": payload["node_id"], "kind": "dataset.version.import"})
            current["edges"].append({"source_node": payload["node_id"], "target_node": payload["target_node"]})
            current["revision"] += 1
            return {"recipe": deepcopy(current)}
        raise AssertionError(name)
    obj.call = call
    obj.configure_case("jsonl-proportional")
    obj.configure_case("csv-equal")
    assert len(reviews) == len(bindings) == 1
    assert current["extensions"]["artifact_rights"]["sources"] == [{"license_policy_id": "source-policy"}]
    assert obj.policies == {"model-policy", "csv-policy", "source-policy"}
    assert obj.parameters["max_steps"] == 2 and obj.parameters["architecture"]["limits"]["max_rows"] >= 12
