"""Trusted step deadline journal, enforced out-of-process by the supervisor.

This is a termination deadline, not a hostile-code sandbox. A deadline aborts
all descendants of the local run, including native work that cannot catch a
Python exception. Retry backoff is handled by ZenML, outside active-node time.
"""
from __future__ import annotations
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import time
import uuid
from modelbuilder.backend.common import read_json, write_json


@contextmanager
def active_node(job: Path, node_id: str, timeout: float):
    path = Path(job) / "active-node.json"
    if node_timeout(job):
        raise TimeoutError("A prior node deadline expired; supervisor termination is pending")
    token = str(uuid.uuid4())
    write_json(path, {"node_id": node_id, "token": token,
                      "started_monotonic": time.monotonic(), "timeout_seconds": timeout})
    try:
        yield
    finally:
        # Preserve expired evidence until supervisor consumes it, even if the
        # callable returns in the narrow interval after its deadline.
        current = read_json(path)
        if current.get("token") == token and not node_timeout(job):
            path.unlink(missing_ok=True)


def node_timeout(job: Path) -> dict | None:
    current = read_json(Path(job) / "active-node.json")
    if current and time.monotonic() - current["started_monotonic"] > current["timeout_seconds"]:
        return current
    return None


def bounded_callable(function, job: Path, node: dict):
    """Wrap before applying ZenML's step decorator; preserve typed signature."""
    @wraps(function)
    def execute(*args, **kwargs):
        with active_node(job, node["id"], node["timeout"]):
            return function(*args, **kwargs)
    return execute


def deadline_start() -> None:
    """ZenML public on_start hook, resolved by stable platform module path.

    ZenML logs and swallows ordinary hook exceptions. Fail closed via process
    exit if the deadline cannot be established, rather than run without limits.
    The independent supervisor still owns descendant cleanup and reconciliation.
    """
    import os
    try:
        from zenml import get_step_context
        job = Path(os.environ["MODELBUILDER_JOB_DIR"])
        context = get_step_context()
        request = read_json(job / "request.json")
        node = next(node for node in request["recipe"]["nodes"] if node["id"] == context.step_name)
        if node_timeout(job):
            os._exit(124)
        write_json(job / "active-node.json", {"node_id": node["id"], "token": str(uuid.uuid4()),
                   "started_monotonic": time.monotonic(), "timeout_seconds": node["timeout"]})
        write_json(job / "native.json", {"backend_run_id": str(context.pipeline_run.id),
                   "model_version_ref": str(context.model_version.id) if context.model_version else None,
                   "backend_project_id": str(context.pipeline_run.project.id)})
    except Exception:
        os._exit(70)


def deadline_end() -> None:
    """ZenML public per-attempt on_end hook; expired evidence is retained."""
    import os
    from zenml import get_step_context
    job = Path(os.environ["MODELBUILDER_JOB_DIR"])
    current = read_json(job / "active-node.json")
    if current.get("node_id") == get_step_context().step_name and not node_timeout(job):
        (job / "active-node.json").unlink(missing_ok=True)
