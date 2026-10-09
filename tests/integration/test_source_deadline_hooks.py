"""Real native hooks survive a model source checkout that also contains the platform runtime."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import pytest
import modelbuilder
from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.common import read_json

ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(os.environ.get("MODELBUILDER_BACKEND_PYTHON", str(ROOT / ".venv-backend/bin/python")))
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not PYTHON.is_file(), reason="Pinned backend required")]


def test_platform_main_survives_source_loading_for_end_and_next_start_hooks(tmp_path):
    source = tmp_path / "owned-source-and-runtime"
    (source / "src").mkdir(parents=True)
    shutil.copytree(Path(modelbuilder.__file__).parent, source / "src/modelbuilder", ignore=shutil.ignore_patterns("__pycache__"))
    code = source / "harness.py"
    code.write_text("import time\ndef first() -> int:\n    return 1\ndef second(value: int, delay: float) -> int:\n    time.sleep(delay)\n    return value + 1\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "harness.py"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Synthetic deadline harness"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    digest = hashlib.sha256(code.read_bytes()).hexdigest()
    backend = ZenMLBackend(tmp_path / "state", python_executable=str(PYTHON), source_root=source, trusted_code_roots=[str(source)])
    # Reproduce the supported editable-install case: runtime and model source
    # share a containing checkout, while only the model harness is recipe source.
    backend.package_root = source / "src"
    def node(name, timeout, delay):
        return {"id": name, "kind": "code.python", "implementation_ref": {"module": "deadline_harness", "entrypoint": name,
            "path": "harness.py", "sha256": digest, "commit": commit}, "inputs": [{"name": "value", "type": "int"}] if name == "second" else [],
            "outputs": [{"name": "value", "type": "int"}], "parameters": {"delay": delay} if name == "second" else {},
            "environment_ref": "default", "resource_request": {"gpu": 0}, "cache_policy": "disabled", "timeout": timeout,
            "retry": {"max_attempts": 1, "backoff_seconds": 0}, "provenance": {}}
    for slow in (False, True):
        project = str(uuid.uuid4())
        body = {"schema_version": "0", "recipe_id": str(uuid.uuid4()), "project_id": project, "revision": 1, "name": "Deadline hooks",
            "source": {"repository": "local-test", "commit": commit, "files": [{"path": "harness.py", "sha256": digest}]},
            "nodes": [node("first", 60, 0), node("second", .15 if slow else 60, 5 if slow else 0)],
            "edges": [{"source_node": "first", "source_port": "value", "target_node": "second", "target_port": "value"}],
            "resources": [], "external_dependencies": [], "environment": {"python": "3.12", "requirements": ["zenml==0.97.0"]}, "evaluation": {}, "outputs": []}
        request = {"project_id": project, "recipe": body, "trusted": True, "operation_id": str(uuid.uuid4()),
                   "idempotency_key": str(uuid.uuid4()), "max_runtime_seconds": 90}
        backend.invoke("run.submit", request)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = backend.invoke("run.status", request)
            if result["status"] in {"succeeded", "cancelled", "failed"}:
                break
            time.sleep(.25)
        else:
            raise AssertionError(result)
        job = backend.root / "jobs" / request["operation_id"]
        log = (job / "worker.log").read_text()
        assert "main module could not be found" not in log
        assert "hook from source" not in log
        if slow:
            assert result["status"] == "cancelled", result
            cancellation = read_json(job / "cancel.json")
            assert cancellation["reason"] == "node_timeout_exceeded" and cancellation["node_id"] == "second"
            assert result["worker_alive"] is False and result["process_group_alive"] is False
        else:
            assert result["status"] == "succeeded", result
            assert not (job / "active-node.json").exists()
