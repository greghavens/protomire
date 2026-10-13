"""Operator-owned source aliases and immutable, host-local checkout identities.

No client path or recipe URL is used to locate a checkout. Probing is read-only:
Git metadata and declared bytes are inspected without importing project code.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .models import ServiceError, utcnow

ALIAS_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}"


class SourceWorkspaces:
    def __init__(self, aliases: dict | None):
        self.aliases = {}
        for alias, settings in (aliases or {}).items():
            if not isinstance(alias, str) or not re.fullmatch(ALIAS_PATTERN, alias):
                raise ValueError("Source aliases must be simple identifiers")
            if not isinstance(settings, dict) or set(settings) - {"path", "trusted", "dependency_paths", "project_ids", "label", "kind"}:
                raise ValueError("Source aliases require supported operator settings")
            if not isinstance(settings.get("path"), str) or not settings["path"].strip():
                raise ValueError("Source alias paths must be nonempty strings")
            if type(settings.get("trusted", False)) is not bool:
                raise ValueError("Source alias trust must be an explicit boolean")
            projects = settings.get("project_ids")
            if projects is not None and (not isinstance(projects, list) or any(not isinstance(p, str) or not p for p in projects)):
                raise ValueError("Source project_ids must be an array of project IDs")
            dependencies = settings.get("dependency_paths", {})
            if not isinstance(dependencies, dict) or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", k)
                    or not isinstance(v, str) or not v for k, v in dependencies.items()):
                raise ValueError("Source dependencies must map IDs to operator paths")
            kind = settings.get("kind", "git")
            if not isinstance(kind, str) or kind not in {"git", "imported_snapshot"}:
                raise ValueError("Source kind must be git or imported_snapshot")
            path = Path(settings["path"]).expanduser()
            # Preserve the operator-selected route so a symlink cannot be hidden
            # by normalization before snapshot inspection.
            path = path.absolute() if kind == "imported_snapshot" else path.resolve()
            self.aliases[alias] = {**settings, "kind": kind, "path": str(path),
                                   "trusted": settings.get("trusted", False),
                                   "dependency_paths": {k: str(Path(v).expanduser().resolve()) for k, v in dependencies.items()}}

    def settings(self, alias, project_id=None):
        settings = self.aliases.get(alias)
        if settings is None or (settings.get("project_ids") is not None and project_id not in settings["project_ids"]):
            raise ServiceError("source_not_allowed", "Source alias is not approved for this project", status_code=403)
        return settings

    @staticmethod
    def _git(root: Path, *arguments):
        try:
            result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "protocol.allow=never", "-C", str(root), *arguments],
                                    env={**{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
                                         "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"},
                                    capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ServiceError("source_unavailable", "Approved source checkout cannot be inspected", status_code=422) from error
        if result.returncode:
            raise ServiceError("source_unavailable", "Approved source must be a readable committed Git checkout", status_code=422)
        return result.stdout

    def inspect(self, alias, project_id=None):
        settings = self.settings(alias, project_id)
        root = Path(settings["path"])
        if not root.is_dir() or root.resolve() != root:
            raise ServiceError("source_unavailable", "Approved checkout is missing or has been redirected", status_code=422)
        if settings["kind"] == "imported_snapshot":
            return self._inspect_snapshot(alias, settings, root)
        top = Path(self._git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        if top != root:
            raise ServiceError("source_unavailable", "Approved source path must identify the Git checkout root", status_code=422)
        git_dir = Path(self._git(root, "rev-parse", "--absolute-git-dir").decode().strip()).resolve()
        try:
            location = git_dir.stat()
        except OSError as error:
            raise ServiceError("source_unavailable", "Approved Git checkout identity is unavailable", status_code=422) from error
        identity = {"root": str(root), "git_dir": str(git_dir), "device": location.st_dev, "inode": location.st_ino}
        repository_id = "sha256:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return {"alias": alias, "label": settings.get("label", alias), "source_kind": "git", "repository_id": repository_id,
                "workspace_path": str(root), "head_commit": self._git(root, "rev-parse", "HEAD").decode().strip(),
                "operator_trusted": settings["trusted"], "available": True}

    @staticmethod
    def _inspect_snapshot(alias, settings, root):
        """Verify an explicitly selected transport, never infer Git authenticity."""
        from . import recipe as recipe_module
        from .export_packages import safe_file
        try:
            if any(path.is_symlink() for path in (root, *root.parents)) or (root / ".git").exists() or (root / ".git").is_symlink():
                raise ValueError("Imported snapshots must be Git-free directories without redirected ancestors")
            before = root.stat()
            with safe_file(root / "recipe.json", max_bytes=recipe_module.MAX_BUNDLE_BYTES) as stream:
                manifest = json.load(stream)
            bundle = recipe_module.export_bundle(manifest, source_root=root)
            report = recipe_module.import_bundle(bundle)
            source = report["recipe"]["source"]
            if not source["files"] or source["commit"] == "0" * len(source["commit"]):
                raise ValueError("Imported snapshots require a declared upstream commit and nonempty source inventory")
            # A review must cover every executable/lock/profile reference, even
            # when a transport author omitted it from the source.files array.
            if {item["path"]: item["sha256"] for item in source["files"]} != {name: item["sha256"] for name, item in bundle["files"].items()}:
                raise ValueError("Imported source inventory must include every implementation, lock and profile reference")
            for item in source["files"]:
                with safe_file(root / item["path"], max_bytes=recipe_module.MAX_FILE_BYTES) as stream:
                    if hashlib.sha256(stream.read(recipe_module.MAX_FILE_BYTES + 1)).hexdigest() != item["sha256"]:
                        raise ValueError("Imported snapshot bytes changed during inspection")
            after = root.stat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("Imported snapshot directory changed during inspection")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ServiceError("source_snapshot_invalid", "Imported snapshot manifest, complete source inventory or exact bytes are unavailable, changed or unsafe", status_code=422) from error
        identity = {"root": str(root), "device": before.st_dev, "inode": before.st_ino,
                    "snapshot_digest": report["bundle_digest"], "kind": "imported_snapshot"}
        repository_id = "sha256:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return {"alias": alias, "label": settings.get("label", alias), "source_kind": "imported_snapshot",
                "repository_id": repository_id, "workspace_path": str(root),
                "declared_source_commit": source["commit"], "snapshot_digest": report["bundle_digest"],
                "transport_recipe_digest": report["digest"], "source_files": source["files"],
                "git_provenance_verified": False, "operator_trusted": settings["trusted"], "available": True}

    def list(self, project_id=None):
        result = []
        for alias in self.aliases:
            try:
                settings = self.settings(alias, project_id)
            except ServiceError:
                continue
            try:
                result.append(self.inspect(alias, project_id))
            except (ServiceError, OSError) as error:
                result.append({"alias": alias, "label": settings.get("label", alias), "available": False,
                               "operator_trusted": settings["trusted"], "diagnostic": getattr(error, "code", "source_unavailable")})
        return result

    def bind(self, alias, project_id, actor_id, trusted, repository_id):
        if trusted is not True:
            raise ServiceError("source_trust_required", "Acknowledge trusted local code before binding this source", status_code=403)
        source = self.inspect(alias, project_id)
        if repository_id != source["repository_id"]:
            raise ServiceError("source_identity_conflict", "Approved checkout identity changed since review; inspect it again before binding", status_code=409)
        return {"alias": alias, "repository_id": source["repository_id"], "source_kind": source["source_kind"], "bound_at": utcnow(),
                "bound_by": actor_id, "trust_acknowledged": True}

    def resolve(self, binding, project_id, *, execute=False):
        if not binding:
            raise ServiceError("source_binding_required", "Bind this model project to an approved source workspace", status_code=422)
        source = self.inspect(binding["alias"], project_id)
        if source["repository_id"] != binding["repository_id"]:
            raise ServiceError("source_binding_changed", "The approved alias now identifies a different checkout; explicitly rebind the project", status_code=409)
        if execute and not source["operator_trusted"]:
            raise ServiceError("untrusted_execution_blocked", "Operator approval is required for this source checkout", status_code=403)
        return source, self.settings(binding["alias"], project_id)

    def status(self, project, recipe):
        binding = project.get("source_binding")
        result = {"project_id": project["project_id"], "project_revision": project["revision"], "binding": binding,
                  "bound": bool(binding), "available": False, "source_kind": (binding or {}).get("source_kind", "git"),
                  "execution_requires_trust": True,
                  "isolation": "trusted_local_process", "diagnostics": []}
        try:
            source, _ = self.resolve(binding, project["project_id"])
            result.update(source)
            root = Path(source["workspace_path"])
            declared = recipe.get("source", {})
            result["recipe_commit"] = declared.get("commit")
            files = declared.get("files", [])
            commit = declared.get("commit", "")
            if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit) or not files:
                raise ServiceError("source_pin_required", "The current recipe needs a Git commit and SHA256-pinned source closure")
            if source["source_kind"] == "imported_snapshot":
                if source["declared_source_commit"] != commit or source["source_files"] != files:
                    raise ServiceError("dependency_mismatch", "Current recipe differs from the exact imported source snapshot")
                result["pinned_source_matches"] = True
                return result
            self._git(root, "cat-file", "-e", commit + "^{commit}")
            for reference in files:
                candidate = (root / reference["path"]).resolve()
                if (root / reference["path"]).is_symlink() or not candidate.is_relative_to(root) or not candidate.is_file():
                    raise ServiceError("dependency_mismatch", "Pinned source file is missing or escapes the approved checkout")
                digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
                committed = hashlib.sha256(self._git(root, "show", f"{commit}:{reference['path']}")).hexdigest()
                if digest != reference["sha256"] or committed != reference["sha256"]:
                    raise ServiceError("dependency_mismatch", "Source bytes differ from the recipe's pinned Git closure")
            result["pinned_source_matches"] = True
        except (ServiceError, OSError) as error:
            result["pinned_source_matches"] = False
            result["diagnostics"].append({"code": getattr(error, "code", "source_unavailable"),
                                          "message": str(error) if isinstance(error, ServiceError) else "Approved source is unavailable"})
        return result
