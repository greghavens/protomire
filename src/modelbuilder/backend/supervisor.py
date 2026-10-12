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
from .adapter import _durable_launch_record
from .execution_limits import node_timeout
from .common import now, process_identity, process_group_state, runtime_identity, read_json, write_json


def main(job: Path, lease_fd: int | None = None) -> None:
    # Acknowledge before optional imports or worker creation. The inherited lease
    # spans this window; the durable attempt remains authoritative if we die here.
    attempt = read_json(job / "spawn_attempt.json")
    acknowledged = {"attempt_id": attempt.get("attempt_id"), "supervisor_pid": os.getpid(),
                    "supervisor_identity": process_identity(os.getpid()), "runtime_identity": runtime_identity(),
                    "acknowledged_at": now()}
    _durable_launch_record(job / "supervisor_ack.json", acknowledged)
    request = read_json(job / "request.json")
    write_json(job / "state.json", {**acknowledged, "supervisor_state": "starting",
                                   "submitted_at": request.get("submitted_at")})
    from .tracking_bridge import training_environment, LiveTracking
    live_tracking = LiveTracking(job)
    with (job / "worker.log").open("a") as log:
        process = subprocess.Popen([sys.executable, "-m", "modelbuilder.backend.worker", str(job)],
                                   stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                                   env=training_environment(),
                                   pass_fds=(lease_fd,) if lease_fd is not None else ())
        state = {"supervisor_pid": os.getpid(), "supervisor_identity": process_identity(os.getpid()),
                 "worker_pid": process.pid, "worker_identity": process_identity(process.pid),
                 "supervisor_state": "running", "runtime_identity": runtime_identity(), "started_at": now(), "worker_reaped": False}
        write_json(job / "state.json", state)
        started = time.monotonic()
        cancelled_at = None
        term_sent = False
        while process.poll() is None or process_group_state(process.pid) is not False:
            expired_node = node_timeout(job)
            timed_out = bool(expired_node) or time.monotonic() - started > float(request.get("max_runtime_seconds", 600))
            cancellation = (job / "cancel.json").exists()
            if timed_out and not cancellation:
                write_json(job / "cancel.json", {"requested_at": now(), "reason": "node_timeout_exceeded" if expired_node else "max_runtime_exceeded",
                                                 **({key: expired_node[key] for key in ("node_id", "canonical_node_id", "invocation_id", "iteration_block_id", "iteration", "max_iterations") if key in expired_node} if expired_node else {})})
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
            live_tracking.poll()
            time.sleep(0.1)
        code = process.wait()
        state.update(worker_reaped=True, process_group_terminated=process_group_state(process.pid) is False, worker_exit_code=code, worker_terminated_at=now(),
                     supervisor_state="reconciling", cancelled=(job / "cancel.json").exists())
        if state["process_group_terminated"] is True:
            state["termination"] = {"confirmed": True,
                                    "reason": "cancelled" if cancelled_at is not None else ("completed" if code == 0 else "worker_failure"),
                                    "worker_pid": process.pid, "worker_identity": state["worker_identity"],
                                    "runtime_identity": state["runtime_identity"], "process_group_terminated": True,
                                    "confirmed_at": state["worker_terminated_at"],
                                    "proof_source": "local_supervisor_wait_and_group_observation"}
        write_json(job / "state.json", state)
        if state["process_group_terminated"] is True:
            from .live_evaluation import reconcile_evaluations
            reconcile_evaluations(job, reason="Training canceled" if state["cancelled"] else
                                  "Training ended before pending evaluation completed")
        # Public SDK store reconciliation follows confirmed termination, never precedes it.
        from .sdk import reconcile, status
        try:
            native_run = reconcile(job)
            state.update(supervisor_state="finished", reconciled_at=now())
            write_json(job / "state.json", state)
            from .live_evaluation import record_run_terminal
            record_run_terminal(job, status(job))
            live_tracking.finish(native_run)
        except Exception as error:
            state.update(supervisor_state="reconciliation_required", reconciliation_error=str(error))
        write_json(job / "state.json", state)


if __name__ == "__main__":
    main(Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else None)
