"""Durable, project-scoped Hub downloads and explicitly approved publications.

I/O never holds the store writer lock. Transfer locks fence workers across service
processes; a process restart leaves downloads retryable and uploads uncertain.
Only the original live grant can dispatch work. Stored approval receipts cannot
resurrect revoked authority. Verified assets never create another model/recipe.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import threading
from datetime import datetime, timedelta, timezone

from .export_packages import safe_file
from .hub_provider import HubCancelled, HuggingFaceProvider
from .models import OPERATIONS, ServiceError, new_id, utcnow

DEFAULT_MAX_BYTES = 16 * 1024**3
MAX_BYTES = 2**50  # 1 PiB; exact in JSON/JavaScript even with 2x staging overhead.
MAX_FILES = 10000
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted", "needs_reconciliation"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def fail(code, message, status=409):
    raise ServiceError(code, message, status_code=status)


def safe_name(name):
    if not isinstance(name, str):
        fail("hub_unsafe_path", "Hub file path must be a string", 400)
    path = PurePosixPath(name)
    if (not isinstance(name, str) or not name or str(path) != name or path.is_absolute() or ".." in path.parts
            or "\\" in name or ":" in name or len(name.encode()) > 1024
            or any(ord(c) < 32 for c in name)
            or any(len(part.encode()) > 255 or part.endswith((".", " ")) or
                   re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in path.parts)
            or any(part.casefold() in {".git", ".cache", ".ssh", ".aws", "id_rsa", "id_ed25519", "credentials.json", "secrets.json", "token"}
                   or part.casefold().startswith(".env") for part in path.parts)):
        fail("hub_unsafe_path", "Hub file path is unsafe", 400)
    return name


def directory(path):
    """Only service-owned nonsymlink directories are used for generated assets."""
    path = Path(path).absolute()
    for ancestor in reversed([path, *path.parents]):
        if ancestor.exists():
            info = ancestor.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                fail("hub_unsafe_path", "Hub directory contains an unsafe component")
        else:
            ancestor.mkdir(mode=0o700)
    return path


def safe_output(path):
    """Exclusively create a regular output through no-follow parent descriptors."""
    path = Path(path).absolute()
    if os.name == "nt":
        directory(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    else:
        parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        finally:
            os.close(parent)
    return os.fdopen(fd, "wb")


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def fingerprint(path, limit, destination=None):
    with safe_file(path, max_bytes=limit) as source:
        size = os.fstat(source.fileno()).st_size
        sha = hashlib.sha256()
        git = hashlib.sha1(f"blob {size}\0".encode(), usedforsecurity=False)
        output = None
        try:
            if destination is not None:
                directory(destination.parent)
                output = safe_output(destination)
            total = 0
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > limit or total > size:
                    fail("hub_budget_exceeded", "Hub file exceeded its approved byte limit")
                sha.update(chunk)
                git.update(chunk)
                if output:
                    output.write(chunk)
            if total != size:
                fail("hub_file_changed", "Hub file changed during its snapshot")
            if output:
                output.flush()
                os.fsync(output.fileno())
            return {"size_bytes": size, "sha256": sha.hexdigest(), "git_blob_sha1": git.hexdigest()}
        finally:
            if output:
                output.close()


class TransferLock:
    """Nonblocking OS ownership. A dead process releases its lock automatically."""
    def __init__(self, path):
        directory(path.parent)
        self.stream = open(path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                self.stream.write(b"0")
                self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise BlockingIOError() from None

    def close(self):
        self.stream.close()


class HubTransfers:
    def __init__(self, service, *, config=None, providers=None):
        self.service, self.store = service, service.store
        config = config or {}
        allowed = {"root", "python_executable", "accounts", "publication_roots", "max_bytes", "max_concurrent"}
        if set(config) - allowed:
            raise ValueError("Unsupported Hub operator setting")
        self._temporary = None
        if self.store.path == ":memory:" and not config.get("root"):
            self._temporary = tempfile.TemporaryDirectory(prefix="modelbuilder-hub-")
        self.root = directory(config.get("root") or (self._temporary.name if self._temporary else Path(self.store.path).absolute().with_suffix(".hub")))
        self.max_bytes = config.get("max_bytes", DEFAULT_MAX_BYTES)
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= MAX_BYTES:
            raise ValueError("Hub max_bytes must be an integer between 1 byte and 1 PiB")
        self.accounts = copy.deepcopy(config.get("accounts", {}))
        for alias, account in self.accounts.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", alias) or set(account) - {"keyring_service", "username", "project_ids"} or not all(isinstance(account.get(k), str) and account[k] for k in ("keyring_service", "username")):
                raise ValueError("Hub accounts require opaque aliases and OS keyring lookup names only")
        self.publication_roots = copy.deepcopy(config.get("publication_roots", {}))
        self.providers = dict(providers or {})
        if config.get("python_executable"):
            self.providers.setdefault("huggingface", HuggingFaceProvider(config["python_executable"], self.accounts))
        self._threads = {}
        self._dispatch = threading.RLock()
        concurrent = config.get("max_concurrent", 2)
        if type(concurrent) is not int or not 1 <= concurrent <= 4:
            raise ValueError("Hub max_concurrent must be between 1 and 4")
        self._slots = threading.BoundedSemaphore(concurrent)
        for asset in self.store.list("hub_asset"):
            self._register_root(asset)

    def byte_budget(self, project_id):
        """Operator cap; managed hosts override with an owner-reviewed project cap."""
        return self.max_bytes

    def _authority(self, op, actor, project):
        self.service._require_current_authority(op, actor, project)
        self.service._get("project", project, project)

    def _get(self, kind, identity, project, actor=None):
        row = self.service._get(kind, identity, project)
        if actor is not None and row.get("actor_id") != actor.id:
            fail("forbidden", "This Hub request belongs to another actor", 403)
        return row

    def _put(self, resource_kind, identity, row, **changes):
        updated = {**row, **changes, "revision": row["revision"] + 1, "updated_at": utcnow()}
        self.store.put(resource_kind, identity, row["project_id"], updated, row["revision"])
        return updated

    def _provider(self, p):
        provider = self.providers.get(p["provider"])
        if provider is None:
            fail("hub_provider_unavailable", "This Hub provider has no reviewed configured runtime", 503)
        if "repo_id" in p:
            provider.validate_repository(p["repo_id"], p["repo_type"])
        account = p.get("account_alias")
        if account:
            settings = self.accounts.get(account)
            if settings is None or (settings.get("project_ids") is not None and p["project_id"] not in settings["project_ids"]):
                fail("hub_account_unavailable", "This secure account alias is unavailable to this project", 403)
        return provider

    def _call(self, action, p, *, cancelled=lambda: False):
        try:
            result = self._provider(p).call(action, copy.deepcopy(p), cancelled=cancelled)
            if not isinstance(result, dict) or len(json.dumps(result).encode()) > 16 * 1024 * 1024:
                fail("hub_response_invalid", "Hub provider returned an invalid bounded response", 502)
            self.service._reject_secrets(result)
            return result
        except (ServiceError, HubCancelled):
            raise
        except Exception:
            fail("hub_provider_failed", "Hub provider could not complete the operation safely", 502)

    def _inspect(self, p):
        return self._validate_inspection(p, self._call("inspect", p))

    def _validate_inspection(self, p, result):
        if (result.get("repo_id") != p["repo_id"] or result.get("repo_type") != p["repo_type"]
                or not self._provider(p).immutable_revision(result.get("commit"))
                or not isinstance(result.get("files"), list) or len(result["files"]) > MAX_FILES):
            fail("hub_response_invalid", "Provider did not resolve an immutable bounded repository")
        names = set()
        for item in result["files"]:
            if not isinstance(item, dict) or "path" not in item:
                fail("hub_response_invalid", "Provider file metadata is invalid")
            name = safe_name(item["path"])
            if name.casefold() in names:
                fail("hub_unsafe_path", "Hub repository has colliding file paths")
            names.add(name.casefold())
            if type(item.get("size_bytes")) is not int or not 0 <= item["size_bytes"] <= MAX_BYTES:
                fail("hub_size_unknown", "Exact file sizes are required before transfer")
            if not (re.fullmatch(r"[a-f0-9]{64}", item.get("sha256") or "") or re.fullmatch(r"[a-f0-9]{40}", item.get("git_blob_sha1") or "")):
                fail("hub_hash_unknown", "Immutable provider content hashes are required before transfer")
        if sum(item["size_bytes"] for item in result["files"]) > MAX_BYTES:
            fail("hub_budget_exceeded", "Repository inventory exceeds the supported exact-integer byte ceiling")
        if any(any("/".join(PurePosixPath(name).parts[:index]) in names for index in range(1, len(PurePosixPath(name).parts))) for name in names):
            fail("hub_unsafe_path", "Hub repository file and directory paths collide")
        return result

    def _bound(self, files, budget, project_id=None):
        cap = self.byte_budget(project_id)
        if type(cap) is not int or not 1 <= cap <= MAX_BYTES or type(budget) is not int or not 1 <= budget <= MAX_BYTES:
            fail("hub_budget_exceeded", "Hub byte budgets must be exact positive integers within the supported ceiling")
        if any(type(item.get("size_bytes")) is not int or not 0 <= item["size_bytes"] <= MAX_BYTES for item in files):
            fail("hub_budget_exceeded", "Hub inventory contains an invalid byte count")
        total = sum(item["size_bytes"] for item in files)
        if not 0 < total <= min(budget, cap):
            fail("hub_budget_exceeded", "Selected files exceed the approved byte budget or are empty")
        return total

    def _base(self, project, actor):
        return {"project_id": project, "actor_id": actor.id, "revision": 1, "created_at": utcnow(), "updated_at": utcnow()}

    def _record_result(self, op, p, actor, payload_hash, request_id, result, kind=None, identity=None):
        with self.store.transaction():
            self._authority(op, actor, p["project_id"])
            previous = self.store.idempotent_result(actor.id, p["idempotency_key"], op, payload_hash)
            if previous is not None:
                return previous
            if kind == "hub_asset":
                from .hub_rights import validate_artifact_rights
                validate_artifact_rights(result["rights"], "download", self.policy_snapshot(p["project_id"]), result["usage_context"])
                self._record_materialized_hashes(result)
            if kind == "hub_dataset_policy":
                from .hub_rights import validate_artifact_rights
                validate_artifact_rights(result["rights"], result["intended_use"], self.policy_snapshot(p["project_id"]), result["usage_context"])
                try:
                    prior = self.store.get(kind, identity)
                except ServiceError as error:
                    if error.code != "not_found":
                        raise
                else:
                    if prior["binding_digest"] != result["binding_digest"]:
                        fail("license_dataset_binding_exists", "This dataset revision has a different immutable rights binding; create an explicitly reviewed dataset declaration revision before changing its source policy links")
                    result, kind = prior, None
            if kind:
                self.store.put(kind, identity, p["project_id"], result)
            self.store.save_idempotent(actor.id, p["idempotency_key"], op, payload_hash, result)
            self.store.audit(actor_id=actor.id, operation=op, request_id=request_id, outcome="succeeded",
                             project_id=p["project_id"], target_id=identity)
        return result

    def invoke(self, op, p, actor, payload_hash, request_id):
        project = p["project_id"]
        previous = None
        with self.store.transaction():
            self._authority(op, actor, project)
            if OPERATIONS[op].mutating:
                previous = self.store.idempotent_result(actor.id, p["idempotency_key"], op, payload_hash)
        if previous is not None:
            return self.status(previous["transfer_id"], project) if previous.get("transfer_id") else previous
        if op == "hub.license.local.inspect":
            from .hub_local import inspect_local
            result = inspect_local(self, p, actor)
            return self._record_result(op, p, actor, payload_hash, request_id, result, "hub_license_evidence", result["evidence_id"])
        if op in {"hub.source.policy.bind", "hub.source.policy.replace"}:
            from .hub_source import bind_origin
            return bind_origin(self, p, actor, payload_hash, request_id, operation=op)
        if op == "hub.local.register":
            from .hub_local import register_local
            result = register_local(self, p, actor)
            try:
                saved = self._record_result(op, p, actor, payload_hash, request_id, result, "hub_asset", result["asset_id"])
            except BaseException:
                shutil.rmtree(self.root / "assets" / result["asset_id"], ignore_errors=True)
                raise
            if saved["asset_id"] != result["asset_id"]:
                shutil.rmtree(self.root / "assets" / result["asset_id"], ignore_errors=True)
            self._register_root(saved)
            return saved
        if op == "hub.license.inspect":
            remote = self._validate_inspection(p, self._call("license.inspect", p))
            if (not self._provider(p).immutable_revision(remote.get("commit")) or remote.get("repo_id") != p["repo_id"] or remote.get("repo_type") != p["repo_type"]):
                fail("hub_response_invalid", "License evidence must reference an immutable provider revision")
            documents = remote.get("documents", [])
            if not isinstance(documents, list) or len(documents) > 32:
                fail("license_evidence_incomplete", "License evidence documents are missing or oversized")
            total, seen = 0, set()
            available = {item["path"]: item for item in remote["files"]}
            for document in documents:
                if not isinstance(document, dict) or not isinstance(document.get("text"), str):
                    fail("license_evidence_incomplete", "License evidence requires exact UTF-8 document text")
                safe_name(document["path"])
                if document["path"] in seen or document["path"] not in available:
                    fail("license_evidence_incomplete", "License documents must be unique files from the immutable source tree")
                seen.add(document["path"])
                encoded = document["text"].encode("utf-8")
                total += len(encoded)
                actual = {"size_bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest(),
                          "git_blob_sha1": hashlib.sha1(f"blob {len(encoded)}\0".encode() + encoded, usedforsecurity=False).hexdigest()}
                if (len(encoded) > 1024 * 1024 or actual["sha256"] != document.get("sha256") or document.get("size_bytes") != len(encoded)
                        or not self._matches(available[document["path"]], actual)):
                    fail("license_evidence_incomplete", "License document text does not match its immutable byte hash")
            if total > 4 * 1024 * 1024:
                fail("license_evidence_incomplete", "License evidence exceeds its bounded text budget")
            if not isinstance(remote.get("card_metadata", {}), dict):
                fail("license_evidence_incomplete", "Immutable card metadata must be an object")
            label = remote.get("card_metadata", {}).get("license", remote.get("license"))
            known_labels = {"mit", "apache-2.0", "cc0-1.0", "cc-by-4.0", "cc-by-sa-4.0", "bsd-3-clause", "bsd-2-clause", "gpl-3.0", "agpl-3.0", "openrail", "llama3", "llama3.1"}
            label_state = "ambiguous" if label is not None and not isinstance(label, str) else "unknown" if not label or label in {"unspecified", "unknown"} else "declared" if label in known_labels else "custom_or_unrecognized"
            evidence = {"schema_version": "modelbuilder.license-evidence/v1", "provider": p["provider"],
                        "repo_id": p["repo_id"], "repo_type": p["repo_type"], "commit": remote["commit"],
                        "declared_license": label, "label_state": label_state,
                        "card_metadata": remote.get("card_metadata", {}), "documents": documents, "source_file_manifest": remote["files"],
                        "gated": bool(remote.get("gated")), "gated_terms_state": "separate_acknowledgment_required" if remote.get("gated") else "not_gated",
                        "permissions_inferred_from_label": False, "captured_at": utcnow()}
            result = {**self._base(project, actor), "evidence_id": new_id(), "evidence": evidence, "evidence_digest": digest(evidence)}
            return self._record_result(op, p, actor, payload_hash, request_id, result, "hub_license_evidence", result["evidence_id"])
        if op == "hub.license.get":
            return self._get("hub_license_evidence", p["evidence_id"], project)
        if op == "hub.license.review":
            from .hub_contract import POLICY_FIELDS
            evidence = self._get("hub_license_evidence", p["evidence_id"], project)
            if p["evidence_digest"] != evidence["evidence_digest"]:
                fail("license_evidence_changed", "Review must bind the exact captured upstream license evidence")
            if not evidence["evidence"]["documents"]:
                fail("license_evidence_incomplete", "A label alone is insufficient; capture the actual authoritative license and notice text before review")
            if evidence["evidence"]["gated"] and not p["gated_terms_acknowledged"]:
                fail("license_terms_unacknowledged", "A token or gated access does not acknowledge the terms; review them separately")
            captured = {document["path"]: document for document in evidence["evidence"]["documents"]}
            if any(name not in captured or not captured[name]["text"].strip() for name in p["license_document_paths"]):
                fail("license_evidence_incomplete", "Explicitly identify the captured documents containing the actual applicable terms; a label is not license text")
            hashes = {document["sha256"] for document in evidence["evidence"]["documents"]}
            for notice in p["required_files"]:
                safe_name(notice["path"])
                if notice["sha256"] not in hashes:
                    fail("license_evidence_changed", "Preserved notice hashes must belong to the captured upstream documents")
            for attribution in p["required_attributions"]:
                safe_name(attribution["path"])
            result = {"schema_version": "modelbuilder.license-policy/v1", "project_id": project, "revision": 1,
                      "policy_id": new_id(), "source": {key: evidence["evidence"][key] for key in ("provider", "repo_id", "repo_type", "commit")},
                      "license_evidence_digest": evidence["evidence_digest"], "evidence_id": evidence["evidence_id"],
                      "source_files": evidence["evidence"]["source_file_manifest"], "verified_source_files": [],
                      **({"source_origin": copy.deepcopy(evidence["evidence"]["source_origin"])} if "source_origin" in evidence["evidence"] else {}),
                      "gated": evidence["evidence"]["gated"], "status": "reviewed", "reviewed_by": actor.id,
                      "reviewed_at": utcnow(), "unresolved_obligations": [],
                      **{key: copy.deepcopy(p[key]) for key in POLICY_FIELDS if key != "confirm_review"}}
            result["review_digest"] = digest(result)
            return self._record_result(op, p, actor, payload_hash, request_id, result, "hub_license_policy", result["policy_id"])
        if op == "hub.license.policy.list":
            return {"policies": list(self.policy_snapshot(project).values())}
        if op == "hub.dataset.policy.bind":
            reference = self.service._dataset_registry.portable_reference(project, p["dataset_id"], p["dataset_revision"])
            valid = self.service._dataset_registry.validate(project, dataset_id=p["dataset_id"], revision=p["dataset_revision"])
            if not valid["valid"]:
                fail("license_evidence_changed", "Registered dataset bytes no longer match its immutable source identity")
            sources = reference["manifest"]["sources"]
            if set(p["source_policies"]) != {item["id"] for item in sources}:
                fail("license_rights_unreviewed", "Explicit policy links must cover every registered dataset source exactly once")
            from .hub_rights import source_reference, validate_artifact_rights
            rights_sources = []
            policies = self.policy_snapshot(project)
            for item in sources:
                policy = self._get("hub_license_policy", p["source_policies"][item["id"]], project)
                hashes = {file.get("sha256") for file in policy.get("source_files", []) + policy.get("verified_source_files", [])}
                if item["sha256"] not in hashes:
                    fail("license_evidence_changed", "Registered dataset source bytes are outside the selected reviewed policy")
                rights_sources.append(source_reference(policy["source"], policy["license_evidence_digest"], policy["policy_id"]))
            rights = {"schema_version": "modelbuilder.artifact-rights/v1", "sources": rights_sources, "unresolved": [], "parent_rights_digests": []}
            admission = validate_artifact_rights(rights, p["intended_use"], policies, p["usage_context"])
            payload = {key: copy.deepcopy(p[key]) for key in ("project_id", "dataset_id", "dataset_revision", "source_policies", "intended_use", "usage_context")}
            payload.update(version_id=reference["version_id"], content_digest=reference["content_digest"], rights=rights)
            identity = digest({"project_id": project, "dataset_id": p["dataset_id"], "dataset_revision": p["dataset_revision"]})
            result = {**self._base(project, actor), **payload, "schema_version": "modelbuilder.dataset-rights-binding/v1",
                      "binding_id": identity, "binding_digest": digest(payload), "admission": admission}
            return self._record_result(op, p, actor, payload_hash, request_id, result, "hub_dataset_policy", identity)
        if op == "hub.license.policy.get":
            return self._get("hub_license_policy", p["policy_id"], project)
        if op == "hub.license.revoke":
            with self.store.transaction():
                self._authority(op, actor, project)
                previous = self.store.idempotent_result(actor.id, p["idempotency_key"], op, payload_hash)
                if previous is not None:
                    return previous
                row = self._get("hub_license_policy", p["policy_id"], project)
                self.service._revision(row, p["expected_revision"])
                result = {**row, "status": "revoked", "revision": row["revision"] + 1,
                          "revoked_by": actor.id, "revoked_at": utcnow(), "revocation_reason": p["reason"]}
                result["review_digest"] = digest({key: value for key, value in result.items() if key != "review_digest"})
                self.store.put("hub_license_policy", row["policy_id"], project, result, row["revision"])
                self.store.save_idempotent(actor.id, p["idempotency_key"], op, payload_hash, result)
                self.store.audit(actor_id=actor.id, operation=op, request_id=request_id, outcome="succeeded", project_id=project, target_id=row["policy_id"])
            return result
        if op == "hub.providers":
            return {"providers": [provider.capabilities() for provider in self.providers.values()],
                    "accounts": [alias for alias, account in self.accounts.items() if account.get("project_ids") is None or project in account["project_ids"]],
                    "publication_sources": sorted(self.publication_roots.get(project, {})), "max_bytes": self.byte_budget(project), "max_supported_bytes": MAX_BYTES,
                    "publisher_scope_required": True, "base_model_optional": True}
        if op in {"hub.search", "hub.inspect"}:
            result = self._inspect(p) if op.endswith("inspect") else self._call("search", p)
            with self.store.transaction():
                self._authority(op, actor, project)
            return result
        if op == "hub.repository.compare":
            remote = self._inspect(p)
            root = self._source(project, p["source_alias"])
            listing = self.invoke("hub.local.files", {"project_id": project, "source_alias": p["source_alias"]}, actor, "", request_id)
            remote_files = {item["path"]: item for item in remote["files"]}
            changes, total = [], 0
            for item in listing["files"]:
                total += item["size_bytes"]
                if total > min(self.byte_budget(p["project_id"]), p["max_bytes"]):
                    fail("hub_budget_exceeded", "Local comparison exceeds its approved byte budget")
                local = {"path": item["path"], **fingerprint(root / item["path"], item["size_bytes"])}
                prior = remote_files.pop(item["path"], None)
                state = "added" if prior is None else "unchanged" if self._matches(prior, local) else "modified"
                changes.append({"path": item["path"], "state": state, "local": local, "remote": prior})
            changes += [{"path": name, "state": "remote_only", "local": None, "remote": item} for name, item in sorted(remote_files.items())]
            with self.store.transaction():
                self._authority(op, actor, project)
            return {"provider": p["provider"], "repo_id": p["repo_id"], "repo_type": p["repo_type"],
                    "commit": remote["commit"], "source_alias": p["source_alias"], "changes": changes,
                    "total_local_bytes": total, "deletion_supported": False}
        if op == "hub.asset.manifest":
            asset = self._get("hub_asset", p["asset_id"], project)
            self._verify_asset(asset)
            return {"schema_version": "modelbuilder.hub-asset/v1", **{key: asset[key] for key in
                    ("asset_id", "provider", "repo_type", "repo_id", "commit", "license", "private", "files", "resources", "license_provenance", "license_evidence_digest", "rights")}}
        if op == "hub.publish.receipt":
            transfer = self.status(p["transfer_id"], project)
            if transfer["kind"] != "publish" or transfer["state"] != "succeeded":
                fail("hub_receipt_unavailable", "A publication receipt requires a verified immutable commit")
            preview = self._get("hub_preview", transfer["preview_id"], project)
            return {"schema_version": "modelbuilder.hub-publication/v1", "payload_digest": preview["payload_digest"],
                    "commit": transfer["result"]["commit"], "verified": True,
                    **{key: preview["payload"][key] for key in ("provider", "repo_id", "repo_type", "visibility", "license", "files", "change_effects", "parent_commit", "rights", "license_admission")}}
        if op == "hub.download.plan":
            inspected = self._inspect(p)
            policy = self._get("hub_license_policy", p["license_policy_id"], project)
            evidence = self._get("hub_license_evidence", policy["evidence_id"], project)
            from .hub_rights import source_reference, validate_artifact_rights
            rights = {"schema_version": "modelbuilder.artifact-rights/v1", "sources": [source_reference({**p, "commit": inspected["commit"]}, evidence["evidence_digest"], policy["policy_id"])], "unresolved": [], "parent_rights_digests": []}
            validate_artifact_rights(rights, "download", {policy["policy_id"]: policy}, policy["reviewed_usage_context"])
            if inspected.get("access_state") not in {"available", "gated_authorized"}:
                fail("hub_access_required", "Provider access must be resolved before download")
            available = {item["path"]: item for item in inspected["files"]}
            if any(name not in available for name in p["files"]):
                fail("hub_file_missing", "A selected file is absent from the immutable repository")
            required_documents = {document["path"] for document in evidence["evidence"]["documents"]}
            if not required_documents.issubset(available):
                fail("license_evidence_changed", "Upstream licensing files changed; capture the immutable evidence again")
            files = [available[name] for name in sorted(set(p["files"]) | required_documents)]
            plan_id = new_id()
            payload = {key: p[key] for key in ("project_id", "provider", "repo_id", "repo_type", "account_alias", "max_bytes") if key in p}
            payload.update(commit=inspected["commit"], files=files, total_bytes=self._bound(files, p["max_bytes"], p["project_id"]),
                           license=inspected.get("license", "unspecified"), private=bool(inspected.get("private")), gated=bool(inspected.get("gated")),
                           license_provenance=evidence["evidence"], license_evidence_digest=evidence["evidence_digest"],
                           license_policy_id=policy["policy_id"], rights=rights, usage_context=policy["reviewed_usage_context"])
            result = {**self._base(project, actor), "plan_id": plan_id, "payload": payload, "plan_digest": digest(payload)}
            return self._record_result(op, p, actor, payload_hash, request_id, result, "hub_plan", plan_id)
        if op == "hub.download.start":
            plan = self._get("hub_plan", p["plan_id"], project, actor)
            if p["plan_digest"] != plan["plan_digest"]:
                fail("hub_plan_changed", "Download plan digest does not match")
            return self._start(op, p, actor, payload_hash, request_id, "download", plan)
        if op == "hub.transfer.get":
            return self.status(p["transfer_id"], project)
        if op == "hub.transfer.list":
            return {"transfers": [self.status(row["transfer_id"], project) for row in self.store.list("hub_transfer", project)]}
        if op == "hub.transfer.cancel":
            with self._dispatch:
                row = self.status(p["transfer_id"], project)
                with self.store.transaction():
                    self._authority(op, actor, project)
                    row = self._get("hub_transfer", p["transfer_id"], project, actor)
                    if row["state"] not in TERMINAL:
                        row = self._put("hub_transfer", row["transfer_id"], row, cancel_requested=True)
                return self._record_result(op, p, actor, payload_hash, request_id, row)
        if op == "hub.transfer.retry":
            row = self.status(p["transfer_id"], project)
            if row["kind"] != "download" or row["state"] not in {"failed", "cancelled", "interrupted"}:
                fail("hub_retry_unsafe", "Only stopped downloads can retry; uncertain publication requires reconciliation")
            plan = self._get("hub_plan", row["plan_id"], project, actor)
            return self._start(op, p, actor, payload_hash, request_id, "download", plan, previous=row)
        if op == "hub.asset.list":
            return {"assets": self.store.list("hub_asset", project)}
        if op == "hub.asset.get":
            return self._get("hub_asset", p["asset_id"], project)
        if op == "hub.asset.bind":
            asset = self._get("hub_asset", p["asset_id"], project)
            self._verify_asset(asset)
            with self.store.transaction():
                self._authority(op, actor, project)
                recipe = self.service._current_recipe(project)
                self.service._revision(recipe, p["expected_revision"])
                body = copy.deepcopy(recipe)
                if "usage_context" in p:
                    if p["usage_context"] != asset["usage_context"]:
                        fail("license_evidence_changed", "Selected usage context differs from the asset's reviewed acquisition context")
                    extensions = body.setdefault("extensions", {})
                    if extensions.get("usage_context", p["usage_context"]) != p["usage_context"]:
                        fail("license_rights_unreviewed", "Existing recipe usage context differs; explicitly review all inputs for the intended context before changing it")
                    extensions["usage_context"] = copy.deepcopy(p["usage_context"])
                selected = {item["id"]: item for item in asset["resources"]}
                body["resources"] = [item for item in body["resources"] if item["id"] not in selected] + list(selected.values())
                result = self.service._save_recipe(body, project, recipe["recipe_id"], recipe["revision"] + 1, recipe["revision"])
                self.store.save_idempotent(actor.id, p["idempotency_key"], op, payload_hash, result)
                self.store.audit(actor_id=actor.id, operation=op, request_id=request_id, outcome="succeeded", project_id=project, target_id=recipe["recipe_id"])
                return result
        if op == "hub.local.files":
            root = self._source(project, p["source_alias"])
            files = []
            for directory_name, directories, names in os.walk(root, followlinks=False):
                if any((Path(directory_name) / name).is_symlink() for name in directories):
                    fail("hub_unsafe_path", "Publication source contains a symlink directory")
                for name in sorted(names):
                    path = Path(directory_name) / name
                    relative = safe_name(path.relative_to(root).as_posix())
                    if len(files) >= MAX_FILES:
                        fail("hub_budget_exceeded", "Publication source has too many files")
                    with safe_file(path, max_bytes=MAX_BYTES) as source:
                        files.append({"path": relative, "size_bytes": os.fstat(source.fileno()).st_size})
            if sum(item["size_bytes"] for item in files) > MAX_BYTES:
                fail("hub_budget_exceeded", "Local inventory exceeds the supported byte ceiling")
            return {"source_alias": p["source_alias"], "files": files}
        if op == "hub.publish.preview":
            self._expire_previews()
            result = self._preview(p, actor)
            try:
                saved = self._record_result(op, p, actor, payload_hash, request_id, result, "hub_preview", result["preview_id"])
            except BaseException:
                shutil.rmtree(self.root / "publications" / result["preview_id"], ignore_errors=True)
                raise
            if saved["preview_id"] != result["preview_id"]:
                shutil.rmtree(self.root / "publications" / result["preview_id"], ignore_errors=True)
            return saved
        if op == "hub.publish.discard":
            with self.store.transaction():
                self._authority(op, actor, project)
                preview = self._get("hub_preview", p["preview_id"], project, actor)
                if any(row.get("preview_id") == p["preview_id"] and row["state"] not in TERMINAL for row in self.store.list("hub_transfer", project)):
                    fail("hub_preview_in_use", "This publication snapshot has an active transfer")
                preview = self._put("hub_preview", p["preview_id"], preview, discarded=True)
            shutil.rmtree(self.root / "publications" / p["preview_id"], ignore_errors=True)
            return self._record_result(op, p, actor, payload_hash, request_id, {"preview_id": p["preview_id"], "discarded": True})
        if op == "hub.publish.approve":
            preview = self._get("hub_preview", p["preview_id"], project)
            if preview.get("discarded") or preview.get("expires_at", "") <= utcnow():
                fail("hub_preview_expired", "Publication preview expired or was discarded; prepare a new preview")
            if p["payload_digest"] != preview["payload_digest"]:
                fail("hub_preview_changed", "Publication preview digest does not match")
            result = {**self._base(project, actor), "approval_id": new_id(), "preview_id": p["preview_id"],
                      "payload_digest": p["payload_digest"], "consumed": False,
                      "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
                      "authorization": "authorized_publisher_grant"}
            return self._record_result(op, p, actor, payload_hash, request_id, result, "hub_approval", result["approval_id"])
        if op == "hub.publish.start":
            preview = self._get("hub_preview", p["preview_id"], project)
            if preview.get("discarded") or preview.get("expires_at", "") <= utcnow():
                fail("hub_preview_expired", "Publication preview expired or was discarded; prepare a new preview")
            if p["payload_digest"] != preview["payload_digest"]:
                fail("hub_preview_changed", "Publication preview digest does not match")
            return self._start(op, p, actor, payload_hash, request_id, "publish", preview)
        if op == "hub.publish.reconcile":
            row = self.status(p["transfer_id"], project)
            if row["kind"] != "publish" or row["state"] != "needs_reconciliation":
                fail("hub_reconcile_unavailable", "Only uncertain publications need reconciliation")
            preview = self._get("hub_preview", row["preview_id"], project)
            result = self._call("reconcile", preview["payload"])
            with self.store.transaction():
                self._authority(op, actor, project)
                current = self._get("hub_transfer", row["transfer_id"], project)
                if result.get("verified") and self._provider(preview["payload"]).immutable_revision(result.get("commit")):
                    current = self._put("hub_transfer", row["transfer_id"], current, state="succeeded", result=result)
            return self._record_result(op, p, actor, payload_hash, request_id, current)
        fail("unknown_operation", "Unknown Hub operation", 404)

    def _source(self, project, alias):
        path = self.publication_roots.get(project, {}).get(alias)
        if not path:
            fail("hub_source_unavailable", "Choose an operator-approved publication source alias", 403)
        if not Path(path).is_dir():
            fail("hub_source_unavailable", "Publication source directory is unavailable", 404)
        return directory(path)

    def _preview(self, p, actor):
        if bool(p.get("source_alias")) == bool(p.get("asset_id")):
            fail("validation_error", "Choose exactly one publication source alias or verified asset", 400)
        self._provider(p)
        if p.get("asset_id"):
            asset = self._get("hub_asset", p["asset_id"], p["project_id"])
            if asset["repo_type"] != p["repo_type"]:
                fail("hub_type_mismatch", "Asset and destination repository types differ")
            self._verify_asset(asset)
            root = self.root / "assets" / asset["asset_id"]
        else:
            root = self._source(p["project_id"], p["source_alias"])
        inventory, total = [], 0
        for name in p["files"]:
            safe_name(name)
            with safe_file(root / name, max_bytes=min(self.byte_budget(p["project_id"]), p["max_bytes"]) - total) as handle:
                size = os.fstat(handle.fileno()).st_size
            inventory.append({"path": name, "size_bytes": size})
            total += size
        self._bound(inventory, p["max_bytes"], p["project_id"])
        from .hub_storage import reserve_space
        with reserve_space(self.root, total):
            return self._snapshot_preview(p, actor, root, {item["path"]: item["size_bytes"] for item in inventory})

    def _snapshot_preview(self, p, actor, root, reserved_sizes):
        preview_id = new_id()
        directory(self.root / "publications")
        target = self.root / "publications" / preview_id
        target.mkdir(mode=0o700)
        try:
            files, total, names = [], 0, set()
            for name in sorted(p["files"]):
                safe_name(name)
                if name.casefold() in names:
                    fail("hub_unsafe_path", "Publication paths collide")
                names.add(name.casefold())
                from .hub_storage import check_free_space
                check_free_space(self.root, reserved_sizes[name])
                item = {"path": name, **fingerprint(root / name, min(reserved_sizes[name], min(self.byte_budget(p["project_id"]), p["max_bytes"]) - total), target / name)}
                files.append(item)
                total += item["size_bytes"]
            self._bound(files, p["max_bytes"], p["project_id"])
            destination = self._call("destination", p)
            create = p.get("create_repo", False)
            if destination.get("exists"):
                if create or destination.get("private") != (p["visibility"] == "private"):
                    fail("hub_destination_changed", "Existing destination visibility or creation choice differs; preview again")
                if not self._provider(p).immutable_revision(destination.get("commit")):
                    fail("hub_response_invalid", "Destination has no immutable parent commit")
                if p.get("expected_remote_commit") and p["expected_remote_commit"] != destination["commit"]:
                    fail("hub_destination_changed", "Remote revision changed after staging; refresh the repository comparison")
            elif not create:
                fail("hub_destination_unavailable", "Destination is missing or inaccessible; explicit new-repository approval is required")
            card = ""
            if "README.md" in p["files"]:
                with safe_file(target / "README.md", max_bytes=1024 * 1024) as stream:
                    card = stream.read(1024 * 1024).decode("utf-8")
                metadata = self._call("card.inspect", {"project_id": p["project_id"], "provider": p["provider"], "card": card})
                if metadata.get("license") != p["license"]:
                    fail("hub_license_mismatch", "README.md must declare exactly the previewed license in its metadata")
            elif create or destination.get("license") != p["license"]:
                fail("hub_license_required", "Select a README.md with the approved license, or preserve the existing repository card license")
            old = {item["path"]: item for item in destination.get("files", [])}
            changes = [{"path": item["path"], "effect": ("unchanged" if self._matches(old[item["path"]], item) else "replace") if item["path"] in old else "add",
                        "previous": old.get(item["path"])} for item in files]
            payload = {key: p[key] for key in ("project_id", "provider", "repo_id", "repo_type", "account_alias", "visibility", "license", "max_bytes")}
            rights, policies = self._publication_rights(p)
            from .hub_rights import validate_publication_rights
            license_admission = validate_publication_rights(rights, policies, files, target, p["license"], p["usage_context"])
            policies = {identity: policies[identity] for identity in license_admission["policy_ids"]}
            payload.update(usage_context=p["usage_context"], rights=rights, reviewed_license_policies=policies, license_admission=license_admission, files=files, total_bytes=total, create_repo=create, parent_commit=destination.get("commit"),
                           change_effects=changes, deletes=[], visibility_change=False,
                           commit_marker=f"ModelBuilder publication {preview_id}")
            return {**self._base(p["project_id"], actor), "preview_id": preview_id, "payload": payload,
                    "payload_digest": digest(payload), "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(), "approval_required": "publish_scope", "card_preview": card[:16000],
                    "card_preview_truncated": len(card) > 16000, "license_source": "staged_card" if card else "existing_remote_card", "source": {key: p[key] for key in ("source_alias", "asset_id") if key in p}}
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise

    def _publication_rights(self, payload):
        from .hub_rights import blocked
        identities = [payload["asset_id"]] if payload.get("asset_id") else payload.get("source_asset_ids", [])
        if not identities:
            blocked("Declare all upstream model/dataset assets or a reviewed locally-authored source before publishing; unknown lineage is unresolved")
        sources, unresolved = [], []
        for identity in identities:
            asset = self._get("hub_asset", identity, payload["project_id"])
            rights = asset.get("rights")
            if not rights:
                unresolved.append({"artifact_id": identity, "reason": "missing_license_provenance"})
            else:
                sources.extend(rights.get("sources", []))
                unresolved.extend(rights.get("unresolved", []))
        envelope = {"schema_version": "modelbuilder.artifact-rights/v1", "sources": sources, "unresolved": unresolved, "parent_rights_digests": []}
        policies = {row["policy_id"]: row for row in self.store.list("hub_license_policy", payload["project_id"])}
        # Derived exports carry an additional authoritative declaration that cannot
        # be replaced by the shorter UI list. Unknown parent rights remain blocking.
        if payload.get("source_alias"):
            manifest_path = self._source(payload["project_id"], payload["source_alias"]) / "export.manifest.json"
            if manifest_path.exists():
                with safe_file(manifest_path, max_bytes=4 * 1024 * 1024) as handle:
                    manifest = json.load(handle)
                rights = manifest.get("rights")
                if not isinstance(rights, dict):
                    envelope["unresolved"].append({"reason": "export_manifest_missing_rights"})
                else:
                    envelope["sources"].extend(rights.get("sources", []))
                    envelope["unresolved"].extend(rights.get("unresolved", []))
                    envelope["parent_rights_digests"].append(digest(rights))
        return envelope, policies

    def _expire_previews(self):
        """Expired derived snapshots are disposable; receipts and source bytes stay."""
        now = utcnow()
        snapshots = self.store.list("hub_preview")
        active = {row.get("preview_id") for row in self.store.list("hub_transfer") if row["state"] not in TERMINAL}
        for preview in snapshots:
            if preview.get("expires_at", now) <= now and preview["preview_id"] not in active:
                shutil.rmtree(self.root / "publications" / preview["preview_id"], ignore_errors=True)

    def _lock(self, identity):
        return TransferLock(self.root / "locks" / f"{identity}.lock")

    def _start(self, op, p, actor, payload_hash, request_id, kind, intent, previous=None):
        with self._dispatch:
            if not self._slots.acquire(blocking=False):
                fail("hub_busy", "Hub transfer capacity is busy; retry explicitly", 503)
            identity = previous["transfer_id"] if previous else new_id()
            lock = None
            try:
                lock = self._lock(identity)
                if self._bound(intent["payload"]["files"], intent["payload"]["max_bytes"], p["project_id"]) != intent["payload"]["total_bytes"]:
                    fail("hub_plan_changed", "Stored transfer total differs from its approved file inventory")
                with self.store.transaction():
                    self._authority(op, actor, p["project_id"])
                    cached = self.store.idempotent_result(actor.id, p["idempotency_key"], op, payload_hash)
                    if cached is not None:
                        lock.close()
                        self._slots.release()
                        return cached
                    if kind == "publish":
                        approval = self._get("hub_approval", p["approval_id"], p["project_id"], actor)
                        if approval["consumed"] or approval["preview_id"] != intent["preview_id"] or approval["payload_digest"] != intent["payload_digest"] or approval["expires_at"] <= utcnow():
                            fail("hub_approval_invalid", "Publication approval is expired, used or bound to a different payload", 403)
                        self._put("hub_approval", approval["approval_id"], approval, consumed=True, transfer_id=identity)
                    result = {**self._base(p["project_id"], actor), "transfer_id": identity, "kind": kind, "state": "running",
                              "plan_id" if kind == "download" else "preview_id": intent["plan_id" if kind == "download" else "preview_id"],
                              "total_bytes": intent["payload"]["total_bytes"], "verified_bytes": previous.get("verified_bytes", 0) if previous else 0,
                              "completed_files": previous.get("completed_files", []) if previous else [], "cancel_requested": False,
                              "attempt": previous["attempt"] + 1 if previous else 1, "phase": "preparing"}
                    if previous:
                        current = self._get("hub_transfer", identity, p["project_id"], actor)
                        if current["state"] not in {"failed", "cancelled", "interrupted"}:
                            fail("hub_retry_unsafe", "Transfer is no longer retryable")
                        result = self._put("hub_transfer", identity, current, **{key: value for key, value in result.items() if key not in {"revision", "created_at"}})
                    else:
                        self.store.put("hub_transfer", identity, p["project_id"], result)
                    self.store.save_idempotent(actor.id, p["idempotency_key"], op, payload_hash, result)
                    self.store.audit(actor_id=actor.id, operation=op, request_id=request_id, outcome="succeeded", project_id=p["project_id"], target_id=identity)
                thread = threading.Thread(target=self._run, args=(result, intent, actor, op, lock), daemon=True,
                                          name=f"hub-transfer-{identity}")
                self._threads[identity] = thread
                thread.start()
                return result
            except Exception:
                if lock:
                    lock.close()
                self._slots.release()
                raise

    def status(self, identity, project):
        with self._dispatch:
            row = self._get("hub_transfer", identity, project)
            if row["state"] not in TERMINAL:
                try:
                    lock = self._lock(identity)
                except BlockingIOError:
                    return row
                try:
                    with self.store.transaction():
                        row = self._get("hub_transfer", identity, project)
                        if row["state"] not in TERMINAL:
                            row = self._put("hub_transfer", identity, row,
                                            state="needs_reconciliation" if row["kind"] == "publish" and row["phase"] == "transmitting" else "interrupted")
                finally:
                    lock.close()
            return row

    def _progress(self, identity, project, **changes):
        with self.store.transaction():
            row = self._get("hub_transfer", identity, project)
            return self._put("hub_transfer", identity, row, **changes)

    def _run(self, row, intent, actor, op, lock):
        identity, project = row["transfer_id"], row["project_id"]
        transmitting = False

        def cancelled():
            current = self._get("hub_transfer", identity, project)
            try:
                self._authority(op, actor, project)
            except ServiceError:
                return True
            return current["cancel_requested"]

        try:
            if cancelled():
                raise HubCancelled()
            self._provider(intent["payload"])
            if row["kind"] == "download":
                asset = self._download(row, intent, cancelled)
                with self.store.transaction():
                    self._authority(op, actor, project)
                    current = self._get("hub_transfer", identity, project)
                    if current["cancel_requested"]:
                        raise HubCancelled()
                    from .hub_rights import validate_artifact_rights
                    validate_artifact_rights(intent["payload"]["rights"], "download", self.policy_snapshot(project), intent["payload"]["usage_context"])
                    self._record_materialized_hashes(asset)
                    self.store.put("hub_asset", asset["asset_id"], project, asset)
                    self._put("hub_transfer", identity, current, state="succeeded", phase="verified", asset_id=asset["asset_id"], verified_bytes=row["total_bytes"])
                self._register_root(asset)
            else:
                payload = intent["payload"]
                snapshot = self.root / "publications" / intent["preview_id"]
                for item in payload["files"]:
                    actual = fingerprint(snapshot / item["path"], item["size_bytes"])
                    if any(actual[key] != item[key] for key in actual):
                        fail("hub_file_changed", "Publication snapshot changed after approval")
                from .hub_rights import validate_publication_rights
                policies = {item["policy_id"]: item for item in self.store.list("hub_license_policy", project)}
                validate_publication_rights(payload["rights"], policies, payload["files"], snapshot, payload["license"], payload["usage_context"])
                if cancelled():
                    raise HubCancelled()
                self._progress(identity, project, phase="transmitting")
                transmitting = True
                result = self._call("publish", {**payload, "snapshot_dir": str(snapshot)}, cancelled=cancelled)
                if not result.get("verified") or not self._provider(payload).immutable_revision(result.get("commit")):
                    fail("hub_verification_failed", "Publication has no verified immutable commit")
                self._progress(identity, project, state="succeeded", phase="verified", result=result, verified_bytes=row["total_bytes"])
        except HubCancelled:
            self._progress(identity, project, state="needs_reconciliation" if transmitting else "cancelled")
        except Exception as error:
            code = error.code if isinstance(error, ServiceError) else "hub_transfer_failed"
            self._progress(identity, project, state="needs_reconciliation" if transmitting and code != "hub_destination_changed" else "failed", error={"code": code, "message": "Hub transfer stopped; inspect its status before retrying"})
        finally:
            lock.close()
            self._slots.release()

    def _download(self, row, intent, cancelled):
        payload = intent["payload"]
        from .hub_rights import validate_artifact_rights
        validate_artifact_rights(payload["rights"], "download", self.policy_snapshot(row["project_id"]), payload["usage_context"])
        if self._bound(payload["files"], payload["max_bytes"], row["project_id"]) != payload["total_bytes"]:
            fail("hub_plan_changed", "Approved transfer inventory changed")
        from .hub_storage import reserve_space
        with reserve_space(self.root, payload["total_bytes"] * 2):
            return self._download_files(row, intent, cancelled)

    def _download_files(self, row, intent, cancelled):
        payload, identity = intent["payload"], row["transfer_id"]
        staging = directory(self.root / "staging" / identity)
        ready = directory(staging / "verified")
        completed, verified = [], 0
        for item in payload["files"]:
            if cancelled():
                raise HubCancelled()
            name = item["path"]
            final = ready / name
            valid = False
            if final.exists():
                actual = fingerprint(final, item["size_bytes"])
                valid = self._matches(item, actual)
                if not valid:
                    final.unlink()
            if not valid:
                from .hub_storage import check_free_space
                check_free_space(self.root, item["size_bytes"] * 2)
                self._progress(identity, row["project_id"], phase="downloading", active_file=name)
                # SDK local_dir is never reused or supplied by a caller. Resume
                # keeps canonical verified files; SDK in-call range retries stay
                # inside this exclusive private attempt directory.
                with tempfile.TemporaryDirectory(prefix=".sdk-attempt-", dir=staging) as attempt:
                    result = self._call("download", {**payload, "files": [item], "staging_dir": attempt}, cancelled=cancelled)
                    returned = result.get("files", [])
                    if len(returned) != 1 or returned[0].get("path") != name:
                        fail("hub_response_invalid", "Provider did not return the exact planned file")
                    local = Path(returned[0]["local_path"])
                    if local.absolute() != (Path(attempt) / name).absolute():
                        fail("hub_unsafe_path", "Provider returned a file outside its owned staging destination")
                    directory(final.parent)
                    temporary = final.with_name(final.name + ".verified-part")
                    temporary.unlink(missing_ok=True)
                    try:
                        actual = fingerprint(local, item["size_bytes"], temporary)
                        if not self._matches(item, actual):
                            fail("hub_hash_mismatch", "Downloaded file does not match the immutable provider hash")
                    except BaseException:
                        temporary.unlink(missing_ok=True)
                        raise
                os.replace(temporary, final)
                sync_directory(final.parent)
            completed.append({"path": name, **actual})
            verified += actual["size_bytes"]
            self._progress(identity, row["project_id"], phase="verifying", completed_files=completed, verified_bytes=verified)
        if cancelled():
            raise HubCancelled()
        asset_id = digest({"project_id": row["project_id"], "plan_digest": intent["plan_digest"], "transfer_id": identity})
        destination = self.root / "assets" / asset_id
        directory(destination.parent)
        if destination.exists():
            for item in completed:
                if fingerprint(destination / item["path"], item["size_bytes"]) != {key: item[key] for key in ("size_bytes", "sha256", "git_blob_sha1")}:
                    fail("hub_asset_changed", "An existing asset destination has changed")
        else:
            os.rename(ready, destination)
            sync_directory(destination.parent)
        return self._asset_record(row, payload, completed, asset_id, verified)

    def _record_materialized_hashes(self, asset):
        """Writer-transaction only: derive SHA256 from the approved provider hash.

        This adds integrity evidence, never permissions or a new legal decision.
        The new policy revision invalidates any stale prepared execution receipt.
        """
        for source in asset["rights"]["sources"]:
            policy = self._get("hub_license_policy", source["license_policy_id"], asset["project_id"])
            expected = {item["path"]: item for item in policy["source_files"]}
            verified = {item["path"]: item for item in policy.get("verified_source_files", [])}
            changed = False
            for item in asset["files"]:
                if item["path"] not in expected or not self._matches(expected[item["path"]], item):
                    fail("license_evidence_changed", "Materialized bytes are outside the exact reviewed source file closure")
                if expected[item["path"]].get("sha256"):
                    continue
                if item["path"] in verified and verified[item["path"]] != item:
                    fail("license_evidence_changed", "A reviewed source has conflicting materialized content hashes")
                if item["path"] not in verified:
                    verified[item["path"]] = item
                    changed = True
            if changed:
                updated = {**policy, "revision": policy["revision"] + 1, "verified_source_files": [verified[name] for name in sorted(verified)],
                           "materialization_verified_at": utcnow(), "materialization_changes_permissions": False}
                updated["review_digest"] = digest({key: value for key, value in updated.items() if key != "review_digest"})
                self.store.put("hub_license_policy", policy["policy_id"], asset["project_id"], updated, policy["revision"])

    def _asset_record(self, row, payload, completed, asset_id, verified):
        resources = []
        for index, item in enumerate(completed):
            resources.append({"id": f"hub_{asset_id[:24]}_{index}", "kind": payload["repo_type"],
                              "url": f"urn:sha256:{item['sha256']}",
                              "revision": payload["commit"], "sha256": item["sha256"], "license": payload["license"],
                              "private": payload["private"], "redistribution_allowed": False, "required": True,
                              "metadata": {"hub_asset_id": asset_id, "path": item["path"], "provider": payload["provider"], "repo_id": payload["repo_id"], "commit": payload["commit"],
                                           "license_source": {key: payload[key] for key in ("provider", "repo_type", "repo_id", "commit")},
                                           "license_evidence_digest": payload["license_evidence_digest"], "license_policy_id": payload["license_policy_id"], "rights": payload["rights"]}})
        return {"schema_version": "modelbuilder.hub-asset/v1", "asset_id": asset_id, "project_id": row["project_id"], "revision": 1, "created_at": utcnow(),
                "provider": payload["provider"], "repo_id": payload["repo_id"], "repo_type": payload["repo_type"],
                "commit": payload["commit"], "license": payload["license"], "private": payload["private"],
                "license_provenance": payload["license_provenance"], "license_evidence_digest": payload["license_evidence_digest"],
                "rights": payload["rights"], "usage_context": payload["usage_context"], "files": completed, "total_bytes": verified, "resources": resources, "dataset_root_alias": "hub_" + asset_id,
                "usage": "Verified inert files; choose a compatible recipe component or explicitly register dataset splits"}

    @staticmethod
    def _matches(expected, actual):
        return expected["size_bytes"] == actual["size_bytes"] and bool(
            (expected.get("sha256") and expected["sha256"] == actual["sha256"]) or
            (expected.get("git_blob_sha1") and expected["git_blob_sha1"] == actual["git_blob_sha1"]))

    def _verify_asset(self, asset):
        for item in asset["files"]:
            actual = fingerprint(self.root / "assets" / asset["asset_id"] / item["path"], item["size_bytes"])
            if actual != {key: item[key] for key in actual}:
                fail("hub_asset_changed", "Verified Hub asset bytes are missing or changed")

    def _register_root(self, asset):
        if asset["repo_type"] != "dataset":
            return
        path = self.root / "assets" / asset["asset_id"]
        if not path.is_dir() or path.is_symlink():
            return
        info = path.stat()
        if os.name == "nt":
            from .platform_file_access import snapshot_windows_root
            captured = snapshot_windows_root(path)
        else:
            captured = (path, info.st_dev, info.st_ino)
        self.service._dataset_registry._roots.setdefault(asset["project_id"], {})[asset["dataset_root_alias"]] = captured

    def dataset_rights(self, reference):
        """Derive dataset bindings only from exact project-owned verified asset roots."""
        identity = digest({"project_id": reference["project_id"], "dataset_id": reference["dataset_id"], "dataset_revision": reference["revision"]})
        try:
            binding = self._get("hub_dataset_policy", identity, reference["project_id"])
        except ServiceError as error:
            if error.code != "not_found":
                raise
        else:
            if binding["version_id"] != reference["version_id"] or binding["content_digest"] != reference["content_digest"]:
                fail("license_evidence_changed", "Dataset rights binding differs from the registered immutable version")
            return copy.deepcopy(binding["rights"])
        sources, unresolved, parents = [], [], []
        found = False
        for item in reference["manifest"]["sources"]:
            alias = item["root_id"]
            if not alias.startswith("hub_"):
                unresolved.append({"source_id": item["id"], "reason": "dataset_source_has_no_reviewed_asset_reference"})
                continue
            asset = self._get("hub_asset", alias[4:], reference["project_id"])
            match = next((file for file in asset["files"] if file["path"] == item["path"] and file["sha256"] == item["sha256"]), None)
            if asset["repo_type"] != "dataset" or not match:
                fail("license_evidence_changed", "Dataset source differs from its exact reviewed asset file")
            found = True
            rights = asset["rights"]
            sources.extend(rights["sources"])
            unresolved.extend(rights["unresolved"])
            parents.append(digest(rights))
        if not found:
            return None  # Missing metadata remains visibly unresolved at admission.
        return {"schema_version": "modelbuilder.artifact-rights/v1", "sources": sources,
                "unresolved": unresolved, "parent_rights_digests": sorted(set(parents))}

    def policy_snapshot(self, project_id):
        """Internal authority only: callers must never supply this map in payloads."""
        return {row["policy_id"]: row for row in self.store.list("hub_license_policy", project_id)}

    def resolve_backend(self, backend, payload):
        payload.pop("reviewed_license_policies", None)
        if hasattr(backend, "with_execution_rights"):
            backend = backend.with_execution_rights(project_id=payload["project_id"],
                                                   policies=self.policy_snapshot(payload["project_id"]),
                                                   policy_store_path=self.store.path)
        else:
            recipe = payload.get("recipe", {})
            if any(item.get("kind") in {"model", "dataset", "checkpoint"} for item in recipe.get("resources", []) + recipe.get("external_dependencies", [])) or recipe.get("extensions", {}).get("artifact_rights") is not None:
                fail("capability_unavailable", "Backend lacks authoritative declared-input rights admission", 503)
        paths = {}
        for item in payload.get("recipe", {}).get("resources", []) + payload.get("recipe", {}).get("external_dependencies", []):
            asset_id = item.get("metadata", {}).get("hub_asset_id")
            if not asset_id:
                continue
            asset = self._get("hub_asset", asset_id, payload["project_id"])
            if item not in asset["resources"]:
                fail("hub_asset_changed", "Recipe resource differs from its verified Hub asset")
            path = self.root / "assets" / asset_id / item["metadata"]["path"]
            actual = fingerprint(path, MAX_BYTES)
            if actual["sha256"] != item["sha256"]:
                fail("hub_asset_changed", "Recipe Hub asset hash changed")
            paths[item["id"]] = str(path)
        if paths:
            if not hasattr(backend, "with_dataset_dependencies"):
                fail("capability_unavailable", "Backend cannot resolve project Hub resource paths", 503)
            return backend.with_dataset_dependencies(paths, getattr(backend, "dataset_source_guards", {}))
        return backend

    def close(self):
        # Shutdown requests cancellation and waits for SDK child termination;
        # uncertain publication remains reconcilable, never reported cancelled.
        for identity, thread in list(self._threads.items()):
            if thread.is_alive():
                row = self.store.get("hub_transfer", identity)
                self._progress(identity, row["project_id"], cancel_requested=True)
        for thread in list(self._threads.values()):
            thread.join(timeout=5)
