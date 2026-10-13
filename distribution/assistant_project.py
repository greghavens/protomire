"""Project-only, reviewed multi-file assistant integration. No vendor invocation.

Receipts contain only managed public content and hashes, never snapshots of an
existing config (which can contain secrets). Rollback removes only owned content.
Multi-file writes stage and validate first; an interruption retains an applying
receipt for explicit recovery, while ordinary errors restore originals in memory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import tomllib

import assistant_setup as setup

VERSION = "1"
CLIENTS = frozenset({"codex", "claude"})


def checked_root(workspace: Path) -> Path:
    root = setup.absolute(workspace)
    if not root.is_dir() or any(p.is_symlink() for p in (root, *root.parents)):
        raise setup.SetupError("workspace_required", "Choose an existing, non-symlink local project folder.")
    return root


def checked_path(root: Path, relative: str) -> Path:
    path = root / relative
    if (not relative or Path(relative).is_absolute() or "\\" in relative
            or any(p in {"", ".", ".."} for p in relative.split("/"))):
        raise setup.SetupError("unsafe_config_path", "Managed paths must remain inside the selected project.")
    setup.config_bytes(path)  # validates all parents, links, type and size
    return path


def hash_bytes(raw):
    return setup.digest(raw) if raw is not None else None


def receipt_name(client):
    return f".modelbuilder/assistant-setup/{client}.json"


def load_receipt(root, client):
    raw = setup.config_bytes(checked_path(root, receipt_name(client)))
    if raw is None:
        return None, None
    try:
        receipt = setup.parse_json(raw)
        if (receipt.get("schema_version") != VERSION or receipt.get("client") != client
                or receipt.get("state") not in {"applying", "applied", "rolled_back"}
                or not isinstance(receipt.get("files"), list)):
            raise ValueError("receipt")
        for item in receipt["files"]:
            checked_path(root, item["path"])
            if item["kind"] not in {"config", "append", "owned"}:
                raise ValueError("kind")
    except (KeyError, TypeError, ValueError):
        raise setup.SetupError("receipt_conflict", "The managed receipt is malformed. No project files were changed.") from None
    return receipt, raw


def entry_from_command(client: str, command: dict, project_id: str) -> dict:
    if (not isinstance(command, dict) or set(command) != {"command", "args"}
            or not isinstance(command["command"], str) or not Path(command["command"]).is_absolute()
            or not Path(command["command"]).is_file() or not isinstance(command["args"], list)):
        raise setup.SetupError("invalid_managed_command", "Use the native managed-core command for this project.")
    args = command["args"]
    if (len(args) != 9 or args[0] != "-I" or not isinstance(args[1], str)
            or not Path(args[1]).is_absolute() or Path(args[1]).name != "managed_core.py"
            or not Path(args[1]).is_file() or args[2] != "mcp"
            or args[3] != "--runtime-root" or args[5] != "--state-dir" or args[7] != "--project-id"
            or args[8] != project_id or any(not isinstance(v, str) or "\x00" in v for v in args)
            or not Path(args[4]).is_absolute() or not Path(args[6]).is_absolute()):
        raise setup.SetupError("invalid_managed_command", "Managed MCP requires the exact native project-scoped command.")
    return {**command, **({"type": "stdio"} if client == "claude" else {})}


def config_entry(client, raw):
    try:
        data = setup.parse_json(raw or b"{}") if client == "claude" else tomllib.loads((raw or b"").decode())
        servers = data.get("mcpServers" if client == "claude" else "mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError("servers")
        return servers.get("modelbuilder")
    except (ValueError, UnicodeError):
        raise setup.SetupError("unsupported_config", "The selected project MCP config cannot be safely edited.") from None


def template_files(client):
    base = Path(__file__).with_name("assistant_templates") / f"v{VERSION}"
    skill = ".agents/skills/modelbuilder-project" if client == "codex" else ".claude/skills/modelbuilder-project"
    return {f"{skill}/SKILL.md": (base / "SKILL.md").read_bytes(),
            f"{skill}/references/instructions.md": (base / "instructions.md").read_bytes(),
            f"{skill}/references/operation-catalog.json": (base / "operation-catalog.json").read_bytes()}


def instruction_block(client):
    skill = ".agents/skills/modelbuilder-project" if client == "codex" else ".claude/skills/modelbuilder-project"
    # Appended without normalizing/reformatting existing instructions. Never touch
    # AGENTS.override.md, nested files or the .claude/CLAUDE.md hierarchy.
    return (f"\n<!-- modelbuilder:{client}:v{VERSION}:begin -->\n"
            f"ModelBuilder project workflow: read {skill}/SKILL.md and its references.\n"
            "This integration grants no permissions. Existing owner instructions and override scopes remain in force.\n"
            f"<!-- modelbuilder:{client}:v{VERSION}:end -->\n").encode()


def preview_project(client, workspace, project_id, *, command=None, python=None, service_url=None):
    if client not in CLIENTS:
        raise setup.SetupError("capability_unavailable", "Triggerfish setup is an interface only; compatibility has not been accepted.")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", project_id or ""):
        raise setup.SetupError("project_required", "Create or select one model project before configuring its assistant.")
    root = checked_root(workspace)
    if command is not None:
        entry = entry_from_command(client, command, project_id)
    else:
        if not python or not service_url:
            raise setup.SetupError("runtime_required", "Choose the native managed runtime, or an explicit external service interpreter and URL.")
        python = setup.interpreter_path(python)
        setup.inspect_interpreter(python)
        entry = setup.server_entry(client, python, setup.validate_url(service_url))
    receipt, receipt_raw = load_receipt(root, client)
    current = receipt if receipt and receipt["state"] != "rolled_back" else None
    if current:
        if (current["state"] != "applied" or current.get("project_id") != project_id
                or current.get("entry") != entry or current.get("template_version") != VERSION):
            raise setup.SetupError("receipt_conflict", "A different or interrupted managed setup exists. Review rollback before replacing it.")
        for item in current["files"]:
            if hash_bytes(setup.config_bytes(root / item["path"])) != item["after_sha256"]:
                raise setup.SetupError("managed_file_changed", "A managed file changed since setup. Review rollback before reconfiguring; edits are preserved.")
    config = ".codex/config.toml" if client == "codex" else ".mcp.json"
    instructions = "AGENTS.md" if client == "codex" else "CLAUDE.md"
    originals = {}
    for path in [config, instructions, *template_files(client)]:
        originals[path] = setup.config_bytes(checked_path(root, path))
    old_entry = config_entry(client, originals[config])
    if old_entry is not None and not current:
        raise setup.SetupError("config_conflict", "An unmanaged modelbuilder MCP entry already exists. It was preserved; review it before using managed setup.")
    updated, _ = setup.render_config(client, originals[config], entry)
    block = instruction_block(client)
    files = [{"path": config, "kind": "config", "content": updated.decode()}]
    if not current and b"<!-- modelbuilder:" in (originals[instructions] or b""):
        raise setup.SetupError("instruction_conflict", "Untracked managed instruction markers exist. No instructions were changed.")
    files.append({"path": instructions, "kind": "append", "content":
                  ((originals[instructions] or b"") + (b"" if current else block)).decode(), "managed_block": block.decode()})
    for path, content in template_files(client).items():
        if originals[path] is not None and not current:
            raise setup.SetupError("skill_conflict", "A project skill already occupies the managed name. It was preserved.")
        files.append({"path": path, "kind": "owned", "content": content.decode()})
    for item in files:
        item.update(before_sha256=hash_bytes(originals[item["path"]]),
                    after_sha256=setup.digest(item["content"].encode()), created=originals[item["path"]] is None)
    warnings = []
    if client == "codex" and (root / "AGENTS.override.md").exists():
        warnings.append("AGENTS.override.md exists and may override AGENTS.md. It is preserved; review the vendor's instruction hierarchy.")
    if client == "claude" and (root / ".claude/CLAUDE.md").exists():
        warnings.append(".claude/CLAUDE.md also exists. Its instructions and hierarchy are preserved.")
    # Do not return existing file content: it may contain user credentials.
    plan = {"action": "configure-project", "client": client, "project_id": project_id,
            "workspace": str(root), "scope": "project", "template_version": VERSION,
            "entry": entry, "files": [{k: v for k, v in item.items() if k not in {"content", "managed_block"}} for item in files],
            "receipt_before_sha256": hash_bytes(receipt_raw), "change": "unchanged" if current else "add",
            "warnings": warnings, "credentials_stored": False,
            "capability": "project_read_only" if command else "external_service_scopes",
            "notice": "Review the exact project paths. Only managed files/blocks are added. Vendor sign-in, trust, approval settings and global files are unchanged. Runtime permissions are enforced by the service."}
    return {"ok": True, "plan": plan, "approval": setup.digest(plan)}, files


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    setup.config_bytes(path)
    if data is None:
        path.unlink(missing_ok=True)
        return
    fd, name = tempfile.mkstemp(prefix=".modelbuilder-stage-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _transaction(root, changes, receipt_before, receipt_after):
    receipt_path = checked_path(root, receipt_name(receipt_after["client"]))
    receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = receipt_path.parent / "project.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise setup.SetupError("config_busy", "Another project setup owns the lock. No files were changed.") from None
    os.close(fd)
    written = []
    originals = {}
    try:
        if setup.config_bytes(receipt_path) != receipt_before:
            raise setup.SetupError("config_changed", "Setup receipt changed since preview. Review again.")
        for item in changes:
            path = checked_path(root, item["path"])
            originals[path] = setup.config_bytes(path)
            if hash_bytes(originals[path]) != item["before_sha256"]:
                raise setup.SetupError("config_changed", "A project file changed since preview. Nothing was overwritten.")
        applying = {**receipt_after, "state": "applying"}
        _atomic(receipt_path, (json.dumps(applying, indent=2) + "\n").encode())
        for item in changes:
            path = checked_path(root, item["path"])
            if setup.config_bytes(path) != originals[path]:
                raise setup.SetupError("config_changed", "A project file changed during setup. It was preserved.")
            data = item["content"].encode() if item["content"] is not None else None
            _atomic(path, data)
            written.append((path, data))
        _atomic(receipt_path, (json.dumps(receipt_after, indent=2) + "\n").encode())
    except BaseException:
        incomplete = False
        for path, installed in reversed(written):
            try:
                if setup.config_bytes(path) == installed:
                    _atomic(path, originals[path])
                else:
                    incomplete = True
            except OSError:
                incomplete = True
        if not incomplete:
            _atomic(receipt_path, receipt_before)
        else:
            raise setup.SetupError("rollback_incomplete", "Setup was interrupted and a concurrent edit was preserved. Review the applying receipt before recovery.") from None
        raise
    finally:
        lock.unlink(missing_ok=True)


def apply_project(preview, files, approval):
    plan = preview["plan"]
    setup.require_approval(plan, approval)
    root = checked_root(Path(plan["workspace"]))
    receipt, receipt_raw = load_receipt(root, plan["client"])
    if hash_bytes(receipt_raw) != plan["receipt_before_sha256"]:
        # The same approved transaction is safe to retry if every result still matches.
        if receipt and receipt.get("approval") == approval and receipt["state"] == "applied" and all(
                hash_bytes(setup.config_bytes(root / item["path"])) == item["after_sha256"] for item in receipt["files"]):
            return {"ok": True, "status": "unchanged", "receipt": str(root / receipt_name(plan["client"]))}
        raise setup.SetupError("config_changed", "Setup receipt changed since review.")
    if plan["change"] == "unchanged":
        for item in files:
            if hash_bytes(setup.config_bytes(root / item["path"])) != item["before_sha256"]:
                raise setup.SetupError("config_changed", "A project file changed since review.")
        return {"ok": True, "status": "unchanged", "receipt": str(root / receipt_name(plan["client"]))}
    receipt = {"schema_version": VERSION, "template_version": VERSION, "client": plan["client"],
               "project_id": plan["project_id"], "state": "applied", "approval": approval,
               "entry": plan["entry"], "files": [{k: v for k, v in item.items() if k != "content"} for item in files]}
    _transaction(root, files, receipt_raw, receipt)
    return {"ok": True, "status": "configured", "receipt": str(root / receipt_name(plan["client"])),
            "files": [item["path"] for item in files], "credentials_stored": False,
            "authentication": "UNVERIFIED", "transport": "UNVERIFIED", "warnings": plan["warnings"]}


def remove_entry(client, raw, expected):
    if config_entry(client, raw) != expected:
        raise setup.SetupError("managed_file_changed", "The managed MCP entry changed. Rollback will not remove an edited entry.")
    if client == "claude":
        data = setup.parse_json(raw)
        data["mcpServers"].pop("modelbuilder")
        if not data["mcpServers"]:
            data.pop("mcpServers")
        return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()
    # Match the exact public stanza written by this helper. This preserves owner
    # comments/formatting outside it and refuses edits inside the owned stanza.
    owned = setup.render_codex(b"", expected).lstrip(b"\r\n")
    if raw.count(owned) != 1:
        raise setup.SetupError("managed_file_changed", "The managed TOML stanza was edited. Rollback preserves it and requires review.")
    updated = raw.replace(owned, b"", 1)
    if tomllib.loads(updated.decode()) != setup.without_server(tomllib.loads(raw.decode()), "mcp_servers"):
        raise setup.SetupError("unsupported_config", "Edited TOML layout cannot be rolled back safely.")
    return updated


def preview_rollback(client, workspace):
    if client not in CLIENTS:
        raise setup.SetupError("capability_unavailable", "This assistant has no accepted project integration.")
    root = checked_root(workspace)
    receipt, raw_receipt = load_receipt(root, client)
    if not receipt:
        raise setup.SetupError("receipt_required", "No managed project setup receipt exists.")
    changes = []
    if receipt["state"] != "rolled_back":
        for item in receipt["files"]:
            raw = setup.config_bytes(root / item["path"])
            # Interrupted apply: files still at the before hash were never installed.
            if receipt["state"] == "applying" and hash_bytes(raw) == item["before_sha256"]:
                continue
            if item["kind"] == "owned":
                if hash_bytes(raw) != item["after_sha256"]:
                    raise setup.SetupError("managed_file_changed", "A managed skill was edited. Rollback preserves it and requires review.")
                updated = None
            elif item["kind"] == "append":
                block = item["managed_block"].encode()
                if raw is None or raw.count(block) != 1:
                    raise setup.SetupError("managed_file_changed", "Managed instructions were edited. Rollback preserves them and requires review.")
                updated = raw.replace(block, b"", 1)
                if not updated and item["created"]:
                    updated = None
            else:
                updated = remove_entry(client, raw, receipt["entry"])
                if item["created"] and (not updated.strip() or updated.strip() == b"{}"):
                    updated = None
            changes.append({"path": item["path"], "before_sha256": hash_bytes(raw),
                            "after_sha256": hash_bytes(updated), "content": updated.decode() if updated is not None else None})
    plan = {"action": "rollback-project", "client": client, "workspace": str(root),
            "receipt_before_sha256": setup.digest(raw_receipt),
            "files": [{k: v for k, v in item.items() if k != "content"} for item in changes],
            "notice": "Only managed content is removed. Unrelated project edits and vendor override files remain."}
    return {"ok": True, "plan": plan, "approval": setup.digest(plan)}, changes


def rollback_project(preview, changes, approval):
    plan = preview["plan"]
    setup.require_approval(plan, approval)
    root = checked_root(Path(plan["workspace"]))
    receipt, raw = load_receipt(root, plan["client"])
    if hash_bytes(raw) != plan["receipt_before_sha256"]:
        raise setup.SetupError("config_changed", "Setup receipt changed since rollback review.")
    if receipt["state"] == "rolled_back":
        return {"ok": True, "status": "unchanged"}
    _transaction(root, changes, raw, {**receipt, "state": "rolled_back", "rollback_approval": approval})
    return {"ok": True, "status": "rolled_back", "credentials_stored": False}
