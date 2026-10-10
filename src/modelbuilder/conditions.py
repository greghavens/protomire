"""Explicit typed terminal conditions on the existing canonical recipe graph."""
from __future__ import annotations

CONDITION_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"source_node": {"type": "string", "minLength": 1},
                   "source_port": {"type": "string", "minLength": 1}, "equals": {"type": "boolean"}},
    "required": ["source_node", "source_port", "equals"]}


class ConditionError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def control_dependencies(recipe: dict) -> list[tuple[str, str]]:
    return [(node["condition"]["source_node"], node["id"]) for node in recipe.get("nodes", []) if "condition" in node]


def validate_conditions(recipe: dict) -> None:
    nodes = {node["id"]: node for node in recipe.get("nodes", [])}
    for node in nodes.values():
        if {"loop", "repeat", "iterations"}.intersection(node):
            raise ConditionError("unsupported_control_flow", "Loop/repeat stages are not supported. Use explicit stages or separate immutable recipe revisions; no implicit iteration is executed.", node_id=node["id"])
        if "condition" not in node:
            continue
        condition = node["condition"]
        if (type(condition) is not dict or set(condition) != {"source_node", "source_port", "equals"}
                or type(condition.get("equals")) is not bool
                or type(condition.get("source_node")) is not str or type(condition.get("source_port")) is not str):
            raise ConditionError("invalid_condition", "A condition requires source_node, source_port and a boolean equals value; expressions are not supported.", node_id=node["id"])
        source = nodes.get(condition["source_node"])
        if source is None or source["id"] == node["id"]:
            raise ConditionError("invalid_condition_source", "Choose a different declared node's boolean output for the condition.", node_id=node["id"])
        if not any(port["name"] == condition["source_port"] and port["type"] == "bool" for port in source.get("outputs", [])):
            raise ConditionError("condition_type_mismatch", "Condition gates must reference an explicitly typed bool output.", node_id=node["id"])
        if any(edge["source_node"] == node["id"] for edge in recipe.get("edges", [])) or any(
                other.get("condition", {}).get("source_node") == node["id"] for other in nodes.values()
                if type(other.get("condition")) is dict):
            raise ConditionError("unsupported_conditional_downstream", "Conditions currently support terminal stages only. Remove downstream data/control consumers or use an unconditional stage; skipped-output merging is not supported.", node_id=node["id"])
    if any("condition" in node for node in nodes.values()) and recipe.get("evaluation", {}).get("live", {}).get("enabled"):
        raise ConditionError("unsupported_conditional_live_evaluation", "Disable live evaluation before adding conditions. Live sidecars cannot honor conditional terminal gates.")


def validate_selected_gates(recipe: dict, chosen: set[str]) -> None:
    for node in recipe.get("nodes", []):
        if node["id"] in chosen and "condition" in node and node["condition"]["source_node"] not in chosen:
            raise ConditionError("unselected_condition_gate", "Select the declared boolean gate explicitly. Conditional execution never adds or executes unselected ancestors.",
                                 node_id=node["id"], gate_node_id=node["condition"]["source_node"])


def condition_decision(condition: dict, value) -> bool:
    if type(value) is not bool:
        raise ValueError("Native condition artifact must contain an actual boolean; truthiness coercion is forbidden")
    return value == condition["equals"]
