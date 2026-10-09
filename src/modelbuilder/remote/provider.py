"""ZenML/provider adapter boundary and an explicitly local deterministic simulator.

No provider SDK, network request, credentials, provisioning, or training runner
is implemented here. A future adapter delegates execution to its existing
ZenML stack/orchestrator, and provides separate cleanup/billing observations.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Protocol

from pydantic import Field

from .contracts import (
    AccountBinding, Contract, Money, ProviderObservation, Quote, RateComponent,
    REQUIRED_CAPABILITIES, REQUIRED_COST_CATEGORIES,
)
from .ledger import digest


class Submission(Contract):
    provider_job_id: str = Field(min_length=1, max_length=200)
    backend_run_id: str = Field(min_length=1, max_length=200)


class ProviderAdapter(Protocol):
    """Trusted operator-owned objects. Public clients cannot register implementations.

    submit must use reservation.submit_key as its provider idempotency key and
    bind the supplied maximum total and hard deadline to provider enforcement.
    recover(None) means unknown, never proof of no billable resources.
    cleanup must verify idle compute AND persistent storage independently.
    observe must identify cumulative account/job charges, source and finality.
    """
    binding: AccountBinding
    simulation: bool

    def declared_capabilities(self) -> frozenset[str]: ...
    def quote(self, project_id: str, recipe_digest: str, max_runtime_seconds: int, request_id: str) -> Quote: ...
    def submit(self, reservation: dict) -> Submission: ...
    def recover(self, submit_key: str) -> Submission | None: ...
    def stop(self, submission: Submission, request_id: str) -> None: ...
    def cleanup(self, submission: Submission, request_id: str) -> None: ...
    def observe(self, reservation: dict) -> ProviderObservation: ...


def diagnostics(adapter: ProviderAdapter | None = None) -> dict:
    declared = adapter.declared_capabilities() if adapter else frozenset()
    return {
        "configured": adapter is not None,
        "adapter_kind": "local_simulator" if adapter and adapter.simulation else "unverified_provider" if adapter else "unconfigured",
        "binding": adapter.binding.model_dump(mode="json") if adapter else None,
        "declared_capabilities": sorted(declared),
        "missing_declared_capabilities": sorted(REQUIRED_CAPABILITIES - declared),
        "verified_remote_capabilities": [],
        "remote_execution_enabled": False,
        "spending_enforced": False,
        "remote_acceptance": "BLOCKED",
        "blockers": [
            "Production dispatch is disabled in this release.",
            "No configured provider has passed authorized funded concurrency, cancellation, cleanup and invoice tests.",
            "Local simulator results are accounting-contract tests, not remote enforcement evidence.",
        ],
    }


class LocalCostSimulator:
    """State-machine test double with caller-supplied, explicitly synthetic money.

    The instance represents surviving provider state while a controller/ledger
    is restarted in tests. It has no external side effects or credentials.
    """
    simulation = True

    def __init__(self, binding: AccountBinding, synthetic_maximum_total: Money, *, clock: Callable[[], datetime],
                 quote_lifetime_seconds: int = 300):
        if binding.provider_id != "local-simulator":
            raise ValueError("Simulator requires the explicit local-simulator provider identity")
        if synthetic_maximum_total.minor_units < 1 or quote_lifetime_seconds < 1:
            raise ValueError("Simulator requires positive synthetic money and quote lifetime")
        self.binding = binding
        self.synthetic_maximum_total = synthetic_maximum_total
        self.clock = clock
        self.quote_lifetime_seconds = quote_lifetime_seconds
        self.quotes: dict[str, Quote] = {}
        self.jobs: dict[str, dict] = {}
        self.submit_calls = 0
        self.stop_calls = 0
        self.cleanup_calls = 0
        self.fail_after_submission = False
        self.cleanup_available = True
        self.recovery_available = True
        self.billing_final = False

    def declared_capabilities(self) -> frozenset[str]:
        # Declarations are not verified capabilities, even in this simulator.
        return REQUIRED_CAPABILITIES

    def quote(self, project_id: str, recipe_digest: str, max_runtime_seconds: int, request_id: str) -> Quote:
        key = digest({"project_id": project_id, "recipe_digest": recipe_digest,
                      "runtime": max_runtime_seconds, "request_id": request_id})
        if key not in self.quotes:
            now = self.clock()
            money = self.synthetic_maximum_total
            components = tuple(RateComponent(category=category, description="Explicit synthetic test fixture",
                                             unit="entire bounded simulated job", maximum_units=1,
                                             unit_price=money if category == "compute" else money.amount(0))
                               for category in sorted(REQUIRED_COST_CATEGORIES))
            self.quotes[key] = Quote(
                quote_id="simulation-" + key, project_id=project_id, recipe_digest=recipe_digest,
                binding=self.binding, evidence_kind="simulation", rate_source="simulation://explicit-test-fixture",
                rate_observed_at=now, created_at=now,
                expires_at=now + timedelta(seconds=self.quote_lifetime_seconds),
                max_runtime_seconds=max_runtime_seconds, estimated_total=money, maximum_total=money,
                components=components, all_costs_bounded=True,
                limitations=("Synthetic fixture amounts are not provider prices or actual charges.",
                             "No real remote resource, runtime stop, invoice or budget enforcement is verified."),
            )
        return self.quotes[key]

    def submit(self, reservation: dict) -> Submission:
        key = reservation["submit_key"]
        self.submit_calls += 1
        if key not in self.jobs:
            self.jobs[key] = {
                "submission": Submission(provider_job_id="simulation-job-" + key, backend_run_id="simulation-zenml-run-" + key),
                "execution_state": "running", "cleanup_state": "required", "sequence": 0,
                "charge": self.synthetic_maximum_total.amount(0), "billing_final": False,
                "idle_resources_stopped": False, "storage_resources_deleted": False,
            }
        if self.fail_after_submission:
            raise TimeoutError("Simulated ambiguous submit response")
        return self.jobs[key]["submission"]

    def recover(self, submit_key: str) -> Submission | None:
        if not self.recovery_available:
            raise TimeoutError("Simulated unavailable provider recovery")
        job = self.jobs.get(submit_key)
        return job["submission"] if job else None

    def _job(self, submission: Submission) -> dict:
        return next(job for job in self.jobs.values() if job["submission"] == submission)

    def stop(self, submission: Submission, request_id: str) -> None:
        self.stop_calls += 1
        job = self._job(submission)
        job["execution_state"] = "stopped"
        job["idle_resources_stopped"] = True

    def cleanup(self, submission: Submission, request_id: str) -> None:
        self.cleanup_calls += 1
        if not self.cleanup_available:
            raise TimeoutError("Simulated storage cleanup failure")
        job = self._job(submission)
        if job["execution_state"] == "running":
            raise RuntimeError("Stop is required before simulated cleanup")
        job["cleanup_state"] = "confirmed"
        job["storage_resources_deleted"] = True

    def set_cumulative_charge(self, submit_key: str, minor_units: int, *, final: bool = False) -> None:
        """Test-fixture control only. This is not a service operation."""
        job = self.jobs[submit_key]
        job["charge"] = self.synthetic_maximum_total.amount(minor_units)
        job["billing_final"] = final

    def observe(self, reservation: dict) -> ProviderObservation:
        job = self.jobs[reservation["submit_key"]]
        job["sequence"] += 1
        return ProviderObservation(
            reservation_id=reservation["reservation_id"], provider_id=self.binding.provider_id,
            account_ref=self.binding.account_ref, provider_job_id=job["submission"].provider_job_id,
            sequence=job["sequence"], observed_at=self.clock(), execution_state=job["execution_state"],
            cleanup_state=job["cleanup_state"], billing_state="final" if job["billing_final"] else "provisional",
            cumulative_charge=job["charge"], evidence_kind="simulation",
            evidence_ref="simulation://observation/" + reservation["submit_key"] + "/" + str(job["sequence"]),
            idle_resources_stopped=job["idle_resources_stopped"], storage_resources_deleted=job["storage_resources_deleted"],
        )
