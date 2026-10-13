"""Revisioned recipe bindings to external, immutable registered datasets.

Portable manifests stay in recipe metadata. Runtime paths and captured root
identities remain request-local, never recipe state or an operator-map mutation.
Only the small reference JSON is cached; dataset bytes remain externally owned.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import stat

from .dataset import PYARROW_REQUIREMENT
from .models import ServiceError


def reference_bytes(reference):
    return json.dumps(reference, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def reference_dependency(reference, rights=None):
    sha = hashlib.sha256(reference_bytes(reference)).hexdigest()
    return {"id": "dataset-reference-" + sha, "kind": "dataset", "url": "urn:sha256:" + sha,
            "sha256": sha, "license": reference["manifest"]["license"], "required": True,
            "redistribution_allowed": False, "metadata": {"dataset_reference": reference, **({"rights": rights} if rights else {})}}


def source_dependencies(reference, rights=None):
    return [{**{key: value for key, value in item.items() if key != "source_id"},
             "url": "urn:sha256:" + item["sha256"], "redistribution_allowed": False,
             "metadata": {"dataset_source": True, **({"rights": rights} if rights else {})}} for item in reference["dependencies"]]



def _retire_replaced_dependencies(recipe, replacing_node_id):
    """Drop orphaned generated closure before checking a replacement identity.

    Preserve any generated reference mentioned by another node, including generic
    operator nodes. A still-shared source cannot silently change its reviewed
    provenance just because its byte-derived identifier happens to match.
    """
    declared = {item["id"]: item for item in recipe["resources"] + recipe["external_dependencies"]}

    def references(value):
        if isinstance(value, str):
            return {value} if value in declared else set()
        if isinstance(value, dict):
            return set().union(*(references(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(references(item) for item in value))
        return set()

    used = set().union(*(references(node) for node in recipe["nodes"] if node["id"] != replacing_node_id))
    source_ids = {source["id"] for identifier in used
                  for source in declared[identifier].get("metadata", {}).get("dataset_reference", {}).get("dependencies", [])}
    used.update(source_ids)
    for field in ("resources", "external_dependencies"):
        recipe[field] = [item for item in recipe[field]
                         if not (item.get("metadata", {}).get("dataset_reference") or item.get("metadata", {}).get("dataset_source"))
                         or item["id"] in used]


def bind(service, payload):
    project_id = payload["project_id"]
    current = service._current_recipe(project_id, payload["recipe_id"])
    service._revision(current, payload["expected_revision"])
    if ("target_node" in payload) != ("target_port" in payload):
        raise ServiceError("validation_error", "Specify both target_node and target_port, or neither")
    registry = service._dataset_registry
    reference = registry.portable_reference(project_id, payload["dataset_id"], payload["dataset_revision"])
    # Read approved files and verify immutable identity and role before saving.
    data = registry.read_split(project_id, payload["dataset_id"], payload["dataset_revision"],
                               payload["split_id"], purpose=payload["purpose"])
    recipe = copy.deepcopy(current)
    if any(source["format"] == "parquet" for source in reference["manifest"]["sources"]):
        requirements = recipe.setdefault("environment", {}).setdefault("requirements", [])
        existing_pin = next((pin for pin in requirements
                             if pin.split("==")[0].split("[")[0].lower() == "pyarrow"), None)
        if existing_pin and existing_pin.split("==")[1] != PYARROW_REQUIREMENT.split("==")[1]:
            raise ServiceError("dataset_binding_conflict", "Recipe declares a conflicting Parquet adapter version")
        if existing_pin is None:
            requirements.append(PYARROW_REQUIREMENT)
    node_id = payload["node_id"]
    existing = next((node for node in recipe["nodes"] if node["id"] == node_id), None)
    if existing and existing["kind"] != "dataset.version.import":
        raise ServiceError("dataset_binding_conflict", "Dataset binding cannot replace a non-dataset node")
    if "target_node" in payload:
        target = next((node for node in recipe["nodes"] if node["id"] == payload["target_node"]), None)
        port = next((port for port in (target or {}).get("inputs", []) if port["name"] == payload["target_port"]), None)
        if target is None or target["id"] == node_id or port is None or port["type"] != "dict":
            raise ServiceError("dataset_binding_invalid", "Target must be an existing dict input on another recipe node")
        recipe["edges"] = [edge for edge in recipe["edges"] if
                           (edge["target_node"], edge["target_port"]) != (target["id"], port["name"])]
        recipe["edges"].append({"source_node": node_id, "source_port": "data",
                                "target_node": target["id"], "target_port": port["name"]})
    from .backend.execution_rights import dataset_action
    from .backend.admission import BackendError
    try:
        intended_use = dataset_action(payload.get("purpose"))
    except BackendError as error:
        raise ServiceError(error.code, error.message, error.details) from None
    provenance = copy.deepcopy((existing or {}).get("provenance", {}))
    if provenance.get("intended_use", intended_use) != intended_use:
        raise ServiceError("license_usage_conflict", "Existing dataset action conflicts with the explicitly selected split purpose")
    provenance["intended_use"] = intended_use
    rights = service.hub.dataset_rights(reference)
    dependency = reference_dependency(reference, rights)
    node = {"id": node_id, "kind": "dataset.version.import", "implementation_ref": "builtin:dataset.version.import",
            "inputs": [], "outputs": [{"name": "data", "type": "dict"}],
            "parameters": {"reference_id": dependency["id"], "split_id": payload["split_id"], "purpose": payload["purpose"]},
            "environment_ref": "default", "resource_request": {"gpu": 0}, "retry": {"max_attempts": 1, "backoff_seconds": 0},
            "timeout": 300, "cache_policy": "disabled", "provenance": provenance}
    recipe["nodes"] = [node if item["id"] == node_id else item for item in recipe["nodes"]]
    if existing is None:
        recipe["nodes"].append(node)
    _retire_replaced_dependencies(recipe, node_id)
    declared = {item["id"]: item for item in recipe["resources"] + recipe["external_dependencies"]}
    for item in [dependency, *source_dependencies(reference, rights)]:
        if item["id"] in declared and declared[item["id"]] != item:
            raise ServiceError("dataset_binding_conflict", "A recipe dependency has a conflicting dataset identity",
                               {"dependency_id": item["id"]})
        if item["id"] not in declared:
            recipe["external_dependencies"].append(item)
            declared[item["id"]] = item
    saved = service._save_recipe(recipe, project_id, current["recipe_id"], current["revision"] + 1, current["revision"])
    service._confirm_recipe_review(project_id)
    return {"project_id": project_id, "recipe_id": saved["recipe_id"], "revision": saved["revision"], "recipe": saved,
            "binding": {"node_id": node_id, "dataset_id": reference["dataset_id"], "dataset_revision": reference["revision"],
                        "version_id": reference["version_id"], "content_digest": reference["content_digest"],
                        "reference_id": dependency["id"], "split_id": payload["split_id"], "purpose": payload["purpose"],
                        "dataset": data["dataset"], "split": data["split"]}, "execution_started": False}


def _cache_reference(root, dependency):
    root = Path(root) / "dataset-references"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise ServiceError("dataset_reference_invalid", "Dataset reference cache is unsafe")
    path = root / (dependency["sha256"] + ".json")
    content = reference_bytes(dependency["metadata"]["dataset_reference"])
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or stream.read(len(content) + 1) != content:
                raise ServiceError("dataset_reference_invalid", "Cached dataset reference differs from its pinned identity")
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
    return str(path)


def resolve_backend(service, backend, payload):
    """Prepare an isolated runtime view; validate every bound source again."""
    recipe = payload.get("recipe", {})
    declared = {item["id"]: item for item in recipe.get("resources", []) + recipe.get("external_dependencies", [])}
    paths, guards = {}, {}
    execution = payload.get("execution")
    selected = execution.get("node_ids") if isinstance(execution, dict) else None
    for node in recipe.get("nodes", []):
        if isinstance(selected, list) and node["id"] not in selected:
            continue
        if node["kind"] != "dataset.version.import":
            continue
        parameters = node.get("parameters", {})
        if (not isinstance(parameters, dict) or set(parameters) - {"reference_id", "split_id", "purpose"}
                or any(not isinstance(parameters.get(key), str) or not parameters[key]
                       for key in ("reference_id", "split_id"))
                or not isinstance(parameters.get("purpose", "training"), str)
                or parameters.get("purpose", "training") not in {"training", "evaluation", "calibration"}):
            raise ServiceError("validation_error", "Dataset import needs reference_id, split_id and a supported purpose")
        dependency = declared.get(parameters["reference_id"], {})
        reference = dependency.get("metadata", {}).get("dataset_reference")
        if reference is None:
            continue  # Existing explicit operator-resolved imports retain their contract.
        if not isinstance(reference, dict) or reference.get("project_id") != payload["project_id"]:
            raise ServiceError("dataset_reference_invalid", "Bound dataset does not belong to this project")
        if (not isinstance(reference.get("dataset_id"), str) or not reference["dataset_id"]
                or type(reference.get("revision")) is not int or reference["revision"] < 1):
            raise ServiceError("dataset_reference_invalid", "Bound dataset is missing its registered version identity")
        registry = service._dataset_registry
        registered = registry.portable_reference(payload["project_id"], reference["dataset_id"], reference["revision"])
        rights = service.hub.dataset_rights(registered)
        if reference_bytes(reference) != reference_bytes(registered) or dependency != reference_dependency(registered, rights):
            raise ServiceError("dataset_reference_invalid", "Bound reference differs from the registered immutable manifest")
        registry.read_split(payload["project_id"], reference["dataset_id"], reference["revision"],
                            parameters.get("split_id"), purpose=parameters.get("purpose", "training"))
        for source in source_dependencies(reference, rights):
            if declared.get(source["id"]) != source:
                raise ServiceError("dataset_reference_invalid", "Bound recipe is missing its exact dataset dependency closure")
        if not hasattr(backend, "with_dataset_dependencies"):
            raise ServiceError("capability_unavailable", "Execution backend does not support registered dataset bindings", status_code=503)
        source_paths, source_guards = registry.execution_sources(payload["project_id"], reference["dataset_id"], reference["revision"])
        paths.update(source_paths)
        guards.update(source_guards)
        try:
            paths[dependency["id"]] = _cache_reference(backend.root, dependency)
        except OSError as exc:
            raise ServiceError("dataset_reference_unavailable", "Dataset reference metadata cannot be safely prepared") from exc
    return backend.with_dataset_dependencies(paths, guards) if paths else backend
