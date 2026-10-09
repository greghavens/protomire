"""Real process cancellation before native run creation, without invented IDs."""
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from modelbuilder.backend import BackendError, ZenMLBackend
from modelbuilder.backend.common import read_json

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv-backend/bin/python"


def early_request(tmp_path):
    if not PYTHON.is_file():
        pytest.skip("Pinned backend interpreter is required")
    source = tmp_path / "trusted-source"
    source.mkdir()
    code = source / "slow_import.py"
    code.write_text('import time\nfrom pathlib import Path\n'
                    'Path(__file__).with_name("import-started").write_text("ready")\n'
                    'time.sleep(60)\n'
                    'def result() -> str:\n    return "finished"\n')
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "add", "slow_import.py"], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=Modelbuilder fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-qm", "Pin owned slow-start test source"], cwd=source, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    sha = hashlib.sha256(code.read_bytes()).hexdigest()
    recipe = {"schema_version": "0", "source": {"repository": "https://example.invalid/disposable-test.git",
                                                "commit": commit, "files": [{"path": code.name, "sha256": sha}]},
              "nodes": [{"id": "slow_import", "kind": "code.python", "implementation_ref": {
                  "module": "owned_slow_import", "entrypoint": "result", "path": code.name, "sha256": sha, "commit": commit},
                  "inputs": [], "outputs": [{"name": "result", "type": "str"}], "parameters": {}}],
              "edges": [], "resources": [], "external_dependencies": [],
              "environment": {"python": "3.12", "requirements": ["zenml==0.97.0"]}}
    backend = ZenMLBackend(tmp_path / "data", python_executable=str(PYTHON), source_root=source,
                           trusted_code_roots=[str(source)])
    request = {"project_id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "trusted": True,
               "idempotency_key": str(uuid.uuid4()), "recipe": recipe, "max_runtime_seconds": 90}
    backend.invoke("run.submit", request)
    deadline = time.monotonic() + 30
    while not (source / "import-started").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (source / "import-started").exists(), read_json(backend.root / "jobs" / request["operation_id"] / "state.json")
    return backend, request


def test_confirmed_cancel_before_native_run_creation(tmp_path):
    backend, request = early_request(tmp_path)
    before = backend.invoke("run.status", request)
    assert before["backend_run_id"] is None and before["worker_alive"] is True
    cancelled = backend.invoke("run.cancel", request)
    (ROOT / "evidence/backend/early-cancellation.json").write_text(json.dumps({"before": before, "after": cancelled}, indent=2))
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
    fixture_path = Path("src/modelbuilder/backend/fixtures.py")
    copied_fixture = source / fixture_path
    copied_fixture.parent.mkdir(parents=True)
    copied_fixture.write_bytes((ROOT / fixture_path).read_bytes())
    pause = source / "pause.py"
    pause.write_text('import signal\nimport time\nfrom pathlib import Path\n'
                     'def pause(checkpoint: Path) -> str:\n'
                     '    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                     '    Path(__file__).with_name("pause-started").write_text("ready")\n'
                     '    time.sleep(60)\n'
                     '    return str(checkpoint)\n')
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=Modelbuilder fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-qm", "Pin owned pending-cancel fixture"], cwd=source, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    recipe = json.loads((ROOT / "examples/custom-pytorch/recipe.json").read_text())
    recipe.pop("digest", None)
    recipe["source"] = {"repository": "https://example.invalid/owned-test.git", "commit": commit,
                        "files": [{"path": str(fixture_path), "sha256": hashlib.sha256(copied_fixture.read_bytes()).hexdigest()},
                                  {"path": "pause.py", "sha256": hashlib.sha256(pause.read_bytes()).hexdigest()}]}
    next(n for n in recipe["nodes"] if n["kind"] == "model.train")["parameters"]["steps"] = 3
    recipe["nodes"].append({"id": "pause", "kind": "code.python", "implementation_ref": {
        "module": "owned_checkpoint_pause", "entrypoint": "pause", "path": "pause.py", "commit": commit,
        "sha256": hashlib.sha256(pause.read_bytes()).hexdigest()}, "inputs": [{"name": "checkpoint", "type": "checkpoint"}],
        "outputs": [{"name": "result", "type": "str"}], "parameters": {}})
    recipe["edges"].append({"source_node": "train", "source_port": "checkpoint", "target_node": "pause", "target_port": "checkpoint"})
    backend = ZenMLBackend(tmp_path / "data", python_executable=str(PYTHON), source_root=source, trusted_code_roots=[str(source)])
    project_id = str(uuid.uuid4())
    recipe["project_id"] = project_id
    request = {"project_id": project_id, "operation_id": str(uuid.uuid4()), "trusted": True, "recipe": recipe,
               "idempotency_key": str(uuid.uuid4()), "max_runtime_seconds": 90}
    backend.invoke("run.submit", request)
    deadline = time.monotonic() + 45
    while not (source / "pause-started").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert (source / "pause-started").exists()
    before = backend.invoke("run.status", request)
    assert before["status"] == "running"
    assert any(a["port"] == "checkpoint" for a in before["artifacts"])
    continued = copy.deepcopy(recipe)
    continued["revision"] = recipe["revision"] + 1
    continued["nodes"] = [n for n in continued["nodes"] if n["id"] != "pause"]
    continued["edges"] = [e for e in continued["edges"] if e["target_node"] != "pause"]
    next(n for n in continued["nodes"] if n["kind"] == "model.train")["parameters"]["steps"] = 5
    resume = {**request, "parent_operation_id": request["operation_id"], "operation_id": str(uuid.uuid4()),
              "idempotency_key": str(uuid.uuid4()), "recipe": continued, "mode": "exact_state", "steps": 5}
    with ThreadPoolExecutor(max_workers=1) as executor:
        cancellation = executor.submit(backend.invoke, "run.cancel", request)
        cancel_file = backend.root / "jobs" / request["operation_id"] / "cancel.json"
        while not cancel_file.exists():
            time.sleep(0.02)
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
    (ROOT / "evidence/backend/pending-cancel-resume.json").write_text(json.dumps({
        "before": before, "rejection": {"code": rejection.value.code, "message": str(rejection.value)},
        "cancelled": cancelled, "resumed": child}, indent=2))
