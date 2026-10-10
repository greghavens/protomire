"""Opt-in metadata scheduling on the existing operation service.

SQLite claims and commits are short transactions; provider I/O is never inside
one. Leases are shared by manual refresh, every API worker and scheduler. A
crashed process may repeat an in-flight GET after expiry, but never commits its
late response or duplicates findings. This is metadata discovery, not an agent,
training registry, proposal application or scientific verification.
"""
from __future__ import annotations

from datetime import timedelta
import json
import threading

from .models import Actor, ServiceError, new_id, utcnow
from .research import ResearchWorkflow, _date

LEASE_SECONDS = 40
MAX_CONCURRENT_FETCHES = 2
POLL_SECONDS = 15
MAX_TICK_WATCHES = 2


def _expires():
    return (_date(utcnow()) + timedelta(seconds=LEASE_SECONDS)).isoformat()


def authorized(service, actor, project_id):
    if "modify" not in actor.scopes or (actor.project_ids is not None and project_id not in actor.project_ids):
        return False
    resolver = getattr(service, "research_authorizer", None)
    return resolver(actor.id, project_id) if resolver else True


def schedule_enabled(watch):
    return watch.get("schedule", {}).get("enabled") is True


class ResearchRefresh:
    """Durable request progress plus per-watch leases for synchronous HTTP/MCP."""

    def __init__(self, service):
        self.service, self.store = service, service.store
        self.workflow = ResearchWorkflow(service)

    def invoke(self, operation, payload, actor, payload_hash, request_id):
        key, project_id, token = payload["idempotency_key"], payload["project_id"], new_id()
        with self.store.transaction():
            self.service._get("project", project_id, project_id)
            if not authorized(self.service, actor, project_id):
                raise ServiceError("forbidden", "Research actor authority was revoked", status_code=403)
            previous = self.store.idempotent_result(actor.id, key, operation, payload_hash, allow_pending=True)
            if previous is not None:
                return previous
            pending = self.store.connection.execute(
                "SELECT * FROM research_requests WHERE actor_id=? AND key=?", (actor.id, key)).fetchone()
            if pending:
                if pending["expires_at"] > utcnow():
                    raise ServiceError("request_in_progress", "This research request is already running; retry the same key later", status_code=409)
                work = json.loads(pending["body"])
                self.store.connection.execute("UPDATE research_requests SET token=?,expires_at=? WHERE actor_id=? AND key=?",
                                              (token, _expires(), actor.id, key))
            else:
                if operation == "research.watch.refresh":
                    watch = self.workflow._get("watch", payload["watch_id"], project_id)
                    self.service._revision(watch, payload["expected_revision"])
                    if not watch["enabled"]:
                        raise ServiceError("watch_disabled", "Enable the watch before refreshing it", status_code=409)
                    watches = [watch]
                else:
                    watches = self.store.due_research_watches(project_id=project_id, limit=payload.get("max_watches", 1))
                work = {"watches": watches, "results": [], "cursor": 0, "skipped": 0}
                self.store.connection.execute("INSERT INTO research_requests VALUES(?,?,?,?,?,?,?)",
                    (actor.id, key, operation, payload_hash, token, _expires(), json.dumps(work)))
        try:
            while work["cursor"] < len(work["watches"]):
                watch = work["watches"][work["cursor"]]
                result = self.refresh_one(watch, actor, automatic=False, request=(actor.id, key, token))
                with self.store.transaction():
                    self._require_request(actor.id, key, token)
                    if result is None:
                        if operation == "research.watch.refresh":
                            raise ServiceError("watch_busy", "Another refresh owns this watch; retry the same request later", status_code=409)
                        work["skipped"] += 1
                        work["cursor"] += 1
                        self.store.connection.execute("UPDATE research_requests SET body=?,expires_at=? WHERE actor_id=? AND key=? AND token=?",
                            (json.dumps(work), _expires(), actor.id, key, token))
                    else:
                        # Every result (including a pre-fetch discard) committed
                        # progress atomically. Always reload that durable state.
                        row = self.store.connection.execute("SELECT body FROM research_requests WHERE actor_id=? AND key=?", (actor.id, key)).fetchone()
                        work = json.loads(row["body"])
            with self.store.transaction():
                self._require_request(actor.id, key, token)
                if operation == "research.watch.refresh":
                    if not work["results"]:
                        raise ServiceError("watch_busy", "Another refresh owns this watch; retry the same request later", status_code=409)
                    result = work["results"][0]
                else:
                    result = {"refreshes": work["results"], "refreshed": len(work["results"]),
                              "remaining_due": self.store.count_due_research_watches(project_id),
                              "skipped_busy": work["skipped"], "scheduler_running": self.service.research_scheduler_running}
                self.store.save_idempotent(actor.id, key, operation, payload_hash, result)
                self.store.connection.execute("DELETE FROM research_requests WHERE actor_id=? AND key=? AND token=?", (actor.id, key, token))
                self.store.audit(actor_id=actor.id, operation=operation, request_id=request_id, outcome="succeeded",
                                 project_id=project_id, target_id=payload.get("watch_id"),
                                 before_revision=payload.get("expected_revision"), after_revision=result.get("revision"))
                return result
        except BaseException:
            # Keep completed progress across retries, but do not keep a live
            # reservation after this caller has stopped working.
            with self.store.transaction():
                self.store.connection.execute("UPDATE research_requests SET expires_at=? WHERE actor_id=? AND key=? AND token=?",
                                              (utcnow(), actor.id, key, token))
            raise

    def _require_request(self, actor_id, key, token):
        row = self.store.connection.execute("SELECT token,expires_at FROM research_requests WHERE actor_id=? AND key=?",
                                            (actor_id, key)).fetchone()
        if row is None or row["token"] != token or row["expires_at"] <= utcnow():
            raise ServiceError("research_lease_lost", "Research request lease expired; retry the same key", status_code=409)

    def refresh_one(self, snapshot, actor, *, automatic, stop=None, request=None):
        project_id, watch_id, token = snapshot["project_id"], snapshot["watch_id"], new_id()
        with self.store.transaction():
            if request:
                self._require_request(*request)
            watch = self.workflow._get("watch", watch_id, project_id)
            # A stale scheduler candidate must not revoke a replacement
            # owner's newly approved schedule, even if the old actor vanished.
            if automatic and (watch["revision"] != snapshot["revision"] or
                              watch.get("schedule", {}).get("actor_id") != actor.id):
                return None
            project = self.service._get("project", project_id, project_id)
            if not authorized(self.service, actor, project_id) or project["status"] != "active":
                if automatic and schedule_enabled(watch):
                    self._revoke(watch)
                    return None
                raise ServiceError("forbidden", "Research authority or project access was revoked", status_code=403)
            if automatic and not schedule_enabled(watch):
                return None
            if stop and stop.is_set():
                return None
            if watch["revision"] != snapshot["revision"] or not watch["enabled"]:
                result = self._discard(watch, snapshot, actor, "watch_changed")
                if request:
                    self._persist_progress(request, result)
                return result
            if not self.store.claim_research_watch(watch_id, token, _expires(), MAX_CONCURRENT_FETCHES):
                return None
        # Provider adapter has a hard, process-enforced deadline. No database
        # transaction or store lock is held while starting, reading or stopping it.
        refresh, records = self.workflow._fetch(watch, actor)
        with self.store.transaction():
            if request:
                self._require_request(*request)
            lease = self.store.connection.execute("SELECT token,expires_at FROM research_leases WHERE watch_id=?", (watch_id,)).fetchone()
            current = self.workflow._get("watch", watch_id, project_id)
            reason = None
            if lease is None or lease["token"] != token or lease["expires_at"] <= utcnow():
                reason = "lease_lost"
            elif current["revision"] != watch["revision"] or not current["enabled"]:
                reason = "watch_changed"
            elif automatic and not schedule_enabled(current):
                reason = "schedule_disabled"
            elif not authorized(self.service, actor, project_id) or self.service._get("project", project_id, project_id)["status"] != "active":
                reason = "authority_revoked"
                if automatic and schedule_enabled(current):
                    current = self._revoke(current)
            elif stop and stop.is_set():
                reason = "shutdown"
            if reason:
                result = self._discard(current, watch, actor, reason, refresh)
            else:
                result = self.workflow._commit_refresh(current, refresh, records)
            self.store.connection.execute("DELETE FROM research_leases WHERE watch_id=? AND token=?", (watch_id, token))
            # Persist request progress with the refresh itself. A crash between
            # this commit and the outer loop cannot refetch committed work.
            if request:
                self._persist_progress(request, result)
            if automatic:
                self.store.audit(actor_id=actor.id, operation="research.watch.scheduled_refresh", request_id=refresh["refresh_id"],
                                 outcome=refresh["status"], project_id=project_id, target_id=watch_id,
                                 before_revision=watch["revision"], after_revision=result["revision"])
            return result

    def _persist_progress(self, request, result):
        actor_id, key, token = request
        row = self.store.connection.execute("SELECT body FROM research_requests WHERE actor_id=? AND key=?", (actor_id, key)).fetchone()
        work = json.loads(row["body"])
        work["results"].append(result)
        work["cursor"] += 1
        self.store.connection.execute("UPDATE research_requests SET body=?,expires_at=? WHERE actor_id=? AND key=? AND token=?",
                                      (json.dumps(work), _expires(), actor_id, key, token))

    def _discard(self, current, snapshot, actor, reason, refresh=None):
        refresh = refresh or {"refresh_id": new_id(), "project_id": current["project_id"], "watch_id": current["watch_id"],
                              "watch_revision": snapshot["revision"], "actor": actor.id, "revision": 1,
                              "started_at": utcnow(), "new_findings": [], "changed_findings": [], "unchanged": 0,
                              "claim_verified": False}
        refresh.update(status="discarded", finished_at=utcnow(), error={"code": reason,
                       "message": "Refresh result discarded because its authorization, configuration or lease changed."})
        self.store.put("research_refresh", refresh["refresh_id"], current["project_id"], refresh)
        return {"watch": current, "refresh": refresh, "watch_id": current["watch_id"],
                "project_id": current["project_id"], "revision": current["revision"]}

    def _revoke(self, watch):
        body = {**watch, "revision": watch["revision"] + 1, "updated_at": utcnow(),
                "schedule": {**watch["schedule"], "enabled": False, "disabled_at": utcnow(),
                             "reason": "authority_revoked_or_project_inactive"}}
        self.workflow._save("watch", body, watch)
        self.store.audit(actor_id="system:research-scheduler", operation="research.watch.schedule_revoked", request_id=new_id(),
                         outcome="denied", project_id=watch["project_id"], target_id=watch["watch_id"])
        return body


class ResearchScheduler:
    """One bounded loop per service worker. SQLite coordinates worker overlap."""

    def __init__(self, service, *, poll_seconds=POLL_SECONDS):
        if not 0.01 <= poll_seconds <= 300:
            raise ValueError("Scheduler poll interval is out of bounds")
        self.service, self.poll_seconds = service, poll_seconds
        self.stop_event, self.thread = threading.Event(), None
        self.last_error = None

    def tick(self):
        results = []
        if not self.service.research_authorizer:
            return results
        for watch in self.service.store.due_research_watches(limit=MAX_TICK_WATCHES, automatic=True):
            if self.stop_event.is_set():
                break
            actor = Actor(watch["schedule"]["actor_id"], {"modify"}, {watch["project_id"]})
            try:
                result = ResearchRefresh(self.service).refresh_one(watch, actor, automatic=True, stop=self.stop_event)
                if result:
                    results.append(result)
            except Exception:
                # Do not persist provider details, bearer tokens or arbitrary
                # exception text. The lease bounds retries after any failure.
                self.last_error = "research_tick_failed"
        return results

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="modelbuilder-research", daemon=True)
        self.thread.start()

    def _run(self):
        self.service.research_scheduler_running = True
        try:
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception:
                    self.last_error = "research_tick_failed"
                self.stop_event.wait(self.poll_seconds)
        finally:
            self.service.research_scheduler_running = False

    def stop(self, timeout=25):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=timeout)
        return not (self.thread and self.thread.is_alive())
