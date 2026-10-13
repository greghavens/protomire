"""Inert builtin fixture provenance; never grants permission from an origin label."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from ..artifact_rights import combine_parent_rights
from .components import identity, read_checkpoint
from .common import write_json

FORMAT = "modelbuilder.fixture-pytorch/v1"


def declared_rights(spec):
    parents = []
    extensions = spec.get("rights_extensions", {})
    if "artifact_rights" in extensions:
        parents.append({"artifact_id": "recipe-declared-origin", "rights": extensions["artifact_rights"]})
    for resource in spec.get("dependencies", []):
        if resource.get("kind") not in {"model", "dataset", "checkpoint"}:
            continue
        metadata = resource.get("metadata", {})
        rights = metadata.get("rights")
        if rights is None and metadata.get("license_source"):
            from ..hub_rights import source_reference
            source = source_reference(metadata["license_source"], metadata.get("license_evidence_digest"), metadata.get("license_policy_id"))
            rights = {"schema_version": "modelbuilder.artifact-rights/v1", "sources": [source],
                      "unresolved": [], "parent_rights_digests": []}
        parents.append({"artifact_id": resource["id"], "rights": rights})
    if not parents:
        parents.append({"artifact_id": "unreviewed-local-origin:" + spec["node_id"], "rights": None})
    return combine_parent_rights(parents)


def attach_declared_rights(value, spec):
    parents = [{"artifact_id": "declared-inputs:" + spec["node_id"], "rights": declared_rights(spec)}]
    if "rights" in value:
        parents.append({"artifact_id": "imported-dataset:" + identity(value), "rights": value["rights"]})
    return {**value, "rights": combine_parent_rights(parents)}


def input_rights(spec, data, checkpoints=()):
    parents = [{"artifact_id": "dataset:" + data["sha256"], "rights": data.get("rights")},
               {"artifact_id": "declared-inputs:" + spec["node_id"], "rights": declared_rights(spec)}]
    for path in checkpoints:
        if path is None:
            continue
        descriptor = read_checkpoint(Path(path)) if (Path(path) / "checkpoint.manifest.json").is_file() else None
        parents.append({"artifact_id": "checkpoint:" + identity(descriptor) if descriptor else str(path),
                        "rights": descriptor.get("rights") if descriptor else None})
    return combine_parent_rights(parents)


def checkpoint_manifest(root, state, rights):
    """Publish verified asset hashes after each complete builtin checkpoint write.

    Last/best continue to identify their exact .pt bytes, not this new metadata.
    Direct fixture callers without reviewed lineage get an unresolved envelope.
    """
    import hashlib
    root = Path(root)
    assets, selections = [], {}
    for kind, filename in (("last", "checkpoint.pt"), ("best", "best.pt")):
        path = root / filename
        if path.is_file():
            asset = {"path": filename, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            assets.append(asset)
            selections[kind] = asset
    source = hashlib.sha256((Path(__file__).parent / "fixtures.py").read_bytes()).hexdigest()
    exact = all(key in state for key in ("optimizer", "scheduler", "torch_rng", "python_rng", "cursor", "order", "epoch"))
    manifest = {"schema_version": "modelbuilder.checkpoint/v1", "format": FORMAT,
                "purpose": "resumable" if exact else "inference", "step": state["step"],
                "resume_modes": ["exact_state", "warm_start"] if exact else ["warm_start"],
                "state": ["model", "optimizer", "scheduler", "rng", "data_cursor"] if exact else ["model", "rng"],
                "identities": {"architecture": identity({"fixture": state["fixture"], "width": state["width"]}),
                               "source": source, "dependencies": identity(state.get("initialization", {})),
                               "training": identity(state.get("training_contract", {"initialization_seed": state["seed"]})),
                               "environment": identity({"placement": state.get("placement", {"device": "cpu"})}),
                               "data": state["dataset_sha256"]},
                "assets": assets, "selections": selections,
                "rights": deepcopy(rights) if rights is not None else combine_parent_rights([
                    {"artifact_id": "fixture-checkpoint:" + selections["last"]["sha256"], "rights": None}])}
    write_json(root / "checkpoint.manifest.json", manifest)
    return manifest


def selected_identity(descriptor, kind):
    if descriptor.get("format") != FORMAT or kind not in {"last", "best"}:
        raise ValueError("Unsupported builtin checkpoint selection contract")
    selection = descriptor.get("selections", {}).get(kind)
    expected = "best.pt" if kind == "best" else "checkpoint.pt"
    if (not isinstance(selection, dict) or selection.get("path") != expected
            or selection not in descriptor["assets"]):
        raise ValueError("Builtin checkpoint selection must name an exact verified asset")
    return selection["sha256"]
