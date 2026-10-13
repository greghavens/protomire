"""Typed branch dependencies on the existing canonical acyclic recipe graph."""
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
        for guard_id in node.get('required_guards', []):
            guard = nodes.get(guard_id, {})
            condition = node.get('condition', {})
            if (guard.get('kind') != 'component.run' or guard.get('component', {}).get('id') != 'modelbuilder.rules.gate'
                    or guard.get('condition') or condition != {'source_node': guard_id, 'source_port': 'allowed', 'equals': True}):
                raise ConditionError('invalid_required_guard', 'A required rule guard must be unconditional and govern the selected action through its allowed boolean.', node_id=node['id'], guard_id=guard_id)
            # A guard's evidence producers must not be conditionally skipped.
            pending, visited = {guard_id}, set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                if nodes[current].get('condition'):
                    raise ConditionError('conditional_required_guard', 'Required guard evidence cannot come from a conditional producer.', guard_id=guard_id)
                pending.update(edge['source_node'] for edge in recipe.get('edges', []) if edge['target_node'] == current and edge['source_node'] in nodes)
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
    validate_live_conditions(recipe)
    for node in nodes.values():
        if ((recipe.get("bounded_iterations") or any("condition" in other for other in nodes.values()))
                and node.get("kind") not in {"code.python", "component.run"} and any(
                port.get("required", True) is False for port in node.get("inputs", []))):
            raise ConditionError("unsupported_optional_input", "Optional inputs require a source-bound Python or component stage; built-in lifecycle stages require their declared inputs.", node_id=node["id"])
        if node.get("kind") == "code.python" and set(node.get("parameters", {})).intersection(
                port["name"] for port in node.get("inputs", [])):
            raise ConditionError("ambiguous_input_parameter", "Python parameters cannot replace declared native input ports.", node_id=node["id"])


def validate_live_conditions(recipe: dict) -> None:
    """A sidecar's gate must already be available before its trainer begins.

    No hidden dependency, additional gate execution or speculative sidecar is
    permitted. A gate produced by training can govern final evaluation only.
    """
    if not recipe.get("evaluation", {}).get("live", {}).get("enabled"):
        return
    nodes = {node["id"]: node for node in recipe.get("nodes", [])}
    dependencies = [(edge["source_node"], edge["target_node"]) for edge in recipe.get("edges", [])] + control_dependencies(recipe)
    for trainer in nodes.values():
        if trainer.get("kind") != "model.train" and trainer.get("component", {}).get("role") != "training":
            continue
        ancestors, pending = set(), {trainer["id"]}
        while pending:
            next_nodes = {source for source, target in dependencies if target in pending} - ancestors
            ancestors.update(next_nodes)
            pending = next_nodes
        for edge in recipe.get("edges", []):
            evaluator = nodes.get(edge["target_node"], {})
            if (edge["source_node"] != trainer["id"] or "condition" not in evaluator
                    or evaluator.get("kind") != "model.evaluate" and evaluator.get("component", {}).get("role") != "evaluation"):
                continue
            if evaluator["condition"]["source_node"] not in ancestors - {trainer["id"]}:
                raise ConditionError("unsupported_conditional_live_gate", "Live evaluation requires its boolean gate to be an explicit ancestor of the trainer. Use a pre-training gate or disable live evaluation for a post-training decision.", node_id=evaluator["id"], trainer_node_id=trainer["id"])


def skipped_dependencies(recipe: dict, node: dict, chosen: set[str], skipped: set[str], *, optional=False) -> list[dict]:
    """Return actual selected dependencies whose producers were recorded skipped.

    This never classifies an absent/pending/failed producer as skipped. Optional
    data inputs can be omitted; required inputs and control gates block a call.
    """
    ports = {port["name"] for port in node.get("inputs", []) if (port.get("required", True) is False) == optional}
    result = [{"kind": "input", "source_node": edge["source_node"], "source_port": edge["source_port"],
               "target_port": edge["target_port"]}
              for edge in recipe.get("edges", []) if edge["target_node"] == node["id"]
              and edge["target_port"] in ports and edge["source_node"] in chosen & skipped]
    gate = node.get("condition")
    if not optional and gate and gate["source_node"] in chosen & skipped:
        result.append({"kind": "condition", "source_node": gate["source_node"], "source_port": gate["source_port"]})
    return sorted(result, key=lambda item: (item["kind"], item.get("target_port", ""), item["source_node"], item["source_port"]))


def recorded_decisions(recipe: dict, chosen: set[str], recorded: dict, native_steps: dict) -> dict:
    """Project only native decisions backed by the exact selected dependency graph."""
    import hashlib
    import json
    if not isinstance(recorded, dict):
        return {}
    nodes = {node["id"]: node for node in recipe["nodes"] if node["id"] in chosen}
    accepted, skipped = {}, set()
    for node_id, node in nodes.items():
        outcome = recorded.get(node_id)
        if not isinstance(outcome, dict) or outcome.get("status_source") != "native_run_metadata":
            continue
        gate = node.get("condition")
        if (not gate or gate["source_node"] not in chosen or outcome.get("condition") != gate
                or type(outcome.get("gate_value")) is not bool):
            continue
        value = outcome["gate_value"]
        expected = "eligible" if condition_decision(gate, value) else "skipped"
        producer = native_steps.get(gate["source_node"])
        artifacts = producer.outputs.get(gate["source_port"], []) if producer else []
        if (outcome.get("status") != expected or not any(str(artifact.id) == outcome.get("gate_artifact_id") for artifact in artifacts)
                or outcome.get("gate_value_sha256") != hashlib.sha256(json.dumps(value).encode()).hexdigest()):
            continue
        accepted[node_id] = outcome
        if expected == "skipped" and node_id not in native_steps:
            skipped.add(node_id)
    # A recorded transitive skip must terminate at a verified native gate
    # decision. Bounded fixed-point projection tolerates arbitrary JSON ordering.
    pending = set(nodes) - set(accepted)
    while pending:
        progress = set()
        for node_id in pending:
            outcome = recorded.get(node_id)
            if (not isinstance(outcome, dict) or outcome.get("status_source") != "native_run_metadata"
                    or outcome.get("status") != "skipped" or outcome.get("reason") != "upstream_skipped"
                    or outcome.get("condition") != nodes[node_id].get("condition") or node_id in native_steps):
                continue
            blocked = skipped_dependencies(recipe, nodes[node_id], chosen, skipped)
            if blocked and outcome.get("blocked_by") == blocked:
                accepted[node_id] = outcome
                skipped.add(node_id)
                progress.add(node_id)
        if not progress:
            break
        pending -= progress
    return accepted


def validate_selected_gates(recipe: dict, chosen: set[str]) -> None:
    for node in recipe.get("nodes", []):
        if node['id'] in chosen and not set(node.get('required_guards', [])) <= chosen:
            raise ConditionError('required_guard_omitted', 'Selected actions require their declared rule guards; saved input bindings cannot replace them.', node_id=node['id'])
        if node["id"] in chosen and "condition" in node and node["condition"]["source_node"] not in chosen:
            raise ConditionError("unselected_condition_gate", "Select the declared boolean gate explicitly. Conditional execution never adds or executes unselected ancestors.",
                                 node_id=node["id"], gate_node_id=node["condition"]["source_node"])


def condition_decision(condition: dict, value) -> bool:
    if type(value) is not bool:
        raise ValueError("Native condition artifact must contain an actual boolean; truthiness coercion is forbidden")
    return value == condition["equals"]
