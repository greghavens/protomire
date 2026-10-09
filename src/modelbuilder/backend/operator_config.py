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
    allowed = {"source_root", "python_executable", "pretrained_path", "trusted_code_roots", "dependency_paths", "dataset_roots"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Operator configuration must be an object with supported backend settings only")
    def local_path(item):
        if not isinstance(item, str) or not item.strip():
            raise ValueError("Operator paths must be nonempty strings")
        candidate = Path(item).expanduser()
        return str((source.parent / candidate).resolve() if not candidate.is_absolute() else candidate.resolve())
    result = {}
    for key in ("source_root", "python_executable", "pretrained_path"):
        if key in value:
            result[key] = local_path(value[key])
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
    return result
