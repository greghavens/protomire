"""Source-closure eviction retains only the platform worker's process identity."""
import hashlib
from pathlib import Path
import sys
from types import ModuleType

from modelbuilder.backend.components import runtime


def test_source_loader_preserves_platform_main_but_evicts_checkout_helpers(tmp_path, monkeypatch):
    code = tmp_path / "harness.py"
    code.write_text("def calculate():\n    return 7\n")
    digest = hashlib.sha256(code.read_bytes()).hexdigest()
    fake_runtime = tmp_path / "src/modelbuilder/backend/components/runtime.py"
    platform_main = ModuleType("__main__")
    platform_main.__file__ = str(fake_runtime.parents[1] / "worker.py")
    unrelated = ModuleType("unrelated_source_helper")
    unrelated.__file__ = str(tmp_path / "helper.py")
    monkeypatch.setattr(runtime, "__file__", str(fake_runtime))
    monkeypatch.setitem(sys.modules, "__main__", platform_main)
    monkeypatch.setitem(sys.modules, "unrelated_source_helper", unrelated)
    monkeypatch.setattr(sys, "path", sys.path[:])
    before = dict(sys.modules)
    try:
        loaded = runtime.load_callable({"path": "harness.py", "sha256": digest, "module": "closure_probe", "entrypoint": "calculate"},
                                       str(tmp_path), [str(tmp_path)], [{"path": "harness.py", "sha256": digest}])
        assert loaded() == 7
        assert sys.modules["__main__"] is platform_main
        assert "unrelated_source_helper" not in sys.modules
        assert Path(sys.modules["__main__"].__file__).parent == fake_runtime.parents[1]
    finally:
        for key, module in before.items():
            if key not in sys.modules:
                sys.modules[key] = module
        sys.modules.pop("closure_probe", None)
