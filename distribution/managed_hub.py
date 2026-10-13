"""Native-only, project-scoped Hub settings. Stores keyring references, never secrets.

This protects service/MCP authority boundaries, not other same-user OS processes.
Credentials stay in an OS-backed keyring and are resolved only by the SDK worker.
"""
from __future__ import annotations

import copy
from pathlib import Path
import re
import threading
import time

from modelbuilder.hub import HubTransfers, TERMINAL, DEFAULT_MAX_BYTES, MAX_BYTES, digest, fail
from modelbuilder.hub_provider import HuggingFaceProvider
from modelbuilder.models import utcnow

KIND = "desktop_hub_settings"


def byte_budget(value):
    if type(value) is not int or not 1 <= value <= MAX_BYTES:
        fail("invalid_hub_budget", "Project transfer limit must be an integer between 1 byte and 1 PiB", 400)
    return value


def folder_identity(value):
    if not isinstance(value, str):
        fail("invalid_hub_folder", "Select a native publication folder", 400)
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or not path.is_dir():
        fail("invalid_hub_folder", "Select the canonical existing publication directory", 400)
    for ancestor in (path, *path.parents):
        info = ancestor.lstat()
        if ancestor.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            fail("invalid_hub_folder", "Publication folders cannot contain redirected directory components", 400)
    info = path.stat()
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino}


def account_reference(value, project):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"keyring_service", "username"}:
        fail("invalid_hub_account", "Only an existing OS-keyring service and username reference may be linked", 400)
    for item in value.values():
        if (not isinstance(item, str) or not 1 <= len(item) <= 160 or item != item.strip()
                or any(ord(c) < 32 for c in item) or re.match(r"(?i)(hf_|bearer |sk-)", item)):
            fail("invalid_hub_account", "Enter keyring lookup names, never a password or access token", 400)
    # A changed reference NEVER reuses an alias captured by an old transfer.
    return {**value, "alias": "account-" + digest([project, value])[:32]}


class ManagedHubTransfers(HubTransfers):
    def __init__(self, service, *, config, guard):
        self.settings_guard = guard
        self.folder_identities = {}
        self.project_budgets = {}
        super().__init__(service, config=config)

    def invoke(self, *args, **kwargs):
        # Fences synchronous preview/approval/start against settings updates.
        # No database writer lock is held across provider I/O.
        with self.settings_guard:
            return super().invoke(*args, **kwargs)

    def byte_budget(self, project_id):
        return byte_budget(self.project_budgets.get(project_id, DEFAULT_MAX_BYTES))

    def _source(self, project, alias):
        identity = self.folder_identities.get((project, alias))
        if identity is None:
            fail("hub_source_unavailable", "Select a publication folder in native Hub settings", 403)
        if folder_identity(identity["path"]) != identity:
            fail("hub_source_changed", "Publication directory identity changed; review its native settings again")
        return super()._source(project, alias)


class HubSettings:
    def __init__(self, service, python, state):
        self.service, self.store = service, service.store
        self.python = str(python)
        self.guard = threading.RLock()
        service.hub.close()
        service.hub = ManagedHubTransfers(service, config={"root": str(state / "hub"), "python_executable": self.python}, guard=self.guard)
        self.refresh()

    def _owner(self, actor, project):
        if actor.id != "desktop-owner":
            fail("forbidden", "Hub account and folder settings require the native desktop owner", 403)
        self.service.invoke("project.get", {"project_id": project}, actor)

    def _row(self, project):
        rows = self.store.list(KIND, project)
        return {**rows[0], "max_bytes": byte_budget(rows[0].get("max_bytes", DEFAULT_MAX_BYTES))} if rows else {"project_id": project, "revision": 0, "account": None, "folder": None, "max_bytes": DEFAULT_MAX_BYTES}

    def status(self, actor, project):
        self._owner(actor, project)
        with self.guard:
            return {**copy.deepcopy(self._row(project)), "worker": "bundled", "credential_status": "not_checked",
                    "authentication": "existing_os_keyring_reference_only"}

    def preview(self, actor, payload):
        if not isinstance(payload, dict) or set(payload) != {"project_id", "expected_revision", "account", "folder_path", "folder_selected", "max_bytes"}:
            fail("invalid_hub_settings", "Native settings require the exact project, revision, account reference and selected folder", 400)
        project = payload["project_id"]
        self._owner(actor, project)
        with self.guard:
            row = self._row(project)
            if type(payload["expected_revision"]) is not int or row["revision"] != payload["expected_revision"]:
                fail("hub_settings_changed", "Hub settings changed; reopen the native settings dialog")
            if type(payload["folder_selected"]) is not bool:
                fail("invalid_hub_folder", "Native folder selection must be explicit", 400)
            folder = folder_identity(payload["folder_path"]) if payload["folder_path"] is not None else None
            if folder is not None and folder != row["folder"] and not payload["folder_selected"]:
                fail("hub_source_changed", "Choose the changed publication folder again in the native folder picker")
            plan = {"project_id": project, "expected_revision": row["revision"],
                    "account": account_reference(payload["account"], project),
                    "folder": folder, "max_bytes": byte_budget(payload["max_bytes"]),
                    "expires_at": int(time.time()) + 900}
            return {"plan": plan, "review_digest": digest(plan)}

    def apply(self, actor, payload):
        if not isinstance(payload, dict) or set(payload) != {"plan", "review_digest"}:
            fail("invalid_hub_settings", "Review the exact native Hub settings before applying", 400)
        plan = payload["plan"]
        if not isinstance(plan, dict) or set(plan) != {"project_id", "expected_revision", "account", "folder", "max_bytes", "expires_at"} or digest(plan) != payload["review_digest"]:
            fail("invalid_hub_settings", "The reviewed Hub settings changed", 400)
        project = plan["project_id"]
        self._owner(actor, project)
        with self.guard:
            if type(plan["expires_at"]) is not int or not time.time() < plan["expires_at"] <= time.time() + 901:
                fail("hub_settings_expired", "Reopen the expired native Hub settings review")
            limit = byte_budget(plan["max_bytes"])
            account = plan["account"]
            if account is not None and account_reference({k: v for k, v in account.items() if k != "alias"}, project) != account:
                fail("invalid_hub_account", "The reviewed account reference changed", 400)
            folder = plan["folder"]
            if folder is not None and folder_identity(folder.get("path")) != folder:
                fail("hub_source_changed", "The selected publication directory changed during review")
            with self.store.transaction():
                current = self._row(project)
                if type(plan["expected_revision"]) is not int or current["revision"] != plan["expected_revision"]:
                    fail("hub_settings_changed", "Hub settings changed; reopen the native settings dialog")
                row = {"project_id": project, "revision": current["revision"] + 1,
                       "account": account, "folder": folder, "max_bytes": limit, "updated_at": utcnow()}
                self.store.put(KIND, project, project, row, current["revision"] or None)
                revoked, cancelling = [], []
                for preview in self.store.list("hub_preview", project):
                    if not preview.get("discarded"):
                        self.service.hub._put("hub_preview", preview["preview_id"], preview, discarded=True, settings_revoked=True)
                        revoked.append(preview["preview_id"])
                for approval in self.store.list("hub_approval", project):
                    if not approval.get("consumed"):
                        self.service.hub._put("hub_approval", approval["approval_id"], approval, consumed=True, settings_revoked=True)
                for transfer in self.store.list("hub_transfer", project):
                    if transfer["state"] not in TERMINAL:
                        self.service.hub._put("hub_transfer", transfer["transfer_id"], transfer, cancel_requested=True)
                        cancelling.append(transfer["transfer_id"])
                self.store.audit(actor_id=actor.id, operation="desktop.hub.settings", request_id=payload["review_digest"],
                                 project_id=project, outcome="succeeded", before_revision=current["revision"], after_revision=row["revision"],
                                 details={"max_bytes": limit, "account_alias": account["alias"] if account else None, "revoked_previews": revoked, "cancellation_requested": cancelling})
            self.refresh()
            return {**row, "credential_status": "not_checked", "revoked_previews": revoked, "cancellation_requested": cancelling,
                    "notice": "Existing previews are invalid. Cancellation cannot undo already transmitted files; reconcile submitted transfers."}

    def refresh(self):
        accounts, roots, identities, budgets = {}, {}, {}, {}
        for row in self.store.list(KIND):
            project = row["project_id"]
            budgets[project] = byte_budget(row.get("max_bytes", DEFAULT_MAX_BYTES))
            if row["account"]:
                account = row["account"]
                accounts[account["alias"]] = {"keyring_service": account["keyring_service"], "username": account["username"], "project_ids": [project]}
            if row["folder"]:
                folder = row["folder"]
                alias = "publication-" + digest([project, folder])[:24]
                roots[project] = {alias: folder["path"]}
                identities[project, alias] = folder
        hub = self.service.hub
        hub.accounts, hub.publication_roots, hub.folder_identities, hub.project_budgets = accounts, roots, identities, budgets
        hub.providers["huggingface"] = HuggingFaceProvider(self.python, copy.deepcopy(accounts))
