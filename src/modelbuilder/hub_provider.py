"""Pluggable Hub capabilities and an isolated, pinned Hugging Face SDK boundary.

Only operator-owned account aliases cross the service boundary. The SDK worker
resolves credentials directly from an OS keyring; no tokens enter service state.
"""
from __future__ import annotations

from collections.abc import Callable
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Protocol

from .models import ServiceError

SDK_VERSION = "0.36.2"
MAX_METADATA_BYTES = 16 * 1024 * 1024


class HubProvider(Protocol):
    def capabilities(self) -> dict: ...
    def validate_repository(self, repo_id: str, repo_type: str) -> None: ...
    def immutable_revision(self, revision: str) -> bool: ...
    def call(self, action: str, payload: dict, *, cancelled: Callable[[], bool] = lambda: False) -> dict: ...


class HubCancelled(Exception):
    pass


class HuggingFaceProvider:
    """One bounded SDK subprocess per call, with an explicitly selected interpreter.

    No inherited HF tokens, implicit login cache, proxy configuration or arbitrary
    endpoints. Network/TLS/retry/resume and commit protocols belong to the SDK.
    """
    def __init__(self, python_executable: str, accounts: dict | None = None, *, timeout: int = 3600):
        self.python = os.path.abspath(python_executable)  # Preserve venv symlinks.
        self.accounts = accounts or {}
        self.timeout = timeout

    def validate_repository(self, repo_id, repo_type):
        if repo_type not in {"model", "dataset"} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", repo_id):
            raise ServiceError("hub_repository_invalid", "Hugging Face repositories require owner/name and model or dataset type")

    def immutable_revision(self, revision):
        return isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{40}", revision) is not None

    def capabilities(self):
        return {"id": "huggingface", "repo_types": ["model", "dataset"], "search": True,
                "download": True, "publish": True, "immutable_revisions": True, "resume": True,
                "credential_provider": "os_keyring", "sdk": f"huggingface_hub=={SDK_VERSION}",
                "repository_identity": "owner/name", "immutable_revision_identity": "git-sha1",
                "external_acceptance": "pending"}

    def call(self, action, payload, *, cancelled=lambda: False):
        account = payload.get("account_alias")
        settings = self.accounts.get(account) if account else None
        if account and settings is None:
            raise ServiceError("hub_account_unavailable", "Configure the selected secure account alias", status_code=403)
        request = {"action": action, "payload": payload,
                   "credential": {"service": settings["keyring_service"], "username": settings["username"]} if settings else None}
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "LANG", "TMPDIR", "TEMP", "TMP", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"}}
        env.update(HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_XET="1",
                   HF_HUB_DISABLE_PROGRESS_BARS="1", HF_HUB_DOWNLOAD_TIMEOUT="20", HF_HUB_ETAG_TIMEOUT="20",
                   PYTHONNOUSERSITE="1")
        worker = Path(__file__).with_name("hub_sdk_worker.py")
        # A temporary SDK HOME also prevents saved login or endpoint configuration.
        with tempfile.TemporaryDirectory(prefix="modelbuilder-hub-call-") as temporary:
            env["HF_HOME"] = temporary
            output = Path(temporary) / "result.json"
            request["result_path"] = str(output)
            process = subprocess.Popen([self.python, "-I", str(worker)], stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            try:
                process.stdin.write(json.dumps(request, allow_nan=False).encode())
                process.stdin.close()
                deadline = time.monotonic() + (min(self.timeout, 90) if action in {"search", "inspect", "destination", "reconcile"} else self.timeout)
                while process.poll() is None:
                    if cancelled():
                        raise HubCancelled()
                    if time.monotonic() > deadline:
                        raise ServiceError("hub_timeout", "The bounded Hub operation timed out", status_code=504)
                    if output.exists() and output.stat().st_size > MAX_METADATA_BYTES:
                        raise ServiceError("hub_response_invalid", "Hub response exceeds the metadata limit", status_code=502)
                    time.sleep(0.05)
                if not output.is_file() or output.stat().st_size > MAX_METADATA_BYTES:
                    raise ServiceError("hub_worker_unavailable", "The isolated Hub worker did not return a bounded result", status_code=503)
                result = json.loads(output.read_bytes())
                if not isinstance(result, dict):
                    raise ValueError()
                if "error" in result:
                    code = result["error"].get("code", "hub_provider_failed")
                    allowed = {"hub_gated", "hub_private_or_missing", "hub_revision_missing", "hub_access_denied", "hub_dependency_unavailable", "hub_account_unavailable", "hub_budget_exceeded", "hub_destination_changed", "hub_verification_failed", "hub_provider_failed", "hub_license_mismatch", "hub_upload_memory_budget"}
                    if code not in allowed:
                        code = "hub_provider_failed"
                    raise ServiceError(code, {"hub_gated": "Accept the provider's gated-access terms in its own website before retrying",
                                              "hub_private_or_missing": "Repository is private, missing or unavailable to this account",
                                              "hub_account_unavailable": "The OS keyring account alias is unavailable",
                                              "hub_dependency_unavailable": "Install the reviewed isolated Hub dependency profile",
                                              "hub_upload_memory_budget": "Provider requested an unbounded regular upload; large files require the SDK streaming upload path"}.get(code, "Hub operation could not complete safely"), status_code=409)
                return result
            except (OSError, ValueError, json.JSONDecodeError):
                raise ServiceError("hub_worker_unavailable", "The isolated Hub worker is unavailable", status_code=503) from None
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
