"""Durable native-submission identity, distinct from the financial ledger.

Claim before the first side effect. An ambiguous claim is never reset or
automatically resubmitted, including after process death. This is controller
at-most-once behavior, not a claim of provider-side idempotency.
"""
from __future__ import annotations

import json
from pathlib import Path

from modelbuilder.models import ServiceError
from modelbuilder.store import SQLiteStore

from .ledger import canonical
from .modal_contract import NativeModalPlan
from .provider import Submission


class ModalJournal:
    def __init__(self, path: str | Path):
        self.store = SQLiteStore(path)
        with self.store.transaction():
            self.store.connection.execute("""CREATE TABLE IF NOT EXISTS modal_submissions (
                submit_key TEXT PRIMARY KEY, native_run_id TEXT NOT NULL UNIQUE,
                plan_digest TEXT NOT NULL, plan TEXT NOT NULL, state TEXT NOT NULL,
                submission TEXT, observation_sequence INTEGER NOT NULL DEFAULT 0)""")
            self.store.connection.execute("""CREATE TABLE IF NOT EXISTS modal_submission_approvals (
                submit_key TEXT PRIMARY KEY REFERENCES modal_submissions(submit_key), body TEXT NOT NULL)""")

    def close(self):
        self.store.close()

    def bind(self, plan: NativeModalPlan) -> None:
        with self.store.transaction():
            db = self.store.connection
            rows = db.execute("SELECT * FROM modal_submissions WHERE submit_key=? OR native_run_id=?",
                              (plan.submit_key, str(plan.backend_run_id))).fetchall()
            if rows:
                if len(rows) != 1 or rows[0]["plan_digest"] != plan.plan_digest:
                    raise ServiceError("idempotency_conflict", "Native run, submit key and immutable plan are already bound", status_code=409)
                return
            db.execute("INSERT INTO modal_submissions VALUES(?,?,?,?,?,NULL,0)",
                       (plan.submit_key, str(plan.backend_run_id), plan.plan_digest,
                        canonical(plan.model_dump(mode="json")), "prepared"))

    def get(self, submit_key: str) -> dict:
        with self.store._lock:
            row = self.store.connection.execute("SELECT * FROM modal_submissions WHERE submit_key=?", (submit_key,)).fetchone()
            if row is None:
                raise ServiceError("not_found", "No native submission is bound to this key", status_code=404)
            result = dict(row)
            result["plan"] = json.loads(result["plan"])
            result["submission"] = json.loads(result["submission"]) if result["submission"] else None
            return result

    def claim_once(self, plan: NativeModalPlan, approval=None) -> bool:
        self.bind(plan)
        with self.store.transaction():
            if approval is not None:
                if approval.plan_digest != plan.plan_digest:
                    raise ServiceError("authorization_mismatch", "Approval does not bind this plan", status_code=403)
                body = canonical(approval.model_dump(mode="json"))
                old = self.store.connection.execute("SELECT body FROM modal_submission_approvals WHERE submit_key=?",
                                                    (plan.submit_key,)).fetchone()
                if old and old["body"] != body:
                    raise ServiceError("authorization_mismatch", "The durable launch approval is immutable", status_code=409)
                self.store.connection.execute("INSERT OR IGNORE INTO modal_submission_approvals VALUES(?,?)", (plan.submit_key, body))
            result = self.store.connection.execute(
                "UPDATE modal_submissions SET state='submission_unknown' WHERE submit_key=? AND state='prepared'",
                (plan.submit_key,))
            return result.rowcount == 1

    def record(self, plan: NativeModalPlan, submission: Submission) -> None:
        if submission.backend_run_id != str(plan.backend_run_id):
            raise ServiceError("provider_identity_mismatch", "Native run identity changed", status_code=403)
        self.bind(plan)
        with self.store.transaction():
            existing = self.get(plan.submit_key)["submission"]
            body = submission.model_dump(mode="json")
            if existing is not None and existing != body:
                raise ServiceError("provider_identity_mismatch", "Provider submission identity changed", status_code=409)
            self.store.connection.execute("UPDATE modal_submissions SET state='identified',submission=? WHERE submit_key=?",
                                          (canonical(body), plan.submit_key))

    def next_sequence(self, submit_key: str) -> int:
        with self.store.transaction():
            self.get(submit_key)
            self.store.connection.execute("UPDATE modal_submissions SET observation_sequence=observation_sequence+1 WHERE submit_key=?",
                                          (submit_key,))
            return self.get(submit_key)["observation_sequence"]
