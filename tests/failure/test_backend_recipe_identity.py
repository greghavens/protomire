"""Continuation identity contracts; these doubles are not execution evidence."""
from copy import deepcopy

import pytest

from modelbuilder.backend import BackendError, ZenMLBackend
from modelbuilder.backend.common import recipe_provenance


def parent_recipe():
    return {"recipe_id": "single-project-recipe", "project_id": "project", "revision": 2,
            "nodes": [{"id": "train", "kind": "model.train", "parameters": {"steps": 4}}]}


def boundary(tmp_path, monkeypatch):
    backend = ZenMLBackend(tmp_path)
    parent = {"project_id": "project", "recipe": parent_recipe()}
    monkeypatch.setattr(backend, "_job", lambda payload: (tmp_path, parent))
    monkeypatch.setattr(backend, "_rpc", lambda operation, payload: (
        {"status": "succeeded", "worker_alive": False} if operation == "run.status" else
        {"backend_run_id": "native-parent", "artifact_id": "native-checkpoint", "path": "/trusted/checkpoint"}))
    monkeypatch.setattr(backend, "submit", lambda request: request)
    return backend, parent


def payload(parent):
    revision = deepcopy(parent["recipe"])
    revision["revision"] += 1
    revision["nodes"][0]["parameters"]["steps"] = 6
    return {"project_id": "project", "project_recipe_id": revision["recipe_id"], "parent_operation_id": "parent-operation",
            "operation_id": "new-operation", "recipe": revision, "steps": 6, "mode": "exact_state", "trusted": True}


def test_continuation_preserves_identity_and_historical_revision(tmp_path, monkeypatch):
    backend, parent = boundary(tmp_path, monkeypatch)
    original = deepcopy(parent)
    result = backend.resume(payload(parent))
    assert result["recipe"]["recipe_id"] == original["recipe"]["recipe_id"]
    assert result["recipe"]["revision"] == 3
    assert result["parent_recipe_revision"] == 2
    assert result["parent_recipe_digest"] == recipe_provenance(original["recipe"])["recipe_digest"]
    assert result["executed_recipe_digest"] != result["parent_recipe_digest"]
    assert result["operation_id"] == "new-operation"
    assert result["parent_run_id"] == "native-parent"
    assert result["parent_checkpoint_ref"] == "native-checkpoint"
    assert parent == original


def test_continuation_cannot_create_second_recipe_identity(tmp_path, monkeypatch):
    backend, parent = boundary(tmp_path, monkeypatch)
    request = payload(parent)
    request["recipe"]["recipe_id"] = "another-independent-recipe"
    with pytest.raises(BackendError, match="single recipe identity"):
        backend.resume(request)


def test_changed_snapshot_cannot_reuse_same_revision(tmp_path, monkeypatch):
    backend, parent = boundary(tmp_path, monkeypatch)
    request = payload(parent)
    request["recipe"]["revision"] = parent["recipe"]["revision"]
    with pytest.raises(BackendError, match="new immutable revision"):
        backend.resume(request)


def test_legacy_parent_requires_explicit_canonical_mapping(tmp_path, monkeypatch):
    backend, parent = boundary(tmp_path, monkeypatch)
    request = payload(parent)
    request["project_recipe_id"] = request["recipe"]["recipe_id"] = "migrated-project-recipe"
    with pytest.raises(BackendError, match="legacy parent"):
        backend.resume(request)
    request["legacy_parent_recipe_id"] = parent["recipe"]["recipe_id"]
    result = backend.resume(request)
    assert result["recipe"]["recipe_id"] == "migrated-project-recipe"
    assert result["parent_recipe_id"] == "single-project-recipe"
    assert parent["recipe"]["recipe_id"] == "single-project-recipe"
