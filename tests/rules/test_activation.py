"""Persistent explicit user scopes survive recipe edits and action transport choice."""

from copy import deepcopy
import pytest
from modelbuilder.evaluation import digest
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService
from modelbuilder.rule_operations import enforce_activation
from modelbuilder.backend.rule_activation import recheck
from test_rules import policy


def active(tmp_path):
    service = OperationService(tmp_path / "service.sqlite")
    actor = Actor("owner", {"read", "modify"})
    project = service.invoke(
        "project.create", {"slug": "active", "model_name": "Active", "idempotency_key": "p"}, actor
    )
    row = service.invoke(
        "rules.policy.register",
        {"project_id": project["project_id"], "policy": policy(), "idempotency_key": "policy"},
        actor,
    )
    request = {
        "project_id": project["project_id"],
        "policy_digest": row["policy_digest"],
        "expected_revision": project["revision"],
        "operations": ["run.submit", "run.resume", "model.export.package", "model.version.promote"],
        "node_roles": ["training"],
        "idempotency_key": "activate",
    }
    project = service.invoke("rules.policy.activate", request, actor)
    return service, actor, project, request


def test_activation_is_explicit_version_fenced_and_permission_checked(tmp_path):
    service, actor, project, request = active(tmp_path)
    assert service.invoke("rules.policy.activate", request, actor) == project
    with pytest.raises(ServiceError) as error:
        service.invoke("rules.policy.activate", {**request, "idempotency_key": "stale"}, actor)
    assert error.value.code == "revision_conflict"
    with pytest.raises(ServiceError) as error:
        service.invoke(
            "rules.policy.deactivate",
            {
                "project_id": project["project_id"],
                "expected_revision": project["revision"],
                "idempotency_key": "deny",
            },
            Actor("owner", {"read"}),
        )
    assert error.value.code == "forbidden"
    service.store.close()


def test_recipe_edit_or_inert_import_cannot_remove_active_scope(tmp_path):
    service, actor, project, _ = active(tmp_path)
    pid = project["project_id"]
    guard = {
        "id": "guard",
        "kind": "component.run",
        "component": {"id": "modelbuilder.rules.gate", "role": "utility"},
        "parameters": {"policy": policy()},
        "outputs": [{"name": "allowed", "type": "bool"}],
    }
    train = {
        "id": "train",
        "kind": "model.train",
        "inputs": [],
        "outputs": [],
        "required_guards": ["guard"],
        "condition": {"source_node": "guard", "source_port": "allowed", "equals": True},
    }
    recipe = {"nodes": [guard, train], "edges": []}
    payload = {"recipe": recipe}
    enforce_activation(service, "run.submit", project, payload)
    recheck({"rule_authorization": payload["rule_authorization"]})
    for edited in (deepcopy(recipe), deepcopy(recipe)):
        action = edited["nodes"][1]
        action.pop("required_guards")
        action.pop("condition")
        with pytest.raises(ServiceError) as error:
            enforce_activation(service, "run.submit", project, {"recipe": edited})
        assert error.value.code == "active_rule_guard_required"
    changed = service.invoke(
        "rules.policy.deactivate",
        {"project_id": pid, "expected_revision": project["revision"], "idempotency_key": "deactivate"},
        actor,
    )
    with pytest.raises(ValueError, match="active_rule_policy_changed"):
        recheck({"rule_authorization": payload["rule_authorization"]})
    assert changed["rule_policy_activation"] is None
    service.store.close()


@pytest.mark.parametrize("operation", ["model.export.package", "model.version.promote"])
def test_standalone_actions_fail_closed_when_active_evidence_is_unsupported(tmp_path, operation):
    service, _, project, _ = active(tmp_path)
    with pytest.raises(ServiceError) as error:
        enforce_activation(service, operation, project, {})
    assert error.value.code == "active_rule_decision_required"
    assert project["rule_policy_activation"]["policy_digest"] == digest(policy())
    service.store.close()


def test_actual_recipe_update_and_import_keep_activation_and_block_unprotected_admission(tmp_path):
    from modelbuilder.recipe import default_recipe, export_bundle

    service, actor, project, _ = active(tmp_path)
    pid = project["project_id"]
    current = service.invoke("project.open", {"project_id": pid}, actor)["recipe"]
    # Authoring/import stays inert. Neither operation has authority to edit the
    # separately activated project safeguard.
    recipe = default_recipe(pid, current["recipe_id"])
    recipe["nodes"] = [
        {
            "id": "unprotected",
            "kind": "model.train",
            "implementation_ref": "builtin:model.train",
            "inputs": [],
            "outputs": [],
            "parameters": {},
            "environment_ref": "default",
            "resource_request": {"gpu": 0},
            "retry": {"max_attempts": 1, "backoff_seconds": 0},
            "timeout": 30,
            "cache_policy": "disabled",
            "provenance": {},
        }
    ]
    changed = service.invoke(
        "recipe.update",
        {
            "project_id": pid,
            "recipe_id": current["recipe_id"],
            "recipe": recipe,
            "expected_revision": current["revision"],
            "idempotency_key": "remove-guards",
        },
        actor,
    )
    assert (
        service.invoke("project.get", {"project_id": pid}, actor)["rule_policy_activation"]
        == project["rule_policy_activation"]
    )
    bundle = export_bundle(changed)
    imported = service.invoke(
        "recipe.import",
        {
            "project_id": pid,
            "bundle": bundle,
            "expected_revision": changed["revision"],
            "idempotency_key": "import-unguarded",
        },
        actor,
    )
    assert (
        service.invoke("project.get", {"project_id": pid}, actor)["rule_policy_activation"]
        == project["rule_policy_activation"]
    )
    # There need not be a configured backend: this must fail at rule admission,
    # before any execution backend or source implementation is reached.
    with pytest.raises(ServiceError) as error:
        service._execution(
            "run.plan", {"project_id": pid, "recipe_id": imported["recipe_id"]}, actor, prepare_only=True
        )
    assert error.value.code == "active_rule_guard_required"
    with pytest.raises(ServiceError) as error:
        service.invoke(
            "project.update",
            {
                "project_id": pid,
                "expected_revision": project["revision"],
                "changes": {"rule_policy_activation": None},
                "idempotency_key": "ordinary-update",
            },
            actor,
        )
    assert error.value.code == "validation_error"
    service.store.close()
