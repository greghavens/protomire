"""Actual filesystem SQLite acceptance for inert private recipe publication.

No mocked registry, public account, external network, training or paid resource.
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
from threading import Barrier

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
import pytest

from modelbuilder.api import create_app
from modelbuilder.models import Actor, ServiceError
from modelbuilder.recipe import default_recipe, import_bundle, parse_bundle_json
from modelbuilder.recipe_registry import REGISTRY_ID, SUPPORTED_LICENSES
from modelbuilder.recipe_registry_contract import REGISTRY_OPERATIONS
from modelbuilder.service import OperationService

OWNER = Actor("publisher", frozenset({"read", "modify", "publish"}))
READER = Actor("reader", frozenset({"read"}))
EDITOR = Actor("editor", frozenset({"read", "modify"}))


def commit_source(source):
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Pinned source fixture"]):
        subprocess.run(["git", "-C", str(source), *args], check=True)
    return subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()


@pytest.fixture
def service(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    sentinel = tmp_path / "SHOULD_NOT_EXIST"
    (source / "model.py").write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\nraise RuntimeError('must never execute')\n")
    commit_source(source)
    instance = OperationService(tmp_path / "private-registry.sqlite3", source_root=source)
    yield instance
    assert not sentinel.exists()
    instance.store.close()


def project(service, slug="source-model", license_id="MIT", private_reference=False, publication_enabled=True):
    manifest = default_recipe("template-project", "template-recipe", "Small classifier")
    data = (service.source_root / "model.py").read_bytes()
    manifest["source"] = {"repository": "https://example.invalid/model.git", "commit": subprocess.check_output(["git", "-C", str(service.source_root), "rev-parse", "HEAD"], text=True).strip(),
                          "files": [{"path": "model.py", "sha256": hashlib.sha256(data).hexdigest()}]}
    manifest["licenses"] = [{"scope": "source", "license": license_id, "redistribution_allowed": True}]
    if private_reference:
        manifest["resources"] = [{"id": "private-dataset", "kind": "dataset", "url": "https://private.example.invalid/data",
                                  "revision": "d" * 40, "license": "NOASSERTION", "private": True, "required": True}]
        manifest["external_dependencies"] = [{"id": "weights", "kind": "model", "url": "https://huggingface.co/example/pinned",
                                              "revision": "b" * 40, "license": "Apache-2.0", "required": True}]
    created = service.invoke("project.create", {"slug": slug, "model_name": "Classifier", "recipe": manifest,
                                               "idempotency_key": "project-" + slug}, OWNER)
    if publication_enabled:
        service.invoke("policy.update", {"project_id": created["project_id"], "expected_revision": 1,
                       "changes": {"publication_enabled": True}, "idempotency_key": "enable-publish-" + slug}, OWNER)
    return created


def publish_fields(service, source=None, version="1.0.0", **overrides):
    source = source or project(service)
    recipe = service.invoke("recipe.get", {"project_id": source["project_id"], "recipe_id": source["recipe_id"]}, OWNER)
    return {"registry_id": REGISTRY_ID, "project_id": source["project_id"], "recipe_id": source["recipe_id"],
            "expected_revision": recipe["revision"], "package_name": "small-classifier", "package_version": version,
            "visibility": "private", "metadata": {"title": "Small classifier", "description": "Pinned model-development recipe",
                                                      "tags": ["classification", "pytorch"]}, **overrides}


def publish(service, fields=None, key="publish", actor=OWNER):
    fields = fields or publish_fields(service)
    review = service.invoke("recipe.publish.preview", fields, actor)
    request = {**fields, "review_digest": review["review_digest"], "confirm_publish": True,
               "acknowledge_unresolved_dependencies": True, "idempotency_key": key}
    return service.invoke("recipe.publish", request, actor), request


def fork_fields(publication, target):
    return {"registry_id": REGISTRY_ID, "publication_id": publication["publication_id"], "target": target}


def fork(service, publication, target, key="fork", actor=OWNER):
    fields = fork_fields(publication, target)
    preview = service.invoke("recipe.fork.preview", fields, actor)
    request = {**fields, "review_digest": preview["review_digest"], "confirm_import": True,
               "acknowledge_unresolved_dependencies": True, "idempotency_key": key}
    return service.invoke("recipe.fork", request, actor), request


def error_code(code, callable):
    with pytest.raises(ServiceError) as caught:
        callable()
    assert caught.value.code == code
    return caught.value


def test_schema_catalog_is_typed_and_transport_independent(service):
    catalog = {item["name"]: item for item in service.catalog(OWNER)}
    assert REGISTRY_OPERATIONS <= set(catalog)
    for name in REGISTRY_OPERATIONS:
        Draft202012Validator.check_schema(catalog[name]["input_schema"])
    assert catalog["recipe.publish"]["required_scopes"] == ["publish", "read"]
    assert catalog["recipe.fork"]["required_scopes"] == ["modify", "read"]
    assert not catalog["recipe.publish.preview"]["mutating"]
    assert not catalog["recipe.fork.preview"]["mutating"]
    registry = service.invoke("recipe.registry.list", {}, READER)
    assert registry["public_registry_integration"] == "not_configured"
    assert registry["registries"][0]["can_publish"] is False
    assert registry["registries"][0]["hosts_weights_or_datasets"] is False


def test_standalone_registry_schema_matches_shared_catalog(service):
    root = Path(__file__).resolve().parents[2]
    schema = json.loads((root / "schemas/recipe-registry.operations.v0.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    exported = {item["properties"]["operation"]["const"]: item["properties"]["payload"] for item in schema["oneOf"]}
    actual = {item["name"]: item["input_schema"] for item in service.catalog(OWNER) if item["name"] in REGISTRY_OPERATIONS}
    assert exported == actual


def test_publication_review_is_inert_and_content_bound(service):
    fields = publish_fields(service)
    before = service.store.list("recipe_publication")
    preview = service.invoke("recipe.publish.preview", fields, OWNER)
    review = preview["review"]
    assert service.store.list("recipe_publication") == before
    assert review["visibility"] == "private"
    assert review["public_upload"] is False
    assert review["execution_allowed"] is False
    assert review["file_inventory"][0]["path"] == "model.py"
    assert review["licenses"]["legal_clearance_verified"] is False
    assert review["publisher_signature_verified"] is False
    assert len(review["bundle_digest"]) == 64
    assert review["source"]["commit"] == subprocess.check_output(["git", "-C", str(service.source_root), "rev-parse", "HEAD"], text=True).strip()
    request = {**fields, "review_digest": preview["review_digest"], "confirm_publish": True, "idempotency_key": "edited"}
    request["metadata"] = {**fields["metadata"], "title": "Changed after review"}
    error_code("review_conflict", lambda: service.invoke("recipe.publish", request, OWNER))
    assert service.store.list("recipe_publication") == []


def test_private_registry_reopens_and_returns_exact_verified_bytes(service):
    publication, request = publish(service)
    assert publication["revision"] == 1
    assert "bundle_json" not in publication
    assert service.invoke("recipe.publish", request, OWNER) == publication
    reopened = OperationService(service.store.path, source_root=service.source_root)
    try:
        result = reopened.invoke("recipe.registry.get", {"registry_id": REGISTRY_ID,
                                  "publication_id": publication["publication_id"]}, READER)
        bundle = parse_bundle_json(result["bundle_json"])
        assert import_bundle(bundle)["bundle_digest"] == publication["bundle_digest"]
        assert result["bundle_json"] == service.store.get("recipe_publication", publication["publication_id"])["bundle_json"]
        assert len(reopened.store.resource_versions("recipe_publication", publication["publication_id"])) == 1
        discovered = reopened.invoke("recipe.discover", {"registry_id": REGISTRY_ID, "query": "CLASSIFIER", "tags": ["pytorch"]}, READER)
        assert discovered["total"] == 1
        assert discovered["publications"][0]["publication_id"] == publication["publication_id"]
        assert "bundle_json" not in discovered["publications"][0]
        assert not discovered["network_accessed"]
        assert reopened.invoke("recipe.publish", request, OWNER) == publication
    finally:
        reopened.store.close()


def test_immutable_versions_cannot_be_replaced(service):
    fields = publish_fields(service)
    first, _ = publish(service, fields)
    error_code("publication_conflict", lambda: publish(service, fields, key="replacement"))
    second, _ = publish(service, {**fields, "package_version": "1.1.0"}, key="new-version")
    assert first["publication_id"] != second["publication_id"]
    assert service.store.get("recipe_publication", first["publication_id"])["revision"] == 1
    assert len(service.store.list("recipe_publication")) == 2
    page = service.invoke("recipe.discover", {"registry_id": REGISTRY_ID, "limit": 1}, READER)
    assert page["next_offset"] == 1
    next_page = service.invoke("recipe.discover", {"registry_id": REGISTRY_ID, "limit": 1, "offset": 1}, READER)
    assert next_page["next_offset"] is None
    assert page["publications"][0]["publication_id"] != next_page["publications"][0]["publication_id"]


@pytest.mark.parametrize("license_id", ["NOASSERTION", "UNLICENSED", "GPL-3.0-only", "Custom-Internal-Terms"])
def test_unsupported_source_license_fails_closed(service, license_id):
    fields = publish_fields(service, project(service, license_id=license_id))
    error_code("unsupported_license", lambda: service.invoke("recipe.publish.preview", fields, OWNER))
    assert service.store.list("recipe_publication") == []


@pytest.mark.parametrize("mode", ["absent", "not-source", "denied"])
def test_source_rights_cannot_be_invented_by_catalog_metadata(service, mode):
    source = project(service)
    recipe = service._current_recipe(source["project_id"])
    incoming = copy.deepcopy(recipe)
    if mode == "absent":
        incoming["licenses"] = []
    elif mode == "not-source":
        incoming["licenses"][0]["scope"] = "dataset"
    else:
        incoming["licenses"][0]["redistribution_allowed"] = False
    service.invoke("recipe.update", {"project_id": source["project_id"], "recipe_id": source["recipe_id"],
                   "expected_revision": recipe["revision"], "recipe": incoming, "idempotency_key": "license-edit"}, OWNER)
    fields = publish_fields(service, source)
    error_code("redistribution_not_allowed" if mode == "denied" else "license_review_required",
               lambda: service.invoke("recipe.publish.preview", fields, OWNER))


def test_private_and_unresolved_dependencies_require_explicit_acknowledgement(service):
    source = project(service, private_reference=True)
    fields = publish_fields(service, source)
    preview = service.invoke("recipe.publish.preview", fields, OWNER)
    unresolved = preview["review"]["import_report"]["unresolved_dependencies"]
    assert any(item["reason"] == "private_reference" for item in unresolved)
    assert any(item["reason"] == "license_review_required" for item in unresolved)
    assert any(item["id"] == "weights" for item in unresolved)
    error_code("dependency_acknowledgement_required", lambda: service.invoke("recipe.publish", {
        **fields, "review_digest": preview["review_digest"], "confirm_publish": True, "idempotency_key": "unacknowledged"}, OWNER))
    publication, _ = publish(service, fields)
    assert publication["import_report"]["dependencies_ready"] is False
    target = {"mode": "new_project", "slug": "private-fork", "model_name": "Private fork"}
    fork_request = fork_fields(publication, target)
    fork_preview = service.invoke("recipe.fork.preview", fork_request, OWNER)
    error_code("dependency_acknowledgement_required", lambda: service.invoke("recipe.fork", {
        **fork_request, "review_digest": fork_preview["review_digest"], "confirm_import": True, "idempotency_key": "unacknowledged-fork"}, OWNER))
    assert len(service.store.list("project")) == 1
    imported, _ = fork(service, publication, target)
    assert imported["import_report"]["unresolved_dependencies"] == unresolved
    assert imported["import_report"]["execution_allowed"] is False
    assert imported["import_report"]["trust_required"] is True


def test_existing_project_fork_preserves_single_recipe_and_history(service):
    publication, _ = publish(service)
    target = project(service, "target-model")
    before = service._current_recipe(target["project_id"])
    target_spec = {"mode": "existing_project", "project_id": target["project_id"], "expected_revision": before["revision"]}
    preview = service.invoke("recipe.fork.preview", fork_fields(publication, target_spec), OWNER)
    assert preview["review"]["stable_recipe_identity_preserved"]
    assert "registry_origin" in preview["review"]["diff"]
    assert service._current_recipe(target["project_id"]) == before
    imported, request = fork(service, publication, target_spec)
    assert imported["recipe_id"] == before["recipe_id"]
    assert imported["revision"] == before["revision"] + 1
    assert len(service.store.list("recipe", target["project_id"])) == 1
    assert service.store.get("recipe", before["recipe_id"], before["revision"]) == before
    assert imported["recipe"]["extensions"]["registry_origin"]["publication_digest"] == publication["publication_digest"]
    assert imported["import_report"]["files_materialized"] is False
    assert service.invoke("recipe.fork", request, OWNER) == imported
    assert len(service.store.list("recipe_import_snapshot", target["project_id"])) == 1


def test_new_project_fork_is_atomic_and_has_one_recipe(service):
    publication, _ = publish(service)
    target = {"mode": "new_project", "slug": "new-fork", "model_name": "New fork", "description": "Reviewed inert template"}
    preview = service.invoke("recipe.fork.preview", fork_fields(publication, target), OWNER)
    assert preview["review"]["new_project_created_only_on_confirmation"] is True
    assert len(service.store.list("project")) == 1
    imported, request = fork(service, publication, target)
    assert imported["project_id"] != publication["project_id"]
    assert imported["recipe_id"] != publication["recipe_id"]
    assert imported["recipe_id"] == imported["project"]["recipe_id"] == imported["project"]["active_recipe_id"]
    assert imported["revision"] == 1
    assert len(service.store.list("recipe", imported["project_id"])) == 1
    assert service.invoke("recipe.fork", request, OWNER) == imported
    assert len(service.store.list("project")) == 2
    error_code("slug_conflict", lambda: fork(service, publication, target, key="same-slug"))
    assert len(service.store.list("project")) == 2
    assert len(service.store.list("recipe_import_snapshot")) == 1


def test_imported_original_bundle_is_target_scoped_and_survives_source_access_removal(service):
    publication, _ = publish(service)
    imported, _ = fork(service, publication, {"mode": "new_project", "slug": "owned-copy", "model_name": "Owned copy"})
    target_actor = Actor("new-owner", {"read"}, {imported["project_id"]})
    fields = {"project_id": imported["project_id"], "recipe_id": imported["recipe_id"], "revision": imported["revision"]}
    retained = service.invoke("recipe.registry.imported_bundle", fields, target_actor)
    assert retained["bundle_contains_original_publisher_recipe"]
    assert retained["target_recipe_digest"] == imported["recipe"]["digest"]
    assert parse_bundle_json(retained["bundle_json"])["digest"] == publication["recipe_digest"]
    assert retained["target_recipe_digest"] != publication["recipe_digest"]
    assert retained["files_materialized"] is False
    error_code("forbidden", lambda: service.invoke("recipe.registry.get", {
        "registry_id": REGISTRY_ID, "publication_id": publication["publication_id"]}, target_actor))
    source_actor = Actor("source-only", {"read"}, {publication["project_id"]})
    error_code("forbidden", lambda: service.invoke("recipe.registry.imported_bundle", fields, source_actor))


def test_stale_import_never_overwrites_newer_recipe(service):
    publication, _ = publish(service)
    target = project(service, "stale-target")
    before = service._current_recipe(target["project_id"])
    fields = fork_fields(publication, {"mode": "existing_project", "project_id": target["project_id"], "expected_revision": before["revision"]})
    preview = service.invoke("recipe.fork.preview", fields, OWNER)
    updated = service.invoke("recipe.update", {"project_id": target["project_id"], "recipe_id": before["recipe_id"],
                             "expected_revision": before["revision"], "recipe": {**before, "name": "Newer user edit"},
                             "idempotency_key": "newer-edit"}, OWNER)
    error_code("revision_conflict", lambda: service.invoke("recipe.fork", {
        **fields, "review_digest": preview["review_digest"], "confirm_import": True, "idempotency_key": "stale-fork"}, OWNER))
    assert service._current_recipe(target["project_id"]) == updated
    assert service.store.list("recipe_import_snapshot") == []


def test_import_review_cannot_be_retargeted(service):
    publication, _ = publish(service)
    fields = fork_fields(publication, {"mode": "new_project", "slug": "first-target", "model_name": "Target"})
    preview = service.invoke("recipe.fork.preview", fields, OWNER)
    fields["target"] = {**fields["target"], "slug": "different-target"}
    error_code("review_conflict", lambda: service.invoke("recipe.fork", {
        **fields, "review_digest": preview["review_digest"], "confirm_import": True, "idempotency_key": "retarget"}, OWNER))
    assert len(service.store.list("project")) == 1


def test_read_only_actor_and_editor_cannot_publish_by_hidden_endpoint(service):
    fields = publish_fields(service)
    for actor in (READER, EDITOR):
        preview = service.invoke("recipe.publish.preview", fields, actor)
        request = {**fields, "review_digest": preview["review_digest"], "confirm_publish": True,
                   "idempotency_key": "hidden-write-" + actor.id}
        error_code("forbidden", lambda: service.invoke("recipe.publish", request, actor))
    assert service.store.list("recipe_publication") == []
    assert any(item["outcome"] == "denied" and item["operation"] == "recipe.publish"
               for item in service.store.history(fields["project_id"]))


def test_reader_cannot_fork_and_project_scopes_apply_to_both_ends(service):
    publication, _ = publish(service)
    target = project(service, "protected-target")
    target_spec = {"mode": "existing_project", "project_id": target["project_id"], "expected_revision": 1}
    fields = fork_fields(publication, target_spec)
    preview = service.invoke("recipe.fork.preview", fields, READER)
    error_code("forbidden", lambda: service.invoke("recipe.fork", {
        **fields, "review_digest": preview["review_digest"], "confirm_import": True, "idempotency_key": "reader-write"}, READER))
    source_only = Actor("source-only", {"read", "modify"}, {publication["project_id"]})
    target_only = Actor("target-only", {"read", "modify"}, {target["project_id"]})
    for actor in (source_only, target_only):
        error_code("forbidden", lambda: service.invoke("recipe.fork.preview", fields, actor))
    error_code("forbidden", lambda: service.invoke("recipe.fork.preview", fork_fields(publication, {
        "mode": "new_project", "slug": "no-global-rights", "model_name": "No"}), source_only))
    assert service.invoke("recipe.discover", {"registry_id": REGISTRY_ID}, target_only)["publications"] == []
    assert len(service.store.list("project")) == 2


@pytest.mark.parametrize("operation,extra", [
    ("recipe.publish", {"confirm_publish": False}),
    ("recipe.publish", {}),
    ("recipe.publish", {"confirm_publish": True, "visibility": "public"}),
    ("recipe.publish", {"confirm_publish": True, "destination_url": "https://example.invalid/registry"}),
])
def test_publication_requires_exact_explicit_confirmation_schema(service, operation, extra):
    fields = publish_fields(service)
    preview = service.invoke("recipe.publish.preview", fields, OWNER)
    error_code("validation_error", lambda: service.invoke(operation, {
        **fields, "review_digest": preview["review_digest"], "idempotency_key": "invalid-confirm", **extra}, OWNER))
    assert service.store.list("recipe_publication") == []


def test_no_unconfigured_registry_or_automatic_public_upload(service):
    fields = publish_fields(service, registry_id="public-marketplace")
    error_code("capability_unavailable", lambda: service.invoke("recipe.publish.preview", fields, OWNER))
    error_code("capability_unavailable", lambda: service.invoke("recipe.discover", {"registry_id": "https://public.example.invalid"}, READER))
    assert service.store.list("recipe_publication") == []


@pytest.mark.parametrize("field", ["metadata", "bundle_json", "bundle_digest", "source"])
def test_tampered_persistent_catalog_is_rejected_before_import(service, field):
    publication, _ = publish(service)
    entry = service.store.get("recipe_publication", publication["publication_id"])
    if field == "metadata":
        entry["metadata"]["title"] = "Tampered"
    elif field == "source":
        entry["source"]["commit"] = "f" * 40
    else:
        entry[field] = "tampered"
    service.store.connection.execute("UPDATE resources SET body=? WHERE kind='recipe_publication' AND id=?",
                                     (json.dumps(entry), publication["publication_id"]))
    error_code("registry_integrity_error", lambda: service.invoke("recipe.registry.get", {
        "registry_id": REGISTRY_ID, "publication_id": publication["publication_id"]}, OWNER))
    error_code("registry_integrity_error", lambda: service.invoke("recipe.fork.preview", fork_fields(publication, {
        "mode": "new_project", "slug": "tampered-copy", "model_name": "Tampered"}), OWNER))
    assert len(service.store.list("project")) == 1


def test_tampered_source_file_rejected_before_publication(service):
    fields = publish_fields(service)
    preview = service.invoke("recipe.publish.preview", fields, OWNER)
    (service.source_root / "model.py").write_text("print('changed after review')\n")
    error_code("source_hash_mismatch", lambda: service.invoke("recipe.publish", {
        **fields, "review_digest": preview["review_digest"], "confirm_publish": True, "idempotency_key": "source-changed"}, OWNER))
    assert service.store.list("recipe_publication") == []


def test_secret_bearing_metadata_is_never_persisted(service):
    fields = publish_fields(service)
    fields["metadata"]["description"] = "https://private.example.invalid/path?access_token=do-not-persist"
    error_code("secret_rejected", lambda: service.invoke("recipe.publish.preview", fields, OWNER))
    assert service.store.list("recipe_publication") == []


@pytest.mark.parametrize("description", [
    'api_key="synthetic_test_value_not_a_real_key"',
    "-----BEGIN " + "PRIVATE KEY-----\nsynthetic\n-----END PRIVATE KEY-----",
    "/home/private/research", "C:\\private\\research",
], ids=["credential_field", "synthetic_key_header", "posix_private_path", "windows_private_path"])
def test_catalog_text_uses_existing_recipe_secret_and_host_path_scan(service, description):
    fields = publish_fields(service)
    fields["metadata"]["description"] = description
    with pytest.raises(ServiceError) as error:
        service.invoke("recipe.publish.preview", fields, OWNER)
    assert error.value.code in {"secret_detected", "absolute_path"}
    assert service.store.list("recipe_publication") == []


def test_same_version_concurrent_publication_is_atomic(service):
    fields = publish_fields(service)
    review = service.invoke("recipe.publish.preview", fields, OWNER)
    barrier = Barrier(4)

    def submit(index):
        instance = OperationService(service.store.path, source_root=service.source_root)
        try:
            barrier.wait()
            return instance.invoke("recipe.publish", {**fields, "review_digest": review["review_digest"],
                                   "confirm_publish": True, "idempotency_key": f"publish-{index}"}, OWNER)
        except ServiceError as error:
            return error
        finally:
            instance.store.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert all(item.code == "publication_conflict" for item in results if isinstance(item, ServiceError))
    assert len(service.store.list("recipe_publication")) == 1


def test_concurrent_imports_keep_exactly_one_editable_recipe(service):
    publication, _ = publish(service)
    target = project(service, "concurrent-target")
    fields = fork_fields(publication, {"mode": "existing_project", "project_id": target["project_id"], "expected_revision": 1})
    preview = service.invoke("recipe.fork.preview", fields, OWNER)
    barrier = Barrier(4)

    def submit(index):
        instance = OperationService(service.store.path, source_root=service.source_root)
        try:
            barrier.wait()
            return instance.invoke("recipe.fork", {**fields, "review_digest": preview["review_digest"],
                                   "confirm_import": True, "idempotency_key": f"import-{index}"}, OWNER)
        except ServiceError as error:
            return error
        finally:
            instance.store.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert all(item.code == "revision_conflict" for item in results if isinstance(item, ServiceError))
    assert len(service.store.list("recipe", target["project_id"])) == 1
    assert len(service.store.list("recipe_import_snapshot", target["project_id"])) == 1
    assert service._current_recipe(target["project_id"])["revision"] == 2


def test_real_http_boundary_reuses_auth_schemas_audit_and_idempotency(service):
    fields = publish_fields(service)
    with TestClient(create_app(service, {"publisher-secret": OWNER, "reader-secret": READER})) as client:
        path = "/v1/operations/recipe.publish.preview"
        assert client.post(path, json=fields).status_code == 401
        preview = client.post(path, json=fields, headers={"Authorization": "Bearer publisher-secret"})
        assert preview.status_code == 200
        request = {**fields, "review_digest": preview.json()["review_digest"], "confirm_publish": True, "idempotency_key": "http-publication"}
        denied = client.post("/v1/operations/recipe.publish", json=request, headers={"Authorization": "Bearer reader-secret"})
        assert denied.status_code == 403
        accepted = client.post("/v1/operations/recipe.publish", json=request, headers={"Authorization": "Bearer publisher-secret"})
        assert accepted.status_code == 200
        assert client.post("/v1/operations/recipe.publish", json=request, headers={"Authorization": "Bearer publisher-secret"}).json() == accepted.json()
        catalog = client.get("/v1/catalog", headers={"Authorization": "Bearer reader-secret"}).json()
        assert REGISTRY_OPERATIONS <= {item["name"] for item in catalog["operations"]}
        assert accepted.json()["public_upload"] is False


@pytest.mark.parametrize("license_id", sorted(SUPPORTED_LICENSES))
def test_supported_licenses_are_declarations_not_legal_clearance(service, license_id):
    fields = publish_fields(service, project(service, license_id=license_id))
    publication, _ = publish(service, fields)
    assert publication["licenses"]["declarations"][0]["license"] == license_id
    assert publication["licenses"]["legal_clearance_verified"] is False


def test_retained_import_snapshot_tamper_is_rejected(service):
    publication, _ = publish(service)
    imported, _ = fork(service, publication, {"mode": "new_project", "slug": "retained", "model_name": "Retained"})
    snapshot_id = imported["import_report"]["snapshot_id"]
    snapshot = service.store.get("recipe_import_snapshot", snapshot_id)
    snapshot["bundle_json"] = "{}"
    service.store.connection.execute("UPDATE resources SET body=? WHERE kind='recipe_import_snapshot' AND id=?",
                                     (json.dumps(snapshot), snapshot_id))
    error_code("registry_integrity_error", lambda: service.invoke("recipe.registry.imported_bundle", {
        "project_id": imported["project_id"], "recipe_id": imported["recipe_id"], "revision": imported["revision"]}, OWNER))


def test_idempotency_key_cannot_publish_different_package_or_actor_review(service):
    fields = publish_fields(service)
    publication, request = publish(service, fields)
    changed = {**request, "package_version": "2.0.0"}
    error_code("idempotency_conflict", lambda: service.invoke("recipe.publish", changed, OWNER))
    another = Actor("other-publisher", OWNER.scopes)
    error_code("review_conflict", lambda: service.invoke("recipe.publish", {**request, "idempotency_key": "different-actor"}, another))
    assert service.store.get("recipe_publication", publication["publication_id"])["publisher_actor_id"] == OWNER.id


@pytest.mark.parametrize("granted", ["none", "source", "target"])
def test_idempotency_does_not_bypass_narrowed_project_scopes(service, granted):
    publication, _ = publish(service)
    imported, request = fork(service, publication, {"mode": "new_project", "slug": "before-revocation", "model_name": "Before revocation"})
    ids = {"none": set(), "source": {publication["project_id"]}, "target": {imported["project_id"]}}[granted]
    narrowed = Actor(OWNER.id, OWNER.scopes, ids)
    error_code("forbidden", lambda: service.invoke("recipe.fork", request, narrowed))
    assert len(service.store.list("project")) == 2


def test_raw_bundle_json_retains_float_and_large_integer_integrity(service):
    source = project(service)
    recipe = service._current_recipe(source["project_id"])
    changed = copy.deepcopy(recipe)
    changed["evaluation"] = {"threshold": 1.0, "tiny": 1e-9, "large_integer": 9007199254740993}
    service.invoke("recipe.update", {"project_id": source["project_id"], "recipe_id": recipe["recipe_id"],
                   "expected_revision": recipe["revision"], "recipe": changed, "idempotency_key": "numbers"}, OWNER)
    publication, _ = publish(service, publish_fields(service, source))
    fetched = service.invoke("recipe.registry.get", {"registry_id": REGISTRY_ID, "publication_id": publication["publication_id"]}, OWNER)
    assert '"threshold":1.0' in fetched["bundle_json"]
    assert '"large_integer":9007199254740993' in fetched["bundle_json"]
    assert import_bundle(parse_bundle_json(fetched["bundle_json"]))["digest"] == publication["recipe_digest"]


def test_private_publication_requires_explicit_policy_enablement(service):
    fields = publish_fields(service, project(service, publication_enabled=False))
    preview = service.invoke("recipe.publish.preview", fields, OWNER)
    assert not preview["can_publish"]
    assert preview["review"]["publication_policy"]["scope"] == "local-private-only"
    error_code("publication_disabled", lambda: service.invoke("recipe.publish", {
        **fields, "review_digest": preview["review_digest"], "confirm_publish": True, "idempotency_key": "disabled"}, OWNER))
    assert service.store.list("recipe_publication") == []
    error_code("forbidden", lambda: service.invoke("policy.update", {
        "project_id": fields["project_id"], "expected_revision": 1, "changes": {"publication_enabled": True},
        "idempotency_key": "editor-enabling"}, EDITOR))
    service.invoke("policy.update", {"project_id": fields["project_id"], "expected_revision": 1,
                   "changes": {"publication_enabled": True}, "idempotency_key": "explicit-enable"}, OWNER)
    # The original review cannot be replayed after the policy revision changed.
    error_code("review_conflict", lambda: service.invoke("recipe.publish", {
        **fields, "review_digest": preview["review_digest"], "confirm_publish": True, "idempotency_key": "old-policy-review"}, OWNER))
    publication, _ = publish(service, fields)
    assert publication["publication_policy"]["enabled"] is True


def test_disabling_publication_does_not_delete_or_publish_existing_versions(service):
    fields = publish_fields(service)
    publication, _ = publish(service, fields)
    service.invoke("policy.update", {"project_id": fields["project_id"], "expected_revision": 2,
                   "changes": {"publication_enabled": False}, "idempotency_key": "disable"}, EDITOR)
    assert service.invoke("recipe.discover", {"registry_id": REGISTRY_ID}, READER)["total"] == 1
    assert service.invoke("recipe.registry.get", {"registry_id": REGISTRY_ID,
                          "publication_id": publication["publication_id"]}, OWNER)["publication_id"] == publication["publication_id"]
    error_code("publication_disabled", lambda: publish(service, {**fields, "package_version": "2.0.0"}, key="disabled-new"))


def test_no_training_execution_or_source_materialization_on_fork(service, tmp_path):
    class NoExecution:
        def __getattr__(self, name):
            pytest.fail("Registry unexpectedly accessed execution backend: " + name)
    service.backend = NoExecution()
    publication, _ = publish(service)
    target_root = tmp_path / "future-workspace"
    imported, _ = fork(service, publication, {"mode": "new_project", "slug": "inert", "model_name": "Inert"})
    assert not target_root.exists()
    assert imported["execution_allowed"] is False
    assert service.store.list("operation") == []


@pytest.mark.parametrize("scheme", ["bundle", "BUNDLE", "BuNdLe"])
@pytest.mark.parametrize("rights", ["denied", "unsupported"])
def test_bundled_dependency_rights_use_normalized_url_scheme(service, scheme, rights):
    source = project(service)
    recipe = service._current_recipe(source["project_id"])
    incoming = copy.deepcopy(recipe)
    incoming["resources"] = [{"id": "source-copy", "kind": "source", "url": scheme + ":model.py",
                              "sha256": incoming["source"]["files"][0]["sha256"],
                              "license": "Custom-Terms" if rights == "unsupported" else "MIT",
                              "redistribution_allowed": rights != "denied"}]
    service.invoke("recipe.update", {"project_id": source["project_id"], "recipe_id": source["recipe_id"],
                   "expected_revision": recipe["revision"], "recipe": incoming, "idempotency_key": "bundle-reference"}, OWNER)
    fields = publish_fields(service, source)
    error_code("unsupported_license" if rights == "unsupported" else "redistribution_not_allowed",
               lambda: service.invoke("recipe.publish.preview", fields, OWNER))
    assert service.store.list("recipe_publication") == []


@pytest.mark.parametrize("kind", ["dataset", "model", "checkpoint", "artifact"])
def test_registry_refuses_explicitly_bundled_dataset_weights_or_runtime_artifacts(service, kind):
    source = project(service)
    recipe = service._current_recipe(source["project_id"])
    incoming = copy.deepcopy(recipe)
    payload = b'{"values":[1,2,3]}\n'
    (service.source_root / "data.json").write_bytes(payload)
    incoming["source"]["commit"] = commit_source(service.source_root)
    digest = hashlib.sha256(payload).hexdigest()
    incoming["source"]["files"].append({"path": "data.json", "sha256": digest})
    incoming["resources"] = [{"id": "bundled-data", "kind": kind, "url": "BUNDLE:data.json", "sha256": digest,
                              "license": "MIT", "redistribution_allowed": True}]
    service.invoke("recipe.update", {"project_id": source["project_id"], "recipe_id": source["recipe_id"],
                   "expected_revision": recipe["revision"], "recipe": incoming, "idempotency_key": "bundled-data"}, OWNER)
    error_code("unsupported_registry_content", lambda: service.invoke("recipe.publish.preview", publish_fields(service, source), OWNER))
    assert service.store.list("recipe_publication") == []


def test_new_version_cannot_hijack_another_projects_package_name(service):
    source = project(service)
    first, _ = publish(service, publish_fields(service, source))
    second_source = project(service, "different-project")
    fields = publish_fields(service, second_source, version="2.0.0")
    error_code("package_ownership_conflict", lambda: publish(service, fields, key="cross-project-version"))
    assert len(service.store.list("recipe_publication")) == 1
    assert len(service.store.list("recipe_registry_package")) == 1
    second, _ = publish(service, {**fields, "package_name": "different-project-classifier"}, key="own-package")
    assert second["project_id"] == second_source["project_id"]
    assert first["package_name"] != second["package_name"]


def test_publication_and_import_audit_identify_exact_target_and_revisions(service):
    publication, _ = publish(service)
    event = next(item for item in service.store.history(publication["project_id"]) if item["operation"] == "recipe.publish")
    assert event["target_id"] == publication["publication_id"]
    assert event["actor_id"] == OWNER.id
    assert event["before_revision"] is None
    assert event["after_revision"] == 1
    target = project(service, "audit-target")
    imported, _ = fork(service, publication, {"mode": "existing_project", "project_id": target["project_id"], "expected_revision": 1})
    event = next(item for item in service.store.history(target["project_id"]) if item["operation"] == "recipe.fork")
    assert event["target_id"] == imported["recipe_id"]
    assert (event["before_revision"], event["after_revision"]) == (1, 2)
