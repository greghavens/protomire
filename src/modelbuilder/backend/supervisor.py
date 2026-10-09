"""Independent process supervisor survives application service restarts.

Cancellation is requested cooperatively, then SIGTERM/SIGKILL escalated. Only
wait()/PID identity proof permits marking the native backend run cancelled.
"""
from __future__ import annotations
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from .common import now, process_identity, process_group_state, runtime_identity, read_json, write_json


def main(job: Path, lease_fd: int | None = None) -> None:
    request = read_json(job / "request.json")
    with (job / "worker.log").open("a") as log:
        process = subprocess.Popen([sys.executable, "-m", "modelbuilder.backend.worker", str(job)],
                                   stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                                   pass_fds=(lease_fd,) if lease_fd is not None else ())
        state = {"supervisor_pid": os.getpid(), "supervisor_identity": process_identity(os.getpid()),
                 "worker_pid": process.pid, "worker_identity": process_identity(process.pid),
                 "supervisor_state": "running", "runtime_identity": runtime_identity(), "started_at": now(), "worker_reaped": False}
        write_json(job / "state.json", state)
        started = time.monotonic()
        cancelled_at = None
        term_sent = False
        while process.poll() is None or process_group_state(process.pid) is not False:
            timed_out = time.monotonic() - started > float(request.get("max_runtime_seconds", 600))
            cancellation = (job / "cancel.json").exists()
            if timed_out and not cancellation:
                write_json(job / "cancel.json", {"requested_at": now(), "reason": "max_runtime_exceeded"})
                cancellation = True
            if cancellation:
                if cancelled_at is None:
                    cancelled_at = time.monotonic()
                    state.update(supervisor_state="cancel_requested", cancel_requested_at=now(), timed_out=timed_out)
                    write_json(job / "state.json", state)
                elapsed = time.monotonic() - cancelled_at
                if elapsed > 2 and not term_sent:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    term_sent = True
                if elapsed > 5:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            time.sleep(0.1)
        code = process.wait()
        state.update(worker_reaped=True, process_group_terminated=process_group_state(process.pid) is False, worker_exit_code=code, worker_terminated_at=now(),
                     supervisor_state="reconciling", cancelled=(job / "cancel.json").exists())
        write_json(job / "state.json", state)
        # Public SDK store reconciliation follows confirmed termination, never precedes it.
        from .sdk import reconcile
        try:
            reconcile(job)
            state.update(supervisor_state="finished", reconciled_at=now())
        except Exception as error:
            state.update(supervisor_state="reconciliation_required", reconciliation_error=str(error))
        write_json(job / "state.json", state)


if __name__ == "__main__":
    main(Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else None)
