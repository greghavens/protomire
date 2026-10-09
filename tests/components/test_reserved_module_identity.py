"""Recipe module labels cannot overwrite process identity and break native hooks."""
import hashlib
import sys

import pytest
from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.components.runtime import load_callable


@pytest.mark.parametrize("module_name", ["__main__", "__mp_main__"])
def test_reserved_process_modules_are_rejected_at_plan_and_import(tmp_path, module_name):
    source = tmp_path / "source"
    source.mkdir()
    marker = tmp_path / "imported"
    code = source / "harness.py"
    code.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef calculate() -> int:\n    return 7\n")
    digest = hashlib.sha256(code.read_bytes()).hexdigest()
    reference = {"module": module_name, "entrypoint": "calculate", "path": "harness.py", "sha256": digest, "commit": "1" * 40}
    files = [{"path": "harness.py", "sha256": digest}]
    original_main = sys.modules.get("__main__")
    with pytest.raises(ValueError, match="reserved process module"):
        load_callable(reference, str(source), [str(source)], files)
    assert not marker.exists()
    assert sys.modules.get("__main__") is original_main
    backend = ZenMLBackend(tmp_path / "state", python_executable=sys.executable, source_root=source, trusted_code_roots=[str(source)])
    recipe = {"source": {"commit": reference["commit"], "files": files}, "environment": {"python": "3.12", "requirements": []},
              "nodes": [{"id": "model", "kind": "code.python", "implementation_ref": reference, "inputs": [],
                         "outputs": [{"name": "result", "type": "int"}], "parameters": {}, "timeout": 10}]}
    plan = backend.invoke("run.plan", {"project_id": "project", "trusted": True, "recipe": recipe})
    assert not plan["executable"]
    assert plan["blocked_reasons"][0]["code"] == "validation_error"
    assert "reserved process module" in plan["blocked_reasons"][0]["message"]
    assert not marker.exists()
