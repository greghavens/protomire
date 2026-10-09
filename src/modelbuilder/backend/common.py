"""Small shared filesystem protocol for the local worker supervisor."""
from __future__ import annotations
import json
import os
import sys
import ctypes

import psutil
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {} if default is None else default


def process_identity(pid: int) -> str | None:
    """OS-backed process birth identity; never use a numeric PID alone."""
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        return "psutil:" + process.create_time().hex()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
        return None


def _legacy_linux_identity(pid: int) -> str | None:
    """Read v1 Linux journal identities only; keep upgrades recoverable."""
    if sys.platform != "linux":
        raise OSError("Legacy Linux identity is not observable on this OS")
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return None if fields[0] == "Z" else fields[19]


def alive(pid: int | None, identity: str | None) -> bool:
    if not pid or not identity:
        return False
    if identity.startswith("psutil:"):
        return process_identity(pid) == identity
    try:
        return identity.isdecimal() and _legacy_linux_identity(pid) == identity
    except (OSError, IndexError, ValueError):
        return False


def process_group_alive(group_id: int | None) -> bool:
    """Conservatively keep waiting when group observation is unavailable."""
    return process_group_state(group_id) is not False


def _darwin_boot_session() -> str:
    # Native boot-session UUID, unlike wall-clock boot_time which may shift.
    libc = ctypes.CDLL(None, use_errno=True)
    size = ctypes.c_size_t()
    query = libc.sysctlbyname
    query.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                      ctypes.c_void_p, ctypes.c_size_t]
    query.restype = ctypes.c_int
    key = b"kern.bootsessionuuid"
    if query(key, None, ctypes.byref(size), None, 0) != 0 or not 1 < size.value <= 128:
        raise OSError(ctypes.get_errno(), "Cannot read kernel boot session")
    value = ctypes.create_string_buffer(size.value)
    if query(key, value, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot read kernel boot session")
    return value.value.decode("ascii")


def runtime_identity() -> str | None:
    """A real boot/namespace boundary, never a guessed portable timestamp."""
    try:
        if sys.platform == "linux":
            # These are Linux kernel interfaces, not a user's filesystem paths.
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return "linux:" + boot + ":" + os.readlink("/proc/self/ns/pid") if boot else None
        if sys.platform == "darwin":
            boot = _darwin_boot_session()
            return "darwin:" + boot if boot else None
    except (OSError, AttributeError, UnicodeError):
        pass
    return None


def process_group_state(group_id: int | None) -> bool | None:
    """True=live, False=positively absent/zombies only, None=not observable.

    Query native POSIX group/session IDs and psutil's platform-specific process
    API. In particular, an absent /proc on macOS must not mean every job died.
    """
    if not group_id or group_id <= 0 or not hasattr(os, "killpg"):
        return None
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    uncertain = False
    zombies = False
    try:
        members = psutil.pids()
    except (psutil.Error, OSError):
        return None
    for pid in members:
        try:
            if os.getpgid(pid) != group_id:
                continue
            if os.getsid(pid) != group_id:
                # A matching group outside the owned session is not ours.
                uncertain = True
                continue
            if psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
                return True
            zombies = True
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
        except (psutil.AccessDenied, OSError, ValueError):
            uncertain = True
    if uncertain:
        return None
    if zombies:
        return False
    # Empty/partial enumeration is not proof. Recheck the kernel group API.
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass
    return None


def termination_confirmed(state: dict) -> bool:
    """Require original identity and positive observation; ignorance is not death."""
    if not state.get("worker_pid") or not state.get("worker_identity"):
        return False
    if state.get("worker_reaped") is True and state.get("process_group_terminated") is True:
        return True
    recorded_runtime = state.get("runtime_identity")
    current_runtime = runtime_identity()
    if not recorded_runtime or not current_runtime:
        return False
    legacy_runtime = sys.platform == "linux" and current_runtime == "linux:" + recorded_runtime
    if recorded_runtime != current_runtime and not legacy_runtime:
        return False
    try:
        if state["worker_identity"].startswith("psutil:"):
            process = psutil.Process(state["worker_pid"])
            original_gone = (process.status() == psutil.STATUS_ZOMBIE or
                             "psutil:" + process.create_time().hex() != state["worker_identity"])
        elif sys.platform == "linux" and state["worker_identity"].isdecimal():
            original_gone = _legacy_linux_identity(state["worker_pid"]) != state["worker_identity"]
        else:
            return False
    except (psutil.NoSuchProcess, FileNotFoundError, ProcessLookupError):
        original_gone = True
    except (psutil.AccessDenied, OSError, ValueError, IndexError):
        return False
    return original_gone and process_group_state(state["worker_pid"]) is False


def recipe_provenance(recipe: dict) -> dict:
    """Identity of one immutable revision of the project's single recipe."""
    import hashlib
    body = {key: value for key, value in recipe.items() if key != "digest"}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return {"recipe_id": recipe.get("recipe_id"), "recipe_revision": recipe.get("revision"), "recipe_digest": digest}
