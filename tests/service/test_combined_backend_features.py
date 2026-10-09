"""Cross-feature invariants for source routing, registry receipts and cost sidecar."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from modelbuilder.models import Actor, ServiceError
from modelbuilder.recipe import default_recipe
from modelbuilder.service import OperationService

OWNER = Actor("owner", {"read", "modify", "publish", "execute", "spend"})


def repository(path, value):
    path.mkdir()
    (path / "model.py").write_text(f"raise RuntimeError('inert registry source {value}')\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Fixture"]):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    return path


def manifest(root):
    result = default_recipe("template", "template", "Bound source")
    result["source"] = {"repository": "https://example.invalid/model.git",
                        "commit": subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
                        "files": [{"path": "model.py", "sha256": hashlib.sha256((root / "model.py").read_bytes()).hexdigest()}]}
    result["licenses"] = [{"scope": "source", "license": "MIT", "redistribution_allowed": True}]
    return result


@pytest.fixture
def setup(tmp_path):
    roots = {name: {"path": str(repository(tmp_path / name, name)), "trusted": True} for name in ("first", "second")}
    # An unrelated legacy root must never override a project's explicit binding.
    service = OperationService(tmp_path / "service.sqlite", source_root=roots["second"]["path"], source_roots=roots)
    yield service, roots
    if service._remote_cost_operations:
        service._remote_cost_operations.ledger.close()
    service.store.close()


def create(service, roots, alias):
    payload = {"slug": alias, "model_name": alias, "recipe": manifest(Path(roots[alias]["path"])),
               "source_alias": alias, "source_repository_id": service.source_workspaces.inspect(alias)["repository_id"],
               "trusted": True, "idempotency_key": "create-" + alias}
    return service.invoke("project.create", payload, OWNER), payload


def publication_fields(project):
    return {"registry_id": "local-private", "project_id": project["project_id"], "recipe_id": project["recipe_id"],
            "expected_revision": 1, "package_name": project["slug"], "package_version": "1.0.0", "visibility": "private",
            "metadata": {"title": "Bound source", "description": "Inert fixture", "tags": []}}


def enable(service, project):
    payload = {"project_id": project["project_id"], "changes": {"publication_enabled": True},
               "expected_revision": 1, "idempotency_key": "publish-policy-" + project["slug"]}
    return service.invoke("policy.update", payload, OWNER), payload


def test_publication_uses_project_bound_source_and_retains_immutable_bytes(setup):
    service, roots = setup
    first, _ = create(service, roots, "first")
    second, _ = create(service, roots, "second")
    enable(service, first)
    fields = publication_fields(first)
    preview = service.invoke("recipe.publish.preview", fields, OWNER)
    expected = hashlib.sha256((Path(roots["first"]["path"]) / "model.py").read_bytes()).hexdigest()
    assert preview["review"]["file_inventory"] == [{"path": "model.py", "sha256": expected}]
    published = service.invoke("recipe.publish", {**fields, "review_digest": preview["review_digest"],
        "confirm_publish": True, "idempotency_key": "publish"}, OWNER)
    alias = service.source_workspaces.inspect("second")
    rebound = service.invoke("project.source.bind", {"project_id": first["project_id"], "source_alias": "second",
        "source_repository_id": alias["repository_id"], "trusted": True, "expected_revision": 1, "idempotency_key": "rebind"}, OWNER)
    assert rebound["recipe_id"] == first["recipe_id"]
    with pytest.raises(ServiceError) as caught:
        service.invoke("recipe.publish.preview", fields, OWNER)
    assert caught.value.code == "source_hash_mismatch"
    retained = service.invoke("recipe.registry.get", {"registry_id": "local-private", "publication_id": published["publication_id"]}, OWNER)
    assert json.loads(retained["bundle_json"])["files"]["model.py"]["sha256"] == expected
    reader = Actor("other-project", {"read"}, project_ids={second["project_id"]})
    with pytest.raises(ServiceError) as caught:
        service.invoke("recipe.registry.get", {"registry_id": "local-private", "publication_id": published["publication_id"]}, reader)
    assert caught.value.code == "forbidden"


def test_publication_preview_respects_revoked_source_alias(setup):
    service, roots = setup
    project, _ = create(service, roots, "first")
    service.source_workspaces.aliases["first"]["project_ids"] = []
    with pytest.raises(ServiceError) as caught:
        service.invoke("recipe.publish.preview", publication_fields(project), OWNER)
    assert caught.value.code == "source_not_allowed"


def test_publication_policy_authorization_precedes_cached_replay(setup):
    service, roots = setup
    project, _ = create(service, roots, "first")
    original, payload = enable(service, project)
    assert service.invoke("policy.update", payload, OWNER) == original
    narrowed = Actor(OWNER.id, {"read", "modify"})
    with pytest.raises(ServiceError) as caught:
        service.invoke("policy.update", payload, narrowed)
    assert caught.value.code == "forbidden"


@pytest.mark.parametrize("operation", ["project.create", "project.source.bind"])
def test_source_alias_authorization_precedes_cached_replay_without_rewriting_history(setup, operation):
    service, roots = setup
    project, payload = create(service, roots, "first")
    original = project
    if operation == "project.source.bind":
        payload = {"project_id": project["project_id"], "source_alias": "first", "trusted": True,
                   "source_repository_id": project["source_binding"]["repository_id"], "expected_revision": 1,
                   "idempotency_key": "bind-again"}
        original = service.invoke(operation, payload, OWNER)
    # Cached receipts need authorization, not a newly live checkout.
    Path(roots["first"]["path"]).rename(Path(roots["first"]["path"]).with_name("moved"))
    assert service.invoke(operation, payload, OWNER) == original
    service.source_workspaces.aliases["first"]["project_ids"] = []
    with pytest.raises(ServiceError) as caught:
        service.invoke(operation, payload, OWNER)
    assert caught.value.code == "source_not_allowed"
    assert service.invoke("project.get", {"project_id": project["project_id"]}, OWNER) == original
    assert service.store.resource_versions("project", project["project_id"])[-1] == original


def test_registry_fork_preserves_target_identity_binding_and_inert_source_snapshot(setup):
    service, roots = setup
    source, _ = create(service, roots, "first")
    target, _ = create(service, roots, "second")
    enable(service, source)
    fields = publication_fields(source)
    preview = service.invoke("recipe.publish.preview", fields, OWNER)
    published = service.invoke("recipe.publish", {**fields, "review_digest": preview["review_digest"],
        "confirm_publish": True, "idempotency_key": "publish"}, OWNER)
    fields = {"registry_id": "local-private", "publication_id": published["publication_id"],
              "target": {"mode": "existing_project", "project_id": target["project_id"], "expected_revision": 1}}
    preview = service.invoke("recipe.fork.preview", fields, OWNER)
    result = service.invoke("recipe.fork", {**fields, "review_digest": preview["review_digest"],
        "confirm_import": True, "idempotency_key": "fork"}, OWNER)
    assert result["recipe_id"] == target["recipe_id"] and result["revision"] == 2
    assert result["project"]["source_binding"] == target["source_binding"]
    assert result["execution_allowed"] is False and result["import_report"]["files_materialized"] is False
    assert len(service.store.list("recipe", target["project_id"])) == 1
    assert service._get("recipe", target["recipe_id"], target["project_id"], 1)["digest"] != result["recipe"]["digest"]
    status = service.invoke("project.source.status", {"project_id": target["project_id"]}, OWNER)
    assert status["pinned_source_matches"] is False


def test_cost_sidecar_recovers_outer_transaction_failure_without_duplicate_policy(setup, monkeypatch):
    service, roots = setup
    project, _ = create(service, roots, "first")
    payload = {"project_id": project["project_id"], "idempotency_key": "cost-policy", "expected_revision": None,
               "policy": {"policy_id": "budget", "scope": "project", "project_id": project["project_id"],
                          "provider_id": "unconfigured", "account_ref": "acct_" + "a" * 32,
                          "maximum_total": {"currency": "USD", "minor_units": 123}, "max_runtime_seconds": 60}}
    save = service.store.save_idempotent
    with monkeypatch.context() as scoped:
        scoped.setattr(service.store, "save_idempotent", lambda *args: (_ for _ in ()).throw(RuntimeError("outer receipt failure")))
        with pytest.raises(RuntimeError, match="outer receipt"):
            service.invoke("cost.policy.put", payload, OWNER)
    assert service.store.save_idempotent == save
    result = service.invoke("cost.policy.put", payload, OWNER)
    assert result["policy"]["revision"] == 1 and result["remote_execution_enabled"] is False
    history = service.invoke("cost.history", {"project_id": project["project_id"]}, OWNER)["events"]
    assert len(history) == 1
    assert service.invoke("cost.policy.put", payload, OWNER) == result
