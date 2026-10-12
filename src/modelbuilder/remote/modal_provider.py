"""Real Modal adapter, with an intentionally unsatisfied financial launch gate.

The provider bridge is implemented; remote product acceptance is not. Public
pricing/budget documentation is not a job quote, and a usage report is not an
invoice. No boolean, caller-supplied cap, or simulator result changes that.
"""
from __future__ import annotations

from datetime import datetime

from modelbuilder.models import ServiceError

from .modal_contract import (
    MODAL_BLOCKERS, ModalOperatorConfig, NativeModalPlan, NativeSubmissionApproval,
    TerminationApproval, implementation_diagnostics,
)
from .modal_journal import ModalJournal
from .modal_runtime import ModalRuntime, NativeContext
from .provider import Submission


class ModalProviderAdapter:
    simulation = False

    def __init__(self, config: ModalOperatorConfig, journal: ModalJournal, *, runtime: ModalRuntime | None = None):
        self.config, self.binding, self.journal = config, config.binding, journal
        self.runtime = runtime or ModalRuntime(config)
        if self.runtime.config != config:
            raise ValueError("Runtime and adapter operator bindings differ")

    def declared_capabilities(self) -> frozenset[str]:
        # None of the ledger's *complete* capability promises are established.
        # More narrow implemented functions are listed separately in diagnostics.
        return frozenset()

    def diagnostics(self) -> dict:
        return {**implementation_diagnostics(), "configured": True,
                "binding": self.binding.model_dump(mode="json"),
                "adapter_kind": "native_modal_unverified", "dependencies": self.runtime.dependencies()}

    @staticmethod
    def _financial_block():
        raise ServiceError("provider_total_bound_unavailable",
                           "Modal cannot currently establish the required provider-enforced all-in total-charge bound",
                           {"blockers": list(MODAL_BLOCKERS), "remote_execution_enabled": False,
                            "spending_enforced": False}, 403)

    def quote(self, project_id: str, recipe_digest: str, max_runtime_seconds: int, request_id: str):
        # Never turn a public rate or manually declared budget into a Quote.
        self._financial_block()

    def submit(self, reservation: dict) -> Submission:
        # Gate BEFORE imports, credentials, provider reads, journal claims and
        # all side effects. There is intentionally no production-unlock flag.
        self._financial_block()

    def prepare(self, context: NativeContext, **inputs) -> NativeModalPlan:
        plan = self.runtime.prepare(context, **inputs)
        self.journal.bind(plan)
        return plan

    def prepare_from_project(self, context: NativeContext, *, workspaces, project, recipe, backend,
                             submit_key: str, max_runtime_seconds: int, created_at: datetime) -> dict:
        from .modal_build_inputs import verify_local_build_inputs
        verified = verify_local_build_inputs(workspaces=workspaces, project=project, recipe=recipe, backend=backend)
        plan = self.runtime.prepare(context, project_id=verified.project_id, submit_key=submit_key,
            recipe_digest=verified.recipe_digest, source_digest=verified.source_digest,
            environment_lock_digest=verified.environment_lock_digest, max_runtime_seconds=max_runtime_seconds,
            created_at=created_at).model_copy(update={"local_build_inputs_digest": verified.evidence_digest})
        self.journal.bind(plan)
        return {"plan": plan.model_dump(mode="json"), "local_build_inputs": verified.model_dump(mode="json"),
                "preflight": self.preflight(plan)}

    def preflight(self, plan: NativeModalPlan) -> dict:
        self.runtime._check_plan(plan)
        return {"plan_digest": plan.plan_digest, "admissible": False, "remote_execution_enabled": False,
                "local_build_inputs_verified": plan.local_build_inputs_digest is not None,
                "image_source_linkage": plan.image_source_linkage, "blockers": list(MODAL_BLOCKERS)}

    def _dispatch_admitted(self, plan: NativeModalPlan, context: NativeContext,
                           approval: NativeSubmissionApproval) -> Submission:
        """Native at-most-once transaction for future financial admission.

        Kept behind the same permanent gate, including for direct operator use.
        The SDK bridge can be tested offline without enabling this transaction.
        """
        self._financial_block()
        return self._native_transaction(plan, context, approval)

    def _native_transaction(self, plan: NativeModalPlan, context: NativeContext,
                            approval: NativeSubmissionApproval) -> Submission:
        """Internal transaction; no public operation or CLI invokes this.

        The only intended caller is _dispatch_admitted after financial admission.
        Its claim remains unknown after any exception, including process death.
        """
        if approval.plan_digest != plan.plan_digest:
            raise ServiceError("authorization_mismatch", "Spend approval does not bind the native plan", status_code=403)
        if not self.journal.claim_once(plan, approval):
            recovered = self.recover(plan.submit_key)
            if recovered is None:
                raise ServiceError("submission_unknown", "Prior native submission remains unknown; no resubmission", status_code=503)
            return recovered
        try:
            result = self.runtime._submit_native(plan, context, approval)
        except Exception:
            raise ServiceError("submission_unknown", "Native submission outcome is unknown; retain reservation and recover", status_code=503) from None
        self.journal.record(plan, result)
        return result

    def _plan(self, submit_key: str) -> NativeModalPlan:
        plan = NativeModalPlan.model_validate(self.journal.get(submit_key)["plan"])
        self.runtime._check_plan(plan)
        return plan

    def recover(self, submit_key: str) -> Submission | None:
        plan = self._plan(submit_key)
        current = self.journal.get(submit_key)["submission"]
        result = self.runtime.recover(plan)
        if result is None:
            # A missing native record never proves no resources or no charges.
            return None
        if current and current != result.model_dump(mode="json"):
            raise ServiceError("provider_identity_mismatch", "Recovered native identity differs from the durable record", status_code=409)
        self.journal.record(plan, result)
        return result

    def inspect(self, submit_key: str) -> dict:
        plan = self._plan(submit_key)
        return {**self.runtime.inspect(plan), "plan_digest": plan.plan_digest,
                "observation_sequence": self.journal.next_sequence(submit_key),
                "billing_state": "unknown", "cumulative_charge": None,
                "spending_enforced": False}

    def stop_approved(self, submit_key: str, approval: TerminationApproval) -> dict:
        return self.runtime.terminate(self._plan(submit_key), approval)

    def stop(self, submission: Submission, request_id: str) -> None:
        # Protocol compatibility does not invent operator approval or accept an
        # arbitrary request ID as authorization to terminate resources.
        raise ServiceError("provider_termination_approval_required",
                           "Use stop_approved with the exact native plan and operator termination approval", status_code=403)

    def cleanup(self, submission: Submission, request_id: str) -> None:
        raise ServiceError("provider_storage_cleanup_unverified",
                           "No provider storage-deletion receipt is available; persistent native artifacts are retained", status_code=409)

    def observe(self, reservation: dict):
        # ProviderObservation requires an actual cumulative job charge. Modal
        # cannot safely provide that from delayed/unallocated usage alone.
        plan = self._plan(reservation["submit_key"])
        if reservation.get("binding") != self.binding.model_dump(mode="json") or reservation.get("project_id") != plan.project_id:
            raise ServiceError("provider_identity_mismatch", "Reservation does not match the native plan", status_code=403)
        raise ServiceError("provider_billing_attribution_unknown",
                           "Use provider_usage_evidence; usage cannot be promoted to cumulative invoice charges or finality",
                           {"hold_retained": True, "cumulative_charge": None}, 409)

    def provider_usage_evidence(self, submit_key: str, *, start: datetime, end: datetime) -> dict:
        return self.runtime.usage(self._plan(submit_key), start=start, end=end)
