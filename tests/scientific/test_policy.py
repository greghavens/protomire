from copy import deepcopy
import json
import math
import subprocess

import pytest
from modelbuilder.evaluation import make_evaluation_record
from modelbuilder.scientific import evaluate_policy, known_sd_adequacy, Method, method_catalog
from modelbuilder.backend.components.scientific_gate import gate


def policy(method="scalar_threshold"):
    method_entry = next(m for m in method_catalog() if m["id"] == method)
    params = {} if method == "scalar_threshold" else {"population_difference_sd": 1., "detectable_effect": .5, "power": .8,
        "variance_source": "prior-independent-study:sha256:abc", "alternative": "greater",
        "assumptions": {"independent_pairs": True, "normal_difference_population": True, "known_population_sd": True}}
    return {"schema": "modelbuilder.scientific-policy/v1", "id": "generic-quality", "version": "1", "purpose": "promotion",
        "family_error_rate": .05, "multiple_testing": "bonferroni", "allowed_variations": ["model", "checkpoint"], "unknown_behavior": "block",
        "rules": [{"id": name, "metric": name, "direction": "minimize", "minimum_improvement": 0.,
                   "method": {k: deepcopy(v) for k, v in {**method_entry, "parameters": params}.items() if k != "parameter_schema"}}
                  for name in ("loss", "retention")]}


def record(metric="loss", value=1., n=100, checkpoint="a"):
    return make_evaluation_record(dataset={"id": "generic", "revision": "1", "content_digest": "a" * 64, "benchmark": "heldout/v1"},
        split={"id": "dev", "role": "validation", "sample_ids": [str(i) for i in range(n)]},
        protocol={"implementation": "independent.evaluate", "version": "1", "configuration": {}, "preprocessing": {}, "seed": 0},
        metric={"name": metric, "implementation": "mean", "version": "1", "parameters": {}, "aggregation": "mean", "direction": "minimize"},
        treatment={"model": checkpoint, "checkpoint": checkpoint, "calibration": None, "reference": None}, value=value,
        predictions=[{"item_id": str(i), "value": value} for i in range(n)])


def evidence(loss=.5, retention=.5, n=100):
    return {metric: {"baseline": {"record": record(metric, 1., n)}, "candidate": {"record": record(metric, value, n, "b")}}
            for metric, value in (("loss", loss), ("retention", retention))}


def test_primary_win_retention_loss_cannot_pass():
    result = evaluate_policy(policy(), evidence(retention=1.1))
    assert result["outcome"] == "failed" and not result["passed"]
    assert [r["outcome"] for r in result["rules"]] == ["passed", "failed"]
    assert not result["rules"][0]["significance_claim"]


def test_paired_known_sd_pass_and_insufficient_sample_are_distinct():
    p = policy("paired_normal_known_sd")
    result = evaluate_policy(p, evidence())
    assert result["passed"]
    assert result["rules"][0]["sample_adequacy"]["required_pairs"] == 32
    assert result["multiple_testing"]["rule_error_rate"] == .025
    insufficient = evaluate_policy(p, evidence(n=3))
    assert insufficient["outcome"] == "unknown" and not insufficient["passed"]
    assert not insufficient["rules"][0]["significance_claim"]
    assert evaluate_policy(p, evidence()) == result


@pytest.mark.parametrize("kind", ["missing", "different_cohort", "unknown_method", "missing_predictions", "unsupported_aggregation"])
def test_unusable_scientific_evidence_blocks(kind):
    p, e = policy("paired_normal_known_sd"), evidence()
    if kind == "missing":
        e["loss"]["candidate"]["record"] = {}
    elif kind == "different_cohort":
        e["loss"]["candidate"]["record"] = record("loss", .5, 99, "b")
    elif kind == "unknown_method":
        p["rules"][0]["method"]["version"] = "unavailable"
    else:
        from modelbuilder.evaluation import digest
        for side in ("baseline", "candidate"):
            r = e["loss"][side]["record"]
            if kind == "missing_predictions":
                r.pop("predictions")
            else:
                r["metric"]["aggregation"] = "custom"
            r["digest"] = digest({k: v for k, v in r.items() if k != "digest"})
    result = evaluate_policy(p, e)
    assert result["outcome"] == "unknown" and not result["passed"]


def test_custom_reviewed_method_requires_exact_implementation_and_schema():
    p = policy()
    p["rules"][0]["method"] = {"id": "independent.robust", "version": "4", "implementation": {"source_sha256": "a" * 64, "entrypoint": "robust"}, "parameters": {"margin": .3}}
    def fn(**kwargs):
        return {"outcome": "passed", "uncertainty": None, "significance_claim": False, "sample_adequacy": None}
    extension = Method("independent.robust", "4", p["rules"][0]["method"]["implementation"],
        {"type": "object", "additionalProperties": False, "properties": {"margin": {"type": "number"}}, "required": ["margin"]}, fn)
    assert evaluate_policy(p, evidence())["outcome"] == "unknown"
    assert evaluate_policy(p, evidence(), extensions=[extension])["passed"]
    p["rules"][0]["method"]["implementation"]["source_sha256"] = "b" * 64
    # Method declarations are immutable by convention; use an independent pin.
    wrong = Method(extension.id, extension.version, {"source_sha256": "a" * 64, "entrypoint": "robust"}, extension.parameter_schema, fn)
    assert not evaluate_policy(p, evidence(), extensions=[wrong])["passed"]


def test_native_branch_adapter_uses_same_policy_outcomes():
    p = policy()
    inputs = {side: {"evaluation_records": {metric: item[side]["record"] for metric, item in evidence().items()}} for side in ("baseline", "candidate")}
    result = gate(inputs=inputs, parameters={"policy": p}, context={})
    assert type(result["passed"]) is bool and result["passed"]
    assert result["result"] == evaluate_policy(p, evidence())
    inputs["candidate"]["evaluation_records"].pop("retention")
    assert gate(inputs=inputs, parameters={"policy": p}, context={})["passed"] is False


def test_statistics_normaldist_matches_scipy_oracle():
    # SciPy is a test oracle in the host test image, not an undeclared service dependency.
    script = "from scipy.stats import norm; import json; print(json.dumps({'z':norm.ppf(.975),'power':norm.cdf(5-norm.ppf(.975)),'p':norm.sf(5)}))"
    completed = subprocess.run(["python", "-c", script], capture_output=True, text=True)
    if completed.returncode:
        pytest.skip("Independent SciPy oracle is not installed in the test host")
    oracle = json.loads(completed.stdout)
    result = evaluate_policy(policy("paired_normal_known_sd"), evidence())
    rule = result["rules"][0]
    assert rule["uncertainty"]["p_value"] == pytest.approx(oracle["p"], abs=1e-16)
    assert rule["uncertainty"]["lower_confidence_bound"] == pytest.approx(.5 - oracle["z"] * .1)
    assert rule["sample_adequacy"]["design_power_at_available_n"] == pytest.approx(oracle["power"])
    params = policy("paired_normal_known_sd")["rules"][0]["method"]["parameters"]
    assert known_sd_adequacy(params, .025, 100)["required_pairs"] == math.ceil(((oracle["z"] + .8416212335729143) / .5) ** 2)


def test_extreme_alpha_does_not_cancel_or_underestimate_p_value():
    p = policy("paired_normal_known_sd")
    p["rules"] = [p["rules"][0]]
    p["family_error_rate"] = 6e-17
    result = evaluate_policy(p, {"loss": evidence(loss=1-.0826, n=10000)["loss"]})
    rule = result["rules"][0]
    assert result["outcome"] == "failed"
    assert rule["uncertainty"]["p_value"] == pytest.approx(7.283623381655497e-17, rel=1e-10, abs=0)
    assert rule["sample_adequacy"]["required_pairs"] == 334
    params = p["rules"][0]["method"]["parameters"]
    assert known_sd_adequacy(params, 1e-17, 10000)["required_pairs"] > 334


def test_unrepresentable_standard_error_is_unknown_not_significant():
    p = policy("paired_normal_known_sd")
    for r in p["rules"]:
        r["method"]["parameters"].update(population_difference_sd=5e-324, detectable_effect=5e-324)
    result = evaluate_policy(p, evidence())
    assert result["outcome"] == "unknown" and not result["passed"]
    assert all(not r["significance_claim"] for r in result["rules"])
