"""Deterministic, fail-closed functionality-family coverage evidence.

Dataset counts, checkpoint exposure, and evaluation evidence are independent.
This module never infers one from another or chooses a research taxonomy.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from fractions import Fraction
from typing import Any, Literal, TypedDict


class CoverageAssessment(TypedDict):
    status: Literal["passed", "failed", "unknown"]
    policy_id: str
    families: dict[str, dict[str, Any]]
    failures: list[str]
    unknowns: list[str]


_METRIC_SCHEMA = {
    "type": "object", "additionalProperties": False, "minProperties": 1,
    "properties": {"min": {"type": "number"}, "max": {"type": "number"},
                   "max_regression": {"type": "number", "minimum": 0},
                   "direction": {"enum": ["higher", "lower"]}},
    "dependentRequired": {"max_regression": ["direction"], "direction": ["max_regression"]},
}
FAMILY_POLICY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "mapping", "families"],
    "properties": {
        "schema_version": {"const": "modelbuilder.family-policy.v1"},
        "mapping": {"type": "object", "additionalProperties": False,
                    "required": ["version", "field"],
                    "properties": {k: {"type": "string", "minLength": 1, "maxLength": 200} for k in ("version", "field")}},
        "families": {"type": "array", "minItems": 1, "maxItems": 256, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "min_rows", "min_exposure", "min_eval_support"],
            "properties": {"id": {"type": "string", "minLength": 1, "maxLength": 200},
                           "min_rows": {"type": "integer", "minimum": 0},
                           "min_exposure": {"type": "integer", "minimum": 0},
                           "min_eval_support": {"type": "integer", "minimum": 1},
                           "metrics": {"type": "object", "maxProperties": 256, "propertyNames": {"minLength": 1, "maxLength": 200},
                                       "additionalProperties": _METRIC_SCHEMA}}}},
        "research_sources": {"type": "array", "maxItems": 256, "uniqueItems": True,
                             "items": {"type": "string", "minLength": 1, "maxLength": 2048}},
    },
}


def _object(value: Any, label: str) -> Mapping:
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        raise ValueError(f"{label} must be an object with string keys")
    return value


def _keys(value: Mapping, required: set[str], optional: set[str], label: str) -> None:
    if required - value.keys() or value.keys() - required - optional:
        raise ValueError(f"{label} has missing or unsupported fields")


def _string(value: Any, label: str, maximum: int = 200) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded nonempty string")


def _count(value: Any, label: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")


def _number(value: Any, label: str) -> None:
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ValueError(f"{label} must be a finite number")


def validate_policy(policy: Mapping[str, Any]) -> None:
    """Raise ValueError for malformed, ambiguous, or unsupported policy data."""
    _object(policy, "policy")
    _keys(policy, {"schema_version", "mapping", "families"}, {"research_sources"}, "policy")
    if policy["schema_version"] != "modelbuilder.family-policy.v1":
        raise ValueError("unsupported family policy schema_version")
    mapping = _object(policy["mapping"], "mapping")
    _keys(mapping, {"version", "field"}, set(), "mapping")
    for key, value in mapping.items():
        _string(value, f"mapping.{key}")
    if not isinstance(policy["families"], list) or not 1 <= len(policy["families"]) <= 256:
        raise ValueError("families must be a nonempty array")
    seen = set()
    for family in policy["families"]:
        _object(family, "family")
        _keys(family, {"id", "min_rows", "min_exposure", "min_eval_support"}, {"metrics"}, "family")
        _string(family["id"], "family.id")
        if family["id"] in seen:
            raise ValueError(f"duplicate family: {family['id']}")
        seen.add(family["id"])
        for key in ("min_rows", "min_exposure", "min_eval_support"):
            _count(family[key], key, 1 if key == "min_eval_support" else 0)
        metrics = _object(family.get("metrics", {}), "metrics")
        if len(metrics) > 256:
            raise ValueError("too many metrics")
        for name, rule in metrics.items():
            _string(name, "metric name")
            _object(rule, "metric rule")
            _keys(rule, set(), {"min", "max", "max_regression", "direction"}, "metric rule")
            if not rule or ("direction" in rule) != ("max_regression" in rule):
                raise ValueError("metric rule requires a threshold; regression requires direction")
            for key in rule.keys() - {"direction"}:
                _number(rule[key], key)
            if "min" in rule and "max" in rule and rule["min"] > rule["max"]:
                raise ValueError("metric min exceeds max")
            if "max_regression" in rule and (
                rule["max_regression"] < 0 or rule["direction"] not in ("higher", "lower")
            ):
                raise ValueError("invalid regression rule")
    sources = policy.get("research_sources", [])
    if not isinstance(sources, list) or len(sources) > 256:
        raise ValueError("research_sources must be an array")
    for source in sources:
        _string(source, "research source", 2048)
    if len(set(sources)) != len(sources):
        raise ValueError("duplicate research source")


def canonical_policy_identity(policy: Mapping[str, Any]) -> str:
    """Hash canonical JSON; family/source order is semantically irrelevant."""
    validate_policy(policy)
    normalized = dict(policy)
    normalized["families"] = sorted(policy["families"], key=lambda family: family["id"])
    normalized["research_sources"] = sorted(policy.get("research_sources", []))
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def family_for_row(row: Mapping[str, Any], policy: Mapping[str, Any]) -> str:
    """Read exactly one top-level scalar label; no inference or fallback."""
    validate_policy(policy)
    return _family_for_row(row, policy)


def _family_for_row(row: Mapping[str, Any], policy: Mapping[str, Any]) -> str:
    _object(row, "row")
    value = row.get(policy["mapping"]["field"])
    if not isinstance(value, str) or value not in {f["id"] for f in policy["families"]}:
        raise ValueError(f"missing or unsupported family label: {value!r}")
    return value


def _result(policy: Mapping[str, Any]) -> CoverageAssessment:
    return {"status": "unknown", "policy_id": canonical_policy_identity(policy),
            "families": {}, "failures": [], "unknowns": []}


def _finish(result: CoverageAssessment) -> CoverageAssessment:
    result["status"] = "failed" if result["failures"] else "unknown" if result["unknowns"] else "passed"
    return result


def summarize_rows(rows: Iterable[Mapping[str, Any]], policy: Mapping[str, Any]) -> dict[str, Any]:
    """Assess final materialized rows, after all filtering, truncation and deduplication."""
    validate_policy(policy)
    counts = {f["id"]: 0 for f in policy["families"]}
    for row in rows:
        counts[_family_for_row(row, policy)] += 1
    return assess_composition(policy, counts)


def assess_composition(policy: Mapping[str, Any], measured_counts: Mapping[str, int]) -> dict[str, Any]:
    """Assess complete final-dataset counts; omitted families have zero rows.

    This function requires a complete composition count, unlike partial exposure
    evidence. Callers must bind its result to the final artifact and split.
    """
    result = _result(policy)
    _known_families(measured_counts, policy)
    for name, count in measured_counts.items():
        _count(count, f"{name} row count")
    counts = {f["id"]: measured_counts.get(f["id"], 0) for f in policy["families"]}
    for family in policy["families"]:
        name = family["id"]
        passed = counts[name] >= family["min_rows"]
        result["families"][name] = {"status": "passed" if passed else "failed",
                                     "count": counts[name], "minimum": family["min_rows"]}
        if not passed:
            result["failures"].append(f"{name}: row count below minimum")
    return {**_finish(result), "counts": counts, "total_rows": sum(counts.values())}


def _known_families(measured: Mapping, policy: Mapping) -> None:
    _object(measured, "measurements")
    if measured.keys() - {f["id"] for f in policy["families"]}:
        raise ValueError("measurements contain unsupported families")


def assess_exposure(policy: Mapping[str, Any], measured_counts: Mapping[str, int]) -> CoverageAssessment:
    """Assess observed training presentations at one checkpoint, never available rows."""
    result = _result(policy)
    _known_families(measured_counts, policy)
    for name, count in measured_counts.items():
        _count(count, f"{name} exposure")
    for family in policy["families"]:
        name = family["id"]
        count = measured_counts.get(name)
        status = "unknown" if count is None else "passed" if count >= family["min_exposure"] else "failed"
        result["families"][name] = {"status": status, "count": count, "minimum": family["min_exposure"]}
        if status == "unknown":
            result["unknowns"].append(f"{name}: exposure evidence missing")
        elif status == "failed":
            result["failures"].append(f"{name}: exposure below minimum")
    return _finish(result)


def _validate_evidence(evidence: Mapping, policy: Mapping) -> None:
    _known_families(evidence, policy)
    for name, item in evidence.items():
        _object(item, f"{name} evaluation")
        _keys(item, set(), {"support", "metrics"}, f"{name} evaluation")
        if "support" in item:
            _count(item["support"], f"{name} support")
        for metric, value in _object(item.get("metrics", {}), "measured metrics").items():
            _string(metric, "metric name")
            _number(value, f"{name}.{metric}")


def assess_evaluation(
    policy: Mapping[str, Any], measured: Mapping[str, Any], baseline: Mapping[str, Any] | None = None,
) -> CoverageAssessment:
    """Assess support and configured scalar metrics; absent required evidence is unknown.

    Regression is absolute metric-unit degradation relative to a supported baseline.
    Extra metric names are allowed for auxiliary diagnostics; extra families are not.
    """
    result = _result(policy)
    _validate_evidence(measured, policy)
    if baseline is not None:
        _validate_evidence(baseline, policy)
    for family in policy["families"]:
        name = family["id"]
        item = measured.get(name, {})
        failures, unknowns = [], []
        support = item.get("support")
        if support is None:
            unknowns.append("evaluation support missing")
        elif support < family["min_eval_support"]:
            failures.append("evaluation support below minimum")
        for metric, rule in family.get("metrics", {}).items():
            value = item.get("metrics", {}).get(metric)
            if value is None:
                unknowns.append(f"{metric}: metric missing")
                continue
            if "min" in rule and value < rule["min"]:
                failures.append(f"{metric}: below minimum")
            if "max" in rule and value > rule["max"]:
                failures.append(f"{metric}: above maximum")
            if "max_regression" in rule:
                reference = (baseline or {}).get(name, {})
                previous = reference.get("metrics", {}).get(metric)
                if reference.get("support", 0) < family["min_eval_support"] or previous is None:
                    unknowns.append(f"{metric}: supported baseline missing")
                else:
                    old, new = Fraction(str(previous)), Fraction(str(value))
                    degradation = old - new if rule["direction"] == "higher" else new - old
                    if degradation > Fraction(str(rule["max_regression"])):
                        failures.append(f"{metric}: regression exceeds tolerance")
        status = "failed" if failures else "unknown" if unknowns else "passed"
        result["families"][name] = {"status": status, "support": support,
                                     "minimum": family["min_eval_support"],
                                     "failures": failures, "unknowns": unknowns}
        result["failures"].extend(f"{name}: {reason}" for reason in failures)
        result["unknowns"].extend(f"{name}: {reason}" for reason in unknowns)
    return _finish(result)
