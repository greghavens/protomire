"""Inert registration, source identities and independently implemented methods."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest
from modelbuilder.evaluation import digest
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService
from modelbuilder.rules import evaluate_policy, validate_policy, sample_rows
from modelbuilder.backend.components.rule_gate import validate_checkpoint_state
from modelbuilder.scientific import Method
from test_rules import policy, evidence, sample_spec


def declaration(reference=None):
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"width": {"type": "number", "minimum": 0}},
        "required": ["width"],
    }
    body = {
        "id": "independent.noisy-range",
        "version": "1",
        "decision_domain": "predicate",
        "implementation_ref": reference
        or {
            "module": "independent_method",
            "entrypoint": "range_rule",
            "commit": "a" * 40,
            "path": "method.py",
            "sha256": "b" * 64,
        },
        "parameter_schema": schema,
        "parameter_schema_digest": digest(schema),
    }
    return {**body, "method_digest": digest(body)}


def custom_policy(method=None):
    return {
        "schema_version": "modelbuilder.rules/v1",
        "id": "custom-options",
        "version": "1",
        "rules": [
            {
                "id": "spread",
                "kind": "method/v1",
                "method": method or declaration(),
                "parameters": {"width": 0.1},
            }
        ],
    }


def custom_evidence():
    e = evidence()
    e["facts"] = {"spread": {"values": [1.0, 1.02, 0.99]}}
    return e


def independent_range(*, facts, parameters, subject):
    spread = max(facts["values"]) - min(facts["values"])
    return {
        "outcome": "passed" if spread <= parameters["width"] else "blocked",
        "reasons": ["range_within_width" if spread <= parameters["width"] else "range_exceeds_width"],
        "observed": {"spread": spread},
    }


def test_custom_method_is_registered_by_full_implementation_and_schema_identity():
    d, e = declaration(), custom_evidence()
    method = Method(d["id"], d["version"], d, d["parameter_schema"], independent_range)
    assert evaluate_policy(custom_policy(d), e, extensions=(method,))["outcome"] == "passed"
    assert evaluate_policy(custom_policy(d), e)["outcome"] == "blocked"
    changed = deepcopy(d)
    changed["implementation_ref"]["sha256"] = "c" * 64
    changed["method_digest"] = digest({k: v for k, v in changed.items() if k != "method_digest"})
    assert evaluate_policy(custom_policy(changed), e, extensions=(method,))["outcome"] == "blocked"
    assert evaluate_policy(custom_policy(d), e, extensions=(method,)) == evaluate_policy(
        custom_policy(d), e, extensions=(method,)
    )


def test_registration_version_schema_project_and_readonly_preview(tmp_path):
    service = OperationService(tmp_path / "service.sqlite")
    actor = Actor("owner", {"read", "modify"})
    pid = service.invoke(
        "project.create", {"slug": "methods", "model_name": "Methods", "idempotency_key": "p"}, actor
    )["project_id"]
    d = declaration()
    request = {"project_id": pid, "method": d, "idempotency_key": "register"}
    row = service.invoke("rules.method.register", request, actor)
    assert service.invoke("rules.method.register", request, actor) == row
    changed = deepcopy(d)
    changed["parameter_schema"]["properties"]["width"]["minimum"] = 0.01
    changed["parameter_schema_digest"] = digest(changed["parameter_schema"])
    changed["method_digest"] = digest({k: v for k, v in changed.items() if k != "method_digest"})
    with pytest.raises(ServiceError) as error:
        service.invoke(
            "rules.method.register", {**request, "method": changed, "idempotency_key": "changed"}, actor
        )
    assert error.value.code == "rule_method_version_conflict"
    with pytest.raises(ServiceError) as error:
        service.invoke("rules.method.register", request, Actor("owner", {"read"}))
    assert error.value.code == "forbidden"
    registered = service.invoke(
        "rules.policy.register",
        {"project_id": pid, "policy": custom_policy(d), "idempotency_key": "policy"},
        actor,
    )
    with pytest.raises(ServiceError) as error:
        service.invoke(
            "rules.evaluate",
            {"project_id": pid, "policy_digest": registered["policy_digest"], "evidence": custom_evidence()},
            actor,
        )
    assert error.value.code == "custom_rule_execution_required"
    unknown = custom_policy(changed)
    with pytest.raises(ServiceError) as error:
        service.invoke(
            "rules.policy.register",
            {"project_id": pid, "policy": unknown, "idempotency_key": "unknown"},
            actor,
        )
    assert error.value.code == "unknown_rule_method"
    malformed = deepcopy(d)
    malformed["parameter_schema_digest"] = "0" * 64
    with pytest.raises(ServiceError) as error:
        service.invoke(
            "rules.method.register", {**request, "method": malformed, "idempotency_key": "bad-schema"}, actor
        )
    assert error.value.code == "invalid_rule_method"
    service.store.close()


def test_null_checkpoint_and_nonboolean_continuation_are_blocked():
    with pytest.raises(ValueError):
        validate_checkpoint_state(
            {k: None for k in ("model", "optimizer", "scheduler", "rng", "data_cursor")}
        )
    state = {
        "schema_version": "modelbuilder.rule-checkpoint-state/v1",
        "resume_modes": ["exact_state"],
        "state": {
            k: {"kind": "value", "value": 0}
            for k in ("model", "optimizer", "scheduler", "rng", "data_cursor")
        },
    }
    assert validate_checkpoint_state(state) == state
    state["state"]["rng"] = {"kind": "no_op", "reason": "No random operations"}
    assert validate_checkpoint_state(state) == state
    state["state"]["model"]["value"] = None
    with pytest.raises(ValueError):
        validate_checkpoint_state(state)
    e = evidence()
    e["facts"]["curve"]["continuation"]["supported"] = 1
    assert evaluate_policy(policy(), e)["outcome"] == "blocked"


@pytest.mark.parametrize("key", ["window", "min_epochs", "max_epochs", "max_steps", "next_epoch_steps"])
def test_count_options_reject_integer_valued_float_consistently(key):
    with pytest.raises(ValueError):
        validate_policy(policy(**{key: 3.0}))


def test_constructive_lower_bounds_required_strata_and_explicit_unverified_coverage():
    rows = [
        {"sample_id": str(i), "split_id": "train", "row": {"category": "A" if i < 98 else "B"}}
        for i in range(100)
    ]
    selected, report = sample_rows(
        rows, sample_spec(max_share_error=0.1, required_strata=[{"category": "A"}, {"category": "B"}])
    )
    assert sum(x["row"]["category"] == "B" for x in selected) == 1 and len(selected) == 10
    assert report["representation"][0]["coverage_status"] == "declared"
    equal, _ = sample_rows(
        rows, sample_spec(allocation="equal", max_share_error=0.3, required_strata=[{"category": "B"}])
    )
    assert len(equal) == 10 and sum(x["row"]["category"] == "B" for x in equal) == 2
    with pytest.raises(ValueError, match="absent"):
        sample_rows(rows[:98], sample_spec(required_strata=[{"category": "B"}]))
    assert sample_rows(rows[:98], sample_spec())[1]["representation"][0]["coverage_status"] == "unspecified"


def test_exhausted_active_zero_projection_and_sparse_history_do_not_continue():
    e = evidence()
    p = policy(max_steps=30, next_epoch_steps=0)
    assert evaluate_policy(p, e)["outcome"] == "stop"
    assert evaluate_policy(policy(), e)["outcome"] == "continue"  # explicitly free cost counter
    e["facts"]["curve"]["observations"].pop(1)
    assert evaluate_policy(policy(insufficient_window="collect_within_bounds"), e)["outcome"] == "blocked"
    e = evidence()
    e["facts"]["curve"]["resource_units"]["cost"] = "different-currency"
    assert evaluate_policy(policy(), e)["outcome"] == "blocked"


def test_pure_quota_planner_scales_by_strata_and_constructs_required_retention():
    from modelbuilder.rules import plan_quotas

    counts = {"small": 2, "large": 100_000_000}
    result = plan_quotas(counts, 50_000_000, "equal", 0, ["small"])
    assert result == {"small": 2, "large": 49_999_998}
    assert plan_quotas({"a": 98, "b": 2}, 10, "proportional", 0, ["b"]) == {"a": 9, "b": 1}


def load_native_rule_test_runner():
    root = Path(__file__).resolve().parents[2]
    name = "_modelbuilder_rules_native_test_runner"
    spec = importlib.util.spec_from_file_location(name, root / "scripts/native_test_runner.py")
    module = importlib.util.module_from_spec(spec)
    missing = object()
    previous = sys.modules.get(name, missing)
    try:
        # Dataclasses resolve their defining module while the script imports.
        sys.modules[name] = module
        spec.loader.exec_module(module)
    finally:
        if previous is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module


def test_native_queue_includes_rule_tests_without_duplicate_partition():
    root = Path(__file__).resolve().parents[2]
    module = load_native_rule_test_runner()
    for name in ("test_native_rules.py", "test_rule_stdio.py", "test_custom_native.py", "test_standalone_native.py"):
        path = "tests/rules/" + name
        assert module.SERVICE_TESTS.count(path) == 1
        assert path not in module.COMPONENT_TESTS and path not in module.LIFECYCLE_TESTS
        assert (
            sum(line.split().count(path) for line in (root / "scripts/test_all.sh").read_text().splitlines())
            == 1
        )


@pytest.mark.parametrize("preexisting", [False, True])
@pytest.mark.parametrize("fail_import", [False, True])
def test_native_queue_inspection_preserves_module_isolation(monkeypatch, preexisting, fail_import):
    import importlib.machinery
    import queue
    from multiprocessing import get_context

    name = "_modelbuilder_rules_native_test_runner"
    previous = ModuleType(name)
    if preexisting:
        monkeypatch.setitem(sys.modules, name, previous)
    else:
        monkeypatch.delitem(sys.modules, name, raising=False)
    if fail_import:
        def fail(*args):
            raise RuntimeError("isolated import failure")

        with monkeypatch.context() as importing:
            importing.setattr(importlib.machinery.SourceFileLoader, "exec_module", fail)
            with pytest.raises(RuntimeError, match="isolated import failure"):
                load_native_rule_test_runner()
    else:
        load_native_rule_test_runner()

    if preexisting:
        assert sys.modules.get(name) is previous
    else:
        assert name not in sys.modules
    assert sys.modules["queue"] is queue
    assert importlib.import_module("queue").Empty is queue.Empty
    assert importlib.import_module("queue").Full is queue.Full
    # This imports multiprocessing.queues, which failed after the old helper
    # replaced stdlib queue. No worker process or training is launched.
    channel = get_context("spawn").SimpleQueue()
    try:
        channel.put("stdlib queue is intact")
        assert channel.get() == "stdlib queue is intact"
    finally:
        channel.close()


def continuation_fixture(outcome='continue'):
    d = declaration()
    d['decision_domain'] = 'continuation'
    d['method_digest'] = digest({key: value for key, value in d.items() if key != 'method_digest'})
    state = {'schema_version': 'modelbuilder.rule-checkpoint-state/v1', 'resume_modes': ['exact_state'],
             'state': {key: {'kind': 'value', 'value': 0} for key in ('model', 'optimizer', 'scheduler', 'rng', 'data_cursor')}}
    e = custom_evidence()
    e['facts']['spread'].update(status='completed', checkpoint_digest=digest(state),
                              expected_checkpoint_digest=digest(state), continuation={'mode': 'exact_state', 'supported': True})
    def scientific_choice(*, facts, parameters, subject):
        return {'outcome': outcome, 'reasons': ['user_selected_scientific_choice'], 'observed': {'criterion': 'independent'}}
    method = Method(d['id'], d['version'], d, d['parameter_schema'], scientific_choice)
    return {'inputs': {'checkpoint_state': state, 'evidence': e},
            'parameters': {'policy': custom_policy(d)}, 'context': {'rule_methods': (method,)}}


def test_custom_continuation_gate_preserves_scientific_choice_with_exact_capability():
    from modelbuilder.backend.components.rule_gate import gate
    request = continuation_fixture()
    result = gate(**request)
    assert result['allowed'] is True and result['done'] is False
    assert result['result']['rules'][0]['reasons'] == ['user_selected_scientific_choice']
    assert gate(**request) == result
    # Unsupported continuation does not prevent a method from stopping safely.
    stopped = continuation_fixture('stop')
    stopped['inputs']['evidence']['facts']['spread']['continuation'] = {'mode': 'warm_start', 'supported': False}
    result = gate(**stopped)
    assert result['allowed'] is False and result['done'] is True


@pytest.mark.parametrize('capability', ['missing', None, {}, {'mode': 'unknown', 'supported': True}, {'mode': 'warm_start', 'supported': False},
    {'mode': 'warm_start', 'supported': True}, {'mode': 'exact_state', 'supported': False},
    {'mode': 'exact_state', 'supported': 1}, {'mode': 'exact_state', 'supported': True, 'extra': True}])
def test_custom_continuation_gate_rejects_missing_or_conflicting_capability(capability):
    from modelbuilder.backend.components.rule_gate import gate
    request = continuation_fixture()
    if capability == 'missing':
        del request['inputs']['evidence']['facts']['spread']['continuation']
    else:
        request['inputs']['evidence']['facts']['spread']['continuation'] = capability
    with pytest.raises(ValueError, match='rule_blocked:continuation_unsupported'):
        gate(**request)


@pytest.mark.parametrize('conflict', ['actual_digest', 'expected_digest', 'incomplete_epoch', 'state_modes'])
def test_custom_continuation_gate_rejects_conflicting_state_and_facts(conflict):
    from modelbuilder.backend.components.rule_gate import gate
    request = continuation_fixture()
    facts = request['inputs']['evidence']['facts']['spread']
    if conflict == 'actual_digest':
        facts['checkpoint_digest'] = 'f' * 64
    elif conflict == 'expected_digest':
        facts['expected_checkpoint_digest'] = 'f' * 64
    elif conflict == 'incomplete_epoch':
        facts['status'] = 'running'
    else:
        request['inputs']['checkpoint_state']['resume_modes'] = ['warm_start']
        facts['checkpoint_digest'] = digest(request['inputs']['checkpoint_state'])
        facts['expected_checkpoint_digest'] = facts['checkpoint_digest']
    with pytest.raises(ValueError, match='rule_blocked:'):
        gate(**request)
