"""Explicit operator-controlled configuration; never editable via recipe content."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path


def load_operator_config(path: str | Path | None = None) -> dict:
    path = path or os.environ.get("MODELBUILDER_OPERATOR_CONFIG")
    if not path:
        return {}
    source = Path(path).expanduser().resolve()
    value = json.loads(source.read_text())
    allowed = {"source_root", "python_executable", "mlx_python_executable", "pretrained_path", "trusted_code_roots", "dependency_paths", "dataset_roots", "source_roots", "dataset_limits"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Operator configuration must be an object with supported backend settings only")
    def local_path(item, *, preserve_symlink=False):
        if not isinstance(item, str) or not item.strip():
            raise ValueError("Operator paths must be nonempty strings")
        candidate = Path(item).expanduser()
        candidate = source.parent / candidate if not candidate.is_absolute() else candidate
        # A venv interpreter is normally a symlink. Resolving it selects the base
        # interpreter and silently discards the explicitly chosen environment.
        return os.path.abspath(candidate) if preserve_symlink else str(candidate.resolve())
    result = {}
    for key in ("source_root", "python_executable", "mlx_python_executable", "pretrained_path"):
        if key in value:
            result[key] = local_path(value[key], preserve_symlink=key in {"python_executable", "mlx_python_executable"})
    roots = value.get("trusted_code_roots", [])
    if not isinstance(roots, list):
        raise ValueError("trusted_code_roots must be an array")
    result["trusted_code_roots"] = [local_path(root) for root in roots]
    dependencies = value.get("dependency_paths", {})
    if not isinstance(dependencies, dict) or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", key) for key in dependencies):
        raise ValueError("dependency_paths must map manifest IDs to local paths")
    result["dependency_paths"] = {key: local_path(item) for key, item in dependencies.items()}
    dataset_roots = value.get("dataset_roots", {})
    if not isinstance(dataset_roots, dict):
        raise ValueError("dataset_roots must map project IDs to root alias maps")
    resolved_roots = {}
    for project, aliases in dataset_roots.items():
        if not isinstance(project, str) or not project or not isinstance(aliases, dict):
            raise ValueError("dataset_roots must map project IDs to root alias maps")
        if any(not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", alias) for alias in aliases):
            raise ValueError("Dataset root aliases must be simple identifiers")
        resolved_roots[project] = {alias: local_path(item) for alias, item in aliases.items()}
    if resolved_roots:
        result["dataset_roots"] = resolved_roots
    if "dataset_limits" in value:
        from ..dataset_streaming import limits
        result["dataset_limits"] = limits(value["dataset_limits"])
    if "source_roots" in value:
        aliases = value["source_roots"]
        if not isinstance(aliases, dict):
            raise ValueError("source_roots must map approved aliases to operator settings")
        result["source_roots"] = {}
        for alias, settings in aliases.items():
            if not isinstance(settings, dict) or "path" not in settings:
                raise ValueError("Source aliases must have an operator-owned path")
            dependencies = settings.get("dependency_paths", {})
            if not isinstance(dependencies, dict):
                raise ValueError("Source dependency_paths must be an object")
            result["source_roots"][alias] = {**settings, "path": local_path(settings["path"]),
                "dependency_paths": {key: local_path(item) for key, item in dependencies.items()}}
        from ..source_workspace import SourceWorkspaces
        SourceWorkspaces(result["source_roots"])
    return result
