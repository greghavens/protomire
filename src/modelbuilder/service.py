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
                     PROJECT_FIELDS, CLAIM_FIELDS, HYPOTHESIS_FIELDS, ServiceError, new_id,
                     operation_catalog, utcnow)
from .store import SQLiteStore


class OperationService:
    def __init__(self, store: SQLiteStore | str | Path | None = None, backend=None, *,
                 db_path: str | Path | None = None, source_root: str | Path | None = None):
        self.store = store if isinstance(store, SQLiteStore) else SQLiteStore(db_path or store or "modelbuilder.sqlite3")
        self.backend = backend
        self.source_root = Path(source_root).resolve() if source_root else None

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
            if spec.scope not in actor.scopes:
                raise ServiceError("forbidden", f"Operation requires {spec.scope} scope", {"required_scope": spec.scope}, 403)
            if project_id and actor.project_ids is not None and project_id not in actor.project_ids:
                raise ServiceError("forbidden", "Actor cannot access this project", status_code=403)
            if operation == "project.create" and actor.project_ids is not None:
                raise ServiceError("forbidden", "Project-scoped actors cannot create new projects", status_code=403)
            errors = sorted(Draft202012Validator(spec.input_schema).iter_errors(payload), key=lambda e: list(map(str, e.path)))
            if errors:
                raise ServiceError("validation_error", "Operation payload does not match its schema", {
                    "errors": [{"path": ".".join(map(str, error.path)), "message": error.message} for error in errors]})
            self._reject_secrets(payload)
            hashed_payload = {key: value for key, value in payload.items() if key != "request_id"}
            payload_hash = hashlib.sha256(json.dumps(hashed_payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            with self.store.transaction():
                if project_id:
                    self._get("project", project_id, project_id)
                if spec.mutating:
                    previous = self.store.idempotent_result(actor.id, payload["idempotency_key"], operation, payload_hash)
                    if previous is not None:
                        return previous
                result = self._dispatch(operation, payload, actor)
                if spec.mutating:
                    self.store.save_idempotent(actor.id, payload["idempotency_key"], operation, payload_hash, result)
                    result_project = project_id or result.get("project_id")
                    target_id = next((result[k] for k in ("operation_id", "recipe_id", "evidence_id", "claim_id", "hypothesis_id", "decision_id", "project_id") if k in result), None)
                    self.store.audit(actor_id=actor.id, operation=operation, request_id=request_id,
                                     outcome="succeeded", project_id=result_project, target_id=target_id,
                                     before_revision=payload.get("expected_revision"), after_revision=result.get("revision"))
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

    def _dispatch(self, op, p, actor):
        if op.startswith("project."):
            return self._project(op[8:], p, actor)
        if op.startswith("recipe."):
            return self._recipe(op[7:], p)
        if op.startswith(("evidence.", "claim.", "hypothesis.", "decision.")):
            return self._research(op, p, actor)
        if op.startswith("policy.") or op == "cost.estimate":
            return self._policy(op, p)
        if op == "operation.get":
            return self._get("operation", p["operation_id"], p["project_id"])
        return self._execution(op, p, actor)

    def _project(self, action, p, actor):
        if action == "create":
            project_id, now = new_id(), utcnow()
            body = {key: value for key, value in p.items() if key in PROJECT_FIELDS}
            if body.get("research_watch_config"):
                raise ServiceError("capability_unavailable", "Automated research monitoring is not implemented", status_code=422)
            if body.get("active_recipe_id") or body.get("active_model_version_ref"):
                raise ServiceError("validation_error", "Create the project before linking its resources")
            project = ModelProject(project_id=project_id, id=project_id, created_at=now, updated_at=now,
                                   revision=1, **body).model_dump()
            self.store.reserve_slug(project["slug"], project_id)
            return self.store.put("project", project_id, project_id, project)
        if action == "list":
            projects = self.store.list("project")
            return {"projects": [p for p in projects if actor.project_ids is None or p["project_id"] in actor.project_ids]}
        project_id = p["project_id"]
        project = self._get("project", project_id, project_id)
        if action == "get":
            return project
        if action == "history":
            return {"events": self.store.history(project_id, p.get("limit", 200))}
        if action == "open":
            return {"project": project, "recipes": self.store.list("recipe", project_id),
                    "evidence": self.store.list("evidence", project_id), "claims": self.store.list("claim", project_id),
                    "hypotheses": self.store.list("hypothesis", project_id), "decisions": self.store.list("decision", project_id),
                    "runs": self.store.list("operation", project_id)}
        self._revision(project, p["expected_revision"])
        changes = p["changes"]
        self._allowed_changes(changes, PROJECT_FIELDS)
        if changes.get("active_recipe_id"):
            self._get("recipe", changes["active_recipe_id"], project_id)
        if changes.get("active_model_version_ref"):
            raise ServiceError("validation_error", "Use model.version.promote to select an authoritative model version")
        if changes.get("research_watch_config"):
            raise ServiceError("capability_unavailable", "Automated research monitoring is not implemented", status_code=422)
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
        if action == "list":
            return {"recipes": self.store.list("recipe", project_id)}
        if action == "create":
            recipe_id = new_id()
            body = p.get("recipe") or recipe_module.default_recipe(project_id, recipe_id, p.get("name", "New recipe"))
            if "name" in p:
                body = {**body, "name": p["name"]}
            return self._save_recipe(body, project_id, recipe_id, 1)
        if action == "import":
            report = recipe_module.import_bundle(p["bundle"])
            body = report["recipe"]
            if p.get("name"):
                body = {**body, "name": p["name"]}
            result = self._save_recipe(body, project_id, new_id(), 1)
            # Import never marks code trusted. Dependency inspection is explicit in its response.
            return {**result, "import_report": {"unresolved_dependencies": report.get("unresolved_dependencies", []),
                                                 "execution_allowed": False}}
        if action == "validate" and "recipe" in p:
            body = dict(p["recipe"])
            body.setdefault("project_id", project_id)
            body.setdefault("recipe_id", p.get("recipe_id", new_id()))
            body.setdefault("revision", 1)
            if body["project_id"] != project_id:
                raise ServiceError("validation_error", "Recipe project identity differs from request")
            normalized = recipe_module.normalize_recipe({k: v for k, v in body.items() if k != "digest"})
            return {"valid": True, "errors": [], "normalized": normalized, "digest": recipe_module.recipe_digest(normalized)}
        if "recipe_id" not in p:
            raise ServiceError("validation_error", "recipe_id or a recipe document is required")
        recipe = self._get("recipe", p["recipe_id"], project_id, p.get("revision") if action == "get" else None)
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
            return recipe_module.export_bundle({k: v for k, v in recipe.items() if k != "digest"}, source_root=self.source_root)
        if action == "resolve_dependencies":
            return recipe_module.resolve_dependencies(recipe, base_path=self.source_root)
        self._revision(recipe, p["expected_revision"], p.get("recipe"))
        if action == "clone":
            return self._save_recipe({**recipe, "name": p.get("name", recipe["name"] + " copy")}, project_id, new_id(), 1)
        incoming = p["recipe"]
        for key, expected in (("recipe_id", recipe["recipe_id"]), ("project_id", project_id)):
            if key in incoming and incoming[key] != expected:
                raise ServiceError("validation_error", "Recipe identity is immutable", {"field": key})
        if "revision" in incoming and incoming["revision"] not in {p["expected_revision"], p["expected_revision"] + 1}:
            raise ServiceError("validation_error", "Recipe body revision does not match update precondition")
        return self._save_recipe(incoming, project_id, recipe["recipe_id"], recipe["revision"] + 1, recipe["revision"])

    def _save_recipe(self, body, project_id, recipe_id, revision, expected=None):
        canonical = {key: value for key, value in body.items() if key not in {"digest", "import_report"}}
        canonical.update(recipe_id=recipe_id, project_id=project_id, revision=revision)
        normalized = recipe_module.normalize_recipe(canonical)
        result = {**normalized, "digest": recipe_module.recipe_digest(normalized)}
        return self.store.put("recipe", recipe_id, project_id, result, expected)

    def _check_links(self, body, project_id):
        for field, kind in (("evidence_ids", "evidence"), ("claim_ids", "claim"), ("supports", "claim"), ("contradicts", "claim")):
            for resource_id in body.get(field, []):
                self._get(kind, resource_id, project_id)

    def _research(self, op, p, actor):
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
            for backend_run_id in body["run_ids"]:
                correlation = self._find_operation(project_id, backend_run_id=backend_run_id)
                if not correlation.get("backend_run_id"):
                    raise ServiceError("validation_error", "Decision must link actual backend run identifiers")
            body.update(actor=actor.id, timestamp=now)
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
            if target != "local-cpu":
                raise ServiceError("remote_disabled", "Remote cost estimates require an authorized, tested accounting provider", status_code=403)
            return {"target": target, "currency": "USD", "provider_charge": 0, "total_cost": None,
                    "spending_enforced": False, "note": "Local CPU has no provider charge; electricity and hardware costs are not estimated."}
        if op == "policy.get":
            return policy
        self._revision(policy, p["expected_revision"])
        self._allowed_changes(p["changes"], {"allowed_targets", "max_local_runtime_seconds", "remote_execution_enabled",
                                            "publication_enabled", "untrusted_execution_enabled", "remote_spend_cap"})
        updated = {**policy, **p["changes"], "revision": policy["revision"] + 1}
        if any(updated[key] is not False for key in ("remote_execution_enabled", "publication_enabled", "untrusted_execution_enabled")):
            raise ServiceError("capability_unavailable", "Remote, publication, and untrusted execution are disabled in this slice", status_code=403)
        if updated["allowed_targets"] not in ([], ["local-cpu"]) or updated["remote_spend_cap"] != 0:
            raise ServiceError("remote_disabled", "Only the verified local CPU target is permitted", status_code=403)
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

    def _call_backend(self, operation, payload, actor):
        if self.backend is None:
            raise ServiceError("capability_unavailable", "No execution backend has been configured", status_code=503)
        try:
            result = self.backend.invoke(operation, payload, actor)
        except ServiceError:
            raise
        except Exception as exc:
            if hasattr(exc, "code"):
                raise ServiceError(exc.code, str(exc), getattr(exc, "details", {}),
                                   {"capability_unavailable": 503, "not_found": 404, "forbidden": 403, "conflict": 409}.get(exc.code, 422)) from exc
            raise ServiceError("backend_error", "Execution backend could not complete the operation", {"exception": type(exc).__name__}, 502) from exc
        if not isinstance(result, dict):
            raise ServiceError("backend_contract_error", "Backend did not return a structured response", status_code=502)
        return result

    def _execution(self, op, p, actor):
        project_id = p["project_id"]
        project = self._get("project", project_id, project_id)
        enriched = {**p, "model_name": project["model_name"]}
        if op == "run.list":
            runs = []
            for operation in self.store.list("operation", project_id):
                runs.append(self._execution("run.status", {"project_id": project_id, "operation_id": operation["operation_id"]}, actor))
            return {"runs": runs}
        if op in {"run.plan", "run.submit"}:
            recipe = self._get("recipe", p["recipe_id"], project_id)
            enriched["recipe"] = recipe
            target = p.get("target", "local-cpu")
            policy = self._policy("policy.get", p)
            if target != "local-cpu" or target not in policy["allowed_targets"]:
                raise ServiceError("remote_disabled", "Execution target is not permitted; remote spending is disabled", status_code=403)
            enriched.update(target=target, max_runtime_seconds=policy["max_local_runtime_seconds"])
            if op == "run.submit":
                self._revision(recipe, p["expected_revision"])
                if p.get("trusted") is not True:
                    raise ServiceError("untrusted_execution_blocked", "Explicit trust consent is required for repository-owned fixtures", status_code=403)
                enriched["operation_id"] = str(uuid5(NAMESPACE_URL, f"modelbuilder:{actor.id}:{op}:{p['idempotency_key']}"))
        correlation = None
        if op in {"run.status", "run.logs", "run.cancel", "run.artifacts", "run.resume", "model.checkpoint.get"}:
            correlation = self._find_operation(project_id, p.get("operation_id"), p.get("backend_run_id"))
            enriched.update(operation_id=correlation["operation_id"], backend_run_id=correlation.get("backend_run_id"))
            if op == "run.resume":
                if p.get("trusted") is not True:
                    raise ServiceError("untrusted_execution_blocked", "Explicit fixture trust is required to resume", status_code=403)
                policy = self._policy("policy.get", p)
                if "local-cpu" not in policy["allowed_targets"]:
                    raise ServiceError("target_disabled", "Local execution is disabled by project policy", status_code=403)
                enriched["parent_operation_id"] = correlation["operation_id"]
                enriched["operation_id"] = str(uuid5(NAMESPACE_URL, f"modelbuilder:{actor.id}:{op}:{p['idempotency_key']}"))
                parent_recipe = self._get("recipe", correlation["recipe_id"], project_id, correlation["recipe_revision"])
                resumed_recipe = copy.deepcopy(parent_recipe)
                if "steps" not in p:
                    raise ServiceError("validation_error", "Resume requires a declared final total step count")
                for node in resumed_recipe["nodes"]:
                    if node["kind"] == "model.train":
                        node["parameters"]["steps"] = p["steps"]
                resumed_recipe["name"] = parent_recipe["name"][:220] + " continuation"
                resumed_recipe_id = str(uuid5(NAMESPACE_URL, f"modelbuilder:resume-recipe:{enriched['operation_id']}"))
                enriched["recipe"] = self._save_recipe(resumed_recipe, project_id, resumed_recipe_id, 1)
                enriched["recipe_id"] = resumed_recipe_id
                enriched["max_runtime_seconds"] = policy["max_local_runtime_seconds"]
                if not p.get("mode", p.get("resume_mode")):
                    raise ServiceError("validation_error", "Resume must explicitly select exact_state or warm_start")
                enriched["mode"] = p.get("mode", p.get("resume_mode"))
        if op == "run.compare":
            for run_id in p["run_ids"]:
                self._find_operation(project_id, backend_run_id=run_id)
        if op == "model.version.promote":
            self._revision(project, p["expected_revision"])
            decision = self._get("decision", p["decision_id"], project_id)
            if decision["outcome"] != "accepted":
                raise ServiceError("validation_error", "Only an accepted decision can promote a model version")
            enriched["decision"] = decision
            enriched["metric"] = decision["selection_rule"]["metric"]
            enriched["direction"] = decision["selection_rule"]["direction"]
            enriched["candidate_run_ids"] = decision["run_ids"]
        result = self._call_backend(op, enriched, actor)
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
                      "revision": 1, "status": result.get("status", "unknown"), "errors": result.get("errors") or ([result["error"]] if result.get("error") else [])}
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
