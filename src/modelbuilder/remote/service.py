"""Safe public diagnostics plus opt-in LOCAL simulator orchestration.

There is deliberately no live provider registration or remote execution switch.
A future funded provider gate must be reviewed and implemented explicitly; a
capability boolean, client quote, or claimed invoice cannot unlock dispatch.
"""
from __future__ import annotations

from modelbuilder.models import Actor, ServiceError

from .contracts import SpendConsent
from .ledger import CostLedger, require
from .provider import LocalCostSimulator, ProviderAdapter, Submission, diagnostics


class RemoteCostService:
    def __init__(self, ledger: CostLedger, *, simulator: LocalCostSimulator | None = None):
        if simulator is not None and type(simulator) is not LocalCostSimulator:
            raise ValueError("Only the built-in deterministic local simulator can be configured in this release")
        self.ledger = ledger
        self._simulator = simulator

    def providers(self, actor: Actor, project_id: str) -> dict:
        require(actor, {"read"}, project_id)
        return diagnostics(self._simulator)

    def submit_remote(self, actor: Actor, project_id: str, consent: SpendConsent, request_id: str) -> dict:
        require(actor, {"execute", "spend"}, project_id)
        raise ServiceError("remote_disabled", "Production execution is disabled pending authorized real-provider enforcement tests",
                           diagnostics(), 403)

    def _simulator_for(self, reservation: dict | None = None) -> LocalCostSimulator:
        adapter = self._simulator
        if adapter is None:
            raise ServiceError("simulation_disabled", "No explicit local test simulator is configured", status_code=403)
        if reservation is not None and (reservation["evidence_kind"] != "simulation" or
                                       reservation["binding"] != adapter.binding.model_dump(mode="json")):
            raise ServiceError("provider_identity_mismatch", "Reservation is not bound to this local simulator", status_code=403)
        return adapter

    def quote_simulated(self, actor: Actor, project_id: str, recipe_digest: str,
                        max_runtime_seconds: int, request_id: str) -> dict:
        require(actor, {"read"}, project_id)
        adapter = self._simulator_for()
        quote = adapter.quote(project_id, recipe_digest, max_runtime_seconds, actor.id + ":" + request_id)
        return self.ledger._record_quote(actor, quote, request_id, requested_inputs={
            "project_id": project_id, "recipe_digest": recipe_digest, "max_runtime_seconds": max_runtime_seconds,
            "binding": adapter.binding.model_dump(mode="json")})

    def reserve_simulated(self, actor: Actor, project_id: str, consent: SpendConsent, request_id: str) -> dict:
        require(actor, {"execute", "spend"}, project_id)
        adapter = self._simulator_for()
        # Quote reading requires read scope in addition to execute+spend.
        quote = self.ledger.get_quote(actor, project_id, consent.quote_id)["quote"]
        if quote["evidence_kind"] != "simulation" or quote["binding"] != adapter.binding.model_dump(mode="json"):
            raise ServiceError("evidence_mismatch", "Only this simulator's quote can enter simulated admission", status_code=403)
        return self.ledger._reserve(actor, project_id, consent, request_id)

    def launch_simulated(self, actor: Actor, project_id: str, reservation_id: str) -> dict:
        require(actor, {"execute", "spend"}, project_id)
        reservation = self.ledger.get_reservation(actor, project_id, reservation_id)
        adapter = self._simulator_for(reservation)
        if reservation["actor_id"] != actor.id:
            raise ServiceError("forbidden", "The actor that approved spend must launch this reservation", status_code=403)
        if reservation["execution_state"] != "reserved":
            # Never resubmit after an uncertain response. Recover via the stable key.
            if reservation["provider_job_id"]:
                return reservation
            return self._recover_submission(adapter, reservation)
        reservation = self.ledger._claim_submission(reservation_id)
        try:
            result = adapter.submit(reservation)
        except Exception:
            self.ledger._submission_unknown(reservation_id)
            raise ServiceError("submission_unknown", "Submission outcome is unknown; hold retained and recovery is required", status_code=503) from None
        return self.ledger._record_submission(reservation_id, result.provider_job_id, result.backend_run_id)

    def _recover_submission(self, adapter: ProviderAdapter, reservation: dict) -> dict:
        if reservation["execution_state"] not in {"submitting", "unknown", "stop_requested"}:
            raise ServiceError("invalid_transition", "Reservation is not recoverable as a submitted job", status_code=409)
        try:
            result = adapter.recover(reservation["submit_key"])
        except Exception:
            raise ServiceError("recovery_unknown", "Provider recovery is unavailable; hold retained", status_code=503) from None
        if result is None:
            raise ServiceError("recovery_unknown", "No authoritative submission outcome; hold retained", status_code=503)
        return self.ledger._record_submission(reservation["reservation_id"], result.provider_job_id, result.backend_run_id)

    def reconcile_simulated(self, actor: Actor, project_id: str, reservation_id: str) -> dict:
        require(actor, {"read", "execute"}, project_id)
        reservation = self.ledger.get_reservation(actor, project_id, reservation_id)
        adapter = self._simulator_for(reservation)
        if not reservation["provider_job_id"]:
            reservation = self._recover_submission(adapter, reservation)
        observation = adapter.observe(reservation)
        return self.ledger._apply_observation(observation)

    def tick_simulated(self) -> list[dict]:
        """Internal trusted controller tick. No public endpoint and no remote work."""
        self._simulator_for()
        results = []
        reconciled = set()
        for reservation in self.ledger.recover_due():
            try:
                adapter = self._simulator_for(reservation)
                if not reservation["provider_job_id"]:
                    reservation = self._recover_submission(adapter, reservation)
                submission = Submission(provider_job_id=reservation["provider_job_id"], backend_run_id=reservation["backend_run_id"])
                adapter.stop(submission, "stop:" + reservation["reservation_id"])
                adapter.cleanup(submission, "cleanup:" + reservation["reservation_id"])
                results.append(self.ledger._apply_observation(adapter.observe(reservation)))
                reconciled.add(reservation["reservation_id"])
            except Exception:
                # Failures never release the hold or masquerade as successful cleanup.
                results.append({"reservation_id": reservation["reservation_id"], "status": "cleanup_unknown", "hold_retained": True})
        # Billing progresses independently of worker state and cleanup. Also inspect
        # settled jobs for delayed charges; never claim that finality prevents them.
        for reservation in self.ledger._reconciliation_candidates():
            if reservation["reservation_id"] in reconciled:
                continue
            try:
                adapter = self._simulator_for(reservation)
                results.append(self.ledger._apply_observation(adapter.observe(reservation)))
            except Exception:
                results.append({"reservation_id": reservation["reservation_id"], "status": "billing_unknown",
                                "last_verified_exposure_retained": True})
        return results
