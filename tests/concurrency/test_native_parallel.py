"""Actual ZenML concurrency, isolated source bodies, and durable native lineage.

The fixture is an independent, temporary, pinned Git source closure. Overlap is
proved by a two-way filesystem barrier, never inferred from elapsed run time.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import subprocess
import time
import uuid

import psutil
import pytest

from modelbuilder.backend import ZenMLBackend
from modelbuilder.iterations import invocation_identity


ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv-backend/bin/python"
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not PYTHON.is_file(), reason="Pinned native backend required")]

SOURCE = r'''
from pathlib import Path
import fcntl
import json
import os
import random
import subprocess
import sys
import time

def event(directory, kind, name, **details):
    raw = (json.dumps(dict(kind=kind, name=name, pid=os.getpid(),
                           at=time.monotonic(), **details)) + "\n").encode()
    fd = os.open(str(Path(directory) / "events.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try: os.write(fd, raw)
    finally: os.close(fd)

def counter(directory, change):
    path = Path(directory) / "counts.json"
    with (Path(directory) / "counts.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else {"active": 0, "maximum": 0}
        state["active"] += change
        state["maximum"] = max(state["active"], state["maximum"])
        path.write_text(json.dumps(state))

def root(name: str, directory: str, seed: int, peers: list, delay: float = .2,
         fail_once: bool = False, use_torch: bool = True, spawn_child: bool = False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if use_torch:
        import torch
        torch.set_num_threads(1)
        torch.manual_seed(seed)
    random.seed(seed)
    counter(directory, 1)
    attempt_file = directory / (name + ".attempt")
    attempt = int(attempt_file.read_text()) + 1 if attempt_file.exists() else 1
    attempt_file.write_text(str(attempt))
    event(directory, "start", name, attempt=attempt, module=MODULE_NAME)
    child = None
    try:
        if spawn_child:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            event(directory, "child", name, child_pid=child.pid)
        (directory / (name + ".ready")).write_text(str(os.getpid()))
        deadline = time.monotonic() + 20
        while not all((directory / (peer + ".ready")).is_file() for peer in peers):
            if time.monotonic() > deadline:
                raise RuntimeError("Independent native stages did not overlap at the barrier: " + name)
            time.sleep(.01)
        # Draw only after BOTH stages seed their interpreter-global RNGs.
        python_values = [random.random() for _ in range(8)]
        torch_values = torch.rand(8).tolist() if use_torch else []
        time.sleep(delay)
        if fail_once and attempt == 1:
            raise RuntimeError("deliberate first-attempt failure")
        path = directory / (name + "-" + str(os.getpid()) + ".txt")
        path.write_text(name)
        result = {"name": name, "pid": os.getpid(), "module": MODULE_NAME,
                  "attempt": attempt, "python": python_values, "torch": torch_values}
        return path, result
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=5)
        event(directory, "end", name, attempt=attempt)
        counter(directory, -1)

def join(left_path: Path, right_path: Path, left: dict, right: dict,
         third: dict, fourth: dict) -> dict:
    assert left_path.read_text() == left["name"] == "left"
    assert right_path.read_text() == right["name"] == "right"
    return {"names": [left["name"], right["name"], third["name"], fourth["name"]],
            "pids": [left["pid"], right["pid"], third["pid"], fourth["pid"]]}

def root_data(**kwargs) -> dict:
    return root(**kwargs)[1]

def join_values(left: dict, right: dict, third: dict, fourth: dict) -> dict:
    return {"names": [item["name"] for item in (left, right, third, fourth)]}

def seed() -> int:
    return 0

def iterate(value: int, name: str, directory: str):
    value += 1
    _, result = root(name + "-" + str(value), directory, value,
                     [other + "-" + str(value) for other in ["left", "right"]],
                     use_torch=False)
    return value, result

def finish(left: int, right: int) -> dict:
    return {"left": left, "right": right}
'''


def setup(tmp_path, *, cache=False, roots=4, torch=True, delay=.2, spawn_child=False):
    source = tmp_path / "source"
    source.mkdir()
    # Intentionally reuse the same declared Python module identity while each
    # pinned file has a different global. Native threads must not race imports.
    for name in ("left", "right", "third", "fourth"):
        (source / (name + ".py")).write_text("MODULE_NAME = " + repr(name) + "\n" + SOURCE)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "Independent concurrency fixture"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    files = [{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
             for path in sorted(source.glob("*.py"))]
    project = str(uuid.uuid4())
    recipe = {"schema_version": "0", "recipe_id": str(uuid.uuid4()), "project_id": project,
              "revision": 1, "name": "Independent native bounded concurrency",
              "source": {"repository": str(source), "commit": commit, "files": files},
              "execution": {"max_parallel_stages": 2}, "nodes": [], "edges": [],
              "environment": {"python": "3.12", "requirements": ["zenml==0.97.0"]},
              "resources": [], "external_dependencies": [], "evaluation": {}, "outputs": []}
    for name in ("left", "right", "third", "fourth")[:roots]:
        recipe["nodes"].append(node(recipe, name, "root_data" if cache else "root", {},
            {"data": "dict"} if cache else {"file": "Path", "data": "dict"},
            {"name": name, "directory": str(tmp_path / "observed"), "seed": 17 if name == "left" else 43,
             "peers": ["left", "right"] if name in {"left", "right"} else [], "delay": delay,
             "use_torch": torch, "spawn_child": spawn_child}, file=name + ".py", cache=cache))
    if roots == 4:
        join_inputs = {name: "dict" for name in ("left", "right", "third", "fourth")}
        if not cache:
            join_inputs.update(left_path="Path", right_path="Path")
        recipe["nodes"].append(node(recipe, "join", "join_values" if cache else "join", join_inputs,
            {"summary": "dict"}, {}))
        for name in ("left", "right", "third", "fourth"):
            recipe["edges"].append(edge(name, "data", "join", name))
        if not cache:
            recipe["edges"] += [edge(name, "file", "join", name + "_path") for name in ("left", "right")]
    backend = ZenMLBackend(tmp_path / "state", python_executable=str(PYTHON),
                           source_root=source, trusted_code_roots=[str(source)])
    return backend, {"project_id": project, "project_recipe_id": recipe["recipe_id"],
                     "recipe": recipe, "trusted": True, "max_runtime_seconds": 150}


def node(recipe, name, entrypoint, inputs, outputs, parameters, *, file="left.py", cache=False):
    reference = next(item for item in recipe["source"]["files"] if item["path"] == file)
    return {"id": name, "kind": "code.python", "implementation_ref": {
                **reference, "commit": recipe["source"]["commit"], "module": "shared_parallel_source", "entrypoint": entrypoint},
            "inputs": [{"name": key, "type": value} for key, value in inputs.items()],
            "outputs": [{"name": key, "type": value} for key, value in outputs.items()],
            "parameters": parameters, "environment_ref": "default", "resource_request": {"gpu": 0},
            "retry": {"max_attempts": 1, "backoff_seconds": 0}, "timeout": 60,
            "cache_policy": "enabled" if cache else "disabled", "provenance": {}}


def edge(source, source_port, target, target_port):
    return {"source_node": source, "source_port": source_port, "target_node": target, "target_port": target_port}


def submit(backend, payload):
    request = {**copy.deepcopy(payload), "operation_id": str(uuid.uuid4()), "idempotency_key": str(uuid.uuid4())}
    plan = backend.invoke("run.plan", request)
    assert plan["executable"], plan
    backend.invoke("run.submit", request)
    return request


def finish(backend, request, *, limit=180):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        result = backend.invoke("run.status", request)
        if result["status"] in {"succeeded", "failed", "cancelled"} and not result["worker_alive"]:
            (backend.root.parent / (request["operation_id"] + "-acceptance.json")).write_text(json.dumps(result, indent=2))
            return result
        time.sleep(.2)
    backend.invoke("run.cancel", request)
    raise AssertionError(result)


def execute(backend, payload):
    request = submit(backend, payload)
    return finish(backend, request), request


def native_steps(backend, result):
    script = """import json,sys
from zenml.client import Client
run=Client().get_pipeline_run(sys.argv[1])
print(json.dumps({name:{'id':str(step.id),'extra':step.config.extra,'inputs':{port:[str(a.id) for a in values] for port,values in step.inputs.items()},'outputs':{port:[str(a.id) for a in values] for port,values in step.outputs.items()}} for name,step in run.steps.items()}))
"""
    response = subprocess.run([str(PYTHON), "-c", script, result["backend_run_id"]],
                              env=backend._env(), capture_output=True, text=True, check=True)
    return json.loads(response.stdout.strip().splitlines()[-1])


def data_artifact(result, name, port="data"):
    return next(item for item in result["artifacts"] if item["node_id"] == name and item["port"] == port)


def data_value(result, name, port="data"):
    return json.loads((Path(data_artifact(result, name, port)["uri"]) / "data.json").read_text())


def events(tmp_path):
    path = tmp_path / "observed/events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def assert_overlap(rows, left="left", right="right"):
    starts = {row["name"]: row for row in rows if row["kind"] == "start"}
    ends = {row["name"]: row for row in rows if row["kind"] == "end"}
    assert starts[left]["pid"] != starts[right]["pid"]
    assert max(starts[left]["at"], starts[right]["at"]) < min(ends[left]["at"], ends[right]["at"])


def reopened(backend):
    return ZenMLBackend(backend.root.parent, python_executable=str(PYTHON),
                        source_root=backend.repository, trusted_code_roots=backend.trusted_code_roots)


def assert_reaped(job, expected_pids):
    state = json.loads((job / "state.json").read_text())
    assert state["worker_reaped"] is True and state["process_group_terminated"] is True, state
    owners = [json.loads(path.read_text()) for path in (job / "processes").glob("*/owner.json")]
    assert expected_pids <= {owner["pid"] for owner in owners}, owners
    assert all(owner["reaped"] for owner in owners), owners
    for pid in expected_pids:
        assert not psutil.pid_exists(pid), f"Trusted stage child {pid} was not reaped"
    assert not list((job / "processes").glob("*/request.json"))


def test_native_parallel_barrier_bound_typed_lineage_and_rng_isolation(tmp_path):
    backend, payload = setup(tmp_path)
    result, request = execute(backend, payload)
    assert result["status"] == "succeeded", result
    rows = events(tmp_path)
    assert_overlap(rows)
    counts = json.loads((tmp_path / "observed/counts.json").read_text())
    assert counts == {"active": 0, "maximum": 2}
    roots = ("left", "right", "third", "fourth")
    starts = [row for row in rows if row["kind"] == "start"]
    assert {row["name"] for row in starts} == set(roots)
    assert len({row["pid"] for row in starts}) == 4
    expected = subprocess.run([str(PYTHON), "-c",
        "import json,torch; print(json.dumps({str(seed):torch.rand(8,generator=torch.Generator().manual_seed(seed)).tolist() for seed in (17,43)}))"],
        text=True, capture_output=True, check=True)
    expected_torch = json.loads(expected.stdout.strip().splitlines()[-1])
    native = native_steps(backend, result)
    for name in roots:
        value = data_value(result, name)
        seed = 17 if name == "left" else 43
        rng = random.Random(seed)
        assert value["python"] == [rng.random() for _ in range(8)]
        assert value["torch"] == expected_torch[str(seed)]
        assert value["module"] == name
        assert native["join"]["inputs"][name] == [data_artifact(result, name)["artifact_id"]]
        assert set(native[name]["outputs"]) == {"file", "data"}
    for name in ("left", "right"):
        assert native["join"]["inputs"][name + "_path"] == [data_artifact(result, name, "file")["artifact_id"]]
    assert data_value(result, "join", "summary")["names"] == list(roots)
    assert_reaped(backend.root / "jobs" / request["operation_id"], {row["pid"] for row in starts})


@pytest.mark.parametrize("explicit", [False, True])
def test_native_serial_default_and_explicit_one_preserve_previous_execution(tmp_path, explicit):
    backend, payload = setup(tmp_path, torch=False)
    if explicit:
        payload["recipe"]["execution"]["max_parallel_stages"] = 1
    else:
        payload["recipe"].pop("execution")
    for item in payload["recipe"]["nodes"]:
        if item["id"] != "join":
            item["parameters"]["peers"] = []
    result, request = execute(backend, payload)
    assert result["status"] == "succeeded", result
    assert json.loads((tmp_path / "observed/counts.json").read_text()) == {"active": 0, "maximum": 1}
    assert data_value(result, "join", "summary")["names"] == ["left", "right", "third", "fourth"]
    assert not list((backend.root / "jobs" / request["operation_id"] / "processes").glob("*/owner.json"))


def test_native_parallel_retry_isolated_attempts_and_cache_lineage(tmp_path):
    backend, payload = setup(tmp_path, cache=True, torch=False)
    left = payload["recipe"]["nodes"][0]
    left["parameters"]["fail_once"] = True
    left["retry"]["max_attempts"] = 2
    original, first = execute(backend, payload)
    assert original["status"] == "succeeded", original
    starts = [row for row in events(tmp_path) if row["kind"] == "start"]
    left_attempts = [row for row in starts if row["name"] == "left"]
    assert [row["attempt"] for row in left_attempts] == [1, 2]
    assert len({row["pid"] for row in left_attempts}) == 2
    assert all(len([row for row in starts if row["name"] == name]) == 1 for name in ("right", "third", "fourth"))
    replay, second = execute(backend, payload)
    assert replay["status"] == "succeeded", replay
    assert set(replay["cached_node_ids"]) == {"left", "right", "third", "fourth"}
    assert [row for row in events(tmp_path) if row["kind"] == "start"] == starts
    native = native_steps(backend, replay)
    for name in ("left", "right", "third", "fourth"):
        for port in ("data",):
            before, after = data_artifact(original, name, port), data_artifact(replay, name, port)
            assert after["artifact_id"] == before["artifact_id"]
            assert after["cache"]["reused"] is True
            assert after["cache"]["producer_backend_run_id"] == original["backend_run_id"]
            assert after["cache"]["producer_step_run_id"] == before["step_id"]
        assert native["join"]["inputs"][name] == [data_artifact(original, name)["artifact_id"]]
    fresh = reopened(backend).invoke("run.status", second)
    assert fresh["artifacts"] == replay["artifacts"]
    assert not list((backend.root / "jobs" / first["operation_id"] / "active-nodes").glob("*.json"))
    artifacts = backend.invoke("run.artifacts", {**second, "include_binding_descriptors": True})["artifacts"]
    bindings = []
    for name in ("left", "right", "third", "fourth"):
        descriptor = next(item["binding_descriptor"] for item in artifacts if item["node_id"] == name)
        bindings.append({"node_id": "join", "input_port": name, **{key: descriptor[key] for key in
            ("backend_run_id", "source_node_id", "source_port", "artifact_id", "content_sha256")}})
    selected = {**payload, "execution": {"node_ids": ["join"], "input_bindings": bindings}}
    cut, third = execute(backend, selected)
    assert cut["status"] == "succeeded" and cut["executed_node_ids"] == ["join"], cut
    assert cut["cached_node_ids"] == []
    assert [row for row in events(tmp_path) if row["kind"] == "start"] == starts
    selected_native = native_steps(backend, cut)
    assert set(selected_native) == {"join"}
    for name in ("left", "right", "third", "fourth"):
        assert selected_native["join"]["inputs"][name] == [data_artifact(original, name)["artifact_id"]]
        bound = next(item for item in cut["execution"]["input_bindings"] if item["input_port"] == name)
        assert bound["cache"]["producer_backend_run_id"] == original["backend_run_id"]
    assert reopened(backend).invoke("run.status", third)["execution"] == cut["execution"]


def test_native_parallel_iterations_join_each_wave_before_feedback(tmp_path):
    backend, payload = setup(tmp_path, torch=False)
    recipe = payload["recipe"]
    recipe["nodes"] = [node(recipe, "seed", "seed", {}, {"value": "int"}, {})]
    for name in ("left", "right"):
        recipe["nodes"].append(node(recipe, name, "iterate", {"value": "int"}, {"value": "int", "data": "dict"},
                                    {"name": name, "directory": str(tmp_path / "observed")}, file=name + ".py"))
    recipe["nodes"].append(node(recipe, "finish", "finish", {"left": "int", "right": "int"}, {"summary": "dict"}, {}))
    recipe["edges"] = [edge("seed", "value", name, "value") for name in ("left", "right")]
    recipe["edges"] += [edge(name, "value", "finish", name) for name in ("left", "right")]
    block = {"id": "repeat", "node_ids": ["left", "right"], "max_iterations": 3,
             "state": [edge(name, "value", name, "value") for name in ("left", "right")]}
    recipe["bounded_iterations"] = [block]
    result, request = execute(backend, payload)
    assert result["status"] == "succeeded", result
    rows = events(tmp_path)
    native = native_steps(backend, result)
    previous = {name: native["seed"]["outputs"]["value"][0] for name in ("left", "right")}
    for iteration in range(1, 4):
        assert_overlap(rows, f"left-{iteration}", f"right-{iteration}")
        if iteration > 1:
            prior_ends = [row["at"] for row in rows if row["kind"] == "end" and row["name"] in {f"left-{iteration-1}", f"right-{iteration-1}"}]
            starts = [row["at"] for row in rows if row["kind"] == "start" and row["name"] in {f"left-{iteration}", f"right-{iteration}"}]
            assert max(prior_ends) < min(starts)
        for name in ("left", "right"):
            identity = invocation_identity(name, block, iteration)
            invocation = native[identity["invocation_id"]]
            assert all(invocation["extra"][key] == value for key, value in identity.items())
            assert invocation["inputs"]["value"] == [previous[name]]
            previous[name] = invocation["outputs"]["value"][0]
    assert all(native["finish"]["inputs"][name] == [previous[name]] for name in ("left", "right"))
    assert data_value(result, "finish", "summary") == {"left": 3, "right": 3}
    assert json.loads((tmp_path / "observed/counts.json").read_text())["maximum"] == 2
    recovered = reopened(backend).invoke("run.status", request)
    assert recovered["invocation_statuses"] == result["invocation_statuses"]
    assert recovered["bounded_iterations"] == result["bounded_iterations"]


@pytest.mark.parametrize("allowed,component", [(True, False), (False, True)])
def test_native_parallel_conditions_optional_join_and_no_false_import(tmp_path, allowed, component):
    path = ROOT / "tests/conditions/test_native_conditions.py"
    spec = importlib.util.spec_from_file_location("parallel_condition_fixture", path)
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    backend, payload = helpers.branch_setup(tmp_path, allowed, component=component)
    payload["recipe"]["execution"] = {"max_parallel_stages": 2}
    result, request = execute(backend, payload)
    assert result["status"] == "succeeded", result
    skipped = {"never", "alternate"} if allowed else {"branch_gate", "target", "never", "required"}
    assert set(result["skipped_node_ids"]) == skipped
    kept, omitted, producer = ("left", "right", "required") if allowed else ("right", "left", "alternate")
    inputs = native_steps(backend, result)["join"]["inputs"]
    assert omitted not in inputs
    assert inputs[kept] == [data_artifact(result, producer, "metrics")["artifact_id"]]
    assert data_value(result, "join", "joined")["present"] == kept
    assert not any(item["node_id"] in skipped for item in result["artifacts"])
    job = backend.root / "jobs" / request["operation_id"]
    assert "false branch source must never be imported" not in (job / "worker.log").read_text()
    assert reopened(backend).invoke("run.status", request)["conditional_inputs"] == result["conditional_inputs"]


def wait_for_roots(backend, request, tmp_path):
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        rows = events(tmp_path)
        if {row["name"] for row in rows if row["kind"] == "start"} == {"left", "right"}:
            return rows
        result = backend.invoke("run.status", request)
        assert result["status"] not in {"failed", "cancelled", "succeeded"}, result
        time.sleep(.1)
    raise AssertionError("Both independent native bodies did not start")


def test_native_parallel_deadlines_are_per_invocation_and_cancel_all_children(tmp_path):
    backend, payload = setup(tmp_path, roots=2, torch=False, delay=30)
    for item in payload["recipe"]["nodes"]:
        item["timeout"] = 8
    request = submit(backend, payload)
    job = backend.root / "jobs" / request["operation_id"]
    try:
        rows = wait_for_roots(backend, request, tmp_path)
        active = [json.loads(path.read_text()) for path in (job / "active-nodes").glob("*.json")]
        assert {item["node_id"] for item in active} == {"left", "right"}, active
        assert len({item["token"] for item in active}) == 2
        assert len({item["invocation_id"] for item in active}) == 2
        expected = min(active, key=lambda item: item["started_monotonic"] + item["timeout_seconds"])
        result = finish(backend, request)
        assert result["status"] == "cancelled" and not result["worker_alive"], result
        cancellation = json.loads((job / "cancel.json").read_text())
        assert cancellation["reason"] == "node_timeout_exceeded"
        assert cancellation["node_id"] == expected["node_id"]
        assert cancellation["invocation_id"] == expected["invocation_id"]
        assert not result["artifacts"]
        assert_reaped(job, {row["pid"] for row in rows if row["kind"] == "start"})
        assert reopened(backend).invoke("run.status", request)["status"] == "cancelled"
    finally:
        backend.invoke("run.cancel", request)


def test_native_parallel_cancel_after_service_reopen_reaps_bodies_and_descendants(tmp_path):
    backend, payload = setup(tmp_path, roots=2, torch=False, delay=60, spawn_child=True)
    request = submit(backend, payload)
    job = backend.root / "jobs" / request["operation_id"]
    try:
        wait_for_roots(backend, request, tmp_path)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            children = [row["child_pid"] for row in events(tmp_path) if row["kind"] == "child"]
            if len(children) == 2:
                break
            time.sleep(.05)
        assert len(children) == 2 and all(psutil.pid_exists(pid) for pid in children)
        restored = reopened(backend)
        assert restored.invoke("run.status", request)["worker_alive"]
        result = restored.invoke("run.cancel", request)
        assert result["status"] == "cancelled" and not result["worker_alive"], result
        result = finish(restored, request)
        assert not result["artifacts"]
        starts = [row for row in events(tmp_path) if row["kind"] == "start"]
        assert_reaped(job, {row["pid"] for row in starts})
        assert all(not psutil.pid_exists(pid) for pid in children), children
        assert not result["skipped_node_ids"]
        assert reopened(backend).invoke("run.status", request)["status"] == "cancelled"
    finally:
        backend.invoke("run.cancel", request)
