"""Unpinned checkout siblings cannot shadow runtime startup before closure staging."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.common import read_json, write_json


def test_sdk_rpc_uses_safe_path_and_neutral_cwd_after_source_disappears(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    backend = ZenMLBackend(tmp_path / "state", python_executable=sys.executable, source_root=source)
    source.rmdir()
    def run(command, **kwargs):
        assert command[1:4] == ["-P", "-m", "modelbuilder.backend.sdk"]
        assert kwargs["cwd"] == backend.root
        assert kwargs["env"]["PYTHONSAFEPATH"] == "1"
        request = read_json(Path(command[-1]))
        write_json(Path(request["result"]), {"status": "succeeded"})
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("modelbuilder.backend.adapter.subprocess.run", run)
    assert backend._rpc("run.status", {"project_id": "project"})["status"] == "succeeded"


def test_supervisor_launch_inherits_safe_startup_before_user_import(tmp_path, monkeypatch):
    backend = ZenMLBackend(tmp_path / "state", python_executable=sys.executable, source_root=tmp_path)
    monkeypatch.setattr(backend, "_validate_execution", lambda payload: None)
    monkeypatch.setattr(backend, "_rpc", lambda operation, payload: {})
    calls = []
    def launch(command, **kwargs):
        assert command[1:4] == ["-P", "-m", "modelbuilder.backend.supervisor"]
        assert kwargs["cwd"] == backend.root
        assert kwargs["env"]["PYTHONSAFEPATH"] == "1"
        calls.append(command)
        # No worker or native workload is started by this contract test.
        write_json(Path(command[-2]) / "native.json", {"backend_run_id": "test-native"})
        return SimpleNamespace(pid=123)
    monkeypatch.setattr("modelbuilder.backend.adapter.subprocess.Popen", launch)
    result = backend.submit({"project_id": "project", "recipe": {"recipe_id": "recipe", "revision": 1, "environment": {}},
                             "operation_id": str(uuid.uuid4()), "idempotency_key": "test"})
    assert result["backend_run_id"] == "test-native" and len(calls) == 1


def test_descendant_environment_excludes_unpinned_checkout_imports(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "modelbuilder").mkdir()
    (source / "modelbuilder/__init__.py").write_text("raise RuntimeError('unpinned platform shadow imported')")
    (source / "json.py").write_text("raise RuntimeError('unpinned stdlib shadow imported')")
    backend = ZenMLBackend(tmp_path / "state", python_executable=sys.executable, source_root=source)
    # No explicit -P here: supervisor descendants inherit PYTHONSAFEPATH even
    # when they invoke Python -m/-c themselves from a checkout directory.
    result = subprocess.run([backend.python, "-c", "import importlib.util,json; print(json.dumps({'platform':importlib.util.find_spec('modelbuilder').origin,'json':json.__file__}))"],
                            cwd=source, env=backend._env(), text=True, capture_output=True, check=True)
    origins = json.loads(result.stdout)
    assert not Path(origins["platform"]).is_relative_to(source)
    assert not Path(origins["json"]).is_relative_to(source)
