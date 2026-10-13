"""Reusable policy evaluation emits one typed bool for existing native branches."""
from copy import deepcopy
from . import PROTOCOL, identity
from ...scientific import POLICY_SCHEMA, evaluate_policy


def catalog_entries():
    descriptor = {"id": "modelbuilder.scientific.gate", "version": "1", "protocol": PROTOCOL,
        "role": "scientific_gate", "description": "Evaluate a pinned multi-metric policy on exact item-addressed evaluation records; unknown blocks the typed bool output.",
        "parameter_schema": {"type": "object", "additionalProperties": False, "properties": {"policy": POLICY_SCHEMA}, "required": ["policy"]},
        "capabilities": {"devices": ["cpu"], "resume_modes": [], "export_formats": [], "frameworks": ["python.statistics"]}}
    return [{**descriptor, "component_digest": identity(descriptor),
        "inputs": [{"name": name, "type": "dict"} for name in ("baseline", "candidate")],
        "outputs": [{"name": "passed", "type": "bool"}, {"name": "result", "type": "dict"}],
        "adapter": {"module": __name__, "entrypoint": "gate", "source_binding": "Pin a reviewed wrapper; custom methods supply versioned Method declarations to gate().",
                    "example_source": "examples/scientific_workflows/adapter.py"}}]


def gate(*, inputs, parameters, context, extensions=()):
    from ...evaluation import evaluation_for
    evidence = {}
    for rule in parameters["policy"]["rules"]:
        pair = {}
        for side in ("baseline", "candidate"):
            record = evaluation_for(inputs[side], rule["metric"])
            if record is None:
                record = {}
            pair[side] = {"record": deepcopy(record)}
        evidence[rule["id"]] = pair
    result = evaluate_policy(parameters["policy"], evidence, extensions=extensions)
    # Native input/output IDs remain in ZenML. This component report by itself
    # does not grant publication, spending, or promote any model version.
    return {"passed": result["passed"], "result": result}
