"""Verify checkpoint attribution, without trusting evaluator claims as ownership.

Explicit comparisons/promotions read native checkpoint bytes. Ordinary status
polling does not. This checks native input identity, not the scientific honesty
of arbitrary source code, its predictions, or other evaluator-supplied metadata.
"""
from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

from .adapter import BackendError
from .common import recipe_provenance
from .components import identity, read_checkpoint
from .native_exports import native_directory
from .native_inputs import storage_identity, validate_native_type
from ..export_packages import safe_file


def pending_verification():
    return {"verified": False, "code": "checkpoint_verification_pending",
            "message": "Checkpoint identity and ownership are verified on explicit comparison or promotion"}


def checkpoint_candidates(run, request, node_id, *, invocation_id=None):
    """Return proven connected checkpoint identities and their native owners.

    Only native Path checkpoints using the published manifest, plus the explicit
    built-in fixture format, are supported. No custom materializers or pickle.
    Boundary bindings must agree with immutable native execution provenance.
    """
    recipe = request["recipe"]
    extra = getattr(getattr(run, "config", None), "extra", {})
    if (any(extra.get(key) != value for key, value in recipe_provenance(recipe).items())
            or extra.get("application_project_id") != request["project_id"]):
        raise ValueError("Evaluation recipe/project provenance differs from its native run")
    nodes = {node["id"]: node for node in recipe["nodes"]}
    node = nodes[node_id]
    from .native_invocations import invocation, output_step
    invocation_id = invocation_id or node_id
    evaluator_step = run.steps[invocation_id]
    evaluator_info = invocation(invocation_id, evaluator_step, recipe)
    from ..iterations import memberships
    member = memberships(recipe)
    bindings = request.get("resolved_input_bindings", [])
    native_execution = extra.get("execution", {})
    if bindings != native_execution.get("input_bindings", []):
        raise ValueError("Checkpoint bindings differ from immutable native execution provenance")
    selected = set(native_execution.get("node_ids", nodes))
    if node_id not in selected:
        raise ValueError("Evaluator was not in the executed node selection")
    root = Path(request["root"])

    semantic_cache = {}

    def semantic(artifact):
        key = (str(artifact.id), artifact.uri)
        if key in semantic_cache:
            return semantic_cache[key]
        validate_native_type(artifact, "Path")
        before = storage_identity(artifact, root)
        with native_directory(artifact, root) as directory:
            if (directory / "checkpoint.manifest.json").is_file():
                checkpoint = identity(read_checkpoint(directory))
            elif node["kind"] == "model.evaluate":
                kind = node.get("parameters", {}).get("checkpoint_kind", "best")
                if kind not in {"best", "last"}:
                    raise ValueError("Unsupported fixture checkpoint selection")
                with safe_file(directory / ("best.pt" if kind == "best" else "checkpoint.pt")) as stream:
                    checkpoint = hashlib.file_digest(stream, "sha256").hexdigest()
            else:
                raise ValueError("Connected checkpoint has no supported identity manifest")
        if storage_identity(artifact, root) != before:
            raise ValueError("Native checkpoint changed during identity verification")
        semantic_cache[key] = checkpoint
        return checkpoint

    def owner(target, port, artifact, target_info, seen=()):
        if target_info["invocation_id"] in seen or str(artifact.project.id) != str(run.project.id):
            raise ValueError("Checkpoint input has cyclic or foreign-project ownership")
        state = next((state for state in member.get(target, {}).get("state", []) if state["target_node"] == target and state["target_port"] == port), None) if target_info.get("iteration", 1) > 1 else None
        bound = [entry for entry in bindings if entry["node_id"] == target and entry["input_port"] == port] if state is None else []
        if bound:
            if len(bound) != 1 or bound[0]["artifact_id"] != str(artifact.id):
                raise ValueError("Native checkpoint input differs from its admitted binding")
            binding = bound[0]
            # A checkpoint relay's run version is not necessarily the original
            # training owner. Until that lineage is available, do not guess.
            if binding.get("producer_role") != "training" or not binding.get("producer_model_version_ref"):
                raise ValueError("Bound checkpoint has no unambiguous training model owner")
            if storage_identity(artifact, root) != binding["content_sha256"]:
                raise ValueError("Bound native checkpoint storage changed after admission")
            return binding["producer_model_version_ref"], [binding]
        edges = [state] if state else [edge for edge in recipe["edges"] if edge["target_node"] == target and edge["target_port"] == port
                 and edge["source_node"] in selected]
        if len(edges) != 1:
            raise ValueError("Checkpoint input has no unique executed producer")
        edge = edges[0]
        source = nodes[edge["source_node"]]
        source_info, source_step = output_step(run, recipe, source["id"], edge["source_port"], str(artifact.id))
        if target_info.get("iteration_block_id") and source_info.get("iteration_block_id") == target_info["iteration_block_id"]:
            expected = target_info["iteration"] - (1 if state else 0)
            if source_info.get("iteration") != expected:
                raise ValueError("Native checkpoint input comes from the wrong declared iteration")
        produced = source_step.outputs.get(edge["source_port"], [])
        if len(produced) != 1 or str(produced[0].id) != str(artifact.id):
            raise ValueError("Native checkpoint input differs from its connected producer output")
        if source["kind"] == "model.train" or source.get("component", {}).get("role") == "training":
            if not run.model_version or getattr(getattr(source_step, "status", None), "value", None) == "cached":
                raise ValueError("Checkpoint training version is absent or cached ownership is unsupported")
            return str(run.model_version.id), []
        if source["kind"] == "model.checkpoint":
            incoming = source_step.inputs.get("checkpoint", [])
            if len(incoming) != 1:
                raise ValueError("Checkpoint relay input is absent or ambiguous")
            # Re-materialization may change archive headers. Compare the actual
            # supported checkpoint identity, never unlike container-byte digests.
            if str(incoming[0].project.id) != str(run.project.id) or semantic(incoming[0]) != semantic(artifact):
                raise ValueError("Checkpoint relay content differs from its input")
            return owner(source["id"], "checkpoint", incoming[0], source_info, (*seen, target_info["invocation_id"]))
        raise ValueError("Checkpoint producer does not establish a training model owner")

    candidates, errors = [], []
    for port in node.get("inputs", []):
        if port["type"] not in {"Path", "checkpoint", "CheckpointArtifact"}:
            continue
        if port["type"] == "Path":
            bound = [entry for entry in bindings if entry["node_id"] == node_id and entry["input_port"] == port["name"]]
            edges = [edge for edge in recipe["edges"] if edge["target_node"] == node_id and edge["target_port"] == port["name"]
                     and edge["source_node"] in selected]
            roles = ([entry.get("producer_role") for entry in bound] if bound else
                     [(nodes[edge["source_node"]].get("component", {}).get("role") or nodes[edge["source_node"]]["kind"]) for edge in edges])
            # Calibration/export/dataset Paths are not checkpoints. Only skip
            # positively identified non-training producers; missing/ambiguous
            # lineage and explicit checkpoint types must still fail closed.
            if len(roles) == 1 and roles[0] and roles[0] not in {"training", "model.train", "model.checkpoint"}:
                continue
        artifacts = evaluator_step.inputs.get(port["name"], [])
        if len(artifacts) > 1:
            errors.append("Checkpoint input port has multiple native artifacts")
            continue
        for artifact in artifacts:
            try:
                if str(artifact.project.id) != str(run.project.id):
                    raise ValueError("Checkpoint artifact belongs to another native project")
                validate_native_type(artifact, "Path")
                version, checkpoint_bindings = owner(node_id, port["name"], artifact, evaluator_info)
                checkpoint = semantic(artifact)
                candidates.append({"artifact_id": str(artifact.id), "input_port": port["name"], "identity": checkpoint,
                                   "model_version_ref": version, "checkpoint_bindings": checkpoint_bindings})
            except (BackendError, OSError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError) as error:
                errors.append(str(error))
    return candidates, errors


def verify_checkpoint(record, candidates, errors):
    treatment = record.get("treatment")
    checkpoint = treatment.get("checkpoint") if isinstance(treatment, dict) else None
    matching = [entry for entry in candidates if entry["identity"] == checkpoint]
    owners = {entry["model_version_ref"] for entry in matching}
    if len(owners) == 1 and not errors:
        return {"verified": True, "identity": checkpoint,
                "inputs": [{key: value for key, value in entry.items() if key != "checkpoint_bindings"} for entry in matching]}, next(iter(owners)), [binding for entry in matching for binding in entry["checkpoint_bindings"]]
    reason = ("Connected checkpoint ownership or identity could not be verified: " + "; ".join(errors) if errors else
              "Evaluation checkpoint does not match a connected native checkpoint" if not matching else
              "Evaluation checkpoint matches multiple model owners")
    return {"verified": False, "code": "evaluation_checkpoint_unverified", "message": reason}, None, []
