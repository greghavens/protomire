"""Resolve actual native invocations back to immutable canonical declarations."""
from __future__ import annotations

from ..iterations import invocation_identity, memberships


def invocation(step_name, step, recipe):
    nodes = {node["id"] for node in recipe.get("nodes", [])}
    member = memberships(recipe)
    if step_name in nodes and step_name not in member:
        return invocation_identity(step_name)
    extra = getattr(getattr(step, "config", None), "extra", {}) or {}
    node_id, index = extra.get("canonical_node_id"), extra.get("iteration")
    block = member.get(node_id)
    if block and type(index) is int and 1 <= index <= block["max_iterations"]:
        expected = invocation_identity(node_id, block, index)
        if expected["invocation_id"] == step_name and all(extra.get(key) == value for key, value in expected.items()):
            return expected
    raise ValueError("Native invocation does not match the immutable canonical node/block identity")


def invocations(run, recipe):
    return [(invocation(name, step, recipe), step) for name, step in run.steps.items()] if run else []


def output_step(run, recipe, node_id, port, artifact_id):
    matches = [(info, step) for info, step in invocations(run, recipe) if info["canonical_node_id"] == node_id
               and any(str(artifact.id) == artifact_id for artifact in step.outputs.get(port, []))]
    if len(matches) != 1:
        raise ValueError("Native output must identify exactly one declared producer invocation")
    return matches[0]


def execution_projection(run, recipe, chosen, metadata):
    """Reconstruct per-invocation and final-stage views from native identities.

    Iteration metadata can record a skipped/noninvoked call; actual StepRun
    states always win. A previous iteration cannot impersonate a missing latest
    invocation after failure, cancellation or a branch skip.
    """
    from ..conditions import recorded_decisions, skipped_dependencies
    member = memberships(recipe)
    entries = invocations(run, recipe)
    observed = {block["id"]: 0 for block in recipe.get("bounded_iterations", [])}
    saved = metadata.get("bounded_iterations", {})
    saved = saved if isinstance(saved, dict) else {}
    for info, _ in entries:
        if "iteration" in info:
            observed[info["iteration_block_id"]] = max(observed[info["iteration_block_id"]], info["iteration"])
    for block in recipe.get("bounded_iterations", []):
        rows = saved.get(block["id"], {})
        if isinstance(rows, dict):
            for key, row in rows.items():
                if (isinstance(row, dict) and type(row.get("iteration")) is int and str(row["iteration"]) == key
                        and 1 <= row["iteration"] <= block["max_iterations"] and row.get("max_iterations") == block["max_iterations"]
                        and row.get("status_source") == "native_run_metadata" and row.get("status") in {"started", "completed"}):
                    observed[block["id"]] = max(observed[block["id"]], row["iteration"])
    final_steps = {info["canonical_node_id"]: step for info, step in entries
                   if "iteration" not in info or info["iteration"] == observed[info["iteration_block_id"]]}
    decisions = metadata.get("conditional_execution", {})
    decisions = decisions if isinstance(decisions, dict) else {}
    joins = metadata.get("conditional_inputs", {})
    joins = joins if isinstance(joins, dict) else {}

    def scope(block=None, index=None):
        identities = {node["id"]: invocation_identity(node["id"], member.get(node["id"]),
            index if block and node["id"] in block["node_ids"] else observed.get(member.get(node["id"], {}).get("id")))
            for node in recipe["nodes"] if node["id"] not in member or observed[member[node["id"]]["id"]]}
        native = dict(final_steps)
        if block:
            for node_id in block["node_ids"]:
                native.pop(node_id, None)
            native.update({info["canonical_node_id"]: step for info, step in entries
                           if info.get("iteration_block_id") == block["id"] and info.get("iteration") == index})
        recorded = {}
        for node_id, info in identities.items():
            row = decisions.get(info["invocation_id"])
            if isinstance(row, dict) and (row.get("invocation", info) == info):
                recorded[node_id] = row
        accepted = recorded_decisions(recipe, chosen, recorded, native)
        omitted_nodes = {node_id for node_id, row in accepted.items() if row["status"] == "skipped" and node_id not in native}
        accepted_joins = {}
        for node in recipe["nodes"]:
            info = identities.get(node["id"])
            row = joins.get(info["invocation_id"]) if info else None
            step = native.get(node["id"])
            omitted = skipped_dependencies(recipe, node, chosen, omitted_nodes, optional=True)
            ports = {port["name"] for port in node.get("inputs", [])}
            actual = {port: str(artifacts[0].id) for port, artifacts in step.inputs.items() if port in ports and len(artifacts) == 1} if step else None
            if (omitted and isinstance(row, dict) and row.get("omitted_inputs") == omitted and row.get("input_artifact_ids") == actual
                    and row.get("status_source") == "native_run_metadata" and row.get("invocation", info) == info):
                accepted_joins[node["id"]] = row
        return accepted, omitted_nodes, accepted_joins, native

    final_decisions, skipped, final_joins, _ = scope()
    history, iteration_rows = {}, {}
    for block in recipe.get("bounded_iterations", []):
        block_rows = []
        for index in range(1, observed[block["id"]] + 1):
            accepted, omitted, accepted_joins, native = scope(block, index)
            statuses = {node_id: native[node_id].status.value if node_id in native else "skipped" if node_id in omitted else "pending"
                        for node_id in block["node_ids"]}
            for node_id in block["node_ids"]:
                info = invocation_identity(node_id, block, index)
                history[info["invocation_id"]] = {**info, "status": statuses[node_id],
                    "conditional_execution": accepted.get(node_id), "conditional_inputs": accepted_joins.get(node_id)}
            stored_rows = saved.get(block["id"], {})
            recorded = stored_rows.get(str(index), {}) if isinstance(stored_rows, dict) else {}
            body_completed = all(value in {"completed", "cached", "skipped"} for value in statuses.values())
            outcome = {"iteration": index, "node_statuses": statuses,
                       "status": "failed" if "failed" in statuses.values() else "incomplete"}
            if body_completed and isinstance(recorded, dict) and recorded.get("status") == "completed":
                reason = recorded.get("stop_reason")
                gate = block.get("until")
                if gate:
                    import hashlib
                    import json
                    artifacts = native.get(gate["source_node"]).outputs.get(gate["source_port"], []) if native.get(gate["source_node"]) else []
                    value = recorded.get("gate_value")
                    if (type(value) is bool and recorded.get("until") == gate and any(str(artifact.id) == recorded.get("gate_artifact_id") for artifact in artifacts)
                            and recorded.get("gate_value_sha256") == hashlib.sha256(json.dumps(value).encode()).hexdigest()
                            and reason == ("until" if value == gate["equals"] else "bound" if index == block["max_iterations"] else "continue")):
                        outcome.update(status="completed", **{key: recorded[key] for key in ("stop_reason", "gate_artifact_id", "gate_value", "gate_value_sha256")})
                elif reason == ("bound" if index == block["max_iterations"] else "continue"):
                    outcome.update(status="completed", stop_reason=reason)
            block_rows.append(outcome)
        iteration_rows[block["id"]] = {"max_iterations": block["max_iterations"], "iterations": block_rows,
            "status_source": "native_steps_and_run_metadata"}
    nodes = {node["id"]: node for node in recipe["nodes"]}
    for info, step in entries:
        row = history.setdefault(info["invocation_id"], {**info, "status": step.status.value})
        ports = {port["name"] for port in nodes[info["canonical_node_id"]].get("inputs", [])}
        row.update(step_id=str(step.id), input_artifact_ids={port: [str(artifact.id) for artifact in artifacts]
                   for port, artifacts in step.inputs.items() if port in ports})
    return final_steps, final_decisions, skipped, final_joins, history, iteration_rows
