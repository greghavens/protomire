"""Artifact parity and checkpoint evidence never accept non-finite JSON."""
import hashlib
import json

import pytest

from modelbuilder.backend.components import read_checkpoint, read_export


def export_descriptor(tmp_path):
    asset = tmp_path / "loader.py"
    asset.write_text("# Source-pinned artifact loader\n")
    return {"schema_version": "modelbuilder.export/v1", "selected_checkpoint": "checkpoint",
            "loader": {"path": asset.name}, "signature": {"inputs": ["x"]},
            "dependencies": {"python": "3.12"}, "source": {"commit": "pinned"},
            "verification": {"passed": True, "atol": 0.0},
            "assets": [{"path": asset.name, "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()}]}


@pytest.mark.parametrize("tolerance", [True, False, float("nan"), float("inf"), -float("inf"), -0.1, 10 ** 1000])
def test_export_rejects_invalid_numeric_tolerance(tmp_path, tolerance):
    manifest = export_descriptor(tmp_path)
    manifest["verification"]["atol"] = tolerance
    (tmp_path / "export.manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        read_export(tmp_path)


@pytest.mark.parametrize("tolerance", [0, 0.0, 1e-6])
def test_export_accepts_finite_nonnegative_numeric_tolerance(tmp_path, tolerance):
    manifest = export_descriptor(tmp_path)
    manifest["verification"]["atol"] = tolerance
    (tmp_path / "export.manifest.json").write_text(json.dumps(manifest))
    assert read_export(tmp_path)["verification"]["atol"] == tolerance


@pytest.mark.parametrize("reader,filename", [(read_checkpoint, "checkpoint.manifest.json"),
                                             (read_export, "export.manifest.json")])
@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_artifact_descriptor_rejects_nonfinite_json_anywhere(tmp_path, reader, filename, invalid):
    (tmp_path / filename).write_text('{"arbitrary_nested_metadata": {"value": ' + invalid + '}}')
    with pytest.raises(ValueError, match="finite numbers"):
        reader(tmp_path)
