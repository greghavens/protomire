"""SQLite persistence: revisions, immutable history, idempotency and audit."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .models import ServiceError, utcnow


_STARTUP_TIMEOUT_SECONDS = 30.0


class SQLiteStore:
    def __init__(self, path: str | Path = "modelbuilder.sqlite3"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=0)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self._initialize()
            self.connection.execute("PRAGMA busy_timeout=30000")
        except BaseException:
            self.connection.close()
            raise

    def _initialize(self):
        # Changing journal mode may return BUSY immediately despite SQLite's
        # busy_timeout (e.g. simultaneous DELETE -> WAL opens). Retry the
        # idempotent startup only, across processes, with one monotonic deadline.
        # SQLite's busy handler stays disabled here so a final attempt cannot
        # exceed that deadline by another full connection timeout.
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        delay = 0.005
        while True:
            try:
                self.connection.execute("PRAGMA journal_mode=WAL").fetchone()
                self._initialize_schema()
                return
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", None)
                if code is None or code & 0xFF not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.1)

    def _initialize_schema(self):
        # Every statement is idempotent, including after a partial startup.
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
        CREATE TABLE IF NOT EXISTS project_recipes (
            project_id TEXT PRIMARY KEY, recipe_id TEXT NOT NULL UNIQUE, migration TEXT NOT NULL);
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
            if kind == "recipe":
                binding = self.recipe_binding(project_id)
                if binding and binding["recipe_id"] != resource_id:
                    raise ServiceError("recipe_not_current", "Historical recipe identities are read-only; open the project's current recipe to restore content",
                                       {"current_recipe_id": binding["recipe_id"]}, 409)
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

    def recipe_binding(self, project_id: str) -> dict | None:
        with self._lock:
            row = self.connection.execute("SELECT recipe_id,migration FROM project_recipes WHERE project_id=?", (project_id,)).fetchone()
            return {"recipe_id": row[0], "migration": json.loads(row[1])} if row else None

    def bind_recipe(self, project_id: str, recipe_id: str, migration: dict | None = None):
        resource = self.get("recipe", recipe_id)
        if resource["project_id"] != project_id:
            raise ServiceError("not_found", "Recipe does not belong to this project", status_code=404)
        self.connection.execute("INSERT INTO project_recipes VALUES(?,?,?)", (project_id, recipe_id, json.dumps(migration or {})))

    def update_recipe_migration(self, project_id: str, migration: dict):
        self.connection.execute("UPDATE project_recipes SET migration=? WHERE project_id=?", (json.dumps(migration), project_id))

    def resource_versions(self, kind: str, resource_id: str) -> list[dict]:
        with self._lock:
            rows = self.connection.execute("SELECT body FROM versions WHERE kind=? AND id=? ORDER BY revision", (kind, resource_id))
            return [json.loads(row[0]) for row in rows]

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
