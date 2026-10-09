"""Inert immutable private recipe publication, discovery and reviewed imports.

The host OperationService owns authentication, schema checks, transactions,
idempotency and auditing. SQLiteStore owns durable records. This module never
opens a URL, imports bundled Python, writes extracted files, installs packages,
creates accounts or uploads anything. Digests establish integrity, not trust,
legal clearance, publisher authenticity or execution readiness.
"""
from __future__ import annotations

import copy
import hashlib
from urllib.parse import urlsplit

from . import recipe as recipe_module
from .models import Actor, MAX_BUNDLE_JSON_BYTES, ServiceError, utcnow
from .recipe_registry_contract import REGISTRY_OPERATIONS

REGISTRY_ID = "local-private"
PUBLICATION_KIND = "recipe_publication"
SCHEMA_VERSION = "0"
# Deliberately conservative identifier policy, not a license-compatibility engine.
SUPPORTED_LICENSES = frozenset({"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "CC0-1.0"})
AUDIENCE = "Authenticated actors with read scope and access to the source project in this service"


def _hash(value):
    return hashlib.sha256(recipe_module.canonical_json(value).encode("utf-8")).hexdigest()


def _body(recipe):
    return {key: copy.deepcopy(value) for key, value in recipe.items() if key not in {"digest", "import_report"}}


def _authorize(actor, scopes=("read",), project_id=None):
    if not isinstance(actor, Actor):
        raise ServiceError("unauthenticated", "Explicit actor identity is required", status_code=401)
    if not set(scopes).issubset(actor.scopes):
        raise ServiceError("forbidden", "Recipe registry operation requires explicit scopes",
                           {"required_scopes": list(scopes)}, 403)
    if project_id and actor.project_ids is not None and project_id not in actor.project_ids:
        raise ServiceError("forbidden", "Actor cannot access this project", status_code=403)


def _registry(registry_id):
    if registry_id != REGISTRY_ID:
        raise ServiceError("capability_unavailable", "Only the local private recipe registry is configured; external/public publication is unavailable",
                           {"registry_id": registry_id, "available_registry_ids": [REGISTRY_ID]}, 422)


def authorize_publication_policy_change(payload, actor):
    """Service policy.update hook; enabling publication needs publish scope.

    This flag enables only implemented private publication. It never activates
    an external destination, raises token scopes or grants a project audience.
    """
    changes = payload["changes"]
    if "publication_enabled" not in changes:
        return
    if type(changes["publication_enabled"]) is not bool:
        raise ServiceError("validation_error", "Publication policy must be a boolean")
    if changes["publication_enabled"]:
        _authorize(actor, ("modify", "publish"), payload["project_id"])


def _licenses(recipe, files):
    declarations = copy.deepcopy(recipe.get("licenses", []))
    if not declarations or not any(item["scope"] == "source" for item in declarations):
        raise ServiceError("license_review_required", "Publication requires an explicit source license declaration")
    for item in declarations:
        if item["license"] not in SUPPORTED_LICENSES:
            raise ServiceError("unsupported_license", "Publication license is outside the registry's supported declaration policy",
                               {"scope": item["scope"], "license": item["license"], "supported_licenses": sorted(SUPPORTED_LICENSES)})
        if item["redistribution_allowed"] is not True:
            raise ServiceError("redistribution_not_allowed", "Publication requires permission to redistribute bundled recipe/source content",
                               {"scope": item["scope"]})
    dependencies = copy.deepcopy(recipe["resources"] + recipe["external_dependencies"])
    for item in dependencies:
        # External data/model references are descriptions, not redistributed bytes.
        # Unsupported/uncleared rights remain explicit unresolved import blockers.
        if urlsplit(item["url"]).scheme == "bundle":
            if item["kind"] not in {"source", "environment"}:
                raise ServiceError("unsupported_registry_content", "Recipe registry bundles contain source/environment text only; datasets, weights, checkpoints and runtime artifacts must remain external references",
                                   {"id": item["id"], "kind": item["kind"]})
            if item["license"] not in SUPPORTED_LICENSES:
                raise ServiceError("unsupported_license", "Bundled dependency license is not supported",
                                   {"id": item["id"], "license": item["license"]})
            if item.get("redistribution_allowed") is not True:
                raise ServiceError("redistribution_not_allowed", "Bundled dependencies require explicit redistribution permission",
                                   {"id": item["id"]})
    return {"declarations": declarations, "dependency_declarations": dependencies,
            "bundled_file_paths": sorted(files), "policy": "supported-declarations-v0",
            "legal_clearance_verified": False}


def _import_report(bundle):
    report = recipe_module.import_bundle(bundle)
    # Unknown external licenses are retained as metadata but never silently cleared.
    unresolved = copy.deepcopy(report["unresolved_dependencies"])
    for item in report["recipe"]["resources"] + report["recipe"]["external_dependencies"]:
        if item["license"] not in SUPPORTED_LICENSES and not any(
            issue["id"] == item["id"] and issue["reason"] == "license_review_required" for issue in unresolved
        ):
            unresolved.append({"id": item["id"], "reason": "license_review_required", "required": True,
                               "license": item["license"]})
    return {"resolved_dependencies": report["resolved_dependencies"], "unresolved_dependencies": unresolved,
            "dependencies_ready": not any(item.get("required", True) for item in unresolved),
            "execution_allowed": False, "trust_required": True, "network_accessed": False}


class RecipeRegistry:
    """Construct cheaply per dispatch. No alternate auth, DB or transport stack."""

    def __init__(self, service):
        self.service, self.store = service, service.store

    def authorize_cached_result(self, operation, payload, actor, result):
        """Call before returning a cached fork result from OperationService.invoke.

        A new-project target has no top-level project_id in its request. Actor
        project grants may have changed since the original successful action.
        Idempotency never restores access or resurrects a retired recipe ID.
        """
        if operation != "recipe.fork":
            return
        _authorize(actor, ("read", "modify"), result["project_id"])
        self._entry(payload["publication_id"], actor)
        current = self.service._current_recipe(result["project_id"])
        if result["recipe_id"] != current["recipe_id"]:
            raise ServiceError("migration_conflict", "Cached import predates this project's current recipe identity",
                               {"current_recipe_id": current["recipe_id"]}, 409)

    def dispatch(self, operation, payload, actor):
        if operation not in REGISTRY_OPERATIONS:
            raise ServiceError("unknown_operation", "Recipe registry operation is unavailable", status_code=404)
        _authorize(actor)
        if operation == "recipe.registry.list":
            return {"schema_version": SCHEMA_VERSION, "registries": [{
                "registry_id": REGISTRY_ID, "name": "Local private recipe registry", "provider": "sqlite",
                "visibility": "private", "audience": AUDIENCE, "network_accessed": False,
                "content_types": ["recipe", "source-text", "dependency-reference"],
                "supported_licenses": sorted(SUPPORTED_LICENSES), "publish_scope_required": "publish",
                "can_publish": "publish" in actor.scopes, "publication_requires_project_policy": True,
                "public_publication_available": False,
                "hosts_weights_or_datasets": False,
            }], "public_registry_integration": "not_configured"}
        if operation == "recipe.registry.imported_bundle":
            return self._imported_bundle(payload, actor)
        _registry(payload["registry_id"])
        if operation == "recipe.discover":
            return self._discover(payload, actor)
        if operation == "recipe.registry.get":
            entry = self._entry(payload["publication_id"], actor)
            return {**self._metadata(entry), "bundle_json": entry["bundle_json"],
                    "media_type": "application/json", "import_report": entry["import_report"]}
        if operation.startswith("recipe.publish"):
            return self._publish(payload, actor, preview=operation.endswith(".preview"))
        return self._fork(payload, actor, preview=operation.endswith(".preview"))

    def _entry(self, publication_id, actor):
        entry = self.store.get(PUBLICATION_KIND, publication_id)
        # Filter before returning or validating untrusted catalog metadata.
        _authorize(actor, project_id=entry["project_id"])
        if entry.get("registry_id") != REGISTRY_ID or entry.get("publication_id") != publication_id:
            raise ServiceError("registry_integrity_error", "Stored publication identity is inconsistent")
        expected = entry.get("publication_digest")
        if not isinstance(expected, str) or expected != _hash({key: value for key, value in entry.items()
                                                             if key != "publication_digest"}):
            raise ServiceError("registry_integrity_error", "Stored immutable publication metadata changed")
        bundle = recipe_module.parse_bundle_json(entry["bundle_json"], max_bytes=MAX_BUNDLE_JSON_BYTES)
        report = _import_report(bundle)
        if (bundle["digest"] != entry["recipe_digest"] or bundle["bundle_digest"] != entry["bundle_digest"]
                or report != entry["import_report"]):
            raise ServiceError("registry_integrity_error", "Stored publication provenance differs from its verified bundle")
        return entry

    @staticmethod
    def _metadata(entry):
        return {key: copy.deepcopy(value) for key, value in entry.items() if key != "bundle_json"}

    def _imported_bundle(self, payload, actor):
        _authorize(actor, project_id=payload["project_id"])
        target = self.service._get("recipe", payload["recipe_id"], payload["project_id"], payload["revision"])
        snapshot_id = _hash([payload["project_id"], payload["recipe_id"], payload["revision"]])
        snapshot = self.service._get("recipe_import_snapshot", snapshot_id, payload["project_id"])
        expected = snapshot.get("snapshot_digest")
        if expected != _hash({key: value for key, value in snapshot.items() if key != "snapshot_digest"}):
            raise ServiceError("registry_integrity_error", "Stored import snapshot changed")
        bundle = recipe_module.parse_bundle_json(snapshot["bundle_json"], max_bytes=MAX_BUNDLE_JSON_BYTES)
        recipe_module.import_bundle(bundle)
        if (target["digest"] != snapshot["target_recipe_digest"]
                or bundle["bundle_digest"] != snapshot["origin"]["bundle_digest"]):
            raise ServiceError("registry_integrity_error", "Imported recipe and retained source provenance differ")
        return {**snapshot, "execution_allowed": False, "files_materialized": False,
                "media_type": "application/json", "bundle_contains_original_publisher_recipe": True}

    def _discover(self, payload, actor):
        query = payload.get("query", "").strip().casefold()
        tags = set(payload.get("tags", []))
        matches = []
        for row in self.store.list(PUBLICATION_KIND):
            if actor.project_ids is not None and row["project_id"] not in actor.project_ids:
                continue
            metadata = row["metadata"]
            searchable = " ".join([row["package_name"], metadata["title"], metadata["description"], *metadata["tags"]]).casefold()
            if query and query not in searchable or not tags.issubset(metadata["tags"]):
                continue
            matches.append(self._entry(row["publication_id"], actor))
        matches.sort(key=lambda item: (item["published_at"], item["publication_id"]), reverse=True)
        offset, limit = payload.get("offset", 0), payload.get("limit", 20)
        page = matches[offset:offset + limit]
        return {"registry_id": REGISTRY_ID, "publications": [self._metadata(item) for item in page],
                "total": len(matches), "offset": offset,
                "next_offset": offset + limit if offset + limit < len(matches) else None,
                "network_accessed": False, "execution_allowed": False}

    def _publication_review(self, payload, actor):
        _authorize(actor, project_id=payload["project_id"])
        # Catalog prose is persisted and shared alongside the manifest. Apply
        # the same credential/host-path scan used by recipe normalization.
        recipe_module._scan_manifest(payload["metadata"], "publication.metadata")
        current = self.service._current_recipe(payload["project_id"], payload["recipe_id"])
        self.service._revision(current, payload["expected_revision"])
        bundle = self.service._export_project_recipe(current)
        text = recipe_module.canonical_json(bundle)
        if len(text.encode("utf-8")) > MAX_BUNDLE_JSON_BYTES:
            raise ServiceError("bundle_limit", "Registry bundle exceeds the 8 MiB transport limit", status_code=413)
        # Publication reuses import validation as well as export validation.
        imported = _import_report(bundle)
        licenses = _licenses(bundle["recipe"], bundle["files"])
        policy = self.service._policy("policy.get", {"project_id": current["project_id"]})
        review = {
            "schema_version": SCHEMA_VERSION, "action": "recipe.publish", "registry_id": REGISTRY_ID,
            "visibility": "private", "audience": AUDIENCE, "provider": "sqlite",
            "package_name": payload["package_name"], "package_version": payload["package_version"],
            "metadata": copy.deepcopy(payload["metadata"]), "project_id": current["project_id"],
            "recipe_id": current["recipe_id"], "recipe_revision": current["revision"],
            "recipe_digest": bundle["digest"], "bundle_digest": bundle["bundle_digest"],
            "bundle_size_bytes": len(text.encode("utf-8")), "source": copy.deepcopy(current["source"]),
            "file_inventory": [{"path": path, "sha256": value["sha256"]} for path, value in sorted(bundle["files"].items())],
            "environment": copy.deepcopy(current["environment"]), "capabilities": current.get("capabilities", []),
            "licenses": licenses, "import_report": imported, "immutable_version": True,
            "publisher_actor_id": actor.id, "publisher_signature_verified": False,
            "publication_policy": {"enabled": policy["publication_enabled"], "revision": policy["revision"],
                                   "scope": "local-private-only"},
            "public_upload": False, "execution_allowed": False,
        }
        return review, text

    @staticmethod
    def _confirm(payload, review):
        if payload["review_digest"] != _hash(review):
            raise ServiceError("review_conflict", "The content, actor, audience or target changed since review; review again",
                               {"current_review_digest": _hash(review)}, 409)
        if review["import_report"]["unresolved_dependencies"] and payload.get("acknowledge_unresolved_dependencies") is not True:
            raise ServiceError("dependency_acknowledgement_required", "Acknowledge the reviewed unresolved/private dependencies before this inert action",
                               {"unresolved_dependencies": review["import_report"]["unresolved_dependencies"]})

    def _publish(self, payload, actor, *, preview):
        if not preview:
            _authorize(actor, ("read", "publish"), payload["project_id"])
        review, text = self._publication_review(payload, actor)
        if preview:
            return {"review": review, "review_digest": _hash(review),
                    "can_publish": "publish" in actor.scopes and review["publication_policy"]["enabled"],
                    "confirmation_required": True, "execution_allowed": False}
        if not review["publication_policy"]["enabled"]:
            raise ServiceError("publication_disabled", "Enable publication explicitly in this project's policy before private publication",
                               {"publication_scope": "local-private-only"}, 403)
        self._confirm(payload, review)
        package_id = _hash([REGISTRY_ID, payload["package_name"]])
        try:
            package = self.store.get("recipe_registry_package", package_id)
        except ServiceError as exc:
            if exc.code != "not_found":
                raise
            package = None
        if package is not None and package["project_id"] != payload["project_id"]:
            raise ServiceError("package_ownership_conflict", "This package name belongs to another source project; choose a new package name",
                               {"package_name": payload["package_name"]}, 409)
        publication_id = _hash([REGISTRY_ID, payload["package_name"], payload["package_version"]])
        try:
            self.store.get(PUBLICATION_KIND, publication_id)
        except ServiceError as exc:
            if exc.code != "not_found":
                raise
        else:
            raise ServiceError("publication_conflict", "This immutable package version already exists; choose a new version",
                               {"package_name": payload["package_name"], "package_version": payload["package_version"]}, 409)
        entry = {**review, "publication_id": publication_id, "revision": 1, "published_at": utcnow(),
                 "publication_review_digest": _hash(review), "bundle_json": text}
        entry["publication_digest"] = _hash(entry)
        if package is None:
            self.store.put("recipe_registry_package", package_id, payload["project_id"], {
                "schema_version": SCHEMA_VERSION, "package_id": package_id, "registry_id": REGISTRY_ID,
                "package_name": payload["package_name"], "project_id": payload["project_id"],
                "recipe_id": payload["recipe_id"], "revision": 1,
            })
        self.store.put(PUBLICATION_KIND, publication_id, payload["project_id"], entry)
        return self._metadata(entry)

    def _fork_review(self, payload, actor):
        entry = self._entry(payload["publication_id"], actor)
        target = copy.deepcopy(payload["target"])
        bundle = recipe_module.parse_bundle_json(entry["bundle_json"], max_bytes=MAX_BUNDLE_JSON_BYTES)
        body = copy.deepcopy(bundle["recipe"])
        origin = {key: entry[key] for key in ("registry_id", "publication_id", "publication_digest", "package_name",
                  "package_version", "recipe_id", "recipe_revision", "recipe_digest", "bundle_digest", "publisher_actor_id")}
        body.setdefault("extensions", {})["registry_origin"] = origin
        review = {"schema_version": SCHEMA_VERSION, "action": "recipe.fork", "registry_id": REGISTRY_ID,
                  "publication_id": entry["publication_id"], "publication_digest": entry["publication_digest"],
                  "package_name": entry["package_name"], "package_version": entry["package_version"],
                  "bundle_digest": entry["bundle_digest"], "source_recipe_digest": entry["recipe_digest"],
                  "target": target, "actor_id": actor.id, "import_report": entry["import_report"],
                  "licenses": entry["licenses"], "file_inventory": entry["file_inventory"],
                  "files_materialized": False, "execution_allowed": False, "trust_required": True}
        if target["mode"] == "existing_project":
            _authorize(actor, project_id=target["project_id"])
            self.service._get("project", target["project_id"], target["project_id"])
            current = self.service._current_recipe(target["project_id"])
            self.service._revision(current, target["expected_revision"])
            body.update(project_id=current["project_id"], recipe_id=current["recipe_id"], revision=current["revision"] + 1)
            review.update(current_recipe_id=current["recipe_id"], current_recipe_digest=current["digest"],
                          current_revision=current["revision"], next_revision=current["revision"] + 1,
                          diff=self.service._diff(_body(current), body), stable_recipe_identity_preserved=True)
        else:
            if actor.project_ids is not None:
                raise ServiceError("forbidden", "Project-scoped actors cannot create new projects", status_code=403)
            review.update(new_project_created_only_on_confirmation=True, new_recipe_identity_allocated_on_create=True)
        review["proposed_recipe"] = recipe_module.normalize_recipe(body)
        return review, body

    def _fork(self, payload, actor, *, preview):
        if not preview:
            _authorize(actor, ("read", "modify"))
        review, body = self._fork_review(payload, actor)
        if preview:
            return {"review": review, "review_digest": _hash(review), "can_import": "modify" in actor.scopes,
                    "confirmation_required": True, "execution_allowed": False}
        self._confirm(payload, review)
        target = payload["target"]
        if target["mode"] == "existing_project":
            project_id = target["project_id"]
            saved = self.service._save_recipe(body, project_id, review["current_recipe_id"],
                                              review["next_revision"], target["expected_revision"])
            self.service._confirm_recipe_review(project_id)
            project = self.service._get("project", project_id, project_id)
        else:
            project = self.service._project("create", {key: value for key, value in {**target, "recipe": body}.items()
                                                       if key != "mode"}, actor)
            saved = self.service._current_recipe(project["project_id"])
        # Retain inert bytes under the target project's authorization, including
        # if access to the original source project is later removed. The retained
        # bundle is explicitly the publisher's original recipe, not an export of
        # the target's newly revised manifest. No source-root writes occur.
        entry = self._entry(payload["publication_id"], actor)
        snapshot_id = _hash([project["project_id"], saved["recipe_id"], saved["revision"]])
        snapshot = {"schema_version": SCHEMA_VERSION, "snapshot_id": snapshot_id,
                    "project_id": project["project_id"], "recipe_id": saved["recipe_id"],
                    "recipe_revision": saved["revision"], "target_recipe_digest": saved["digest"],
                    "revision": 1, "origin": body["extensions"]["registry_origin"], "bundle_json": entry["bundle_json"]}
        snapshot["snapshot_digest"] = _hash(snapshot)
        self.store.put("recipe_import_snapshot", snapshot_id, project["project_id"], snapshot)
        imported = {**copy.deepcopy(review["import_report"]), "files_materialized": False,
                    "publication_id": payload["publication_id"], "registry_id": REGISTRY_ID,
                    "snapshot_id": snapshot_id}
        return {"project_id": project["project_id"], "recipe_id": saved["recipe_id"], "revision": saved["revision"],
                "recipe": saved, "project": project, "publication_id": payload["publication_id"],
                "import_report": imported, "target_mode": target["mode"], "execution_allowed": False}
