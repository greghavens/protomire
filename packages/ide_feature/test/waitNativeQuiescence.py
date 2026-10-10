"""Test-only cleanup fence; never remove state while native owners may write it."""
from __future__ import annotations

import fcntl
import json
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import psutil


def gone(pid, identity):
    if not isinstance(pid, int) or not isinstance(identity, str) or not identity.startswith("psutil:"):
        return False
    try:
        process = psutil.Process(pid)
        return process.status() == psutil.STATUS_ZOMBIE or "psutil:" + process.create_time().hex() != identity
    except psutil.NoSuchProcess:
        return True
    except (psutil.AccessDenied, OSError):
        return False


def verify(backend, expected_job=None):
    if expected_job is not None and not (backend / "jobs" / expected_job).is_dir():
        raise RuntimeError("Submitted job ownership evidence is missing")
    with ExitStack() as locks:
        for job in sorted((backend / "jobs").glob("*")):
            if not job.is_dir():
                continue
            # No creation: missing ownership evidence must remain unknown.
            lease = locks.enter_context((job / "launch.lock").open("r"))
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not (job / "spawn_attempt.json").exists():
                continue
            state = json.loads((job / "state.json").read_text())
            if state.get("worker_reaped") is not True or state.get("process_group_terminated") is not True:
                raise RuntimeError("Worker/group termination is not confirmed")
            for role in ("supervisor", "worker"):
                if not gone(state.get(role + "_pid"), state.get(role + "_identity")):
                    raise RuntimeError(f"{role} remains live or identity is unknown")


def main():
    backend, timeout = Path(sys.argv[1]), float(sys.argv[2])
    if not 0 < timeout <= 120:
        raise ValueError("Cleanup deadline must be positive and at most 120 seconds")
    expected_job = sys.argv[3] if len(sys.argv) > 3 else None
    if expected_job is not None and (Path(expected_job).name != expected_job or expected_job in {".", ".."}):
        raise ValueError("Expected job must be a directory name")
    deadline = time.monotonic() + timeout
    while True:
        try:
            verify(backend, expected_job)
            print("Native owners quiescent; inherited leases released and termination verified")
            return 0
        except (OSError, ValueError, RuntimeError) as error:
            if time.monotonic() >= deadline:
                print(f"Cleanup refused; retain evidence at {backend}: {error}", file=sys.stderr)
                return 1
            time.sleep(.05)


if __name__ == "__main__":
    raise SystemExit(main())
