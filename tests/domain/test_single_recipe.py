"""Independent one-project/one-model/one-recipe invariant regressions.

The backend seam is deliberately nonexecuting. These tests establish domain,
transaction, history and migration guarantees, not native training acceptance.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import sqlite3
from threading import Barrier
from uuid import uuid4

import pytest

from modelbuilder.models import Actor, ServiceError, utcnow
from modelbuilder.recipe import default_recipe, export_bundle, normalize_recipe, recipe_digest
from modelbuilder.service import OperationService


OWNER = Actor("single-recipe-auditor", {"read", "modify", "execute"})


class NonexecutingBackend:
    def __init__(self):
        self.calls = []
        self.fail_resume = False

    def invoke(self, operation, payload, actor):
        self.calls.append((operation, deepcopy(payload)))
        if operation == "run.resume" and self.fail_resume:
            raise ServiceError("fixture_backend_failure", "Nonexecuting resume failure", status_code=502)
        return {"operation_id": payload.get("operation_id"), "project_id": payload["project_id"],
                "backend_run_id": f"native-{payload.get('operation_id', 'audit')}",
                "status": "submitted", "worker_alive": False}


@pytest.fixture
def service(tmp_path):
    instance = OperationService(tmp_path / "domain.sqlite3", backend=NonexecutingBackend())
    yield instance
    instance.store.close()


def project(service, slug="single"):
    return service.invoke("project.create", {"slug": slug, "model_name": slug,
                          "idempotency_key": f"create-{slug}"}, OWNER)


def canonical(service, project_id):
    snapshot = service.invoke("project.open", {"project_id": project_id}, OWNER)
    current = snapshot["recipe"]
    listed = service.invoke("recipe.list", {"project_id": project_id}, OWNER)["recipes"]
    assert len(listed) == 1
    assert listed[0] == current
    assert snapshot["recipes"] == [current]
    assert snapshot["project"]["active_recipe_id"] == current["recipe_id"]
    assert current["project_id"] == project_id
    return current


def template(name="Template", steps=None):
    body = default_recipe("template-project", "template-recipe", name)
    if steps is not None:
        body["nodes"] = [{"id": "train", "kind": "model.train",
            "implementation_ref": "builtin:model.train", "inputs": [], "outputs": [],
            "parameters": {"steps": steps, "marker": "historical configuration"},
            "environment_ref": "default", "resource_request": {},
            "retry": {"max_attempts": 1, "backoff_seconds": 0}, "timeout": 30,
            "cache_policy": "disabled", "provenance": {}}]
    return body


def revise(service, project_id, current, name="Revised", steps=None, key="revise"):
    body = template(name, steps)
    body.update(project_id=project_id, recipe_id=current["recipe_id"], revision=current["revision"])
    return service.invoke("recipe.update", {"project_id": project_id, "recipe_id": current["recipe_id"],
        "expected_revision": current["revision"], "recipe": body, "idempotency_key": key}, OWNER)


def historical(service, project_id, recipe_id, revision):
    return service.invoke("recipe.get", {"project_id": project_id, "recipe_id": recipe_id,
                          "revision": revision}, OWNER)


def test_project_creation_atomically_owns_one_stable_recipe_and_restart(service):
    created = project(service)
    current = canonical(service, created["project_id"])
    assert current["recipe_id"] == created["active_recipe_id"]
    assert current["revision"] == 1
    reopened = OperationService(service.store.path)
    try:
        assert canonical(reopened, created["project_id"]) == current
    finally:
        reopened.store.close()


def test_invalid_initial_recipe_cannot_leave_orphan_project_or_binding(service):
    body = template()
    body["schema_version"] = "unknown"
    payload = {"slug": "atomic", "model_name": "Atomic model", "recipe": body,
               "idempotency_key": "atomic-project"}
    with pytest.raises(ServiceError) as error:
        service.invoke("project.create", payload, OWNER)
    assert error.value.code == "unsupported_schema_version"
    assert service.store.list("project") == []
    assert service.store.list("recipe") == []
    assert service.store.connection.execute("SELECT COUNT(*) FROM project_recipes").fetchone()[0] == 0
    created = service.invoke("project.create", {**payload, "recipe": template()}, OWNER)
    current = canonical(service, created["project_id"])
    assert current["revision"] == 1


@pytest.mark.parametrize("operations", [("recipe.create",) * 6,
                                      ("recipe.update", "recipe.import", "recipe.clone") * 2])
def test_concurrent_clients_cannot_create_multiple_recipes_or_lose_revisions(service, operations):
    created = project(service)
    pid = created["project_id"]
    before = canonical(service, pid)
    bundle = export_bundle(template("Imported template"))
    def mutate(item):
        index, operation = item
        other = OperationService(service.store.path)
        payload = {"project_id": pid, "expected_revision": before["revision"],
                   "idempotency_key": f"concurrent-{index}"}
        if operation == "recipe.import":
            payload["bundle"] = bundle
        elif operation == "recipe.clone":
            payload.update(recipe_id=before["recipe_id"], source_revision=before["revision"], name="History apply")
        else:
            payload["recipe"] = template(f"Client {index}")
            if operation == "recipe.update":
                payload["recipe_id"] = before["recipe_id"]
                payload["recipe"].update(project_id=pid, recipe_id=before["recipe_id"], revision=before["revision"])
        try:
            return other.invoke(operation, payload, OWNER)
        except ServiceError as error:
            return error
        finally:
            other.store.close()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(mutate, enumerate(operations)))
    accepted = [result for result in results if isinstance(result, dict)]
    rejected = [result for result in results if isinstance(result, ServiceError)]
    assert len(accepted) == 1
    assert len(rejected) == 5
    assert all(error.status_code == 409 for error in rejected)
    after = canonical(service, pid)
    assert after["recipe_id"] == before["recipe_id"]
    assert after["revision"] == before["revision"] + 1
    assert historical(service, pid, before["recipe_id"], before["revision"]) == before
    assert len(service.store.list("recipe", pid)) == 1


def test_update_import_create_and_clone_are_revisioned_same_identity(service):
    pid = project(service)["project_id"]
    original = canonical(service, pid)
    current = original
    for index, operation in enumerate(["recipe.update", "recipe.import", "recipe.create", "recipe.clone"], 1):
        payload = {"project_id": pid, "expected_revision": current["revision"],
                   "idempotency_key": f"sequential-{index}"}
        if operation == "recipe.import":
            payload["bundle"] = export_bundle(template("Imported"))
        elif operation == "recipe.clone":
            payload.update(recipe_id=original["recipe_id"], source_revision=original["revision"], name="Restored draft")
        else:
            payload["recipe"] = template(f"Version {index}")
            if operation == "recipe.update":
                payload["recipe_id"] = current["recipe_id"]
                payload["recipe"].update(project_id=pid, recipe_id=current["recipe_id"], revision=current["revision"])
        updated = service.invoke(operation, payload, OWNER)
        assert updated["recipe_id"] == original["recipe_id"]
        assert updated["project_id"] == pid
        assert updated["revision"] == current["revision"] + 1
        assert service.invoke(operation, payload, OWNER) == updated
        assert historical(service, pid, current["recipe_id"], current["revision"]) == current
        current = canonical(service, pid)
    assert current["nodes"] == original["nodes"]
    assert len(service.store.list("recipe", pid)) == 1


@pytest.mark.parametrize("pointer", [None, "a-different-recipe"])
def test_project_update_cannot_detach_or_replace_canonical_identity(service, pointer):
    created = project(service)
    before = canonical(service, created["project_id"])
    with pytest.raises(ServiceError):
        service.invoke("project.update", {"project_id": created["project_id"],
            "expected_revision": created["revision"], "changes": {"active_recipe_id": pointer},
            "idempotency_key": "detach"}, OWNER)
    assert canonical(service, created["project_id"])["recipe_id"] == before["recipe_id"]


def submitted_then_edited(service):
    pid = project(service)["project_id"]
    initial = canonical(service, pid)
    trained = revise(service, pid, initial, "Original training", steps=4)
    run = service.invoke("run.submit", {"project_id": pid, "recipe_id": trained["recipe_id"],
        "expected_revision": trained["revision"], "trusted": True, "idempotency_key": "train"}, OWNER)
    latest = revise(service, pid, trained, "Newer unsent configuration", steps=9, key="edit-after-submit")
    return pid, trained, run, latest


def test_later_edit_cannot_mutate_historical_run_recipe(service):
    pid, trained, run, latest = submitted_then_edited(service)
    assert run["recipe_id"] == latest["recipe_id"]
    assert run["recipe_revision"] == trained["revision"]
    assert run["recipe_digest"] == trained["digest"]
    assert historical(service, pid, trained["recipe_id"], trained["revision"]) == trained
    stored = service.store.get("operation", run["operation_id"])
    assert stored["recipe_id"] == trained["recipe_id"]
    assert stored["recipe_revision"] == trained["revision"]
    assert stored["recipe_digest"] == trained["digest"]
    reopened = OperationService(service.store.path)
    try:
        persisted = reopened.invoke("operation.get", {"project_id": pid, "operation_id": run["operation_id"]}, OWNER)
        assert persisted["recipe_revision"] == trained["revision"]
        assert persisted["recipe_digest"] == trained["digest"]
        assert canonical(reopened, pid) == latest
    finally:
        reopened.store.close()


def test_resume_revises_same_canonical_identity_and_preserves_parent_and_newer_history(service):
    pid, parent, run, latest = submitted_then_edited(service)
    payload = {"project_id": pid, "operation_id": run["operation_id"], "mode": "exact_state",
               "steps": 6, "trusted": True, "expected_recipe_revision": latest["revision"],
               "idempotency_key": "resume"}
    resumed = service.invoke("run.resume", payload, OWNER)
    assert resumed["recipe_id"] == parent["recipe_id"] == latest["recipe_id"]
    assert resumed["recipe_revision"] == latest["revision"] + 1
    current = canonical(service, pid)
    assert current["recipe_id"] == parent["recipe_id"]
    assert current["revision"] == resumed["recipe_revision"]
    assert current["nodes"][0]["parameters"]["steps"] == 6
    assert historical(service, pid, parent["recipe_id"], parent["revision"]) == parent
    assert historical(service, pid, latest["recipe_id"], latest["revision"]) == latest
    assert service.invoke("run.resume", payload, OWNER) == resumed
    assert len(service.store.list("recipe", pid)) == 1


def test_stale_resume_fails_before_backend_or_recipe_mutation(service):
    pid, parent, run, latest = submitted_then_edited(service)
    service.backend.calls.clear()
    with pytest.raises(ServiceError) as error:
        service.invoke("run.resume", {"project_id": pid, "operation_id": run["operation_id"],
            "mode": "exact_state", "steps": 6, "trusted": True,
            "expected_recipe_revision": parent["revision"], "idempotency_key": "stale-resume"}, OWNER)
    assert error.value.status_code == 409
    assert service.backend.calls == []
    assert canonical(service, pid) == latest


def test_failed_resume_rolls_back_canonical_revision(service):
    pid, _, run, latest = submitted_then_edited(service)
    service.backend.fail_resume = True
    with pytest.raises(ServiceError) as error:
        service.invoke("run.resume", {"project_id": pid, "operation_id": run["operation_id"],
            "mode": "exact_state", "steps": 6, "trusted": True,
            "expected_recipe_revision": latest["revision"], "idempotency_key": "failed-resume"}, OWNER)
    assert error.value.code == "fixture_backend_failure"
    assert canonical(service, pid) == latest
    with pytest.raises(ServiceError):
        historical(service, pid, latest["recipe_id"], latest["revision"] + 1)


def legacy_database(path, active=True):
    """Write the old schema directly, so the migration is genuinely exercised."""
    pid = str(uuid4())
    now = utcnow()
    project_body = {"schema_version": "0", "project_id": pid, "id": pid, "slug": "legacy",
        "model_name": "Legacy model", "description": "", "created_at": now, "updated_at": now,
        "revision": 1, "git_remote": None, "head_commit": None, "active_model_version_ref": None,
        "research_watch_config": None, "status": "active",
        "active_recipe_id": active if isinstance(active, str) else "legacy-b" if active else None}
    versions = []
    for identity, revision, name in [("legacy-a", 1, "Historical run source"),
                                     ("legacy-a", 2, "Later unused change"),
                                     ("legacy-b", 1, "Explicit current recipe")]:
        body = template(name, steps=revision + 3)
        body.update(project_id=pid, recipe_id=identity, revision=revision)
        body = normalize_recipe(body)
        versions.append({**body, "digest": recipe_digest(body)})
    operation = {"operation_id": str(uuid4()), "project_id": pid, "backend_run_id": "legacy-native-run",
        "recipe_id": "legacy-a", "recipe_revision": 1, "recipe_digest": versions[0]["digest"],
        "created_at": now, "last_reconciled_at": now, "revision": 1, "status": "succeeded", "errors": []}
    connection = sqlite3.connect(path)
    connection.executescript("""
      CREATE TABLE resources (kind TEXT NOT NULL, id TEXT NOT NULL, project_id TEXT NOT NULL,
        revision INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(kind,id));
      CREATE TABLE versions (kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
        body TEXT NOT NULL, PRIMARY KEY(kind,id,revision));
    """)
    for kind, identity, body in [("project", pid, project_body), ("recipe", "legacy-a", versions[1]),
                                 ("recipe", "legacy-b", versions[2]),
                                 ("operation", operation["operation_id"], operation)]:
        connection.execute("INSERT INTO resources VALUES(?,?,?,?,?)",
                           (kind, identity, pid, body["revision"], json.dumps(body)))
    for kind, identity, body in [("project", pid, project_body), ("operation", operation["operation_id"], operation),
                                 *[("recipe", body["recipe_id"], body) for body in versions]]:
        connection.execute("INSERT INTO versions VALUES(?,?,?,?)", (kind, identity, body["revision"], json.dumps(body)))
    connection.commit()
    connection.close()
    return pid, versions, operation


def test_legacy_migration_preserves_every_historical_revision_and_operation(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    pid, versions, operation = legacy_database(path)
    for _ in range(2):
        service = OperationService(path, backend=NonexecutingBackend())
        try:
            current = canonical(service, pid)
            assert current["recipe_id"] == "legacy-b"
            for old in versions:
                assert historical(service, pid, old["recipe_id"], old["revision"]) == old
            assert service.invoke("operation.get", {"project_id": pid,
                                  "operation_id": operation["operation_id"]}, OWNER) == operation
            report = service.invoke("recipe.history", {"project_id": pid}, OWNER)
            assert "legacy-a" in json.dumps(report)
            assert "legacy-b" in json.dumps(report)
            with pytest.raises(ServiceError):
                service.invoke("recipe.get", {"project_id": pid, "recipe_id": "legacy-a"}, OWNER)
            for name, payload in [
                ("recipe.update", {"recipe_id": "legacy-a", "expected_revision": 2, "recipe": versions[1]}),
                ("recipe.clone", {"recipe_id": "legacy-a", "expected_revision": current["revision"]}),
                ("run.submit", {"recipe_id": "legacy-a", "expected_revision": 2, "trusted": True}),
            ]:
                with pytest.raises(ServiceError):
                    service.invoke(name, {"project_id": pid, **payload, "idempotency_key": f"retired-{name}"}, OWNER)
            assert service.backend.calls == []
        finally:
            service.store.close()


def test_concurrent_restarts_migrate_legacy_history_once_without_duplicate_revisions(tmp_path):
    path = tmp_path / "concurrent-legacy.sqlite3"
    pid, versions, operation = legacy_database(path)
    barrier = Barrier(6)

    def reopen(_):
        barrier.wait()
        service = OperationService(path)
        try:
            current = canonical(service, pid)
            for old in versions:
                assert historical(service, pid, old["recipe_id"], old["revision"]) == old
            assert service.invoke("operation.get", {"project_id": pid,
                "operation_id": operation["operation_id"]}, OWNER) == operation
            return current
        finally:
            service.store.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(reopen, range(6)))
    assert all(result == versions[2] for result in results)
    service = OperationService(path)
    try:
        assert len(service.store.resource_versions("project", pid)) == 2
        assert len(service.store.resource_versions("recipe", "legacy-a")) == 2
        assert len(service.store.resource_versions("recipe", "legacy-b")) == 1
        migrations = [item for item in service.store.history(pid)
                      if item["operation"] == "project.recipe_migrate"]
        assert len(migrations) == 1
        assert service.store.connection.execute("SELECT COUNT(*) FROM project_recipes").fetchone()[0] == 1
    finally:
        service.store.close()


def test_ambiguous_migration_requires_explicit_review_before_execution(tmp_path):
    path = tmp_path / "ambiguous.sqlite3"
    pid, versions, _ = legacy_database(path, active=False)
    service = OperationService(path, backend=NonexecutingBackend())
    try:
        current = canonical(service, pid)
        with pytest.raises(ServiceError):
            service.invoke("run.submit", {"project_id": pid, "recipe_id": current["recipe_id"],
                "expected_revision": current["revision"], "trusted": True,
                "idempotency_key": "unreviewed-execution"}, OWNER)
        assert service.backend.calls == []
        reviewed = service.invoke("recipe.update", {"project_id": pid, "recipe_id": current["recipe_id"], "recipe": current,
            "expected_revision": current["revision"], "idempotency_key": "review-migration"}, OWNER)
        submitted = service.invoke("run.submit", {"project_id": pid, "recipe_id": reviewed["recipe_id"],
            "expected_revision": reviewed["revision"], "trusted": True, "idempotency_key": "reviewed-execution"}, OWNER)
        assert submitted["recipe_id"] == current["recipe_id"]
        for old in versions:
            assert historical(service, pid, old["recipe_id"], old["revision"]) == old
    finally:
        service.store.close()


def save_legacy_idempotency(path, operation, payload, result):
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE IF NOT EXISTS idempotency (
        actor_id TEXT NOT NULL, key TEXT NOT NULL, operation TEXT NOT NULL,
        payload_hash TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(actor_id,key))""")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    connection.execute("INSERT INTO idempotency VALUES(?,?,?,?,?)", (OWNER.id, payload["idempotency_key"],
                       operation, digest, json.dumps(result)))
    connection.commit()
    connection.close()


def test_legacy_clone_retry_cannot_return_an_archived_recipe_as_current_success(tmp_path):
    path = tmp_path / "legacy-idempotency.sqlite3"
    pid, versions, _ = legacy_database(path, active="legacy-a")
    payload = {"project_id": pid, "recipe_id": "legacy-a", "expected_revision": 1,
               "name": "Explicit current recipe", "idempotency_key": "legacy-clone"}
    save_legacy_idempotency(path, "recipe.clone", payload, versions[2])
    service = OperationService(path)
    try:
        current = canonical(service, pid)
        try:
            result = service.invoke("recipe.clone", payload, OWNER)
        except ServiceError as error:
            assert error.status_code == 409
        else:
            assert result["recipe_id"] == current["recipe_id"]
        assert canonical(service, pid) == current
        assert historical(service, pid, "legacy-b", 1) == versions[2]
    finally:
        service.store.close()


def test_legacy_project_create_retry_exposes_canonical_recipe_or_explicit_conflict(tmp_path):
    path = tmp_path / "legacy-project-idempotency.sqlite3"
    pid, _, _ = legacy_database(path)
    connection = sqlite3.connect(path)
    legacy_project = json.loads(connection.execute("SELECT body FROM resources WHERE kind='project'").fetchone()[0])
    connection.close()
    payload = {"slug": "legacy", "model_name": "Legacy model", "idempotency_key": "legacy-project-create"}
    save_legacy_idempotency(path, "project.create", payload, {**legacy_project, "active_recipe_id": None})
    service = OperationService(path)
    try:
        current = canonical(service, pid)
        try:
            result = service.invoke("project.create", payload, OWNER)
        except ServiceError as error:
            assert error.status_code == 409
        else:
            assert result.get("recipe_id") == current["recipe_id"]
            assert result.get("active_recipe_id") == current["recipe_id"]
        assert canonical(service, pid) == current
    finally:
        service.store.close()


def test_legacy_run_retry_preserves_original_identity_without_new_execution(tmp_path):
    path = tmp_path / "legacy-run-idempotency.sqlite3"
    pid, _, operation = legacy_database(path)
    payload = {"project_id": pid, "recipe_id": "legacy-a", "expected_revision": 1,
               "trusted": True, "idempotency_key": "legacy-submission"}
    save_legacy_idempotency(path, "run.submit", payload, operation)
    backend = NonexecutingBackend()
    service = OperationService(path, backend=backend)
    try:
        assert canonical(service, pid)["recipe_id"] == "legacy-b"
        assert service.invoke("run.submit", payload, OWNER) == operation
        assert backend.calls == []
        assert service.invoke("operation.get", {"project_id": pid,
            "operation_id": operation["operation_id"]}, OWNER) == operation
        assert canonical(service, pid)["recipe_id"] == "legacy-b"
    finally:
        service.store.close()


def test_legacy_parent_resume_creates_revision_of_current_id_without_rewriting_parent(tmp_path):
    path = tmp_path / "legacy-parent-resume.sqlite3"
    pid, versions, operation = legacy_database(path)
    service = OperationService(path, backend=NonexecutingBackend())
    try:
        current = canonical(service, pid)
        resumed = service.invoke("run.resume", {"project_id": pid, "operation_id": operation["operation_id"],
            "expected_recipe_revision": current["revision"], "mode": "exact_state", "steps": 8,
            "trusted": True, "idempotency_key": "legacy-parent-resume"}, OWNER)
        assert resumed["recipe_id"] == "legacy-b"
        assert resumed["recipe_revision"] == current["revision"] + 1
        assert canonical(service, pid)["nodes"][0]["parameters"]["steps"] == 8
        for old in versions:
            assert historical(service, pid, old["recipe_id"], old["revision"]) == old
        assert service.invoke("operation.get", {"project_id": pid,
            "operation_id": operation["operation_id"]}, OWNER) == operation
    finally:
        service.store.close()
