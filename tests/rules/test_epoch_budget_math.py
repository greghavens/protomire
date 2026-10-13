"""Epoch admission and shared convergence use the same exact decimal rationals."""

from copy import deepcopy
import pytest
from modelbuilder.backend.components.transformers_epoch_policy import admission, selected_bounds
from modelbuilder.rules import evaluate_policy
from test_rules import policy, evidence


@pytest.mark.parametrize(
    "used,increment,limit,outcome",
    [
        (0.1, 0.2, 0.3, "continue"),
        (0.1, 0.2, 0.29999999999999993, "stop"),
        (0.3, 0, 0.3, "stop"),
        (0.30000000000000004, 0, 0.3, "blocked"),
        (0.1, 0, 0.3, "continue"),
        (0, 0, 0, "stop"),
        (0.2, 0.1, 0.3, "continue"),
    ],
)
def test_runtime_boundary_matches_shared_rule_exact_numeric_semantics(used, increment, limit, outcome):
    p = policy(
        metric="validation_error",
        criterion="target",
        threshold=0,
        max_epochs=8,
        max_runtime_seconds=limit,
        next_epoch_seconds=increment,
    )
    e = evidence()
    e["facts"]["curve"]["runtime_seconds"] = used
    bounds = selected_bounds({"policy": p})
    assert evaluate_policy(p, e)["outcome"] == outcome
    result = admission(bounds, 3, 30, used)
    assert result["outcome"] == outcome
    assert result["consumed"]["runtime_seconds"] == used


def test_exact_integer_bound_at_equality_stops_even_with_zero_increment():
    p = policy()
    bounds = deepcopy(selected_bounds({"policy": p}))
    bounds["max_steps"] = 30
    bounds["next_epoch_steps"] = 0
    result = admission(bounds, 3, 30, 0)
    assert result["outcome"] == "stop" and result["reasons"] == ["resource_bound:max_steps"]
