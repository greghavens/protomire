"""Transport contract tests, still with no provider, network or paid workload."""
from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from modelbuilder.models import Actor, ServiceError
from modelbuilder.remote import BudgetPolicy, CostLedger, Money
from modelbuilder.remote.operations import CostOperations, remote_cost_operations


OWNER = Actor(id="owner", scopes=frozenset({"read", "modify", "execute", "spend"}))
ACCOUNT = "acct_" + "a" * 32


@pytest.fixture
def operations(tmp_path):
    ledger = CostLedger(tmp_path / "ledger.sqlite")
    yield CostOperations(ledger)
    ledger.close()


def test_all_operation_schemas_validate_and_disallow_unlisted_fields():
    specs = remote_cost_operations()
    assert len(specs) == 9
    for spec in specs:
        Draft202012Validator.check_schema(spec.input_schema)
        assert spec.input_schema["additionalProperties"] is False
    assert not any(spec.name in {"cost.reconcile", "cost.simulate", "cost.provider.register"} for spec in specs)


def test_spending_mutations_declare_all_scopes():
    specs = {spec.name: spec for spec in remote_cost_operations()}
    assert set(specs["cost.policy.put"].required_scopes) == {"modify", "spend"}
    assert set(specs["cost.reserve"].required_scopes) == {"read", "execute", "spend"}


def policy_payload():
    policy = BudgetPolicy(policy_id="project-policy", scope="project", project_id="p", provider_id="future-provider",
                          account_ref=ACCOUNT, maximum_total=Money(currency="USD", minor_units=100), max_runtime_seconds=60)
    return {"project_id": "p", "policy": policy.model_dump(mode="json"), "expected_revision": None,
            "idempotency_key": "set-policy"}


def test_policy_contract_embedded_refs_resolve_and_float_money_is_rejected():
    spec = next(spec for spec in remote_cost_operations() if spec.name == "cost.policy.put")
    payload = policy_payload()
    Draft202012Validator(spec.input_schema).validate(payload)
    payload["policy"]["maximum_total"]["minor_units"] = 0.1
    assert list(Draft202012Validator(spec.input_schema).iter_errors(payload))


def test_policy_save_is_durable_idempotent_and_does_not_enable_remote(operations):
    first = operations.invoke("cost.policy.put", policy_payload(), OWNER)
    assert operations.invoke("cost.policy.put", policy_payload(), OWNER) == first
    assert first["remote_execution_enabled"] is False
    assert first["spending_enforced"] is False
    read = operations.invoke("cost.policy.get", {"project_id": "p", "provider_id": "future-provider", "account_ref": ACCOUNT}, OWNER)
    assert read["balances"][0]["policy"]["maximum_total"]["minor_units"] == 100
    events = operations.invoke("cost.history", {"project_id": "p"}, OWNER)
    assert len(events["events"]) == 1
    assert events["events"][0]["actor_id"] == "owner"


def test_public_quote_and_reserve_cannot_enable_production(operations):
    with pytest.raises(ServiceError) as caught:
        operations.invoke("cost.quote.create", {"project_id": "p"}, OWNER)
    assert caught.value.code == "remote_disabled"
    payload = {"project_id": "p", "idempotency_key": "reserve", "consent": {
        "execute_and_spend": True, "quote_id": "client-forged-quote", "quote_digest": "a" * 64,
        "maximum_total": {"currency": "USD", "minor_units": 100}, "max_runtime_seconds": 60}}
    with pytest.raises(ServiceError) as caught:
        operations.invoke("cost.reserve", payload, OWNER)
    assert caught.value.code == "remote_disabled"
    assert operations.invoke("cost.ledger.get", {"project_id": "p"}, OWNER)["reservations"] == []


def test_no_evidence_ingestion_operation(operations):
    with pytest.raises(ServiceError) as caught:
        operations.invoke("cost.reconcile", {"project_id": "p", "billing_state": "final", "cleanup_state": "confirmed"}, OWNER)
    assert caught.value.code == "unknown_operation"


def test_read_token_cannot_change_policy(operations):
    with pytest.raises(ServiceError) as caught:
        operations.invoke("cost.policy.put", policy_payload(), Actor(id="reader", scopes=frozenset({"read"})))
    assert caught.value.code == "forbidden"


def test_policy_project_cannot_differ_from_route_project(operations):
    payload = policy_payload()
    payload["policy"]["project_id"] = "other"
    with pytest.raises(ServiceError) as caught:
        operations.invoke("cost.policy.put", payload, OWNER)
    assert caught.value.code == "validation_error"
