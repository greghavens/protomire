"""Opt-in Linux native-test queue: four full groups, at most two pytest roots.

This stays on the existing CPU runner. No xdist, new packages, training worker
changes, or additional CI machines. The serial aggregate remains
the default. Detached native descendants are adopted and cleaned up on exit.
"""
from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

# HTTP transport tests use the existing service interpreter's httpx dependency;
# importing TestClient in the pinned backend environment is unsupported.
SERVICE_ONLY_TESTS = (
    "tests/live_evaluation/test_selected_live_native.py",
    "tests/iterations/test_native_iterations.py",
    "tests/components/test_runner_native.py",
    "tests/rules/test_native_rules.py", "tests/rules/test_rule_stdio.py", "tests/rules/test_custom_native.py", "tests/rules/test_standalone_native.py",
)
SERVICE_TESTS = (
    "tests/service/test_real_backend.py", "tests/integration/test_mcp_training.py",
    "tests/security/test_adversarial_review.py", "tests/integration/test_source_bound_execution.py",
    "tests/integration/test_native_multi_output.py", "tests/integration/test_project_source_execution.py",
    "tests/integration/test_bound_dataset_checkpoint_release.py", "tests/integration/test_source_deadline_hooks.py",
    "tests/integration/test_live_mcp_tracking.py", "tests/datasets/test_mcp_dataset.py",
    "tests/datasets/test_native_dataset.py", "tests/native_cache/test_native_reuse.py",
    "tests/conditions/test_native_conditions.py", "tests/integration/test_admission_live_native.py", *SERVICE_ONLY_TESTS,
)
LIFECYCLE_TESTS = (
    "tests/integration/test_cpu_fixtures.py", "tests/integration/test_native_lifecycle.py",
    "tests/integration/test_early_cancellation.py", "tests/integration/test_builtin_warm_start.py",
    "tests/integration/test_model_release_workflow.py", "tests/integration/test_checkpoint_subgraph.py",
    "tests/integration/test_conditional_checkpoint.py", "tests/integration/test_calibrated_reference_lifecycle.py",
    "tests/integration/test_live_reads_during_discovery.py",
)
COMPONENT_TESTS = (
    "tests/components", "tests/evaluation", "tests/live_evaluation",
    "tests/scientific/test_derivation.py", "tests/scientific/test_native_derivation.py",
    "tests/datasets/test_reference_atomic_read.py", "tests/tracking/test_cpu_live_tracking.py",
    "tests/placement/test_placement.py", "tests/placement/test_checkpoint_capabilities.py",
    "tests/placement/test_cuda_execution.py", "tests/integration/test_mlx_service.py",
)
# Keep mixed light/native concurrency files in one backend-only selection.
# The integration marker leaves their light contracts to the service unit phase.
CONCURRENCY_TESTS = ("tests/concurrency",)
MAX_SECONDS = 2700
HEARTBEAT_SECONDS = 30
MAX_JUNIT_EVIDENCE_BYTES = 16 * 1024**2


@dataclass(frozen=True)
class Group:
    name: str
    command: tuple[str, ...]
    environment: dict[str, str]
    log: Path
    xml: Path


def _subreaper():
    if sys.platform != "linux" or not Path("/proc/self/stat").is_file():
        raise RuntimeError("Concurrent native tests require Linux process containment; use the default serial aggregate")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("Concurrent cleanup requires Linux pidfd signaling; use the default serial aggregate")
    try:
        descriptor = os.pidfd_open(os.getpid(), 0)
        try:
            signal.pidfd_send_signal(descriptor, 0, None, 0)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise RuntimeError("Kernel pidfd cleanup capability is unavailable; use the default serial aggregate") from error
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
        raise RuntimeError("Cannot establish native-test descendant cleanup")
    enabled = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(enabled), 0, 0, 0) or enabled.value != 1:
        raise RuntimeError("Native-test descendant cleanup could not be verified")


def _identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[1]), int(fields[19])  # parent PID and kernel start ticks
    except (OSError, IndexError, ValueError):
        return None


def _descendants():
    rows = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdecimal() and (value := _identity(int(entry.name))) is not None:
            rows[int(entry.name)] = value
    found, parents = {}, {os.getpid()}
    while parents:
        children = {pid for pid, (parent, _) in rows.items() if parent in parents and pid not in found}
        found.update({pid: rows[pid] for pid in children})
        parents = children
    return found


def _signal(pid, identity, signum):
    # Bind a kernel process handle before validating identity. A process exiting
    # after validation cannot redirect a signal to a subsequently reused PID.
    try:
        descriptor = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        return
    try:
        current = _identity(pid)
        if current is not None and current[1] == identity[1]:
            try:
                signal.pidfd_send_signal(descriptor, signum, None, 0)
            except ProcessLookupError:
                pass  # The bound process exited; never fall back to os.kill.
    finally:
        os.close(descriptor)


def _reap(processes):
    for process in processes:
        process.poll()
    protected = {process.pid for process in processes}
    for pid, (parent, _) in _descendants().items():
        if parent == os.getpid() and pid not in protected:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass


def _cleanup(processes, *, grace=2.0, deadline=8.0):
    """Contain ordinary, new-session and orphaned descendants, not just groups."""
    started, signaled = time.monotonic(), set()
    while True:
        _reap(processes)
        children = _descendants()
        if not children:
            return {"complete": True, "signaled_processes": len({(pid, ticks) for pid, ticks, _ in signaled})}
        elapsed = time.monotonic() - started
        for pid, identity in children.items():
            signum = signal.SIGTERM if elapsed < grace else signal.SIGKILL
            key = (pid, identity[1], signum)
            if key not in signaled:
                try:
                    _signal(pid, identity, signum)
                except OSError as error:
                    return {"complete": False, "remaining_pids": sorted(children),
                            "error": type(error).__name__, "signaled_processes": len({(pid, ticks) for pid, ticks, _ in signaled})}
                signaled.add(key)
        if elapsed >= deadline:
            return {"complete": False, "remaining_pids": sorted(children), "signaled_processes": len({(pid, ticks) for pid, ticks, _ in signaled})}
        time.sleep(.05)


def _write_report(report, value):
    """Publish a whole, durable JSON document; readers see the old or new receipt."""
    report.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, indent=2, allow_nan=False) + "\n"
    descriptor, name = tempfile.mkstemp(prefix="." + report.name + ".", suffix=".tmp", dir=report.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, report)
        directory = os.open(report.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _junit_evidence(path):
    """Identify observed completed-group evidence without copying test contents.

    Collection is bounded and diagnostic only: the existing nonempty-file/exit
    rule, not XML contents or these extra fields, still determines group status.
    """
    try:
        # A finished test may have left a FIFO or another nonregular path.
        # Nonblocking/no-follow open plus fstat avoids hanging before cleanup.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {"status": "unavailable", "error": "nonregular_file"}
            content = stream.read(MAX_JUNIT_EVIDENCE_BYTES + 1)
            if len(content) > MAX_JUNIT_EVIDENCE_BYTES:
                return {"status": "present", "sha256": None,
                        "hash_unavailable": "size_limit", "size_bytes": os.fstat(stream.fileno()).st_size}
        return {"status": "present" if content else "empty", "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest()}
    except FileNotFoundError:
        return {"status": "missing"}
    except OSError as error:
        return {"status": "unavailable", "error": type(error).__name__}


def _receipt_fields(run_id, *, finalized=False):
    return {"schema_version": "modelbuilder.native-test-queue/v1", "run_id": run_id,
            "updated_at": datetime.now(timezone.utc).isoformat(), "finalized": finalized,
            "exit_code": None, "cleanup": None}


def run_groups(groups, root, report, *, timeout=MAX_SECONDS, continue_on_failure=False, run_id=None, run_state=None):
    """Each group is a complete existing pytest selection; run no more than two."""
    # Clear every planned receipt before launch, including groups that may never
    # start after an earlier failure. CI must not upload an older passing receipt.
    report.parent.mkdir(parents=True, exist_ok=True)
    run_state = run_state if run_state is not None else {}
    run_state.update(launched=False, cleanup=None)
    for group in groups:
        group.xml.unlink(missing_ok=True)
        group.log.unlink(missing_ok=True)
    run_id = run_id or uuid.uuid4().hex
    run_state["snapshot"] = {**_receipt_fields(run_id), "status": "starting", "concurrency": 2,
        "groups": [{"name": group.name, "status": "not_started", "log": str(group.log), "xml": str(group.xml)}
                   for group in groups]}
    _write_report(report, run_state["snapshot"])
    if not 0 < timeout <= MAX_SECONDS or len({group.name for group in groups}) != len(groups):
        raise ValueError(f"Native queue requires unique groups and a deadline of at most {MAX_SECONDS} seconds")
    _subreaper()
    started, running, processes, records = time.monotonic(), {}, [], []
    pending = list(groups)
    stopped = 0
    previous_handlers = {}
    def stop(signum, _frame):
        nonlocal stopped
        stopped = signum
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, stop)
    exit_code, reason, cleanup = 0, "completed", None
    next_heartbeat = started
    def publish(status="running", *, finalized=False):
        snapshot = {**_receipt_fields(run_id, finalized=finalized),
            "concurrency": 2, "deadline_seconds": timeout, "status": status,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "groups": records + [{"name": group.name, "status": "not_started",
                                   "log": str(group.log), "xml": str(group.xml)} for group in pending],
            "continue_on_failure": continue_on_failure,
            "cleanup": cleanup if finalized else None, "exit_code": exit_code if finalized else None}
        run_state["snapshot"] = snapshot
        try:
            _write_report(report, snapshot)
        except OSError as error:
            if finalized:
                raise
            # Progress is observational: an unavailable snapshot must not alter
            # scheduling, test deadlines or owned-child cleanup. Final writes
            # remain mandatory; the previous nonfinal receipt cannot pass.
            print(f"Native progress receipt unavailable: {type(error).__name__}", file=sys.stderr, flush=True)
    try:
        while pending or running:
            now = time.monotonic()
            if stopped:
                exit_code, reason = 128 + stopped, "interrupted"
                break
            if now - started >= timeout:
                exit_code, reason = 124, "deadline_exceeded"
                break
            while pending and len(running) < 2:
                group = pending.pop(0)
                group.log.parent.mkdir(parents=True, exist_ok=True)
                group.xml.unlink(missing_ok=True)  # Never accept an earlier run's JUnit receipt.
                record = {"name": group.name, "pid": None, "status": "starting", "returncode": None,
                          "log": str(group.log), "xml": str(group.xml), "started_seconds": round(now - started, 3)}
                records.append(record)
                publish()
                stream = group.log.open("wb")
                try:
                    process = subprocess.Popen(group.command, cwd=root, env=group.environment,
                        stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                except Exception:
                    record["status"] = "failed_to_start"
                    raise
                finally:
                    stream.close()
                run_state["launched"] = True
                processes.append(process)
                record.update(pid=process.pid, status="running")
                running[process.pid] = (process, group, record)
                publish()
                print(f"Native group started: {group.name}; log: {group.log}", flush=True)
            failed = False
            for pid, (process, group, record) in list(running.items()):
                code = process.poll()
                if code is None:
                    continue
                record.update(returncode=code, status="passed" if code == 0 else "failed",
                              finished_seconds=round(time.monotonic() - started, 3))
                if code == 0 and (not group.xml.is_file() or not group.xml.stat().st_size):
                    record.update(status="failed", error="missing_junit_report")
                publish()  # Preserve the observed exit before reading evidence.
                record["junit"] = _junit_evidence(group.xml)
                publish()
                print(f"Native group finished: {group.name}; status: {record['status']}; exit: {code}", flush=True)
                del running[pid]
                failed |= record["status"] != "passed"
            if failed:
                exit_code, reason = 1, "group_failed"
                if not continue_on_failure:
                    break
            if now >= next_heartbeat:
                print(f"Native queue: {len(running)} running, {len(pending)} waiting; elapsed {int(now - started)}s", flush=True)
                publish()
                next_heartbeat = now + HEARTBEAT_SECONDS
            time.sleep(.1)
    except Exception as error:
        exit_code, reason = 1, "runner_error"
        print(f"Native test runner failed: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
    finally:
        try:
            try:
                try:
                    publish("finalizing")
                except Exception as error:
                    exit_code, reason = 1, "runner_error"
                    print(f"Native progress receipt failed: {type(error).__name__}", file=sys.stderr, flush=True)
            finally:
                # Even receipt/logging failures (including closed pipes) cannot
                # bypass owned-child cleanup or outer signal restoration.
                cleanup = _cleanup(processes)
                run_state["cleanup"] = cleanup
            for process, group, record in running.values():
                record.update(status="canceled", returncode=process.returncode,
                              finished_seconds=round(time.monotonic() - started, 3),
                              junit=_junit_evidence(group.xml))
            if not cleanup["complete"]:
                exit_code, reason = 1, "cleanup_incomplete"
            publish(reason, finalized=True)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    return exit_code


def build_groups(root, python, temporary):
    backend = root / ".venv-backend/bin/python"
    if not backend.is_file():
        raise ValueError("Pinned separate backend interpreter is required")
    groups = []
    for name, interpreter, tests, marker in (
        ("cpu-lifecycle", str(backend), LIFECYCLE_TESTS, ()),
        ("integration", str(python), SERVICE_TESTS, ("-m", "integration")),
        ("cpu-components", str(backend), COMPONENT_TESTS, ()),
        ("cpu-concurrency", str(backend), CONCURRENCY_TESTS, ("-m", "integration")),
    ):
        directory = temporary / name
        directory.mkdir()
        for name_path in ("tmp", "home", "xdg-cache", "zenml-config", "zenml-stores"):
            (directory / name_path).mkdir()
        environment = dict(os.environ)
        environment.update(MODELBUILDER_BACKEND_PYTHON=str(backend), PYTHONPATH=str(root / "src"),
            TMPDIR=str(directory / "tmp"), TMP=str(directory / "tmp"), TEMP=str(directory / "tmp"),
            HOME=str(directory / "home"), XDG_CACHE_HOME=str(directory / "xdg-cache"),
            ZENML_CONFIG_PATH=str(directory / "zenml-config"), ZENML_LOCAL_STORES_PATH=str(directory / "zenml-stores"),
            OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", ARROW_NUM_THREADS="1",
            NUMEXPR_NUM_THREADS="1", TOKENIZERS_PARALLELISM="false")
        if name != "integration":
            environment["MODELBUILDER_REQUIRE_LOCAL_INFERENCE"] = "1"
            environment["MODELBUILDER_REQUIRE_TRAINER"] = "1"
        log, xml = root / f"evidence/tests/{name}.log", root / f"evidence/tests/{name}.xml"
        exclusions = tuple(f"--ignore={path}" for path in SERVICE_ONLY_TESTS) if name != "integration" else ()
        command = (interpreter, "-m", "pytest", *tests, *marker, *exclusions, "-v",
                   f"--basetemp={directory / 'pytest'}", "-o", f"cache_dir={directory / 'cache'}", f"--junitxml={xml}")
        groups.append(Group(name, command, environment, log, xml))
    return groups


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--timeout-seconds", type=int, default=MAX_SECONDS)
    parser.add_argument("--continue-on-failure", action="store_true",
                        help="Collect remaining groups after a failed group; retain failing exit status and deadline")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    evidence = root / "evidence/tests"
    evidence.mkdir(parents=True, exist_ok=True)
    report = evidence / "native-runner.json"
    for name in ("integration", "cpu-lifecycle", "cpu-components", "cpu-concurrency", "cpu-fixtures"):
        for suffix in ("log", "xml"):
            (evidence / f"{name}.{suffix}").unlink(missing_ok=True)
    run_id = uuid.uuid4().hex
    _write_report(report, {**_receipt_fields(run_id), "status": "starting", "concurrency": 2, "groups": []})
    temporary = Path(tempfile.mkdtemp(prefix="modelbuilder-native-tests-"))
    run_state = {"launched": False, "cleanup": None}
    try:
        return run_groups(build_groups(root, args.python, temporary), root, report, timeout=args.timeout_seconds,
                          continue_on_failure=args.continue_on_failure, run_id=run_id, run_state=run_state)
    except Exception as error:
        receipt = run_state.get("snapshot") or json.loads(report.read_text())
        launched = run_state["launched"] or any(group.get("pid") is not None for group in receipt.get("groups", []))
        # A failure while finalizing is not a setup failure. Preserve every
        # observed group and leave cleanup/acceptance unconfirmed.
        receipt.update(**_receipt_fields(run_id, finalized=not launched),
                       status="runner_error" if launched else "setup_failed",
                       error=type(error).__name__)
        receipt["exit_code"] = None if launched else 1
        _write_report(report, receipt)
        phase = "finalization" if launched else "setup"
        print(f"Native queue {phase} failed: {error}", file=sys.stderr)
        return 1
    finally:
        receipt = run_state.get("snapshot") or json.loads(report.read_text())
        # Progress writes are best-effort: stale on-disk PID/cleanup fields
        # cannot authorize deletion of state still used by launched children.
        completed_cleanup = (run_state["cleanup"] or receipt.get("cleanup") or {}).get("complete")
        launched = run_state["launched"] or any(group.get("pid") is not None for group in receipt.get("groups", []))
        if completed_cleanup is False or (launched and completed_cleanup is not True):
            # Never delete state still potentially used by an uncontained child.
            # Keep only the local path in the receipt; do not upload test state.
            receipt["retained_temporary_directory"] = str(temporary)
            _write_report(report, receipt)
        else:
            shutil.rmtree(temporary)


if __name__ == "__main__":
    raise SystemExit(main())
