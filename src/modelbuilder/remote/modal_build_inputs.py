"""Verify local declared build inputs, without pretending they attest an image.

Reuses the canonical recipe, operator source binding and installed-environment
verifiers. No image build, registry access, source upload or user-code import.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field

from modelbuilder import recipe as recipe_module
from modelbuilder.backend.admission import verify_environment
from modelbuilder.models import ServiceError
from modelbuilder.source_workspace import SourceWorkspaces

from .contracts import Contract
from .ledger import digest


class LocalBuildInputs(Contract):
    project_id: str
    recipe_id: str
    recipe_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_lock_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    repository_id: str
    environment_probe_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @property
    def evidence_digest(self):
        return digest(self.model_dump(mode="json"))


def verify_local_build_inputs(*, workspaces: SourceWorkspaces, project: dict, recipe: dict, backend) -> LocalBuildInputs:
    """Operator-side only. Public callers cannot provide checkout paths/receipts."""
    normalized = recipe_module.normalize_recipe(recipe)
    actual_recipe_digest = recipe_module.recipe_digest(recipe)
    if (recipe.get("digest") != actual_recipe_digest or recipe.get("project_id") != project["project_id"]
            or recipe.get("recipe_id") != project["recipe_id"]):
        raise ServiceError("source_recipe_mismatch", "Build inputs must use this project's canonical immutable recipe", status_code=409)
    source, _ = workspaces.resolve(project.get("source_binding"), project["project_id"], execute=True)
    if Path(source["workspace_path"]).resolve() != Path(backend.repository).resolve():
        raise ServiceError("source_binding_changed", "Build-input interpreter repository differs from the approved source checkout", status_code=409)
    status = workspaces.status(project, normalized)
    if not status.get("pinned_source_matches"):
        raise ServiceError("source_unverified", "Declared build source does not match the approved Git closure",
                           {"diagnostics": status["diagnostics"]}, 409)
    lock = normalized.get("environment", {}).get("lock_file")
    if not lock:
        raise ServiceError("environment_lock_required", "Remote build inputs need an explicit SHA256-pinned environment lock", status_code=409)
    environment = verify_environment(backend, normalized)
    return LocalBuildInputs(project_id=project["project_id"], recipe_id=recipe["recipe_id"],
        recipe_digest=actual_recipe_digest, source_digest=digest(normalized["source"]),
        environment_lock_digest=lock["sha256"], repository_id=source["repository_id"],
        environment_probe_digest=digest(environment))
