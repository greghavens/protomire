import copy
import math

import pytest

from modelbuilder.family_coverage import (
    FAMILY_POLICY_SCHEMA, assess_composition, assess_evaluation, assess_exposure,
    canonical_policy_identity, family_for_row, summarize_rows, validate_policy,
)


def policy():
    return {"schema_version": "modelbuilder.family-policy.v1",
            "mapping": {"version": "research-v1", "field": "skill"},
            "families": [{"id": name, "min_rows": 1, "min_exposure": 2,
                          "min_eval_support": 2, "metrics": {"loss": {"max": 0.5}}}
                         for name in ("parse", "compose")]}


def evidence():
    return {name: {"support": 2, "metrics": {"loss": 0.4}} for name in ("parse", "compose")}


def test_schema_and_identity():
    from jsonschema import Draft202012Validator
    p = policy()
    Draft202012Validator.check_schema(FAMILY_POLICY_SCHEMA)
    Draft202012Validator(FAMILY_POLICY_SCHEMA).validate(p)
    validate_policy(p)
    reordered = copy.deepcopy(p)
    reordered["families"].reverse()
    assert canonical_policy_identity(p) == canonical_policy_identity(reordered)
    reordered["mapping"]["version"] = "v2"
    assert canonical_policy_identity(p) != canonical_policy_identity(reordered)


@pytest.mark.parametrize("change", [
    lambda p: p["families"].append(p["families"][0]),
    lambda p: p["families"][0].update(min_rows=True),
    lambda p: p["families"][0].update(min_eval_support=0),
    lambda p: p["families"][0]["metrics"]["loss"].update(max=math.nan),
    lambda p: p["families"][0]["metrics"]["loss"].update(max=math.inf),
    lambda p: p["families"][0]["metrics"]["loss"].update(max=True),
    lambda p: p["families"][0]["metrics"]["loss"].update(max_regression=0.1),
    lambda p: p["families"][0]["metrics"]["loss"].update(min=1),
    lambda p: p["mapping"].update(infer=True),
    lambda p: p.update(schema_version="unknown"),
])
def test_bad_policy_rejected(change):
    p = policy()
    change(p)
    with pytest.raises(ValueError):
        validate_policy(p)


@pytest.mark.parametrize("row", [{}, {"skill": None}, {"skill": True}, {"skill": []}, {"skill": "other"}])
def test_explicit_single_family_only(row):
    with pytest.raises(ValueError):
        family_for_row(row, policy())


def test_final_quota_loss_is_not_hidden_by_source_composition():
    rows = [{"skill": "parse"}, {"skill": "compose"}]
    assert summarize_rows(rows, policy())["status"] == "passed"
    result = summarize_rows(rows[:1], policy())
    assert result["status"] == "failed"
    assert result["counts"] == {"parse": 1, "compose": 0}
    assert assess_composition(policy(), {"parse": 1}) == result


def test_available_data_never_substitutes_for_exposure():
    p = policy()
    assert summarize_rows([{"skill": "parse"}, {"skill": "compose"}], p)["status"] == "passed"
    assert assess_exposure(p, {})["status"] == "unknown"
    assert assess_exposure(p, {"parse": 2, "compose": 0})["status"] == "failed"
    assert assess_exposure(p, {"parse": 2, "compose": 2})["status"] == "passed"


def test_zero_minimum_waives_quota_but_requires_exposure_evidence():
    p = policy()
    for f in p["families"]:
        f.update(min_rows=0, min_exposure=0)
    assert assess_composition(p, {})["status"] == "passed"
    assert assess_exposure(p, {})["status"] == "unknown"
    assert assess_exposure(p, {"parse": 0, "compose": 0})["status"] == "passed"


@pytest.mark.parametrize("counts", [{"parse": True}, {"parse": -1}, {"parse": 1.0}, {"other": 1}])
def test_bad_measurements_rejected(counts):
    for function in (assess_composition, assess_exposure):
        with pytest.raises(ValueError):
            function(policy(), counts)


def test_per_family_support_not_aggregate():
    e = evidence()
    assert assess_evaluation(policy(), e)["status"] == "passed"
    e["compose"]["support"] = 0
    assert assess_evaluation(policy(), e)["status"] == "failed"
    del e["compose"]["support"]
    assert assess_evaluation(policy(), e)["status"] == "unknown"
    del e["compose"]
    assert assess_evaluation(policy(), e)["status"] == "unknown"


def test_missing_metric_unknown_and_nonfinite_rejected():
    e = evidence()
    e["compose"]["metrics"] = {}
    assert assess_evaluation(policy(), e)["status"] == "unknown"
    for bad in (True, math.nan, math.inf):
        e["compose"]["metrics"]["loss"] = bad
        with pytest.raises(ValueError):
            assess_evaluation(policy(), e)


@pytest.mark.parametrize("direction,previous,current", [("higher", 0.4, 0.2), ("lower", 0.2, 0.4)])
def test_regression_requires_supported_baseline(direction, previous, current):
    p, e, b = policy(), evidence(), evidence()
    p["families"][0]["metrics"]["loss"] = {"max_regression": 0.1, "direction": direction}
    e["parse"]["metrics"]["loss"] = current
    b["parse"]["metrics"]["loss"] = previous
    assert assess_evaluation(p, e)["status"] == "unknown"
    assert assess_evaluation(p, e, b)["status"] == "failed"
    b["parse"]["support"] = 0
    assert assess_evaluation(p, e, b)["status"] == "unknown"


def test_threshold_boundary_and_failed_priority():
    e = evidence()
    e["parse"]["metrics"]["loss"] = 0.5
    assert assess_evaluation(policy(), e)["status"] == "passed"
    e["parse"]["metrics"]["loss"] = 0.6
    del e["compose"]
    result = assess_evaluation(policy(), e)
    assert result["status"] == "failed"
    assert result["unknowns"] and result["failures"]


def test_bounded_catalog():
    p = policy()
    p["families"] = [{**p["families"][0], "id": str(n)} for n in range(257)]
    with pytest.raises(ValueError):
        validate_policy(p)


def test_builtin_exposure_rule_dispatch_and_checkpoint_binding():
    from modelbuilder.rules import evaluate_policy
    p = policy()
    rule_policy = {"schema_version": "modelbuilder.rules/v1", "id": "coverage", "version": "1",
                   "rules": [{"id": "exposure", "kind": "family_exposure/v1", "policy": p}]}
    facts = {"policy_id": canonical_policy_identity(p), "checkpoint_digest": "c" * 64,
             "expected_checkpoint_digest": "c" * 64, "counts": {"parse": 2, "compose": 2}}
    e = {"schema_version": "modelbuilder.rule-evidence/v1",
         "subject": {"project_id": "p", "run_id": "r", "config_digest": "a" * 64, "data_digest": "b" * 64},
         "source": "test", "verification": "verified", "facts": {"exposure": facts}}
    assert evaluate_policy(rule_policy, e)["outcome"] == "passed"
    facts["counts"]["compose"] = 1
    assert evaluate_policy(rule_policy, e)["outcome"] == "blocked"
    facts["counts"]["compose"] = 2
    facts["expected_checkpoint_digest"] = "d" * 64
    assert evaluate_policy(rule_policy, e)["outcome"] == "blocked"
    facts["expected_checkpoint_digest"] = "c" * 64
    facts["policy_id"] = "sha256:" + "0" * 64
    assert evaluate_policy(rule_policy, e)["outcome"] == "blocked"


def test_regression_boundary_uses_decimal_metric_units():
    p, e, b = policy(), evidence(), evidence()
    p["families"][0]["metrics"]["loss"] = {"max_regression": 0.1, "direction": "lower"}
    b["parse"]["metrics"]["loss"] = 0.3
    e["parse"]["metrics"]["loss"] = 0.4
    assert assess_evaluation(p, e, b)["status"] == "passed"
