"""Durable live checkpoint evaluation around existing framework/harness callables.

The trainer explicitly calls ``checkpoint_ready`` after completing a checkpoint.
This adapter publishes a verified immutable copy, then runs the existing evaluator
in a bounded child process. It does not implement a training or inference engine.
The SQLite journal is authoritative for these asynchronous observations only;
ZenML remains authoritative for the parent run and its final artifacts.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import traceback
import uuid

import psutil

from .common import now, process_identity, read_json, write_json

SCHEMA = "modelbuilder.live-evaluation/v1"
TERMINAL = {"completed", "failed", "canceled"}
STATUSES = ("queued", "running", "completed", "failed", "canceled")
MAX_SNAPSHOT_BYTES = 4 * 1024**3


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _encode(value):
    """Tag every container so an ordinary dictionary cannot masquerade as a Path."""
    if isinstance(value, Path):
        return {"type": "path", "value": str(value)}
    if isinstance(value, dict):
        return {"type": "dict", "value": {k: _encode(v) for k, v in value.items()}}
    if isinstance(value, (list, tuple)):
        return {"type": "list", "value": [_encode(v) for v in value]}
    if value is None or isinstance(value, (str, int, float, bool)):
        _json(value)
        return {"type": "scalar", "value": value}
    raise ValueError("Live evaluator inputs must be JSON-compatible values or Path artifacts")


def _decode(value):
    if value["type"] == "path":
        return Path(value["value"])
    if value["type"] == "dict":
        return {k: _decode(v) for k, v in value["value"].items()}
    if value["type"] == "list":
        return [_decode(v) for v in value["value"]]
    if value["type"] == "scalar":
        return value["value"]
    raise ValueError("Unknown evaluator input encoding")


def _root(job: Path) -> Path:
    return Path(job) / "live_evaluation"


def _database(job: Path) -> Path:
    return _root(job) / "journal.sqlite3"


@contextmanager
def _connect(job: Path, *, create=False, write=False):
    path = _database(job)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path) if create else path.resolve().as_uri() + "?mode=" + ("rw" if write else "ro"),
                                 uri=not create, timeout=20 if write or create else 0.05)
    connection.row_factory = sqlite3.Row
    try:
        if create:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, step INTEGER NOT NULL,
                    record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evaluations (
                    evaluation_id TEXT PRIMARY KEY, checkpoint_id TEXT NOT NULL, node_id TEXT NOT NULL,
                    step INTEGER NOT NULL, status TEXT NOT NULL, record TEXT NOT NULL, request TEXT NOT NULL,
                    owner_pid INTEGER, owner_identity TEXT, worker_pid INTEGER, worker_identity TEXT);
                CREATE INDEX IF NOT EXISTS evaluations_step ON evaluations(step,evaluation_id);
                CREATE INDEX IF NOT EXISTS evaluations_status ON evaluations(status);
                CREATE TABLE IF NOT EXISTS events (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                    record TEXT NOT NULL);
            """)
        if write:
            connection.execute("BEGIN IMMEDIATE")
        yield connection
        if write:
            connection.commit()
    except BaseException:
        if write:
            connection.rollback()
        raise
    finally:
        connection.close()


def _event(connection, job: Path, kind: str, record: dict) -> None:
    native = read_json(Path(job) / "native.json")
    identity = record.get("evaluation_id", record["checkpoint_id"])
    event = {"schema": SCHEMA, "event_id": _digest([Path(job).name, identity, kind]), "kind": kind,
             "operation_id": Path(job).name, "backend_run_id": native.get("backend_run_id"),
             "model_version_ref": native.get("model_version_ref"), "created_at": now(),
             **{k: record[k] for k in ("evaluation_id", "checkpoint_id", "checkpoint_digest", "node_id", "step", "status") if k in record}}
    if kind == "evaluation.completed":
        event.update(metrics=record["metrics"], comparisons=record.get("comparisons", {}))
    if kind in {"evaluation.failed", "evaluation.canceled"}:
        event["error"] = record.get("error")
    connection.execute("INSERT OR IGNORE INTO events(event_id,record) VALUES (?,?)", (event["event_id"], _json(event)))


def list_events(job: Path, after: int = 0, limit: int = 200) -> dict:
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise ValueError("Event cursor must be a nonnegative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("Event page limit must be between 1 and 1000")
    if not _database(job).exists():
        return {"events": [], "next_cursor": after, "has_more": False}
    with _connect(job) as connection:
        rows = connection.execute("SELECT cursor,record FROM events WHERE cursor>? ORDER BY cursor LIMIT ?", (after, limit + 1)).fetchall()
    page = rows[:limit]
    return {"events": [{**json.loads(row["record"]), "cursor": row["cursor"]} for row in page],
            "next_cursor": page[-1]["cursor"] if page else after, "has_more": len(rows) > limit}


def list_evaluations(job: Path, limit: int | None = None) -> dict:
    """Read all durable history by default, or a bounded latest-evaluation window.

    A window never mutates/truncates the journal. Counts cover the entire run;
    checkpoint records in a window correspond only to its displayed evaluations.
    """
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200):
        raise ValueError("Evaluation window limit must be between 1 and 200")
    summary = dict.fromkeys(STATUSES, 0)
    if not _database(job).exists():
        result = {"schema": SCHEMA, "evaluations": [], "checkpoints": [], "summary": summary}
        if limit is not None:
            result.update(limit=limit, total_evaluations=0, truncated=False)
        return result
    with _connect(job) as connection:
        # Keep counts and the latest window in the same read snapshot while the
        # trainer/evaluator may be appending or finishing checkpoints.
        connection.execute("BEGIN")
        if limit is None:
            evaluations = [json.loads(row[0]) for row in connection.execute("SELECT record FROM evaluations ORDER BY step,evaluation_id")]
            checkpoints = [json.loads(row[0]) for row in connection.execute("SELECT record FROM checkpoints ORDER BY step,checkpoint_id")]
            for record in evaluations:
                summary[record["status"]] += 1
        else:
            evaluations = [json.loads(row[0]) for row in connection.execute(
                "SELECT record FROM evaluations ORDER BY step DESC,evaluation_id DESC LIMIT ?", (limit,))][::-1]
            for row in connection.execute("SELECT status,COUNT(*) FROM evaluations GROUP BY status"):
                summary[row[0]] = row[1]
            ids = list(dict.fromkeys(record["checkpoint_id"] for record in evaluations))
            checkpoints = [json.loads(row[0]) for row in connection.execute(
                "SELECT record FROM checkpoints WHERE checkpoint_id IN (" + ",".join("?" for _ in ids) + ") ORDER BY step,checkpoint_id", ids)] if ids else []
    # Filesystem implementation details are not part of the service contract.
    for record in checkpoints:
        record.pop("path", None)
    result = {"schema": SCHEMA, "evaluations": evaluations, "checkpoints": checkpoints, "summary": summary}
    if limit is not None:
        total = sum(summary.values())
        result.update(limit=limit, total_evaluations=total, truncated=total > len(evaluations))
    return result


def record_run_terminal(job: Path, result: dict) -> bool:
    """Persist one notification only from an authoritative reconciled SDK result.

    The caller supplies ``sdk.status(job)`` after native reconciliation. A process
    exit code, cancellation request, missing native run, or ambiguous termination
    is deliberately insufficient. This helper does not reinterpret native status.
    """
    status = result.get("status")
    if not result.get("backend_run_id") or result.get("operation_id") != Path(job).name:
        return False
    if status == "succeeded":
        verified = result.get("backend_status") in {"completed", "cached"}
    elif status == "failed":
        verified = result.get("backend_status") == "failed"
    elif status == "cancelled":
        termination = result.get("termination", {})
        verified = (result.get("backend_status") in {"failed", "cancelled"}
                    and result.get("termination_record_source") == "native_run_metadata"
                    and termination.get("reason") == "cancelled" and termination.get("confirmed") is True
                    and termination.get("process_group_terminated") is True
                    and result.get("worker_alive") is False and result.get("process_group_alive") is False)
    else:
        return False
    if not verified:
        return False
    event = {"schema": SCHEMA, "event_id": _digest([Path(job).name, result["backend_run_id"], "run.terminal"]),
             "kind": "run." + status, "operation_id": Path(job).name, "backend_run_id": result["backend_run_id"],
             "model_version_ref": result.get("model_version_ref"), "created_at": now(), "status": status,
             "backend_status": result["backend_status"]}
    with _connect(job, create=True, write=True) as connection:
        cursor = connection.execute("INSERT OR IGNORE INTO events(event_id,record) VALUES (?,?)", (event["event_id"], _json(event)))
    return cursor.rowcount == 1


def _file_hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def _safe_file(root: Path, relative: str) -> Path:
    item = Path(relative)
    if item.is_absolute() or ".." in item.parts or not item.parts:
        raise ValueError("Checkpoint asset must be a relative path inside its artifact")
    path = root / item
    if any((root / Path(*item.parts[:i])).is_symlink() for i in range(1, len(item.parts) + 1)):
        raise ValueError("Checkpoint assets cannot contain symlinks")
    if not path.resolve().is_relative_to(root.resolve()) or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Checkpoint asset is absent or not a regular file")
    return path


def _inventory(root: Path, names: list[str]) -> list[dict]:
    if len(names) != len(set(names)) or not names:
        raise ValueError("Checkpoint assets must be nonempty and unique")
    total, assets = 0, []
    for name in sorted(names):
        path = _safe_file(root, name)
        total += path.stat().st_size
        if total > MAX_SNAPSHOT_BYTES:
            raise ValueError("Live checkpoint snapshot exceeds the local 4 GiB publication limit")
        assets.append({"path": name, "sha256": _file_hash(path), "size": path.stat().st_size})
    return assets


def _snapshot(job: Path, node_id: str, source: Path, step: int | None, purpose: str, evaluator_kind: str) -> dict:
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Checkpoint-ready requires a complete checkpoint directory")
    if source.resolve().is_relative_to(_root(job).resolve()):
        raise ValueError("Cannot republish a live evaluation snapshot as mutable training output")
    if evaluator_kind == "fixture":
        if purpose not in {"last", "best"}:
            raise ValueError("Fixture live checkpoint purpose must be last or best")
        names = ["best.pt" if purpose == "best" else "checkpoint.pt"]
        descriptor = None
    else:
        from .components import read_checkpoint
        descriptor = read_checkpoint(source)
        names = [asset["path"] for asset in descriptor["assets"]] + ["checkpoint.manifest.json"]
        if step is None:
            step = descriptor["step"]
        if descriptor["step"] != step:
            raise ValueError("Checkpoint-ready step differs from committed descriptor")
        purpose = descriptor["purpose"]
    if isinstance(step, bool) or not isinstance(step, int) or step < 0:
        raise ValueError("Checkpoint-ready step must be a nonnegative integer")
    before = _inventory(source, names)
    checkpoint_digest = _digest(descriptor) if descriptor is not None else before[0]["sha256"]
    checkpoint_id = _digest([Path(job).name, node_id, step, purpose, checkpoint_digest])
    parent = _root(job) / "checkpoints"
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / checkpoint_id
    ready = {"schema": SCHEMA, "checkpoint_id": checkpoint_id, "checkpoint_digest": checkpoint_digest,
             "node_id": node_id, "step": step, "purpose": purpose, "snapshot_digest": _digest(before),
             "assets": before, "ready": True}
    if destination.exists():
        existing = read_json(destination / "ready.json")
        if existing != ready or _inventory(destination, names) != before:
            raise ValueError("Previously published immutable checkpoint changed")
        return {**ready, "path": str(destination), "created_at": now()}
    staging = parent / (".partial-" + uuid.uuid4().hex)
    staging.mkdir()
    try:
        for item in before:
            target = staging / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            with _safe_file(source, item["path"]).open("rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
        if _inventory(staging, names) != before or _inventory(source, names) != before:
            raise ValueError("Checkpoint changed during publication; call checkpoint_ready only after committing all assets")
        if descriptor is not None:
            from .components import read_checkpoint
            if read_checkpoint(staging) != descriptor:
                raise ValueError("Checkpoint descriptor changed during publication")
        write_json(staging / "ready.json", ready)
        # Rename publishes all files and the ready marker as a single operation.
        try:
            staging.rename(destination)
        except OSError:
            if not destination.exists() or read_json(destination / "ready.json") != ready:
                raise
        for path in destination.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
        return {**ready, "path": str(destination), "created_at": now()}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def verify_snapshot(record: dict) -> Path:
    root = Path(record["path"])
    ready = read_json(root / "ready.json")
    expected = {k: v for k, v in record.items() if k not in {"path", "created_at"}}
    if ready != expected:
        raise ValueError("Checkpoint ready marker identity differs from queued evaluation")
    actual = _inventory(root, [a["path"] for a in ready["assets"]])
    if actual != ready["assets"] or _digest(actual) != ready["snapshot_digest"]:
        raise ValueError("Immutable checkpoint snapshot integrity mismatch")
    return root


def _comparisons(connection, record: dict) -> dict:
    from ..evaluation import compare_records
    previous = connection.execute("SELECT record FROM evaluations WHERE node_id=? AND status='completed' AND step<? ORDER BY step DESC,evaluation_id LIMIT 1",
                                  (record["node_id"], record["step"])).fetchone()
    if not previous:
        return {}
    previous = json.loads(previous[0])
    result = {}
    for name, evidence in record.get("evaluation_records", {}).items():
        direction = evidence["metric"]["direction"]
        rows = [{"backend_run_id": item["evaluation_id"], "status": "succeeded",
                 "metrics": {**item["metrics"], "evaluation_records": item["evaluation_records"]}}
                for item in (previous, record)]
        comparison = compare_records(rows, name, direction)
        delta = record["metrics"].get(name, 0) - previous["metrics"].get(name, 0) if comparison["compatible"] else None
        result[name] = {"metric": name, "direction": direction, "compatible": comparison["compatible"],
                        "previous_evaluation_id": previous["evaluation_id"], "delta_from_previous": delta,
                        "improved": (delta < 0 if direction == "minimize" else delta > 0) if delta is not None else None,
                        "diagnostics": comparison["diagnostics"]}
    return result


def _finish(job: Path, evaluation_id: str, status: str, *, metrics=None, records=None, error=None) -> bool:
    with _connect(job, write=True) as connection:
        row = connection.execute("SELECT record FROM evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone()
        if not row:
            return False
        record = json.loads(row[0])
        if record["status"] in TERMINAL:
            return False
        record.update(status=status, finished_at=now(), error=error)
        if status == "completed":
            record.update(metrics=metrics, evaluation_records=records)
            record["comparisons"] = _comparisons(connection, record)
        connection.execute("UPDATE evaluations SET status=?,record=? WHERE evaluation_id=?", (status, _json(record), evaluation_id))
        _event(connection, job, "evaluation." + status, record)
    return True


def _process_gone(pid: int | None, identity: str | None) -> bool:
    """Positive death/reuse observation; access denial is never proof of death."""
    if not pid:
        return True
    if not identity or not identity.startswith("psutil:"):
        return False
    try:
        process = psutil.Process(pid)
        return process.status() == psutil.STATUS_ZOMBIE or "psutil:" + process.create_time().hex() != identity
    except psutil.NoSuchProcess:
        return True
    except (psutil.AccessDenied, OSError, ValueError):
        return False


def reconcile_evaluations(job: Path, reason: str | None = None) -> dict:
    """Reconcile abandoned records after positively observed owner/child death.

    ``reason`` may be supplied by the supervisor after process-group cleanup to
    cancel queued work too. Merely reconnecting a client never cancels a run.
    """
    if not _database(job).exists():
        return list_evaluations(job)
    with _connect(job) as connection:
        rows = [dict(row) for row in connection.execute("SELECT * FROM evaluations WHERE status IN ('running','queued')")]
    for row in rows:
        if row["status"] == "queued" and not reason:
            continue
        if not _process_gone(row["owner_pid"], row["owner_identity"]) or not _process_gone(row["worker_pid"], row["worker_identity"]):
            continue
        _finish(job, row["evaluation_id"], "canceled" if reason else "failed",
                error={"code": "evaluation_canceled" if reason else "evaluator_interrupted",
                       "message": reason or "Evaluator owner and child exited before recording a result"})
    return list_evaluations(job)


class LiveEvaluationCoordinator:
    """One bounded queue consumer owned by the training worker's process group."""

    def __init__(self, job: Path, node_id: str, evaluator: dict, max_concurrency: int = 1,
                 timeout_seconds: float = 300):
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or not 1 <= max_concurrency <= 4:
            raise ValueError("Live evaluation concurrency must be between 1 and 4")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 3600:
            raise ValueError("Live evaluator timeout must be in (0, 3600] seconds")
        if evaluator.get("kind") not in {"fixture", "component"}:
            raise ValueError("Live evaluation requires an existing fixture or source-pinned component evaluator")
        self.job, self.node_id = Path(job), node_id
        self.evaluator = _encode(evaluator)
        self.evaluator_digest = _digest(self.evaluator)
        self.max_concurrency, self.timeout = max_concurrency, timeout_seconds
        self.publication_lock = threading.Lock()
        self.closing, self.canceling = threading.Event(), threading.Event()
        self.wake = threading.Event()
        self.processes: dict[str, tuple[subprocess.Popen, float, object]] = {}
        self.failure = None
        with _connect(self.job, create=True):
            pass
        reconcile_evaluations(self.job)
        self.thread = threading.Thread(target=self._run, name="checkpoint-evaluation-" + node_id, daemon=True)
        self.thread.start()

    def checkpoint_ready(self, path: Path, step: int | None = None, purpose: str = "last") -> dict:
        with self.publication_lock:
            return self._publish(path, step, purpose)

    def _publish(self, path: Path, step: int | None, purpose: str) -> dict:
        if self.closing.is_set() or self.canceling.is_set():
            raise RuntimeError("Live evaluation coordinator is closing")
        if self.failure:
            raise RuntimeError("Live evaluation coordinator failed: " + str(self.failure))
        checkpoint = _snapshot(self.job, self.node_id, Path(path), step, purpose, _decode(self.evaluator)["kind"])
        evaluation_id = _digest([checkpoint["checkpoint_id"], self.evaluator_digest])
        record = {"schema": SCHEMA, "evaluation_id": evaluation_id, "checkpoint_id": checkpoint["checkpoint_id"],
                  "checkpoint_digest": checkpoint["checkpoint_digest"], "node_id": self.node_id, "step": checkpoint["step"],
                  "status": "queued", "created_at": now(), "started_at": None, "finished_at": None,
                  "metrics": {}, "evaluation_records": {}, "comparisons": {}, "error": None}
        with _connect(self.job, write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO checkpoints VALUES (?,?,?,?)",
                               (checkpoint["checkpoint_id"], self.node_id, checkpoint["step"], _json(checkpoint)))
            _event(connection, self.job, "checkpoint.ready", checkpoint)
            request = {"checkpoint": checkpoint, "evaluator": self.evaluator}
            connection.execute("INSERT OR IGNORE INTO evaluations(evaluation_id,checkpoint_id,node_id,step,status,record,request) VALUES (?,?,?,?,?,?,?)",
                               (evaluation_id, checkpoint["checkpoint_id"], self.node_id, checkpoint["step"], "queued", _json(record), _json(request)))
            row = connection.execute("SELECT record FROM evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone()
            record = json.loads(row[0])
            _event(connection, self.job, "evaluation.queued", {**record, "status": "queued"})
        self.wake.set()
        return record

    def close(self, cancel: bool = False) -> None:
        with self.publication_lock:
            if cancel:
                self.canceling.set()
            self.closing.set()
        self.wake.set()
        self.thread.join()
        if self.failure:
            raise RuntimeError("Live evaluation coordinator failed: " + str(self.failure)) from self.failure

    def _claim(self):
        with _connect(self.job, write=True) as connection:
            if connection.execute("SELECT count(*) FROM evaluations WHERE status='running'").fetchone()[0] >= self.max_concurrency:
                return None
            row = connection.execute("SELECT * FROM evaluations WHERE status='queued' AND node_id=? ORDER BY step,evaluation_id LIMIT 1", (self.node_id,)).fetchone()
            if row is None:
                return None
            record = json.loads(row["record"])
            record.update(status="running", started_at=now())
            connection.execute("UPDATE evaluations SET status='running',record=?,owner_pid=?,owner_identity=? WHERE evaluation_id=?",
                               (_json(record), os.getpid(), process_identity(os.getpid()), row["evaluation_id"]))
            _event(connection, self.job, "evaluation.running", record)
            return dict(row)

    def _launch(self, row):
        evaluation_id = row["evaluation_id"]
        output = _root(self.job) / "evaluations" / evaluation_id
        output.mkdir(parents=True, exist_ok=True)
        log = (output / "evaluator.log").open("a")
        try:
            child = subprocess.Popen([sys.executable, "-m", "modelbuilder.backend.live_evaluation", str(self.job), evaluation_id],
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=False)
        except Exception as error:
            log.close()
            _finish(self.job, evaluation_id, "failed", error={"code": "evaluator_start_failed", "message": str(error)})
            return
        self.processes[evaluation_id] = (child, time.monotonic(), log)
        with _connect(self.job, write=True) as connection:
            connection.execute("UPDATE evaluations SET worker_pid=?,worker_identity=? WHERE evaluation_id=?",
                               (child.pid, process_identity(child.pid), evaluation_id))

    @staticmethod
    def _terminate(child):
        try:
            process = psutil.Process(child.pid)
            descendants = process.children(recursive=True)
        except psutil.NoSuchProcess:
            descendants = []
        for process in reversed(descendants):
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
        if child.poll() is None:
            child.terminate()
        _, remaining = psutil.wait_procs(descendants, timeout=1)
        for process in remaining:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        psutil.wait_procs(remaining, timeout=1)

    def _cancel_pending(self):
        with _connect(self.job) as connection:
            rows = connection.execute("SELECT evaluation_id FROM evaluations WHERE node_id=? AND status='queued'", (self.node_id,)).fetchall()
        for row in rows:
            _finish(self.job, row[0], "canceled", error={"code": "evaluation_canceled", "message": "Training or live evaluation canceled"})

    def _run(self):
        try:
            while True:
                cancel = self.canceling.is_set() or (self.job / "cancel.json").exists()
                if cancel:
                    self.canceling.set()
                    self._cancel_pending()
                for evaluation_id, (child, started, log) in list(self.processes.items()):
                    timed_out = time.monotonic() - started > self.timeout
                    if cancel or timed_out:
                        self._terminate(child)
                        _finish(self.job, evaluation_id, "canceled" if cancel else "failed",
                                error={"code": "evaluation_canceled" if cancel else "evaluation_timeout",
                                       "message": "Evaluation canceled" if cancel else "Evaluator exceeded its bounded runtime"})
                    if child.poll() is not None:
                        child.wait()
                        log.close()
                        del self.processes[evaluation_id]
                        _finish(self.job, evaluation_id, "failed", error={"code": "evaluator_exit", "message": f"Evaluator exited ({child.returncode}) without a result"})
                if not cancel:
                    while len(self.processes) < self.max_concurrency and (row := self._claim()) is not None:
                        self._launch(row)
                with _connect(self.job) as connection:
                    pending = connection.execute("SELECT count(*) FROM evaluations WHERE node_id=? AND status IN ('queued','running')", (self.node_id,)).fetchone()[0]
                if (self.closing.is_set() or cancel) and not self.processes and not pending:
                    # Do not exit between an in-flight snapshot and its enqueue.
                    with self.publication_lock:
                        with _connect(self.job) as connection:
                            pending = connection.execute("SELECT count(*) FROM evaluations WHERE node_id=? AND status IN ('queued','running')", (self.node_id,)).fetchone()[0]
                        if not pending:
                            break
                self.wake.wait(0.05)
                self.wake.clear()
        except BaseException as error:
            self.failure = error
            for evaluation_id, (child, _, log) in self.processes.items():
                self._terminate(child)
                log.close()
                _finish(self.job, evaluation_id, "failed", error={"code": "coordinator_failed", "message": str(error)})
            self._cancel_pending()


def _evaluate(job: Path, evaluation_id: str) -> None:
    with _connect(job) as connection:
        row = connection.execute("SELECT request,status FROM evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone()
    if row is None or row["status"] != "running":
        return
    request = json.loads(row["request"])
    evaluator = _decode(request["evaluator"])
    checkpoint = verify_snapshot(request["checkpoint"])
    output = _root(job) / "evaluations" / evaluation_id / "output"
    output.mkdir(parents=True, exist_ok=True)
    if evaluator["kind"] == "fixture":
        from . import fixtures
        from ..evaluation import fixture_evaluation_records
        parameters = {**evaluator.get("parameters", {}), "checkpoint_kind": request["checkpoint"]["purpose"]}
        metrics = fixtures.evaluate(evaluator["data"], checkpoint, parameters, output)
        metrics["evaluation_records"] = fixture_evaluation_records(evaluator["data"], checkpoint, parameters, metrics)
    else:
        from .components.runtime import load_callable
        from .components import identity
        spec = evaluator["spec"]
        node = spec["component_node"]
        for dependency in spec.get("dependencies", []):
            path = spec.get("dependency_paths", {}).get(dependency["id"])
            if not path or _file_hash(Path(path)) != dependency["sha256"]:
                raise ValueError("Component dependency changed after admission: " + dependency["id"])
        function = load_callable(node["implementation_ref"], spec["source_root"], spec["trusted_code_roots"], spec["source"]["files"])
        inputs = {**evaluator["inputs"], evaluator["checkpoint_port"]: checkpoint}
        parameters = spec.get("parameters", {})
        identities = {"architecture": identity(parameters.get("architecture", parameters)),
                      "source": identity(spec["source"]), "dependencies": identity(spec.get("dependencies", [])),
                      "environment": identity(spec.get("environment", {})),
                      "training": identity({"component": node["component"], "parameters": parameters}),
                      "data": identity({k: v for k, v in inputs.items() if isinstance(v, dict)})}
        context = {"protocol": node["component"]["protocol"], "job_dir": str(job), "node_id": node["id"],
                   "output_dir": str(output), "source_root": function.__source_snapshot_root__, "source": spec["source"],
                   "identities": identities,
                   "dependencies": spec.get("dependency_paths", {}), "cancel_file": str(job / "cancel.json"),
                   "checkpoint_id": request["checkpoint"]["checkpoint_id"],
                   "checkpoint_digest": request["checkpoint"]["checkpoint_digest"],
                   "resume": None, "resume_mode": None}
        result = function(inputs=inputs, parameters=parameters, context=context)
        if not isinstance(result, dict) or set(result) != {port["name"] for port in node["outputs"]}:
            raise ValueError("Live evaluator must return its declared component output ports")
        candidates = [value for value in result.values() if isinstance(value, dict) and ("evaluation_records" in value or "evaluation_record" in value)]
        if len(candidates) != 1:
            raise ValueError("Live evaluator requires one metrics artifact with immutable evaluation_records")
        metrics = candidates[0]
    # Verify again after the harness returns: accidental mutation must never be
    # represented as evaluation of the originally queued checkpoint.
    verify_snapshot(request["checkpoint"])
    from ..evaluation import validate_evaluation_record
    records = metrics.get("evaluation_records")
    if not isinstance(records, dict):
        single = metrics.get("evaluation_record")
        records = {single["metric"]["name"]: single} if isinstance(single, dict) else {}
    if not records:
        raise ValueError("Evaluator returned no immutable measurement identities")
    scalars = {name: value for name, value in metrics.items()
               if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)}
    for name, record in records.items():
        errors = validate_evaluation_record(record)
        if errors or record["metric"]["name"] != name or record["value"] != scalars.get(name):
            raise ValueError("Evaluator returned invalid or mismatched measurement evidence: " + _json(errors))
        if record["treatment"]["checkpoint"] != request["checkpoint"]["checkpoint_digest"]:
            raise ValueError("Evaluation evidence is not bound to the published checkpoint digest")
    write_json(output / "result.json", {"metrics": scalars, "evaluation_records": records})
    _finish(job, evaluation_id, "completed", metrics=scalars, records=records)


def main():
    job, evaluation_id = Path(sys.argv[1]), sys.argv[2]
    try:
        _evaluate(job, evaluation_id)
    except BaseException as error:
        traceback.print_exc()
        _finish(job, evaluation_id, "failed", error={"code": "evaluation_failed", "message": str(error)})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
