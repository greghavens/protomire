import time
from pathlib import Path
import pytest
from modelbuilder.backend.execution_limits import active_node, bounded_callable, node_timeout


def test_completed_node_clears_deadline(tmp_path):
    with active_node(tmp_path, 'test', 10):
        assert len(list((tmp_path/'active-nodes').glob('*.json'))) == 1
        assert node_timeout(tmp_path) is None
    assert not list((tmp_path/'active-nodes').glob('*.json'))


def test_expired_node_cannot_be_erased_by_next_stage(tmp_path):
    with active_node(tmp_path, 'old', .001):
        time.sleep(.01)
    assert node_timeout(tmp_path)['node_id'] == 'old'
    with pytest.raises(TimeoutError):
        with active_node(tmp_path, 'new', 10):
            pass
    assert node_timeout(tmp_path)['node_id'] == 'old'


def test_typed_signature_is_preserved(tmp_path):
    import inspect
    def fn(value: int) -> int:
        return value * 2
    bound = bounded_callable(fn, Path(tmp_path), {'id':'double', 'timeout':2})
    assert inspect.signature(bound) == inspect.signature(fn)
    assert bound(2) == 4


def test_deadline_hook_resolves_without_package_context(tmp_path, monkeypatch):
    """ZenML may execute hooks as source modules, without a Python package."""
    import json
    import sys
    from types import FunctionType, SimpleNamespace
    from modelbuilder.backend import execution_limits
    context = SimpleNamespace(step_name="prompts", step_run=SimpleNamespace(), model_version=None,
                              pipeline_run=SimpleNamespace(id="run", project=SimpleNamespace(id="project")))
    monkeypatch.setitem(sys.modules, "zenml", SimpleNamespace(get_step_context=lambda: context))
    monkeypatch.setenv("MODELBUILDER_JOB_DIR", str(tmp_path))
    (tmp_path / "request.json").write_text(json.dumps({"recipe": {"nodes": [{"id": "prompts", "timeout": 300}]}}))
    hook = FunctionType(execution_limits.deadline_start.__code__,
                        {**execution_limits.deadline_start.__globals__, "__package__": None})
    hook()
    deadline = json.loads(next((tmp_path / "active-nodes").glob("*.json")).read_text())
    assert deadline["node_id"] == deadline["canonical_node_id"] == deadline["invocation_id"] == "prompts"
    assert deadline["timeout_seconds"] == 300


def test_deadline_hook_reports_original_error_and_exits_closed(monkeypatch):
    import os
    import sys
    from types import SimpleNamespace
    from modelbuilder.backend.execution_limits import deadline_start
    monkeypatch.setitem(sys.modules, "zenml", SimpleNamespace(get_step_context=lambda: None))
    monkeypatch.delenv("MODELBUILDER_JOB_DIR", raising=False)
    writes = []
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append((fd, data)))
    def exit_process(code):
        raise SystemExit(code)
    monkeypatch.setattr(os, "_exit", exit_process)
    with pytest.raises(SystemExit) as error:
        deadline_start()
    assert error.value.code == 70
    assert len(writes) == 1 and writes[0][0] == 2
    assert b"KeyError" in writes[0][1] and b"MODELBUILDER_JOB_DIR" in writes[0][1]
