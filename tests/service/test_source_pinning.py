"""Reviewed source authoring is revisioned, inert, bounded and alias-scoped."""
from __future__ import annotations

from copy import deepcopy
import json
import subprocess

from fastapi.testclient import TestClient
import pytest

from modelbuilder.api import create_app
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService

OWNER = Actor("owner", {"read", "modify"})


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def context(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "model.py").write_text("raise RuntimeError('pinning must never execute source')\n")
    (root / "config.json").write_text('{"width":4}\n')
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Source fixture")
    service = OperationService(tmp_path / "state.sqlite", source_roots={"approved": {"path": str(root), "trusted": False}})
    identity = service.source_workspaces.inspect("approved")
    project = service.invoke("project.create", {"slug": "source-pin", "model_name": "Source pin",
        "source_alias": "approved", "source_repository_id": identity["repository_id"], "trusted": True,
        "idempotency_key": "project"}, OWNER)
    fields = {"project_id": project["project_id"], "recipe_id": project["recipe_id"], "expected_revision": 1,
        "expected_project_revision": 1, "source_repository_id": identity["repository_id"],
        "commit": git(root, "rev-parse", "HEAD"), "paths": ["model.py", "config.json"]}
    yield service, root, project, fields
    service.store.close()


def preview(service, fields):
    return service.invoke("recipe.source.pin.preview", fields, OWNER)


def apply(service, fields, reviewed, actor=OWNER, key="pin"):
    return service.invoke("recipe.source.pin", {**fields, "review_digest": reviewed["review_digest"],
        "confirm_pin": True, "idempotency_key": key}, actor)


def test_preview_apply_history_and_http_mcp_catalog_contract(context):
    service, root, project, fields = context
    before = service._current_recipe(project["project_id"])
    reviewed = preview(service, fields)
    assert reviewed["review"]["next_revision"] == 2
    assert reviewed["execution_allowed"] is False
    assert service._current_recipe(project["project_id"]) == before
    assert git(root, "status", "--porcelain") == ""
    result = apply(service, fields, reviewed)
    assert result["recipe_id"] == project["recipe_id"] and result["revision"] == 2
    assert result["recipe"]["source"]["commit"] == fields["commit"]
    assert result["recipe"]["digest"] == reviewed["review"]["proposed_recipe_digest"]
    assert result["execution_allowed"] is False and result["git_modified"] is False
    assert service._get("recipe", project["recipe_id"], project["project_id"], 1) == before
    assert service._get("project", project["project_id"], project["project_id"]) == project
    assert service.store.list("operation") == []
    client = TestClient(create_app(service, {"owner-test-token": OWNER}))
    response = client.post("/v1/operations/recipe.source.pin", json={**fields,
        "review_digest": reviewed["review_digest"], "confirm_pin": True, "idempotency_key": "pin"},
        headers={"Authorization": "Bearer owner-test-token"})
    assert response.status_code == 200 and response.json() == result
    assert {"recipe.source.pin", "recipe.source.pin.preview"} <= {op["name"] for op in service.catalog(OWNER)}


@pytest.mark.parametrize("change", ["recipe", "project", "identity", "review", "dirty", "commit", "untracked", "symlink", "escape"])
def test_stale_dirty_redirected_or_unreviewed_source_never_writes_recipe(context, change):
    service, root, project, fields = context
    reviewed = preview(service, fields)
    proposed = deepcopy(fields)
    if change == "recipe":
        body = service._current_recipe(project["project_id"])
        service.invoke("recipe.update", {"project_id": project["project_id"], "recipe_id": project["recipe_id"],
            "expected_revision": 1, "recipe": {**body, "name": "new"}, "idempotency_key": "edit"}, OWNER)
    elif change == "project":
        service.invoke("project.source.bind", {"project_id": project["project_id"], "expected_revision": 1,
            "source_alias": "approved", "source_repository_id": fields["source_repository_id"],
            "trusted": True, "idempotency_key": "rebind"}, OWNER)
    elif change == "identity":
        proposed["source_repository_id"] = "sha256:" + "0" * 64
    elif change == "review":
        reviewed["review_digest"] = "0" * 64
    elif change == "dirty":
        (root / "model.py").write_text("value='not committed'\n")
    elif change == "commit":
        proposed["commit"] = "a" * 40
    elif change == "untracked":
        (root / "new.py").write_text("value=1\n")
        proposed["paths"].append("new.py")
    elif change == "symlink":
        (root / "link.py").symlink_to(root / "model.py")
        proposed["paths"].append("link.py")
    else:
        proposed["paths"] = ["../outside.py"]
    before = service._current_recipe(project["project_id"])
    with pytest.raises(ServiceError):
        apply(service, proposed, reviewed)
    assert service._current_recipe(project["project_id"]) == before
    assert service.store.list("operation") == []


def test_cached_pin_receipt_reauthorizes_alias_without_rechecking_historical_checkout(context):
    service, root, project, fields = context
    reviewed = preview(service, fields)
    result = apply(service, fields, reviewed)
    root.rename(root.with_name("moved"))
    assert apply(service, fields, reviewed) == result
    service.source_workspaces.aliases["approved"]["project_ids"] = []
    with pytest.raises(ServiceError) as caught:
        apply(service, fields, reviewed)
    assert caught.value.code == "source_not_allowed"
    assert service._current_recipe(project["project_id"])["revision"] == 2


@pytest.mark.parametrize("actor", [Actor("owner", {"read"}), Actor("owner", {"read", "modify"}, project_ids={"elsewhere"})])
def test_pin_requires_modify_and_project_authority(context, actor):
    service, _, project, fields = context
    reviewed = preview(service, fields)
    with pytest.raises(ServiceError) as caught:
        apply(service, fields, reviewed, actor)
    assert caught.value.code == "forbidden"
    assert service._current_recipe(project["project_id"])["revision"] == 1


def test_recipe_implementation_and_lock_closure_cannot_be_dropped(context):
    service, _, project, fields = context
    first = apply(service, fields, preview(service, fields))["recipe"]
    lock = next(item for item in first["source"]["files"] if item["path"] == "config.json")
    incoming = {**first, "environment": {**first["environment"], "lock_file": lock}}
    service.invoke("recipe.update", {"project_id": project["project_id"], "recipe_id": project["recipe_id"],
        "expected_revision": 2, "recipe": incoming, "idempotency_key": "lock"}, OWNER)
    with pytest.raises(ServiceError) as caught:
        preview(service, {**fields, "expected_revision": 3, "paths": ["model.py"]})
    assert caught.value.code == "source_closure_incomplete"


def test_incomplete_unbound_source_cannot_pin(context):
    service, _, _, fields = context
    other = service.invoke("project.create", {"slug": "unbound", "model_name": "Unbound", "idempotency_key": "unbound"}, OWNER)
    with pytest.raises(ServiceError) as caught:
        preview(service, {**fields, "project_id": other["project_id"], "recipe_id": other["recipe_id"]})
    assert caught.value.code == "source_binding_required"


def test_preview_digest_and_apply_are_deterministic_under_path_order(context):
    service, _, _, fields = context
    first = preview(service, fields)
    assert preview(service, {**fields, "paths": list(reversed(fields["paths"]))}) == first
    assert json.dumps(first, allow_nan=False)


def test_explicit_commit_hex_is_canonicalized_for_recipe_contract(context):
    service, _, _, fields = context
    assert preview(service, {**fields, "commit": fields["commit"].upper()}) == preview(service, fields)


def test_source_reads_disable_git_overrides_replacements_and_lazy_network(context, monkeypatch):
    service, root, _, fields = context
    run = subprocess.run
    calls = []
    def checked(*args, **kwargs):
        if args and args[0][0] == "git":
            calls.append(args[0])
            assert "protocol.allow=never" in args[0]
            assert kwargs["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"
            assert kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
            assert "GIT_DIR" not in kwargs["env"]
        return run(*args, **kwargs)
    monkeypatch.setenv("GIT_DIR", str(root / "not-approved"))
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "0")
    monkeypatch.setattr(subprocess, "run", checked)
    assert preview(service, fields)["review"]["after_source"]["commit"] == fields["commit"]
    assert calls
