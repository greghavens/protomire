#!/usr/bin/env python3
"""Private, lease-owned desktop core and per-consumer MCP entrypoint.

Never a login daemon, system service, global MCP config or public HTTP listener.
The installation supplies Python; clients supply only an app-private data path.
Session credentials rotate on restart. The database and source checkout persist.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

SESSION = "session.json"
LEASE_TTL = 15.0
IDLE_GRACE = 8.0
START_TIMEOUT = 45.0


class CoreError(ValueError):
    """Credential-free diagnostic suitable for the native workbench."""


def private_directory(path: Path) -> Path:
    """Fail closed on redirected/shared state; never repair an existing ACL."""
    if path.is_symlink():
        raise CoreError("Core data directory must not be a symbolic link")
    if os.name == "nt":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from windows_storage import private_directory as windows_private_directory
        return windows_private_directory(path)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise CoreError("Core data directory must be owned by this user with mode 0700")
    return path.resolve()


def private_read(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise CoreError("Core session file is not a regular file")
        if os.name == "nt":
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from windows_storage import verify_private
            verify_private(path, directory=False)
        elif info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise CoreError("Core session file has unsafe permissions")
        return json.load(handle)


def private_write(path: Path, value: dict) -> None:
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def relative_file(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise CoreError("Runtime manifest contains an invalid relative path")
    candidate = root / name
    if not candidate.resolve().is_relative_to(root.resolve()) or not candidate.is_file():
        raise CoreError("A required bundled runtime file is missing or redirected")
    return candidate


def runtime_manifest(root: Path) -> dict:
    try:
        value = json.loads((root / "runtime.json").read_text())
    except (OSError, ValueError):
        raise CoreError("Bundled core runtime manifest is missing or invalid") from None
    if value.get("schema_version") != 1:
        raise CoreError("Unsupported bundled core runtime version")
    return value


def verify_runtime(root: Path, manifest: dict) -> None:
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise CoreError("Bundled core runtime has no integrity inventory")
    for name, expected in files.items():
        path = relative_file(root, name)
        with path.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
        if actual != expected:
            raise CoreError("Bundled runtime integrity check failed; reinstall the application")


def service_environment() -> dict[str, str]:
    # No Python injection, alternate Modelbuilder configuration, bearer tokens,
    # Git overrides, proxy redirect or inherited assistant credentials.
    allowed = {"PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR", "TMPDIR", "TMP", "TEMP"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def provision_source(root: Path, state: Path, manifest: dict) -> Path | None:
    bundle = manifest.get("source_bundle")
    if not bundle:
        return None
    git = relative_file(root, manifest["git_executable"])
    archive = relative_file(root, bundle)
    workspace = state / "source"
    env = {**service_environment(), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1"}
    # Backend and SourceWorkspaces use git by name. Only this child process PATH
    # changes; no profile, machine environment, or user Git configuration changes.
    os.environ["PATH"] = str(git.parent) + os.pathsep + os.environ.get("PATH", "")
    if not workspace.exists():
        pending = state / (".source-" + secrets.token_hex(8))
        try:
            subprocess.run([str(git), "-c", "core.hooksPath=" + os.devnull, "clone", "--no-hardlinks", "--no-checkout",
                            str(archive), str(pending)], env=env, check=True, capture_output=True, timeout=60)
            subprocess.run([str(git), "-c", "core.hooksPath=" + os.devnull, "-C", str(pending), "checkout", "--detach",
                            manifest["source_commit"]], env=env, check=True, capture_output=True, timeout=60)
            pending.rename(workspace)
        except (subprocess.SubprocessError, OSError):
            import shutil
            shutil.rmtree(pending, ignore_errors=True)
            raise CoreError("Could not initialize the bundled model source workspace") from None
    if workspace.is_symlink() or not workspace.is_dir():
        raise CoreError("The private model source workspace is redirected or unavailable")
    # Never reset or overwrite the user's working tree on restart/update.
    return workspace


def registered_sources(state: Path) -> tuple[dict, dict]:
    path = state / "sources.json"
    if not path.exists():
        return {}, {}
    value = private_read(path)
    if value.get("schema_version") != 1 or not isinstance(value.get("aliases"), dict) or not isinstance(value.get("identities"), dict):
        raise CoreError("Private source registration is invalid")
    if set(value["aliases"]) != set(value["identities"]):
        raise CoreError("Private source registration identities do not match")
    from modelbuilder.source_workspace import SourceWorkspaces
    # Validate every persisted alias through the existing runtime contract.
    SourceWorkspaces(value["aliases"])
    for alias, settings in value["aliases"].items():
        projects = settings.get("project_ids")
        if not isinstance(projects, list) or len(projects) != 1 or not isinstance(projects[0], str):
            raise CoreError("Source registrations must remain scoped to exactly one project")
        if alias != "workspace-" + hashlib.sha256(projects[0].encode()).hexdigest()[:24]:
            raise CoreError("Source registration alias does not match its project")
        identity = value["identities"][alias]
        if not isinstance(identity, str) or not identity.startswith("sha256:") or len(identity) != 71:
            raise CoreError("Source registration identity is invalid")
    return value["aliases"], value["identities"]


def source_workspace_manager(aliases: dict, identities: dict):
    from modelbuilder.source_workspace import SourceWorkspaces
    class RegisteredSourceWorkspaces(SourceWorkspaces):
        def inspect(self, alias, project_id=None):
            receipt = super().inspect(alias, project_id)
            if alias in identities and receipt["repository_id"] != identities[alias]:
                receipt["operator_trusted"] = False
            return receipt
    return RegisteredSourceWorkspaces(aliases)


def load_source_roots(state: Path, defaults: dict) -> dict:
    from modelbuilder.source_workspace import SourceWorkspaces
    from modelbuilder.models import ServiceError
    aliases, identities = registered_sources(state)
    if set(aliases) & set(defaults):
        raise CoreError("Private source aliases conflict with bundled defaults")
    for alias, settings in aliases.items():
        try:
            path = Path(settings["path"])
            if path.is_symlink() or path.resolve() != path:
                raise CoreError("Registered source directory was redirected")
            receipt = SourceWorkspaces({alias: settings}).inspect(alias, settings["project_ids"][0])
            if receipt["repository_id"] != identities[alias]:
                raise CoreError("Registered source checkout identity changed")
        except (OSError, CoreError, ServiceError):
            # Keep the alias visible for diagnosis, but never silently regrant
            # execution trust to a replacement checkout during restart.
            settings["trusted"] = False
    return {**defaults, **aliases}


def make_application(root: Path, state: Path, manifest: dict, token: str):
    from modelbuilder.api import create_app
    from modelbuilder.service import OperationService
    from modelbuilder.auth import TokenAuth
    from modelbuilder.models import Actor
    class DesktopAuth(TokenAuth):
        def __init__(self):
            super().__init__({token: {"id": "desktop-owner", "scopes": ["read", "modify", "execute"]}})
            self.guard = threading.RLock()
            self.bridge_tokens = {}
        def authenticate(self, authorization):
            with self.guard:
                return super().authenticate(authorization)
        def authorize_research(self, actor_id, project_id):
            with self.guard:
                return super().authorize_research(actor_id, project_id)
        def authorize_operation(self, operation, actor, project_id):
            with self.guard:
                return super().authorize_operation(operation, actor, project_id)
        def issue_bridge(self, lease_id, project_id):
            with self.guard:
                self.revoke_bridge(lease_id)
                credential = secrets.token_hex(32)
                self._tokens[credential] = Actor(id="desktop-assistant:" + lease_id, scopes=frozenset({"read"}), project_ids=frozenset({project_id}))
                self.bridge_tokens[lease_id] = credential
                return credential
        def revoke_bridge(self, lease_id):
            with self.guard:
                credential = self.bridge_tokens.pop(lease_id, None)
                self._tokens.pop(credential, None)
    workspace = provision_source(root, state, manifest)
    backend = None
    if manifest.get("backend_python"):
        from modelbuilder.backend import ZenMLBackend
        backend = ZenMLBackend(data_dir=state, python_executable=str(relative_file(root, manifest["backend_python"])),
                               source_root=workspace, trusted_code_roots=[str(workspace)] if workspace else [],
                               mlx_python_executable=str(relative_file(root, manifest["mlx_python"])) if manifest.get("mlx_python") else None)
    defaults = {"local": {"path": str(workspace), "trusted": True, "label": "Local model source"}} if workspace else {}
    source_roots = load_source_roots(state, defaults)
    service = OperationService(db_path=state / "modelbuilder.sqlite3", backend=backend,
                               source_root=workspace, source_roots=source_roots or None)
    _, identities = registered_sources(state)
    service.source_workspaces = source_workspace_manager(source_roots, identities)
    return create_app(service, DesktopAuth())


def rpc(session: dict, path: str, payload: dict | None = None) -> dict:
    # A tampered session must never cause credentials to be sent off-host.
    port = session.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise CoreError("Invalid managed core port")
    token = session.get("token")
    if not isinstance(token, str) or len(token) != 64:
        raise CoreError("Invalid managed core session")
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            raise CoreError("Managed core redirects are forbidden")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=2) as response:
        return json.load(response)


def discover(state: Path) -> dict | None:
    try:
        session = private_read(state / SESSION)
        response = rpc(session, "/_desktop/session")
        return session if response.get("instance") == session.get("instance") else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def acquire(root: Path, state: Path) -> dict:
    state = private_directory(state)
    fingerprint = hashlib.sha256((root / "runtime.json").read_bytes()).hexdigest()
    session = discover(state)
    if session:
        if session.get("runtime_fingerprint") != fingerprint:
            raise CoreError("Another application runtime owns this data directory; close its clients before updating")
        return session
    # The spawned service takes the lifetime lock. Simultaneous windows may
    # launch contenders; losers exit without changing state or binding ports.
    subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "serve", "--runtime-root", str(root),
                      "--state-dir", str(state)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env=service_environment(), start_new_session=True, close_fds=True)
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        session = discover(state)
        if session:
            if session.get("runtime_fingerprint") != fingerprint:
                raise CoreError("Another application runtime owns this data directory; close its clients before updating")
            return session
        time.sleep(0.1)
    raise CoreError("The bundled core could not start. Check runtime integrity and private app storage")


class Lease:
    def __init__(self, session: dict):
        self.session = session
        self.identity = secrets.token_hex(24)
        self.stopped = threading.Event()
        self.failed = threading.Event()
        rpc(session, "/_desktop/lease", {"id": self.identity})
        self.worker = threading.Thread(target=self._heartbeat, daemon=True)
        self.worker.start()

    def _heartbeat(self):
        while not self.stopped.wait(LEASE_TTL / 3):
            try:
                rpc(self.session, "/_desktop/lease", {"id": self.identity})
            except (OSError, ValueError):
                self.failed.set()
                return

    def close(self):
        self.stopped.set()
        self.worker.join(timeout=3)
        with contextlib.suppress(OSError, ValueError):
            rpc(self.session, "/_desktop/release", {"id": self.identity})


def serve(root: Path, state: Path, *, idle_grace: float = IDLE_GRACE) -> int:
    import uvicorn
    from fastapi import Depends, Header, Body
    state = private_directory(state)
    fd = os.open(state / "service.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                # Byte-range locks may extend past EOF; no persisted credential.
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
            return 0
        manifest = runtime_manifest(root)
        verify_runtime(root, manifest)
        token, instance = secrets.token_hex(32), secrets.token_hex(16)
        app = make_application(root, state, manifest, token)
        leases: dict[str, float] = {}
        last_busy = time.monotonic()
        def owner(authorization: str | None = Header(default=None)):
            actor = app.state.auth.authenticate(authorization)
            if actor.id != "desktop-owner":
                from modelbuilder.models import ServiceError
                raise ServiceError("forbidden", "Desktop management requires the owner session", status_code=403)
            return actor
        @app.get("/_desktop/session", dependencies=[Depends(owner)])
        def session_info():
            return {"instance": instance}
        @app.post("/_desktop/lease", dependencies=[Depends(owner)])
        def touch(payload: dict = Body(...)):
            if server.should_exit:
                from modelbuilder.models import ServiceError
                raise ServiceError("core_restarting", "The local core is restarting", status_code=503)
            identity = payload.get("id")
            if not isinstance(identity, str) or len(identity) != 48 or any(c not in "0123456789abcdef" for c in identity):
                from modelbuilder.models import ServiceError
                raise ServiceError("invalid_lease", "Invalid desktop client lease")
            leases[identity] = time.monotonic()
            return {"instance": instance}
        @app.post("/_desktop/release", dependencies=[Depends(owner)])
        def release(payload: dict = Body(...)):
            identity = payload.get("id")
            if isinstance(identity, str):
                leases.pop(identity, None)
                app.state.auth.revoke_bridge(identity)
            return {"instance": instance}
        @app.post("/_desktop/bridge")
        def bridge(payload: dict = Body(...), actor=Depends(owner)):
            from modelbuilder.models import ServiceError
            identity, project = payload.get("id"), payload.get("project_id")
            if not isinstance(identity, str) or identity not in leases or not isinstance(project, str):
                raise ServiceError("invalid_lease", "An active project-scoped consumer lease is required", status_code=403)
            app.state.service.invoke("project.get", {"project_id": project}, actor)
            credential = app.state.auth.issue_bridge(identity, project)
            return {"token": credential, "scopes": ["read"], "project_ids": [project]}
        source_guard = threading.RLock()
        @app.post("/_desktop/source")
        def register_source(payload: dict = Body(...), actor=Depends(owner)):
            from modelbuilder.source_workspace import SourceWorkspaces
            from modelbuilder.models import ServiceError
            if set(payload) != {"folder_path", "project_id", "trust_consent"} or payload.get("trust_consent") is not True:
                raise ServiceError("source_trust_required", "Select and explicitly trust the source folder in the native application", status_code=403)
            folder, project = payload.get("folder_path"), payload.get("project_id")
            if not isinstance(folder, str) or not isinstance(project, str):
                raise ServiceError("invalid_source", "A native folder and model project are required")
            path = Path(folder)
            if not path.is_absolute() or not path.is_dir() or path.is_symlink() or path.resolve() != path:
                raise ServiceError("invalid_source", "Select the canonical committed Git checkout directory")
            app.state.service.invoke("project.get", {"project_id": project}, actor)
            alias = "workspace-" + hashlib.sha256(project.encode()).hexdigest()[:24]
            settings = {"path": str(path), "trusted": True, "project_ids": [project], "label": path.name}
            proposed = SourceWorkspaces({alias: settings})
            receipt = proposed.inspect(alias, project)
            with source_guard:
                aliases, identities = registered_sources(state)
                aliases[alias], identities[alias] = settings, receipt["repository_id"]
                # Recheck identity before the durable write; a directory swap
                # cannot be accepted from the earlier native-picker receipt.
                if proposed.inspect(alias, project)["repository_id"] != receipt["repository_id"] or path.resolve() != path:
                    raise ServiceError("source_identity_conflict", "Source checkout changed during registration", status_code=409)
                private_write(state / "sources.json", {"schema_version": 1, "aliases": aliases, "identities": identities})
                current = app.state.service.source_workspaces.aliases
                app.state.service.source_workspaces = source_workspace_manager({**current, alias: settings}, identities)
            return {**receipt, "project_id": project, "registered": True}
        # Keep the listening socket open from allocation through uvicorn start.
        # There is no free-port/close/rebind race or externally configurable host.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        config = uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=10)
        server = uvicorn.Server(config)
        async def run():
            nonlocal last_busy
            async def lifecycle():
                nonlocal last_busy
                while not server.started and not server.should_exit:
                    await asyncio.sleep(0.02)
                if not server.started:
                    return
                private_write(state / SESSION, {"schema_version": 1, "instance": instance,
                              "port": sock.getsockname()[1], "token": token, "pid": os.getpid(),
                              "runtime_fingerprint": hashlib.sha256((root / "runtime.json").read_bytes()).hexdigest()})
                while not server.should_exit:
                    await asyncio.sleep(0.2)
                    now = time.monotonic()
                    for identity, touched in list(leases.items()):
                        if now - touched >= LEASE_TTL:
                            leases.pop(identity, None)
                            app.state.auth.revoke_bridge(identity)
                    live = bool(leases)
                    # Submitted execution has its own supervised durable lifetime.
                    # Do not cancel or signal those jobs when the UI closes.
                    if live:
                        last_busy = now
                    elif now - last_busy >= idle_grace:
                        server.should_exit = True
            monitor = asyncio.create_task(lifecycle())
            try:
                await server.serve(sockets=[sock])
            finally:
                monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor
        try:
            asyncio.run(run())
        finally:
            sock.close()
            with contextlib.suppress(OSError, ValueError):
                if private_read(state / SESSION).get("instance") == instance:
                    (state / SESSION).unlink()
            app.state.service.store.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["serve", "lease", "mcp", "verify"])
    parser.add_argument("--runtime-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--project-id", help="Required for MCP; grants read-only access to this one project")
    args = parser.parse_args()
    if args.mode == "mcp" and not args.project_id:
        parser.error("mcp requires --project-id; unscoped assistant access is forbidden")
    os.umask(0o077)
    sys.dont_write_bytecode = True
    try:
        if args.mode == "verify":
            verify_runtime(args.runtime_root, runtime_manifest(args.runtime_root))
            print(json.dumps({"ok": True}))
            return 0
        if args.mode == "serve":
            return serve(args.runtime_root, args.state_dir)
        session = acquire(args.runtime_root, args.state_dir)
        lease = Lease(session)
        try:
            if args.mode == "lease":
                print(json.dumps({"url": f"http://127.0.0.1:{session['port']}", "token": session["token"],
                                  "instance": session["instance"]}), flush=True)
                # EOF belongs to this consumer; another client's lease survives.
                ended = threading.Event()
                def stdin_closed():
                    for _ in sys.stdin:
                        pass
                    ended.set()
                threading.Thread(target=stdin_closed, daemon=True).start()
                while not ended.wait(0.2):
                    if lease.failed.is_set():
                        raise CoreError("Managed core session ended")
            else:
                from modelbuilder.mcp_server import Configuration, serve as serve_mcp
                grant = rpc(session, "/_desktop/bridge", {"id": lease.identity, "project_id": args.project_id})
                asyncio.run(serve_mcp(Configuration(api_url=f"http://127.0.0.1:{session['port']}", token=grant["token"])))
        finally:
            lease.close()
        return 0
    except (CoreError, OSError):
        print("ModelBuilder core could not start or its local session ended; verify the installation and private app storage.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
