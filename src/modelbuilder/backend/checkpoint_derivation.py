"""Versioned non-training checkpoint lineage over native ZenML artifacts.

This is a contract, not another model registry. A trusted source-bound adapter
owns the tensor transformation. The native step owns the resulting model version.
"""
from __future__ import annotations

from pathlib import Path

from jsonschema import Draft202012Validator

from .components import identity, read_checkpoint

TEXT = {"type": "string", "minLength": 1}
SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
PARENT_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"input_port": TEXT, "artifact_id": TEXT, "backend_project_id": TEXT,
                   "content_sha256": SHA, "checkpoint_digest": SHA},
    "required": ["input_port", "artifact_id", "backend_project_id", "content_sha256", "checkpoint_digest"]}
PRODUCER_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {key: TEXT for key in ("project_id", "backend_project_id", "backend_run_id", "model_version_ref", "node_id", "invocation_id")},
    "required": ["project_id", "backend_project_id", "backend_run_id", "model_version_ref", "node_id", "invocation_id"]}
DERIVATION_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"schema": {"const": "modelbuilder.checkpoint-derivation/v1"}, "operation": TEXT,
                   "implementation": {"type": "object"}, "configuration": {"type": "object"},
                   "parents": {"type": "array", "minItems": 1, "maxItems": 32, "items": PARENT_SCHEMA},
                   "producer": PRODUCER_SCHEMA,
                   "parent_rights_evidence": {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "object", "additionalProperties": False, "properties": {"artifact_id": TEXT, "checkpoint_digest": SHA, "rights": {"type": ["object", "null"]}}, "required": ["artifact_id", "checkpoint_digest", "rights"]}},
                   "calibration": {"const": {"status": "invalidated", "requires_refit": True}}},
    "required": ["schema", "operation", "implementation", "configuration", "parents", "producer", "parent_rights_evidence", "calibration"]}


def validate_derivation(checkpoint):
    derivation = checkpoint.get("derivation")
    Draft202012Validator(DERIVATION_SCHEMA).validate(derivation)
    if checkpoint.get("producer_kind") != "derivation" or checkpoint.get("purpose") != "inference":
        raise ValueError("Derived checkpoints must declare an inference-only derivation producer")
    if checkpoint.get("resume_modes") != [] or checkpoint.get("state") != ["model"]:
        raise ValueError("Derived checkpoints cannot claim optimizer or exact continuation state")
    if checkpoint.get("step") != 0 or checkpoint["identities"].get("training") is not None:
        raise ValueError("Derivation is not a training step or training-state identity")
    if checkpoint["identities"].get("calibration") is not None:
        raise ValueError("Derived weights invalidate inherited calibration")
    if checkpoint["identities"].get("derivation") != identity(derivation):
        raise ValueError("Derived checkpoint lineage digest differs")
    parents = derivation["parents"]
    if len({p["input_port"] for p in parents}) != len(parents) or len({p["artifact_id"] for p in parents}) != len(parents):
        raise ValueError("Derived parent input ports and native artifact IDs must be distinct")
    if any(p["backend_project_id"] != derivation["producer"]["backend_project_id"] for p in parents):
        raise ValueError("Derived checkpoint parents belong to another native project")
    from ..artifact_rights import combine_parent_rights
    rights_evidence = derivation["parent_rights_evidence"]
    if len(rights_evidence) != len(parents) or {(p["artifact_id"], p["checkpoint_digest"]) for p in rights_evidence} != {(p["artifact_id"], p["checkpoint_digest"]) for p in parents}:
        raise ValueError("Derived rights lineage must retain every exact parent checkpoint")
    if checkpoint.get("rights") != combine_parent_rights(rights_evidence):
        raise ValueError("Derived rights envelope must preserve all parent obligations and unresolved evidence")
    if not derivation["implementation"]:
        raise ValueError("Derivation requires a pinned implementation identity")
    return derivation


def native_context(spec, inputs):
    """Capture actual native inputs before any trusted adapter or subprocess runs."""
    from zenml import get_step_context
    from .native_inputs import storage_identity, validate_native_type
    ctx = get_step_context()
    if not ctx.model_version:
        raise ValueError("Checkpoint derivation requires its actual native model version")
    project_id = spec["recipe_provenance"]["application_project_id"]
    producer = {"project_id": project_id, "backend_project_id": str(ctx.pipeline_run.project.id),
        "backend_run_id": str(ctx.pipeline_run.id), "model_version_ref": str(ctx.model_version.id),
        "node_id": spec["node_id"], "invocation_id": spec.get("invocation", {}).get("invocation_id", spec["node_id"])}
    parents, parent_rights = [], []
    root = Path(spec["job_dir"]).parent.parent
    for port, value in sorted(inputs.items()):
        if not isinstance(value, Path) or not (value / "checkpoint.manifest.json").is_file():
            continue
        artifacts = ctx.inputs.get(port, [])
        if len(artifacts) != 1:
            raise ValueError("Derived checkpoint requires exactly one native parent per checkpoint input")
        artifact = artifacts[0]
        validate_native_type(artifact, "Path")
        if str(artifact.project.id) != producer["backend_project_id"]:
            raise ValueError("Foreign-project derived parent")
        parents.append({"input_port": port, "artifact_id": str(artifact.id),
            "backend_project_id": str(artifact.project.id), "content_sha256": storage_identity(artifact, root),
            "checkpoint_digest": identity(read_checkpoint(value))})
        parent_rights.append({"artifact_id": str(artifact.id), "checkpoint_digest": parents[-1]["checkpoint_digest"], "rights": read_checkpoint(value).get("rights")})
    return {"producer": producer, "parents": parents, "parent_rights_evidence": parent_rights}


def verify_output(checkpoint, context, node, *, expected_identities=None):
    derivation = validate_derivation(checkpoint)
    if expected_identities is not None and any(checkpoint["identities"].get(key) != expected_identities.get(key) for key in ("source", "dependencies", "environment", "data")):
        raise ValueError("Derived checkpoint identities differ from actual component execution")
    if derivation["producer"] != context["producer"] or derivation["parents"] != context["parents"] or derivation["parent_rights_evidence"] != context["parent_rights_evidence"]:
        raise ValueError("Derived checkpoint does not match its actual native producer and parent inputs")
    expected = {"component_digest": identity(node["component"]), "implementation_ref": node["implementation_ref"]}
    if derivation["implementation"] != expected or derivation["configuration"] != node.get("parameters", {}):
        raise ValueError("Derived checkpoint differs from its immutable implementation/configuration")


def verify_native_output(run, request, node, info, step, artifact, *, depth=0):
    """Recheck all parent bytes and ownership at explicit comparison/promotion."""
    from .native_exports import native_directory
    from .native_inputs import storage_identity
    from .evaluation_binding import checkpoint_candidates
    if depth > 32:
        raise ValueError("Derived checkpoint lineage exceeds its depth bound")
    if not run.model_version or getattr(getattr(step, "status", None), "value", None) == "cached":
        raise ValueError("Derived model owner is absent or cached")
    root = Path(request["root"])
    before = storage_identity(artifact, root)
    with native_directory(artifact, root) as directory:
        checkpoint = read_checkpoint(directory)
    derivation = validate_derivation(checkpoint)
    if checkpoint["identities"]["source"] != identity(request["recipe"]["source"]):
        raise ValueError("Derived checkpoint source differs from native recipe provenance")
    candidates, errors = checkpoint_candidates(run, request, node["id"], invocation_id=info["invocation_id"], derivation_depth=depth + 1)
    if errors:
        raise ValueError("Derived parent ownership could not be verified: " + "; ".join(errors))
    parents, parent_rights = [], []
    for candidate in candidates:
        connected = step.inputs.get(candidate["input_port"], [])
        if len(connected) != 1 or str(connected[0].id) != candidate["artifact_id"]:
            raise ValueError("Derived parent differs from native input")
        parents.append({"input_port": candidate["input_port"], "artifact_id": candidate["artifact_id"],
            "backend_project_id": str(connected[0].project.id), "content_sha256": storage_identity(connected[0], root),
            "checkpoint_digest": candidate["identity"]})
        with native_directory(connected[0], root) as parent_directory:
            parent_rights.append({"artifact_id": candidate["artifact_id"], "checkpoint_digest": candidate["identity"], "rights": read_checkpoint(parent_directory).get("rights")})
    producer = {"project_id": request["project_id"], "backend_project_id": str(run.project.id),
        "backend_run_id": str(run.id), "model_version_ref": str(run.model_version.id), "node_id": node["id"],
        "invocation_id": info["invocation_id"]}
    verify_output(checkpoint, {"producer": producer, "parents": sorted(parents, key=lambda p: p["input_port"]), "parent_rights_evidence": sorted(parent_rights, key=lambda p: next(parent["input_port"] for parent in parents if parent["artifact_id"] == p["artifact_id"]))}, node)
    if storage_identity(artifact, root) != before:
        raise ValueError("Derived native output changed during verification")
    return {"derivation": derivation, "production_eligible": request["recipe"].get("execution", {}).get("purpose") != "smoke" and all(c.get("production_eligible", True) for c in candidates)}


def verify_bound_output(binding, request, artifact, *, depth=0):
    """Follow a cross-run native binding to its real producer, never a relay owner."""
    from .common import read_json
    from .sdk import native_run
    from .native_invocations import output_step
    if depth > 32:
        raise ValueError("Derived checkpoint lineage exceeds its depth bound")
    root = Path(request["root"])
    matches = []
    for job in (root / "jobs").iterdir():
        source_request, native = read_json(job / "request.json"), read_json(job / "native.json")
        if source_request.get("project_id") == request["project_id"] and native.get("backend_run_id") == binding.get("backend_run_id"):
            matches.append((job, source_request))
    if len(matches) != 1:
        raise ValueError("Derived checkpoint producer run is unknown or ambiguous")
    job, source_request = matches[0]
    _, run = native_run(job)
    if not run or str(run.project.id) != str(artifact.project.id) or not run.model_version:
        raise ValueError("Derived checkpoint producer belongs to another native project")
    if str(run.model_version.id) != binding.get("producer_model_version_ref"):
        raise ValueError("Derived checkpoint native model owner changed")
    node = next(n for n in source_request["recipe"]["nodes"] if n["id"] == binding["source_node_id"])
    if node.get("component", {}).get("role") != "derivation":
        raise ValueError("Bound derived checkpoint is not produced by a derivation component")
    info, step = output_step(run, source_request["recipe"], node["id"], binding["source_port"], str(artifact.id))
    return verify_native_output(run, source_request, node, info, step, artifact, depth=depth + 1)
