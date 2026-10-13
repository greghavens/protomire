"""Conservative CPU safetensors blending; never deserialize pickle or train.

The small contract deliberately refuses adapters, quantized weights, mismatched
nonfloating buffers, and unknown model layouts. A different reviewed component
can implement those methods using the same native derivation contract.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

from jsonschema import Draft202012Validator

from . import PROTOCOL, identity, read_checkpoint

PARAMETERS = {"type": "object", "additionalProperties": False,
    "properties": {"alpha": {"type": "number", "minimum": 0, "maximum": 1},
                   "max_asset_bytes": {"type": "integer", "minimum": 1},
                   "max_tensor_elements": {"type": "integer", "minimum": 1}},
    "required": ["alpha", "max_asset_bytes", "max_tensor_elements"]}
LAYOUT = {"type": "object", "additionalProperties": False,
    "properties": {"format": {"const": "safetensors-state-dict/v1"},
                   "weights": {"type": "string", "minLength": 1},
                   "auxiliary_assets": {"type": "array", "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
                   "model_signature": {"type": "object", "minProperties": 1}},
    "required": ["format", "weights", "auxiliary_assets", "model_signature"]}


def catalog_entries():
    descriptor = {"id": "modelbuilder.checkpoint.blend", "version": "1", "protocol": PROTOCOL,
        "role": "derivation", "description": "CPU convex weight blend of two compatible native safetensors checkpoints; calibration invalidated, no optimizer continuity.",
        "parameter_schema": deepcopy(PARAMETERS), "capabilities": {"devices": ["cpu"], "resume_modes": [],
            "export_formats": [], "frameworks": ["pytorch", "safetensors"]}}
    return [{**descriptor, "component_digest": identity(descriptor),
        "inputs": [{"name": name, "type": "Path"} for name in ("left", "right")],
        "outputs": [{"name": "checkpoint", "type": "Path"}],
        "adapter": {"module": __name__, "entrypoint": "blend", "source_binding": "Pin a wrapper in the trusted project source closure.",
                    "example_source": "examples/scientific_workflows/adapter.py"}}]


def blend(*, inputs, parameters, context):
    import torch
    from safetensors import safe_open
    from safetensors.torch import save_file
    Draft202012Validator(PARAMETERS).validate(parameters)
    json.dumps(parameters, allow_nan=False)
    if set(inputs) != {"left", "right"}:
        raise ValueError("Weight blend requires exactly left and right checkpoints")
    roots = {name: Path(value) for name, value in inputs.items()}
    manifests = {name: read_checkpoint(root) for name, root in roots.items()}
    layouts, assets = {}, {}
    for name, manifest in manifests.items():
        layout = manifest.get("weight_layout")
        Draft202012Validator(LAYOUT).validate(layout)
        asset_map = {asset["path"]: asset["sha256"] for asset in manifest["assets"]}
        declared = [layout["weights"], *layout["auxiliary_assets"]]
        if len(set(declared)) != len(declared) or not set(declared) <= asset_map.keys():
            raise ValueError("Weight layout references missing or duplicate declared assets")
        if any("calibration" in Path(path).name.lower() for path in layout["auxiliary_assets"]):
            raise ValueError("Calibration cannot be copied as a weight-blend auxiliary asset")
        if sum((roots[name] / path).stat().st_size for path in declared) > parameters["max_asset_bytes"]:
            raise ValueError("Checkpoint exceeds declared asset budget")
        layouts[name], assets[name] = layout, asset_map
    a, b = manifests["left"], manifests["right"]
    if a["identities"]["architecture"] != b["identities"]["architecture"]:
        raise ValueError("Checkpoint architecture identities differ")
    if layouts["left"] != layouts["right"]:
        raise ValueError("Checkpoint weight layouts, model signatures or auxiliary asset sets differ")
    layout = layouts["left"]
    if any(assets["left"][path] != assets["right"][path] for path in layout["auxiliary_assets"]):
        raise ValueError("Checkpoint tokenizer/configuration auxiliary assets differ")
    lineage = context["derivation"]
    if {p["input_port"] for p in lineage["parents"]} != set(inputs):
        raise ValueError("Blend parents must be actual distinct native checkpoint inputs")
    for parent in lineage["parents"]:
        if parent["checkpoint_digest"] != identity(manifests[parent["input_port"]]):
            raise ValueError("Native parent checkpoint digest changed")
    tensors, elements = {}, 0
    with safe_open(roots["left"] / layout["weights"], framework="pt", device="cpu") as left, safe_open(roots["right"] / layout["weights"], framework="pt", device="cpu") as right:
        if not left.keys() or set(left.keys()) != set(right.keys()):
            raise ValueError("Checkpoint tensor sets differ or are empty")
        for key in sorted(left.keys()):
            x, y = left.get_slice(key), right.get_slice(key)
            if x.get_shape() != y.get_shape() or x.get_dtype() != y.get_dtype():
                raise ValueError("Checkpoint tensor shapes/dtypes differ: " + key)
            import math
            elements += math.prod(x.get_shape())
            if elements > parameters["max_tensor_elements"]:
                raise ValueError("Checkpoint exceeds declared tensor element budget")
        for key in sorted(left.keys()):
            x, y = left.get_tensor(key), right.get_tensor(key)
            if x.dtype not in {torch.float16, torch.bfloat16, torch.float32, torch.float64}:
                if x.is_floating_point() or x.is_complex() or not torch.equal(x, y):
                    raise ValueError("Unsupported tensor dtype or unequal nonfloating buffer: " + key)
                tensors[key] = x.clone().contiguous()
                continue
            if not torch.isfinite(x).all() or not torch.isfinite(y).all():
                raise ValueError("Nonfinite parent tensor: " + key)
            alpha = parameters["alpha"]
            result = x.clone() if alpha == 0 else y.clone() if alpha == 1 else ((1 - alpha) * x.double() + alpha * y.double()).to(x.dtype)
            # Identical finite entries must remain bit-preserving even when
            # separate weighted products would underflow in float64.
            if 0 < alpha < 1:
                result = torch.where(x == y, x, result)
            if not torch.isfinite(result).all():
                raise ValueError("Nonfinite blended tensor: " + key)
            tensors[key] = result.contiguous()
    output = Path(context["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    weight_path = output / layout["weights"]
    weight_path.parent.mkdir(parents=True, exist_ok=True)
    save_file(tensors, weight_path)
    for path in layout["auxiliary_assets"]:
        (output / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(roots["left"] / path, output / path)
    output_assets = [{"path": path, "sha256": hashlib.sha256((output / path).read_bytes()).hexdigest()}
                     for path in [layout["weights"], *layout["auxiliary_assets"]]]
    derivation = {"schema": "modelbuilder.checkpoint-derivation/v1", "operation": "convex_weight_blend/v1",
        "implementation": context["derivation_implementation"], "configuration": deepcopy(parameters),
        "parents": deepcopy(lineage["parents"]), "producer": deepcopy(lineage["producer"]),
        "parent_rights_evidence": deepcopy(lineage["parent_rights_evidence"]),
        "calibration": {"status": "invalidated", "requires_refit": True}}
    identities = {key: value for key, value in context["identities"].items() if key not in {"training", "calibration"}}
    identities.update(architecture=a["identities"]["architecture"], derivation=identity(derivation), calibration=None)
    from ...artifact_rights import combine_parent_rights
    result = {"rights": combine_parent_rights(derivation["parent_rights_evidence"]), "schema_version": "modelbuilder.checkpoint/v2", "producer_kind": "derivation", "purpose": "inference",
        "step": 0, "resume_modes": [], "state": ["model"], "identities": identities,
        "derivation": derivation, "weight_layout": deepcopy(layout), "assets": output_assets}
    (output / "checkpoint.manifest.json").write_text(json.dumps(result, sort_keys=True, allow_nan=False))
    read_checkpoint(output)
    return {"checkpoint": output}
