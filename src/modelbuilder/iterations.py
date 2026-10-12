"""Explicit bounded, typed iteration blocks over one canonical recipe DAG.

These declarations select public native dynamic calls; they are not an executor,
training loop, expression interpreter, or replacement run/artifact registry.
"""
from __future__ import annotations

import hashlib
from jsonschema import Draft202012Validator
from .conditions import CONDITION_SCHEMA, ConditionError, control_dependencies

ID = {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"}
STATE_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {**{key: ID for key in ("source_node", "source_port", "target_node", "target_port")},
                   "resume_mode": {"const": "warm_start"}},
    "required": ["source_node", "source_port", "target_node", "target_port"]}
ITERATION_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"id": ID, "node_ids": {"type": "array", "items": ID, "minItems": 1, "maxItems": 1000, "uniqueItems": True},
                   "max_iterations": {"type": "integer", "minimum": 1, "maximum": 1000},
                   "state": {"type": "array", "items": STATE_SCHEMA, "maxItems": 256}, "until": CONDITION_SCHEMA},
    "required": ["id", "node_ids", "max_iterations", "state"]}
ITERATIONS_SCHEMA = {"type": "array", "items": ITERATION_SCHEMA, "maxItems": 256}
MAX_INVOCATIONS = 10000


def training(node):
    return node.get("kind") == "model.train" or node.get("component", {}).get("role") == "training"


def memberships(recipe):
    return {node_id: block for block in recipe.get("bounded_iterations", []) for node_id in block["node_ids"]}


def invocation_identity(node_id, block=None, iteration=None):
    if block is None:
        return {"canonical_node_id": node_id, "invocation_id": node_id}
    # Leading underscore cannot collide with a canonical node ID. Full hashes
    # keep different valid node/block identities distinct without truncation.
    token = hashlib.sha256((block["id"] + "\0" + node_id).encode()).hexdigest()
    return {"canonical_node_id": node_id, "invocation_id": f"_mbi_{token}_{iteration}",
            "iteration_block_id": block["id"], "iteration": iteration, "max_iterations": block["max_iterations"]}


def ordered_units(recipe):
    """Topologically order atomic declared blocks and ordinary native nodes."""
    member = memberships(recipe)
    owner = {node["id"]: ("block", member[node["id"]]["id"]) if node["id"] in member else ("node", node["id"])
             for node in recipe["nodes"]}
    units = {("block", block["id"]): block for block in recipe.get("bounded_iterations", [])}
    units.update({("node", node["id"]): node for node in recipe["nodes"] if node["id"] not in member})
    dependencies = {(owner[source], owner[target]) for source, target in
        [(edge["source_node"], edge["target_node"]) for edge in recipe.get("edges", [])] + control_dependencies(recipe)
        if source in owner and target in owner and owner[source] != owner[target]}
    pending, result = set(units), []
    while pending:
        ready = sorted(unit for unit in pending if all(source not in pending for source, target in dependencies if target == unit))
        if not ready:
            raise ConditionError("iteration_boundary_cycle", "Iteration blocks must be atomic: no dependency may leave a block and re-enter it.")
        for unit in ready:
            result.append((unit[0], units[unit]))
            pending.remove(unit)
    return result


def validate_iterations(recipe):
    blocks = recipe.get("bounded_iterations", [])
    errors = list(Draft202012Validator(ITERATIONS_SCHEMA).iter_errors(blocks))
    if errors:
        raise ConditionError("invalid_bounded_iteration", "Invalid explicit bounded iteration declaration", reason=errors[0].message)
    if not blocks:
        return
    nodes = {node["id"]: node for node in recipe.get("nodes", [])}
    assigned, identifiers = set(), set()
    total = len(nodes)
    for block in blocks:
        body = set(block["node_ids"])
        if block["id"] in identifiers or block["id"] in nodes or not body <= nodes.keys() or assigned & body:
            raise ConditionError("invalid_iteration_membership", "Blocks need unique IDs and declared, disjoint node sets; nesting is not implicit.", block_id=block["id"])
        assigned.update(body)
        identifiers.add(block["id"])
        total += len(body) * (block["max_iterations"] - 1)
        seen = set()
        for node_id in body:
            if nodes[node_id].get("cache_policy") != "disabled":
                raise ConditionError("iteration_cache_unsupported", "Iteration stages must explicitly disable cache; per-invocation reuse is not yet verified.", node_id=node_id)
        for state in block["state"]:
            target = (state["target_node"], state["target_port"])
            if state["source_node"] not in body or state["target_node"] not in body or target in seen:
                raise ConditionError("invalid_iteration_state", "State feedback must connect body nodes with one feedback producer per input.", block_id=block["id"])
            seen.add(target)
            source_node, target_node = nodes[state["source_node"]], nodes[state["target_node"]]
            source = next((port for port in source_node["outputs"] if port["name"] == state["source_port"]), None)
            port = next((port for port in target_node["inputs"] if port["name"] == state["target_port"]), None)
            if not source or not port or source["type"] != port["type"]:
                raise ConditionError("iteration_state_type_mismatch", "Feedback requires declared exactly matching output and input types.", block_id=block["id"])
            initial = [edge for edge in recipe.get("edges", []) if (edge["target_node"], edge["target_port"]) == target]
            if any(edge["source_node"] in body for edge in initial):
                raise ConditionError("invalid_iteration_initial_state", "Initial feedback inputs must come from outside the block, never another body edge.", block_id=block["id"])
            if port.get("required", True) and len(initial) != 1:
                raise ConditionError("invalid_iteration_initial_state", "Required feedback inputs need one explicit external initial-state edge.", block_id=block["id"])
            if "resume_mode" in state:
                if (source["type"] not in {"Path", "checkpoint"} or not training(source_node) or not training(target_node)
                        or target_node.get("kind") == "model.train" and port["name"] != "initial"
                        or target_node.get("kind") == "component.run" and "warm_start" not in target_node.get("component", {}).get("capabilities", {}).get("resume_modes", [])):
                    raise ConditionError("invalid_iteration_warm_start", "Warm-start feedback requires a native training checkpoint, a declared training input, and supported warm_start capability.", block_id=block["id"])
                if sum(item.get("resume_mode") == "warm_start" and item["target_node"] == target[0] for item in block["state"]) != 1:
                    raise ConditionError("invalid_iteration_warm_start", "A trainer may warm-start from only one checkpoint per invocation.", node_id=target[0])
        gate = block.get("until")
        if gate and (gate["source_node"] not in body or not any(port["name"] == gate["source_port"] and port["type"] == "bool" for port in nodes[gate["source_node"]]["outputs"])):
            raise ConditionError("iteration_until_type_mismatch", "Early exit requires a body node's declared bool output.", block_id=block["id"])
        if recipe.get("evaluation", {}).get("live", {}).get("enabled") and any(training(nodes[node_id]) for node_id in body):
            raise ConditionError("iteration_live_evaluation_unsupported", "Repeated trainers require final native evaluations; live sidecar invocation identity is not yet supported. Disable live evaluation.", block_id=block["id"])
    if total > MAX_INVOCATIONS:
        raise ConditionError("iteration_invocation_limit", "Recipe exceeds the bounded maximum of 10000 native step invocations.")
    ordered_units(recipe)


def validate_selected_iterations(recipe, chosen):
    for block in recipe.get("bounded_iterations", []):
        body = set(block["node_ids"])
        if chosen & body and not body <= chosen:
            raise ConditionError("partial_iteration_selection", "Select an entire declared iteration block or none of it; no stages are added implicitly.", block_id=block["id"], node_ids=block["node_ids"])
