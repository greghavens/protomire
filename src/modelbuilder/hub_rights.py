"""Inert declared-input license admission. Labels never grant permissions.

The policy map MUST come from authoritative service records, never from a recipe,
artifact or API caller. Evidence and rights envelopes are portable; approvals are
project-scoped decisions that must be resolved again in the receiving service.
This is not legal interpretation or an undeclared-I/O sandbox.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .export_packages import safe_file
from .models import ServiceError

USES = frozenset({"download", "train", "evaluate", "generate", "export", "publish", "promote"})
SOURCE_FIELDS = ("provider", "repo_type", "repo_id", "commit")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def blocked(message, unresolved=None):
    raise ServiceError("license_rights_unreviewed", message,
                       {"unresolved": unresolved or [], "permissions_inferred_from_label": False}, 409)


def source_reference(source, evidence_digest, policy_id):
    return {**{key: source[key] for key in SOURCE_FIELDS}, "license_evidence_digest": evidence_digest,
            "license_policy_id": policy_id}


def validate_usage_context(context):
    if (not isinstance(context, dict) or set(context) != {"purpose", "commercial", "deployment", "conditions"}
            or not isinstance(context.get("purpose"), str) or not 0 < len(context["purpose"]) <= 2000
            or not isinstance(context.get("commercial"), bool)
            or not isinstance(context.get("deployment"), str) or not 0 < len(context["deployment"]) <= 200
            or not isinstance(context.get("conditions"), list) or len(context["conditions"]) > 100
            or any(not isinstance(item, str) or not 0 < len(item) <= 2000 for item in context["conditions"])):
        blocked("Declare an explicit structured usage context and have it reviewed against all upstream rights")
    return digest(context)


def validate_artifact_rights(envelope, intended_use, policies=None, usage_context=None):
    """Validate all declared parents against current service-resolved decisions."""
    if intended_use not in USES:
        blocked("Choose an explicit supported intended use before reviewing rights")
    if (not isinstance(envelope, dict) or envelope.get("schema_version") != "modelbuilder.artifact-rights/v1"
            or not isinstance(envelope.get("sources"), list) or not isinstance(envelope.get("unresolved"), list)):
        blocked("Artifact has no complete declared license lineage")
    if not envelope["sources"] and not envelope["unresolved"]:
        blocked("An empty rights envelope cannot establish rights to an artifact")
    if envelope["unresolved"]:
        blocked("One or more upstream model/dataset rights are unresolved", envelope["unresolved"])
    context_digest = validate_usage_context(usage_context)
    if not isinstance(policies, dict):
        blocked("Authoritative policy map is unavailable")
    policy_ids, evidence_digests = set(), set()
    for source in envelope["sources"]:
        if not isinstance(source, dict) or any(not isinstance(source.get(key), str) or not source[key] for key in (*SOURCE_FIELDS, "license_evidence_digest", "license_policy_id")):
            blocked("Upstream source is missing immutable license evidence or a reviewed policy")
        policy = (policies or {}).get(source["license_policy_id"])
        if (not isinstance(policy, dict) or policy.get("schema_version") != "modelbuilder.license-policy/v1"
                or policy.get("status") != "reviewed" or type(policy.get("revision")) is not int or policy["revision"] < 1 or not policy.get("reviewed_by") or not policy.get("reviewed_at")
                or policy.get("review_digest") != digest({key: value for key, value in policy.items() if key != "review_digest"})):
            blocked("A current authoritative reviewed license policy is required", [{"source": source}])
        if (policy.get("source") != {key: source[key] for key in SOURCE_FIELDS}
                or policy.get("license_evidence_digest") != source["license_evidence_digest"]):
            blocked("License policy does not match the exact immutable upstream source and evidence", [{"source": source}])
        if (not isinstance(policy.get("permitted_uses"), list)
                or validate_usage_context(policy.get("reviewed_usage_context")) != context_digest):
            blocked("The declared usage context differs from the exact reviewed upstream policy", [{"policy_id": source["license_policy_id"]}])
        if intended_use not in policy.get("permitted_uses", []):
            blocked("The reviewed policy does not permit this intended use", [{"policy_id": source["license_policy_id"], "intended_use": intended_use}])
        if policy.get("unresolved_obligations"):
            blocked("The reviewed policy retains unresolved obligations", policy["unresolved_obligations"])
        if policy.get("gated") and not policy.get("gated_terms_acknowledged"):
            blocked("Provider access does not acknowledge gated terms; review them separately")
        policy_ids.add(source["license_policy_id"])
        evidence_digests.add(source["license_evidence_digest"])
    return {"status": "reviewed_declared_inputs", "usage_context_digest": context_digest, "intended_use": intended_use, "policy_ids": sorted(policy_ids), "policy_revisions": [{"policy_id": identity, "revision": policies[identity]["revision"], "review_digest": policies[identity]["review_digest"]} for identity in sorted(policy_ids)], "evidence_digests": sorted(evidence_digests),
            "declared_inputs_only": True, "permissions_inferred_from_label": False}


def validate_source_origin(recipe, policy):
    """A direct recipe origin applies only to the reviewed pinned source closure.

    Inherited resource/checkpoint origins retain their own identity; they are not
    asserted to be the source code of the receiving recipe.
    """
    origin = policy.get("source_origin")
    source = recipe.get("source", {})
    if (not isinstance(origin, dict) or origin.get("schema_version") != "modelbuilder.source-origin/v1"
            or not isinstance(source, dict) or origin.get("commit") != source.get("commit")
            or not isinstance(origin.get("files"), list) or not origin["files"]
            or not isinstance(source.get("files"), list)):
        blocked("The source-origin policy does not match the current pinned recipe source")
    def closure(files):
        if any(not isinstance(item, dict) or not item.get("path") or not item.get("sha256") for item in files):
            blocked("Source-origin file closure is malformed")
        result = {item["path"]: item["sha256"] for item in files}
        if len(result) != len(files):
            blocked("Source-origin file closure contains duplicate paths")
        return result
    reviewed = closure(origin["files"])
    if closure(source["files"]) != reviewed:
        blocked("Pinned source files changed after source-origin review")
    approved = closure(policy.get("source_files", []))
    if any(approved.get(path) != sha for path, sha in reviewed.items()):
        blocked("Source-origin bytes are outside the reviewed evidence closure")


def validate_execution_rights(recipe, intended_use="train", policies=None):
    """Check all registered model/dataset/checkpoint inputs, regardless of provider."""
    if not isinstance(recipe, dict) or intended_use not in USES:
        blocked("Recipe or intended use is malformed")
    sources, unresolved, governed = [], [], []
    collections = [recipe.get("resources", []), recipe.get("external_dependencies", [])]
    if any(not isinstance(items, list) for items in collections):
        blocked("Registered input collection is malformed")
    for resource in collections[0] + collections[1]:
        if not isinstance(resource, dict):
            blocked("Registered input is malformed")
        if resource.get("kind") not in {"model", "dataset", "checkpoint"}:
            continue
        governed.append(resource)
        metadata = resource.get("metadata", {})
        if not isinstance(metadata, dict):
            blocked("Registered input license metadata is malformed")
        rights = metadata.get("rights")
        if "rights" in metadata:
            if (not isinstance(rights, dict) or rights.get("schema_version") != "modelbuilder.artifact-rights/v1"
                    or not isinstance(rights.get("sources"), list) or not isinstance(rights.get("unresolved"), list)
                    or not rights["sources"] and not rights["unresolved"]):
                unresolved.append({"resource_id": resource.get("id"), "reason": "invalid_rights_envelope"})
            else:
                sources.extend(rights.get("sources", []))
                unresolved.extend(rights.get("unresolved", []))
        elif metadata.get("license_source") and metadata.get("license_policy_id") and metadata.get("license_evidence_digest"):
            source = metadata["license_source"]
            if not isinstance(source, dict) or any(not isinstance(source.get(key), str) or not source[key] for key in SOURCE_FIELDS):
                blocked("Registered input source identity is malformed")
            sources.append(source_reference(source, metadata["license_evidence_digest"], metadata["license_policy_id"]))
        else:
            unresolved.append({"resource_id": resource.get("id"), "reason": "missing_reviewed_license_evidence"})
    extensions = recipe.get("extensions", {})
    if not isinstance(extensions, dict):
        blocked("Recipe rights extensions are malformed")
    rights = extensions.get("artifact_rights")
    if rights is not None:
        if (not isinstance(rights, dict) or rights.get("schema_version") != "modelbuilder.artifact-rights/v1"
                or not isinstance(rights.get("sources"), list) or not isinstance(rights.get("unresolved"), list)
                or not rights["sources"] and not rights["unresolved"]):
            unresolved.append({"reason": "invalid_derived_rights_envelope"})
        else:
            sources.extend(rights.get("sources", []))
            unresolved.extend(rights.get("unresolved", []))
    if not sources and not unresolved:
        return {"status": "no_declared_external_asset_inputs", "intended_use": intended_use, "policy_ids": [], "evidence_digests": [], "declared_inputs_only": True, "permissions_inferred_from_label": False}
    receipt = validate_artifact_rights({"schema_version": "modelbuilder.artifact-rights/v1", "sources": sources,
                                      "unresolved": unresolved, "parent_rights_digests": []}, intended_use, policies, extensions.get("usage_context"))
    if isinstance(rights, dict):
        for origin in rights["sources"]:
            if origin.get("repo_type") != "source":
                blocked("Direct recipe origin must be reviewed source; model and dataset parents require verified declared or native inputs")
            validate_source_origin(recipe, policies[origin["license_policy_id"]])
    for resource in governed:
        metadata = resource["metadata"]
        declared_sources = metadata["rights"]["sources"] if "rights" in metadata else [source_reference(metadata["license_source"], metadata["license_evidence_digest"], metadata["license_policy_id"])]
        identities = {item["license_policy_id"] for item in declared_sources}
        if any(not isinstance(policies[identity].get("source_files", []), list) or not isinstance(policies[identity].get("verified_source_files", []), list) for identity in identities):
            blocked("Reviewed source file integrity evidence is malformed")
        approved_hashes = {item.get("sha256") for identity in identities
                           for item in policies[identity].get("source_files", []) + policies[identity].get("verified_source_files", [])
                           if isinstance(item, dict) and item.get("sha256")}
        reference = metadata.get("dataset_reference")
        if reference is not None:
            if not isinstance(reference, dict) or not isinstance(reference.get("manifest"), dict) or not isinstance(reference["manifest"].get("sources"), list):
                blocked("Registered dataset reference has no valid source byte closure")
            hashes = [item.get("sha256") for item in reference["manifest"]["sources"] if isinstance(item, dict)]
        else:
            hashes = [resource.get("sha256")]
        if not hashes or any(not value or value not in approved_hashes for value in hashes):
            blocked("Declared input bytes are outside the exact reviewed and verified source file closure", [{"resource_id": resource.get("id")}])
    return receipt


def validate_publication_rights(envelope, policies, files, snapshot_root, destination_license, usage_context=None):
    result = validate_artifact_rights(envelope, "publish", policies, usage_context)
    if not envelope["sources"]:
        blocked("Publication requires reviewed source or locally-authored evidence; absent lineage is not permission")
    for policy_id in result["policy_ids"]:
        if destination_license not in policies[policy_id].get("allowed_destination_licenses", []):
            blocked("Destination license is not included in the reviewed upstream policy", [{"policy_id": policy_id}])
    validate_notice_obligations(policies, result["policy_ids"], files, snapshot_root)
    return result


def validate_notice_obligations(policies, policy_ids, files, snapshot_root):
    """Validate exact output notice/attribution obligations after rights admission."""
    selected = {item["path"]: item for item in files}
    for policy_id in policy_ids:
        policy = policies[policy_id]
        for notice in policy.get("required_files", []):
            if selected.get(notice["path"], {}).get("sha256") != notice["sha256"]:
                blocked("An upstream license or notice file must be preserved exactly", [{"policy_id": policy_id, "path": notice["path"]}])
        for attribution in policy.get("required_attributions", []):
            if attribution["path"] not in selected:
                blocked("A required attribution file is missing", [{"policy_id": policy_id, "path": attribution["path"]}])
            with safe_file(Path(snapshot_root) / attribution["path"], max_bytes=1024 * 1024) as handle:
                text = handle.read(1024 * 1024).decode("utf-8")
            if attribution["text"] not in text:
                blocked("The exact reviewed attribution is missing", [{"policy_id": policy_id, "path": attribution["path"]}])
    return {"notice_obligations_checked": True, "policy_ids": list(policy_ids)}
