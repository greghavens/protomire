"""Service-boundary Git provenance checks for authored source exports.

Portable bundle primitives remain Git-free and establish manifest integrity only.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

from . import recipe as recipe_module
from .models import ServiceError


def verify_export_git_provenance(bundle: dict, source_root: Path | None) -> None:
    """Check authored checkout bytes without requiring Git for inert snapshots.

    The core bundle API deliberately establishes manifest integrity only. At
    this service boundary an operator-configured Git checkout also proves
    every packaged file belongs to the declared commit. Compare the bundle's
    hashes, not a second working-tree read, so packaging cannot race this check.
    """
    if source_root is None or not bundle["files"]:
        return
    root = source_root
    checkout = next((path for path in (root, *root.parents)
                     if (path / ".git").exists() or (path / ".git").is_symlink()), None)
    if checkout is None:
        # Imported/extracted immutable closures have no Git metadata. Their
        # bytes still passed export_bundle's mandatory manifest hash checks.
        return
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    command = ["git", "--no-pager", "--git-dir", str(checkout / ".git"),
               "--work-tree", str(checkout), "-c", "protocol.allow=never"]

    def git(*arguments):
        try:
            return subprocess.run(command + list(arguments), env=environment, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise ServiceError("source_revision_unavailable",
                               "Cannot verify the declared source revision in the configured Git checkout") from exc

    commit = bundle["recipe"]["source"]["commit"]
    if git("cat-file", "-t", commit).strip() != b"commit":
        raise ServiceError("source_revision_unavailable", "Declared source revision must identify a Git commit")
    prefix = root.relative_to(checkout)
    for path, item in bundle["files"].items():
        revision_path = f"{commit}:{(prefix / path).as_posix()}"
        # Reject oversized objects before reading their content into memory.
        if int(git("cat-file", "-s", revision_path)) > recipe_module.MAX_FILE_BYTES:
            raise ServiceError("dirty_source", "Packaged source differs from the declared Git revision", {"path": path})
        blob = git("cat-file", "blob", revision_path)
        if hashlib.sha256(blob).hexdigest() != item["sha256"]:
            raise ServiceError("dirty_source", "Packaged source differs from the declared Git revision; commit it before export",
                               {"path": path})
