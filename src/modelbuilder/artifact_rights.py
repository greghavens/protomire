"""Inert license-evidence lineage. This module makes no legal compatibility claim."""
from __future__ import annotations
from copy import deepcopy
from jsonschema import Draft202012Validator
from .evaluation import digest

TEXT = {"type": "string", "minLength": 1}
SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
RIGHTS_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"schema_version": {"const": "modelbuilder.artifact-rights/v1"},
        "sources": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {**{key: TEXT for key in ("provider", "repo_type", "repo_id", "commit", "license_policy_id")}, "license_evidence_digest": SHA},
            "required": ["provider", "repo_type", "repo_id", "commit", "license_evidence_digest", "license_policy_id"]}},
        "unresolved": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"artifact_id": TEXT, "reason": TEXT}, "required": ["artifact_id", "reason"]}},
        "parent_rights_digests": {"type": "array", "uniqueItems": True, "items": SHA}},
    "required": ["schema_version", "sources", "unresolved", "parent_rights_digests"]}


def combine_parent_rights(parents):
    """Union every parent, retaining missing/invalid evidence as unresolved.

    ``parents`` is a list of exact native artifact IDs, checkpoint digests and
    full declared rights objects. The caller retains this list in derivation
    provenance. License text/revision evidence remains addressed by its original
    immutable evidence digest, not replaced with a destination license label.
    """
    sources, unresolved, parent_digests = {}, {}, set()
    for parent in parents:
        rights = parent.get("rights")
        if rights is None:
            entry = {"artifact_id": parent["artifact_id"], "reason": "Parent checkpoint has no immutable license-evidence/review envelope"}
            unresolved[digest(entry)] = entry
            continue
        parent_digests.add(digest(rights))
        if list(Draft202012Validator(RIGHTS_SCHEMA).iter_errors(rights)):
            entry = {"artifact_id": parent["artifact_id"], "reason": "Parent rights envelope is unsupported or incomplete; retain its original evidence for review"}
            unresolved[digest(entry)] = entry
            continue
        for source in rights["sources"]:
            sources[digest(source)] = deepcopy(source)
        for entry in rights["unresolved"]:
            unresolved[digest(entry)] = deepcopy(entry)
        parent_digests.update(rights["parent_rights_digests"])
    return {"schema_version": "modelbuilder.artifact-rights/v1", "sources": [sources[key] for key in sorted(sources)],
            "unresolved": [unresolved[key] for key in sorted(unresolved)], "parent_rights_digests": sorted(parent_digests)}


def check_required_review(rights, intended_use):
    """Block explicit unresolved obligations; resolved IDs still require policy review.

    This does not validate permissions or declare a license compatible. The
    authoritative project rights-policy layer must resolve source evidence and
    review IDs for the requested training/export/publication use.
    """
    if rights is None:
        return
    Draft202012Validator(RIGHTS_SCHEMA).validate(rights)
    if rights["unresolved"]:
        raise ValueError(f"{intended_use} requires resolution of upstream license evidence/review")
