"""Dependency-light operation contracts, safe to load while domain models initialize."""
from __future__ import annotations
from copy import deepcopy
from pydantic import BaseModel
from .contracts import AccountBinding, BudgetPolicy, SpendConsent


def inline_schema(model: type[BaseModel]) -> dict:
    """Make embedded JSON schemas self-contained for the shared operation root."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def resolve(value):
        if isinstance(value, dict):
            if "$ref" in value:
                reference = value["$ref"]
                if not reference.startswith("#/$defs/"):
                    raise ValueError("Only local contract schema references are supported")
                return resolve(deepcopy(definitions[reference.rsplit("/", 1)[1]]))
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    return resolve(schema)


S = {"type": "string", "minLength": 1, "maxLength": 200}
P = {"project_id": S}
ACCOUNT_PROPERTIES = {**P, "provider_id": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9_.-]{0,79}$"},
                      "account_ref": {"type": "string", "pattern": r"^acct_[a-f0-9]{32}$"}}


def operation_specs(spec_factory) -> list:
    return [
        spec_factory("cost.providers", "read", "Inspect configured cost providers and honest fail-closed capability diagnostics.",
                      P, ("project_id",)),
        spec_factory("cost.policy.get", "read", "Read exact-money cost policies and ledger balances visible to this actor.",
                      ACCOUNT_PROPERTIES, ("project_id", "provider_id", "account_ref")),
        spec_factory("cost.policy.put", "modify", "Set a versioned total budget; requires spend scope and does not enable remote execution.",
                      {**P, "policy": inline_schema(BudgetPolicy),
                       "expected_revision": {"type": ["integer", "null"], "minimum": 1}},
                      ("project_id", "policy", "expected_revision"), True, ("spend",)),
        spec_factory("cost.quote.create", "read", "Request a provider quote; unavailable until a real provider is configured and verified.",
                      {**P, "binding": inline_schema(AccountBinding), "recipe_digest": {"type": "string", "pattern": r"^[a-f0-9]{64}$"},
                       "max_runtime_seconds": {"type": "integer", "minimum": 1, "maximum": 604800}},
                      ("project_id", "binding", "recipe_digest", "max_runtime_seconds")),
        spec_factory("cost.quote.get", "read", "Read an immutable quote with rate provenance, timestamp and explicit evidence type.",
                      {**P, "quote_id": S}, ("project_id", "quote_id")),
        spec_factory("cost.reserve", "execute", "Explicit spend admission contract; production reservations and dispatch remain disabled.",
                      {**P, "consent": inline_schema(SpendConsent)}, ("project_id", "consent"), True, ("spend", "read")),
        spec_factory("cost.ledger.get", "read", "Read durable reservation, execution, cleanup and billing states for a project.",
                      {**P, "reservation_id": S}, ("project_id",)),
        spec_factory("cost.cancel", "execute", "Request cancellation; a stop request never means resources or billing have stopped.",
                      {**P, "reservation_id": S}, ("project_id", "reservation_id"), True),
        spec_factory("cost.history", "read", "Read append-only project cost events with stable pagination.",
                      {**P, "after_sequence": {"type": "integer", "minimum": 0},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ("project_id",)),
    ]
