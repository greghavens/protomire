"""Inert, versioned component contracts. Importing this module executes no user code.

Components adapt existing framework or user-owned harnesses; Modelbuilder never
owns their optimization loop. JSON-compatible values and files remain ordinary
ZenML artifacts. Trusted source is loaded only in the backend worker.
"""
from __future__ import annotations

import hashlib
import json
import keyword
import math
import re
from pathlib import Path

from jsonschema import Draft202012Validator

PROTOCOL = "modelbuilder.component/v1"
PORT_TYPES = {"dict": dict, "Path": Path, "str": str, "int": int, "float": float, "bool": bool,
              "DatasetArtifact": dict, "CheckpointArtifact": dict, "EvaluationArtifact": dict, "ExportArtifact": dict}
COMPONENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "version", "protocol", "role", "parameter_schema", "capabilities"],
    "properties": {
        "id": {"type": "string", "minLength": 1}, "version": {"type": "string", "minLength": 1},
        "protocol": {"const": PROTOCOL}, "role": {"enum": ["training", "evaluation", "export", "dataset", "model", "utility"]},
        "description": {"type": "string"}, "parameter_schema": {"type": "object"},
        "capabilities": {"type": "object", "additionalProperties": False,
            "required": ["resume_modes", "export_formats", "frameworks"], "properties": {
                "checkpoint_events": {"type": "boolean"},
                "resume_modes": {"type": "array", "uniqueItems": True, "items": {"enum": ["exact_state", "warm_start"]}},
                "export_formats": {"type": "array", "items": {"type": "string"}},
                "frameworks": {"type": "array", "items": {"type": "string"}}}}
    }
}


def identity(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_component(node: dict) -> dict:
    descriptor = node.get("component", {})
    pending, count = [(descriptor, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 32 or count > 10000:
            raise ValueError("Component descriptor exceeds bounded schema complexity")
        if isinstance(item, dict):
            pending.extend((value, depth + 1) for value in item.values())
        elif isinstance(item, list):
            pending.extend((value, depth + 1) for value in item)
        elif isinstance(item, str) and len(item) > 65536:
            raise ValueError("Component descriptor string exceeds size limit")
    Draft202012Validator(COMPONENT_SCHEMA).validate(descriptor)
    from ...recipe import _local_schema
    _local_schema(descriptor["parameter_schema"], "component.parameter_schema")
    Draft202012Validator.check_schema(descriptor["parameter_schema"])
    Draft202012Validator(descriptor["parameter_schema"]).validate(node.get("parameters", {}))
    if node.get("parameter_schema") not in (None, descriptor["parameter_schema"]):
        raise ValueError("Component and node parameter schemas disagree")
    if not isinstance(node.get("implementation_ref"), dict):
        raise ValueError("Components require a source-pinned Python adapter")
    for port in node.get("inputs", []) + node.get("outputs", []):
        if port["type"] not in PORT_TYPES:
            raise ValueError(f"Unsupported component artifact type: {port['type']}")
        if not port["name"].isidentifier() or keyword.iskeyword(port["name"]) or port["name"] in {"spec", "inputs", "parameters", "context"}:
            raise ValueError("Component port names must be Python identifiers and not reserved ABI names")
    if not node.get("outputs"):
        raise ValueError("Components must declare at least one output")
    return descriptor


def component_catalog(recipe: dict | None = None) -> list[dict]:
    """Discover inert reusable adapters and source-bound recipe declarations."""
    from .local_inference import catalog_entries
    catalog = [{"id": "modelbuilder.examples", "version": "1", "protocol": "modelbuilder.fixture/v1",
                "description": "Versioned custom classifier and tiny BERT examples; not a generic training framework.",
                "capabilities": {"resume_modes": ["exact_state", "warm_start"], "export_formats": ["pytorch"], "frameworks": ["pytorch", "transformers"]}}]
    catalog.extend(catalog_entries())
    for node in (recipe or {}).get("nodes", []):
        if node.get("kind") == "component.run":
            descriptor = validate_component(node)
            catalog.append({**descriptor, "node_id": node["id"], "implementation_ref": node["implementation_ref"],
                            "inputs": node["inputs"], "outputs": node["outputs"], "component_digest": identity(descriptor)})
    return catalog


def verify_assets(root: Path, assets: list[dict]) -> None:
    root = root.resolve()
    if not isinstance(assets, list) or not assets or len(assets) > 10000:
        raise ValueError("Artifact must declare between one and 10000 assets")
    names = set()
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("path"), str) or not isinstance(asset.get("sha256"), str):
            raise ValueError("Artifact asset requires a relative path and SHA256")
        relative = Path(asset["path"])
        if (relative.is_absolute() or ".." in relative.parts or asset["path"] in names
                or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])):
            raise ValueError("Artifact asset path or SHA256 is invalid")
        names.add(asset["path"])
        raw = root / relative
        path = raw.resolve()
        if (not path.is_relative_to(root) or not path.is_file()
                or any(part.is_symlink() for part in [raw, *raw.parents] if part != root and part.is_relative_to(root))):
            raise ValueError("Artifact asset is missing or escapes artifact directory")
        with path.open("rb") as stream:
            actual_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual_digest != asset["sha256"]:
            raise ValueError("Artifact asset integrity mismatch: " + asset["path"])


def _reject_nonfinite_json(value: str):
    raise ValueError("Artifact descriptor JSON must contain only finite numbers")


def _finite_json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        _reject_nonfinite_json(value)
    return result


def _read_descriptor(path: Path) -> dict:
    descriptor = json.loads(path.read_text(), parse_constant=_reject_nonfinite_json,
                            parse_float=_finite_json_float)
    if not isinstance(descriptor, dict):
        raise ValueError("Artifact descriptor must be a JSON object")
    return descriptor


def read_checkpoint(root: Path) -> dict:
    """Validate explicit harness checkpoint metadata, never infer weight filenames."""
    manifest_path = root / "checkpoint.manifest.json"
    if manifest_path.stat().st_size > 4 * 1024 * 1024 or manifest_path.is_symlink():
        raise ValueError("Checkpoint manifest is oversized or symlinked")
    descriptor = _read_descriptor(manifest_path)
    if descriptor.get("schema_version") != "modelbuilder.checkpoint/v1":
        raise ValueError("Unsupported checkpoint descriptor")
    for field in ("identities", "resume_modes", "assets", "step", "purpose"):
        if field not in descriptor:
            raise ValueError("Checkpoint descriptor missing " + field)
    for field in ("architecture", "source", "dependencies", "training", "environment"):
        if not descriptor["identities"].get(field):
            raise ValueError("Checkpoint identity missing " + field)
    if type(descriptor["step"]) is not int or descriptor["step"] < 0:
        raise ValueError("Checkpoint step must be a nonnegative integer")
    if descriptor["purpose"] not in {"last", "best", "selected", "resumable", "inference"}:
        raise ValueError("Checkpoint purpose must be explicitly declared")
    if not isinstance(descriptor["resume_modes"], list):
        raise ValueError("Checkpoint resume modes must be a list")
    if not set(descriptor["resume_modes"]).issubset({"exact_state", "warm_start"}):
        raise ValueError("Unknown checkpoint resume mode")
    if "exact_state" in descriptor["resume_modes"]:
        required = {"model", "optimizer", "scheduler", "rng", "data_cursor"}
        if not required.issubset(set(descriptor.get("state", []))):
            raise ValueError("Exact resume requires declared optimizer, scheduler, RNG and data cursor state")
    verify_assets(root, descriptor["assets"])
    return descriptor


def validate_resume(root: Path, mode: str, expected: dict, capabilities: dict) -> dict:
    descriptor = read_checkpoint(root)
    if mode not in capabilities["resume_modes"] or mode not in descriptor["resume_modes"]:
        raise ValueError("Requested resume mode is not supported by component and checkpoint")
    keys = ("architecture", "source", "dependencies", "training", "environment", "data", "calibration", "reference") if mode == "exact_state" else ("architecture",)
    mismatched = [key for key in keys if descriptor["identities"].get(key) != expected.get(key)]
    if mismatched:
        raise ValueError("Checkpoint resume identity mismatch: " + ", ".join(mismatched))
    return descriptor


def read_export(root: Path) -> dict:
    manifest_path = root / "export.manifest.json"
    if manifest_path.stat().st_size > 4 * 1024 * 1024 or manifest_path.is_symlink():
        raise ValueError("Export manifest is oversized or symlinked")
    manifest = _read_descriptor(manifest_path)
    validate_export_manifest(manifest)
    verify_assets(root, manifest.get("assets", []))
    return manifest


def validate_export_manifest(manifest: dict) -> dict:
    """Check the manifest contract without resolving or executing any asset."""
    if manifest.get("schema_version") != "modelbuilder.export/v1":
        raise ValueError("Unsupported model export descriptor")
    for field in ("selected_checkpoint", "loader", "signature", "dependencies", "source", "verification"):
        if not manifest.get(field):
            raise ValueError("Export missing " + field)
    verification = manifest["verification"]
    if not isinstance(verification, dict) or verification.get("passed") is not True or type(verification.get("atol")) not in (int, float):
        raise ValueError("Export requires recorded reload/prediction verification")
    try:
        valid_tolerance = math.isfinite(verification["atol"]) and verification["atol"] >= 0
    except OverflowError:
        valid_tolerance = False
    if not valid_tolerance:
        raise ValueError("Export parity tolerance must be finite and nonnegative")
    loader = manifest["loader"]
    if not isinstance(loader, dict) or loader.get("path") not in {asset["path"] for asset in manifest["assets"]}:
        raise ValueError("Export loader must be a declared, hash-verified asset")
    return manifest


def live_evaluator(node: dict, recipe: dict, *, input_bindings=()) -> dict | None:
    """Resolve one explicit downstream evaluator without inventing model IO rules."""
    settings = recipe.get("evaluation", {}).get("live", {})
    if not settings.get("enabled"):
        return None
    if node["kind"] != "model.train" and node.get("component", {}).get("role") != "training":
        return None
    upstream = {(e["source_node"], e["source_port"]): e["target_port"] for e in recipe["edges"] if e["target_node"] == node["id"]}
    def bound_identity(binding):
        return tuple(binding.get(key) for key in ("backend_run_id", "source_node_id", "source_port", "artifact_id", "content_sha256"))
    trainer_bound = [binding for binding in input_bindings if binding["node_id"] == node["id"]]
    candidates = []
    for evaluator in recipe["nodes"]:
        if evaluator["kind"] != "model.evaluate" and evaluator.get("component", {}).get("role") != "evaluation":
            continue
        edges = [e for e in recipe["edges"] if e["target_node"] == evaluator["id"]]
        checkpoints = [e for e in edges if e["source_node"] == node["id"]]
        if len(checkpoints) != 1:
            continue
        mapping = {}
        compatible = True
        for edge in edges:
            if edge in checkpoints:
                continue
            source = (edge["source_node"], edge["source_port"])
            if source not in upstream:
                compatible = False
                break
            mapping[edge["target_port"]] = upstream[source]
        for binding in input_bindings:
            if binding["node_id"] != evaluator["id"]:
                continue
            matches = [item for item in trainer_bound if bound_identity(item) == bound_identity(binding)]
            if len(matches) != 1 or binding["input_port"] in mapping or binding["input_port"] == checkpoints[0]["target_port"]:
                compatible = False
                break
            mapping[binding["input_port"]] = matches[0]["input_port"]
        supplied = set(mapping) | {checkpoints[0]["target_port"]}
        if not compatible or any(port.get("required", True) and port["name"] not in supplied for port in evaluator.get("inputs", [])):
            continue
        candidates.append({"node": evaluator, "checkpoint_port": checkpoints[0]["target_port"],
                           "input_bindings": mapping, "settings": settings})
    if len(candidates) != 1:
        raise ValueError("Live training requires exactly one downstream evaluator with shared upstream inputs")
    return candidates[0]


def validate_live_evaluation(recipe: dict, *, input_bindings=()) -> None:
    settings = recipe.get("evaluation", {}).get("live", {})
    if not isinstance(settings, dict) or set(settings) - {"enabled", "max_concurrency", "timeout_seconds"}:
        raise ValueError("Unsupported live evaluation settings")
    if settings and type(settings.get("enabled")) is not bool:
        raise ValueError("Live evaluation must declare enabled as a boolean")
    if type(settings.get("max_concurrency", 1)) is not int or not 1 <= settings.get("max_concurrency", 1) <= 4:
        raise ValueError("Live evaluation concurrency must be 1 through 4")
    if type(settings.get("timeout_seconds", 300)) not in (int, float) or not 1 <= settings.get("timeout_seconds", 300) <= 3600:
        raise ValueError("Live evaluation timeout must be 1 through 3600 seconds")
    training = [node for node in recipe.get("nodes", []) if node["kind"] == "model.train" or node.get("component", {}).get("role") == "training"]
    if settings.get("enabled") and not training:
        raise ValueError("Live evaluation requires a training component with checkpoint publication support")
    for node in training:
        if (settings.get("enabled") and node["kind"] == "component.run"
                and not node["component"]["capabilities"].get("checkpoint_events", False)):
            raise ValueError("Training component does not declare checkpoint publication support")
        live_evaluator(node, recipe, input_bindings=input_bindings)
