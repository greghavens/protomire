"""Shared service authorization and native transport schema parity; no provider calls."""
from __future__ import annotations

import pytest

from modelbuilder.models import Actor, ServiceError, operation_catalog
from modelbuilder.service import OperationService


OWNER = Actor(id="owner", scopes=frozenset({"read", "modify", "execute", "spend"}))


def test_shared_service_cost_operations_enforce_scopes_and_no_cloud(tmp_path):
    service = OperationService(db_path=tmp_path / "app.sqlite")
    try:
        project = service.invoke("project.create", {"slug": "cost-fixture", "model_name": "Cost fixture", "idempotency_key": "project"}, OWNER)
        project_id = project["project_id"]
        diagnostics = service.invoke("cost.providers", {"project_id": project_id}, OWNER)
        assert diagnostics["remote_execution_enabled"] is False
        assert diagnostics["remote_acceptance"] == "BLOCKED"
        assert diagnostics["configured"] is False
        payload = {"project_id": project_id, "idempotency_key": "budget", "expected_revision": None,
                   "policy": {"policy_id": "budget", "scope": "project", "project_id": project_id,
                              "provider_id": "not-configured", "account_ref": "acct_" + "a" * 32,
                              "maximum_total": {"currency": "USD", "minor_units": 123}, "max_runtime_seconds": 60}}
        with pytest.raises(ServiceError) as caught:
            service.invoke("cost.policy.put", payload, Actor(id="modifier", scopes=frozenset({"read", "modify"})))
        assert caught.value.code == "forbidden"
        first = service.invoke("cost.policy.put", payload, OWNER)
        assert service.invoke("cost.policy.put", payload, OWNER) == first
        assert first["spending_enforced"] is False
        reserve = {"project_id": project_id, "idempotency_key": "remote-launch", "consent": {
            "execute_and_spend": True, "quote_id": "fake", "quote_digest": "a" * 64,
            "maximum_total": {"currency": "USD", "minor_units": 123}, "max_runtime_seconds": 60}}
        with pytest.raises(ServiceError) as caught:
            service.invoke("cost.reserve", reserve, OWNER)
        assert caught.value.code == "remote_disabled"
        reserve["consent"]["provider_invoice"] = "forged"
        with pytest.raises(ServiceError) as caught:
            service.invoke("cost.reserve", reserve, OWNER)
        assert caught.value.code == "validation_error"
        assert service.invoke("cost.ledger.get", {"project_id": project_id}, OWNER)["reservations"] == []
    finally:
        if service._remote_cost_operations:
            service._remote_cost_operations.ledger.close()
        service.store.close()


def test_shared_catalog_exposes_same_cost_schemas():
    from modelbuilder.remote.operations import remote_cost_operations
    current = {spec["name"]: spec for spec in operation_catalog()}
    for spec in remote_cost_operations():
        assert current[spec.name] == spec.as_dict()


def test_cached_account_policy_denied_after_actor_scope_narrows(tmp_path):
    service = OperationService(db_path=tmp_path / "app.sqlite")
    try:
        project = service.invoke("project.create", {"slug": "scope-replay", "model_name": "Scope replay", "idempotency_key": "project"}, OWNER)
        project_id = project["project_id"]
        payload = {"project_id": project_id, "idempotency_key": "account-budget", "expected_revision": None,
                   "policy": {"policy_id": "account-budget", "scope": "account", "project_id": None,
                              "provider_id": "unconfigured", "account_ref": "acct_" + "a" * 32,
                              "maximum_total": {"currency": "USD", "minor_units": 100}, "max_runtime_seconds": 60}}
        service.invoke("cost.policy.put", payload, OWNER)
        narrowed = Actor(id=OWNER.id, scopes=OWNER.scopes, project_ids=frozenset({project_id}))
        with pytest.raises(ServiceError) as caught:
            service.invoke("cost.policy.put", payload, narrowed)
        assert caught.value.code == "forbidden"
    finally:
        if service._remote_cost_operations:
            service._remote_cost_operations.ledger.close()
        service.store.close()


@pytest.mark.parametrize("first_import", ["modelbuilder.store", "modelbuilder.models", "modelbuilder.remote",
                                          "modelbuilder.remote.ledger", "modelbuilder.remote.operations"])
def test_cost_catalog_import_orders_in_fresh_process(first_import):
    import os
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-c", f"import {first_import}; from modelbuilder.models import OPERATIONS; assert 'cost.providers' in OPERATIONS"],
                            env=os.environ.copy(), capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
