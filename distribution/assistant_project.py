"""Project-only, reviewed multi-file assistant integration. No vendor invocation.

Receipts contain only managed public content and hashes, never snapshots of an
existing config (which can contain secrets). Rollback removes only owned content.
Multi-file writes stage and validate first; an interruption retains an applying
receipt for explicit recovery, while ordinary errors restore originals in memory.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import tomllib

import assistant_setup as setup

VERSION = "1"
CLIENTS = frozenset({"codex", "claude", "triggerfish", "opencode", "kilocode"})
JSON_CLIENTS = frozenset({"opencode", "kilocode"})
SKILL_ROOTS = {"codex": ".agents/skills", "claude": ".claude/skills", "opencode": ".opencode/skills", "kilocode": ".kilo/skills"}
FLATPAK_PROFILE = Path("/app/share/modelbuilder/flatpak-profile.json")
CONFIG_PATHS = {"codex": ".codex/config.toml", "claude": ".mcp.json", "opencode": "opencode.json", "kilocode": ".kilo/kilo.json"}


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
    # Current native commands suppress bytecode; accept the previous exact
    # vector too, without admitting arbitrary Python flags or changing either.
    if len(args) == 10 and args[:2] == ["-I", "-B"]:
        args = [args[0], *args[2:]]
    if (len(args) != 9 or args[0] != "-I" or not isinstance(args[1], str)
            or not Path(args[1]).is_absolute() or Path(args[1]).name != "managed_core.py"
            or not Path(args[1]).is_file() or args[2] != "mcp"
            or args[3] != "--runtime-root" or args[5] != "--state-dir" or args[7] != "--project-id"
            or args[8] != project_id or any(not isinstance(v, str) or "\x00" in v for v in args)
            or not Path(args[4]).is_absolute() or not Path(args[6]).is_absolute()):
        raise setup.SetupError("invalid_managed_command", "Managed MCP requires the exact native project-scoped command.")
    if client in JSON_CLIENTS:
        return {"type": "local", "command": [command["command"], *command["args"]], "enabled": True, "timeout": 30000}
    return {**command, **({"type": "stdio"} if client == "claude" else {})}


def flatpak_project_launcher(root, client, command):
    """Same reviewed config works inside the app and from a host terminal."""
    app_id = os.environ.get("FLATPAK_ID")
    if not app_id or client == "triggerfish":
        return None
    if (not FLATPAK_PROFILE.is_file() or FLATPAK_PROFILE.stat().st_size > 65536
            or any(path.is_symlink() for path in (FLATPAK_PROFILE, *FLATPAK_PROFILE.parents))):
        raise setup.SetupError("flatpak_profile_invalid", "Packaged Flatpak identity is unavailable or unsafe.")
    profile = setup.parse_json(FLATPAK_PROFILE.read_bytes())
    if profile.get("app_id") != app_id or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){2,}", app_id):
        raise setup.SetupError("flatpak_profile_invalid", "Packaged Flatpak identity is missing or differs; no host launcher was configured.")
    args = command["args"]
    state_dir = args[args.index("--state-dir") + 1]
    project_id = args[args.index("--project-id") + 1]
    native = shlex.join([command["command"], *args])
    host = shlex.join(["flatpak", "run", "--command=modelbuilder-mcp", app_id,
                      "--state-dir", state_dir, "--project-id", project_id])
    relative = f".modelbuilder/assistant-setup/{client}-mcp.sh"
    text = '#!/bin/sh\nset -eu\nif [ "${FLATPAK_ID:-}" = ' + shlex.quote(app_id) + ' ]; then\n  exec ' + native + '\nelse\n  exec ' + host + '\nfi\n'
    vector = {"command": "/bin/sh", "args": [str(checked_path(root, relative))]}
    entry = ({"type": "local", "command": [vector["command"], *vector["args"]], "enabled": True, "timeout": 30000}
             if client in JSON_CLIENTS else {**vector, **({"type": "stdio"} if client == "claude" else {})})
    return relative, text, entry


def config_entry(client, raw):
    try:
        data = setup.parse_json(raw or b"{}") if client == "claude" or client in JSON_CLIENTS else tomllib.loads((raw or b"").decode())
        servers = data.get("mcp" if client in JSON_CLIENTS else "mcpServers" if client == "claude" else "mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError("servers")
        return servers.get("modelbuilder")
    except (ValueError, UnicodeError):
        raise setup.SetupError("unsupported_config", "The selected project MCP config cannot be safely edited.") from None


def template_files(client):
    base = Path(__file__).with_name("assistant_templates") / f"v{VERSION}"
    skill = SKILL_ROOTS[client] + "/modelbuilder-project"
    files = {f"{skill}/SKILL.md": (base / "SKILL.md").read_bytes(),
            f"{skill}/references/instructions.md": (base / "instructions.md").read_bytes(),
            f"{skill}/references/operation-catalog.json": (base / "operation-catalog.json").read_bytes()}
    for path in sorted((base / "skills").rglob("*")):
        if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            files[SKILL_ROOTS[client] + "/" + path.relative_to(base / "skills").as_posix()] = path.read_bytes()
    return files


def instruction_block(client):
    skill = SKILL_ROOTS[client] + "/modelbuilder-project"
    # Appended without normalizing/reformatting existing instructions. Never touch
    # AGENTS.override.md, nested files or the .claude/CLAUDE.md hierarchy.
    return (f"\n<!-- modelbuilder:{client}:v{VERSION}:begin -->\n"
            f"ModelBuilder project workflow: read {skill}/SKILL.md and its references.\n"
            "This integration grants no permissions. Existing owner instructions and override scopes remain in force.\n"
            f"<!-- modelbuilder:{client}:v{VERSION}:end -->\n").encode()


def preview_project(client, workspace, project_id, *, command=None, python=None, service_url=None):
    if client not in CLIENTS:
        raise setup.SetupError("capability_unavailable", "This assistant has no supported project preparation path.")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", project_id or ""):
        raise setup.SetupError("project_required", "Create or select one model project before configuring its assistant.")
    root = checked_root(workspace)
    launcher = None
    if command is not None:
        entry = entry_from_command(client, command, project_id)
        launcher = flatpak_project_launcher(root, client, command)
        if launcher:
            entry = launcher[2]
    else:
        if client == "triggerfish" or client in JSON_CLIENTS:
            raise setup.SetupError("managed_runtime_required", "This harness requires the native project-scoped managed MCP command; external token interpolation is not configured.")
        if not python or not service_url:
            raise setup.SetupError("runtime_required", "Choose the native managed runtime, or an explicit external service interpreter and URL.")
        python = setup.interpreter_path(python)
        setup.inspect_interpreter(python)
        entry = setup.server_entry(client, python, setup.validate_url(service_url))
    if client == "triggerfish":
        verify_triggerfish_profile(entry)
        entry = {"command": entry["command"], "args": [*entry["args"], "--mcp-profile", "triggerfish"], "enabled": False}
    receipt, receipt_raw = load_receipt(root, client)
    current = receipt if receipt and receipt["state"] != "rolled_back" else None
    if current:
        if (current["state"] != "applied" or current.get("project_id") != project_id
                or current.get("entry") != entry or current.get("template_version") != VERSION):
            raise setup.SetupError("receipt_conflict", "A different or interrupted managed setup exists. Review rollback before replacing it.")
        for item in current["files"]:
            if hash_bytes(setup.config_bytes(root / item["path"])) != item["after_sha256"]:
                raise setup.SetupError("managed_file_changed", "A managed file changed since setup. Review rollback before reconfiguring; edits are preserved.")
    if client == "triggerfish":
        return preview_triggerfish(root, project_id, entry, current, receipt_raw)
    config = CONFIG_PATHS[client]
    if client in JSON_CLIENTS:
        alternatives = ["opencode.jsonc", ".opencode/opencode.json", ".opencode/opencode.jsonc"] if client == "opencode" else ["kilo.json", "kilo.jsonc", ".kilo/kilo.jsonc", ".kilocode/mcp.json"]
        if any(setup.config_bytes(checked_path(root, path)) is not None for path in alternatives):
            raise setup.SetupError("config_conflict", "An alternate or legacy harness config exists. Review precedence in the vendor UI; no competing config was created.")
    instructions = "CLAUDE.md" if client == "claude" else "AGENTS.md"
    owned_templates = template_files(client)
    if launcher:
        owned_templates[launcher[0]] = launcher[1].encode()
    originals = {}
    for path in [config, instructions, *owned_templates]:
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
    for path, content in owned_templates.items():
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



def verify_triggerfish_profile(entry):
    """Read a capability declaration as data; never execute caller-supplied code.

    This compatibility check is not executable trust. The native runtime seal
    and packaging lifecycle hashes remain authoritative before any launch.
    """
    args = entry["args"]
    script = args[2] if args[:2] == ["-I", "-B"] else args[1]
    try:
        raw = setup.config_bytes(Path(script))
        tree = ast.parse(raw.decode("utf-8") if raw is not None else "")
        supported = any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "parser"
            and node.func.attr == "add_argument" and node.args
            and isinstance(node.args[0], ast.Constant) and node.args[0].value == "--mcp-profile"
            and any(keyword.arg == "choices" and isinstance(keyword.value, (ast.List, ast.Tuple))
                    and all(isinstance(value, ast.Constant) for value in keyword.value.elts)
                    and [value.value for value in keyword.value.elts] == ["native", "triggerfish"]
                    for keyword in node.keywords)
            for node in ast.walk(tree))
    except (OSError, ValueError, SyntaxError, UnicodeError, RecursionError):
        supported = False
    if not supported:
        raise setup.SetupError("managed_runtime_stale", "This managed runtime does not declare the Triggerfish MCP profile. Update/rebuild and reseal the ModelBuilder application before preparing this integration. No executable was invoked or project files changed.")


def preview_triggerfish(root, project_id, entry, current, receipt_raw):
    """Prepare inert, source-scoped artifacts, never a global/active vendor config."""
    base = ".modelbuilder/triggerfish"
    templates = Path(__file__).with_name("assistant_templates") / f"v{VERSION}"
    # JSON is YAML-compatible and safely quotes all paths. This is deliberately
    # a fragment, not triggerfish.yaml: provider/sign-in/classification belong
    # to the owner. enabled:false plus omitted classification fail closed.
    content = {f"{base}/mcp.fragment.yaml": json.dumps({"mcp": {"servers": {"modelbuilder": entry}}}, indent=2) + "\n",
               f"{base}/SETUP.md": (templates / "triggerfish-setup.md").read_text(),
               f"{base}/skill-template/modelbuilder-project/SKILL.md": (templates / "SKILL.md").read_text() + "\n" + (templates / "triggerfish-instructions.md").read_text(),
               f"{base}/skill-template/modelbuilder-project/references/instructions.md": (templates / "instructions.md").read_text(),
               f"{base}/skill-template/modelbuilder-project/references/operation-catalog.json": (templates / "operation-catalog.json").read_text()}
    for path in sorted((templates / "skills").rglob("*")):
        if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            content[f"{base}/skill-template/" + path.relative_to(templates / "skills").as_posix()] = path.read_text()
    files = []
    for path, text in content.items():
        raw = setup.config_bytes(checked_path(root, path))
        if raw is not None and not current:
            raise setup.SetupError("config_conflict", "An existing Triggerfish preparation file was preserved. Review it before managed preparation.")
        files.append({"path": path, "kind": "owned", "content": text,
                      "before_sha256": hash_bytes(raw), "after_sha256": setup.digest(text.encode()), "created": raw is None})
    basename = entry["command"].replace("\\", "/").rsplit("/", 1)[-1]
    allowed = basename in {"npx", "node", "python3", "python", "deno", "uvx"}
    warnings = [
        "Inactive preparation only: this fragment is not loaded by Triggerfish. No vendor is installed, launched, signed in or configured globally.",
        "Upstream hardcodes gateway/CLI port 18789. Concurrent per-project daemons are unsupported; ModelBuilder cannot safely launch or join one automatically.",
        "The owner must review provider authentication, data classification, skill classification and activation in a separately installed Triggerfish. No classification is chosen here.",
        "The managed service is read-only by default; exact one-use native approval is required for mutations. A same-user unrestricted shell is not a security sandbox."]
    if not allowed:
        warnings.append("The managed interpreter basename is rejected by upstream's MCP allowlist. Config startup does not forward allowedCommands. Do not invent an override or substitute an unverified executable.")
    plan = {"action": "configure-project", "client": "triggerfish", "project_id": project_id,
            "workspace": str(root), "scope": "project", "template_version": VERSION,
            "entry": entry, "files": [{k: v for k, v in item.items() if k != "content"} for item in files],
            "receipt_before_sha256": hash_bytes(receipt_raw), "change": "unchanged" if current else "add",
            "warnings": warnings, "credentials_stored": False, "capability": "inactive_compatibility_bundle",
            "mcp_profile": "triggerfish", "upstream_commit": "571c42b9299ab3006ce96fb541b7f1a23988d3b5",
            "latest_release_observed": "v0.7.19", "upstream_checked_at": "2026-10-13", "vendor_acceptance": "UNVERIFIED",
            "activation": "blocked_pending_owner_vendor_setup_and_port_isolation",
            "command_allowlist": "compatible_basename" if allowed else "blocked_basename",
            "skill_template_directory": str(root / base / "skill-template"),
            "notice": "Review the exact project-only inactive files. TRIGGERFISH_DATA_DIR is supported upstream, but does not isolate its fixed ports. No active config, provider, daemon, global plugin or port is changed."}
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
    return {"ok": True, "status": "prepared" if plan["client"] == "triggerfish" else "configured",
            "capability": plan.get("capability"), "receipt": str(root / receipt_name(plan["client"])),
            "files": [item["path"] for item in files], "credentials_stored": False,
            "authentication": "UNVERIFIED", "transport": "UNVERIFIED", "warnings": plan["warnings"]}


def remove_entry(client, raw, expected):
    if config_entry(client, raw) != expected:
        raise setup.SetupError("managed_file_changed", "The managed MCP entry changed. Rollback will not remove an edited entry.")
    if client == "claude" or client in JSON_CLIENTS:
        data = setup.parse_json(raw)
        key = "mcp" if client in JSON_CLIENTS else "mcpServers"
        data[key].pop("modelbuilder")
        if not data[key]:
            data.pop(key)
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
