"""Service regressions for resume selection and authored-checkout provenance."""
from copy import deepcopy
import hashlib
import subprocess

import pytest

from modelbuilder.models import Actor, ServiceError
from modelbuilder.recipe import default_recipe
from modelbuilder.service import OperationService

OWNER = Actor("reviewer", {"read", "modify", "execute", "publish", "spend"})


class NonexecutingBackend:
    def __init__(self):
        self.calls = []

    def invoke(self, operation, payload, actor):
        self.calls.append((operation, deepcopy(payload)))
        return {"operation_id": payload.get("operation_id"), "project_id": payload["project_id"],
                "backend_run_id": "native-" + payload["operation_id"], "status": "submitted", "worker_alive": False}


def project(service, recipe, key="model"):
    return service.invoke("project.create", {"slug": key, "model_name": key, "recipe": recipe,
                                           "idempotency_key": key}, OWNER)


def training_recipe():
    recipe = default_recipe("template", "template", "training")
    recipe["nodes"] = [{"id": "train", "kind": "model.train", "implementation_ref": "builtin:model.train",
        "inputs": [], "outputs": [], "parameters": {"steps": 4, "learning_rate": 0.1}, "environment_ref": "default",
        "resource_request": {}, "retry": {"max_attempts": 1, "backoff_seconds": 0}, "timeout": 30,
        "cache_policy": "disabled", "provenance": {}}]
    return recipe


@pytest.mark.parametrize("mode,expected_rate", [("warm_start", 0.01), ("exact_state", 0.1)])
def test_resume_preserves_selected_recipe_parameters_and_parent_history(tmp_path, mode, expected_rate):
    backend = NonexecutingBackend()
    service = OperationService(tmp_path / "service.db", backend=backend)
    try:
        p = project(service, training_recipe())
        parent = service.invoke("run.submit", {"project_id": p["project_id"], "recipe_id": p["recipe_id"],
            "expected_revision": 1, "trusted": True, "idempotency_key": "parent"}, OWNER)
        edited = deepcopy(service._current_recipe(p["project_id"]))
        edited["nodes"][0]["parameters"]["learning_rate"] = 0.01
        edited = service.invoke("recipe.update", {"project_id": p["project_id"], "recipe_id": p["recipe_id"],
            "expected_revision": 1, "recipe": edited, "idempotency_key": "edit"}, OWNER)
        resumed = service.invoke("run.resume", {"project_id": p["project_id"], "operation_id": parent["operation_id"],
            "mode": mode, "expected_recipe_revision": 2, "steps": 6, "trusted": True,
            "idempotency_key": "warm-start"}, OWNER)
        assert resumed["recipe_id"] == p["recipe_id"]
        assert service._current_recipe(p["project_id"])["nodes"][0]["parameters"]["learning_rate"] == expected_rate
        assert service._current_recipe(p["project_id"])["nodes"][0]["parameters"]["steps"] == 6
        assert service._get("recipe", p["recipe_id"], p["project_id"], 1)["nodes"][0]["parameters"] == {"steps": 4, "learning_rate": 0.1}
        assert service._get("recipe", p["recipe_id"], p["project_id"], 2) == edited
        assert backend.calls[-1][1]["recipe"]["nodes"][0]["parameters"]["learning_rate"] == expected_rate
    finally:
        service.store.close()


def test_export_and_publication_preview_reject_dirty_git_provenance(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    (root / "model.py").write_text("value = 'committed'\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=Review", "-c", "user.email=review@example.invalid", "commit", "-qm", "Pinned fixture"]):
        subprocess.run(["git", "-C", str(root), *args], check=True)
    committed = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    (root / "model.py").write_text("value = 'uncommitted'\n")
    recipe = default_recipe("template", "template", "source")
    recipe["source"] = {"repository": "https://example.invalid/model", "commit": committed,
        "files": [{"path": "model.py", "sha256": hashlib.sha256((root / "model.py").read_bytes()).hexdigest()}]}
    recipe["licenses"] = [{"scope": "source", "license": "MIT", "redistribution_allowed": True}]
    service = OperationService(tmp_path / "service.db", source_roots={"approved": {"path": str(root), "trusted": True}})
    try:
        p = project(service, recipe)
        service.invoke("project.source.bind", {"project_id": p["project_id"], "source_alias": "approved", "trusted": True,
            "source_repository_id": service.source_workspaces.inspect("approved")["repository_id"],
            "expected_revision": 1, "idempotency_key": "bind"}, OWNER)
        status = service.invoke("project.source.status", {"project_id": p["project_id"]}, OWNER)
        assert status["pinned_source_matches"] is False
        assert status["diagnostics"][0]["code"] == "dependency_mismatch"
        fields = {"project_id": p["project_id"], "recipe_id": p["recipe_id"]}
        with pytest.raises(ServiceError):
            service.invoke("recipe.export", fields, OWNER)
        with pytest.raises(ServiceError):
            service.invoke("recipe.publish.preview", {**fields, "registry_id": "local-private", "expected_revision": 1,
                "package_name": "review-fixture", "package_version": "1.0.0", "visibility": "private",
                "metadata": {"title": "Review", "description": "", "tags": []}}, OWNER)
    finally:
        service.store.close()


@pytest.mark.parametrize("training_nodes", [0, 2])
def test_warm_start_rejects_ambiguous_current_builtin_trainer_without_revision_write(tmp_path, training_nodes):
    backend = NonexecutingBackend()
    service = OperationService(tmp_path / "service.db", backend=backend)
    try:
        p = project(service, training_recipe())
        parent = service.invoke("run.submit", {"project_id": p["project_id"], "recipe_id": p["recipe_id"],
            "expected_revision": 1, "trusted": True, "idempotency_key": "parent"}, OWNER)
        edited = deepcopy(service._current_recipe(p["project_id"]))
        edited["nodes"] = [{**deepcopy(edited["nodes"][0]), "id": "train" + str(index)}
                           for index in range(training_nodes)]
        saved = service.invoke("recipe.update", {"project_id": p["project_id"], "recipe_id": p["recipe_id"],
            "expected_revision": 1, "recipe": edited, "idempotency_key": "edit"}, OWNER)
        with pytest.raises(ServiceError, match="exactly one checkpoint-consuming built-in training node"):
            service.invoke("run.resume", {"project_id": p["project_id"], "operation_id": parent["operation_id"],
                "mode": "warm_start", "expected_recipe_revision": 2, "steps": 6, "trusted": True,
                "idempotency_key": "warm-start"}, OWNER)
        assert service._current_recipe(p["project_id"]) == saved
        assert all(operation != "run.resume" for operation, _ in backend.calls)
    finally:
        service.store.close()
