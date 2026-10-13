"""Inert typed epoch evidence selection and non-scientific admission bounds.

Selectors only copy measured platform facts. No expressions, inference, imports
or method execution occur during option validation.
"""

from copy import deepcopy
import math

from . import identity
from ...rules import CONVERGENCE, _number

BOUND_KEYS = (
    "max_epochs",
    "max_steps",
    "max_runtime_seconds",
    "max_cost",
    "next_epoch_steps",
    "next_epoch_seconds",
    "next_epoch_cost",
    "resource_modes",
    "step_unit",
    "cost_unit",
)
BOUNDS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {key: deepcopy(CONVERGENCE["properties"][key]) for key in BOUND_KEYS},
    "required": list(BOUND_KEYS),
}
_SOURCES = [
    "status",
    "completed_epochs",
    "steps",
    "runtime_seconds",
    "cost",
    "checkpoint_digest",
    "expected_checkpoint_digest",
    "continuation",
    "resource_units",
    "identities",
    "lineage",
    "checkpoint_state",
    "epoch_metrics",
    "metric_history",
    "observations",
]
SELECTOR = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"source": {"enum": _SOURCES}, "metric": {"enum": ["eval_loss", "eval_accuracy"]}},
    "required": ["source"],
}
BINDINGS = {"type": "object", "additionalProperties": {"type": "object", "additionalProperties": SELECTOR}}
COMMON = {
    "status",
    "completed_epochs",
    "steps",
    "runtime_seconds",
    "cost",
    "checkpoint_digest",
    "expected_checkpoint_digest",
    "continuation",
    "resource_units",
}


def is_continuation(rule):
    return (
        rule["kind"] == "convergence/v1"
        or rule["kind"] == "method/v1"
        and rule["method"]["decision_domain"] == "continuation"
    )


def selected_bounds(parameters):
    if "epoch_bounds" in parameters:
        return deepcopy(parameters["epoch_bounds"])
    # Explicit compatibility with the first v2 recipe form. Nothing is guessed
    # for a custom or combined policy: those must select independent bounds.
    rules = parameters["policy"]["rules"]
    if len(rules) != 1 or rules[0]["kind"] != "convergence/v1":
        raise ValueError("epoch_bounds required for custom or combined epoch policies")
    return {key: deepcopy(rules[0][key]) for key in BOUND_KEYS}


def validate_options(parameters):
    policy = parameters["policy"]
    bounds = selected_bounds(parameters)
    if any(type(bounds[key]) is not int for key in ("max_epochs", "max_steps", "next_epoch_steps")):
        raise ValueError("Epoch count/step bounds require actual integers")
    if (
        bounds["max_epochs"] > parameters["horizon_epochs"]
        or bounds["step_unit"] != "optimizer_steps"
        or bounds["cost_unit"] != "local-free"
        or bounds["resource_modes"]["steps"] != "active"
        or bounds["resource_modes"]["cost"] != "not_applicable"
        or bounds["max_cost"] != 0
        or bounds["next_epoch_cost"] != 0
        or bounds["resource_modes"]["runtime_seconds"] == "not_applicable"
        and (bounds["max_runtime_seconds"] != 0 or bounds["next_epoch_seconds"] != 0)
    ):
        raise ValueError("Epoch bounds conflict with supported local resource accounting/horizon")
    if not any(is_continuation(rule) for rule in policy["rules"]):
        raise ValueError("Epoch policy requires a declared continuation decision domain")
    bindings = parameters.get("evidence_bindings", {})
    custom_ids = {rule["id"] for rule in policy["rules"] if rule["kind"] != "convergence/v1"}
    if set(bindings) != custom_ids:
        raise ValueError("Explicit evidence_bindings must cover every non-convergence rule exactly")
    metrics = {"eval_loss"}
    for rule in policy["rules"]:
        if rule["kind"] == "convergence/v1":
            if bounds["max_epochs"] > 10000:
                raise ValueError(
                    "convergence/v1 supports at most 10000 full-history epoch observations; choose a supported horizon"
                )
            if rule["metric"] not in {"eval_loss", "eval_accuracy"}:
                raise ValueError("Selected epoch metric is unavailable from Trainer")
            # Each convergence rule retains its user-selected bounds and units.
            if any(identity(rule[key]) != identity(bounds[key]) for key in BOUND_KEYS):
                raise ValueError("Convergence and platform epoch bounds must agree explicitly")
            metrics.add(rule["metric"])
            continue
        if rule["kind"] == "representation/v1":
            raise ValueError("Epoch adapter cannot obtain registered before/after stratum count receipts")
        if rule["kind"] == "identity/v1" and bindings[rule["id"]] != {"actual": {"source": "identities"}}:
            raise ValueError("Epoch identity policy requires explicit measured identity evidence")
        if rule["kind"] == "overlap/v1":
            raise ValueError(
                "Epoch adapter cannot obtain independent cross_run/cross_lineage comparison receipts"
            )
        for name, selector in bindings[rule["id"]].items():
            source = selector["source"]
            if source in {"metric_history", "observations"}:
                if set(selector) != {"source", "metric"}:
                    raise ValueError(
                        "Metric evidence binding requires one explicitly selected measured metric"
                    )
                metrics.add(selector["metric"])
            elif set(selector) != {"source"}:
                raise ValueError("Non-metric evidence binding cannot select a metric")
            if is_continuation(rule) and name in COMMON:
                raise ValueError("Method bindings cannot overwrite shared execution safety facts")
        if rule["kind"] not in {"method/v1", "identity/v1", "overlap/v1", "representation/v1"}:
            raise ValueError("Unsupported declared epoch policy rule")
    if "eval_accuracy" in metrics and parameters["architecture"]["task"] != "sequence-classification":
        raise ValueError("Selected accuracy evidence is unavailable for this task")
    if any(s["source"] == "epoch_metrics" for b in bindings.values() for s in b.values()):
        metrics.update(
            {"eval_loss", "eval_accuracy"}
            if parameters["architecture"]["task"] == "sequence-classification"
            else {"eval_loss"}
        )
    return bounds, metrics


def admission(bounds, completed, steps, runtime):
    active = [("max_epochs", completed, 1), ("max_steps", steps, bounds["next_epoch_steps"])]
    if bounds["resource_modes"]["runtime_seconds"] == "active":
        active.append(("max_runtime_seconds", runtime, bounds["next_epoch_seconds"]))
    if any(
        type(value) not in (int, float) or not math.isfinite(value) or value < 0
        for value in (completed, steps, runtime)
    ):
        raise ValueError("Invalid authoritative epoch resource consumption")
    overrun = [key for key, used, _ in active if _number(used) > _number(bounds[key])]
    exhausted = [
        key
        for key, used, inc in active
        if _number(used) >= _number(bounds[key]) or _number(used) + _number(inc) > _number(bounds[key])
    ]
    return {
        "outcome": "blocked" if overrun else "stop" if exhausted else "continue",
        "reasons": ["resource_bound_exceeded:" + key for key in overrun]
        if overrun
        else ["resource_bound:" + key for key in exhausted] or ["epoch_budget_available"],
        "selected_bounds": deepcopy(bounds),
        "consumed": {"completed_epochs": completed, "steps": steps, "runtime_seconds": runtime, "cost": 0},
    }


def evidence(parameters, subject, history, lineage, state, cursor, identities):
    checkpoint = identity(state)
    common = {
        **cursor,
        "status": "completed",
        "checkpoint_digest": checkpoint,
        "expected_checkpoint_digest": checkpoint,
        "continuation": {"mode": "exact_state", "supported": True},
        "resource_units": {
            "epochs": "complete_dataset_pass",
            "steps": "optimizer_steps",
            "runtime_seconds": "seconds",
            "cost": "local-free",
        },
    }
    sources = {
        **common,
        "lineage": lineage,
        "checkpoint_state": state,
        "epoch_metrics": history[-1]["metrics"],
        "identities": {"status": "completed", **identities},
    }

    def select(selector):
        source = selector["source"]
        if source in {"metric_history", "observations"}:
            metric = selector["metric"]
            if any(metric not in row["metrics"] for row in history):
                raise ValueError("Selected metric evidence is missing: " + metric)
            if source == "metric_history":
                return [row["metrics"][metric] for row in history]
            return [
                {
                    "epoch": row["epoch"],
                    "metric": metric,
                    "value": row["metrics"][metric],
                    "checkpoint_digest": row["checkpoint_digest"],
                    "subject_digest": row["subject_digest"],
                }
                for row in history
            ]
        return deepcopy(sources[source])

    facts = {}
    for rule in parameters["policy"]["rules"]:
        if rule["kind"] == "convergence/v1":
            facts[rule["id"]] = {
                **deepcopy(common),
                "observations": select({"source": "observations", "metric": rule["metric"]}),
            }
        else:
            bound = {
                name: select(selector)
                for name, selector in parameters["evidence_bindings"][rule["id"]].items()
            }
            facts[rule["id"]] = {**deepcopy(common), **bound} if is_continuation(rule) else bound
    return {
        "schema_version": "modelbuilder.rule-evidence/v1",
        "subject": deepcopy(subject),
        "source": "modelbuilder.transformers.train/v2:completed-epoch-heldout",
        "verification": "verified",
        "facts": facts,
    }
