from copy import deepcopy
from decimal import getcontext

import pytest
from modelbuilder.backend.components.rule_gate import gate, catalog_entries
from modelbuilder.backend.components import validate_component
from modelbuilder.rules import evaluate_policy, sample_rows
from test_rules import policy, evidence, rows, sample_spec


@pytest.mark.parametrize(
    "used,bound,limit",
    [("steps", "max_steps", 30), ("runtime_seconds", "max_runtime_seconds", 3), ("cost", "max_cost", 0.5)],
)
def test_every_explicit_resource_bound_prevents_next_epoch(used, bound, limit):
    e = evidence()
    p = policy(**{bound: limit})
    if used == "cost":
        e["facts"]["curve"]["cost"] = 0.5
        p["rules"][0]["resource_modes"]["cost"] = "active"
        p["rules"][0]["next_epoch_cost"] = 0.01
    result = evaluate_policy(p, e)
    assert result["outcome"] == "stop"
    assert result["reasons"] == ["resource_bound:" + bound]


def test_rational_decisions_ignore_external_decimal_context_and_do_not_mutate():
    p, e, data, spec = policy(), evidence(), rows(), sample_spec()
    original = deepcopy((p, e, data, spec))
    first = evaluate_policy(p, e), sample_rows(data, spec)
    previous_precision = getcontext().prec
    try:
        getcontext().prec = 2
        assert first == (evaluate_policy(p, e), sample_rows(data, spec))
    finally:
        getcontext().prec = previous_precision
    assert original == (p, e, data, spec)


def test_selected_effective_configuration_parity_is_typed_and_scoped():
    p, e = policy(), evidence()
    p["rules"] = [
        {"id": "config", "kind": "identity/v1", "expected": {"eval_interval": 500, "loss_weight": 1}}
    ]
    e["facts"] = {"config": {"actual": {"eval_interval": 1000, "loss_weight": True}}}
    result = evaluate_policy(p, e)
    assert result["outcome"] == "blocked"
    assert result["reasons"] == ["identity_mismatch:eval_interval", "identity_mismatch:loss_weight"]
    e["facts"]["config"]["actual"] = {"eval_interval": 500, "loss_weight": 1, "other_setting": "unselected"}
    assert evaluate_policy(p, e)["outcome"] == "passed"


def test_cross_run_overlap_never_imposes_an_epoch_limit():
    p, e = policy(), evidence()
    p["rules"].append({"id": "reuse", "kind": "overlap/v1", "scope": "cross_run", "max_overlap": 0})
    e["facts"]["reuse"] = {"scope": "cross_run", "overlap_count": 0, "checked_identity_digest": "d" * 64}
    assert evaluate_policy(p, e)["outcome"] == "continue"
    e["facts"]["reuse"]["overlap_count"] = 1
    assert evaluate_policy(p, e)["outcome"] == "blocked"
    e["facts"]["reuse"]["scope"] = "within_epoch"
    assert evaluate_policy(p, e)["reasons"][-1] == "invalid_evidence"


def test_sampling_infeasible_capacity_missing_fields_ties_and_duplicate_ids():
    with pytest.raises(ValueError, match="share_error"):
        sample_rows(rows(), sample_spec(allocation="equal", fraction=1))
    source = rows()
    source[0]["row"].pop("category")
    with pytest.raises(ValueError, match="stratum"):
        sample_rows(source, sample_spec())
    with pytest.raises(ValueError, match="identities"):
        sample_rows(rows() + rows()[:1], sample_spec())
    ties = [{"sample_id": str(i), "split_id": "train", "row": {"category": str(i // 3)}} for i in range(9)]
    selected, report = sample_rows(ties, sample_spec(fraction=0.5, min_per_stratum=0, max_share_error=1))
    assert (
        len(selected) == 4
        and report
        == sample_rows(ties[::-1], sample_spec(fraction=0.5, min_per_stratum=0, max_share_error=1))[1]
    )


def test_native_gate_needs_actual_checkpoint_not_a_claimed_digest():
    with pytest.raises(ValueError, match="checkpoint_input_required"):
        gate(inputs={"evidence": evidence()}, parameters={"policy": policy()}, context={})
    with pytest.raises(ValueError, match="checkpoint_state_contract_invalid"):
        gate(
            inputs={
                "evidence": evidence(),
                "checkpoint_state": {
                    "model": {},
                    "optimizer": {},
                    "scheduler": {},
                    "rng": {},
                    "data_cursor": 0,
                },
            },
            parameters={"policy": policy()},
            context={},
        )
    entry = catalog_entries()[0]
    component = {
        key: value
        for key, value in entry.items()
        if key not in {"inputs", "outputs", "adapter", "component_digest"}
    }
    with pytest.raises(ValueError, match="disable cache"):
        validate_component(
            {"component": component, "parameters": {"policy": policy()}, "cache_policy": "enabled"}
        )
