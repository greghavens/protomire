"""Git-free transport intake through real public APIs; synthetic test terms only."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from modelbuilder.backend import execution_rights as rights
from modelbuilder.backend.admission import BackendError
from modelbuilder.hub_rights import validate_execution_rights
from modelbuilder.models import Actor, OPERATIONS, ServiceError
from modelbuilder.recipe import export_bundle, import_bundle
from modelbuilder.service import OperationService

ROOT = Path(__file__).resolve().parents[2]
OWNER = Actor("synthetic-owner", frozenset({"read", "modify", "execute", "publish"}))
AGENT = Actor("synthetic-agent", frozenset({"read", "modify", "execute"}))
USAGE = {"purpose": "Self-authored synthetic intake regression", "commercial": False,
         "deployment": "isolated tests", "conditions": ["No user or third-party legal clearance"]}


class Intake:
    def __init__(self, root):
        self.root = root
        authored = root / "authored"
        authored.mkdir()
        names = ["src/modelbuilder/backend/fixtures.py", "requirements/backend.lock"]
        for name in names:
            path = authored / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / name).read_bytes())
        recipe = json.loads((ROOT / "examples/custom-pytorch/recipe.json").read_text())
        # Explicitly a declared test identity, never an authenticated Git claim.
        recipe["source"]["commit"] = "a" * 40
        recipe["source"]["files"] = [{"path": name, "sha256": hashlib.sha256((authored / name).read_bytes()).hexdigest()} for name in names]
        self.bundle = export_bundle(recipe, source_root=authored)
        self.source = root / "imported"
        import_bundle(self.bundle, self.source)
        (self.source / "TERMS.txt").write_text("Synthetic fixture terms: train, evaluate and export these test-authored bytes in isolated tests only.\n")
        self.db = root / "receiving.sqlite3"
        self.service = OperationService(db_path=self.db)
        project = self.service.invoke("project.create", {"slug": "snapshot", "model_name": "Snapshot fixture", "idempotency_key": "create"}, OWNER)
        self.pid = project["project_id"]
        self.recipe_id = project["recipe_id"]
        self.call("recipe.import", bundle_json=json.dumps(self.bundle), expected_revision=1)
        self.restart()
        identity = self.call("project.source.list")["sources"][0]
        self.call("project.source.bind", expected_revision=project["revision"], source_alias="snapshot",
                  source_repository_id=identity["repository_id"], trusted=True)

    def call(self, op, actor=OWNER, **payload):
        if op == "recipe.get":
            payload.setdefault("recipe_id", self.recipe_id)
        return self.service.invoke(op, {"project_id": self.pid, **payload,
            **({"idempotency_key": str(uuid4())} if OPERATIONS[op].mutating else {})}, actor)

    def restart(self, trusted=True):
        self.service.hub.close()
        self.service.store.close()
        self.service = OperationService(db_path=self.db, source_root=self.source,
            source_roots={"snapshot": {"path": str(self.source), "kind": "imported_snapshot", "trusted": trusted, "project_ids": [self.pid]}},
            hub_config={"publication_roots": {self.pid: {"snapshot": str(self.source)}}}, hub_providers={})

    def capture(self, **changes):
        project, recipe = self.call("project.get"), self.call("recipe.get")
        status = self.call("project.source.status")
        return self.call("hub.license.local.inspect", **{
            "source_alias": "snapshot", "repo_type": "source", "files": [item["path"] for item in recipe["source"]["files"]] + ["TERMS.txt"],
            "evidence_document_paths": ["TERMS.txt"], "max_bytes": 4 * 1024 * 1024, "declared_license": "unknown",
            "source_repository_id": status["binding"]["repository_id"], "source_commit": recipe["source"]["commit"],
            "recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"], "expected_recipe_digest": recipe["digest"],
            "expected_project_revision": project["revision"], **changes})

    def review_body(self, evidence):
        return {"evidence_id": evidence["evidence_id"], "evidence_digest": evidence["evidence_digest"],
                "license_document_paths": ["TERMS.txt"], "permitted_uses": ["train", "evaluate", "export"],
                "intended_context": "Self-authored fixture tests only", "required_files": [], "required_attributions": [],
                "allowed_destination_licenses": [], "gated_terms_acknowledged": False,
                "reviewed_usage_context": USAGE, "confirm_review": True}

    def review(self, evidence=None, actor=OWNER):
        return self.call("hub.license.review", actor=actor, **self.review_body(evidence or self.capture()))

    def bind(self, policy):
        recipe, project = self.call("recipe.get"), self.call("project.get")
        return self.call("hub.source.policy.bind", recipe_id=recipe["recipe_id"], expected_revision=recipe["revision"],
                         expected_project_revision=project["revision"], license_policy_id=policy["policy_id"],
                         intended_use="train", usage_context=USAGE)["recipe"]

    def native(self, recipe):
        backend = SimpleNamespace(repository=self.source, _execution_rights_authority=rights.bind_authority(
            self.pid, self.service.hub.policy_snapshot(self.pid), self.service.store.path))
        authorization = rights.prepare_authorization(backend, {"project_id": self.pid}, recipe, durable=True)
        node = next(item for item in recipe["nodes"] if item["kind"] == "model.train")
        return {"project_id": self.pid, "source": recipe["source"], "dependencies": recipe["resources"] + recipe["external_dependencies"],
                "rights_extensions": recipe["extensions"], "component_node": node, "rights_authorization": authorization}

    def close(self):
        self.service.hub.close()
        self.service.store.close()


@pytest.fixture
def intake(tmp_path):
    item = Intake(tmp_path)
    yield item
    item.close()


def test_gitfree_public_intake_binds_exact_snapshot_target_and_survives_restart(intake):
    assert not (intake.source / ".git").exists()
    status = intake.call("project.source.status")
    assert status["source_kind"] == "imported_snapshot" and status["pinned_source_matches"]
    assert status["git_provenance_verified"] is False and "head_commit" not in status
    evidence = intake.capture()
    origin = evidence["evidence"]["source_origin"]
    assert origin["snapshot_digest"] == intake.bundle["bundle_digest"]
    assert origin["transport_recipe_digest"] == intake.bundle["digest"]
    before = intake.call("recipe.get")
    assert origin["review_target"]["recipe_digest"] == before["digest"]
    assert origin["review_target"]["recipe_revision"] == before["revision"]
    assert origin["review_target"]["project_id"] == intake.pid
    with pytest.raises(ServiceError) as blocked:
        intake.review(evidence, actor=AGENT)
    assert blocked.value.code == "forbidden"
    assert intake.call("hub.license.policy.list")["policies"] == []
    policy = intake.review(evidence)
    recipe = intake.bind(policy)
    assert recipe["revision"] == before["revision"] + 1
    intake.restart()
    assert validate_execution_rights(recipe, "train", intake.service.hub.policy_snapshot(intake.pid))["policy_ids"] == [policy["policy_id"]]
    assert rights.verify_dispatch(intake.native(recipe))["requested_actions"] == ["train"]
    assert intake.call("hub.asset.list")["assets"] == []


@pytest.mark.parametrize("field,value", [("expected_revision", 1), ("expected_recipe_digest", "f" * 64),
                                        ("recipe_id", "unrelated"), ("expected_project_revision", 1)])
def test_capture_requires_exact_current_review_target(intake, field, value):
    with pytest.raises(ServiceError) as blocked:
        intake.capture(**{field: value})
    assert blocked.value.code == "license_source_changed"
    assert intake.call("hub.license.policy.list")["policies"] == []


@pytest.mark.parametrize("phase", ["review", "bind"])
@pytest.mark.parametrize("change", ["recipe", "project", "source", "terms", "manifest", "substitution", "file_symlink", "root_symlink"])
def test_stale_or_substituted_snapshot_never_finishes_review_or_binding(intake, phase, change):
    evidence = intake.capture()
    policy = intake.review(evidence) if phase == "bind" else None
    if change == "recipe":
        recipe = intake.call("recipe.get")
        recipe["description"] = "Changed during owner review"
        intake.call("recipe.update", recipe_id=recipe["recipe_id"], expected_revision=recipe["revision"], recipe=recipe)
    elif change == "project":
        project = intake.call("project.get")
        intake.call("project.source.bind", expected_revision=project["revision"], source_alias="snapshot",
                    source_repository_id=project["source_binding"]["repository_id"], trusted=True)
    elif change in {"source", "terms"}:
        (intake.source / ("TERMS.txt" if change == "terms" else "src/modelbuilder/backend/fixtures.py")).write_text("changed bytes\n")
    elif change == "manifest":
        path = intake.source / "recipe.json"
        body = json.loads(path.read_text())
        body["source"]["commit"] = "b" * 40
        path.write_text(json.dumps(body))
    elif change in {"substitution", "root_symlink"}:
        moved = intake.source.with_name("original-import")
        intake.source.rename(moved)
        if change == "substitution":
            shutil.copytree(moved, intake.source)
        else:
            intake.source.symlink_to(moved, target_is_directory=True)
    else:
        path = intake.source / "TERMS.txt"
        copied = intake.root / "terms-copy.txt"
        shutil.copyfile(path, copied)
        path.unlink()
        path.symlink_to(copied)
    before = intake.call("recipe.get")
    with pytest.raises((ServiceError, OSError, ValueError)):
        intake.bind(policy) if policy else intake.review(evidence)
    assert intake.call("recipe.get") == before
    if phase == "review":
        assert intake.call("hub.license.policy.list")["policies"] == []


def test_parameter_only_edits_keep_source_policy_but_context_and_revocation_block(intake):
    policy = intake.review()
    recipe = intake.bind(policy)
    training = next(item for item in recipe["nodes"] if item["kind"] == "model.train")
    training["parameters"]["steps"] += 1
    saved = intake.call("recipe.update", recipe_id=recipe["recipe_id"], expected_revision=recipe["revision"], recipe=recipe)
    assert rights.verify_dispatch(intake.native(saved))["requested_actions"] == ["train"]
    changed = deepcopy(saved)
    changed["extensions"]["usage_context"]["commercial"] = True
    with pytest.raises(ServiceError):
        validate_execution_rights(changed, "train", intake.service.hub.policy_snapshot(intake.pid))
    native = intake.native(saved)
    intake.call("hub.license.revoke", policy_id=policy["policy_id"], expected_revision=policy["revision"], reason="Synthetic owner revocation")
    with pytest.raises(BackendError):
        rights.verify_dispatch(native)
    with pytest.raises(BackendError):
        rights.recheck_authorization(native["rights_authorization"])


@pytest.mark.parametrize("change", ["source", "terms", "substitution", "symlink"])
def test_dispatch_rechecks_current_imported_snapshot_after_admission(intake, change):
    native = intake.native(intake.bind(intake.review()))
    if change in {"substitution", "symlink"}:
        moved = intake.source.with_name("original-import")
        intake.source.rename(moved)
        if change == "substitution":
            shutil.copytree(moved, intake.source)
        else:
            intake.source.symlink_to(moved, target_is_directory=True)
    else:
        (intake.source / ("TERMS.txt" if change == "terms" else "src/modelbuilder/backend/fixtures.py")).write_text("changed bytes\n")
    with pytest.raises(BackendError):
        rights.verify_dispatch(native)
    with pytest.raises(BackendError):
        rights.recheck_authorization(native["rights_authorization"])


def test_snapshot_is_explicit_and_arbitrary_directory_never_auto_trusted(intake):
    from modelbuilder.source_workspace import SourceWorkspaces
    untyped = SourceWorkspaces({"folder": {"path": str(intake.source)}})
    assert untyped.list()[0]["available"] is False
    intake.restart(trusted=False)
    status = intake.call("project.source.status")
    assert status["operator_trusted"] is False and status["available"]
    with pytest.raises(ServiceError) as blocked:
        intake.service.source_workspaces.resolve(status["binding"], intake.pid, execute=True)
    assert blocked.value.code == "untrusted_execution_blocked"
    assert intake.call("hub.license.policy.list")["policies"] == []


def test_snapshot_native_owner_queue_uses_exact_existing_review_operation(intake):
    sys.path.insert(0, str(ROOT / "distribution"))
    import managed_core as core
    from modelbuilder.hub import HubTransfers
    app = core.make_application(intake.root / "native-app", intake.root, {}, "a" * 64)
    service, queue, auth = app.state.service, app.state.review_queue, app.state.auth
    owner = auth.authenticate("Bearer " + "a" * 64)
    try:
        project = service.invoke("project.create", {"slug": "native-snapshot", "model_name": "Native snapshot", "idempotency_key": "create"}, owner)
        pid = project["project_id"]
        service.invoke("recipe.import", {"project_id": pid, "bundle_json": json.dumps(intake.bundle), "expected_revision": 1, "idempotency_key": "import"}, owner)
        from modelbuilder.source_workspace import SourceWorkspaces
        service.source_workspaces = SourceWorkspaces({"snapshot": {"path": str(intake.source), "kind": "imported_snapshot", "trusted": False, "project_ids": [pid]}})
        service.hub.close()
        service.hub = HubTransfers(service, config={"publication_roots": {pid: {"snapshot": str(intake.source)}}}, providers={})
        def call(op, **payload):
            if op == "recipe.get":
                payload.setdefault("recipe_id", project["recipe_id"])
            return service.invoke(op, {"project_id": pid, **payload,
                **({"idempotency_key": str(uuid4())} if OPERATIONS[op].mutating else {})}, owner)
        identity = call("project.source.list")["sources"][0]
        project = call("project.source.bind", expected_revision=1, source_alias="snapshot", source_repository_id=identity["repository_id"], trusted=True)
        recipe = call("recipe.get")
        evidence = call("hub.license.local.inspect", source_alias="snapshot", repo_type="source",
            files=[item["path"] for item in recipe["source"]["files"]] + ["TERMS.txt"], evidence_document_paths=["TERMS.txt"],
            max_bytes=4 * 1024 * 1024, declared_license="unknown", source_repository_id=identity["repository_id"],
            source_commit=recipe["source"]["commit"], recipe_id=recipe["recipe_id"], expected_revision=recipe["revision"],
            expected_recipe_digest=recipe["digest"], expected_project_revision=project["revision"])
        body = {"project_id": pid, **intake.review_body(evidence), "idempotency_key": str(uuid4())}
        with pytest.raises(ServiceError) as blocked:
            service.invoke("hub.license.review", body, owner)
        assert blocked.value.code == "forbidden"
        pending = queue.submit(owner, pid, "hub.license.review", body)
        assert call("hub.license.policy.list")["policies"] == []
        decision = queue.decide(owner, pending["id"], pending["review_digest"], "approve")
        assert decision["status"] == "succeeded" and decision["approved_by"] == owner.id
        assert decision["result"]["source_origin"]["snapshot_digest"] == intake.bundle["bundle_digest"]
        assert not queue.grants.active()
    finally:
        service.hub.close()
        service.store.close()


@pytest.mark.parametrize("omitted", ["src/modelbuilder/backend/fixtures.py", "requirements/backend.lock"])
def test_snapshot_review_rejects_executable_or_lock_omitted_from_source_inventory(intake, omitted):
    from modelbuilder.source_workspace import SourceWorkspaces
    manifest = deepcopy(intake.bundle["recipe"])
    if omitted.endswith("fixtures.py"):
        sha = next(item["sha256"] for item in manifest["source"]["files"] if item["path"] == omitted)
        manifest["nodes"][0].update(kind="code.python", implementation_ref={"path": omitted, "sha256": sha,
            "commit": manifest["source"]["commit"], "module": "modelbuilder.backend.fixtures", "entrypoint": "fixture"})
    manifest["source"]["files"] = [item for item in manifest["source"]["files"] if item["path"] != omitted]
    # Transport integrity alone is still valid: implementation/lock references
    # carry their own hashes. Source review must require their visible closure.
    transport = export_bundle(manifest, source_root=intake.source)
    assert omitted in transport["files"]
    root = intake.root / "incomplete-source-inventory"
    import_bundle(transport, root)
    workspace = SourceWorkspaces({"snapshot": {"path": str(root), "kind": "imported_snapshot"}})
    with pytest.raises(ServiceError) as blocked:
        workspace.inspect("snapshot")
    assert blocked.value.code == "source_snapshot_invalid"


@pytest.mark.parametrize("field", ["lock_file", "trainer_profile"])
def test_after_binding_new_current_lock_reference_requires_new_source_review(intake, field):
    policy = intake.review()
    recipe = intake.bind(policy)
    outside = intake.source / "replacement.lock"
    outside.write_text("Synthetic replacement lock outside reviewed closure\n")
    recipe["environment"][field] = {"path": "replacement.lock", "sha256": hashlib.sha256(outside.read_bytes()).hexdigest()}
    saved = intake.call("recipe.update", recipe_id=recipe["recipe_id"], expected_revision=recipe["revision"], recipe=recipe)
    with pytest.raises(ServiceError, match="outside the reviewed imported source closure"):
        validate_execution_rights(saved, "train", intake.service.hub.policy_snapshot(intake.pid))
    with pytest.raises(BackendError, match="outside the reviewed imported source closure"):
        intake.native(saved)
