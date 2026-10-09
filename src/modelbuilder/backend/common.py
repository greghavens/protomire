"""Small shared filesystem protocol for the local worker supervisor."""
from __future__ import annotations
import json
import os
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
    try:
        # /proc comm can contain spaces; fields after its final ')' start at stat field 3.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError):
        return None


def alive(pid: int | None, identity: str | None) -> bool:
    return bool(pid and identity and process_identity(pid) == identity)


def process_group_alive(group_id: int | None) -> bool:
    """Check runnable members, including children after the group leader exits."""
    if not group_id:
        return False
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            if fields[0] != "Z" and int(fields[2]) == group_id and int(fields[3]) == group_id:
                return True
        except (FileNotFoundError, PermissionError, ProcessLookupError, IndexError, ValueError):
            continue
    return False


def runtime_identity() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + os.readlink("/proc/self/ns/pid")
    except (OSError, PermissionError):
        return None


def process_group_state(group_id: int | None) -> bool | None:
    """True=live, False=positively absent/zombies only, None=not observable."""
    if not group_id:
        return None
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return None
    uncertain = False
    try:
        members = list(Path("/proc").glob("[0-9]*/stat"))
    except OSError:
        return None
    for path in members:
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            if fields[0] != "Z" and int(fields[2]) == group_id and int(fields[3]) == group_id:
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (PermissionError, OSError, IndexError, ValueError):
            uncertain = True
    return None if uncertain else False


def termination_confirmed(state: dict) -> bool:
    """Require original identity and positive observation; ignorance is not death."""
    if not state.get("worker_pid") or not state.get("worker_identity"):
        return False
    if state.get("worker_reaped") is True and state.get("process_group_terminated") is True:
        return True
    if not state.get("runtime_identity") or state["runtime_identity"] != runtime_identity():
        return False
    try:
        fields = Path(f"/proc/{state['worker_pid']}/stat").read_text().rsplit(")", 1)[1].split()
        original_gone = fields[0] == "Z" or fields[19] != state["worker_identity"]
    except (FileNotFoundError, ProcessLookupError):
        original_gone = True
    except (PermissionError, OSError, IndexError):
        return False
    return original_gone and process_group_state(state["worker_pid"]) is False


def recipe_provenance(recipe: dict) -> dict:
    """Identity of one immutable revision of the project's single recipe."""
    import hashlib
    body = {key: value for key, value in recipe.items() if key != "digest"}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return {"recipe_id": recipe.get("recipe_id"), "recipe_revision": recipe.get("revision"), "recipe_digest": digest}
