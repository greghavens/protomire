"""Trusted step deadline journals, enforced out-of-process by the supervisor.

Each invocation attempt owns an immutable, uniquely named record. This is a
termination deadline, not a hostile-code sandbox: expiry aborts all descendants
of the local run, including native work that cannot catch a Python exception.
Retry backoff is handled by ZenML, outside active-node time.
"""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
import os
import threading
import time
import uuid
from modelbuilder.backend.common import read_json, write_json


# Hooks run in the public StepContext's execution context. Keep the exact
# context object as well as the token: a late/duplicate end hook must not clear
# a newer retry with the same invocation name. Never mutate ZenML internals.
_hook_attempts: ContextVar[tuple] = ContextVar("modelbuilder_deadline_attempts", default=())


def _attempt_path(job: Path, token: str) -> Path:
    return Path(job) / "active-nodes" / f"{token}.json"


def _expired(record: dict, now: float) -> bool:
    return now - record["started_monotonic"] > record["timeout_seconds"]


def _begin_attempt(job: Path, identity: dict, timeout: float, **owner) -> dict:
    if node_timeout(job):
        raise TimeoutError("A prior node deadline expired; supervisor termination is pending")
    record = {"node_id": identity["canonical_node_id"], **identity,
              "token": str(uuid.uuid4()), "owner_pid": os.getpid(),
              "owner_thread_id": threading.get_ident(), **owner,
              "started_monotonic": time.monotonic(), "timeout_seconds": timeout}
    # Unique destinations prevent one attempt from replacing another's record.
    write_json(_attempt_path(job, record["token"]), record)
    return record


def _finish_attempt(job: Path, record: dict) -> None:
    path = _attempt_path(job, record["token"])
    current = read_json(path)
    # Test ownership and this attempt's own deadline, independently of siblings.
    # Preserve expired evidence even when the callable returns before the next
    # supervisor poll. Attempt paths are never reused or rewritten by us.
    if current == record and not _expired(current, time.monotonic()):
        path.unlink(missing_ok=True)


@contextmanager
def active_node(job: Path, node_id: str, timeout: float):
    record = _begin_attempt(job, {"canonical_node_id": node_id, "invocation_id": node_id}, timeout)
    try:
        yield
    finally:
        _finish_attempt(job, record)


def node_timeout(job: Path) -> dict | None:
    """Return the earliest expired attempt, retaining the supervisor's API.

    Read legacy singleton records as well for existing jobs. New attempts never
    write that shared path. Atomic per-attempt writes and disappearance-tolerant
    reads let the supervisor poll while independent steps start and finish.
    """
    paths = [Path(job) / "active-node.json", *sorted((Path(job) / "active-nodes").glob("*.json"))]
    current = [record for path in paths if (record := read_json(path))]
    now = time.monotonic()
    expired = [record for record in current if _expired(record, now)]
    return min(expired, key=lambda record: (
        record["started_monotonic"] + record["timeout_seconds"],
        record.get("invocation_id", record["node_id"]),
        record.get("step_run_id", ""), record.get("token", ""),
    ), default=None)


def bounded_callable(function, job: Path, node: dict):
    """Wrap before applying ZenML's step decorator; preserve typed signature."""
    @wraps(function)
    def execute(*args, **kwargs):
        with active_node(job, node["id"], node["timeout"]):
            return function(*args, **kwargs)
    return execute


def _native_start(job: Path, context) -> None:
    from modelbuilder.backend.native_invocations import invocation
    request = read_json(job / "request.json")
    identity = invocation(context.step_name, context.step_run, request["recipe"])
    node = next(node for node in request["recipe"]["nodes"] if node["id"] == identity["canonical_node_id"])
    attempts = _hook_attempts.get()
    if any(saved_context is context and saved_job == job for saved_context, saved_job, _ in attempts):
        raise RuntimeError("The native attempt already owns a deadline")
    owner = {}
    if getattr(context.step_run, "id", None) is not None:
        owner["step_run_id"] = str(context.step_run.id)
    if getattr(context.step_run, "version", None) is not None:
        owner["step_run_version"] = context.step_run.version
    record = _begin_attempt(job, identity, node["timeout"], **owner)
    _hook_attempts.set((*attempts, (context, job, record)))
    # common.write_json has per-call temporary names even for this shared path.
    write_json(job / "native.json", {"backend_run_id": str(context.pipeline_run.id),
               "model_version_ref": str(context.model_version.id) if context.model_version else None,
               "backend_project_id": str(context.pipeline_run.project.id)})


def _native_end(job: Path, context) -> None:
    attempts = _hook_attempts.get()
    owned = [entry for entry in attempts if entry[0] is context and entry[1] == job]
    for _, _, record in owned:
        _finish_attempt(job, record)
    _hook_attempts.set(tuple(entry for entry in attempts if not (entry[0] is context and entry[1] == job)))


def _hook_failed() -> None:
    # ZenML swallows ordinary hook exceptions; preserve the original error
    # before fail-closed process exit (os._exit cannot flush Python logging).
    import traceback
    try:
        os.write(2, traceback.format_exc().encode("utf-8", "replace"))
    finally:
        os._exit(70)


def deadline_start() -> None:
    """ZenML public per-attempt on_start hook; failures are fail-closed."""
    # Absolute imports also work when ZenML loads a hook as a source module.
    from modelbuilder.backend import execution_limits as limits
    try:
        from zenml import get_step_context
        limits._native_start(Path(os.environ["MODELBUILDER_JOB_DIR"]), get_step_context())
    except TimeoutError:
        os._exit(124)
    except Exception:
        limits._hook_failed()


def deadline_end() -> None:
    """ZenML public per-attempt on_end hook; expired evidence is retained."""
    from modelbuilder.backend import execution_limits as limits
    try:
        from zenml import get_step_context
        limits._native_end(Path(os.environ["MODELBUILDER_JOB_DIR"]), get_step_context())
    except Exception:
        limits._hook_failed()
