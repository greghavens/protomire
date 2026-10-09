import hashlib
import json

import pytest
from modelbuilder.backend.components import validate_component, read_checkpoint, validate_resume
from modelbuilder.backend.components.runtime import load_callable


def component():
    return {"id": "user.model", "version": "1", "protocol": "modelbuilder.component/v1", "role": "training",
            "parameter_schema": {"type": "object", "properties": {"width": {"type": "integer", "minimum": 1}}, "required": ["width"], "additionalProperties": False},
            "capabilities": {"resume_modes": ["warm_start"], "export_formats": [], "frameworks": ["pytorch"]}}


def test_schema_inert_and_capabilities():
    node = {"component": component(), "parameters": {"width": 2}, "implementation_ref": {}, "outputs": [{"name": "weights", "type": "Path"}]}
    assert validate_component(node)["capabilities"]["resume_modes"] == ["warm_start"]
    with pytest.raises(Exception):
        validate_component({**node, "parameters": {"width": 0}})
    node["outputs"][0]["name"] = "class"
    with pytest.raises(ValueError):
        validate_component(node)


def test_untrusted_and_tampered_closure_never_import(tmp_path):
    marker = tmp_path / "imported"
    source = tmp_path / "adapter.py"
    source.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef run(**kwargs): return {{}}\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    ref = {"path": "adapter.py", "sha256": digest, "module": "external_adapter", "entrypoint": "run"}
    files = [{"path": "adapter.py", "sha256": digest}]
    with pytest.raises(ValueError, match="trusted"):
        load_callable(ref, str(tmp_path), [], files)
    assert not marker.exists()
    sibling = tmp_path / "model.py"
    sibling.write_text("a = 1")
    files.append({"path": "model.py", "sha256": "0" * 64})
    with pytest.raises(ValueError, match="closure changed"):
        load_callable(ref, str(tmp_path), [str(tmp_path)], files)
    assert not marker.exists()
    files.pop()
    assert callable(load_callable(ref, str(tmp_path), [str(tmp_path)], files))
    assert marker.exists()


def checkpoint(tmp_path):
    asset = tmp_path / "arbitrary.weights"
    asset.write_bytes(b"weights")
    descriptor = {"schema_version": "modelbuilder.checkpoint/v1", "purpose": "last", "step": 3,
                  "assets": [{"path": asset.name, "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()}],
                  "resume_modes": ["warm_start"], "identities": {k: k for k in ("architecture", "source", "dependencies", "training", "environment")}}
    (tmp_path / "checkpoint.manifest.json").write_text(json.dumps(descriptor))
    return descriptor


def test_weights_only_checkpoint_refuses_exact_but_allows_warm(tmp_path):
    descriptor = checkpoint(tmp_path)
    validate_resume(tmp_path, "warm_start", descriptor["identities"], {"resume_modes": ["warm_start"]})
    with pytest.raises(ValueError, match="not supported"):
        validate_resume(tmp_path, "exact_state", descriptor["identities"], {"resume_modes": ["exact_state"]})
    (tmp_path / "arbitrary.weights").write_bytes(b"tamper")
    with pytest.raises(ValueError, match="integrity"):
        read_checkpoint(tmp_path)


def test_exact_resume_requires_all_state_and_same_contract(tmp_path):
    descriptor = checkpoint(tmp_path)
    descriptor.update(resume_modes=["exact_state"], state=["model", "optimizer", "scheduler", "rng", "data_cursor"])
    (tmp_path / "checkpoint.manifest.json").write_text(json.dumps(descriptor))
    validate_resume(tmp_path, "exact_state", descriptor["identities"], {"resume_modes": ["exact_state"]})
    for field in ("architecture", "training", "environment", "calibration"):
        with pytest.raises(ValueError, match="identity mismatch"):
            validate_resume(tmp_path, "exact_state", {**descriptor["identities"], field: "changed"}, {"resume_modes": ["exact_state"]})


def test_remote_schema_refs_and_symlink_assets_rejected(tmp_path):
    node = {"component": component(), "parameters": {"width": 2}, "implementation_ref": {}, "outputs": [{"name": "weights", "type": "Path"}]}
    node["component"]["parameter_schema"] = {"$ref": "https://example.invalid/schema"}
    with pytest.raises(ValueError):
        validate_component(node)
    descriptor = checkpoint(tmp_path)
    (tmp_path / "link.weights").symlink_to(tmp_path / "arbitrary.weights")
    descriptor["assets"][0]["path"] = "link.weights"
    (tmp_path / "checkpoint.manifest.json").write_text(json.dumps(descriptor))
    with pytest.raises(ValueError, match="escapes"):
        read_checkpoint(tmp_path)


def test_file_input_content_identity_survives_move_and_detects_change(tmp_path):
    import shutil
    from modelbuilder.backend.components.runtime import input_identity
    original = tmp_path / "original"
    original.mkdir()
    (original / "rows.json").write_text('[1,2,3]')
    moved = tmp_path / "moved"
    shutil.copytree(original, moved)
    assert input_identity(original) == input_identity(moved)
    (moved / "rows.json").write_text('[1,2,4]')
    assert input_identity(original) != input_identity(moved)


def test_undeclared_sibling_is_not_importable_from_snapshot(tmp_path, monkeypatch):
    import sys
    source = tmp_path / "adapter.py"
    source.write_text("def run(**kwargs):\n    import undeclared_sibling\n    return undeclared_sibling.VALUE\n")
    (tmp_path / "undeclared_sibling.py").write_text("VALUE = 42\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    reference = {"path": "adapter.py", "sha256": digest, "module": "snapshot_adapter", "entrypoint": "run"}
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    function = load_callable(reference, str(tmp_path), [str(tmp_path)], [{"path": "adapter.py", "sha256": digest}])
    with pytest.raises(ModuleNotFoundError, match="undeclared_sibling"):
        function()
    sibling_hash = hashlib.sha256((tmp_path / "undeclared_sibling.py").read_bytes()).hexdigest()
    function = load_callable(reference, str(tmp_path), [str(tmp_path)], [{"path": "adapter.py", "sha256": digest}, {"path": "undeclared_sibling.py", "sha256": sibling_hash}])
    assert function() == 42
    assert str(tmp_path) not in sys.path
    restricted = load_callable(reference, str(tmp_path), [str(tmp_path)], [{"path": "adapter.py", "sha256": digest}])
    with pytest.raises(ModuleNotFoundError, match="undeclared_sibling"):
        restricted()
