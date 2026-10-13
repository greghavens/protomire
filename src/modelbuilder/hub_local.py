"""Reviewed local artifact acquisition using operator aliases, not arbitrary paths."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile

from .export_packages import safe_file
from .hub import digest, directory, fail, fingerprint, safe_name, sync_directory
from .hub_rights import source_reference, validate_artifact_rights
from .models import new_id, utcnow


def inspect_local(hub, p, actor):
    root = hub._source(p["project_id"], p["source_alias"])
    files, documents, total = [], [], 0
    names = set()
    explicit_documents = set(p.get("evidence_document_paths", []))
    if explicit_documents - set(p["files"]):
        fail("license_evidence_incomplete", "Every selected terms document must be included in the exact file inventory")
    source_origin = None
    if p["repo_type"] == "source":
        from .hub_source import inspect_origin
        source_origin = inspect_origin(hub, p, root)
    elif "source_commit" in p or "source_repository_id" in p:
        fail("license_source_mismatch", "Pinned-source identity fields are only valid for source-origin evidence")
    for name in sorted(p["files"]):
        safe_name(name)
        if name.casefold() in names:
            fail("hub_unsafe_path", "Local artifact names collide")
        names.add(name.casefold())
        item = {"path": name, **fingerprint(root / name, min(p["max_bytes"], hub.byte_budget(p["project_id"])) - total)}
        files.append(item)
        total += item["size_bytes"]
        if name in explicit_documents or re.fullmatch(r"(?i)(readme|license|licence|notice|copying|authors)(?:[._-][a-z0-9-]+)?(?:\.(?:md|txt|rst))?", Path(name).name):
            if len(documents) >= 32:
                fail("license_evidence_incomplete", "Too many local licensing documents")
            with safe_file(root / name, max_bytes=1024 * 1024) as source:
                raw = source.read(1024 * 1024)
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                fail("license_evidence_changed", "Local licensing document changed during capture")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                fail("license_evidence_incomplete", "Selected licensing documents must contain readable UTF-8 text")
            documents.append({**item, "text": text, "source_url": "urn:sha256:" + item["sha256"]})
    if sum(len(item["text"].encode()) for item in documents) > 4 * 1024 * 1024:
        fail("license_evidence_incomplete", "Local licensing evidence exceeds its text budget")
    hub._bound(files, p["max_bytes"], p["project_id"])
    if source_origin is not None:
        captured = {item["path"]: item["sha256"] for item in files}
        if any(captured.get(item["path"]) != item["sha256"] for item in source_origin["files"]):
            fail("license_source_mismatch", "Select all pinned recipe source files with their exact current hashes")
    evidence = {"schema_version": "modelbuilder.license-evidence/v1", "provider": "local", "repo_id": p["source_alias"],
                "repo_type": p["repo_type"], "commit": digest(files), "declared_license": p["declared_license"],
                "label_state": "user_declared_uninterpreted", "card_metadata": {}, "documents": documents,
                "gated": False, "gated_terms_state": "not_gated", "permissions_inferred_from_label": False,
                "captured_at": utcnow(), "source_file_manifest": files,
                **({"source_origin": source_origin} if source_origin is not None else {})}
    return {**hub._base(p["project_id"], actor), "evidence_id": new_id(), "evidence": evidence,
            "evidence_digest": digest(evidence), "source_alias": p["source_alias"], "max_bytes": p["max_bytes"]}


def register_local(hub, p, actor):
    evidence = hub._get("hub_license_evidence", p["evidence_id"], p["project_id"])
    policy = hub._get("hub_license_policy", p["license_policy_id"], p["project_id"])
    data = evidence["evidence"]
    if data["repo_type"] == "source":
        fail("license_source_not_asset", "Source-origin evidence is not a model or dataset asset; bind its reviewed origin to the pinned recipe")
    if data["provider"] != "local" or "source_alias" not in evidence:
        fail("license_evidence_changed", "Select a local captured artifact identity")
    rights = {"schema_version": "modelbuilder.artifact-rights/v1",
              "sources": [source_reference(data, evidence["evidence_digest"], policy["policy_id"])],
              "unresolved": [], "parent_rights_digests": []}
    validate_artifact_rights(rights, "download", {policy["policy_id"]: policy}, policy["reviewed_usage_context"])
    root = hub._source(p["project_id"], evidence["source_alias"])
    asset_id = digest({"project_id": p["project_id"], "evidence_digest": evidence["evidence_digest"], "policy_id": policy["policy_id"], "request_id": new_id()})
    directory(hub.root / "local-staging")
    from .hub_storage import reserve_space
    total = hub._bound(data["source_file_manifest"], evidence["max_bytes"], p["project_id"])
    with reserve_space(hub.root, total), tempfile.TemporaryDirectory(prefix="local-", dir=hub.root / "local-staging") as temporary:
        completed, total = [], 0
        for item in data["source_file_manifest"]:
            copied = fingerprint(root / item["path"], item["size_bytes"], Path(temporary) / item["path"])
            if any(copied[key] != item[key] for key in copied):
                fail("license_evidence_changed", "Local artifact bytes changed since the rights review")
            completed.append({"path": item["path"], **copied})
            total += copied["size_bytes"]
        hub._authority("hub.local.register", actor, p["project_id"])
        destination = hub.root / "assets" / asset_id
        directory(destination.parent)
        os.rename(temporary, destination)
        sync_directory(destination.parent)
        # TemporaryDirectory's cleanup accepts an already moved directory.
    payload = {**{key: data[key] for key in ("provider", "repo_id", "repo_type", "commit")}, "private": True,
               "license": data["declared_license"], "license_provenance": data, "license_evidence_digest": evidence["evidence_digest"],
               "license_policy_id": policy["policy_id"], "rights": rights, "usage_context": policy["reviewed_usage_context"]}
    try:
        return hub._asset_record({"project_id": p["project_id"]}, payload, completed, asset_id, total)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
