"""Reviewed source pinning. All filesystem access stays inside an approved alias."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from . import recipe as recipe_module
from .models import ServiceError
from .source_provenance import verify_export_git_provenance


def review(service, payload):
    project_id = payload["project_id"]
    project = service._get("project", project_id, project_id)
    service._revision(project, payload["expected_project_revision"])
    current = service._current_recipe(project_id, payload["recipe_id"])
    service._revision(current, payload["expected_revision"])
    binding = project.get("source_binding")
    if not binding:
        raise ServiceError("source_binding_required", "Bind this project to an approved checkout before pinning source")
    if binding["repository_id"] != payload["source_repository_id"]:
        raise ServiceError("source_identity_conflict", "The reviewed source checkout identity changed", status_code=409)
    source, _ = service.source_workspaces.resolve(binding, project_id)
    paths = set(payload["paths"])
    required = {node["implementation_ref"]["path"] for node in current["nodes"]
                if isinstance(node["implementation_ref"], dict) and "path" in node["implementation_ref"]}
    for profile in ("lock_file", "mlx_profile", "trainer_profile"):
        if current["environment"].get(profile):
            required.add(current["environment"][profile]["path"])
    if required - paths:
        raise ServiceError("source_closure_incomplete", "Include the recipe's implementation and environment lock files",
                           {"required_paths": sorted(required - paths)})
    root = Path(source["workspace_path"])
    proposed = recipe_module.pin_source_files(current, root, sorted(paths), payload["commit"].lower())
    proposed["revision"] = current["revision"] + 1
    proposed = recipe_module.normalize_recipe(proposed)
    # pin_source_files has already read and validated each selected file. Verify
    # its exact hash inventory against Git without retaining source contents or
    # rereading mutable working-tree bytes into a potentially huge bundle.
    inventory = {item["path"]: {"sha256": item["sha256"]} for item in proposed["source"]["files"]}
    verify_export_git_provenance({"recipe": proposed, "files": inventory}, root)
    result = {
        "schema_version": "modelbuilder.source-pin/v1", "project_id": project_id,
        "recipe_id": current["recipe_id"], "project_revision": project["revision"],
        "current_revision": current["revision"], "next_revision": proposed["revision"],
        "source_binding": copy.deepcopy(binding), "before_source": copy.deepcopy(current["source"]),
        "after_source": copy.deepcopy(proposed["source"]), "before_recipe_digest": current["digest"],
        "proposed_recipe_digest": recipe_module.recipe_digest(proposed),
        "file_inventory": copy.deepcopy(proposed["source"]["files"]), "execution_allowed": False,
        "git_modified": False,
    }
    digest = hashlib.sha256(recipe_module.canonical_json(result).encode()).hexdigest()
    return result, digest, proposed


def dispatch(service, operation, payload):
    details, digest, proposed = review(service, payload)
    if operation.endswith(".preview"):
        return {"review": details, "review_digest": digest, "confirmation_required": True,
                "execution_allowed": False}
    if payload["review_digest"] != digest:
        raise ServiceError("review_conflict", "The recipe, selected source or binding changed; review the pin again", status_code=409)
    saved = service._save_recipe(proposed, payload["project_id"], payload["recipe_id"],
                                 details["next_revision"], payload["expected_revision"])
    service._confirm_recipe_review(payload["project_id"])
    return {"project_id": payload["project_id"], "recipe_id": saved["recipe_id"], "revision": saved["revision"],
            "recipe": saved, "source_binding": details["source_binding"], "review_digest": digest,
            "execution_allowed": False, "git_modified": False}
