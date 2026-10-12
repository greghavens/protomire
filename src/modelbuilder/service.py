"""The sole business entrypoint for the workbench, HTTP and native MCP."""
from __future__ import annotations

import difflib
import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlparse
from uuid import NAMESPACE_URL, uuid5

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from . import recipe as recipe_module
from .models import (Actor, Claim, Decision, Evidence, Hypothesis, ModelProject, OPERATIONS,
                     PROJECT_FIELDS, CLAIM_FIELDS, HYPOTHESIS_FIELDS, MAX_BUNDLE_JSON_BYTES, ServiceError, new_id,
                     operation_catalog, utcnow)
from .source_provenance import verify_export_git_provenance
from .store import SQLiteStore
from .tracking import resolve_tracking, disclosure
from .backend.placement import is_local_target, device_from_recipe, target_for_device


class OperationService:
    def __init__(self, store: SQLiteStore | str | Path | None = None, backend=None, *,
                 db_path: str | Path | None = None, source_root: str | Path | None = None,
                 dataset_roots: dict | None = None, source_roots: dict | None = None, dataset_limits: dict | None = None):
        self.store = store if isinstance(store, SQLiteStore) else SQLiteStore(db_path or store or "modelbuilder.sqlite3")
        self.research_authorizer = None
        self.operation_authorizer = None
        self.research_scheduler_running = False
        self.backend = backend
        self._remote_cost_operations = None
        self.dataset_roots = copy.deepcopy(dataset_roots or {})
        from .dataset import DatasetRegistry
        self._dataset_registry = DatasetRegistry(self.store, project_roots=self.dataset_roots, streaming_limits=dataset_limits)
        self.source_root = Path(source_root).resolve() if source_root else None
        from .source_workspace import SourceWorkspaces
        self.require_source_binding = source_roots is not None
        if source_roots is None and getattr(backend, "repository", None):
            root = backend.repository
            source_roots = {"default": {"path": str(root), "trusted": any(root.is_relative_to(Path(p)) for p in backend.trusted_code_roots),
                                        "dependency_paths": backend.dependency_paths}}
        self.source_workspaces = SourceWorkspaces(source_roots)
        self._migrate_single_recipe()

    def _migrate_single_recipe(self):
        """Bind one editable identity; retain every legacy manifest and run reference byte-for-byte."""
        with self.store.transaction():
            for project in self.store.list("project"):
                project_id = project["project_id"]
                if self.store.recipe_binding(project_id):
                    continue
                recipes = self.store.list("recipe", project_id)
                by_id = {recipe["recipe_id"]: recipe for recipe in recipes}
                chosen = project.get("active_recipe_id")
                reason = "existing_active_pointer"
                if chosen not in by_id:
                    chosen = next((run["recipe_id"] for run in reversed(self.store.list("operation", project_id))
                                   if run.get("recipe_id") in by_id), None)
                    reason = "most_recent_run"
                if chosen is None and recipes:
                    chosen, reason = recipes[-1]["recipe_id"], "most_recent_recipe"
                if chosen is None:
                    chosen, reason = str(uuid5(NAMESPACE_URL, f"modelbuilder:project-recipe:{project_id}")), "created_default_draft"
                    self._save_recipe(recipe_module.default_recipe(project_id, chosen, project["model_name"] + " recipe"),
                                      project_id, chosen, 1)
                archived = [recipe_id for recipe_id in by_id if recipe_id != chosen]
                report = {"version": "single-recipe-v1", "migrated_at": utcnow(), "selection_reason": reason,
                          "archived_recipe_ids": archived, "review_required": bool(archived and reason != "existing_active_pointer"),
                          "history_preserved": True, "run_references_rewritten": False}
                self.store.bind_recipe(project_id, chosen, report)
                updated = {**project, "recipe_id": chosen, "active_recipe_id": chosen,
                           "recipe_migration": report, "revision": project["revision"] + 1, "updated_at": utcnow()}
                self.store.put("project", project_id, project_id, updated, project["revision"])
                self.store.audit(actor_id="system:single-recipe-migration", operation="project.recipe_migrate",
                                 request_id=new_id(), outcome="succeeded", project_id=project_id, target_id=chosen,
                                 before_revision=project["revision"], after_revision=updated["revision"], details=report)

    def _current_recipe(self, project_id, requested_id=None):
        binding = self.store.recipe_binding(project_id)
        if not binding:
            raise ServiceError("invariant_error", "Project has no canonical recipe binding", status_code=500)
        if requested_id is not None and requested_id != binding["recipe_id"]:
            self._get("recipe", requested_id, project_id)
            raise ServiceError("recipe_not_current", "Historical recipes are read-only; open the model's current recipe",
                               {"current_recipe_id": binding["recipe_id"]}, 409)
        return self._get("recipe", binding["recipe_id"], project_id)

    def _confirm_recipe_review(self, project_id):
        binding = self.store.recipe_binding(project_id)
        if binding and binding["migration"].get("review_required"):
            report = {**binding["migration"], "review_required": False, "reviewed_at": utcnow()}
            self.store.update_recipe_migration(project_id, report)
            project = self._get("project", project_id, project_id)
            self.store.put("project", project_id, project_id,
                           {**project, "recipe_migration": report, "updated_at": utcnow(), "revision": project["revision"] + 1},
                           project["revision"])

    def _require_recipe_review(self, project_id):
        binding = self.store.recipe_binding(project_id)
        if binding and binding["migration"].get("review_required"):
            raise ServiceError("migration_review_required", "Review and save the current recipe before execution; previous recipe identities remain in history",
                               {"current_recipe_id": binding["recipe_id"], "migration": binding["migration"]}, 409)

    def catalog(self, actor: Actor | None = None) -> list[dict]:
        if actor is not None and "read" not in actor.scopes:
            raise ServiceError("forbidden", "Catalog requires read scope", status_code=403)
        return operation_catalog()

    def invoke(self, operation: str, payload: dict, actor: Actor) -> dict:
        """Validate, authorize, atomically dispatch, audit and deduplicate a request."""
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        request_id = request_id if isinstance(request_id, str) and request_id else new_id()
        project_id = payload.get("project_id") if isinstance(payload, dict) else None
        project_id = project_id if isinstance(project_id, str) else None
        spec = OPERATIONS.get(operation)
        try:
            if not isinstance(actor, Actor):
                raise ServiceError("unauthenticated", "Explicit actor identity is required", status_code=401)
            if spec is None:
                code = "capability_unavailable" if operation in {"recipe.publish", "recipe.discover", "recipe.fork"} else "unknown_operation"
                raise ServiceError(code, "Operation is not available", {"operation": operation}, 404)
            missing_scopes = set(spec.required_scopes) - actor.scopes
            if missing_scopes:
                raise ServiceError("forbidden", "Operation requires scopes: " + ", ".join(spec.required_scopes),
                                   {"required_scope": spec.scope, "required_scopes": list(spec.required_scopes),
                                    "missing_scopes": sorted(missing_scopes)}, 403)
            if project_id and actor.project_ids is not None and project_id not in actor.project_ids:
                raise ServiceError("forbidden", "Actor cannot access this project", status_code=403)
            if operation == "project.create" and actor.project_ids is not None:
                raise ServiceError("forbidden", "Project-scoped actors cannot create new projects", status_code=403)
            errors = sorted(Draft202012Validator(spec.input_schema).iter_errors(payload), key=lambda e: list(map(str, e.path)))
            if errors:
                raise ServiceError("validation_error", "Operation payload does not match its schema", {
                    "errors": [{"path": ".".join(map(str, error.path)), "message": "Invalid tracking configuration" if "tracking" in error.path else error.message} for error in errors]})
            self._reject_secrets(payload)
            hashed_payload = {key: value for key, value in payload.items() if key != "request_id"}
            payload_hash = hashlib.sha256(json.dumps(hashed_payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            if operation.startswith("cost.") and operation != "cost.estimate":
                from .remote.operations import authorize_cost_operation
                authorize_cost_operation(operation, payload, actor)
            if operation == "policy.update":
                from .recipe_registry import authorize_publication_policy_change
                authorize_publication_policy_change(payload, actor)
            if operation == "evidence.search" and payload.get("provider") == "openalex":
                # Metadata discovery is read-only. Do not hold SQLite's writer
                # lock during the same bounded provider call used by watches.
                with self.store.transaction():
                    self._get("project", project_id, project_id)
                return self._research(operation, payload, actor)
            if operation == "research.text.fetch":
                from .research_content import ContentRetrieval
                return ContentRetrieval(self).invoke(operation, payload, actor, payload_hash, request_id)
            if operation == "research.document.import":
                from .research_documents import DocumentImport
                return DocumentImport(self).invoke(operation, payload, actor, payload_hash, request_id)
            if operation in {"research.watch.refresh", "research.watch.refresh_due"}:
                from .research_scheduler import ResearchRefresh
                return ResearchRefresh(self).invoke(operation, payload, actor, payload_hash, request_id)
            if operation == "execution.targets":
                # Optional driver/runtime probes must not hold the SQLite writer
                # transaction or its process-wide lock. This read has no writes;
                # authorization and payload validation above remain mandatory.
                return self._execution(operation, payload, actor)
            if operation == "run.compare":
                return self._compare_unlocked(payload, actor)
            if operation in {"run.plan", "run.submit", "run.resume", "run.status", "run.cancel", "run.list", "run.logs", "run.artifacts", "run.evaluations.list", "run.events.list"}:
                from .run_preparation import invoke as invoke_run
                return invoke_run(self, operation, payload, actor, payload_hash, request_id)
            dataset_state, dataset_values, placement_values = None, None, None
            from .dataset_preparation import OPERATIONS as DATASET_PREPARED_OPERATIONS
            if operation in DATASET_PREPARED_OPERATIONS:
                from .dataset_preparation import prepare
                dataset_state, dataset_values, previous, placement_values = prepare(self, operation, payload, actor, payload_hash)
                if previous is not None:
                    return previous
            prepared = None
            if operation == "model.version.promote" or (
                operation == "decision.record" and payload["selection_rule"].get("comparison_digest")
            ):
                with self.store.transaction():
                    self._require_current_authority(operation, actor, project_id)
                    self._get("project", project_id, project_id)
                    previous = self.store.idempotent_result(actor.id, payload["idempotency_key"], operation, payload_hash)
                    if previous is not None:
                        return previous
                    snapshot = self._evidence_snapshot(operation, payload)
                if operation == "model.version.promote":
                    evidence = self._call_backend(operation, copy.deepcopy(snapshot["enriched"]), actor,
                                                  prepare_promotion=True)
                    if evidence is None:
                        raise ServiceError("backend_contract_error", "Promotion preparation returned no evidence handle", status_code=502)
                else:
                    evidence = self._call_backend("run.compare", copy.deepcopy(snapshot["comparison_payload"]), actor)
                prepared = (snapshot, evidence)
            with self.store.transaction():
                if prepared is not None or dataset_state is not None:
                    self._require_current_authority(operation, actor, project_id)
                if project_id:
                    self._get("project", project_id, project_id)
                if spec.mutating:
                    previous = self.store.idempotent_result(actor.id, payload["idempotency_key"], operation, payload_hash)
                    if previous is not None:
                        if operation in {"project.create", "project.source.bind"} and "source_alias" in payload:
                            # Revoked source grants cannot be restored by an old receipt.
                            # Do not inspect, rebind, or rewrite its historical checkout identity.
                            self.source_workspaces.settings(payload["source_alias"], previous["project_id"])
                        if operation == "recipe.source.pin":
                            self.source_workspaces.settings(previous["source_binding"]["alias"], previous["project_id"])
                        if operation == "recipe.fork":
                            from .recipe_registry import RecipeRegistry
                            RecipeRegistry(self).authorize_cached_result(operation, payload, actor, previous)
                        self._check_idempotent_recipe_identity(operation, previous)
                        return previous
                if dataset_state is not None:
                    from .dataset_preparation import snapshot
                    if snapshot(self, operation, payload)[0] != dataset_state:
                        raise ServiceError("dataset_preparation_changed", "Project, dataset or source binding changed during preparation; retry explicitly", status_code=409)
                if prepared is not None and self._evidence_snapshot(operation, payload) != prepared[0]:
                    raise ServiceError("comparison_changed", "Decision or execution evidence changed; prepare again", status_code=409)
                from .dataset import materialization_session
                from .backend.placement import probe_session
                with materialization_session(dataset_values or {}, compute=False), probe_session(placement_values or {}, compute=False):
                    result = self._dispatch(operation, payload, actor,
                                            prepared_evidence=prepared[1] if prepared is not None else None)
                if spec.mutating:
                    self.store.save_idempotent(actor.id, payload["idempotency_key"], operation, payload_hash, result)
                    result_project = project_id or result.get("project_id")
                    target_id = next((result[k] for k in ("operation_id", "recipe_id", "evidence_id", "claim_id", "hypothesis_id", "decision_id", "dataset_id", "watch_id", "finding_id", "proposal_id", "text_id", "project_id") if k in result), None)
                    before_revision = payload.get("expected_revision")
                    if operation == "recipe.publish":
                        target_id, before_revision = result["publication_id"], None
                    elif operation == "recipe.fork":
                        before_revision = payload["target"].get("expected_revision")
                    self.store.audit(actor_id=actor.id, operation=operation, request_id=request_id,
                                     outcome="succeeded", project_id=result_project, target_id=target_id,
                                     before_revision=before_revision, after_revision=result.get("revision"))
                return result
        except (ServiceError, ValidationError, ValueError) as exc:
            if isinstance(exc, ServiceError):
                error = exc
            elif isinstance(exc, ValidationError):
                error = ServiceError("validation_error", "Resource validation failed", {"errors": json.loads(exc.json(include_input=False))})
            elif hasattr(exc, "code"):
                error = ServiceError(exc.code, str(exc), getattr(exc, "details", {}))
            else:
                error = ServiceError("validation_error", str(exc))
            self.store.audit(actor_id=getattr(actor, "id", "unauthenticated"), operation=operation,
                             request_id=request_id, outcome="denied" if error.status_code in {401, 403} else "failed",
                             project_id=project_id, details={"code": error.code})
            raise error from exc

    def _check_idempotent_recipe_identity(self, operation, previous):
        """A pre-migration cache cannot revive a retired editable identity."""
        if operation not in {"project.create", "recipe.create", "recipe.update", "recipe.clone", "recipe.import", "recipe.source.pin", "dataset.bind"}:
            return
        project_id = previous.get("project_id")
        binding = self.store.recipe_binding(project_id) if project_id else None
        if not binding:
            return
        if previous.get("recipe_id") != binding["recipe_id"] or (
            operation == "project.create" and previous.get("active_recipe_id") != binding["recipe_id"]
        ):
            raise ServiceError("migration_conflict", "This idempotency record predates the single-recipe migration; read the current project and use a new key for a new mutation",
                               {"current_recipe_id": binding["recipe_id"], "historical_response_preserved": True}, 409)

    @staticmethod
    def _reject_secrets(value):
        """Do not persist credential fields or credential-bearing URLs in manifests."""
        forbidden = {"password", "passwd", "access_token", "token", "api_key", "apikey", "secret_key", "private_key",
                     "authorization", "refresh_token", "client_secret", "credentials", "aws_secret_access_key",
                     "aws_access_key_id", "session_token"}
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in forbidden:
                    raise ServiceError("secret_rejected", "Credentials must not be stored in project resources")
                OperationService._reject_secrets(item)
        elif isinstance(value, list):
            for item in value:
                OperationService._reject_secrets(item)
        elif isinstance(value, str) and "://" in value:
            parsed = urlparse(value)
            if parsed.username or parsed.password:
                raise ServiceError("secret_rejected", "Credential-bearing URLs are forbidden")
            sensitive_query = forbidden | {"signature", "sig", "key", "x-amz-signature", "x-amz-credential",
                                           "x-amz-security-token", "x-goog-signature", "x-goog-credential"}
            if any(key.lower().replace("-", "_") in {k.replace("-", "_") for k in sensitive_query}
                   for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
                raise ServiceError("secret_rejected", "Credential-bearing or signed URL queries are forbidden")

    def _get(self, kind, resource_id, project_id, revision=None):
        resource = self.store.get(kind, resource_id, revision)
        if resource.get("project_id") != project_id:
            raise ServiceError("not_found", f"{kind} does not belong to this project", status_code=404)
        return resource

    @staticmethod
    def _revision(resource, expected, proposed=None):
        if resource["revision"] != expected:
            details = {"expected_revision": expected, "client_revision": expected,
                       "current_revision": resource["revision"], "current": resource}
            if proposed is not None:
                details["diff"] = OperationService._diff(proposed, resource)
            raise ServiceError("revision_conflict", "Resource changed since it was read; reload or merge the changes", details, 409)

    @staticmethod
    def _diff(before, after):
        return "\n".join(difflib.unified_diff(
            json.dumps(before, indent=2, sort_keys=True).splitlines(),
            json.dumps(after, indent=2, sort_keys=True).splitlines(), fromfile="client", tofile="server", lineterm=""))

    def _dispatch(self, op, p, actor, *, prepared_evidence=None):
        if op in {"recipe.source.pin.preview", "recipe.source.pin"}:
            from .source_pinning import dispatch
            return dispatch(self, op, p)
        if op.startswith("cost.") and op != "cost.estimate":
            from .remote.ledger import CostLedger
            from .remote.operations import CostOperations
            if self._remote_cost_operations is None:
                ledger_path = ":memory:" if self.store.path == ":memory:" else self.store.path + ".cost.sqlite3"
                self._remote_cost_operations = CostOperations(CostLedger(ledger_path))
            return self._remote_cost_operations.invoke(op, p, actor)
        from .recipe_registry_contract import REGISTRY_OPERATIONS
        if op in REGISTRY_OPERATIONS:
            from .recipe_registry import RecipeRegistry
            return RecipeRegistry(self).dispatch(op, p, actor)
        if op.startswith("research."):
            from .research import ResearchWorkflow
            return ResearchWorkflow(self).dispatch(op, p, actor)
        if op.startswith("dataset."):
            return self._dataset(op, p)
        if op.startswith("component."):
            return self._component(op, p)
        if op == "evaluation.validate":
            from .evaluation import validate_evaluation_record
            diagnostics = validate_evaluation_record(p["record"])
            return {"valid": not diagnostics, "diagnostics": diagnostics}
        if op.startswith("project."):
            return self._project(op[8:], p, actor)
        if op.startswith("recipe."):
            return self._recipe(op[7:], p)
        if op.startswith(("evidence.", "claim.", "hypothesis.", "decision.")):
            return self._research(op, p, actor, prepared_comparison=prepared_evidence)
        if op.startswith("policy.") or op == "cost.estimate":
            return self._policy(op, p)
        if op == "operation.get":
            return self._get("operation", p["operation_id"], p["project_id"])
        return self._execution(op, p, actor, prepared_promotion=prepared_evidence)

    def _dataset(self, op, p):
        registry = self._dataset_registry
        project_id = p["project_id"]
        if op == "dataset.bind":
            from .dataset_binding import bind
            return bind(self, p)
        if op == "dataset.register":
            return registry.register(project_id, p["manifest"])
        if op == "dataset.version":
            return registry.version(project_id, p["dataset_id"], p["expected_revision"], p["manifest"])
        if op == "dataset.get":
            return registry.get(project_id, p["dataset_id"], p.get("revision"))
        if op == "dataset.list":
            return registry.list(project_id)
        if op == "dataset.preview":
            return registry.preview(project_id, p["dataset_id"], p["revision"], p["split_id"],
                                    purpose=p["purpose"], limit=p.get("limit", 20), offset=p.get("offset", 0))
        if ("manifest" in p) == ("dataset_id" in p) or ("revision" in p and "dataset_id" not in p):
            raise ServiceError("validation_error", "Provide a manifest or dataset_id with optional revision, not both")
        return registry.validate(project_id, p.get("manifest"), dataset_id=p.get("dataset_id"), revision=p.get("revision"))

    def _component(self, op, p):
        from .backend.components import component_catalog, validate_component
        from jsonschema.exceptions import SchemaError, ValidationError as SchemaValidationError
        if op == "component.validate":
            try:
                descriptor = validate_component(p["node"])
            except (ValueError, KeyError, TypeError, SchemaError, SchemaValidationError):
                return {"valid": False, "diagnostics": [{"code": "invalid_component", "message": "Component declaration or parameters are invalid"}]}
            return {"valid": True, "component": descriptor, "diagnostics": []}
        project_id = p["project_id"]
        current = self._current_recipe(project_id)
        recipe = self._get("recipe", current["recipe_id"], project_id, p["revision"]) if "revision" in p else current
        try:
            components = component_catalog(recipe)
        except (ValueError, KeyError, TypeError, SchemaError, SchemaValidationError) as exc:
            raise ServiceError("invalid_component", "Recipe contains an invalid component declaration") from exc
        if op == "component.list":
            return {"components": components, "recipe_id": recipe["recipe_id"], "recipe_revision": recipe["revision"]}
        matches = [item for item in components if item["id"] == p["component_id"] and ("node_id" not in p or item.get("node_id") == p["node_id"])]
        if not matches:
            raise ServiceError("not_found", "Component is not declared in this recipe", status_code=404)
        if len(matches) != 1:
            raise ServiceError("ambiguous_component", "Specify node_id to select this component declaration")
        return matches[0]

    def _project(self, action, p, actor):
        if action == "source.list":
            if not p.get("project_id") and actor.project_ids is not None:
                raise ServiceError("forbidden", "Project-scoped actors must specify their project", status_code=403)
            return {"sources": self.source_workspaces.list(p.get("project_id")),
                    "binding_required": self.require_source_binding, "arbitrary_paths_allowed": False}
        if action == "create":
            project_id, now = new_id(), utcnow()
            body = {key: value for key, value in p.items() if key in PROJECT_FIELDS}
            if body.get("research_watch_config"):
                raise ServiceError("capability_unavailable", "Use research.watch.schedule_enable for an explicit, disclosed per-watch schedule", status_code=422)
            if body.get("active_recipe_id") or body.get("active_model_version_ref"):
                raise ServiceError("validation_error", "Create the project before linking its resources")
            recipe_id = str(uuid5(NAMESPACE_URL, f"modelbuilder:project-recipe:{project_id}"))
            body.pop("active_recipe_id", None)
            project = ModelProject(project_id=project_id, id=project_id, created_at=now, updated_at=now,
                                   recipe_id=recipe_id, active_recipe_id=recipe_id, revision=1, **body).model_dump()
            if "source_alias" in p:
                project["source_binding"] = self.source_workspaces.bind(p["source_alias"], project_id, actor.id, p.get("trusted"), p.get("source_repository_id"))
            elif p.get("trusted") is not None or "source_repository_id" in p:
                raise ServiceError("validation_error", "Source trust must accompany an approved source alias")
            self.store.reserve_slug(project["slug"], project_id)
            initial = p.get("recipe") or recipe_module.default_recipe(project_id, recipe_id, project["model_name"] + " recipe")
            self._save_recipe(initial, project_id, recipe_id, 1)
            self.store.bind_recipe(project_id, recipe_id)
            return self.store.put("project", project_id, project_id, project)
        if action == "list":
            projects = self.store.list("project")
            return {"projects": [p for p in projects if actor.project_ids is None or p["project_id"] in actor.project_ids]}
        project_id = p["project_id"]
        project = self._get("project", project_id, project_id)
        if action == "source.status":
            return self.source_workspaces.status(project, self._current_recipe(project_id))
        if action == "source.bind":
            self._revision(project, p["expected_revision"])
            binding = self.source_workspaces.bind(p["source_alias"], project_id, actor.id, p["trusted"], p["source_repository_id"])
            updated = {**project, "source_binding": binding, "revision": project["revision"] + 1, "updated_at": utcnow()}
            return self.store.put("project", project_id, project_id, updated, project["revision"])
        if action == "get":
            return project
        if action == "history":
            return {"events": self.store.history(project_id, p.get("limit", 200))}
        if action == "open":
            current_recipe = self._current_recipe(project_id)
            return {"project": project, "recipe": current_recipe, "recipes": [current_recipe],
                    "evidence": self.store.list("evidence", project_id), "claims": self.store.list("claim", project_id),
                    "hypotheses": self.store.list("hypothesis", project_id), "decisions": self.store.list("decision", project_id),
                    "runs": self.store.list("operation", project_id),
                    "research_watches": self.store.list("research_watch", project_id),
                    "research_findings": self.store.list("research_finding", project_id),
                    "research_proposals": self.store.list("research_proposal", project_id),
                    "source": self.source_workspaces.status(project, current_recipe)}
        self._revision(project, p["expected_revision"])
        changes = p["changes"]
        self._allowed_changes(changes, PROJECT_FIELDS)
        if "active_recipe_id" in changes and changes["active_recipe_id"] != project["recipe_id"]:
            raise ServiceError("recipe_identity_immutable", "A model project owns one stable recipe identity", status_code=409)
        if changes.get("active_model_version_ref"):
            raise ServiceError("validation_error", "Use model.version.promote to select an authoritative model version")
        if changes.get("research_watch_config"):
            raise ServiceError("capability_unavailable", "Use research.watch.schedule_enable for an explicit, disclosed per-watch schedule", status_code=422)
        updated = ModelProject(**{**project, **changes, "updated_at": utcnow(), "revision": project["revision"] + 1}).model_dump()
        if project["slug"] != updated["slug"]:
            self.store.replace_slug(project["slug"], updated["slug"], project_id)
        return self.store.put("project", project_id, project_id, updated, project["revision"])

    @staticmethod
    def _allowed_changes(changes, allowed):
        unknown = set(changes) - set(allowed)
        if unknown:
            raise ServiceError("validation_error", "Immutable or unknown fields cannot be changed", {"fields": sorted(unknown)})

    def _recipe(self, action, p):
        project_id = p["project_id"]
        current = self._current_recipe(project_id)
        if action == "list":
            return {"recipes": [current], "current_recipe_id": current["recipe_id"]}
        if action == "history":
            binding = self.store.recipe_binding(project_id)
            legacy = [recipe for recipe in self.store.list("recipe", project_id) if recipe["recipe_id"] != current["recipe_id"]]
            return {"recipe_id": current["recipe_id"], "current_revision": current["revision"],
                    "revisions": self.store.resource_versions("recipe", current["recipe_id"]),
                    "legacy_recipes": [{"recipe_id": recipe["recipe_id"], "name": recipe["name"],
                                        "last_revision": recipe["revision"], "digest": recipe["digest"], "read_only": True}
                                       for recipe in legacy], "migration": binding["migration"]}
        if action in {"create", "import"}:
            self._revision(current, p["expected_revision"], p.get("recipe"))
            report = None
            if action == "import":
                bundle = recipe_module.parse_bundle_json(p["bundle_json"], max_bytes=MAX_BUNDLE_JSON_BYTES) if "bundle_json" in p else p["bundle"]
                self._reject_secrets(bundle)
                report = recipe_module.import_bundle(bundle)
                body = report["recipe"]
            else:
                body = p.get("recipe") or recipe_module.default_recipe(project_id, current["recipe_id"], p.get("name", current["name"]))
            if "name" in p:
                body = {**body, "name": p["name"]}
            result = self._save_recipe(body, project_id, current["recipe_id"], current["revision"] + 1, current["revision"])
            self._confirm_recipe_review(project_id)
            if report is not None:
                return {**result, "import_report": {"unresolved_dependencies": report.get("unresolved_dependencies", []),
                                                     "execution_allowed": False}}
            return result
        if action == "validate" and "recipe" in p:
            body = dict(p["recipe"])
            body.setdefault("project_id", project_id)
            body.setdefault("recipe_id", current["recipe_id"])
            body.setdefault("revision", current["revision"])
            if body["project_id"] != project_id or body["recipe_id"] != current["recipe_id"]:
                raise ServiceError("validation_error", "Recipe identity differs from this model's canonical recipe")
            normalized = recipe_module.normalize_recipe({k: v for k, v in body.items() if k != "digest"})
            return {"valid": True, "errors": [], "normalized": normalized, "digest": recipe_module.recipe_digest(normalized)}
        requested_id = p.get("recipe_id", current["recipe_id"])
        if action == "get" and "revision" in p:
            # Exact legacy manifests remain readable for historical native-run provenance.
            return self._get("recipe", requested_id, project_id, p["revision"])
        recipe = self._current_recipe(project_id, requested_id)
        if action == "get":
            return recipe
        if action == "validate":
            normalized = recipe_module.normalize_recipe({k: v for k, v in recipe.items() if k != "digest"})
            return {"valid": True, "errors": [], "normalized": normalized, "digest": recipe["digest"]}
        if action == "diff":
            before = p.get("recipe") or self._get("recipe", recipe["recipe_id"], project_id, p.get("from_revision", recipe["revision"]))
            after = self._get("recipe", recipe["recipe_id"], project_id, p.get("to_revision", recipe["revision"]))
            return {"recipe_id": recipe["recipe_id"], "current_revision": recipe["revision"],
                    "equal": before == after, "diff": self._diff(before, after)}
        if action == "export":
            bundle = self._export_project_recipe(recipe)
            if p.get("format", "object") == "json":
                text = recipe_module.canonical_json(bundle)
                if len(text.encode("utf-8")) > MAX_BUNDLE_JSON_BYTES:
                    raise ServiceError("bundle_limit", "Raw bundle JSON exceeds the 8 MiB transport limit", status_code=413)
                return {"bundle_json": text, "digest": bundle["digest"], "bundle_digest": bundle["bundle_digest"],
                        "media_type": "application/json"}
            return bundle
        if action == "resolve_dependencies":
            return recipe_module.resolve_dependencies(recipe, base_path=self._project_source_root(project_id))
        self._revision(recipe, p["expected_revision"], p.get("recipe"))
        if action == "clone":
            incoming = self._get("recipe", recipe["recipe_id"], project_id, p.get("source_revision", recipe["revision"]))
            if "name" in p:
                incoming = {**incoming, "name": p["name"]}
        else:
            incoming = p["recipe"]
            for key, expected in (("recipe_id", recipe["recipe_id"]), ("project_id", project_id)):
                if key in incoming and incoming[key] != expected:
                    raise ServiceError("validation_error", "Recipe identity is immutable", {"field": key})
            if "revision" in incoming and incoming["revision"] not in {p["expected_revision"], p["expected_revision"] + 1}:
                raise ServiceError("validation_error", "Recipe body revision does not match update precondition")
        result = self._save_recipe(incoming, project_id, recipe["recipe_id"], recipe["revision"] + 1, recipe["revision"])
        self._confirm_recipe_review(project_id)
        return result

    def _recipe_candidate(self, body, project_id, recipe_id, revision):
        canonical = {key: value for key, value in body.items() if key not in {"digest", "import_report"}}
        canonical.update(recipe_id=recipe_id, project_id=project_id, revision=revision)
        normalized = recipe_module.normalize_recipe(canonical)
        result = {**normalized, "digest": recipe_module.recipe_digest(normalized)}
        return result

    def _save_recipe(self, body, project_id, recipe_id, revision, expected=None):
        result = self._recipe_candidate(body, project_id, recipe_id, revision)
        return self.store.put("recipe", recipe_id, project_id, result, expected)

    def _check_links(self, body, project_id):
        for field, kind in (("evidence_ids", "evidence"), ("claim_ids", "claim"), ("supports", "claim"), ("contradicts", "claim")):
            for resource_id in body.get(field, []):
                self._get(kind, resource_id, project_id)

    def _research(self, op, p, actor, *, prepared_comparison=None):
        kind, action = op.split(".")
        project_id = p["project_id"]
        if kind == "evidence" and action == "search" and p.get("provider") == "openalex":
            from .literature import LiteratureUnavailable, search_openalex
            try:
                records = search_openalex(p.get("query", ""), p.get("limit", 5))
            except LiteratureUnavailable as exc:
                raise ServiceError("source_unavailable", str(exc), status_code=503) from exc
            return {"evidence": records, "provider": "openalex", "persisted": False, "claim_verified": False}
        if action in {"list", "search"}:
            records = self.store.list(kind, project_id)
            query = p.get("query", "").casefold()
            if query:
                records = [r for r in records if query in json.dumps(r, ensure_ascii=False).casefold()]
            return {{"evidence": "evidence", "claim": "claims", "hypothesis": "hypotheses", "decision": "decisions"}[kind]: records}
        if action == "get":
            return self._get(kind, p[f"{kind}_id"], project_id)
        if action == "update":
            original = self._get(kind, p[f"{kind}_id"], project_id)
            self._revision(original, p["expected_revision"])
            self._allowed_changes(p["changes"], CLAIM_FIELDS if kind == "claim" else HYPOTHESIS_FIELDS)
            body = {**original, **p["changes"], "revision": original["revision"] + 1, "updated_at": utcnow()}
            self._check_links(body, project_id)
            body = (Claim if kind == "claim" else Hypothesis)(**body).model_dump()
            return self.store.put(kind, p[f"{kind}_id"], project_id, body, original["revision"])
        resource_id, now = new_id(), utcnow()
        body = {key: value for key, value in p.items() if key not in {"idempotency_key", "request_id"}}
        body.update({f"{kind}_id": resource_id, "revision": 1})
        if kind == "evidence":
            if not body.get("source_url") and not body.get("source_identifier"):
                raise ServiceError("validation_error", "Evidence requires a source URL or scholarly identifier")
            if body.get("source_url") and urlparse(body["source_url"]).scheme not in {"https", "http"}:
                raise ServiceError("validation_error", "Evidence source URLs must use HTTP or HTTPS")
            body["retrieved_at"] = now
            body = Evidence(**body).model_dump()
        elif kind == "decision":
            self._get("hypothesis", body["hypothesis_id"], project_id)
            self._get("recipe", body["recipe_id"], project_id, body["recipe_revision"])
            if not body["selection_rule"].get("metric") or body["selection_rule"].get("direction") not in {"minimize", "maximize"}:
                raise ServiceError("validation_error", "Selection rule requires metric and direction minimize/maximize")
            if len(set(body["run_ids"])) != len(body["run_ids"]):
                raise ServiceError("validation_error", "Decision run identifiers must be distinct")
            provenance = []
            for backend_run_id in body["run_ids"]:
                correlation = self._find_operation(project_id, backend_run_id=backend_run_id)
                if not correlation.get("backend_run_id"):
                    raise ServiceError("validation_error", "Decision must link actual backend run identifiers")
                provenance.append({"backend_run_id": backend_run_id, "operation_id": correlation["operation_id"],
                    "recipe_id": correlation["recipe_id"], "recipe_revision": correlation["recipe_revision"],
                    "recipe_digest": correlation["recipe_digest"]})
            if not any(item["recipe_id"] == body["recipe_id"] and item["recipe_revision"] == body["recipe_revision"] for item in provenance):
                raise ServiceError("validation_error", "Decision recipe revision was not executed by any linked run")
            if body["selection_rule"].get("comparison_digest"):
                if prepared_comparison is None:
                    raise ServiceError("backend_contract_error", "Decision comparison requires an unlocked preparation phase", status_code=502)
                comparison = prepared_comparison
                if body["outcome"] == "accepted" and not comparison.get("compatible"):
                    raise ServiceError("incompatible_evaluation", "An accepted decision requires compatible evaluation evidence", comparison)
                if comparison.get("comparison_digest") != body["selection_rule"]["comparison_digest"]:
                    raise ServiceError("comparison_changed", "Comparison evidence changed; compare again before recording the decision", status_code=409)
            body.update(actor=actor.id, timestamp=now, run_provenance=provenance)
            body = Decision(**body).model_dump()
        else:
            self._check_links(body, project_id)
            body.update(created_at=now, updated_at=now)
            if kind == "hypothesis" and not body["acceptance_metric"].get("name"):
                raise ServiceError("validation_error", "Hypothesis acceptance_metric requires a measurable name")
            body = (Claim if kind == "claim" else Hypothesis)(**body).model_dump()
        return self.store.put(kind, resource_id, project_id, body)

    def _policy(self, op, p):
        project_id = p["project_id"]
        try:
            policy = self.store.get("policy", project_id)
        except ServiceError as error:
            if error.code != "not_found":
                raise
            policy = {"project_id": project_id, "revision": 1, "remote_execution_enabled": False,
                      "publication_enabled": False, "untrusted_execution_enabled": False,
                      "allowed_targets": ["local-cpu"], "max_local_runtime_seconds": 600,
                      "spending_enforced": False, "remote_spend_cap": 0}
        if op == "cost.estimate":
            target = p.get("target", "local-cpu")
            if not is_local_target(target):
                raise ServiceError("remote_disabled", "Remote cost estimates require an authorized, tested accounting provider", status_code=403)
            return {"target": target, "currency": "USD", "provider_charge": 0, "total_cost": None,
                    "spending_enforced": False, "note": "Local execution has no provider charge; electricity and hardware costs are not estimated."}
        if op == "policy.get":
            return policy
        self._revision(policy, p["expected_revision"])
        self._allowed_changes(p["changes"], {"allowed_targets", "max_local_runtime_seconds", "remote_execution_enabled",
                                            "publication_enabled", "untrusted_execution_enabled", "remote_spend_cap"})
        updated = {**policy, **p["changes"], "revision": policy["revision"] + 1}
        if any(updated[key] is not False for key in ("remote_execution_enabled", "untrusted_execution_enabled")):
            raise ServiceError("capability_unavailable", "Remote and untrusted execution are disabled; publication is limited to the local private registry", status_code=403)
        if (not isinstance(updated["allowed_targets"], list) or not all(is_local_target(target) for target in updated["allowed_targets"])
                or updated["remote_spend_cap"] != 0):
            raise ServiceError("remote_disabled", "Only explicit local placement targets are permitted; runtime/profile admission is checked separately", status_code=403)
        maximum = updated["max_local_runtime_seconds"]
        if type(maximum) is not int or not 1 <= maximum <= 3600:
            raise ServiceError("validation_error", "Local runtime limit must be an integer from 1 to 3600 seconds")
        try:
            self.store.get("policy", project_id)
            expected = policy["revision"]
        except ServiceError:
            expected = None
        return self.store.put("policy", project_id, project_id, updated, expected)

    def _find_operation(self, project_id, operation_id=None, backend_run_id=None):
        if operation_id:
            record = self._get("operation", operation_id, project_id)
            if backend_run_id and record.get("backend_run_id") != backend_run_id:
                raise ServiceError("validation_error", "Operation and backend run identifiers do not match")
            return record
        if backend_run_id:
            for record in self.store.list("operation", project_id):
                if record.get("backend_run_id") == backend_run_id:
                    return record
        raise ServiceError("not_found", "No matching run in this project", status_code=404)

    def _comparison_snapshot(self, payload):
        """Capture ownership and immutable execution provenance, not poll revisions."""
        project_id = payload["project_id"]
        project = self._get("project", project_id, project_id)
        configurations = []
        for run_id in payload["run_ids"]:
            executed = self._find_operation(project_id, backend_run_id=run_id)
            manifest = self._get("recipe", executed["recipe_id"], project_id, executed["recipe_revision"])
            configurations.append({"backend_run_id": run_id, "operation_id": executed["operation_id"],
                "recipe_id": executed["recipe_id"], "recipe_revision": executed["recipe_revision"],
                "recipe_digest": executed["recipe_digest"],
                "operation_identity": {key: value for key, value in executed.items()
                                       if key not in {"revision", "status", "last_reconciled_at", "errors", "dispatch_state"}},
                "configuration": {key: manifest.get(key) for key in
                    ("nodes", "edges", "source", "environment", "external_dependencies", "evaluation")}})
        enriched = {**payload, "model_name": project["model_name"], "project_recipe_id": project["recipe_id"]}
        return enriched, configurations

    def _require_current_authority(self, operation, actor, project_id):
        if (not set(OPERATIONS[operation].required_scopes).issubset(actor.scopes)
                or (actor.project_ids is not None and project_id not in actor.project_ids)
                or (self.operation_authorizer is not None
                    and not self.operation_authorizer(operation, actor, project_id))):
            raise ServiceError("forbidden", "Operation authority changed during evidence preparation", status_code=403)

    def _evidence_snapshot(self, operation, payload):
        """Only stable project/research inputs and immutable run provenance cross I/O."""
        project_id = payload["project_id"]
        project = self._get("project", project_id, project_id)
        if operation == "model.version.promote":
            self._revision(project, payload["expected_revision"])
            decision = self._get("decision", payload["decision_id"], project_id)
            if decision["outcome"] != "accepted":
                raise ServiceError("validation_error", "Only an accepted decision can promote a model version")
            enriched = {**payload, "model_name": project["model_name"], "project_recipe_id": project["recipe_id"],
                        "decision": decision, "metric": decision["selection_rule"]["metric"],
                        "direction": decision["selection_rule"]["direction"], "candidate_run_ids": decision["run_ids"]}
            if decision["selection_rule"].get("evaluations"):
                enriched["evaluations"] = decision["selection_rule"]["evaluations"]
            _, provenance = self._comparison_snapshot({"project_id": project_id, "run_ids": decision["run_ids"]})
            return {"project": project, "decision": decision, "enriched": enriched, "provenance": provenance}
        hypothesis = self._get("hypothesis", payload["hypothesis_id"], project_id)
        recipe = self._get("recipe", payload["recipe_id"], project_id, payload["recipe_revision"])
        rule = payload["selection_rule"]
        if not rule.get("metric") or rule.get("direction") not in {"minimize", "maximize"}:
            raise ServiceError("validation_error", "Selection rule requires metric and direction minimize/maximize")
        if len(set(payload["run_ids"])) != len(payload["run_ids"]):
            raise ServiceError("validation_error", "Decision run identifiers must be distinct")
        comparison_payload, provenance = self._comparison_snapshot({"project_id": project_id,
            "run_ids": payload["run_ids"], "metric": rule["metric"], "direction": rule["direction"],
            "comparison_contract": rule.get("comparison_contract", {}),
            **({"evaluations": rule["evaluations"]} if rule.get("evaluations") else {})})
        if not any(item["recipe_id"] == payload["recipe_id"] and item["recipe_revision"] == payload["recipe_revision"]
                   for item in provenance):
            raise ServiceError("validation_error", "Decision recipe revision was not executed by any linked run")
        return {"project": project, "hypothesis": hypothesis, "recipe": recipe,
                "comparison_payload": comparison_payload, "provenance": provenance}

    def _compare_unlocked(self, payload, actor):
        """Native verification must not hold the service lock while polls arrive."""
        with self.store.transaction():
            self._require_current_authority("run.compare", actor, payload["project_id"])
            enriched, configurations = self._comparison_snapshot(payload)
        result = self._call_backend("run.compare", copy.deepcopy(enriched), actor)
        with self.store.transaction():
            self._require_current_authority("run.compare", actor, payload["project_id"])
            current, current_configurations = self._comparison_snapshot(payload)
            if current != enriched or current_configurations != configurations:
                raise ServiceError("comparison_changed", "Run ownership or execution provenance changed; compare again",
                                   status_code=409)
        result["configuration_diffs"] = [{"baseline_run_id": configurations[0]["backend_run_id"],
            "backend_run_id": item["backend_run_id"], "recipe_revision": item["recipe_revision"],
            "diff": self._diff(configurations[0]["configuration"], item["configuration"])}
            for item in configurations[1:]]
        result["run_provenance"] = [{key: value for key, value in item.items()
                                  if key not in {"configuration", "operation_id", "operation_identity"}}
                                  for item in configurations]
        return result

    def _project_source_root(self, project_id):
        project = self._get("project", project_id, project_id)
        if not project.get("source_binding") and not self.require_source_binding:
            return self.source_root
        source, _ = self.source_workspaces.resolve(project.get("source_binding"), project_id)
        return Path(source["workspace_path"])

    def _export_project_recipe(self, recipe):
        root = self._project_source_root(recipe["project_id"])
        body = {key: value for key, value in recipe.items() if key != "digest"}
        bundle = recipe_module.export_bundle(body, source_root=root)
        verify_export_git_provenance(bundle, root)
        return bundle

    def _source_backend(self, operation, payload):
        if operation not in {"execution.targets", "run.plan", "run.submit", "run.resume"}:
            return self.backend
        project = self._get("project", payload["project_id"], payload["project_id"])
        binding = project.get("source_binding")
        if operation == "run.resume" and payload.get("mode", payload.get("resume_mode")) == "exact_state":
            parent = self._find_operation(payload["project_id"], payload.get("parent_operation_id"))
            binding = parent.get("source_binding")
            if binding is None and project.get("source_binding"):
                raise ServiceError("source_binding_required", "Historical run has no approved source binding; exact resume cannot substitute the current checkout", status_code=409)
        if binding is None and not self.require_source_binding:
            return self.backend
        source, settings = self.source_workspaces.resolve(binding, payload["project_id"], execute=True)
        if not hasattr(self.backend, "with_source_workspace"):
            raise ServiceError("capability_unavailable", "Execution backend does not support project source bindings", status_code=503)
        payload["source_binding"] = copy.deepcopy(binding)
        payload["source_workspace"] = copy.deepcopy(source)
        return self.backend.with_source_workspace(source["workspace_path"], trusted=settings["trusted"],
                                                  dependency_paths=settings["dependency_paths"])

    def _call_backend(self, operation, payload, actor, *, prepare_promotion=False, prepared_promotion=None):
        if self.backend is None:
            raise ServiceError("capability_unavailable", "No execution backend has been configured", status_code=503)
        try:
            try:
                backend = self._source_backend(operation, payload)
                if operation in {"run.plan", "run.submit", "run.resume"}:
                    from .dataset_binding import resolve_backend
                    backend = resolve_backend(self, backend, payload)
            except ServiceError as error:
                if operation == "execution.targets":
                    result = self.backend.invoke(operation, payload, actor)
                    for target in result.get("targets", []):
                        target.update(available=False, unavailable_reason=error.message)
                    result["source_diagnostics"] = [{"code": error.code, "message": error.message}]
                    return result
                if operation != "run.plan":
                    raise
                return {"project_id": payload["project_id"], "executable": False, "blocked_reasons": [
                    {"code": error.code, "message": error.message, "details": error.details}], "warnings": [error.message]}
            if prepare_promotion:
                prepare = getattr(backend, "prepare_promotion", None)
                if not callable(prepare):
                    raise ServiceError("capability_unavailable", "Backend does not support unlocked promotion preparation", status_code=503)
                result = prepare(payload, actor)
            elif prepared_promotion is not None:
                result = backend.invoke(operation, payload, actor, prepared_promotion=prepared_promotion)
            else:
                result = backend.invoke(operation, payload, actor)
            if operation in {"execution.targets", "run.plan"} and payload.get("source_workspace"):
                result["source_workspace"] = copy.deepcopy(payload["source_workspace"])
        except ServiceError:
            raise
        except Exception as exc:
            if hasattr(exc, "code"):
                raise ServiceError(exc.code, str(exc), getattr(exc, "details", {}),
                                   {"capability_unavailable": 503, "not_found": 404, "forbidden": 403, "conflict": 409}.get(exc.code, 422)) from exc
            raise ServiceError("backend_error", "Execution backend could not complete the operation", {"exception": type(exc).__name__}, 502) from exc
        if not prepare_promotion and not isinstance(result, dict):
            raise ServiceError("backend_contract_error", "Backend did not return a structured response", status_code=502)
        return result

    def _execution(self, op, p, actor, *, prepared_promotion=None, prepare_only=False):
        project_id = p["project_id"]
        project = self._get("project", project_id, project_id)
        enriched = {**p, "model_name": project["model_name"], "project_recipe_id": project["recipe_id"]}
        if op in {"run.plan", "run.submit", "run.resume"}:
            enriched["tracking"] = resolve_tracking(project.get("tracking"), p.get("tracking"),
                online_consent=op == "run.plan" or p.get("tracking_online_consent", False))
            enriched["tracking_online_consent"] = p.get("tracking_online_consent") is True
        if op == "run.list":
            runs = []
            for operation in self.store.list("operation", project_id):
                runs.append(self._execution("run.status", {"project_id": project_id, "operation_id": operation["operation_id"]}, actor))
            return {"runs": runs}
        if op in {"run.plan", "run.submit"}:
            recipe = self._current_recipe(project_id, p["recipe_id"])
            self._require_recipe_review(project_id)
            enriched["recipe"] = recipe
            if "expected_revision" in p:
                self._revision(recipe, p["expected_revision"])
            if "execution" in p:
                from .execution import selection
                enriched["execution"] = selection(recipe, p["execution"])
                for binding in enriched["execution"]["input_bindings"]:
                    self._find_operation(project_id, backend_run_id=binding["backend_run_id"])
            target = p.get("target", "local-cpu")
            policy = self._policy("policy.get", p)
            if not is_local_target(target) or target not in policy["allowed_targets"]:
                raise ServiceError("remote_disabled", "Execution target is not permitted; remote spending is disabled", status_code=403)
            enriched.update(target=target, max_runtime_seconds=policy["max_local_runtime_seconds"])
            if op == "run.submit":
                self._revision(recipe, p["expected_revision"])
                if p.get("trusted") is not True:
                    raise ServiceError("untrusted_execution_blocked", "Explicit trust consent is required for source-bound execution", status_code=403)
                enriched["operation_id"] = str(uuid5(NAMESPACE_URL, f"modelbuilder:{actor.id}:{op}:{p['idempotency_key']}"))
        correlation = None
        if op in {"run.status", "run.logs", "run.cancel", "run.artifacts", "run.resume", "model.checkpoint.get", "run.evaluations.list", "run.events.list"}:
            correlation = self._find_operation(project_id, p.get("operation_id"), p.get("backend_run_id"))
            enriched.update(operation_id=correlation["operation_id"], backend_run_id=correlation.get("backend_run_id"))
            if op == "run.resume":
                if p.get("trusted") is not True:
                    raise ServiceError("untrusted_execution_blocked", "Explicit source trust is required to resume", status_code=403)
                policy = self._policy("policy.get", p)
                enriched["parent_operation_id"] = correlation["operation_id"]
                enriched["operation_id"] = str(uuid5(NAMESPACE_URL, f"modelbuilder:{actor.id}:{op}:{p['idempotency_key']}"))
                self._require_recipe_review(project_id)
                current_recipe = self._current_recipe(project_id)
                self._revision(current_recipe, p["expected_recipe_revision"])
                parent_recipe = self._get("recipe", correlation["recipe_id"], project_id, correlation["recipe_revision"])
                mode = p.get("mode", p.get("resume_mode"))
                if mode not in {"exact_state", "warm_start"}:
                    raise ServiceError("validation_error", "Resume must explicitly select exact_state or warm_start")
                # Resolve the saved cut before trainer validation and dataset
                # dependency admission. Native provenance may enrich bindings;
                # only their original public pins survive into a fresh resolve.
                from .execution import continuation_selection, selected_recipe
                resumed_recipe = copy.deepcopy(current_recipe if mode == "warm_start" else parent_recipe)
                parent_execution = continuation_selection(parent_recipe, parent_recipe, correlation.get("execution"), "exact_state")
                enriched["execution"] = continuation_selection(parent_recipe, resumed_recipe, correlation.get("execution"), mode)
                resume_target = target_for_device(device_from_recipe(resumed_recipe))
                if resume_target not in policy["allowed_targets"]:
                    raise ServiceError("target_disabled", "Selected local device is disabled by project policy", status_code=403)
                if mode == "exact_state" and (resume_target != "local-cpu" or device_from_recipe(parent_recipe) != "cpu"):
                    raise ServiceError("capability_unavailable", "Accelerator exact-state continuation is not hardware-verified; use warm_start")
                enriched["target"] = resume_target
                parent_nodes = selected_recipe(parent_recipe, parent_execution)["nodes"]
                resumed_nodes = selected_recipe(resumed_recipe, enriched["execution"])["nodes"]
                component_training = [node for node in parent_nodes
                    if node["kind"] == "component.run" and node.get("component", {}).get("role") == "training"]
                if component_training:
                    if "steps" in p:
                        raise ServiceError("validation_error", "Component resume does not infer training parameter names; exact resume retains the parent budget, while warm start uses the edited current recipe")
                    if len(component_training) != 1 or any(node["kind"] == "model.train" for node in parent_nodes):
                        raise ServiceError("capability_unavailable", "Resume requires one unambiguous training component")
                    selected_trainers = [node for node in resumed_nodes
                        if node["kind"] == "component.run" and node.get("component", {}).get("role") == "training"]
                    if len(selected_trainers) != 1 or any(node["kind"] == "model.train" for node in resumed_nodes):
                        raise ServiceError("capability_unavailable", "Selected recipe must have exactly one checkpoint-consuming training component")
                else:
                    selected_trainers = [node for node in resumed_nodes if node["kind"] == "model.train"]
                    if len(selected_trainers) != 1 or any(node["kind"] == "component.run" and
                            node.get("component", {}).get("role") == "training" for node in resumed_nodes):
                        raise ServiceError("capability_unavailable", "Selected recipe must have exactly one checkpoint-consuming built-in training node")
                    if "steps" not in p:
                        raise ServiceError("validation_error", "Resume requires a declared final total step count")
                    for node in resumed_nodes:
                        if node["kind"] == "model.train":
                            node["parameters"]["steps"] = p["steps"]
                if parent_recipe["recipe_id"] != current_recipe["recipe_id"]:
                    enriched["legacy_parent_recipe_id"] = parent_recipe["recipe_id"]
                resumed_recipe["name"] = current_recipe["name"]
                enriched["recipe"] = self._recipe_candidate(resumed_recipe, project_id, current_recipe["recipe_id"],
                                                             current_recipe["revision"] + 1)
                if not prepare_only:
                    self.store.put("recipe", current_recipe["recipe_id"], project_id, enriched["recipe"], current_recipe["revision"])
                enriched["recipe_id"] = current_recipe["recipe_id"]
                enriched["max_runtime_seconds"] = policy["max_local_runtime_seconds"]
                if not p.get("mode", p.get("resume_mode")):
                    raise ServiceError("validation_error", "Resume must explicitly select exact_state or warm_start")
                enriched["mode"] = p.get("mode", p.get("resume_mode"))
        if op.startswith("model.export."):
            if not p.get("backend_run_id") and not p.get("model_version_id"):
                raise ServiceError("validation_error", "Select a native run or model version")
            if p.get("backend_run_id"):
                self._find_operation(project_id, backend_run_id=p["backend_run_id"])
        if op == "run.compare":
            for run_id in p["run_ids"]:
                self._find_operation(project_id, backend_run_id=run_id)
        if op == "model.version.promote":
            if prepared_promotion is None:
                raise ServiceError("backend_contract_error", "Promotion requires an unlocked preparation phase", status_code=502)
            self._revision(project, p["expected_revision"])
            decision = self._get("decision", p["decision_id"], project_id)
            if decision["outcome"] != "accepted":
                raise ServiceError("validation_error", "Only an accepted decision can promote a model version")
            enriched["decision"] = decision
            enriched["metric"] = decision["selection_rule"]["metric"]
            enriched["direction"] = decision["selection_rule"]["direction"]
            enriched["candidate_run_ids"] = decision["run_ids"]
            if decision["selection_rule"].get("evaluations"):
                enriched["evaluations"] = decision["selection_rule"]["evaluations"]
        if prepare_only:
            return enriched
        result = self._call_backend(op, enriched, actor, prepared_promotion=prepared_promotion)
        if op == "run.compare":
            configurations = []
            for run_id in p["run_ids"]:
                executed = self._find_operation(project_id, backend_run_id=run_id)
                manifest = self._get("recipe", executed["recipe_id"], project_id, executed["recipe_revision"])
                configurations.append({"backend_run_id": run_id, "recipe_id": executed["recipe_id"],
                    "recipe_revision": executed["recipe_revision"], "recipe_digest": executed["recipe_digest"],
                    "configuration": {key: manifest.get(key) for key in ("nodes", "edges", "source", "environment", "external_dependencies", "evaluation")}})
            result["configuration_diffs"] = [{"baseline_run_id": configurations[0]["backend_run_id"],
                "backend_run_id": item["backend_run_id"], "recipe_revision": item["recipe_revision"],
                "diff": self._diff(configurations[0]["configuration"], item["configuration"])} for item in configurations[1:]]
            result["run_provenance"] = [{key: value for key, value in item.items() if key != "configuration"} for item in configurations]
        if op == "run.plan":
            result = {**result, "tracking": disclosure(enriched["tracking"])}
        if op in {"run.submit", "run.resume", "run.status", "run.cancel"}:
            self._check_run_result(result, enriched, correlation if op in {"run.status", "run.cancel"} else None)
        if op in {"run.submit", "run.resume"}:
            operation_id = enriched["operation_id"]
            if result.get("operation_id", operation_id) != operation_id:
                raise ServiceError("backend_contract_error", "Backend changed the application correlation identity", status_code=502)
            recipe = enriched["recipe"]
            record = {"operation_id": operation_id, "project_id": project_id,
                      "backend_run_id": result.get("backend_run_id"), "recipe_id": recipe["recipe_id"],
                      "recipe_revision": recipe["revision"], "recipe_digest": recipe["digest"],
                      "created_at": utcnow(), "last_reconciled_at": utcnow(),
                      "revision": 1, "tracking_config": enriched["tracking"],
                      "source_binding": copy.deepcopy(enriched.get("source_binding")),
                      "source_workspace": copy.deepcopy(enriched.get("source_workspace")),
                      "execution": copy.deepcopy(result.get("execution", enriched.get("execution"))),
                      "status": result.get("status", "unknown"), "errors": result.get("errors") or ([result["error"]] if result.get("error") else [])}
            self.store.put("operation", operation_id, project_id, record)
            return {**result, **record}
        if op in {"run.status", "run.cancel"}:
            record = {**correlation, "backend_run_id": result.get("backend_run_id") or correlation.get("backend_run_id"),
                      "status": result.get("status", "unknown"), "last_reconciled_at": utcnow(),
                      "errors": result.get("errors") or ([result["error"]] if result.get("error") else []), "revision": correlation["revision"] + 1}
            self.store.put("operation", record["operation_id"], project_id, record, correlation["revision"])
            return {**result, **record}
        if op == "model.version.promote":
            version = result.get("model_version_id") or result.get("model_version_ref") or p.get("model_version_id") or p.get("model_version_ref")
            if not version:
                raise ServiceError("backend_contract_error", "Promotion returned no authoritative model-version identifier", status_code=502)
            self.store.put("project", project_id, project_id,
                           {**project, "active_model_version_ref": version, "revision": project["revision"] + 1, "updated_at": utcnow()},
                           project["revision"])
        return result

    @staticmethod
    def _check_run_result(result, request, previous=None):
        statuses = {"pending", "submitted", "running", "cancel_requested", "cancelled", "failed", "succeeded", "unknown"}
        if not isinstance(result.get("status"), str) or result["status"] not in statuses:
            raise ServiceError("backend_contract_error", "Backend returned an unsupported run state", status_code=502)
        if result.get("backend_run_id") is not None and (not isinstance(result["backend_run_id"], str) or not result["backend_run_id"]):
            raise ServiceError("backend_contract_error", "Backend run identity must be a nonempty string or null", status_code=502)
        if result.get("operation_id", request["operation_id"]) != request["operation_id"]:
            raise ServiceError("backend_contract_error", "Backend changed the application operation identity", status_code=502)
        if result.get("project_id", request["project_id"]) != request["project_id"]:
            raise ServiceError("backend_contract_error", "Backend returned a run from another project", status_code=502)
        if previous:
            known, returned = previous.get("backend_run_id"), result.get("backend_run_id")
            if known and returned and known != returned:
                raise ServiceError("backend_contract_error", "Backend changed an authoritative run identity", status_code=502)
            if previous.get("status") in {"succeeded", "failed", "cancelled"} and result["status"] != previous["status"]:
                raise ServiceError("backend_contract_error", "Backend attempted to change a terminal run state", status_code=502)
        if result["status"] == "cancelled" and result.get("worker_alive") is True:
            raise ServiceError("backend_contract_error", "A live worker cannot be reported cancelled", status_code=502)
        if result["status"] == "cancelled" and not (
            result.get("worker_alive") is False or result.get("confirmed") is True
            or result.get("cancellation_acknowledged") is True or result.get("termination_confirmed") is True
        ):
            raise ServiceError("backend_contract_error", "Cancellation requires confirmed worker termination", status_code=502)
