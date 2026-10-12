"""Opt-in Linux native-test queue: three full groups, at most two pytest roots.

This stays on the existing CPU runner. No xdist, new packages, test selection,
training worker changes, or additional CI machines. The serial aggregate remains
the default. Detached native descendants are adopted and cleaned up on exit.
"""
from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

# HTTP transport tests use the existing service interpreter's httpx dependency;
# importing TestClient in the pinned backend environment is unsupported.
SERVICE_ONLY_TESTS = (
    "tests/live_evaluation/test_selected_live_native.py",
    "tests/iterations/test_native_iterations.py",
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
    "tests/datasets/test_reference_atomic_read.py", "tests/tracking/test_cpu_live_tracking.py",
    "tests/placement/test_placement.py", "tests/placement/test_checkpoint_capabilities.py",
    "tests/placement/test_cuda_execution.py",
)
MAX_SECONDS = 2100


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


def run_groups(groups, root, report, *, timeout=MAX_SECONDS):
    """Each group is a complete existing pytest selection; run no more than two."""
    # Clear every planned receipt before launch, including groups that may never
    # start after an earlier failure. CI must not upload an older passing receipt.
    report.parent.mkdir(parents=True, exist_ok=True)
    for group in groups:
        group.xml.unlink(missing_ok=True)
        group.log.unlink(missing_ok=True)
    report.write_text(json.dumps({"status": "starting", "concurrency": 2,
        "groups": [{"name": group.name, "status": "not_started"} for group in groups]}, indent=2) + "\n")
    if not 0 < timeout <= MAX_SECONDS or len({group.name for group in groups}) != len(groups):
        raise ValueError("Native queue requires unique groups and a deadline of at most 2100 seconds")
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
                stream = group.log.open("wb")
                try:
                    process = subprocess.Popen(group.command, cwd=root, env=group.environment,
                        stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                except Exception:
                    record["status"] = "failed_to_start"
                    raise
                finally:
                    stream.close()
                processes.append(process)
                record.update(pid=process.pid, status="running")
                running[process.pid] = (process, group, record)
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
                print(f"Native group finished: {group.name}; status: {record['status']}; exit: {code}", flush=True)
                del running[pid]
                failed |= record["status"] != "passed"
            if failed:
                exit_code, reason = 1, "group_failed"
                break
            if now >= next_heartbeat:
                print(f"Native queue: {len(running)} running, {len(pending)} waiting; elapsed {int(now - started)}s", flush=True)
                next_heartbeat = now + 30
            time.sleep(.1)
    except Exception as error:
        exit_code, reason = 1, "runner_error"
        print(f"Native test runner failed: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
    finally:
        cleanup = _cleanup(processes)
        for process, _, record in running.values():
            record.update(status="canceled", returncode=process.returncode,
                          finished_seconds=round(time.monotonic() - started, 3))
        records.extend({"name": group.name, "status": "not_started", "log": str(group.log), "xml": str(group.xml)}
                       for group in pending)
        if not cleanup["complete"]:
            exit_code, reason = 1, "cleanup_incomplete"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({"concurrency": 2, "deadline_seconds": timeout, "status": reason,
            "elapsed_seconds": round(time.monotonic() - started, 3), "groups": records,
            "cleanup": cleanup, "exit_code": exit_code}, indent=2) + "\n")
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
    args = parser.parse_args(argv)
    root = args.root.resolve()
    evidence = root / "evidence/tests"
    evidence.mkdir(parents=True, exist_ok=True)
    report = evidence / "native-runner.json"
    for name in ("integration", "cpu-lifecycle", "cpu-components", "cpu-fixtures"):
        for suffix in ("log", "xml"):
            (evidence / f"{name}.{suffix}").unlink(missing_ok=True)
    report.write_text(json.dumps({"status": "starting", "concurrency": 2}) + "\n")
    temporary = Path(tempfile.mkdtemp(prefix="modelbuilder-native-tests-"))
    try:
        return run_groups(build_groups(root, args.python, temporary), root, report, timeout=args.timeout_seconds)
    except Exception as error:
        report.write_text(json.dumps({"status": "setup_failed", "exit_code": 1,
            "error": type(error).__name__, "message": str(error), "groups": []}, indent=2) + "\n")
        print(f"Native queue setup failed: {error}", file=sys.stderr)
        return 1
    finally:
        receipt = json.loads(report.read_text())
        if receipt.get("cleanup", {}).get("complete") is False:
            # Never delete state still potentially used by an uncontained child.
            # Keep only the local path in the receipt; do not upload test state.
            receipt["retained_temporary_directory"] = str(temporary)
            report.write_text(json.dumps(receipt, indent=2) + "\n")
        else:
            shutil.rmtree(temporary)


if __name__ == "__main__":
    raise SystemExit(main())
