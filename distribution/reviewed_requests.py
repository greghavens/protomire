"""Native-reviewed exact requests; no mutating bearer credential leaves the core.

The journal is authorization state in the existing service database. Business
operations still execute through OperationService and its existing dispatcher.
"""
from __future__ import annotations

import copy
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import threading
import time
from typing import Callable
import uuid
import weakref

from jsonschema import Draft202012Validator
from modelbuilder.models import Actor, OPERATIONS, ServiceError
from modelbuilder.service import OperationService

PENDING_TTL = 15 * 60
ACTION_TTL = 30 * 60
MAX_PENDING = 32
MAX_PAYLOAD = 16 * 1024 * 1024
TERMINAL_TRANSFERS = {"succeeded", "failed", "cancelled", "interrupted", "needs_reconciliation"}
COMBINED_PUBLISH = "desktop.publish.reviewed"


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def fail(code, message, status=409):
    raise ServiceError(code, message, status_code=status)


@dataclass(eq=False)
class Grant:
    actor: Actor
    request_id: str
    project_id: str
    operations: dict[str, str]
    expires: float
    snapshot: dict
    revoked: bool = False
    dispatched: bool = False
    transfer_id: str | None = None
    reason: str | None = None
    invocations: set[str] = field(default_factory=set)
    research_admitted: bool = False


class ReviewGrants:
    """Capabilities are actual Actor instances, never actor ID strings or tokens."""
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._values: dict[int, Grant] = {}
        self._lock = threading.RLock()
        self._retired = {}
        self.queue = None
        self.invocation = ContextVar("desktop_review_invocation", default=None)

    def add(self, grant: Grant):
        with self._lock:
            self._values[id(grant.actor)] = grant

    def find(self, actor: Actor) -> Grant | None:
        with self._lock:
            grant = self._values.get(id(actor))
            return grant if grant is not None and grant.actor is actor else None

    def authorize(self, operation, actor, project_id):
        grant = self.find(actor)
        if grant is None:
            return False
        if grant.revoked or self.clock() >= grant.expires:
            grant.revoked = True
            grant.reason = grant.reason or "expired"
            return False
        return operation in grant.operations and (project_id == grant.project_id or operation == "project.create")

    def guard(self, operation, payload, actor):
        grant = self.find(actor)
        if grant is None:
            retired = self._retired.get(id(actor))
            if retired is not None and retired() is actor:
                fail("review_grant_revoked", "This completed exact-request authorization cannot be reused", 403)
            return
        if not self.authorize(operation, actor, payload.get("project_id")):
            fail("review_grant_revoked", "The exact-request authorization is no longer active", 403)
        if digest(payload) != grant.operations.get(operation):
            fail("review_payload_changed", "The approved operation payload changed", 403)
        if not grant.dispatched:
            self.queue.check_snapshot(grant.project_id, grant.snapshot)

    def authorize_research(self, actor_id, project_id):
        context = self.invocation.get()
        if context is None:
            return None
        operation, actor = context
        if actor.id != actor_id:
            return None
        grant = self.find(actor)
        if (grant is None or operation not in {"research.watch.refresh", "research.watch.refresh_due", "research.text.fetch", "research.document.import"}
                or not self.authorize(operation, actor, project_id)):
            return False
        self.queue.check_snapshot(project_id, grant.snapshot)
        if not grant.research_admitted:
            # Called by the existing manual research path inside its initial
            # store transaction, before it captures any watches/provider work.
            record = self.queue._get(grant.request_id)
            if record["disclosure"] != self.queue._disclosure(operation, record["payload"], project_id):
                return False
            grant.research_admitted = True
        return True

    def claim_invocation(self, operation, actor):
        with self._lock:
            grant = self.find(actor)
            if grant is not None:
                if operation in grant.invocations:
                    fail("review_replayed", "This exact approved invocation has already been dispatched", 403)
                grant.invocations.add(operation)

    def revoke(self, request_id, reason="owner_revoked"):
        with self._lock:
            for grant in self._values.values():
                if grant.request_id == request_id:
                    grant.revoked, grant.reason = True, reason

    def remove(self, grant):
        with self._lock:
            identity = id(grant.actor)
            self._values.pop(identity, None)
            self._retired[identity] = weakref.ref(grant.actor, lambda reference: self._retired.pop(identity, None))

    def active(self):
        with self._lock:
            return list(self._values.values())


class ReviewedOperationService(OperationService):
    """Guard privileged invocations and common write admission inside its transaction."""
    review_grants: ReviewGrants | None = None

    def invoke(self, operation, payload, actor):
        grants = self.review_grants
        if grants is not None:
            grants.guard(operation, payload, actor)
            grants.claim_invocation(operation, actor)
        context = grants.invocation.set((operation, actor)) if grants is not None and grants.find(actor) is not None else None
        try:
            return super().invoke(operation, payload, actor)
        finally:
            if context is not None:
                grants.invocation.reset(context)

    def _dispatch(self, operation, payload, actor, **kwargs):
        if self.review_grants is not None:
            self.review_grants.guard(operation, payload, actor)
        return super()._dispatch(operation, payload, actor, **kwargs)

    def _require_current_authority(self, operation, actor, project_id):
        super()._require_current_authority(operation, actor, project_id)
        grant = self.review_grants.find(actor) if self.review_grants is not None else None
        if grant is not None and not grant.dispatched:
            self.review_grants.queue.check_snapshot(grant.project_id, grant.snapshot)


class ReviewQueue:
    def __init__(self, service, grants: ReviewGrants, clock: Callable[[], float] = time.time):
        self.service, self.store, self.grants, self.clock = service, service.store, grants, clock
        grants.queue = self
        service.review_grants = grants
        with self.store.transaction():
            self.store.connection.execute("""CREATE TABLE IF NOT EXISTS desktop_reviews (
                id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, project_id TEXT NOT NULL,
                operation TEXT NOT NULL, request_key TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL,
                UNIQUE(actor_id, operation, request_key))""")
            # A prior process may have crossed an external side-effect boundary.
            # Never auto-replay an uncertain invocation after restarting the app.
            for row in self.store.connection.execute("SELECT id,body FROM desktop_reviews WHERE status IN ('executing','active','revoking')").fetchall():
                body = json.loads(row["body"])
                self._save({**body, "status": "indeterminate", "notice": "Core restarted; inspect the correlated operation or transfer before retrying."})
            for row in self.store.connection.execute("SELECT id,body FROM desktop_reviews WHERE status='pending'").fetchall():
                self._save({**json.loads(row["body"]), "status": "revoked", "notice": "The requesting core session ended before approval."})

    def _get(self, identity):
        row = self.store.connection.execute("SELECT body FROM desktop_reviews WHERE id=?", (identity,)).fetchone()
        if row is None:
            fail("review_not_found", "Review request is unavailable", 404)
        return json.loads(row["body"])

    def _save(self, record):
        self.store.connection.execute("UPDATE desktop_reviews SET status=?,body=? WHERE id=?", (record["status"], canonical(record), record["id"]))

    def _audit(self, record, actor_id, outcome):
        self.store.audit(actor_id=actor_id, operation="desktop.review", project_id=record["project_id"],
                         request_id=record["id"], target_id=record["id"], outcome=outcome,
                         details={"requested_by": record["requested_by"], "approved_by": record.get("approved_by"),
                                  "executed_actor": record.get("executed_actor"), "operation": record["operation"],
                                  "payload_digest": record["payload_digest"], "review_digest": record["review_digest"]})

    def snapshot(self, project_id):
        project = self.store.get("project", project_id)
        recipe = self.store.get("recipe", project["recipe_id"])
        return {"project_revision": project["revision"], "recipe_id": recipe["recipe_id"],
                "recipe_revision": recipe["revision"], "recipe_digest": recipe["digest"],
                "source_binding": copy.deepcopy(project.get("source_binding"))}

    def check_snapshot(self, project_id, snapshot):
        if self.snapshot(project_id) != snapshot:
            fail("review_context_changed", "The project, current recipe or source binding changed. Request a new review.")

    def _disclosure(self, operation, payload, project_id):
        def resource(kind, identity):
            value = self.store.get(kind, identity)
            if value.get("project_id") != project_id:
                fail("review_project_mismatch", "Referenced review material belongs to another project", 403)
            return value
        if operation in {"research.text.fetch", "research.document.import"}:
            return {"evidence": resource("evidence", payload["evidence_id"])}
        if operation == "research.watch.refresh":
            return {"watch": resource("research_watch", payload["watch_id"])}
        if operation == "research.watch.refresh_due":
            return {"configured_watches": sorted(self.store.list("research_watch", project_id), key=lambda item: item["watch_id"])}
        if operation == "hub.license.review":
            evidence = resource("hub_license_evidence", payload["evidence_id"])
            if evidence.get("evidence_digest") != payload["evidence_digest"]:
                fail("license_evidence_changed", "The actual captured license terms changed")
            return {"license_evidence": evidence}
        if operation == "hub.source.policy.replace":
            recipe = resource("recipe", payload["recipe_id"])
            old_policies = [resource("hub_license_policy", identity) for identity in payload["replace_source_policy_ids"]]
            new_policy = resource("hub_license_policy", payload["license_policy_id"])
            return {"current_recipe": recipe, "replaced_direct_source_policies": old_policies,
                    "new_source_policy": new_policy, "new_evidence": resource("hub_license_evidence", new_policy["evidence_id"]),
                    "consequence": "Only selected direct source-origin entries are replaced for future outputs. Existing recipe history, input resources, checkpoints and unresolved lineage are preserved."}
        if operation == "hub.license.revoke":
            return {"license_policy": resource("hub_license_policy", payload["policy_id"])}
        if operation in {"research.proposal.review", "research.proposal.apply"}:
            return {"proposal": resource("research_proposal", payload["proposal_id"])}
        if operation in {"hub.download.start", "hub.transfer.retry"}:
            plan_id = payload.get("plan_id")
            if operation == "hub.transfer.retry":
                plan_id = resource("hub_transfer", payload["transfer_id"])["plan_id"]
            plan = resource("hub_plan", plan_id)
            return {"download_plan": plan}
        if operation.startswith("run.") and operation in {"run.submit", "run.resume"}:
            project = self.store.get("project", project_id)
            return {"recipe": self.store.get("recipe", project["recipe_id"]), "source_binding": project.get("source_binding")}
        if operation not in {COMBINED_PUBLISH, "hub.publish.approve", "hub.publish.start"}:
            return None
        preview = self.store.get("hub_preview", payload.get("preview_id", ""))
        if preview.get("project_id") != project_id or preview.get("payload_digest") != payload.get("payload_digest"):
            fail("publication_preview_changed", "Publication preview does not match this project and payload")
        if preview.get("discarded"):
            fail("publication_preview_changed", "Publication preview was discarded")
        # Entire frozen intent, not an agent-written summary. Includes files,
        # hashes, destination, visibility, license/lineage and expected parent.
        return {"preview": copy.deepcopy(preview), "preview_record_digest": digest(preview)}

    def _validate(self, operation, payload, project_id):
        if operation == COMBINED_PUBLISH:
            if set(payload) != {"project_id", "preview_id", "payload_digest", "idempotency_key"}:
                fail("validation_error", "Reviewed publication requires one exact stored preview", 400)
            if payload.get("project_id") != project_id:
                fail("review_project_mismatch", "Publication must remain in the selected project", 403)
            for name in ["hub.publish.approve", "hub.publish.start"]:
                if name not in OPERATIONS:
                    fail("capability_unavailable", "The installed service has no publication capability", 503)
            candidate = {**payload, "confirm_publication": True}
            if list(Draft202012Validator(OPERATIONS["hub.publish.approve"].input_schema).iter_errors(candidate)):
                fail("validation_error", "Reviewed publication must identify one exact stored intent", 400)
            return frozenset({"read", "publish", "execute"})
        if operation == "research.watch.schedule_enable":
            fail("standing_review_required", "Recurring work needs separately reviewed standing authorization; this bridge only authorizes bounded exact actions", 403)
        spec = OPERATIONS.get(operation)
        if spec is None:
            fail("unknown_operation", "Operation is not available", 404)
        if not spec.mutating and set(spec.required_scopes) <= {"read"}:
            fail("review_not_required", "This read operation uses the ordinary scoped service API", 400)
        if list(Draft202012Validator(spec.input_schema).iter_errors(payload)):
            fail("validation_error", "Operation payload does not match its schema", 400)
        if operation != "project.create" and payload.get("project_id") != project_id:
            fail("review_project_mismatch", "The requested operation must remain in the selected project", 403)
        scopes = {"read", *spec.required_scopes}
        if operation == "policy.update" and payload.get("changes", {}).get("publication_enabled") is True:
            scopes.add("publish")
        return frozenset(scopes)

    def submit(self, actor, project_id, operation, payload):
        if not isinstance(project_id, str) or not isinstance(operation, str):
            fail("validation_error", "Project and operation must be exact string identities", 400)
        if actor.project_ids is not None and project_id not in actor.project_ids:
            fail("forbidden", "The requesting client cannot access this project", 403)
        if not (actor.id.startswith("desktop-assistant:") or actor.id == "desktop-owner"):
            fail("forbidden", "This interface is for the managed native application", 403)
        if not isinstance(payload, dict) or len(canonical(payload).encode()) > MAX_PAYLOAD:
            fail("review_payload_too_large", "Review payload must be a bounded JSON object", 400)
        self.service._reject_secrets(payload)
        scopes = self._validate(operation, payload, project_id)
        request_key = payload.get("idempotency_key") or digest(payload)
        with self.store.transaction():
            existing = self.store.connection.execute("SELECT body FROM desktop_reviews WHERE actor_id=? AND operation=? AND request_key=?", (actor.id, operation, request_key)).fetchone()
            if existing:
                prior = json.loads(existing["body"])
                if prior["submitted_digest"] != digest(payload) or prior["project_id"] != project_id:
                    fail("review_idempotency_conflict", "Request identity was reused for a different operation payload")
                return prior
            count = self.store.connection.execute("SELECT COUNT(*) FROM desktop_reviews WHERE actor_id=? AND status='pending' AND json_extract(body,'$.expires_at')>?", (actor.id, self.clock())).fetchone()[0]
            if count >= MAX_PENDING:
                fail("review_capacity", "Review or cancel existing requests before submitting more", 429)
            identity = str(uuid.uuid4())
            submitted_digest = digest(payload)
            payload = {**payload, "request_id": identity} if operation != COMBINED_PUBLISH else payload
            snapshot = self.snapshot(project_id)
            disclosure = self._disclosure(operation, payload, project_id)
            record = {"id": identity, "submitted_digest": submitted_digest, "requested_by": actor.id, "project_id": project_id,
                      "project_name": self.store.get("project", project_id)["model_name"], "operation": operation,
                      "payload": copy.deepcopy(payload), "payload_digest": digest(payload), "snapshot": snapshot,
                      "disclosure": disclosure, "required_scopes": sorted(scopes), "created_at": self.clock(),
                      "expires_at": self.clock() + PENDING_TTL, "action_ttl_seconds": ACTION_TTL}
            record["review_digest"] = digest(record)
            record["status"] = "pending"
            self.store.connection.execute("INSERT INTO desktop_reviews VALUES (?,?,?,?,?,?,?)", (record["id"], actor.id, project_id, operation, request_key, "pending", canonical(record)))
            self._audit(record, actor.id, "requested")
            return record

    def status(self, actor, identity):
        self.refresh_active()
        with self.store.transaction():
            record = self._get(identity)
            if actor.id != "desktop-owner" and (actor.id != record["requested_by"] or actor.project_ids is None or record["project_id"] not in actor.project_ids):
                fail("forbidden", "Review request belongs to another client or project", 403)
            if record["status"] == "pending" and self.clock() >= record["expires_at"]:
                record["status"] = "expired"
                self._save(record)
                self._audit(record, record["requested_by"], "expired")
            return record

    def pending(self, owner):
        if owner.id != "desktop-owner":
            fail("forbidden", "Native owner authorization is required", 403)
        self.refresh_active()
        with self.store.transaction():
            rows = self.store.connection.execute("SELECT id FROM desktop_reviews WHERE status IN ('pending','executing','active','revoking') ORDER BY rowid").fetchall()
        return [self.status(owner, row["id"]) for row in rows]

    def cancel(self, actor, identity):
        record = self.status(actor, identity)
        if record["status"] != "pending":
            fail("review_already_decided", "Only a pending request can be withdrawn")
        with self.store.transaction():
            current = self._get(identity)
            if current["status"] != "pending":
                fail("review_already_decided", "The review changed before cancellation")
            current["status"] = "withdrawn"
            self._save(current)
            self._audit(current, actor.id, "withdrawn")
        return current

    def decide(self, owner, identity, review_digest, decision):
        if owner.id != "desktop-owner":
            fail("forbidden", "Only the native owner can approve or revoke a request", 403)
        if decision == "revoke":
            return self.revoke(owner, identity, review_digest)
        if decision not in {"approve", "deny"}:
            fail("validation_error", "Choose an explicit native approval or denial", 400)
        with self.store.transaction():
            record = self._get(identity)
            if record["review_digest"] != review_digest:
                fail("review_changed", "The displayed review does not match this exact request")
            if record["status"] != "pending":
                fail("review_already_decided", "This request was already decided; it cannot execute twice")
            if self.clock() >= record["expires_at"]:
                fail("review_expired", "This request expired; ask for a new review", 403)
            if decision == "deny":
                record["status"], record["approved_by"] = "denied", owner.id
                self._save(record)
                self._audit(record, owner.id, "denied")
                return record
            self.check_snapshot(record["project_id"], record["snapshot"])
            if record["disclosure"] != self._disclosure(record["operation"], record["payload"], record["project_id"]):
                fail("review_material_changed", "The referenced action or publication intent changed after review")
            record.update(status="executing", approved_by=owner.id, approved_at=self.clock(), executed_actor=record["requested_by"])
            self._save(record)
            self._audit(record, owner.id, "approved")
            actor = Actor(id=record["requested_by"], scopes=frozenset(record["required_scopes"]),
                          project_ids=None if record["operation"] == "project.create" else frozenset({record["project_id"]}))
            operations = {record["operation"]: record["payload_digest"]}
            grant = Grant(actor, identity, record["project_id"], operations, self.clock() + record["action_ttl_seconds"], record["snapshot"])
            self.grants.add(grant)
        try:
            if record["operation"] == COMBINED_PUBLISH:
                result = self._publish(record, grant)
            else:
                result = self.service.invoke(record["operation"], copy.deepcopy(record["payload"]), actor)
            grant.dispatched = True
            if isinstance(result, dict) and result.get("transfer_id") and record["operation"] in {COMBINED_PUBLISH, "hub.publish.start", "hub.download.start", "hub.transfer.retry"}:
                grant.transfer_id = result["transfer_id"]
                state = "revoking" if grant.revoked else "active"
            else:
                self.grants.remove(grant)
                state = "revoked" if grant.revoked else "succeeded"
            with self.store.transaction():
                record.update(status=state, result=result, authorized_until=grant.expires)
                self._save(record)
                self._audit(record, actor.id, state)
            return record
        except ServiceError as error:
            self.grants.remove(grant)
            with self.store.transaction():
                record.update(status="failed", error=error.as_dict()["error"])
                self._save(record)
                self._audit(record, actor.id, "failed")
            return record
        except BaseException:
            self.grants.remove(grant)
            with self.store.transaction():
                record.update(status="indeterminate", notice="Execution outcome is uncertain. Inspect shared operation state; do not resubmit automatically.")
                self._save(record)
                self._audit(record, actor.id, "indeterminate")
            raise

    def _publish(self, record, grant):
        payload = record["payload"]
        approve = {"project_id": record["project_id"], "preview_id": payload["preview_id"], "payload_digest": payload["payload_digest"],
                   "confirm_publication": True, "idempotency_key": record["id"] + ":approve", "request_id": record["id"]}
        grant.operations = {"hub.publish.approve": digest(approve)}
        approval = self.service.invoke("hub.publish.approve", approve, grant.actor)
        if (approval.get("project_id") != record["project_id"] or approval.get("preview_id") != payload["preview_id"]
                or approval.get("payload_digest") != payload["payload_digest"] or approval.get("actor_id") != grant.actor.id
                or approval.get("consumed") is not False or not isinstance(approval.get("approval_id"), str)):
            fail("publication_approval_mismatch", "Generated approval did not match the exact reviewed publication", 403)
        # The generated receipt is merely a reference to this checked intent;
        # no intermediate output can change files, target, visibility or license.
        start = {"project_id": record["project_id"], "preview_id": payload["preview_id"], "payload_digest": payload["payload_digest"],
                 "approval_id": approval["approval_id"], "idempotency_key": record["id"] + ":start", "request_id": record["id"]}
        grant.operations = {"hub.publish.start": digest(start)}
        return self.service.invoke("hub.publish.start", start, grant.actor)

    def revoke(self, owner, identity, review_digest):
        if owner.id != "desktop-owner":
            fail("forbidden", "Only the native owner can revoke active authority", 403)
        with self.store.transaction():
            record = self._get(identity)
            if record["review_digest"] != review_digest:
                fail("review_changed", "The displayed review does not match this request")
            if record["status"] == "pending":
                record["status"] = "revoked"
            elif record["status"] in {"executing", "active"}:
                self.grants.revoke(identity)
                record["status"] = "revoking"
            else:
                fail("review_not_revocable", "This exact invocation already ended. Submitted native jobs require their ordinary cancellation operation.")
            record["notice"] = "Future authorized work is revoked. An in-flight external publication may require reconciliation; submitted native jobs are not undone."
            self._save(record)
            self._audit(record, owner.id, "revocation_requested")
            return record

    def refresh_active(self):
        for grant in self.grants.active():
            if grant.transfer_id is None:
                continue
            if self.clock() >= grant.expires:
                grant.revoked, grant.reason = True, grant.reason or "expired"
                with self.store.transaction():
                    record = self._get(grant.request_id)
                    if record["status"] == "active":
                        record.update(status="revoking", notice="The bounded authority expired. Await transfer cancellation or reconciliation; already transmitted data is not undone.")
                        self._save(record)
                        self._audit(record, grant.actor.id, "authority_expired")
            try:
                transfer = self.store.get("hub_transfer", grant.transfer_id)
            except ServiceError:
                continue
            if transfer.get("state") not in TERMINAL_TRANSFERS:
                continue
            with self.store.transaction():
                record = self._get(grant.request_id)
                record.update(status="revoked" if grant.revoked else "completed", transfer=transfer,
                              notice="Inspect the transfer state; a transmitted publication is never assumed to be undone.")
                self._save(record)
                self._audit(record, grant.actor.id, record["status"])
            self.grants.remove(grant)

    def has_active_transfers(self):
        self.refresh_active()
        return any(grant.transfer_id is not None for grant in self.grants.active())
