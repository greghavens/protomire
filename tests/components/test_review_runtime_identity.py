"""Editable runtime source identity is stable across installation locations."""
import shutil

from modelbuilder.backend.components import runtime


def test_runtime_source_identity_is_location_independent_and_content_sensitive(tmp_path):
    original = tmp_path / "original"
    (original / "backend" / "components").mkdir(parents=True)
    (original / "backend" / "worker.py").write_text("WORKER = 1\n")
    (original / "backend" / "components" / "runtime.py").write_text("RUNTIME = 1\n")
    (original / "dataset.py").write_text("DATASET = 1\n")
    (original / "__init__.py").write_text("")
    moved = tmp_path / "relocated-installation"
    shutil.copytree(original, moved)
    first = runtime.runtime_source_identity(original)
    assert first == runtime.runtime_source_identity(moved)
    (moved / "dataset.py").write_text("DATASET = 2\n")
    assert first != runtime.runtime_source_identity(moved)
    (moved / "dataset.py").write_text("DATASET = 1\n")
    (moved / "backend" / "worker.py").write_text("WORKER = 2\n")
    assert first != runtime.runtime_source_identity(moved)


def test_runtime_environment_includes_platform_source_identity(monkeypatch):
    monkeypatch.setattr(runtime, "runtime_source_identity", lambda: "reviewed-runtime-digest")
    assert runtime.runtime_environment()["modelbuilder_runtime_source"] == "reviewed-runtime-digest"
