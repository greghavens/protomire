"""Fresh-interpreter trusted step bodies; ZenML still owns steps and artifacts.

This prevents source imports and framework process globals from racing native
inline threads. It is not a hostile-code sandbox or a resource reservation.
The child stays in the supervisor's owned process group. Inert tagged JSON,
not pickle, carries already materialized native inputs and return values.
"""
from __future__ import annotations

from functools import wraps
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback
import uuid

import psutil

from .common import now, process_identity, write_json

_CHILD = False
_METADATA: list[dict] = []
MAX_MESSAGE_BYTES = 256 * 1024 * 1024


def in_child() -> bool:
    return _CHILD


def context(spec: dict):
    if _CHILD:
        return spec["parameters"], Path(spec["job_dir"]) / "outputs" / spec.get("invocation", {}).get("invocation_id", spec["node_id"])
    from .worker import context as native_context
    return native_context(spec)


def enabled(spec: dict) -> bool:
    return spec.get("max_parallel_stages", 1) > 1 and not _CHILD


def encode(value):
    if isinstance(value, Path):
        return ["path", str(value.absolute())]
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise TypeError("Process ABI requires string dictionary keys")
        return ["dict", {key: encode(item) for key, item in value.items()}]
    if isinstance(value, (list, tuple)):
        return ["tuple" if isinstance(value, tuple) else "list", [encode(item) for item in value]]
    if value is None or type(value) is bool:
        return ["value", value]
    # JSON-native subclasses (e.g. torch.__version__'s TorchVersion) serialize
    # as their primitive value; no custom type is reconstructed in the owner.
    for primitive in (str, int, float):
        if isinstance(value, primitive):
            return ["value", primitive(value)]
    raise TypeError("Unsupported trusted process ABI value: " + type(value).__name__)


def decode(value):
    kind, payload = value
    if kind == "path":
        return Path(payload)
    if kind == "dict":
        return {key: decode(item) for key, item in payload.items()}
    if kind in ("list", "tuple"):
        result = [decode(item) for item in payload]
        return tuple(result) if kind == "tuple" else result
    if kind == "value":
        return payload
    raise ValueError("Unknown trusted process ABI tag")


def _write_message(path: Path, value: dict):
    raw = json.dumps(encode(value), allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("Trusted process ABI exceeds 256 MiB message bound")
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, path)


def _read_message(path: Path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_MESSAGE_BYTES + 1)
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("Trusted process ABI exceeds 256 MiB message bound")
    return decode(json.loads(raw))


def enable_child_subreaper() -> bool:
    """Adopt orphaned descendants in dedicated execution/supervision processes."""
    if sys.platform != "linux":
        return False
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "Cannot establish child reaping ownership")
    return True


def reap_session_children(session_id: int) -> list[int]:
    """Reap only adopted children in this completed job's owned POSIX session."""
    reaped = []
    for process in psutil.Process().children():
        try:
            if os.getsid(process.pid) == session_id:
                pid, _ = os.waitpid(process.pid, os.WNOHANG)
                if pid:
                    reaped.append(pid)
        except (ChildProcessError, ProcessLookupError, psutil.NoSuchProcess):
            pass
    return reaped


def _stop_descendants(processes):
    uncertain = set()
    for process in reversed(processes):
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
        except (psutil.Error, OSError):
            uncertain.add(process.pid)
    try:
        _, remaining = psutil.wait_procs(processes, timeout=.3)
    except (psutil.Error, OSError):
        remaining = processes
    for process in remaining:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
        except (psutil.Error, OSError):
            uncertain.add(process.pid)
    try:
        _, remaining = psutil.wait_procs(remaining, timeout=.3)
    except (psutil.Error, OSError):
        uncertain.update(process.pid for process in remaining)
    for process in remaining:
        try:
            if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                uncertain.add(process.pid)
        except psutil.NoSuchProcess:
            pass
        except (psutil.Error, OSError):
            uncertain.add(process.pid)
    return sorted(uncertain)


def publish_cancellation(job: Path, record: dict):
    """First cancellation wins, even across native attempts and supervision."""
    cancellation = Path(job) / "cancel.json"
    temporary = cancellation.with_name(".cancel-" + uuid.uuid4().hex + ".json")
    write_json(temporary, record)
    try:
        try:
            os.link(temporary, cancellation)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _abort(job: Path, reason: str, spec: dict):
    publish_cancellation(job, {"requested_at": now(), "reason": reason,
                              "node_id": spec["node_id"], **spec.get("invocation", {})})


def isolated_command(module: str, *arguments: str) -> list[str]:
    """Start only an explicitly named platform module from this installed tree."""
    bootstrap = ("import importlib,sys; sys.path.insert(0, sys.argv.pop(1)); "
                 "importlib.import_module(sys.argv.pop(1)).main()")
    return [sys.executable, "-I", "-c", bootstrap,
            str(Path(__file__).resolve().parents[2]), module, *map(str, arguments)]


def execute(kind: str, inputs: dict, spec: dict):
    """Run one native attempt's body, returning values for native materialization."""
    if (Path(spec["job_dir"]) / "cancel.json").exists():
        raise InterruptedError("Cancellation requested before trusted stage process startup")
    token = uuid.uuid4().hex
    directory = Path(spec["job_dir"]) / "processes" / token
    directory.mkdir(parents=True, exist_ok=False)
    request, response = directory / "request.json", directory / "response.json"
    _write_message(request, {"kind": kind, "inputs": inputs, "spec": spec})
    # Do not fork a multithreaded Torch/ZenML process or inherit mutable Python
    # path globals. The operator's selected installed interpreter is authoritative.
    environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    ownership = {"token": token, "node_id": spec["node_id"], **spec.get("invocation", {}),
                 "started_at": now(), "parent_pid": os.getpid(), "reaped": False}
    interrupted = False
    cancellation_started = None
    descendants = []
    child = subprocess.Popen(isolated_command("modelbuilder.backend.process_execution", request, response),
                             stdin=subprocess.DEVNULL, env=environment, start_new_session=False)
    try:
        ownership.update(pid=child.pid, identity=process_identity(child.pid))
        write_json(directory / "owner.json", ownership)
        while child.poll() is None:
            if (Path(spec["job_dir"]) / "cancel.json").exists():
                interrupted = True
                if cancellation_started is None:
                    cancellation_started = time.monotonic()
                    try:
                        descendants = psutil.Process(child.pid).children(recursive=True)
                    except psutil.NoSuchProcess:
                        pass
                    child.terminate()
                elif time.monotonic() - cancellation_started > .75:
                    child.kill()
            time.sleep(.025)
        child.wait()
        if interrupted:
            raise InterruptedError("Native stage execution cancelled")
        if child.returncode != 0 or not response.is_file():
            _abort(Path(spec["job_dir"]), "stage_process_cleanup_unproven", spec)
            raise RuntimeError(f"Trusted stage process exited ({child.returncode}) without a successful response")
        result = _read_message(response)
        ownership["descendant_cleanup"] = result.get("cleanup", {})
        if result.get("cleanup", {}).get("remaining_pids"):
            _abort(Path(spec["job_dir"]), "stage_process_cleanup_unproven", spec)
            raise RuntimeError("Trusted stage descendant cleanup could not be confirmed")
        if "error" in result:
            raise RuntimeError(result["error"] + "\n" + result.get("traceback", ""))
        for record in result.get("metadata", []):
            from zenml import log_metadata
            log_metadata(record["metadata"], **record["options"])
        return result["value"]
    finally:
        if child.poll() is None:
            try:
                descendants = psutil.Process(child.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                pass
            child.kill()
            _abort(Path(spec["job_dir"]), "stage_process_cleanup_unproven", spec)
        child.wait()
        remaining = _stop_descendants(descendants)
        if remaining:
            _abort(Path(spec["job_dir"]), "stage_process_cleanup_unproven", spec)
        ownership.update(reaped=True, direct_child_reaped=True, exit_code=child.returncode,
                         remaining_observed_descendants=remaining, ended_at=now())
        write_json(directory / "owner.json", ownership)
        # Inputs may contain large datasets. Retain only ownership evidence.
        request.unlink(missing_ok=True)
        response.unlink(missing_ok=True)


def scoped_builtin(function):
    """Preserve the native typed signature while isolating fixture process state."""
    @wraps(function)
    def invoke(*args, **kwargs):
        import inspect
        arguments = inspect.signature(function).bind(*args, **kwargs).arguments
        spec = arguments["spec"]
        if not enabled(spec):
            return function(*args, **kwargs)
        from .worker import context
        context(spec)
        return execute("builtin", {key: value for key, value in arguments.items() if key != "spec"}, spec)
    return invoke


def metadata(value: dict, **options):
    if _CHILD:
        _METADATA.append({"metadata": value, "options": options})
    else:
        from zenml import log_metadata
        log_metadata(value, **options)


def stage_returned_paths(value, directory: Path):
    """Copy returned Path contents while the child still owns their lifetime.

    A Path can belong to a source snapshot or a module-owned TemporaryDirectory;
    neither lifetime is visible to native materialization after interpreter exit.
    Copies are durable attempt-local handoffs, not a second artifact registry.
    Originals, including connected input Paths, are never moved or modified.
    """
    import shutil
    copied = {}
    def stage(item):
        if isinstance(item, Path):
            path = item.absolute()
            if path.is_dir() and directory.resolve().is_relative_to(path.resolve()):
                raise ValueError("Returned Path cannot contain its own process handoff storage")
            if path not in copied:
                target = directory / str(len(copied)) / (path.name or "root")
                target.parent.mkdir(parents=True, exist_ok=True)
                if path.is_dir():
                    shutil.copytree(path, target)
                else:
                    shutil.copy2(path, target)
                copied[path] = target
            return copied[path]
        if isinstance(item, dict):
            return {key: stage(child) for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            values = [stage(child) for child in item]
            return tuple(values) if isinstance(item, tuple) else values
        return item
    return stage(value)


def main(request: Path | None = None, response: Path | None = None):
    request = Path(sys.argv[1]) if request is None else request
    response = Path(sys.argv[2]) if response is None else response
    global _CHILD
    _CHILD = True
    subreaper = enable_child_subreaper()
    def cancelled(signum, frame):
        raise InterruptedError("Trusted stage process received termination")
    signal.signal(signal.SIGTERM, cancelled)
    result = {}
    try:
        invocation = _read_message(request)
        spec, inputs = invocation["spec"], invocation["inputs"]
        if (Path(spec["job_dir"]) / "cancel.json").exists():
            raise InterruptedError("Cancellation requested before trusted source import")
        if invocation["kind"] == "builtin":
            from .worker import STEPS
            value = STEPS[spec["component_node"]["kind"]].entrypoint(**inputs, spec=spec)
        elif invocation["kind"] == "component":
            from .components.runtime import dispatch
            value = dispatch(inputs, spec)
        elif invocation["kind"] == "python":
            from .components.runtime import dispatch_python
            value = dispatch_python(inputs, spec)
        else:
            raise ValueError("Unknown isolated native body kind")
        result = {"value": stage_returned_paths(value, request.parent / "returned-paths"), "metadata": _METADATA}
    except BaseException as error:
        result = {"error": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc()}
    finally:
        observed, uncertain = set(), set()
        # Terminating parents may expose newly adopted grandchildren. Reinspect
        # while the dedicated body owns the tree, never other native attempts.
        for _ in range(3):
            descendants = psutil.Process().children(recursive=True)
            observed.update(process.pid for process in descendants)
            uncertain.update(_stop_descendants(descendants))
            if not psutil.Process().children(recursive=True):
                break
        remaining = sorted(uncertain | {process.pid for process in psutil.Process().children(recursive=True)})
        result["cleanup"] = {"subreaper": subreaper, "observed_pids": sorted(observed),
                             "remaining_pids": remaining}
    try:
        _write_message(response, result)
    except Exception as error:
        _write_message(response, {"error": f"{type(error).__name__}: {error}",
                                  "traceback": traceback.format_exc(), "cleanup": result["cleanup"]})


if __name__ == "__main__":
    # Imports by the native wrapper must see this same child flag/module state.
    sys.modules["modelbuilder.backend.process_execution"] = sys.modules[__name__]
    main(Path(sys.argv[1]), Path(sys.argv[2]))
