"""Dependency-light contracts for the existing ZenML/Modal integration.

These are operator-side contracts, not public HTTP/MCP request bodies. Installing
an SDK, supplying a workspace name, or accepting a rate is never spend approval.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .contracts import AccountBinding, Contract, Money
from .ledger import digest

MODAL_VERSION = "1.6.1"
ZENML_VERSION = "0.97.0"
MODAL_BLOCKERS = (
    "No verified build receipt links the declared source/environment lock to the remote OCI image contents.",
    "The public Modal SDK does not expose authoritative workspace spend-limit readback or enforcement receipts.",
    "A Modal workspace limit does not cover the external ZenML artifact store, container registry, or their fees.",
    "All-in tax, subscription, storage, egress and cleanup ceilings have not been established.",
    "ZenML's native Modal orchestrator does not pass a provider idempotency key to Sandbox.create.",
    "Per-sandbox timeout does not establish one provider-enforced deadline for the controller and late child sandboxes.",
    "Usage and workspace billing summaries have no job-scoped invoice-finality signal.",
    "No authorized funded concurrency, termination, storage or invoice acceptance has run.",
)


def implementation_diagnostics() -> dict:
    """Static, safe for the service environment; never imports an SDK or connects."""
    return {
        "provider_id": "modal", "implementation": "zenml.integrations.modal.orchestrators.ModalOrchestrator",
        "zenml_version": ZENML_VERSION, "sdk": "modal", "sdk_version": MODAL_VERSION,
        "implemented": ["native_submission_bridge", "immutable_native_plan", "durable_at_most_once_claim",
                        "native_identity_recovery", "sandbox_status", "scoped_termination",
                        "workspace_rates", "object_usage", "workspace_billing_summary"],
        "remote_execution_enabled": False, "spending_enforced": False,
        "remote_acceptance": "BLOCKED", "verified_remote_capabilities": [],
        "blockers": list(MODAL_BLOCKERS),
    }


class ModalOperatorConfig(Contract):
    binding: AccountBinding
    workspace_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    environment_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    # Off by default. These switches alone never authorize a mutation.
    allow_provider_reads: bool = Field(default=False, strict=True)
    allow_termination: bool = Field(default=False, strict=True)
    allow_native_submission: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def provider_is_modal(self):
        if self.binding.provider_id != "modal":
            raise ValueError("The native Modal adapter requires provider_id=modal")
        return self


class NativeModalPlan(Contract):
    project_id: str = Field(min_length=1, max_length=200)
    binding: AccountBinding
    workspace_name: str
    environment_name: str
    submit_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    backend_run_id: UUID
    native_project_id: UUID
    snapshot_id: UUID
    snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    recipe_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_lock_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    native_settings_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    local_build_inputs_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    image_source_linkage: Literal["unverified"] = "unverified"
    image_references: tuple[str, ...] = Field(min_length=1)
    max_runtime_seconds: int = Field(strict=True, ge=1, le=86400)
    # Persistent native artifacts are explicitly retained, never cleanup targets.
    artifact_store_id: UUID
    created_at: datetime

    @field_validator("image_references")
    @classmethod
    def immutable_images(cls, values):
        import re
        if any(not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image) for image in values):
            raise ValueError("Every native controller/step image must be pinned by OCI SHA256 digest")
        return tuple(sorted(set(values)))

    @field_validator("created_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("An aware timestamp is required")
        return value.astimezone(timezone.utc)

    @property
    def plan_digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    @property
    def app_name(self) -> str:
        # The existing ZenML 0.97.0 helper uses this exact native UUID.
        return f"zenml-{self.backend_run_id}"


class TerminationApproval(Contract):
    """An operator-owned, exact approval, never accepted from a public payload."""
    action: Literal["terminate_owned_sandboxes"]
    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    backend_run_id: UUID
    workspace_name: str
    environment_name: str
    request_id: str = Field(min_length=1, max_length=200)
    actor_id: str = Field(min_length=1, max_length=200)


class NativeSubmissionApproval(Contract):
    """Reserved for a future admitted launch; no current service can issue one.

    This record does not itself establish a provider total-charge ceiling. The
    adapter must pass financial admission before invoking the native bridge.
    """
    execute_and_spend: Literal[True]
    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    maximum_total: Money
    max_runtime_seconds: int = Field(strict=True, ge=1, le=86400)
    actor_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)

    @field_validator("execute_and_spend", mode="before")
    @classmethod
    def exact_bool(cls, value):
        if value is not True:
            raise ValueError("Explicit execute_and_spend=true is required")
        return value
