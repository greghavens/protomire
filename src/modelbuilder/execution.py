"""Exact subgraph selection over one immutable canonical recipe revision.

Selection never adds ancestors or rewrites the canonical recipe. Reused inputs
are explicit native artifact versions, resolved by the existing ZenML backend.
"""
from __future__ import annotations

import copy
from jsonschema import Draft202012Validator

from .smoke import SMOKE_SCHEMA

MAX_PARALLEL_STAGES = 8
RECIPE_EXECUTION_SCHEMA = {"type": "object", "additionalProperties": False,
    "description": "Canonical per-run stage scheduling. Omitted settings preserve sequential execution.",
    "properties": {"purpose": {"enum": ["production", "smoke"]}, "smoke": SMOKE_SCHEMA, "max_parallel_stages": {"type": "integer", "minimum": 1,
        "maximum": MAX_PARALLEL_STAGES, "default": 1,
        "description": "Maximum simultaneously executing independent native stages. Iterations remain sequential barriers; this is not a resource reservation."}}}


def max_parallel_stages(recipe: dict) -> int:
    """Read canonical scheduling without changing historical manifests/digests.

    Run-request ``execution`` remains an exact subgraph selection, never an
    alternate source for this recipe setting. Admission and workers share the
    same strict check, including callers that bypass recipe normalization.
    """
    settings = recipe.get("execution", {})
    if not isinstance(settings, dict) or set(settings) - {"max_parallel_stages", "purpose", "smoke"}:
        raise ValueError("Recipe execution contains unknown fields")
    errors = list(Draft202012Validator(RECIPE_EXECUTION_SCHEMA).iter_errors(settings))
    if errors:
        raise ValueError("Invalid canonical execution settings: " + errors[0].message)
    if (settings.get("purpose") == "smoke") != ("smoke" in settings):
        raise ValueError("Smoke execution requires its pinned budget lineage and purpose")
    limit = settings.get("max_parallel_stages", 1)
    if type(limit) is not int or not 1 <= limit <= MAX_PARALLEL_STAGES:
        raise ValueError(f"Recipe execution.max_parallel_stages must be an integer from 1 through {MAX_PARALLEL_STAGES}")
    return limit


TEXT = {"type": "string", "minLength": 1}
BINDING_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {**{key: TEXT for key in ("node_id", "input_port", "backend_run_id", "source_node_id", "source_port", "artifact_id")},
                   "content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
    "required": ["node_id", "input_port", "backend_run_id", "source_node_id", "source_port", "artifact_id", "content_sha256"]}
EXECUTION_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"node_ids": {"type": "array", "items": TEXT, "minItems": 1, "maxItems": 1000, "uniqueItems": True},
                   "input_bindings": {"type": "array", "items": BINDING_SCHEMA, "maxItems": 10000}},
    "required": ["node_ids", "input_bindings"]}
EVALUATION_SELECTOR_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {key: TEXT for key in ("backend_run_id", "node_id", "output_port", "artifact_id", "metric")},
    "required": ["backend_run_id", "node_id", "output_port", "artifact_id", "metric"]}
EVALUATIONS_SCHEMA = {"type": "array", "items": EVALUATION_SELECTOR_SCHEMA, "minItems": 2, "maxItems": 1000}


def selection(recipe: dict, requested: dict | None = None) -> dict:
    """Validate the cut, including inputs on its boundary, without loading code."""
    from .models import ServiceError
    from .conditions import ConditionError, validate_conditions, validate_selected_gates
    from .iterations import validate_iterations, validate_selected_iterations
    try:
        validate_conditions(recipe)
        validate_iterations(recipe)
    except ConditionError as error:
        raise ServiceError(error.code, error.message, error.details) from error
    nodes = {node["id"]: node for node in recipe.get("nodes", [])}
    if requested is None:
        return {"node_ids": list(nodes), "input_bindings": []}
    errors = list(Draft202012Validator(EXECUTION_SCHEMA).iter_errors(requested))
    if errors:
        raise ServiceError("invalid_execution_selection", "Invalid exact-node execution selection", {"reason": errors[0].message})
    chosen = set(requested["node_ids"])
    if not chosen <= nodes.keys():
        raise ServiceError("invalid_execution_selection", "Execution selection names an unknown recipe node")
    try:
        validate_selected_gates(recipe, chosen)
        validate_selected_iterations(recipe, chosen)
    except ConditionError as error:
        raise ServiceError(error.code, error.message, error.details) from error
    bindings = {}
    for binding in requested["input_bindings"]:
        target = (binding["node_id"], binding["input_port"])
        if target in bindings:
            raise ServiceError("invalid_execution_selection", "Each selected input may have only one native binding")
        if target[0] not in chosen or not any(p["name"] == target[1] for p in nodes[target[0]].get("inputs", [])):
            raise ServiceError("invalid_execution_selection", "Native binding must target a declared input on a selected node")
        bindings[target] = binding
    for node_id in chosen:
        node = nodes[node_id]
        for port in node.get("inputs", []):
            target = (node_id, port["name"])
            edges = [edge for edge in recipe.get("edges", []) if (edge["target_node"], edge["target_port"]) == target]
            internal = [edge for edge in edges if edge["source_node"] in chosen]
            if len(internal) > 1 or (internal and target in bindings):
                raise ServiceError("invalid_execution_selection", "Selected input has conflicting native and selected-node sources", {"node_id": node_id, "input_port": port["name"]})
            if not internal and target not in bindings and port.get("required", True):
                raise ServiceError("unbound_execution_input", "Unselected ancestors never run; bind this input explicitly or select its producer", {"node_id": node_id, "input_port": port["name"]})
    return {"node_ids": [node for node in nodes if node in chosen],
            "input_bindings": sorted(copy.deepcopy(requested["input_bindings"]), key=lambda b: (b["node_id"], b["input_port"]))}


def selected_recipe(recipe: dict, execution: dict) -> dict:
    """Inert admission view only. Its digest is not an executed recipe identity."""
    chosen = set(execution["node_ids"])
    body = {**recipe, "nodes": [node for node in recipe["nodes"] if node["id"] in chosen],
            "edges": [edge for edge in recipe.get("edges", []) if edge["source_node"] in chosen and edge["target_node"] in chosen]}
    if "bounded_iterations" in recipe:
        body["bounded_iterations"] = [block for block in recipe["bounded_iterations"] if set(block["node_ids"]) <= chosen]
    trainers = [node for node in body["nodes"] if node["kind"] == "model.train" or node.get("component", {}).get("role") == "training"]
    evaluators = [node for node in body["nodes"] if node["kind"] == "model.evaluate" or node.get("component", {}).get("role") == "evaluation"]
    if len(chosen) != len(recipe["nodes"]) and not (trainers and evaluators):
        body["evaluation"] = copy.deepcopy(recipe.get("evaluation", {}))
        body["evaluation"].pop("live", None)
    # Generated dataset resources only serve their import node; other resources
    # remain conservative pins because component adapters may consume them.
    imports = {node.get("parameters", {}).get("reference_id") for node in body["nodes"] if node["kind"] == "dataset.version.import"}
    dataset_refs = [ref for ref in recipe.get("external_dependencies", []) if ref.get("metadata", {}).get("dataset_reference")]
    used_sources = set()
    for ref in dataset_refs:
        if ref["id"] in imports:
            portable = ref["metadata"]["dataset_reference"]
            used_sources.update(source["id"] for source in portable.get("dependencies", []))
    for field in ("resources", "external_dependencies"):
        body[field] = [ref for ref in recipe.get(field, []) if
            (not ref.get("metadata", {}).get("dataset_reference") or ref["id"] in imports) and
            (not ref.get("metadata", {}).get("dataset_source") or ref["id"] in used_sources)]
    return body


def continuation_selection(parent: dict, current: dict, recorded: dict | None, mode: str) -> dict:
    """Recover public pins from stored native provenance before resume admission.

    Cached resolution metadata, especially local paths, is never authority. A
    full, unbound parent's warm start retains the existing full-current-DAG
    behavior. Genuine cuts keep their explicit boundary and must still bind.
    """
    if recorded is not None:
        recorded = {"node_ids": copy.deepcopy(recorded.get("node_ids")),
                    "input_bindings": [{key: value for key, value in binding.items()
                                        if key in BINDING_SCHEMA["properties"]}
                                       for binding in recorded.get("input_bindings", [])]}
    original = selection(parent, recorded)
    if mode == "warm_start" and original == selection(parent):
        return selection(current)
    return selection(current, original)
