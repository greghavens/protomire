"""Real process cancellation before native run creation, without invented IDs."""
from contextlib import contextmanager
import hashlib
import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import psutil
from modelbuilder.backend import BackendError, ZenMLBackend
from modelbuilder.backend.common import alive, read_json
from modelbuilder.recipe import validate_recipe

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv-backend/bin/python"


@pytest.fixture(autouse=True)
def cancel_unfinished_owned_jobs(tmp_path):
    yield
    for path in (tmp_path / "data/backend/jobs").glob("*/request.json"):
        state = read_json(path.parent / "state.json")
        if alive(state.get("worker_pid"), state.get("worker_identity")):
            request = read_json(path)
            backend = ZenMLBackend(tmp_path / "data", python_executable=str(PYTHON),
                                   source_root=request["source_root"],
                                   trusted_code_roots=request["trusted_code_roots"])
            backend.invoke("run.cancel", request)


@contextmanager
def early_request(tmp_path):
    if not PYTHON.is_file():
        pytest.skip("Pinned backend interpreter is required")
    source = tmp_path / "trusted-source"
    source.mkdir()
    code = source / "owned_workload.py"
    code.write_text('def result() -> str:\n    return "finished"\n')
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "add", code.name], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=Modelbuilder fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-qm", "Pin owned slow-start test source"], cwd=source, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    sha = hashlib.sha256(code.read_bytes()).hexdigest()
    project_id = str(uuid.uuid4())
    recipe = {"schema_version": "0", "recipe_id": str(uuid.uuid4()), "revision": 1,
              "name": "Owned pre-native cancellation fixture",
              "project_id": project_id, "source": {"repository": "https://example.invalid/disposable-test.git",
                                                "commit": commit, "files": [{"path": code.name, "sha256": sha}]},
              "nodes": [{"id": "owned_workload", "kind": "code.python", "implementation_ref": {
                  "module": "owned_workload", "entrypoint": "result", "path": code.name, "sha256": sha, "commit": commit},
                  "inputs": [], "outputs": [{"name": "result", "type": "str"}], "parameters": {}, "timeout": 120,
                  "environment_ref": "default", "resource_request": {"gpu": 0},
                  "retry": {"max_attempts": 1, "backoff_seconds": 0}, "cache_policy": "disabled", "provenance": {}}],
              "edges": [], "resources": [], "external_dependencies": [],
              "evaluation": {}, "outputs": [{"name": "result", "type": "str"}],
              "environment": {"python": "3.12", "requirements": ["zenml==0.97.0"]}}
    recipe = validate_recipe(recipe)
    backend = ZenMLBackend(tmp_path / "data", python_executable=str(PYTHON), source_root=source,
                           trusted_code_roots=[str(source)])
    request = {"project_id": project_id, "operation_id": str(uuid.uuid4()), "trusted": True,
               "idempotency_key": str(uuid.uuid4()), "recipe": recipe, "max_runtime_seconds": 90}
    backend.invoke("run.submit", request)
    job = backend.root / "jobs" / request["operation_id"]
    try:
        deadline = time.monotonic() + 10
        state = read_json(job / "state.json")
        while not state.get("worker_pid") and time.monotonic() < deadline:
            time.sleep(0.01)
            state = read_json(job / "state.json")
        assert alive(state.get("worker_pid"), state.get("worker_identity")), state
        # code.python imports now happen inside an already-created native step.
        # Hold the actual supervised process during startup instead, before
        # native registration. Cancellation must still prove OS termination.
        os.kill(state["worker_pid"], signal.SIGSTOP)
        while psutil.Process(state["worker_pid"]).status() != psutil.STATUS_STOPPED and time.monotonic() < deadline:
            time.sleep(0.01)
        assert psutil.Process(state["worker_pid"]).status() == psutil.STATUS_STOPPED
        assert alive(state["worker_pid"], state["worker_identity"])
        assert not read_json(job / "native.json"), "Fixture must stop before actual native run creation"
        yield backend, request
    finally:
        state = read_json(job / "state.json")
        if alive(state.get("worker_pid"), state.get("worker_identity")):
            # The supervisor escalates to SIGKILL even for a stopped worker.
            backend.invoke("run.cancel", request)


def test_confirmed_cancel_before_native_run_creation(tmp_path):
    with early_request(tmp_path) as (backend, request):
        before = backend.invoke("run.status", request)
        assert before["backend_run_id"] is None and before["worker_alive"] is True
        cancelled = backend.invoke("run.cancel", request)
        (ROOT / "evidence/backend/early-cancellation.json").write_text(json.dumps({
            "fixture": "actual worker stopped with SIGSTOP before native registration",
            "before": before, "after": cancelled}, indent=2))
        assert cancelled["status"] == "cancelled", cancelled
        assert cancelled["backend_run_id"] is None and cancelled["backend_status"] is None
        assert cancelled["cancellation_acknowledged"] is True
        assert cancelled["worker_alive"] is False and cancelled["process_group_alive"] is False
        assert cancelled["termination"]["reason"] == "cancelled"
        assert cancelled["termination"]["proof_source"] == "local_supervisor_wait_and_group_observation"
        restarted = ZenMLBackend(tmp_path / "data", python_executable=str(PYTHON), source_root=backend.repository,
                                 trusted_code_roots=[str(backend.repository)])
        assert restarted.invoke("run.status", request)["status"] == "cancelled"
        with pytest.raises(BackendError, match="checkpoint"):
            restarted.invoke("run.resume", {**request, "parent_operation_id": request["operation_id"],
                                             "operation_id": str(uuid.uuid4()), "mode": "exact_state", "steps": 2})


def test_resume_waits_for_pending_cancel_even_with_native_checkpoint(tmp_path):
    """A real checkpoint exists, but a same-run child deliberately needs SIGKILL."""
    from concurrent.futures import ThreadPoolExecutor
    import copy

    source = tmp_path / "owned-source"
    source.mkdir()
    recipe = json.loads((ROOT / "examples/custom-pytorch/recipe.json").read_text())
    recipe.pop("digest", None)
    lock_path = Path(recipe["environment"]["lock_file"]["path"])
    copied_lock = source / lock_path
    copied_lock.parent.mkdir(parents=True)
    copied_lock.write_bytes((ROOT / lock_path).read_bytes())
    fixture_path = Path("src/modelbuilder/backend/fixtures.py")
    copied_fixture = source / fixture_path
    copied_fixture.parent.mkdir(parents=True)
    copied_fixture.write_bytes((ROOT / fixture_path).read_bytes())
    pause = source / "pause.py"
    pause.write_text('import json\nimport os\nimport signal\nimport time\nimport torch\nfrom pathlib import Path\n'
                     'def pause(checkpoint: Path) -> str:\n'
                     '    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                     '    marker = Path(os.environ["MODELBUILDER_JOB_DIR"]) / "pause-started.json"\n'
                     '    marker.write_text(json.dumps({"source_file": __file__, "checkpoint": str(checkpoint)}))\n'
                     '    if torch.load(checkpoint / "checkpoint.pt", map_location="cpu", weights_only=False)["step"] == 3:\n'
                     '        time.sleep(60)\n'
                     '    return str(checkpoint)\n')
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=Modelbuilder fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-qm", "Pin owned pending-cancel fixture"], cwd=source, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    recipe["source"] = {"repository": "https://example.invalid/owned-test.git", "commit": commit,
                        "files": [{"path": str(fixture_path), "sha256": hashlib.sha256(copied_fixture.read_bytes()).hexdigest()},
                                  {"path": str(lock_path), "sha256": hashlib.sha256(copied_lock.read_bytes()).hexdigest()},
                                  {"path": "pause.py", "sha256": hashlib.sha256(pause.read_bytes()).hexdigest()}]}
    next(n for n in recipe["nodes"] if n["kind"] == "model.train")["parameters"]["steps"] = 3
    # Native Python checkpoint values are pathlib.Path. Keep both ends of
    # every connected port consistent so the complete recipe validates too.
    for node in recipe["nodes"]:
        for port in node["inputs"] + node["outputs"]:
            if port["type"] == "checkpoint":
                port["type"] = "Path"
    recipe["nodes"].append({"id": "pause", "kind": "code.python", "implementation_ref": {
        "module": "owned_checkpoint_pause", "entrypoint": "pause", "path": "pause.py", "commit": commit,
        "sha256": hashlib.sha256(pause.read_bytes()).hexdigest()}, "inputs": [{"name": "checkpoint", "type": "Path"}],
        "outputs": [{"name": "result", "type": "str"}], "parameters": {}, "timeout": 120,
        "environment_ref": "default", "resource_request": {"gpu": 0},
        "retry": {"max_attempts": 1, "backoff_seconds": 0}, "cache_policy": "disabled",
        "provenance": {"intended_use": "evaluate"}})
    recipe["edges"].append({"source_node": "train", "source_port": "checkpoint", "target_node": "pause", "target_port": "checkpoint"})
    backend = ZenMLBackend(tmp_path / "data", python_executable=str(PYTHON), source_root=source, trusted_code_roots=[str(source)])
    project_id = str(uuid.uuid4())
    recipe["project_id"] = project_id
    recipe = validate_recipe(recipe)
    request = {"project_id": project_id, "operation_id": str(uuid.uuid4()), "trusted": True, "recipe": recipe,
               "idempotency_key": str(uuid.uuid4()), "max_runtime_seconds": 90}
    from tests.integration.native_fixture_support import authorized_backend
    backend = authorized_backend(backend, request)
    backend.invoke("run.submit", request)
    marker = backend.root / "jobs" / request["operation_id"] / "pause-started.json"
    deadline = time.monotonic() + 45
    while not marker.exists() and time.monotonic() < deadline:
        state = read_json(backend.root / "jobs" / request["operation_id"] / "state.json")
        assert not state.get("worker_reaped"), state
        time.sleep(0.1)
    assert marker.exists(), read_json(backend.root / "jobs" / request["operation_id"] / "state.json")
    # The marker is explicitly external to the checkout. __file__ proves the
    # callback came from the immutable pinned snapshot, not mutable user source.
    paused = read_json(marker)
    snapshot_source = Path(paused["source_file"])
    assert not snapshot_source.is_relative_to(source)
    assert hashlib.sha256(snapshot_source.read_bytes()).hexdigest() == hashlib.sha256(pause.read_bytes()).hexdigest()
    before = backend.invoke("run.status", request)
    assert before["status"] == "running"
    assert any(a["port"] == "checkpoint" for a in before["artifacts"])
    continued = copy.deepcopy(recipe)
    continued["revision"] = recipe["revision"] + 1
    # Exact continuation retains the complete graph. The immutable pause source
    # only waits at the original checkpoint, so the resumed stage can finish.
    next(n for n in continued["nodes"] if n["kind"] == "model.train")["parameters"]["steps"] = 5
    resume = {**request, "parent_operation_id": request["operation_id"], "operation_id": str(uuid.uuid4()),
              "idempotency_key": str(uuid.uuid4()), "recipe": continued, "mode": "exact_state", "steps": 5}
    with ThreadPoolExecutor(max_workers=1) as executor:
        cancellation = executor.submit(backend.invoke, "run.cancel", request)
        cancel_file = backend.root / "jobs" / request["operation_id"] / "cancel.json"
        deadline = time.monotonic() + 20
        while not cancel_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert cancel_file.exists(), "Cancellation request did not reach the supervisor"
        with pytest.raises(BackendError) as rejection:
            backend.invoke("run.resume", resume)
        assert rejection.value.code == "conflict"
        assert not (backend.root / "jobs" / resume["operation_id"]).exists()
        cancelled = cancellation.result(timeout=30)
    assert cancelled["status"] == "cancelled" and cancelled["worker_alive"] is False
    backend.invoke("run.resume", resume)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        child = backend.invoke("run.status", resume)
        if child["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.1)
    assert child["status"] == "succeeded", child
    assert child["parent_run_id"] == cancelled["backend_run_id"]
    assert child["execution"]["node_ids"] == before["execution"]["node_ids"]
    assert "pause" in child["executed_node_ids"]
    (ROOT / "evidence/backend/pending-cancel-resume.json").write_text(json.dumps({
        "source_snapshot": {**paused, "outside_owned_checkout": True,
                            "sha256": hashlib.sha256(pause.read_bytes()).hexdigest()},
        "before": before, "rejection": {"code": rejection.value.code, "message": str(rejection.value)},
        "cancelled": cancelled, "resumed": child}, indent=2))
