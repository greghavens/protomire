"""Deterministic, versioned scientific policy contracts and small verified methods.

No universal sample count or significance promise. Custom methods are supplied by
reviewed source-bound components as callables plus exact implementation/schema
identities. The control plane never imports an implementation from a payload.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
import re
from statistics import NormalDist, fmean
from typing import Callable

from jsonschema import Draft202012Validator

from .evaluation import compare_records, digest, validate_evaluation_record
from .family_coverage import FAMILY_POLICY_SCHEMA
from .family_evaluation import family_metric_id, scientific_family_diagnostics, scientific_family_failures, scientific_partition_diagnostics

TEXT = {"type": "string", "minLength": 1}
NUMBER = {"type": "number"}
POSITIVE = {"type": "number", "exclusiveMinimum": 0}
PROBABILITY = {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1}
METHOD_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"id": TEXT, "version": TEXT, "implementation": {"type": "object", "minProperties": 1}, "parameters": {"type": "object"}},
    "required": ["id", "version", "implementation", "parameters"]}
RULE_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"id": TEXT, "metric": TEXT, "direction": {"enum": ["minimize", "maximize"]},
                   "minimum_improvement": NUMBER, "method": METHOD_SCHEMA},
    "required": ["id", "metric", "direction", "minimum_improvement", "method"]}
POLICY_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"schema": {"const": "modelbuilder.scientific-policy/v1"}, "id": TEXT, "version": TEXT,
        "purpose": {"enum": ["selection", "continuation", "promotion"]},
        "family_error_rate": PROBABILITY, "multiple_testing": {"const": "bonferroni"},
        "allowed_variations": {"type": "array", "uniqueItems": True, "items": {"enum": ["model", "checkpoint", "architecture", "objective", "calibration", "reference"]}},
        "rules": {"type": "array", "minItems": 1, "maxItems": 32, "items": RULE_SCHEMA},
        "unknown_behavior": {"const": "block"},
        "family_policy": FAMILY_POLICY_SCHEMA,
        "family_metrics": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"enum": ["cross_entropy", "accuracy", "perplexity"]}},
        "required_benchmark": TEXT,
        "required_evaluation": {"type": "object", "additionalProperties": False,
            "properties": {"dataset_digest": {"type": "string", "minLength": 64, "maxLength": 64},
                "split_digest": {"type": "string", "minLength": 64, "maxLength": 64},
                "sample_count": {"type": "integer", "minimum": 1}},
            "required": ["dataset_digest", "split_digest", "sample_count"]},
        "evaluator": {"type": "object", "additionalProperties": False,
            "properties": {"component_digest": {"type": "string", "minLength": 64, "maxLength": 64},
                           "implementation_ref": {"type": "object", "minProperties": 1}},
            "required": ["component_digest", "implementation_ref"]}},
    "required": ["schema", "id", "version", "purpose", "family_error_rate", "multiple_testing", "allowed_variations", "rules", "unknown_behavior"]}
KNOWN_SD = {"type": "object", "additionalProperties": False,
    "properties": {"population_difference_sd": POSITIVE, "detectable_effect": POSITIVE, "power": PROBABILITY,
        "variance_source": TEXT, "assumptions": {"const": {"independent_pairs": True, "normal_difference_population": True, "known_population_sd": True}},
        "alternative": {"const": "greater"}},
    "required": ["population_difference_sd", "detectable_effect", "power", "variance_source", "assumptions", "alternative"]}


@dataclass(frozen=True)
class Method:
    id: str
    version: str
    implementation: dict
    parameter_schema: dict
    evaluate: Callable


def known_sd_adequacy(parameters, alpha, count):
    """One-sided z test: known population SD, independent normal paired deltas.

    Effect is in metric units beyond the rule's declared null boundary. Power is
    the prospective design power at that effect, never observed/post-hoc power.
    """
    Draft202012Validator(KNOWN_SD).validate(parameters)
    if type(count) is not int or not 0 <= count <= 10**9 or not 0 < alpha < 1:
        raise ValueError("Sample design requires a finite alpha and zero to one billion available pairs")
    digest(parameters)
    normal = NormalDist()
    if parameters["power"] <= alpha:
        raise ValueError("Target design power must exceed the rule error rate")
    standardized = parameters["detectable_effect"] / parameters["population_difference_sd"]
    if not math.isfinite(standardized) or standardized <= 0:
        raise ValueError("Standardized design effect is outside the supported numerical range")
    ratio = ((-normal.inv_cdf(alpha)) + normal.inv_cdf(parameters["power"])) / standardized
    if not math.isfinite(ratio) or ratio > math.sqrt(10**9):
        raise ValueError("Sample planning exceeds the supported finite design bound")
    required = ratio ** 2
    if not math.isfinite(required) or required > 10**9:
        raise ValueError("Sample planning exceeds the supported finite design bound")
    n = max(1, math.ceil(required))
    achieved_design_power = 0.5 * math.erfc(((-normal.inv_cdf(alpha)) - math.sqrt(count) * standardized) / math.sqrt(2)) if count else None
    return {"method": "normal-known-sd-one-sided/v1", "required_pairs": n, "available_pairs": count,
            "adequate": count >= n, "alpha": alpha, "design_power_at_available_n": achieved_design_power,
            "assumptions": deepcopy(parameters), "limitations": "Conditional on supplied known population SD and independent normal paired differences; not an empirical guarantee."}


def scalar_threshold(*, baseline, candidate, rule, alpha):
    improvement = (candidate["value"] - baseline["value"]) * (1 if rule["direction"] == "maximize" else -1)
    return {"outcome": "passed" if improvement >= rule["minimum_improvement"] else "failed",
        "improvement": improvement, "threshold": rule["minimum_improvement"], "uncertainty": None,
        "significance_claim": False, "sample_adequacy": None,
        "limitations": "Descriptive scalar threshold only; no sampling uncertainty or adequacy claim."}


def paired_normal_known_sd(*, baseline, candidate, rule, alpha):
    params = rule["method"]["parameters"]
    if baseline["metric"]["aggregation"] != "mean":
        return {"outcome": "unknown", "reason": "Method requires a decomposable mean metric"}
    ids = baseline["split"]["sample_ids"]
    a = {p["item_id"]: p.get("value") for p in baseline.get("predictions", [])}
    b = {p["item_id"]: p.get("value") for p in candidate.get("predictions", [])}
    if set(a) != set(ids) or set(b) != set(ids) or any(type(v) not in (float, int) or not math.isfinite(v) for v in [*a.values(), *b.values()]):
        return {"outcome": "unknown", "reason": "Exact finite paired item contributions are required"}
    deltas = [(b[key] - a[key]) * (1 if rule["direction"] == "maximize" else -1) for key in ids]
    adequacy = known_sd_adequacy(params, alpha, len(deltas))
    mean = fmean(deltas)
    se = params["population_difference_sd"] / math.sqrt(len(deltas))
    if not math.isfinite(se) or se <= 0:
        return {"outcome": "unknown", "reason": "Standard error is outside the supported floating-point range", "sample_adequacy": adequacy, "significance_claim": False}
    z = (mean - rule["minimum_improvement"]) / se
    normal = NormalDist()
    p = 0.5 * math.erfc(z / math.sqrt(2))
    lower = mean - (-normal.inv_cdf(alpha)) * se
    return {"outcome": "unknown" if not adequacy["adequate"] else "passed" if p <= alpha else "failed",
        "reason": "Insufficient prospective design sample" if not adequacy["adequate"] else None,
        "improvement": mean, "threshold": rule["minimum_improvement"], "sample_adequacy": adequacy,
        "uncertainty": {"method": "normal-known-sd-one-sided/v1", "standard_error": se, "z": z,
                        "p_value": p, "lower_confidence_bound": lower, "confidence": 1 - alpha},
        "significance_claim": adequacy["adequate"] and p <= alpha,
        "limitations": adequacy["limitations"]}


def builtin_methods():
    return [Method("scalar_threshold", "1", {"module": __name__, "entrypoint": "scalar_threshold"},
                   {"type": "object", "additionalProperties": False}, scalar_threshold),
            Method("paired_normal_known_sd", "1", {"module": __name__, "entrypoint": "paired_normal_known_sd"}, KNOWN_SD, paired_normal_known_sd)]


def method_catalog():
    return [{"id": m.id, "version": m.version, "implementation": m.implementation, "parameter_schema": m.parameter_schema}
            for m in builtin_methods()]


def validate_policy(policy):
    Draft202012Validator(POLICY_SCHEMA).validate(policy)
    digest(policy)  # finite JSON only
    if any(not re.fullmatch(r"[0-9a-f]{64}", policy.get("required_evaluation", {}).get(key, "0" * 64))
           for key in ("dataset_digest", "split_digest")):
        raise ValueError("Required evaluation cohort digests must be lowercase SHA256")
    if ("family_policy" in policy) != ("family_metrics" in policy):
        raise ValueError("Scientific family policy and family_metrics must be configured together")
    if "family_policy" in policy:
        from .family_coverage import validate_policy as validate_family_policy
        validate_family_policy(policy["family_policy"])
        if any(set(family.get("metrics", {})) - set(policy["family_metrics"]) for family in policy["family_policy"]["families"]):
            raise ValueError("Scientific family_metrics must include all family policy metric obligations")
        required = {family_metric_id(family["id"], metric)
                    for family in policy["family_policy"]["families"] for metric in policy["family_metrics"]}
        if len(required) > POLICY_SCHEMA["properties"]["rules"]["maxItems"]:
            raise ValueError("Family scientific policy exceeds the supported 32 rules; no families may be truncated")
        if not required <= {rule["metric"] for rule in policy["rules"]}:
            raise ValueError("Scientific rules must cover every required family and metric")
    if policy.get("evaluator") and not re.fullmatch(r"[0-9a-f]{64}", policy["evaluator"]["component_digest"]):
        raise ValueError("Scientific evaluator component digest must be SHA256")
    if policy["family_error_rate"] / len(policy["rules"]) <= 0:
        raise ValueError("Multiplicity-adjusted error rate is outside the supported floating-point range")
    if len({r["id"] for r in policy["rules"]}) != len(policy["rules"]):
        raise ValueError("Scientific rule IDs must be distinct")
    return policy


def evaluate_policy(policy, evidence, *, extensions=()):
    """Evaluate one baseline/candidate evidence pair per rule. Unknown blocks.

    Evidence entries preserve native selectors when available. Supplying records
    alone is useful for tests/components, but is not native ownership verification.
    """
    validate_policy(policy)
    registered = {(m.id, m.version): m for m in builtin_methods()}
    for method in extensions:
        if (method.id, method.version) in registered:
            raise ValueError("Extensions cannot replace a registered method identity")
        registered[(method.id, method.version)] = method
    if set(evidence) != {r["id"] for r in policy["rules"]}:
        raise ValueError("Policy evidence must exactly match declared rule IDs")
    partition_diagnostics = scientific_partition_diagnostics(policy, evidence)
    results = []
    alpha = policy["family_error_rate"] / len(policy["rules"])
    for rule in policy["rules"]:
        pair = evidence[rule["id"]]
        result = {"rule_id": rule["id"], "metric": rule["metric"], "direction": rule["direction"], "method": deepcopy(rule["method"]),
                  "alpha": alpha, "threshold": rule["minimum_improvement"], "evidence": deepcopy(pair)}
        baseline, candidate = pair["baseline"]["record"], pair["candidate"]["record"]
        diagnostics = [*validate_evaluation_record(baseline), *validate_evaluation_record(candidate)]
        records = [{"backend_run_id": side, "model_version_ref": side, "status": "succeeded", "metrics": {rule["metric"]: item["record"].get("value"), "evaluation_record": item["record"]}} for side, item in pair.items()]
        comparison = compare_records(records, rule["metric"], rule["direction"], {"allowed_variations": policy["allowed_variations"]})
        diagnostics.extend(comparison["diagnostics"])
        diagnostics.extend(scientific_family_diagnostics(policy, rule, pair))
        diagnostics.extend(partition_diagnostics)
        method = registered.get((rule["method"]["id"], rule["method"]["version"]))
        if diagnostics:
            result.update(outcome="unknown", diagnostics=diagnostics)
        elif method is None or method.implementation != rule["method"]["implementation"]:
            result.update(outcome="unknown", reason="Method implementation unavailable; execute its reviewed source-bound component")
        else:
            Draft202012Validator(method.parameter_schema).validate(rule["method"]["parameters"])
            measured = method.evaluate(baseline=deepcopy(baseline), candidate=deepcopy(candidate), rule=deepcopy(rule), alpha=alpha)
            digest(measured)
            allowed_result_fields = {"outcome", "reason", "improvement", "threshold", "sample_adequacy", "uncertainty", "significance_claim", "limitations", "diagnostics"}
            if set(measured) - allowed_result_fields:
                raise ValueError("Scientific methods cannot overwrite policy or evidence identities")
            if measured.get("outcome") not in {"passed", "failed", "unknown"}:
                raise ValueError("Scientific method must return a typed outcome")
            result.update(measured)
            failures = scientific_family_failures(policy, rule, pair)
            if failures:
                result.update(outcome="failed", reason="; ".join(failures), significance_claim=False)
        result["cohort_digest"] = digest(baseline.get("split"))
        results.append(result)
    outcome = "failed" if any(r["outcome"] == "failed" for r in results) else "unknown" if any(r["outcome"] == "unknown" for r in results) else "passed"
    result = {"schema": "modelbuilder.scientific-result/v1", "policy": deepcopy(policy), "policy_digest": digest(policy),
        "outcome": outcome, "passed": outcome == "passed", "rules": results,
        "multiple_testing": {"method": "bonferroni", "family_error_rate": policy["family_error_rate"], "rule_error_rate": alpha,
                             "family_power_claim": False}}
    result["digest"] = digest(result)
    return result
