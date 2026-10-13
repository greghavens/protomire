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
import platform
import re
from pathlib import Path
import secrets
import shutil
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
    actual_names = set()
    for entry in root.rglob("*"):
        if entry == root / "runtime.json":
            continue
        if entry.is_symlink() and not entry.resolve().is_relative_to(root.resolve()):
            raise CoreError("Bundled runtime contains an unsealed redirected entry")
        if entry.is_file():
            actual_names.add(entry.relative_to(root).as_posix())
        elif not entry.is_dir():
            raise CoreError("Bundled runtime contains an unsealed filesystem entry")
    if actual_names != set(files):
        raise CoreError("Bundled runtime contains unlisted or missing files; reinstall or reseal the reviewed payload")
    for name, expected in files.items():
        path = relative_file(root, name)
        with path.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
        if actual != expected:
            raise CoreError("Bundled runtime integrity check failed; reinstall the application")


def service_environment() -> dict[str, str]:
    # No Python injection, alternate Modelbuilder configuration, bearer tokens,
    # Git overrides, proxy redirect or inherited assistant credentials.
    allowed = {"PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR", "TMPDIR", "TMP", "TEMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def runtime_environment(state: Path) -> dict[str, str]:
    """Caches live in private app state; HOME remains the OS-keyring identity."""
    cache = private_directory(state / "cache")
    temporary = private_directory(cache / "tmp")
    env = service_environment()
    env.update(XDG_CACHE_HOME=str(cache), HF_HOME=str(cache / "huggingface"), TORCH_HOME=str(cache / "torch"),
               TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary), PYTHONDONTWRITEBYTECODE="1")
    return env


def restore_source(git: Path, archive: Path, destination: Path, commit: str,
                   shallow: Path | None = None) -> dict:
    """Restore a complete or shallow bundle only into a new disposable checkout.

    Bundles do not carry .git/shallow. Install its reviewed sidecar before
    unbundling, then restore every real ref and verify connectivity and identity.
    This path never fetches, runs checkout hooks, or touches an existing tree.
    """
    if destination.exists() or destination.is_symlink():
        raise CoreError("Source restore requires a new destination")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise CoreError("Source restore requires a full source commit")
    boundaries = shallow.read_text().splitlines() if shallow else []
    if shallow and (not boundaries or len(set(boundaries)) != len(boundaries)
                    or any(not re.fullmatch(r"[0-9a-f]{40}", line) for line in boundaries)):
        raise CoreError("Source shallow sidecar contains invalid boundaries")
    env = {**service_environment(), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1",
           "GIT_ALLOW_PROTOCOL": "file"}

    def run(*arguments, **options):
        return subprocess.run([str(git), "-c", "core.hooksPath=" + os.devnull, *arguments],
                              env=env, check=True, capture_output=True, text=True, timeout=60, **options).stdout

    heads = run("bundle", "list-heads", str(archive)).splitlines()
    refs = {}
    for line in heads:
        sha, name = line.split(" ", 1)
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise CoreError("Source bundle contains an invalid ref identity")
        # HEAD and linked-worktree HEAD entries are not real refs to recreate.
        if name.startswith("refs/"):
            run("check-ref-format", name)
            if name in refs:
                raise CoreError("Source bundle contains duplicate refs")
            refs[name] = sha
    if commit not in refs.values():
        raise CoreError("Source bundle does not expose the selected source commit")
    try:
        run("init", "--template=", str(destination))
        if shallow:
            shutil.copyfile(shallow, destination / ".git/shallow")
        run("-C", str(destination), "bundle", "unbundle", str(archive))
        run("-C", str(destination), "update-ref", "--stdin",
            input="start\n" + "".join(f"create {name} {sha}\n" for name, sha in sorted(refs.items()))
            + "prepare\ncommit\n")
        actual = run("-C", str(destination), "for-each-ref", "--format=%(objectname) %(refname)").splitlines()
        if actual != [f"{sha} {name}" for name, sha in sorted(refs.items())]:
            raise CoreError("Restored source refs do not match the bundle")
        if run("-C", str(destination), "cat-file", "-t", commit).strip() != "commit":
            raise CoreError("Selected source identity is not a commit")
        for boundary in boundaries:
            if run("-C", str(destination), "cat-file", "-t", boundary).strip() != "commit":
                raise CoreError("Source shallow boundary is not a bundled commit")
        run("-C", str(destination), "fsck", "--full")
        run("-C", str(destination), "checkout", "--detach", commit)
        if run("-C", str(destination), "rev-parse", "HEAD").strip() != commit:
            raise CoreError("Restored source commit does not match")
        return {"source_commit": commit, "refs": refs, "shallow_boundaries": boundaries, "fsck": "passed"}
    except (subprocess.SubprocessError, OSError, CoreError):
        shutil.rmtree(destination, ignore_errors=True)
        raise


def provision_source(root: Path, state: Path, manifest: dict) -> Path | None:
    bundle = manifest.get("source_bundle")
    if not bundle:
        return None
    git = relative_file(root, manifest["git_executable"])
    archive = relative_file(root, bundle)
    workspace = state / "source"
    shallow = relative_file(root, manifest["source_bundle_shallow"]) if manifest.get("source_bundle_shallow") else None
    # Backend and SourceWorkspaces use git by name. Only this child process PATH
    # changes; no profile, machine environment, or user Git configuration changes.
    os.environ["PATH"] = str(git.parent) + os.pathsep + os.environ.get("PATH", "")
    if not workspace.exists():
        pending = state / (".source-" + secrets.token_hex(8))
        try:
            restore_source(git, archive, pending, manifest["source_commit"], shallow)
            pending.rename(workspace)
        except (subprocess.SubprocessError, OSError, CoreError):
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


def optional_mlx_available(manifest: dict, system: str | None = None, version: str | None = None) -> bool:
    if not manifest.get("mlx_python") or (system or platform.system()) != "Darwin":
        return False
    required = manifest.get("mlx_minimum_os")
    if not isinstance(required, str):
        return False
    try:
        return tuple(map(int, (version or platform.mac_ver()[0]).split(".")[:2])) >= tuple(map(int, required.split(".")))
    except ValueError:
        return False


def make_application(root: Path, state: Path, manifest: dict, token: str):
    from modelbuilder.api import create_app
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from reviewed_requests import ReviewGrants, ReviewQueue, ReviewedOperationService
    OperationService = ReviewedOperationService
    grants = ReviewGrants()
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
            reviewed = grants.authorize_research(actor_id, project_id)
            if reviewed is not None:
                return reviewed
            with self.guard:
                return super().authorize_research(actor_id, project_id)
        def authorize_operation(self, operation, actor, project_id):
            if grants.find(actor) is not None:
                return grants.authorize(operation, actor, project_id)
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
                               mlx_python_executable=str(relative_file(root, manifest["mlx_python"])) if optional_mlx_available(manifest) else None)
    defaults = {"local": {"path": str(workspace), "trusted": True, "label": "Local model source"}} if workspace else {}
    source_roots = load_source_roots(state, defaults)
    service = OperationService(db_path=state / "modelbuilder.sqlite3", backend=backend,
                               source_root=workspace, source_roots=source_roots or None)
    _, identities = registered_sources(state)
    service.source_workspaces = source_workspace_manager(source_roots, identities)
    app = create_app(service, DesktopAuth())
    if manifest.get("hub_python"):
        from managed_hub import HubSettings
        app.state.hub_settings = HubSettings(service, relative_file(root, manifest["hub_python"]), state)
    app.state.review_queue = ReviewQueue(service, grants)
    return app


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
    subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "serve", "--runtime-root", str(root),
                      "--state-dir", str(state)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env=runtime_environment(state), start_new_session=True, close_fds=True)
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
        def consumer(authorization: str | None = Header(default=None)):
            actor = app.state.auth.authenticate(authorization)
            if not actor.id.startswith("desktop-assistant:"):
                from modelbuilder.models import ServiceError
                raise ServiceError("forbidden", "This endpoint requires a managed project consumer", status_code=403)
            return actor
        @app.post("/_assistant/reviews")
        def request_review(payload: dict = Body(...), actor=Depends(consumer)):
            from modelbuilder.models import ServiceError
            if set(payload) != {"project_id", "operation", "payload"}:
                raise ServiceError("validation_error", "Review requests cannot supply native approval flags")
            return app.state.review_queue.submit(actor, payload["project_id"], payload["operation"], payload["payload"])
        @app.get("/_assistant/reviews/{identity}")
        def review_status(identity: str, actor=Depends(consumer)):
            return app.state.review_queue.status(actor, identity)
        @app.post("/_assistant/reviews/{identity}/cancel")
        def cancel_review(identity: str, actor=Depends(consumer)):
            return app.state.review_queue.cancel(actor, identity)
        @app.get("/_desktop/reviews", dependencies=[Depends(owner)])
        def native_reviews(actor=Depends(owner)):
            return {"requests": app.state.review_queue.pending(actor)}
        @app.get("/_desktop/reviews/{identity}")
        def native_review_status(identity: str, actor=Depends(owner)):
            return app.state.review_queue.status(actor, identity)
        @app.post("/_desktop/reviews")
        def native_request_review(payload: dict = Body(...), actor=Depends(owner)):
            return app.state.review_queue.submit(actor, payload["project_id"], payload["operation"], payload["payload"])
        @app.post("/_desktop/reviews/{identity}/decision")
        def native_decision(identity: str, payload: dict = Body(...), actor=Depends(owner)):
            from modelbuilder.models import ServiceError
            if set(payload) != {"review_digest", "decision"}:
                raise ServiceError("validation_error", "Native decisions cannot replace the reviewed payload")
            return app.state.review_queue.decide(actor, identity, payload["review_digest"], payload["decision"])
        def hub_settings():
            from modelbuilder.models import ServiceError
            settings = getattr(app.state, "hub_settings", None)
            if settings is None:
                raise ServiceError("hub_provider_unavailable", "This runtime has no reviewed bundled Hub worker", status_code=503)
            return settings
        @app.get("/_desktop/hub/settings/{project}")
        def native_hub_status(project: str, actor=Depends(owner)):
            return hub_settings().status(actor, project)
        @app.post("/_desktop/hub/settings/preview")
        def native_hub_preview(payload: dict = Body(...), actor=Depends(owner)):
            return hub_settings().preview(actor, payload)
        @app.post("/_desktop/hub/settings/apply")
        def native_hub_apply(payload: dict = Body(...), actor=Depends(owner)):
            return hub_settings().apply(actor, payload)
        source_guard = threading.RLock()
        @app.post("/_desktop/source")
        def register_source(payload: dict = Body(...), actor=Depends(owner)):
            from modelbuilder.source_workspace import SourceWorkspaces
            from modelbuilder.models import ServiceError
            if (not {"folder_path", "project_id", "trust_consent"} <= set(payload)
                    or set(payload) - {"folder_path", "project_id", "trust_consent", "source_kind"}
                    or payload.get("trust_consent") is not True):
                raise ServiceError("source_trust_required", "Select and explicitly trust the source folder in the native application", status_code=403)
            kind = payload.get("source_kind", "git")
            if not isinstance(kind, str) or kind not in {"git", "imported_snapshot"}:
                raise ServiceError("invalid_source", "Explicitly choose a Git checkout or an imported snapshot")
            folder, project = payload.get("folder_path"), payload.get("project_id")
            if not isinstance(folder, str) or not isinstance(project, str):
                raise ServiceError("invalid_source", "A native folder and model project are required")
            path = Path(folder)
            if not path.is_absolute() or not path.is_dir() or path.is_symlink() or path.resolve() != path:
                raise ServiceError("invalid_source", "Select the canonical source directory for the explicitly chosen source kind")
            app.state.service.invoke("project.get", {"project_id": project}, actor)
            alias = "workspace-" + hashlib.sha256(project.encode()).hexdigest()[:24]
            settings = {"path": str(path), "kind": kind, "trusted": True, "project_ids": [project], "label": path.name}
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
                    live = bool(leases) or app.state.review_queue.has_active_transfers()
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
    parser.add_argument("--mcp-profile", choices=["native", "triggerfish"], default="native",
                        help="Explicit MCP compatibility transport; never changes service authorization")
    args = parser.parse_args()
    if args.mode != "mcp" and args.mcp_profile != "native":
        parser.error("--mcp-profile applies only to mcp")
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
                from modelbuilder.mcp_server import Configuration
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                from reviewed_mcp import serve as serve_mcp
                grant = rpc(session, "/_desktop/bridge", {"id": lease.identity, "project_id": args.project_id})
                asyncio.run(serve_mcp(Configuration(api_url=f"http://127.0.0.1:{session['port']}", token=grant["token"]), args.project_id, profile=args.mcp_profile))
        finally:
            lease.close()
        return 0
    except (CoreError, OSError):
        print("ModelBuilder core could not start or its local session ended; verify the installation and private app storage.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
