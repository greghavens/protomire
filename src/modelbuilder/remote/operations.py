"""Shared HTTP/MCP operation declarations and a fail-closed integration facade.

Import declarations in models.operation_catalog; initialize CostOperations with
an operator-chosen sidecar path; route these names before ordinary cost.estimate.
No public operation accepts provider rates, invoice bodies, or cleanup booleans.
"""
from __future__ import annotations

from pydantic import ValidationError

from modelbuilder.models import Actor, OperationSpec, ServiceError

from .contracts import BudgetPolicy, SpendConsent
from .operation_contract import operation_specs
from .ledger import CostLedger, require
from .service import RemoteCostService


def remote_cost_operations() -> list[OperationSpec]:
    return operation_specs(OperationSpec)


def authorize_cost_operation(operation: str, payload: dict, actor: Actor) -> None:
    """Run BEFORE any outer idempotency replay; narrowed tokens cannot read old receipts."""
    spec = next((spec for spec in remote_cost_operations() if spec.name == operation), None)
    if spec is None:
        raise ServiceError("unknown_operation", "Unknown cost operation", status_code=404)
    require(actor, set(spec.required_scopes), payload["project_id"])
    if operation == "cost.policy.put":
        policy = BudgetPolicy.model_validate(payload["policy"])
        if policy.scope == "account":
            require(actor, {"modify", "spend"}, None)
        elif policy.project_id != payload["project_id"]:
            raise ServiceError("validation_error", "Policy project must match the operation project")


class CostOperations:
    """Production-facing facade, with NO simulator configuration or evidence upload."""
    def __init__(self, ledger: CostLedger):
        self.ledger = ledger
        self.service = RemoteCostService(ledger)

    def invoke(self, operation: str, payload: dict, actor: Actor) -> dict:
        project_id = payload["project_id"]
        try:
            authorize_cost_operation(operation, payload, actor)
            if operation == "cost.providers":
                return self.service.providers(actor, project_id)
            if operation == "cost.policy.get":
                return {"balances": self.ledger.balances(actor, project_id, payload["provider_id"], payload["account_ref"]),
                        "remote_execution_enabled": False, "spending_enforced": False}
            if operation == "cost.policy.put":
                policy = BudgetPolicy.model_validate(payload["policy"])
                if policy.scope == "project" and policy.project_id != project_id:
                    raise ServiceError("validation_error", "Policy project must match the operation project")
                result = self.ledger.put_policy(actor, policy, expected_revision=payload["expected_revision"],
                                               request_id=payload["idempotency_key"])
                return {"policy": result, "remote_execution_enabled": False, "spending_enforced": False}
            if operation == "cost.quote.create":
                require(actor, {"read"}, project_id)
                raise ServiceError("remote_disabled", "No verified real provider quote source is configured; rates will not be fabricated",
                                   self.service.providers(actor, project_id), 403)
            if operation == "cost.quote.get":
                return self.ledger.get_quote(actor, project_id, payload["quote_id"])
            if operation == "cost.reserve":
                return self.service.submit_remote(actor, project_id, SpendConsent.model_validate(payload["consent"]),
                                                  payload["idempotency_key"])
            if operation == "cost.ledger.get":
                if payload.get("reservation_id"):
                    return self.ledger.get_reservation(actor, project_id, payload["reservation_id"])
                return {"reservations": self.ledger.list_reservations(actor, project_id), "spending_enforced": False}
            if operation == "cost.cancel":
                return self.ledger.cancel(actor, project_id, payload["reservation_id"], payload["idempotency_key"])
            if operation == "cost.history":
                events = self.ledger.history(actor, project_id, after_sequence=payload.get("after_sequence", 0),
                                             limit=payload.get("limit", 100))
                return {"events": events, "next_after_sequence": events[-1]["sequence"] if events else payload.get("after_sequence", 0)}
        except ValidationError as error:
            raise ServiceError("validation_error", "Cost contract validation failed",
                               {"errors": [{"path": ".".join(map(str, e["loc"])), "message": e["msg"]}
                                           for e in error.errors(include_input=False)]}) from error
        raise ServiceError("unknown_operation", "Unknown cost operation", status_code=404)
