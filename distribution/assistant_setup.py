#!/usr/bin/env python3
"""Explicit optional assistant installation and credential-free MCP setup.

This standalone helper is also copied into the native feature's resources. Every
command returns one JSON object. Preview commands do not write files. An apply
requires the exact preview digest; never call it automatically at IDE startup.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

CLIENTS = {
    "codex": {
        "vendor": "OpenAI", "id": "openai.chatgpt", "version": "26.5908.31748",
        "namespace": "openai", "name": "chatgpt", "binary": "codex",
        "terms": ["https://openai.com/policies/row-terms-of-use/"],
        "documentation": "https://developers.openai.com/codex/ide",
        "mcp_documentation": "https://developers.openai.com/codex/mcp",
    },
    "claude": {
        "vendor": "Anthropic", "id": "Anthropic.claude-code", "version": "2.1.295",
        "namespace": "Anthropic", "name": "claude-code", "binary": "claude",
        "terms": ["https://code.claude.com/docs/en/legal-and-compliance"],
        "documentation": "https://code.claude.com/docs/en/vs-code",
        "mcp_documentation": "https://code.claude.com/docs/en/mcp",
    },
}
NATIVE_TARGETS = frozenset({"linux-x64", "linux-arm64", "linux-armhf", "darwin-x64", "darwin-arm64",
                            "win32-x64", "win32-arm64"})
MAX_CONFIG = 8 * 1024 * 1024
MAX_ARCHIVE = 512 * 1024 * 1024
TOKEN_ENV = "MODELBUILDER_TOKEN"
MCP_ARGS = ["-I", "-m", "modelbuilder.mcp_server"]


class SetupError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def digest(value: bytes | dict) -> str:
    if isinstance(value, dict):
        value = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(value).hexdigest()


def absolute(path: str | Path) -> Path:
    # Do not resolve symlinks here: configuration writes reject them explicitly.
    return Path(os.path.abspath(Path(path).expanduser()))


def host_target() -> str:
    system = {"Linux": "linux", "Darwin": "darwin", "Windows": "win32"}.get(platform.system())
    arch = {"x86_64": "x64", "AMD64": "x64", "amd64": "x64", "aarch64": "arm64",
            "arm64": "arm64", "ARM64": "arm64", "armv7l": "armhf"}.get(platform.machine())
    target = f"{system}-{arch}"
    if target not in NATIVE_TARGETS:
        raise SetupError("native_host_required", "This helper requires a supported native desktop host.")
    return target


def validate_url(value: str) -> str:
    try:
        url = urllib.parse.urlsplit(value)
        _ = url.port
    except ValueError:
        raise SetupError("invalid_service_url", "Enter a valid HTTP(S) service base URL.") from None
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment
            or any(ord(ch) < 33 for ch in value) or "${" in value):
        raise SetupError("invalid_service_url", "Service URL must not contain credentials, query, or fragment.")
    if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SetupError("https_required", "Non-loopback operation services require HTTPS.")
    return value.rstrip("/")


def registry_url(url: str, prefix: str | None = None) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "open-vsx.org" or parsed.port not in (None, 443)
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
            or any(ord(ch) < 33 for ch in url) or not parsed.path.startswith("/api/")
            or (prefix and not url.startswith(prefix))):
        raise SetupError("untrusted_source", "The artifact must come from the selected Open VSX vendor entry.")
    return url


class RegistryRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        registry_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def registry_read(url: str, limit: int) -> bytes:
    registry_url(url)
    try:
        opener = urllib.request.build_opener(RegistryRedirect())
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "Modelbuilder-Assistant-Setup/1"}),
                         timeout=60) as response:
            registry_url(response.url)
            data = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise SetupError("artifact_unavailable", "This exact version/target is absent from Open VSX; "
                             "this is a channel gap, not an unsupported operating system.") from None
        raise SetupError("registry_unavailable", "Open VSX could not supply this artifact's metadata.") from None
    except (OSError, urllib.error.URLError):
        raise SetupError("registry_unavailable", "Open VSX could not be reached.") from None
    if len(data) > limit:
        raise SetupError("oversized_download", "Registry response exceeds the size limit.")
    return data


def installed_extensions(directory: Path | None, client: str) -> list[dict]:
    found = []
    if not directory or not directory.is_dir():
        return found
    for entry in directory.iterdir():
        package = entry / "package.json"
        try:
            if package.stat().st_size > MAX_CONFIG:
                continue
            data = json.loads(package.read_text(encoding="utf-8"))
            identity = f"{data.get('publisher', '')}.{data.get('name', '')}"
            if identity.lower() != CLIENTS[client]["id"].lower():
                continue
            name = CLIENTS[client]["binary"] + (".exe" if os.name == "nt" else "")
            subtree = entry / ("bin" if client == "codex" else "resources/native-binary")
            binaries = [str(p.absolute()) for p in subtree.rglob(name) if p.is_file()]
            found.append({"id": identity, "version": data.get("version"), "directory": str(entry.absolute()),
                          "binaries": sorted(binaries)})
        except (ValueError, OSError, AttributeError):
            continue
    return sorted(found, key=lambda item: item["directory"])


def discover_client(client: str, extensions: Path | None, explicit: str | None = None) -> dict:
    if explicit:
        path = absolute(explicit)
        if not path.is_file():
            raise SetupError("missing_client", "The selected vendor executable does not exist.")
        return {"status": "found", "source": "explicit", "path": str(path)}
    packages = installed_extensions(extensions, client)
    binaries = sorted({p for item in packages for p in item["binaries"]})
    if len(binaries) > 1:
        return {"status": "ambiguous", "message": "Choose --client-binary; multiple vendor binaries are installed."}
    if binaries:
        return {"status": "found", "source": "installed_extension", "path": binaries[0]}
    path = shutil.which(CLIENTS[client]["binary"])
    return {"status": "found", "source": "PATH", "path": str(absolute(path))} if path else {"status": "absent"}


def catalog(extensions: Path | None = None) -> dict:
    return {"ok": True, "host_target": host_target(), "assistants": [
        {"client": name, **spec, "bundled": False,
         "installed_extensions": installed_extensions(extensions, name),
         "client_discovery": discover_client(name, extensions),
         "channel_availability": "Check exact version/target with preview-install; never infer OS support.",
         "authenticated_agent_acceptance": "UNVERIFIED"} for name, spec in CLIENTS.items()]}


def preview_install(client: str, version: str | None, ide_cli: Path, extensions: Path | None,
                    user_data: Path | None, target: str | None = None) -> dict:
    spec = CLIENTS[client]
    version = version or spec["version"]
    target = target or host_target()
    if not re.fullmatch(r"[0-9][A-Za-z0-9_.+-]{0,99}", version):
        raise SetupError("exact_version_required", "Select an exact vendor version, never latest.")
    if target not in NATIVE_TARGETS or target != host_target():
        raise SetupError("target_mismatch", "Installation must target this native host.")
    ide_cli = absolute(ide_cli)
    if not ide_cli.is_file():
        raise SetupError("missing_ide_cli", "Select the generated modelbuilder-ide.py launcher or native IDE CLI.")
    if ide_cli.suffix.lower() in {".cmd", ".bat", ".ps1", ".sh"}:
        raise SetupError("unsafe_cli_wrapper", "Select modelbuilder-ide.py or the native IDE executable, not a shell wrapper.")
    base = f"https://open-vsx.org/api/{spec['namespace']}/{spec['name']}/{target}/{version}"
    try:
        metadata = json.loads(registry_read(base, MAX_CONFIG))
        if (metadata.get("namespace", "").lower() != spec["namespace"].lower()
                or metadata.get("name") != spec["name"] or metadata.get("version") != version
                or metadata.get("targetPlatform") != target or metadata.get("verified") is not True):
            raise SetupError("registry_identity_mismatch", "Registry vendor/version/target verification failed.")
        url = registry_url(metadata["files"]["download"], base + "/file/")
        sha_url = registry_url(metadata["files"]["sha256"], base + "/file/")
        checksum = registry_read(sha_url, 4096).decode("ascii").strip().split()[0].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("checksum")
    except (KeyError, ValueError, UnicodeError, IndexError) as error:
        if isinstance(error, SetupError):
            raise
        raise SetupError("invalid_registry_metadata", "Registry metadata or checksum is invalid.") from None
    plan = {"action": "install", "client": client, "vendor": spec["vendor"], "extension_id": spec["id"],
            "version": version, "target": target, "source": url, "sha256": checksum,
            "metadata_source": base, "checksum_source": sha_url, "terms": spec["terms"],
            "ide_cli": str(ide_cli), "cli_python": sys.executable if ide_cli.suffix.lower() == ".py" else None,
            "extensions_dir": str(absolute(extensions)) if extensions else None,
            "user_data_dir": str(absolute(user_data)) if user_data else None,
            "notice": "Separate user installation under the vendor terms. No redistribution rights granted. "
                      "Vendor sign-in and paid use remain separate. Exact-fork activation is not guaranteed."}
    return {"ok": True, "plan": plan, "approval": digest(plan)}


def verify_archive(path: Path, plan: dict) -> None:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    if hasher.hexdigest() != plan["sha256"]:
        raise SetupError("hash_mismatch", "Downloaded VSIX does not match the approved SHA256.")
    try:
        with zipfile.ZipFile(path) as archive:
            for name in ("extension/package.json", "extension.vsixmanifest"):
                if archive.namelist().count(name) != 1 or archive.getinfo(name).file_size > MAX_CONFIG:
                    raise ValueError("manifest size/duplicate")
            package = json.loads(archive.read("extension/package.json"))
            identity = f"{package['publisher']}.{package['name']}"
            if identity.lower() != plan["extension_id"].lower() or package["version"] != plan["version"]:
                raise ValueError("identity")
            root = ET.fromstring(archive.read("extension.vsixmanifest"))
            identities = [e for e in root.iter() if e.tag.split("}")[-1] == "Identity"]
            if len(identities) != 1 or identities[0].get("TargetPlatform", "universal") != plan["target"]:
                raise ValueError("target")
    except (KeyError, ValueError, ET.ParseError, zipfile.BadZipFile):
        raise SetupError("invalid_vsix", "VSIX embedded identity/version/platform does not match the preview.") from None


def require_approval(plan: dict, approval: str | None) -> None:
    if approval != digest(plan):
        raise SetupError("approval_required", "Review a fresh preview and pass its exact approval digest.")


def safe_environment() -> dict[str, str]:
    # No vendor/service credentials are inherited by installers or executable checks.
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "HOME",
               "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "DISPLAY",
               "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "LANG", "LC_ALL", "LC_CTYPE"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def install(plan: dict, approval: str | None) -> dict:
    require_approval(plan, approval)
    # The entire artifact is discarded after the explicitly requested install.
    with tempfile.TemporaryDirectory(prefix="modelbuilder-assistant-install-") as directory:
        path = Path(directory) / "assistant.vsix"
        path.write_bytes(registry_read(plan["source"], MAX_ARCHIVE))
        verify_archive(path, plan)
        command = ([plan["cli_python"], plan["ide_cli"]] if plan["cli_python"] else [plan["ide_cli"]])
        profile = []
        for flag, key in (("--extensions-dir", "extensions_dir"), ("--user-data-dir", "user_data_dir")):
            if plan[key] is not None:
                profile.extend([flag, plan[key]])
        result = subprocess.run([*command, *profile, "--install-extension", str(path),
                                 "--do-not-include-pack-dependencies", "--force"],
                                env=safe_environment(), capture_output=True, timeout=180, check=False)
        if result.returncode != 0:
            raise SetupError("install_failed", "IDE CLI did not complete installation; no sign-in was attempted.")
        check = subprocess.run([*command, *profile, "--list-extensions", "--show-versions"],
                               env=safe_environment(), capture_output=True, timeout=60, check=False)
        expected = f"{plan['extension_id']}@{plan['version']}".lower()
        lines = check.stdout.decode("utf-8", errors="replace").lower().splitlines()
        if check.returncode or expected not in [line.strip() for line in lines]:
            raise SetupError("install_unverified", "IDE CLI returned success but exact installed version is unverified.")
    return {"ok": True, "status": "installed", "extension_id": plan["extension_id"], "version": plan["version"],
            "sha256": plan["sha256"], "terms": plan["terms"], "activation": "UNVERIFIED",
            "next_step": "Reload the IDE. Sign in yourself if you choose; MCP configuration is a separate action."}


def config_path(client: str, override: Path | None, workspace: Path | None) -> Path:
    if override:
        return absolute(override)
    if not workspace:
        raise SetupError("workspace_required", "Select an explicit local project --workspace. Global configuration is never the default.")
    return absolute(workspace / (".codex/config.toml" if client == "codex" else ".mcp.json"))


def config_bytes(path: Path) -> bytes | None:
    # Refuse redirects through any symlink; do not write via links or hard links.
    for node in (path, *path.parents):
        if node.is_symlink():
            raise SetupError("unsafe_config_path", "Configuration path must not contain symbolic links.")
    if not path.exists():
        return None
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > MAX_CONFIG:
        raise SetupError("unsafe_config_path", "Configuration must be a regular, single-link file below 8 MiB.")
    return path.read_bytes()


def interpreter_path(value: str) -> str:
    path = absolute(value)
    if not path.is_file() or "${" in str(path):
        raise SetupError("missing_interpreter", "Choose an existing absolute service Python interpreter.")
    return str(path)



def inspect_interpreter(python: str) -> dict:
    code = ("import importlib.util,json,sys; "
            "spec=importlib.util.find_spec('modelbuilder.mcp_server'); "
            "print(json.dumps({'python':sys.executable,'module_file':spec.origin if spec else None}))")
    result = subprocess.run([python, "-I", "-B", "-c", code], env=safe_environment(),
                            capture_output=True, timeout=15, check=False)
    try:
        runtime = json.loads(result.stdout)
        if result.returncode or not isinstance(runtime.get("module_file"), str):
            raise ValueError("missing module")
        return {"python": python, "module_file": runtime["module_file"]}
    except (ValueError, AttributeError):
        raise SetupError("missing_modelbuilder", "This interpreter cannot locate the installed native MCP module. "
                         "Install the Modelbuilder service or choose its actual Python environment.") from None


def server_entry(client: str, python: str, service_url: str) -> dict:
    base = {"command": python, "args": MCP_ARGS[:], "env": {"MODELBUILDER_API_URL": service_url}}
    if client == "claude":
        base["type"] = "stdio"
        base["env"][TOKEN_ENV] = "${" + TOKEN_ENV + "}"
    else:
        base["env_vars"] = [TOKEN_ENV]
    return base


def parse_json(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("Configuration must be an object")
    return value


def without_server(config: dict, key: str) -> dict:
    result = copy.deepcopy(config)
    servers = result.get(key, {})
    if not isinstance(servers, dict):
        raise ValueError("MCP servers must be a table/object")
    servers.pop("modelbuilder", None)
    if not servers:
        result.pop(key, None)
    return result


def toml_section(header: str) -> tuple[str, ...] | None:
    try:
        tree = tomllib.loads(header)
    except tomllib.TOMLDecodeError:
        return None
    path = []
    while isinstance(tree, dict) and len(tree) == 1:
        key, tree = next(iter(tree.items()))
        path.append(key)
    return tuple(path) if tree == {} else None


def render_codex(raw: bytes, entry: dict) -> bytes:
    text = raw.decode("utf-8")
    original = tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    # Only conventional tables are rewritten. Validate semantic preservation
    # afterward, so inline/dotted declarations or lookalike multiline strings
    # can never silently damage another entry. Unsupported layouts fail closed.
    headers = [(i, toml_section(line.strip())) for i, line in enumerate(lines) if line.lstrip().startswith("[")]
    headers = [(i, path) for i, path in headers if path is not None]
    remove = set()
    for index, (start, path) in enumerate(headers):
        if path[:2] == ("mcp_servers", "modelbuilder"):
            end = headers[index + 1][0] if index + 1 < len(headers) else len(lines)
            remove.update(range(start, end))
    preserved = "".join(line for i, line in enumerate(lines) if i not in remove)
    j = json.dumps
    block = "\n[mcp_servers.modelbuilder]\ncommand = " + j(entry["command"]) + "\nargs = " + j(entry["args"]) + "\n"
    if entry.get("env_vars"):
        block += "env_vars = " + j(entry["env_vars"]) + "\n"
    if entry.get("env"):
        block += "[mcp_servers.modelbuilder.env]\n"
        block += "".join(j(key) + " = " + j(value) + "\n" for key, value in entry["env"].items())
    candidate = preserved.rstrip("\r\n") + "\n" + block
    parsed = tomllib.loads(candidate)
    if (without_server(parsed, "mcp_servers") != without_server(original, "mcp_servers")
            or parsed["mcp_servers"]["modelbuilder"] != entry):
        raise ValueError("Semantic preservation failed")
    return candidate.encode("utf-8")


def render_config(client: str, raw: bytes | None, entry: dict) -> tuple[bytes, str]:
    try:
        if client == "claude":
            data = parse_json(raw or b"{}")
            servers = data.setdefault("mcpServers", {})
            if not isinstance(servers, dict):
                raise ValueError("MCP servers must be an object")
            old = servers.get("modelbuilder")
            servers["modelbuilder"] = entry
            updated = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()
        else:
            data = tomllib.loads((raw or b"").decode("utf-8"))
            old = data.get("mcp_servers", {}).get("modelbuilder")
            updated = render_codex(raw or b"", entry)
        action = "unchanged" if old == entry else "replace" if old is not None else "add"
        return (raw if action == "unchanged" and raw is not None else updated), action
    except (ValueError, UnicodeError, AttributeError, TypeError):
        raise SetupError("unsupported_config", "Configuration is malformed, contains duplicate keys, or uses a TOML "
                         "layout that cannot be safely edited. Use the vendor settings UI; no file was changed.") from None


def preview_config(client: str, python: str, service_url: str, path: Path) -> dict:
    python = interpreter_path(python)
    service_url = validate_url(service_url)
    path = absolute(path)
    runtime = inspect_interpreter(python)
    raw = config_bytes(path)
    entry = server_entry(client, python, service_url)
    _, action = render_config(client, raw, entry)
    plan = {"action": "configure", "client": client, "config_path": str(path), "server": "modelbuilder",
            "change": action, "before_sha256": digest(raw) if raw is not None else None, "entry": entry,
            "service_url": service_url, "runtime": runtime, "credential": {"mechanism": "environment_reference", "name": TOKEN_ENV,
            "stored": False}, "scope": "project" if client == "claude" else "selected Codex config",
            "notice": "The assistant can use the operations permitted by your service credential. Provide a scoped "
                      "MODELBUILDER_TOKEN in the client process environment yourself, then restart that client. "
                      "This setup never reads IDE SecretStorage, saves a token, signs in, or submits model prompts. "
                      "Close vendor clients before applying to avoid concurrent configuration writes. "
                      "An existing modelbuilder entry is replaced only after this exact preview is approved."}
    return {"ok": True, "plan": plan, "approval": digest(plan)}


def private_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def configure(plan: dict, approval: str | None) -> dict:
    require_approval(plan, approval)
    path = Path(plan["config_path"])
    raw = config_bytes(path)
    if (digest(raw) if raw is not None else None) != plan["before_sha256"]:
        raise SetupError("config_changed", "Configuration changed since preview. Review it again before applying.")
    updated, action = render_config(plan["client"], raw, plan["entry"])
    if action == "unchanged":
        return {"ok": True, "status": "unchanged", "config_path": str(path), "backup": None}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = path.with_name(path.name + ".modelbuilder.lock")
    try:
        lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SetupError("config_busy", "Another setup owns the configuration lock. Retry after it finishes.") from None
    backup = None
    temp = None
    try:
        os.close(lock_fd)
        if config_bytes(path) != raw:
            raise SetupError("config_changed", "Configuration changed since preview. No file was overwritten.")
        if raw is not None:
            backup = path.with_name(f"{path.name}.modelbuilder-backup-{time.time_ns()}")
            private_file(backup, raw)
        fd, name = tempfile.mkstemp(prefix=".modelbuilder-config-", dir=path.parent)
        temp = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        if config_bytes(path) != raw:
            raise SetupError("config_changed", "Configuration changed during setup. No file was overwritten.")
        os.replace(temp, path)
        temp = None
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if temp:
            temp.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
    return {"ok": True, "status": "configured", "config_path": str(path),
            "backup": str(backup) if backup else None, "credential_stored": False,
            "next_step": "Provide MODELBUILDER_TOKEN in the assistant's environment yourself and restart it. "
                         "Run doctor to check transport; vendor sign-in is separate."}


def process_lines(stream):
    messages = queue.Queue()
    def read():
        try:
            for line in stream:
                if len(line) > MAX_CONFIG:
                    messages.put(None)
                    break
                messages.put(line)
        finally:
            messages.put(None)
    threading.Thread(target=read, daemon=True).start()
    return messages


class JsonProcess:
    def __init__(self, command: list[str], env: dict, cwd: Path | None = None):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env, cwd=cwd)
        self.messages = process_lines(self.process.stdout)
        self.sequence = 0

    def notify(self, method: str):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict, timeout=25):
        self.sequence += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.sequence,
                                            "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                break
            try:
                response = json.loads(line)
            except ValueError:
                continue
            if isinstance(response, dict) and response.get("id") == self.sequence:
                if "error" in response:
                    raise SetupError("transport_failed", "MCP discovery returned an error; no model prompt was sent.")
                return response.get("result", {})
        raise SetupError("transport_failed", "MCP discovery did not finish; no model prompt was sent.")

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()


def native_probe(python: str, service_url: str, token: str) -> dict:
    env = safe_environment()
    env.update(MODELBUILDER_API_URL=service_url, MODELBUILDER_TOKEN=token)
    process = JsonProcess([python, *MCP_ARGS], env)
    try:
        process.request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "modelbuilder-readiness", "version": "1"}})
        process.notify("notifications/initialized")
        tools = process.request("tools/list", {}).get("tools")
        resources = process.request("resources/list", {}).get("resources")
        if not isinstance(tools, list) or not isinstance(resources, list) or not tools:
            raise SetupError("transport_failed", "Native MCP returned no valid tool inventory.")
        return {"status": "ready", "tool_count": len(tools), "resource_count": len(resources),
                "tool_calls": 0, "model_prompts": 0}
    finally:
        process.close()


def vendor_probe(client: str, binary: str, python: str, service_url: str, token: str) -> dict:
    # Never load the user's vendor configuration, account, history, hooks, or auth stores.
    with tempfile.TemporaryDirectory(prefix="modelbuilder-vendor-readiness-") as directory:
        home = Path(directory)
        env = safe_environment()
        env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "appdata"),
                   LOCALAPPDATA=str(home / "localappdata"), XDG_CONFIG_HOME=str(home / "config"),
                   CODEX_HOME=str(home / "codex"), CLAUDE_CONFIG_DIR=str(home / "claude"),
                   MODELBUILDER_API_URL=service_url, MODELBUILDER_TOKEN=token)
        for name in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
            Path(env[name]).mkdir()
        workspace = home / "workspace"
        workspace.mkdir()
        entry = server_entry(client, python, service_url)
        if client == "claude":
            config = workspace / ".mcp.json"
            config.write_bytes(render_config(client, None, entry)[0])
            result = subprocess.run([binary, "mcp", "list"], cwd=workspace, env=env,
                                    capture_output=True, timeout=45, check=False)
            # Only a status is returned: CLI output can contain expanded env values.
            output = result.stdout.decode("utf-8", errors="replace")
            ready = result.returncode == 0 and any("modelbuilder" in line and "Connected" in line
                                                   for line in output.splitlines())
            return {"status": "ready" if ready else "unverified", "transport": "vendor_mcp_list",
                    "tool_calls": 0, "model_prompts": 0}
        config = home / "codex" / "config.toml"
        config.write_bytes(render_config(client, None, entry)[0])
        process = JsonProcess([binary, "app-server", "--stdio"], env, workspace)
        try:
            process.request("initialize", {"clientInfo": {"name": "modelbuilder-readiness", "version": "1"},
                                            "capabilities": {"experimentalApi": True}})
            process.notify("initialized")
            inventory = process.request("mcpServerStatus/list", {})
            servers = inventory.get("data", [])
            matches = [server for server in servers if server.get("name") == "modelbuilder"]
            count = len(matches[0].get("tools", {})) if len(matches) == 1 else 0
            return {"status": "ready" if count else "unverified", "transport": "vendor_app_server",
                    "tool_count": count, "tool_calls": 0, "model_prompts": 0}
        finally:
            process.close()


def doctor(client: str, python: str, service_url: str, extensions: Path | None,
           client_binary: str | None = None, probe_client=False) -> dict:
    python = interpreter_path(python)
    service_url = validate_url(service_url)
    client_info = discover_client(client, extensions, client_binary)
    result = {"ok": True, "client": client, "python": python, "service_url": service_url,
              "vendor_client": client_info, "credential": "not_read", "authenticated_agent_acceptance": "UNVERIFIED"}
    check = subprocess.run([python, "-I", "-c", "import importlib.util; "
                            "raise SystemExit(0 if importlib.util.find_spec('modelbuilder.mcp_server') else 1)"],
                           env=safe_environment(), capture_output=True, timeout=15, check=False)
    if check.returncode:
        result.update(status="blocked", interpreter="missing_modelbuilder", native_mcp={"status": "not_run"})
        return result
    result["interpreter"] = "ready"
    token = os.environ.get(TOKEN_ENV, "")
    if not token.strip() or token.startswith("${"):
        result.update(status="blocked", credential="missing_environment_variable", native_mcp={"status": "not_run"},
                      next_step="Supply a scoped MODELBUILDER_TOKEN in the client environment yourself; "
                                "the setup helper does not save or issue credentials.")
        return result
    result["credential"] = "present_in_environment_not_saved"
    try:
        result["native_mcp"] = native_probe(python, service_url, token)
    except (SetupError, OSError, subprocess.SubprocessError):
        result["native_mcp"] = {"status": "failed", "message": "Check that the service is running and the "
                                "environment credential has read permission for its catalog."}
    result["status"] = "blocked" if result["native_mcp"]["status"] != "ready" else "native_ready_vendor_unverified"
    if probe_client and client_info["status"] == "found":
        try:
            result["vendor_mcp"] = vendor_probe(client, client_info["path"], python, service_url, token)
        except (SetupError, OSError, subprocess.SubprocessError):
            result["vendor_mcp"] = {"status": "failed", "message": "Isolated vendor discovery failed; no login "
                                    "or model inference was attempted."}
    else:
        result["vendor_mcp"] = {"status": "not_run"}
    if result["native_mcp"]["status"] == "ready":
        if result["vendor_mcp"]["status"] == "ready":
            result["status"] = "transport_ready"
        elif client_info["status"] != "found":
            result["status"] = "native_ready_client_" + client_info["status"]
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    cat = commands.add_parser("catalog")
    cat.add_argument("--extensions-dir", type=Path)
    for action in ("preview-install", "install"):
        cmd = commands.add_parser(action)
        cmd.add_argument("--client", choices=CLIENTS, required=True)
        cmd.add_argument("--version")
        cmd.add_argument("--target")
        cmd.add_argument("--ide-cli", type=Path, required=True)
        cmd.add_argument("--extensions-dir", type=Path)
        cmd.add_argument("--user-data-dir", type=Path)
        if action == "install":
            cmd.add_argument("--approval", required=True)
    for action in ("preview-config", "configure", "doctor"):
        cmd = commands.add_parser(action)
        cmd.add_argument("--client", choices=CLIENTS, required=True)
        cmd.add_argument("--python", required=True)
        cmd.add_argument("--service-url", required=True)
        if action == "doctor":
            cmd.add_argument("--extensions-dir", type=Path)
            cmd.add_argument("--client-binary")
            cmd.add_argument("--probe-client", action="store_true")
        else:
            cmd.add_argument("--config", type=Path)
            cmd.add_argument("--workspace", type=Path)
        if action == "configure":
            cmd.add_argument("--approval", required=True)
    for action in ("preview-project", "apply-project", "preview-rollback", "rollback-project"):
        cmd = commands.add_parser(action)
        cmd.add_argument("--client", choices=[*CLIENTS, "triggerfish"], required=True)
        cmd.add_argument("--workspace", type=Path, required=True)
        if action in {"preview-project", "apply-project"}:
            cmd.add_argument("--project-id", required=True)
            cmd.add_argument("--python")
            cmd.add_argument("--service-url")
            cmd.add_argument("--mcp-command-json")
        if action in {"apply-project", "rollback-project"}:
            cmd.add_argument("--approval", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action in {"preview-project", "apply-project", "preview-rollback", "rollback-project"}:
            import assistant_project
            if args.action in {"preview-project", "apply-project"}:
                command = json.loads(args.mcp_command_json) if args.mcp_command_json else None
                preview, files = assistant_project.preview_project(args.client, args.workspace, args.project_id,
                    command=command, python=args.python, service_url=args.service_url)
                if args.action == "apply-project" and preview["plan"]["change"] == "unchanged":
                    receipt, _ = assistant_project.load_receipt(assistant_project.checked_root(args.workspace), args.client)
                    if receipt.get("approval") == args.approval:
                        result = {"ok": True, "status": "unchanged", "credentials_stored": False}
                    else:
                        result = assistant_project.apply_project(preview, files, args.approval)
                else:
                    result = preview if args.action == "preview-project" else assistant_project.apply_project(preview, files, args.approval)
            else:
                preview, files = assistant_project.preview_rollback(args.client, args.workspace)
                result = preview if args.action == "preview-rollback" else assistant_project.rollback_project(preview, files, args.approval)
        elif args.action == "catalog":
            result = catalog(args.extensions_dir)
        elif args.action in {"preview-install", "install"}:
            preview = preview_install(args.client, args.version, args.ide_cli, args.extensions_dir,
                                      args.user_data_dir, args.target)
            result = preview if args.action == "preview-install" else install(preview["plan"], args.approval)
        elif args.action in {"preview-config", "configure"}:
            preview = preview_config(args.client, args.python, args.service_url,
                                     config_path(args.client, args.config, args.workspace))
            result = preview if args.action == "preview-config" else configure(preview["plan"], args.approval)
        else:
            result = doctor(args.client, args.python, args.service_url, args.extensions_dir,
                            args.client_binary, args.probe_client)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except SetupError as error:
        print(json.dumps({"ok": False, "error": {"code": error.code, "message": str(error)}}))
    except (OSError, subprocess.SubprocessError, ValueError):
        # Never expose arbitrary subprocess/OS text or inherited secret values.
        print(json.dumps({"ok": False, "error": {"code": "setup_failed", "message":
              "The local setup operation could not finish. Check the selected paths and process availability."}}))
    return 2


if __name__ == "__main__":
    # The companion helper must share this exact exception class when run as a script.
    sys.modules["assistant_setup"] = sys.modules[__name__]
    raise SystemExit(main())
