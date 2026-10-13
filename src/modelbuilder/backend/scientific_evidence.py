"""Bind scientific rules to verified, exact native evaluation outputs."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path

from .adapter import BackendError
from ..evaluation import digest
from ..scientific import evaluate_policy
from ..scientific_operations import BINDINGS
from jsonschema import Draft202012Validator


def evaluate_native_policy(records, policy, bindings, *, native_gate=None):
    Draft202012Validator(BINDINGS).validate(bindings)
    if len({b["rule_id"] for b in bindings}) != len(bindings):
        raise ValueError("Scientific rule bindings must be distinct")
    by_run = {r["backend_run_id"]: r for r in records}
    evidence = {}
    pairs = set()
    for binding in bindings:
        pair = {}
        pairs.add((binding["baseline"]["backend_run_id"], binding["candidate"]["backend_run_id"]))
        for side in ("baseline", "candidate"):
            selector = binding[side]
            run = by_run.get(selector["backend_run_id"])
            if run is None or run.get("status") != "succeeded":
                raise BackendError("invalid_evaluation_selection", "Scientific policy requires completed owned native runs")
            entries = [entry for entry in run.get("evaluations", []) if all(entry.get(k) == v for k, v in selector.items())]
            if len(entries) != 1 or not entries[0].get("checkpoint_verification", {}).get("verified"):
                raise BackendError("evaluation_checkpoint_unverified", "Scientific policy evidence is absent, ambiguous or not checkpoint-verified")
            pair[side] = {"selector": selector, "record": entries[0]["record"], "model_version_ref": entries[0]["model_version_ref"]}
        evidence[binding["rule_id"]] = pair
    if len(pairs) != 1 or next(iter(pairs))[0] == next(iter(pairs))[1]:
        raise ValueError("All scientific rules must compare the same distinct baseline/candidate runs")
    if set(next(iter(pairs))) != set(by_run):
        raise ValueError("Scientific comparison requires exactly its baseline and candidate runs")
    # Every candidate metric must belong to one actual checkpoint/model. Metrics
    # from different checkpoints in one run cannot be pooled into a passing gate.
    for side in ("baseline", "candidate"):
        identities = {(p[side]["record"]["treatment"]["checkpoint"], p[side]["model_version_ref"]) for p in evidence.values()}
        if len(identities) != 1:
            raise ValueError("Scientific metrics must evaluate one checkpoint/model per side")
    if native_gate is None:
        if policy.get("evaluator"):
            raise ValueError("Policy requires its pinned native gate implementation")
        result = evaluate_policy(policy, evidence)
    else:
        from .scientific_gate_artifacts import validate_result
        raw_evidence = {rid: {side: {"record": row[side]["record"]} for side in ("baseline", "candidate")} for rid, row in evidence.items()}
        validate_result(native_gate, policy, raw_evidence)
        receipts = {r["input_port"]: r for r in native_gate["native_execution"]["inputs"]}
        for pair in evidence.values():
            for side in ("baseline", "candidate"):
                if any(receipts[side][key] != pair[side]["selector"][key] for key in ("backend_run_id", "artifact_id")):
                    raise ValueError("Native gate consumed a different evaluation artifact/run")
        result = deepcopy(native_gate)
        result["native_result_digest"] = native_gate["digest"]
        for row in result["rules"]:
            row["evidence"] = evidence[row["rule_id"]]
    result["native_verified"] = True
    result["baseline_run_id"], result["candidate_run_id"] = next(iter(pairs))
    result["candidate_model_version_ref"] = next(iter(evidence.values()))["candidate"]["model_version_ref"]
    result["digest"] = digest({k: v for k, v in result.items() if k != "digest"})
    return result


def apply_policy(comparison, records, payload):
    if not payload.get("scientific_policy"):
        return comparison
    native_gate = None
    if payload.get("scientific_gate"):
        from .scientific_gate_artifacts import load_gate
        native_gate = load_gate(payload["scientific_policy"], payload["scientific_gate"], Path(payload["_scientific_root"]), payload["project_id"])
    result = evaluate_native_policy(records, payload["scientific_policy"], payload.get("scientific_evaluations"), native_gate=native_gate)
    if native_gate is not None:
        result["scientific_gate"] = deepcopy(payload["scientific_gate"])
        result["digest"] = digest({k: v for k, v in result.items() if k != "digest"})
    comparison["scientific_result"] = result
    if not result["passed"]:
        comparison["diagnostics"].append({"code": "scientific_gate_blocked", "field": "scientific_policy", "message": "Scientific policy outcome: " + result["outcome"]})
        comparison.update(compatible=False, selected_run_id=None, selected_model_version_ref=None)
        comparison.pop("comparison_digest", None)
    elif comparison.get("selected_model_version_ref") != result["candidate_model_version_ref"]:
        comparison["diagnostics"].append({"code": "scientific_candidate_not_selected", "field": "scientific_policy", "message": "The gated candidate does not win the declared selection metric"})
        comparison.update(compatible=False, selected_run_id=None, selected_model_version_ref=None)
        comparison.pop("comparison_digest", None)
    if comparison.get("compatible"):
        comparison["comparison_digest"] = digest({k: v for k, v in comparison.items() if k != "comparison_digest"})
    return comparison
