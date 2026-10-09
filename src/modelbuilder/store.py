"""SQLite persistence: revisions, immutable history, idempotency and audit."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .models import ServiceError, utcnow


class SQLiteStore:
    def __init__(self, path: str | Path = "modelbuilder.sqlite3"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS resources (
            kind TEXT NOT NULL, id TEXT NOT NULL, project_id TEXT NOT NULL,
            revision INTEGER NOT NULL, body TEXT NOT NULL,
            PRIMARY KEY(kind,id));
        CREATE INDEX IF NOT EXISTS resources_project ON resources(project_id,kind);
        CREATE TABLE IF NOT EXISTS versions (
            kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
            body TEXT NOT NULL, PRIMARY KEY(kind,id,revision));
        CREATE TABLE IF NOT EXISTS idempotency (
            actor_id TEXT NOT NULL, key TEXT NOT NULL, operation TEXT NOT NULL,
            payload_hash TEXT NOT NULL, result TEXT NOT NULL,
            PRIMARY KEY(actor_id,key));
        CREATE TABLE IF NOT EXISTS audit (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
            actor_id TEXT NOT NULL, project_id TEXT, operation TEXT NOT NULL,
            target_id TEXT, request_id TEXT NOT NULL, outcome TEXT NOT NULL,
            before_revision INTEGER, after_revision INTEGER, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS unique_slugs (slug TEXT PRIMARY KEY, project_id TEXT NOT NULL UNIQUE);
        """)

    @contextmanager
    def transaction(self):
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise

    def close(self):
        with self._lock:
            self.connection.close()

    def get(self, kind: str, resource_id: str, revision: int | None = None) -> dict:
        with self._lock:
            if revision is None:
                row = self.connection.execute("SELECT body FROM resources WHERE kind=? AND id=?",
                                              (kind, resource_id)).fetchone()
            else:
                row = self.connection.execute("SELECT body FROM versions WHERE kind=? AND id=? AND revision=?",
                                              (kind, resource_id, revision)).fetchone()
            if row is None:
                raise ServiceError("not_found", f"{kind} resource was not found", {"id": resource_id}, 404)
            return json.loads(row[0])

    def list(self, kind: str, project_id: str | None = None) -> list[dict]:
        with self._lock:
            if project_id is None:
                rows = self.connection.execute("SELECT body FROM resources WHERE kind=? ORDER BY rowid", (kind,))
            else:
                rows = self.connection.execute("SELECT body FROM resources WHERE kind=? AND project_id=? ORDER BY rowid",
                                               (kind, project_id))
            return [json.loads(row[0]) for row in rows]

    def put(self, kind: str, resource_id: str, project_id: str, body: dict, expected_revision: int | None = None):
        revision = body.get("revision", 1)
        encoded = json.dumps(body, sort_keys=True, allow_nan=False)
        with self._lock:
            if expected_revision is None:
                try:
                    self.connection.execute("INSERT INTO resources VALUES(?,?,?,?,?)",
                                            (kind, resource_id, project_id, revision, encoded))
                except sqlite3.IntegrityError as exc:
                    raise ServiceError("already_exists", "Resource identity already exists", status_code=409) from exc
            else:
                changed = self.connection.execute(
                    "UPDATE resources SET revision=?,body=? WHERE kind=? AND id=? AND revision=?",
                    (revision, encoded, kind, resource_id, expected_revision)).rowcount
                if not changed:
                    current = self.get(kind, resource_id)
                    raise ServiceError("revision_conflict", "Resource changed since it was read", {
                        "expected_revision": expected_revision, "current_revision": current["revision"],
                        "current": current}, 409)
            self.connection.execute("INSERT INTO versions VALUES(?,?,?,?)", (kind, resource_id, revision, encoded))
        return body

    def reserve_slug(self, slug: str, project_id: str):
        try:
            self.connection.execute("INSERT INTO unique_slugs VALUES(?,?)", (slug, project_id))
        except sqlite3.IntegrityError as exc:
            raise ServiceError("slug_conflict", "A project already uses this slug", status_code=409) from exc

    def replace_slug(self, old_slug: str, new_slug: str, project_id: str):
        self.connection.execute("DELETE FROM unique_slugs WHERE slug=? AND project_id=?", (old_slug, project_id))
        self.reserve_slug(new_slug, project_id)

    def idempotent_result(self, actor_id: str, key: str, operation: str, payload_hash: str) -> dict | None:
        row = self.connection.execute("SELECT operation,payload_hash,result FROM idempotency WHERE actor_id=? AND key=?",
                                      (actor_id, key)).fetchone()
        if row is None:
            return None
        if row[0] != operation or row[1] != payload_hash:
            raise ServiceError("idempotency_conflict", "Idempotency key was already used for a different request", status_code=409)
        return json.loads(row[2])

    def save_idempotent(self, actor_id, key, operation, payload_hash, result):
        self.connection.execute("INSERT INTO idempotency VALUES(?,?,?,?,?)",
                                (actor_id, key, operation, payload_hash, json.dumps(result, allow_nan=False)))

    def audit(self, *, actor_id, operation, request_id, outcome, project_id=None, target_id=None,
              before_revision=None, after_revision=None, details=None):
        with self._lock:
            self.connection.execute("""INSERT INTO audit(timestamp,actor_id,project_id,operation,target_id,request_id,
                outcome,before_revision,after_revision,details) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (utcnow(), actor_id, project_id, operation, target_id, request_id, outcome,
                 before_revision, after_revision, json.dumps(details or {}, sort_keys=True)))

    def history(self, project_id: str, limit=200) -> list[dict]:
        with self._lock:
            rows = self.connection.execute("SELECT * FROM audit WHERE project_id=? ORDER BY sequence DESC LIMIT ?",
                                           (project_id, limit)).fetchall()
            return [{**dict(row), "details": json.loads(row["details"])} for row in rows]


Store = SQLiteStore
