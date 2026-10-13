"""Bind one reviewed pinned source origin to future generated artifact lineage.

Folder location and source trust never establish ownership. Only an explicit,
current service-held policy supplies the reviewed rights, context and obligations.
"""
from __future__ import annotations

import copy
from pathlib import Path

from .hub import fail, fingerprint
from .hub_rights import digest, source_reference, validate_artifact_rights, validate_source_origin
from .source_provenance import verify_export_git_provenance
from .models import ServiceError


def inspect_origin(hub, payload, root):
    service = hub.service
    project = service._get("project", payload["project_id"], payload["project_id"])
    recipe = service._current_recipe(payload["project_id"])
    source, _ = service.source_workspaces.resolve(project.get("source_binding"), payload["project_id"])
    if (payload.get("source_repository_id") != source["repository_id"]
            or Path(source["workspace_path"]) != root
            or payload.get("source_commit") != recipe["source"]["commit"]
            or not recipe["source"].get("files")):
        fail("license_source_mismatch", "Source review must select the current bound repository and its exact pinned recipe commit")
    files = copy.deepcopy(recipe["source"]["files"])
    verify_export_git_provenance({"recipe": recipe, "files": {item["path"]: item for item in files}}, root)
    return {"schema_version": "modelbuilder.source-origin/v1", "repository_id": source["repository_id"],
            "source_alias": source["alias"], "commit": recipe["source"]["commit"], "files": files}


def bind_origin(hub, payload, actor, payload_hash, request_id, *, operation="hub.source.policy.bind"):
    project_id = payload["project_id"]
    service = hub.service
    with hub.store.transaction():
        hub._authority(operation, actor, project_id)
        previous = hub.store.idempotent_result(actor.id, payload["idempotency_key"], operation, payload_hash)
        if previous is not None:
            return previous
        project = service._get("project", project_id, project_id)
        service._revision(project, payload["expected_project_revision"])
        recipe = service._current_recipe(project_id, payload["recipe_id"])
        service._revision(recipe, payload["expected_revision"])
        replacing = operation == "hub.source.policy.replace"
        if replacing and recipe["digest"] != payload["expected_recipe_digest"]:
            fail("license_source_changed", "The exact recipe changed after the source-replacement review")
        replacing_ids = set(payload["replace_source_policy_ids"]) if replacing else set()
        policy = hub._get("hub_license_policy", payload["license_policy_id"], project_id)
        if policy["source"].get("provider") != "local" or policy["source"].get("repo_type") != "source":
            fail("license_source_mismatch", "Choose an explicitly reviewed local source-origin policy")
        evidence = hub._get("hub_license_evidence", policy["evidence_id"], project_id)
        data = evidence["evidence"]
        origin = policy.get("source_origin")
        if not origin or origin != data.get("source_origin") or policy["license_evidence_digest"] != evidence["evidence_digest"]:
            fail("license_source_mismatch", "The policy does not match the exact captured source origin")
        root = hub._source(project_id, evidence["source_alias"])
        current_origin = inspect_origin(hub, {"project_id": project_id, "source_repository_id": origin["repository_id"],
                                             "source_commit": origin["commit"]}, root)
        if current_origin != origin:
            fail("license_source_mismatch", "Pinned source identity changed after review")
        validate_source_origin(recipe, policy)
        for item in data["source_file_manifest"]:
            actual = fingerprint(root / item["path"], item["size_bytes"])
            if any(actual[key] != item[key] for key in actual):
                fail("license_evidence_changed", "Reviewed source or terms bytes changed before binding")
        source = source_reference(policy["source"], policy["license_evidence_digest"], policy["policy_id"])
        rights = {"schema_version": "modelbuilder.artifact-rights/v1", "sources": [source], "unresolved": [], "parent_rights_digests": []}
        validate_artifact_rights(rights, payload["intended_use"], hub.policy_snapshot(project_id), payload["usage_context"])
        proposed = copy.deepcopy(recipe)
        extensions = proposed.setdefault("extensions", {})
        if "usage_context" in extensions and extensions["usage_context"] != payload["usage_context"]:
            fail("license_usage_conflict", "Source review cannot overwrite a different recipe usage context")
        prior = extensions.get("artifact_rights")
        if replacing and prior is None:
            fail("license_source_mismatch", "No current direct source origin exists to replace")
        if prior is not None:
            if (not isinstance(prior, dict) or prior.get("schema_version") != rights["schema_version"]
                    or not isinstance(prior.get("sources"), list) or not isinstance(prior.get("unresolved"), list)
                    or not isinstance(prior.get("parent_rights_digests", []), list)):
                fail("license_rights_unreviewed", "Existing origin lineage is malformed; it cannot be silently replaced")
            available_ids = {parent.get("license_policy_id") for parent in prior["sources"] if isinstance(parent, dict) and parent.get("repo_type") == "source"}
            if replacing_ids - available_ids or policy["policy_id"] in replacing_ids:
                fail("license_source_mismatch", "Replacement may select only existing direct source policies, excluding the newly reviewed policy")
            for parent in prior["sources"]:
                if not isinstance(parent, dict) or parent.get("repo_type") != "source":
                    fail("license_source_mismatch", "Existing direct origin is not source evidence; bind model and dataset parents as verified inputs")
                previous_policy = hub._get("hub_license_policy", parent.get("license_policy_id", ""), project_id)
                if parent.get("license_policy_id") in replacing_ids:
                    if previous_policy["source"].get("repo_type") != "source":
                        fail("license_source_mismatch", "Replacement cannot remove model, dataset or checkpoint input rights")
                    continue
                try:
                    validate_source_origin(recipe, previous_policy)
                except ServiceError:
                    fail("license_source_replacement_required", "An existing direct source origin refers to different pinned code. Explicitly review and replace that direct origin in the recipe before binding; existing checkpoint/dataset parents and unresolved lineage must be preserved")
            if any(not isinstance(value, str) for value in prior.get("parent_rights_digests", [])):
                fail("license_rights_unreviewed", "Existing parent lineage digests are malformed")
            rights = copy.deepcopy(prior)
            rights["sources"] = [parent for parent in rights["sources"] if parent["license_policy_id"] not in replacing_ids]
            if source not in rights["sources"]:
                rights["sources"].append(source)
            rights["parent_rights_digests"] = sorted(set(rights.get("parent_rights_digests", []) + [digest(prior)]))
        extensions["artifact_rights"] = rights
        extensions["usage_context"] = copy.deepcopy(payload["usage_context"])
        hub._authority(operation, actor, project_id)
        saved = service._save_recipe(proposed, project_id, recipe["recipe_id"], recipe["revision"] + 1, recipe["revision"])
        service._confirm_recipe_review(project_id)
        result = {"project_id": project_id, "recipe_id": saved["recipe_id"], "revision": saved["revision"],
                  "recipe": saved, "policy_id": policy["policy_id"], "rights": rights, "source_origin": origin,
                  "future_outputs_only": True, "execution_allowed": False, "assets_registered": False,
                  "replaced_direct_source_policy_ids": sorted(replacing_ids), "prior_rights_digest": digest(prior) if prior is not None else None}
        hub.store.save_idempotent(actor.id, payload["idempotency_key"], operation, payload_hash, result)
        hub.store.audit(actor_id=actor.id, operation=operation, request_id=request_id, outcome="succeeded",
                        project_id=project_id, target_id=recipe["recipe_id"],
                        before_revision=recipe["revision"], after_revision=saved["revision"])
        return result
