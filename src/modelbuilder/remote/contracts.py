"""Cost-control contracts; no provider credentials, rates, or launch permissions.

Amounts are integers in an explicitly named currency unit. Synthetic evidence
is not interchangeable with a provider quote, invoice, or live acceptance test.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_MONEY = 9_007_199_254_740_991
REQUIRED_COST_CATEGORIES = frozenset({"compute", "storage", "egress", "tax", "fees", "cleanup"})
REQUIRED_CAPABILITIES = frozenset({
    "hard_runtime_stop", "total_charge_upper_bound", "idempotent_submit", "recover_job",
    "cancel_and_cleanup", "idle_shutdown", "storage_cleanup", "delayed_billing",
    "invoice_reconciliation",
})


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Money(Contract):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    exponent: int = Field(default=2, strict=True, ge=0, le=6)
    minor_units: int = Field(strict=True, ge=0, le=MAX_MONEY)

    def same_unit(self, other: Money) -> bool:
        return self.currency == other.currency and self.exponent == other.exponent

    def amount(self, minor_units: int) -> Money:
        return Money(currency=self.currency, exponent=self.exponent, minor_units=minor_units)


class AccountBinding(Contract):
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,79}$")
    # App-generated opaque alias, not a provider account number, email or secret.
    account_ref: str = Field(pattern=r"^acct_[a-f0-9]{32}$")
    zenml_stack_id: str = Field(min_length=1, max_length=200)
    zenml_orchestrator_id: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=200)


class RateComponent(Contract):
    category: Literal["compute", "storage", "egress", "tax", "fees", "cleanup"]
    description: str = Field(min_length=1, max_length=300)
    unit: str = Field(min_length=1, max_length=80)
    unit_price: Money
    maximum_units: int = Field(strict=True, ge=0, le=MAX_MONEY)

    @property
    def upper_bound_minor(self) -> int:
        value = self.unit_price.minor_units * self.maximum_units
        if value > MAX_MONEY:
            raise ValueError("Cost component exceeds the supported exact-money range")
        return value


class Quote(Contract):
    quote_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    recipe_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    binding: AccountBinding
    evidence_kind: Literal["simulation", "provider_quote", "operator_estimate"]
    rate_source: str = Field(min_length=1, max_length=1000)
    rate_observed_at: datetime
    created_at: datetime
    expires_at: datetime
    max_runtime_seconds: int = Field(strict=True, ge=1, le=604800)
    estimated_total: Money
    maximum_total: Money
    components: tuple[RateComponent, ...]
    all_costs_bounded: bool = Field(strict=True)
    limitations: tuple[str, ...] = ()

    @field_validator("rate_observed_at", "created_at", "expires_at")
    @classmethod
    def aware_times(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTC-offset-aware timestamps are required")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_totals(self) -> Quote:
        if not self.estimated_total.same_unit(self.maximum_total):
            raise ValueError("Quote money units differ")
        if self.estimated_total.minor_units > self.maximum_total.minor_units:
            raise ValueError("Estimated cost exceeds maximum total")
        if self.maximum_total.minor_units == 0:
            raise ValueError("Remote reservations require a positive upper bound")
        if not self.rate_observed_at <= self.created_at < self.expires_at:
            raise ValueError("Quote timestamps are out of order")
        if any(not item.unit_price.same_unit(self.maximum_total) for item in self.components):
            raise ValueError("Rate component currencies or exponents differ")
        if self.all_costs_bounded:
            if {item.category for item in self.components} != REQUIRED_COST_CATEGORIES:
                raise ValueError("A bounded quote must include every cost category, including explicit zeroes")
            if sum(item.upper_bound_minor for item in self.components) != self.maximum_total.minor_units:
                raise ValueError("Maximum total must equal the sum of bounded cost components")
        return self


class BudgetPolicy(Contract):
    policy_id: str = Field(min_length=1, max_length=200)
    scope: Literal["account", "project"]
    project_id: str | None = Field(default=None, min_length=1, max_length=200)
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,79}$")
    account_ref: str = Field(pattern=r"^acct_[a-f0-9]{32}$")
    maximum_total: Money
    max_runtime_seconds: int = Field(strict=True, ge=1, le=604800)
    enabled: bool = Field(default=True, strict=True)
    revision: int = Field(default=1, strict=True, ge=1)

    @model_validator(mode="after")
    def validate_scope(self) -> BudgetPolicy:
        if (self.scope == "project") != (self.project_id is not None):
            raise ValueError("Project scope requires project_id; account scope forbids project_id")
        return self

    @property
    def scope_key(self) -> str:
        return ":".join((self.provider_id, self.account_ref, self.scope, self.project_id or ""))


class BudgetBalance(Contract):
    """Aggregate amounts are decimal strings: late charges can exceed JS safe integers."""
    policy: BudgetPolicy
    exposure_minor_units: str = Field(pattern=r"^(0|[1-9][0-9]*)$")
    actual_minor_units: str = Field(pattern=r"^(0|[1-9][0-9]*)$")
    held_minor_units: str = Field(pattern=r"^(0|[1-9][0-9]*)$")
    available_minor_units: str = Field(pattern=r"^(0|[1-9][0-9]*)$")
    debt_minor_units: str = Field(pattern=r"^(0|[1-9][0-9]*)$")
    authorization_overrun_minor_units: str = Field(pattern=r"^(0|[1-9][0-9]*)$")
    enforcement_alert_count: int = Field(strict=True, ge=0)
    spending_enforced: Literal[False] = False


class SpendConsent(Contract):
    """An affirmative request by the already authenticated actor, never a credential."""
    execute_and_spend: Literal[True]
    quote_id: str = Field(min_length=1, max_length=200)
    quote_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    maximum_total: Money
    max_runtime_seconds: int = Field(strict=True, ge=1, le=604800)

    @field_validator("execute_and_spend", mode="before")
    @classmethod
    def exact_consent(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Explicit execute_and_spend=true is required")
        return value


class ProviderObservation(Contract):
    """Adapter-owned evidence. Never deserialize this from a public mutation body."""
    reservation_id: str = Field(min_length=1, max_length=200)
    provider_id: str = Field(min_length=1, max_length=80)
    account_ref: str = Field(pattern=r"^acct_[a-f0-9]{32}$")
    provider_job_id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(strict=True, ge=1)
    observed_at: datetime
    execution_state: Literal["running", "stopped", "succeeded", "failed", "unknown"]
    cleanup_state: Literal["required", "in_progress", "confirmed", "unknown"]
    billing_state: Literal["pending", "provisional", "final"]
    cumulative_charge: Money
    evidence_kind: Literal["simulation", "provider_usage", "provider_invoice"]
    evidence_ref: str = Field(min_length=1, max_length=1000)
    idle_resources_stopped: bool = Field(default=False, strict=True)
    storage_resources_deleted: bool = Field(default=False, strict=True)

    @field_validator("observed_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        return Quote.aware_times(value)

    @model_validator(mode="after")
    def coherent_observation(self) -> ProviderObservation:
        if self.cleanup_state == "confirmed" and (
            self.execution_state in {"running", "unknown"}
            or not self.idle_resources_stopped or not self.storage_resources_deleted
        ):
            raise ValueError("Cleanup confirmation requires a stopped job, idle shutdown and storage deletion")
        if self.billing_state == "final" and self.evidence_kind == "provider_usage":
            raise ValueError("Usage is not a final invoice")
        return self
