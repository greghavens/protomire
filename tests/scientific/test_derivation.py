from copy import deepcopy
import hashlib
import json

import pytest

torch = pytest.importorskip("torch")
from safetensors.torch import load_file, save_file  # noqa: E402
from modelbuilder.backend.components import identity, read_checkpoint, component_catalog  # noqa: E402
from modelbuilder.backend.components.weight_blend import blend  # noqa: E402
from modelbuilder.backend.checkpoint_derivation import verify_output  # noqa: E402


def parent(root, value=1., *, dtype=torch.float32, buffer=1, architecture="tiny"):
    root.mkdir()
    save_file({"weight": torch.tensor([value, value + 1], dtype=dtype), "counter": torch.tensor(buffer)}, root / "weights.safetensors")
    manifest = {"schema_version": "modelbuilder.checkpoint/v1", "purpose": "best", "step": 2,
        "resume_modes": [], "state": ["model"], "identities": {"architecture": architecture, "source": "source", "dependencies": "deps", "training": "train", "environment": "env", "calibration": "old-fit"},
        "weight_layout": {"format": "safetensors-state-dict/v1", "weights": "weights.safetensors", "auxiliary_assets": [], "model_signature": {"task": "tiny"}},
        "assets": [{"path": "weights.safetensors", "sha256": hashlib.sha256((root / "weights.safetensors").read_bytes()).hexdigest()}]}
    (root / "checkpoint.manifest.json").write_text(json.dumps(manifest))
    return root


def ctx(tmp_path, inputs, parameters):
    entry = next(e for e in component_catalog() if e["id"] == "modelbuilder.checkpoint.blend")
    component = {key: value for key, value in entry.items() if key not in {"adapter", "inputs", "outputs", "component_digest"}}
    implementation = {"component_digest": identity(component), "implementation_ref": {"path": "adapter.py", "sha256": "a" * 64}}
    native = {"producer": {"project_id": "app-project", "backend_project_id": "native-project", "backend_run_id": "derived-run", "model_version_ref": "derived-version", "node_id": "blend", "invocation_id": "blend"},
        "parents": [{"input_port": port, "artifact_id": port, "backend_project_id": "native-project", "content_sha256": "b" * 64, "checkpoint_digest": identity(read_checkpoint(root))} for port, root in sorted(inputs.items())]}
    native["parent_rights_evidence"] = [{"artifact_id": p["artifact_id"], "checkpoint_digest": p["checkpoint_digest"], "rights": read_checkpoint(inputs[p["input_port"]]).get("rights")} for p in native["parents"]]
    return {"output_dir": str(tmp_path / "out"), "identities": {"source": "source", "dependencies": "deps", "environment": "env", "training": "not-training"}, "derivation": native, "derivation_implementation": implementation}, {"component": component, "implementation_ref": implementation["implementation_ref"], "parameters": parameters}


def test_real_blend_matches_torch_oracle_and_never_claims_training(tmp_path):
    inputs = {"left": parent(tmp_path / "a"), "right": parent(tmp_path / "b", 5.)}
    parameters = {"alpha": .25, "max_asset_bytes": 10000, "max_tensor_elements": 100}
    context, node = ctx(tmp_path, inputs, parameters)
    root = blend(inputs=inputs, parameters=parameters, context=context)["checkpoint"]
    ck = read_checkpoint(root)
    assert torch.equal(load_file(root / "weights.safetensors")["weight"], torch.lerp(torch.tensor([1., 2.]), torch.tensor([5., 6.]), .25))
    assert ck["state"] == ["model"] and ck["resume_modes"] == [] and "training" not in ck["identities"]
    assert ck["identities"]["calibration"] is None
    assert ck["derivation"]["calibration"] == {"status": "invalidated", "requires_refit": True}
    verify_output(ck, context["derivation"], node)
    assert {p["artifact_id"] for p in ck["derivation"]["parents"]} == {"left", "right"}


@pytest.mark.parametrize("change", ["architecture", "dtype", "buffer", "nan", "digest", "budget", "parent", "tensor_set", "shape"])
def test_blend_fails_closed(tmp_path, change):
    kwargs = {"architecture": "different"} if change == "architecture" else {"dtype": torch.float64} if change == "dtype" else {"buffer": 2} if change == "buffer" else {}
    inputs = {"left": parent(tmp_path / "a"), "right": parent(tmp_path / "b", float("nan") if change == "nan" else 5., **kwargs)}
    if change in {"tensor_set", "shape"}:
        root = inputs["right"]
        tensors = load_file(root / "weights.safetensors")
        if change == "tensor_set":
            tensors["extra"] = torch.ones(1)
        else:
            tensors["weight"] = torch.ones(3)
        save_file(tensors, root / "weights.safetensors")
        manifest = json.loads((root / "checkpoint.manifest.json").read_text())
        manifest["assets"][0]["sha256"] = hashlib.sha256((root / "weights.safetensors").read_bytes()).hexdigest()
        (root / "checkpoint.manifest.json").write_text(json.dumps(manifest))
    parameters = {"alpha": .25, "max_asset_bytes": 10000, "max_tensor_elements": 1 if change == "budget" else 100}
    context, node = ctx(tmp_path, inputs, parameters)
    if change == "digest":
        (inputs["right"] / "weights.safetensors").write_bytes(b"changed")
    if change == "parent":
        context["derivation"]["parents"][1]["checkpoint_digest"] = "0" * 64
    with pytest.raises(ValueError):
        blend(inputs=inputs, parameters=parameters, context=context)


@pytest.mark.parametrize("field", ["producer", "parents", "configuration", "implementation"])
def test_forged_native_derivation_rejected(tmp_path, field):
    inputs = {"left": parent(tmp_path / "a"), "right": parent(tmp_path / "b", 5.)}
    parameters = {"alpha": .25, "max_asset_bytes": 10000, "max_tensor_elements": 100}
    context, node = ctx(tmp_path, inputs, parameters)
    ck = read_checkpoint(blend(inputs=inputs, parameters=parameters, context=context)["checkpoint"])
    ck = deepcopy(ck)
    if field == "producer":
        ck["derivation"][field]["model_version_ref"] = "fake"
    elif field == "parents":
        ck["derivation"][field][0]["artifact_id"] = "fake"
    else:
        ck["derivation"][field]["fake"] = True
    ck["identities"]["derivation"] = identity(ck["derivation"])
    with pytest.raises(ValueError):
        verify_output(ck, context["derivation"], node)


def test_blend_identical_float64_subnormal_is_identity(tmp_path):
    inputs = {"left": parent(tmp_path / "a", 5e-324, dtype=torch.float64), "right": parent(tmp_path / "b", 5e-324, dtype=torch.float64)}
    parameters = {"alpha": .5, "max_asset_bytes": 10000, "max_tensor_elements": 100}
    context, node = ctx(tmp_path, inputs, parameters)
    output = blend(inputs=inputs, parameters=parameters, context=context)["checkpoint"]
    assert torch.equal(load_file(inputs["left"] / "weights.safetensors")["weight"], load_file(output / "weights.safetensors")["weight"])


def test_blend_right_endpoint_preserves_negative_zero(tmp_path):
    inputs = {"left": parent(tmp_path / "a", 0., dtype=torch.float64), "right": parent(tmp_path / "b", -0., dtype=torch.float64)}
    parameters = {"alpha": 1., "max_asset_bytes": 10000, "max_tensor_elements": 100}
    context, _ = ctx(tmp_path, inputs, parameters)
    output = blend(inputs=inputs, parameters=parameters, context=context)["checkpoint"]
    actual = load_file(output / "weights.safetensors")["weight"]
    assert torch.equal(torch.signbit(actual), torch.signbit(load_file(inputs["right"] / "weights.safetensors")["weight"]))
    assert torch.signbit(actual[0])
