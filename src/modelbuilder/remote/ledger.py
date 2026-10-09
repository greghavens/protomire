"""Durable, exact-money admission ledger. This module never launches compute.

A hold is released only after BOTH authoritative cleanup and final billing.
Unknown outcomes, worker loss, and merely requesting cancellation retain it.
SQLite BEGIN IMMEDIATE serializes admission across processes and connections.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from modelbuilder.models import Actor, ServiceError
from modelbuilder.store import SQLiteStore

from .contracts import BudgetBalance, BudgetPolicy, Money, ProviderObservation, Quote, SpendConsent


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def require(actor: Actor, scopes: set[str], project_id: str | None = None) -> None:
    if not isinstance(actor, Actor):
        raise ServiceError("unauthenticated", "An authenticated actor is required", status_code=401)
    if not scopes <= actor.scopes:
        raise ServiceError("forbidden", "Required scopes are missing", {"required_scopes": sorted(scopes)}, 403)
    if actor.project_ids is not None and (project_id is None or project_id not in actor.project_ids):
        raise ServiceError("forbidden", "Actor cannot access this policy or project", status_code=403)


class CostLedger:
    """Dedicated sidecar database; never hold another SQLite write lock while using it."""

    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] | None = None):
        self.store = SQLiteStore(path)
        self.db = self.store.connection
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self.store.transaction():
            for statement in (
                "CREATE TABLE IF NOT EXISTS cost_policies (id TEXT PRIMARY KEY, scope_key TEXT NOT NULL UNIQUE, body TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS cost_quotes (id TEXT PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS cost_reservations (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, provider_id TEXT NOT NULL, account_ref TEXT NOT NULL, body TEXT NOT NULL)",
                "CREATE INDEX IF NOT EXISTS cost_account ON cost_reservations(provider_id,account_ref,project_id)",
                "CREATE TABLE IF NOT EXISTS cost_requests (actor_id TEXT NOT NULL, request_id TEXT NOT NULL, operation TEXT NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(actor_id,request_id))",
                "CREATE TABLE IF NOT EXISTS cost_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, target_id TEXT NOT NULL, body TEXT NOT NULL)",
            ):
                self.db.execute(statement)

    def close(self) -> None:
        self.store.close()

    def now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("Clock must return an aware timestamp")
        return now.astimezone(timezone.utc)

    def _event(self, operation: str, actor_id: str, target_id: str, request_id: str,
               project_id: str | None, details: dict) -> None:
        body = {"operation": operation, "actor_id": actor_id, "target_id": target_id,
                "request_id": request_id, "project_id": project_id,
                "timestamp": self.now().isoformat(), "details": details}
        self.db.execute("INSERT INTO cost_events(project_id,target_id,body) VALUES(?,?,?)",
                        (project_id, target_id, canonical(body)))

    def _idempotent(self, actor: Actor, request_id: str, operation: str, payload: dict,
                    action: Callable[[], dict]) -> dict:
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise ServiceError("validation_error", "A bounded idempotency request_id is required")
        hashed = digest(payload)
        row = self.db.execute("SELECT * FROM cost_requests WHERE actor_id=? AND request_id=?",
                              (actor.id, request_id)).fetchone()
        if row:
            if row["digest"] != hashed or row["operation"] != operation:
                raise ServiceError("idempotency_conflict", "Request ID was already used for a different action", status_code=409)
            return json.loads(row["result"])
        result = action()
        self.db.execute("INSERT INTO cost_requests VALUES(?,?,?,?,?)",
                        (actor.id, request_id, operation, hashed, canonical(result)))
        return result

    def put_policy(self, actor: Actor, policy: BudgetPolicy, *, expected_revision: int | None,
                   request_id: str) -> dict:
        require(actor, {"modify", "spend"}, policy.project_id)
        payload = {"policy": policy.model_dump(mode="json"), "expected_revision": expected_revision}
        with self.store.transaction():
            def action():
                row = self.db.execute("SELECT body FROM cost_policies WHERE scope_key=?", (policy.scope_key,)).fetchone()
                if row:
                    old = BudgetPolicy.model_validate_json(row["body"])
                    if policy.policy_id != old.policy_id or expected_revision != old.revision:
                        raise ServiceError("conflict", "Policy revision or immutable identity changed", status_code=409)
                    if not old.maximum_total.same_unit(policy.maximum_total):
                        raise ServiceError("currency_mismatch", "Policy currency and exponent cannot be changed")
                    if policy.revision != old.revision + 1:
                        raise ServiceError("conflict", "Policy revision must advance exactly once", status_code=409)
                elif expected_revision is not None or policy.revision != 1:
                    raise ServiceError("conflict", "New policy must start at revision one", status_code=409)
                identity = self.db.execute("SELECT scope_key FROM cost_policies WHERE id=?", (policy.policy_id,)).fetchone()
                if identity and identity["scope_key"] != policy.scope_key:
                    raise ServiceError("conflict", "Policy scope is immutable", status_code=409)
                # All policies for an account use one exact unit, including sibling projects.
                for candidate in self._policies(policy.provider_id, policy.account_ref):
                    if not candidate.maximum_total.same_unit(policy.maximum_total):
                        raise ServiceError("currency_mismatch", "Every account policy must use the same money unit")
                body = policy.model_dump(mode="json")
                self.db.execute("INSERT INTO cost_policies(id,scope_key,body) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                                (policy.policy_id, policy.scope_key, canonical(body)))
                self._event("cost.policy.put", actor.id, policy.policy_id, request_id, policy.project_id,
                            {"before_revision": expected_revision, "after_revision": policy.revision, "policy": body})
                return body
            return self._idempotent(actor, request_id, "cost.policy.put", payload, action)

    def _policies(self, provider_id: str, account_ref: str) -> list[BudgetPolicy]:
        result = [BudgetPolicy.model_validate_json(row["body"])
                  for row in self.db.execute("SELECT body FROM cost_policies")]
        return [policy for policy in result if policy.provider_id == provider_id and policy.account_ref == account_ref]

    def _record_quote(self, actor: Actor, quote: Quote, request_id: str, *, requested_inputs: dict | None = None) -> dict:
        """Called only with a server-owned adapter result, not a client-supplied quote."""
        require(actor, {"read"}, quote.project_id)
        body = quote.model_dump(mode="json")
        with self.store.transaction():
            def action():
                if not quote.created_at <= self.now() < quote.expires_at:
                    raise ServiceError("stale_quote", "Provider quote is not currently valid", status_code=409)
                existing = self.db.execute("SELECT digest FROM cost_quotes WHERE id=?", (quote.quote_id,)).fetchone()
                hashed = digest(body)
                if existing and existing["digest"] != hashed:
                    raise ServiceError("conflict", "Quote identity is immutable", status_code=409)
                self.db.execute("INSERT OR IGNORE INTO cost_quotes VALUES(?,?,?)", (quote.quote_id, canonical(body), hashed))
                self._event("cost.quote.record", actor.id, quote.quote_id, request_id, quote.project_id,
                            {"quote_digest": hashed, "evidence_kind": quote.evidence_kind})
                return {"quote": body, "quote_digest": hashed, "spending_enforced": False}
            return self._idempotent(actor, request_id, "cost.quote.record", requested_inputs or body, action)

    def get_quote(self, actor: Actor, project_id: str, quote_id: str) -> dict:
        require(actor, {"read"}, project_id)
        with self.store._lock:
            row = self.db.execute("SELECT body,digest FROM cost_quotes WHERE id=?", (quote_id,)).fetchone()
            if not row or json.loads(row["body"])["project_id"] != project_id:
                raise ServiceError("not_found", "Quote was not found", status_code=404)
            return {"quote": json.loads(row["body"]), "quote_digest": row["digest"], "spending_enforced": False}

    def _reservations(self, provider_id: str, account_ref: str, project_id: str | None = None) -> list[dict]:
        query = "SELECT body FROM cost_reservations WHERE provider_id=? AND account_ref=?"
        args = [provider_id, account_ref]
        if project_id is not None:
            query += " AND project_id=?"
            args.append(project_id)
        return [json.loads(row["body"]) for row in self.db.execute(query, args)]

    @staticmethod
    def exposure(reservation: dict) -> int:
        charged = reservation["cumulative_charge"]["minor_units"]
        if reservation["cleanup_state"] == "confirmed" and reservation["billing_state"] == "final":
            return charged
        return max(charged, reservation["reserved_total"]["minor_units"])

    def _balance(self, policy: BudgetPolicy) -> dict:
        reservations = self._reservations(policy.provider_id, policy.account_ref, policy.project_id)
        exposure = sum(self.exposure(reservation) for reservation in reservations)
        actual = sum(reservation["cumulative_charge"]["minor_units"] for reservation in reservations)
        cap = policy.maximum_total.minor_units
        return BudgetBalance(
            policy=policy, exposure_minor_units=str(exposure), actual_minor_units=str(actual),
            held_minor_units=str(exposure - actual), available_minor_units=str(max(0, cap - exposure)),
            debt_minor_units=str(max(0, exposure - cap)),
            authorization_overrun_minor_units=str(sum(max(0, r["cumulative_charge"]["minor_units"] - r["reserved_total"]["minor_units"]) for r in reservations)),
            enforcement_alert_count=sum(bool(r.get("enforcement_alerts")) for r in reservations),
        ).model_dump(mode="json")

    def balances(self, actor: Actor, project_id: str, provider_id: str, account_ref: str) -> list[dict]:
        require(actor, {"read"}, project_id)
        with self.store.transaction():
            result = []
            for policy in self._policies(provider_id, account_ref):
                if policy.scope == "project" and policy.project_id == project_id:
                    result.append(self._balance(policy))
                elif policy.scope == "account" and actor.project_ids is None:
                    result.append(self._balance(policy))
            return result

    def _reserve(self, actor: Actor, project_id: str, consent: SpendConsent, request_id: str) -> dict:
        """Accounting admission only. The dispatch gate is separately fail-closed."""
        require(actor, {"execute", "spend"}, project_id)
        payload = {"project_id": project_id, "consent": consent.model_dump(mode="json")}
        with self.store.transaction():
            def action():
                row = self.db.execute("SELECT body,digest FROM cost_quotes WHERE id=?", (consent.quote_id,)).fetchone()
                if not row:
                    raise ServiceError("not_found", "Quote was not found", status_code=404)
                quote = Quote.model_validate_json(row["body"])
                if quote.project_id != project_id:
                    raise ServiceError("not_found", "Quote was not found", status_code=404)
                if row["digest"] != consent.quote_digest:
                    raise ServiceError("quote_changed", "Consent does not bind this immutable quote", status_code=409)
                now = self.now()
                if not quote.created_at <= now < quote.expires_at:
                    raise ServiceError("stale_quote", "Quote has expired; request and approve a new quote", status_code=409)
                if not quote.all_costs_bounded:
                    raise ServiceError("unbounded_cost", "Every total-cost category must be bounded", status_code=403)
                if (consent.maximum_total != quote.maximum_total or
                        consent.max_runtime_seconds != quote.max_runtime_seconds):
                    raise ServiceError("authorization_mismatch", "Consent must match the exact total cap and runtime", status_code=403)
                policies = [policy for policy in self._policies(quote.binding.provider_id, quote.binding.account_ref)
                            if policy.scope == "account" or policy.project_id == project_id]
                if {policy.scope for policy in policies} != {"account", "project"}:
                    raise ServiceError("policy_missing", "Both account and project budgets are required", status_code=403)
                for policy in policies:
                    if not policy.enabled:
                        raise ServiceError("policy_disabled", "A matching cost policy is disabled", status_code=403)
                    if not policy.maximum_total.same_unit(quote.maximum_total):
                        raise ServiceError("currency_mismatch", "Quote and policy money units differ")
                    if quote.max_runtime_seconds > policy.max_runtime_seconds:
                        raise ServiceError("runtime_exceeded", "Quoted maximum runtime exceeds policy", status_code=403)
                    balance = self._balance(policy)
                    if (int(balance["authorization_overrun_minor_units"]) or balance["enforcement_alert_count"] or
                            int(balance["exposure_minor_units"]) + quote.maximum_total.minor_units > policy.maximum_total.minor_units):
                        # Do not leak cross-project account balance details to project-scoped actors.
                        raise ServiceError("budget_exceeded", "A total-spend cap or outstanding debt blocks admission", status_code=403)
                reservation_id = str(uuid4())
                body = {"reservation_id": reservation_id, "project_id": project_id,
                        "provider_id": quote.binding.provider_id, "account_ref": quote.binding.account_ref,
                        "binding": quote.binding.model_dump(mode="json"), "actor_id": actor.id,
                        "quote_id": quote.quote_id, "quote_digest": row["digest"],
                        "authorization_request_id": request_id, "authorized_at": now.isoformat(),
                        "policy_revisions": {policy.policy_id: policy.revision for policy in policies},
                        "reserved_total": quote.maximum_total.model_dump(mode="json"),
                        "max_runtime_seconds": quote.max_runtime_seconds,
                        "cumulative_charge": quote.maximum_total.amount(0).model_dump(mode="json"),
                        "created_at": now.isoformat(), "updated_at": now.isoformat(),
                        "hard_deadline": (now + timedelta(seconds=quote.max_runtime_seconds)).isoformat(),
                        "lease_deadline": None, "execution_state": "reserved", "cleanup_state": "not_started",
                        "billing_state": "pending", "provider_job_id": None, "backend_run_id": None,
                        "submit_key": reservation_id, "observation_sequence": 0, "observation_digest": None,
                        "evidence_kind": quote.evidence_kind, "spending_enforced": False,
                        "last_reconciled_at": None, "stop_reason": None, "enforcement_alerts": []}
                self.db.execute("INSERT INTO cost_reservations VALUES(?,?,?,?,?)",
                                (reservation_id, project_id, quote.binding.provider_id, quote.binding.account_ref, canonical(body)))
                self._event("cost.reserve", actor.id, reservation_id, request_id, project_id,
                            {"consent": payload["consent"], "policy_revisions": body["policy_revisions"],
                             "reserved_total": body["reserved_total"], "evidence_kind": body["evidence_kind"]})
                return body
            return self._idempotent(actor, request_id, "cost.reserve", payload, action)

    def _load(self, reservation_id: str) -> dict:
        row = self.db.execute("SELECT body FROM cost_reservations WHERE id=?", (reservation_id,)).fetchone()
        if not row:
            raise ServiceError("not_found", "Reservation was not found", status_code=404)
        return json.loads(row["body"])

    def _save(self, body: dict) -> dict:
        body["updated_at"] = self.now().isoformat()
        self.db.execute("UPDATE cost_reservations SET body=? WHERE id=?", (canonical(body), body["reservation_id"]))
        return body

    def get_reservation(self, actor: Actor, project_id: str, reservation_id: str) -> dict:
        require(actor, {"read"}, project_id)
        with self.store._lock:
            body = self._load(reservation_id)
            if body["project_id"] != project_id:
                raise ServiceError("not_found", "Reservation was not found", status_code=404)
            return body

    def list_reservations(self, actor: Actor, project_id: str) -> list[dict]:
        require(actor, {"read"}, project_id)
        with self.store._lock:
            return [json.loads(row["body"]) for row in self.db.execute(
                "SELECT body FROM cost_reservations WHERE project_id=? ORDER BY rowid", (project_id,))]

    def cancel(self, actor: Actor, project_id: str, reservation_id: str, request_id: str) -> dict:
        require(actor, {"execute"}, project_id)
        with self.store.transaction():
            def action():
                body = self._load(reservation_id)
                if body["project_id"] != project_id:
                    raise ServiceError("not_found", "Reservation was not found", status_code=404)
                if body["execution_state"] == "reserved":
                    # No provider call can have occurred before claim_submission commits.
                    body.update(execution_state="stopped", cleanup_state="confirmed", billing_state="final",
                                stop_reason="cancelled_before_submission")
                elif body["cleanup_state"] != "confirmed":
                    body.update(execution_state="stop_requested", cleanup_state="required", stop_reason="user_cancelled")
                self._event("cost.cancel", actor.id, reservation_id, request_id, project_id,
                            {"execution_state": body["execution_state"], "cleanup_state": body["cleanup_state"]})
                return self._save(body)
            return self._idempotent(actor, request_id, "cost.cancel", {"project_id": project_id, "reservation_id": reservation_id}, action)

    def _claim_submission(self, reservation_id: str, lease_seconds: int = 60) -> dict:
        with self.store.transaction():
            body = self._load(reservation_id)
            if body["execution_state"] != "reserved":
                raise ServiceError("submission_already_claimed", "Submission is already claimed or cancelled", status_code=409)
            now = self.now()
            quote = Quote.model_validate_json(self.db.execute("SELECT body FROM cost_quotes WHERE id=?", (body["quote_id"],)).fetchone()["body"])
            if now >= quote.expires_at:
                raise ServiceError("stale_quote", "Quote expired before submission; cancel and approve a new quote", status_code=409)
            for policy in self._policies(body["provider_id"], body["account_ref"]):
                if policy.scope == "account" or policy.project_id == body["project_id"]:
                    balance = self._balance(policy)
                    if (not policy.enabled or int(balance["debt_minor_units"]) or int(balance["authorization_overrun_minor_units"])
                            or balance["enforcement_alert_count"] or body["max_runtime_seconds"] > policy.max_runtime_seconds):
                        raise ServiceError("policy_disabled", "Current policy no longer permits starting this reservation", status_code=403)
            if now >= datetime.fromisoformat(body["hard_deadline"]):
                raise ServiceError("runtime_exceeded", "Reservation runtime has elapsed", status_code=403)
            body.update(execution_state="submitting", cleanup_state="required",
                        lease_deadline=(now + timedelta(seconds=lease_seconds)).isoformat())
            self._event("cost.submission.claim", "system:cost-controller", reservation_id, body["submit_key"],
                        body["project_id"], {"submit_key": body["submit_key"], "hard_deadline": body["hard_deadline"]})
            return self._save(body)

    def _record_submission(self, reservation_id: str, provider_job_id: str, backend_run_id: str) -> dict:
        if not provider_job_id or not backend_run_id:
            raise ServiceError("invalid_provider_response", "Provider and ZenML run identities are required")
        with self.store.transaction():
            body = self._load(reservation_id)
            if body["execution_state"] not in {"submitting", "unknown", "stop_requested", "running"}:
                raise ServiceError("invalid_transition", "Reservation cannot accept a submitted job", status_code=409)
            if body["provider_job_id"] and (body["provider_job_id"] != provider_job_id or body["backend_run_id"] != backend_run_id):
                raise ServiceError("provider_identity_mismatch", "Provider submission identity changed", status_code=409)
            body.update(provider_job_id=provider_job_id, backend_run_id=backend_run_id)
            if body["execution_state"] != "stop_requested":
                body["execution_state"] = "running"
            self._event("cost.submission.record", "system:cost-controller", reservation_id, body["submit_key"],
                        body["project_id"], {"provider_job_id": provider_job_id, "backend_run_id": backend_run_id})
            return self._save(body)

    def _submission_unknown(self, reservation_id: str) -> dict:
        with self.store.transaction():
            body = self._load(reservation_id)
            if body["execution_state"] != "stop_requested":
                body["execution_state"] = "unknown"
            body["cleanup_state"] = "unknown"
            self._event("cost.submission.unknown", "system:cost-controller", reservation_id, body["submit_key"],
                        body["project_id"], {"hold_retained": True})
            return self._save(body)

    def _heartbeat(self, reservation_id: str, lease_seconds: int = 60) -> dict:
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("Worker lease must be bounded to 1..300 seconds")
        with self.store.transaction():
            body = self._load(reservation_id)
            if body["execution_state"] not in {"submitting", "running"}:
                raise ServiceError("invalid_transition", "Only an active worker can renew its lease", status_code=409)
            now = self.now()
            old_deadline = datetime.fromisoformat(body["lease_deadline"])
            if now >= min(old_deadline, datetime.fromisoformat(body["hard_deadline"])):
                raise ServiceError("lease_expired", "An expired lease cannot revive a worker", status_code=409)
            body["lease_deadline"] = (now + timedelta(seconds=lease_seconds)).isoformat()
            return self._save(body)

    def recover_due(self) -> list[dict]:
        """Persist stop intents, never infer provider termination from worker death."""
        with self.store.transaction():
            due = []
            for row in self.db.execute("SELECT body FROM cost_reservations").fetchall():
                body = json.loads(row["body"])
                now = self.now()
                if body["execution_state"] == "reserved" and now >= datetime.fromisoformat(body["hard_deadline"]):
                    body.update(execution_state="stopped", cleanup_state="confirmed", billing_state="final",
                                stop_reason="expired_before_submission")
                    self._event("cost.reservation.expire", "system:cost-controller", body["reservation_id"],
                                body["submit_key"], body["project_id"], {"never_submitted": True})
                    self._save(body)
                    continue
                if body["execution_state"] in {"stopped", "succeeded", "failed"} and body["cleanup_state"] != "confirmed":
                    due.append(body)
                    continue
                active = body["execution_state"] in {"submitting", "running", "unknown", "stop_requested"}
                if not active:
                    continue
                runtime_due = now >= datetime.fromisoformat(body["hard_deadline"])
                lease_due = body["lease_deadline"] is not None and now >= datetime.fromisoformat(body["lease_deadline"])
                if runtime_due or lease_due or body["execution_state"] == "stop_requested":
                    if body["execution_state"] != "stop_requested":
                        body.update(execution_state="stop_requested", cleanup_state="required",
                                    stop_reason="maximum_runtime" if runtime_due else "worker_lease_lost")
                        self._event("cost.stop.required", "system:cost-controller", body["reservation_id"],
                                    body["submit_key"], body["project_id"], {"reason": body["stop_reason"], "hold_retained": True})
                        self._save(body)
                    due.append(body)
            return due

    def _apply_observation(self, observation: ProviderObservation) -> dict:
        """Internal server-owned adapter boundary, intentionally absent from public operations."""
        observation = ProviderObservation.model_validate(observation.model_dump(mode="json"))
        with self.store.transaction():
            body = self._load(observation.reservation_id)
            if (observation.provider_id != body["provider_id"] or observation.account_ref != body["account_ref"]
                    or observation.provider_job_id != body["provider_job_id"]):
                raise ServiceError("provider_identity_mismatch", "Billing evidence does not match this reservation", status_code=409)
            is_simulation = body["evidence_kind"] == "simulation"
            if is_simulation != (observation.evidence_kind == "simulation"):
                raise ServiceError("evidence_mismatch", "Synthetic and real provider evidence cannot be mixed", status_code=403)
            amount = Money.model_validate(body["reserved_total"])
            if not amount.same_unit(observation.cumulative_charge):
                raise ServiceError("currency_mismatch", "Billing evidence money unit differs")
            hashed = digest(observation.model_dump(mode="json"))
            if observation.sequence < body["observation_sequence"]:
                raise ServiceError("stale_observation", "Out-of-order billing evidence was rejected", status_code=409)
            if observation.sequence == body["observation_sequence"]:
                if hashed != body["observation_digest"]:
                    raise ServiceError("provider_event_conflict", "Provider sequence was reused with different content", status_code=409)
                return body
            if observation.observed_at > self.now():
                raise ServiceError("invalid_provider_response", "Provider observation is future-dated")
            if body["last_reconciled_at"] and observation.observed_at < datetime.fromisoformat(body["last_reconciled_at"]):
                raise ServiceError("stale_observation", "Provider observation timestamp moved backwards", status_code=409)
            if observation.cumulative_charge.minor_units < body["cumulative_charge"]["minor_units"]:
                raise ServiceError("charge_regression", "Credits require a separate reviewed adjustment; cumulative charges cannot decrease", status_code=409)
            alerts = set(body.get("enforcement_alerts", []))
            if body["cleanup_state"] == "confirmed" and observation.cleanup_state != "confirmed":
                alerts.add("cleanup_reopened")
            if body["billing_state"] == "final" and observation.billing_state != "final":
                alerts.add("billing_reopened")
            if body["execution_state"] in {"stopped", "succeeded", "failed"} and observation.execution_state == "running":
                alerts.add("execution_reopened")
            body["enforcement_alerts"] = sorted(alerts)
            execution = observation.execution_state
            if body["execution_state"] == "stop_requested" and execution in {"running", "unknown"}:
                execution = "stop_requested"
            body.update(execution_state=execution, cleanup_state=observation.cleanup_state,
                        billing_state=observation.billing_state,
                        cumulative_charge=observation.cumulative_charge.model_dump(mode="json"),
                        observation_sequence=observation.sequence, observation_digest=hashed,
                        last_reconciled_at=observation.observed_at.isoformat())
            if observation.cumulative_charge.minor_units >= amount.minor_units and execution in {"running", "unknown", "stop_requested"}:
                body.update(execution_state="stop_requested", cleanup_state="required", stop_reason="reservation_total_reached")
                self._event("cost.stop.required", "system:cost-controller", body["reservation_id"],
                            f"observation:{observation.sequence}", body["project_id"],
                            {"reason": body["stop_reason"], "hold_retained": True})
            self._save(body)
            self._event("cost.reconcile", "system:provider-adapter", body["reservation_id"],
                        f"observation:{observation.sequence}", body["project_id"],
                        {"observation": observation.model_dump(mode="json"), "exposure_minor_units": self.exposure(body),
                         "late_charge_above_reservation": observation.cumulative_charge.minor_units > amount.minor_units})
            return body

    def _reconciliation_candidates(self) -> list[dict]:
        """Include settled jobs: final invoices can still be followed by late adjustments."""
        with self.store._lock:
            return [body for row in self.db.execute("SELECT body FROM cost_reservations")
                    if (body := json.loads(row["body"]))["provider_job_id"] is not None]

    def history(self, actor: Actor, project_id: str, *, after_sequence: int = 0, limit: int = 100) -> list[dict]:
        require(actor, {"read"}, project_id)
        if type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ServiceError("validation_error", "History pagination is invalid")
        with self.store._lock:
            return [{"sequence": row["sequence"], **json.loads(row["body"])} for row in self.db.execute(
                "SELECT sequence,body FROM cost_events WHERE project_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (project_id, after_sequence, limit))]
