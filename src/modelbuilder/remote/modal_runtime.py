"""Actual public ZenML 0.97/Modal 1.6 SDK calls, isolated from the service.

No SDK is imported, credentials resolved, or network used at import/construction.
The product adapter never invokes the submission bridge until its financial
capability gate passes. No alternate scheduler, patched SDK, private RPC, or
custom training entrypoint is used here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from importlib import import_module, metadata
from typing import Any
from uuid import UUID

from modelbuilder.models import ServiceError

from .ledger import digest
from .modal_contract import (
    MODAL_VERSION, ZENML_VERSION, ModalOperatorConfig, NativeModalPlan,
    NativeSubmissionApproval, TerminationApproval,
)
from .provider import Submission

CONTROLLER_KEY = "modal_orchestration_sandbox_id"
STEP_KEY = "sandbox_id"
STEP_PREFIX = "modal_step_sandbox_id_"


def serializable(value: Any) -> Any:
    """Preserve provider precision and signed adjustments; never float money."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite provider money")
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Naive provider timestamp")
        return value.astimezone(timezone.utc).isoformat()
    if is_dataclass(value):
        return serializable(asdict(value))
    if isinstance(value, dict):
        return {str(key): serializable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [serializable(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ValueError(f"Unsupported provider evidence type: {type(value).__name__}")


def metadata_values(resource) -> dict:
    return {key: getattr(value, "value", value) for key, value in resource.run_metadata.items()}


@dataclass(frozen=True)
class NativeContext:
    """Already-resolved native objects; not accepted from external request JSON."""
    snapshot: Any
    run: Any
    stack: Any
    environment: dict[str, str]


class ModalRuntime:
    def __init__(self, config: ModalOperatorConfig, *, client=None):
        self.config = config
        self.client = client  # An already configured SDK client, never raw credentials.

    @staticmethod
    def dependencies() -> dict:
        versions = {}
        for package, expected in (("zenml", ZENML_VERSION), ("modal", MODAL_VERSION)):
            try:
                actual = metadata.version(package)
            except metadata.PackageNotFoundError:
                actual = None
            versions[package] = {"expected": expected, "installed": actual, "compatible": actual == expected}
        return versions

    def _sdk(self):
        if not all(item["compatible"] for item in self.dependencies().values()):
            raise ServiceError("provider_dependencies", "The optional pinned ZenML/Modal runtime is required",
                               self.dependencies(), 409)
        return import_module("modal")

    def _workspace(self):
        if not self.config.allow_provider_reads:
            raise ServiceError("provider_reads_disabled", "Operator provider reads are disabled", status_code=403)
        workspace = self._sdk().Workspace.from_context(client=self.client)
        workspace.hydrate(client=self.client)
        if workspace.name != self.config.workspace_name:
            raise ServiceError("provider_identity_mismatch", "Authenticated Modal workspace does not match the configured binding", status_code=403)
        return workspace

    def load_context(self, *, snapshot_id: UUID, backend_run_id: UUID, environment: dict[str, str]) -> NativeContext:
        """Read an existing native snapshot/run/stack, never create or change one."""
        if not self.config.allow_provider_reads:
            raise ServiceError("provider_reads_disabled", "Operator provider reads are disabled", status_code=403)
        self._sdk()
        from zenml.client import Client
        from zenml.stack import Stack
        client = Client()
        snapshot = client.get_snapshot(snapshot_id, allow_prefix_match=False)
        run = client.get_pipeline_run(backend_run_id, include_full_metadata=True)
        stack = Stack.from_model(client.get_stack(UUID(self.config.binding.zenml_stack_id)))
        return NativeContext(snapshot, run, stack, dict(environment))

    def prepare(self, context: NativeContext, *, project_id: str, submit_key: str,
                recipe_digest: str, source_digest: str, environment_lock_digest: str,
                max_runtime_seconds: int, created_at: datetime) -> NativeModalPlan:
        """Bind native identities and already-built digest-pinned images.

        Source/lock digests must come from the operator's verified build manifest;
        this method does not build/push images or attest their contents remotely.
        """
        self._sdk()
        from zenml.integrations.modal.orchestrators.modal_orchestrator import ModalOrchestrator
        snapshot, run, stack = context.snapshot, context.run, context.stack
        orchestrator = stack.orchestrator
        if type(orchestrator) is not ModalOrchestrator:
            raise ServiceError("provider_identity_mismatch", "The existing native ModalOrchestrator is required", status_code=403)
        if str(stack.id) != self.config.binding.zenml_stack_id or str(orchestrator.id) != self.config.binding.zenml_orchestrator_id:
            raise ServiceError("provider_identity_mismatch", "Native stack/orchestrator changed", status_code=403)
        if snapshot.schedule or snapshot.is_dynamic:
            raise ServiceError("unsupported_remote_plan", "Only unscheduled static native pipelines are supported", status_code=409)
        if not run.snapshot or str(run.snapshot.id) != str(snapshot.id) or not run.stack or str(run.stack.id) != str(stack.id):
            raise ServiceError("provider_identity_mismatch", "Native run does not belong to the selected snapshot and stack", status_code=403)
        if str(run.project.id) != str(snapshot.project.id):
            raise ServiceError("provider_identity_mismatch", "Native snapshot/run projects differ", status_code=403)
        if stack.artifact_store.config.is_local or not stack.container_registry or stack.container_registry.config.is_local:
            raise ServiceError("unsupported_remote_plan", "The native Modal integration requires remote artifacts and registry", status_code=409)
        settings = [orchestrator.get_settings(snapshot)]
        settings.extend(orchestrator.get_settings(step) for step in snapshot.step_configurations.values())
        if any(s.modal_environment != self.config.environment_name or s.timeout > max_runtime_seconds for s in settings):
            raise ServiceError("unsupported_remote_plan", "Every controller/step must bind the approved environment and timeout", status_code=409)
        if any(s.synchronous for s in settings):
            raise ServiceError("unsupported_remote_plan", "Native remote submission must be asynchronous", status_code=409)
        images = []
        try:
            images.append(orchestrator.get_image(snapshot=snapshot))
        except KeyError:
            pass  # Native integration falls back to the first step image.
        images.extend(orchestrator.get_image(snapshot=snapshot, step_name=name) for name in snapshot.step_configurations)
        return NativeModalPlan(
            project_id=project_id, binding=self.config.binding, workspace_name=self.config.workspace_name,
            environment_name=self.config.environment_name, submit_key=submit_key, backend_run_id=run.id,
            native_project_id=run.project.id,
            snapshot_id=snapshot.id, snapshot_digest=digest(snapshot.model_dump(mode="json")),
            recipe_digest=recipe_digest, source_digest=source_digest, environment_lock_digest=environment_lock_digest,
            runtime_environment_digest=digest(context.environment), image_references=tuple(images),
            native_settings_digest=digest([s.model_dump(mode="json") for s in settings]),
            max_runtime_seconds=max_runtime_seconds, artifact_store_id=stack.artifact_store.id, created_at=created_at)

    def _check_plan(self, plan: NativeModalPlan):
        if (plan.binding != self.config.binding or plan.workspace_name != self.config.workspace_name
                or plan.environment_name != self.config.environment_name):
            raise ServiceError("provider_identity_mismatch", "Plan belongs to a different operator binding", status_code=403)

    def _submit_native(self, plan: NativeModalPlan, context: NativeContext,
                       approval: NativeSubmissionApproval) -> Submission:
        # A local source/lock verification is not an OCI build attestation.
        # There is no accepted image-receipt path in this release, including for
        # operator-supplied receipts. Block before every native/provider action.
        raise ServiceError("image_source_linkage_unverified",
                           "The OCI image has no verified linkage to the canonical source/environment build inputs",
                           {"image_source_linkage": plan.image_source_linkage}, 403)

    def _native_submission_bridge(self, plan: NativeModalPlan, context: NativeContext,
                                  approval: NativeSubmissionApproval) -> Submission:
        """Low-level native bridge; product financial admission must precede this.

        Not a public dispatch alternative. Current ModalProviderAdapter.submit
        always refuses before this method because all-in cost guarantees are
        missing. No configuration flag enables that product admission gate.
        """
        self._check_plan(plan)
        if not self.config.allow_native_submission or approval.plan_digest != plan.plan_digest:
            raise ServiceError("provider_submission_disabled", "Exact operator launch authorization is required", status_code=403)
        if approval.max_runtime_seconds != plan.max_runtime_seconds or approval.maximum_total.minor_units < 1:
            raise ServiceError("authorization_mismatch", "Launch authorization changed", status_code=403)
        current = self.prepare(context, project_id=plan.project_id, submit_key=plan.submit_key,
                               recipe_digest=plan.recipe_digest, source_digest=plan.source_digest,
                               environment_lock_digest=plan.environment_lock_digest,
                               max_runtime_seconds=plan.max_runtime_seconds, created_at=plan.created_at)
        current = current.model_copy(update={"local_build_inputs_digest": plan.local_build_inputs_digest})
        if current != plan:
            raise ServiceError("immutable_plan_changed", "Snapshot, image, source or environment changed after approval", status_code=409)
        # Never verify one workspace with our client and then silently submit
        # using a different native credential context/default profile.
        if self.client is None or context.stack.orchestrator.get_modal_client() is not self.client:
            raise ServiceError("provider_identity_mismatch", "Native submission requires the same explicit, preconfigured Modal client", status_code=403)
        self._workspace()
        # Execution remains wholly in the existing ZenML public integration.
        result = context.stack.orchestrator.submit_pipeline(
            snapshot=context.snapshot, stack=context.stack, base_environment=dict(context.environment),
            step_environments={}, placeholder_run=context.run)
        values = result.metadata if result else {}
        controller = values.get(CONTROLLER_KEY)
        if not isinstance(controller, str) or not controller.startswith("sb-"):
            raise ServiceError("submission_unknown", "Native submission returned no provider controller identity", status_code=503)
        # Persist provider identity in the native system; a failure is unknown,
        # never permission to submit a second controller.
        from zenml import log_metadata
        log_metadata(values, run_id_name_or_prefix=plan.backend_run_id)
        return Submission(provider_job_id=controller, backend_run_id=str(plan.backend_run_id))

    def _run(self, plan: NativeModalPlan):
        self._check_plan(plan)
        self._workspace()
        from zenml.client import Client
        run = Client().get_pipeline_run(plan.backend_run_id, include_full_metadata=True)
        if (str(run.id) != str(plan.backend_run_id) or not run.snapshot or str(run.snapshot.id) != str(plan.snapshot_id)
                or not run.stack or str(run.stack.id) != plan.binding.zenml_stack_id
                or str(run.project.id) != str(plan.native_project_id)):
            raise ServiceError("provider_identity_mismatch", "Native run binding changed", status_code=403)
        values = metadata_values(run)
        if (values.get("modal_app_name") not in {None, plan.app_name}
                or values.get("modal_environment") not in {None, plan.environment_name}):
            raise ServiceError("provider_identity_mismatch", "Native provider metadata changed", status_code=403)
        return run

    def recover(self, plan: NativeModalPlan) -> Submission | None:
        run = self._run(plan)
        controller = metadata_values(run).get(CONTROLLER_KEY)
        if not controller:
            # Sandbox.list only includes unfinished sandboxes and native submit
            # does not name the controller. Never guess from the first result.
            return None
        if not isinstance(controller, str) or not controller.startswith("sb-"):
            raise ServiceError("provider_identity_mismatch", "Invalid native controller identity", status_code=403)
        # Native metadata is mutable and is not provider ownership evidence.
        # The public SDK only lists unfinished members of a specific app; a
        # missing/finished ID is unknown, not permission to poll a foreign ID.
        members = self._app_members(self._app(plan))
        if controller not in members:
            return None
        members[controller].poll()
        return Submission(provider_job_id=controller, backend_run_id=str(plan.backend_run_id))

    def _app(self, plan):
        return self._sdk().App.lookup(plan.app_name, environment_name=plan.environment_name,
                                     create_if_missing=False, client=self.client)

    def _app_members(self, app) -> dict:
        """Positive provider-scoped membership, using only public SDK calls."""
        members = {}
        for sandbox in self._sdk().Sandbox.list(app_id=app.app_id, client=self.client):
            identifier = sandbox.object_id
            if not isinstance(identifier, str) or not identifier.startswith("sb-"):
                raise ServiceError("provider_identity_mismatch", "Provider returned an invalid app member identity", status_code=503)
            members[identifier] = sandbox
        return members

    @staticmethod
    def _native_ids(run) -> set[str]:
        values = metadata_values(run)
        identifiers = {str(value) for key, value in values.items() if key == CONTROLLER_KEY or key.startswith(STEP_PREFIX)}
        for step in run.steps.values():
            if value := metadata_values(step).get(STEP_KEY):
                identifiers.add(str(value))
        if any(not value.startswith("sb-") for value in identifiers):
            raise ServiceError("provider_identity_mismatch", "Invalid native child sandbox identity", status_code=403)
        return identifiers

    def inspect(self, plan: NativeModalPlan) -> dict:
        run = self._run(plan)
        return self._inspect_run(plan, run, self._app(plan), {})

    def _inspect_run(self, plan, run, app, proven: dict) -> dict:
        # `proven` contains handles observed in this exact app during the current
        # cancellation call only. No metadata value or supplied receipt enters
        # it. This lets us re-poll resources after they leave the active listing.
        native_ids = self._native_ids(run)
        active_members = self._app_members(app)
        active = set(active_members)
        proven = {**proven, **active_members}
        verified_native = native_ids & proven.keys()
        unverified_native = native_ids - proven.keys()
        statuses = {}
        for sandbox_id in sorted(verified_native | active):
            statuses[sandbox_id] = proven[sandbox_id].poll()
        controller = metadata_values(run).get(CONTROLLER_KEY)
        controller_code = statuses.get(controller)
        controller_verified = bool(controller) and controller in verified_native
        state = "unknown" if not controller_verified else "running" if controller_code is None else "succeeded" if controller_code == 0 else "failed"
        idle = controller_verified and not unverified_native and all(code is not None for code in statuses.values()) and not active
        return {"backend_run_id": str(plan.backend_run_id), "provider_job_id": controller if controller_verified else None,
                "native_controller_id": controller,
                "provider_app_id": app.app_id, "execution_state": state,
                "sandbox_return_codes": statuses, "idle_resources_stopped": idle,
                "native_sandbox_ids": sorted(native_ids), "unattributed_active_sandbox_ids": sorted(active - native_ids),
                "verified_native_sandbox_ids": sorted(verified_native), "unverified_native_sandbox_ids": sorted(unverified_native),
                "cleanup_state": "unknown", "storage_resources_deleted": False,
                "persistent_artifact_store_id": str(plan.artifact_store_id),
                "limitations": ["Provider listing covers unfinished sandboxes only.",
                                "Native metadata without positive app membership is unverified; absence does not prove ownership or completion.",
                                "Terminated compute is not proof of provider-side storage deletion.",
                                "Native model/dataset artifacts and registry images are retained."]}

    def terminate(self, plan: NativeModalPlan, approval: TerminationApproval) -> dict:
        self._check_plan(plan)
        if (not self.config.allow_termination or approval.plan_digest != plan.plan_digest
                or approval.backend_run_id != plan.backend_run_id or approval.workspace_name != plan.workspace_name
                or approval.environment_name != plan.environment_name):
            raise ServiceError("provider_termination_disabled", "Exact operator-owned termination approval is required", status_code=403)
        run = self._run(plan)
        app = self._app(plan)
        controller = metadata_values(run).get(CONTROLLER_KEY)
        if not controller:
            raise ServiceError("recovery_unknown", "Controller identity is unknown; no arbitrary sandbox is selected", status_code=503)
        known = self._native_ids(run)
        proven = self._app_members(app)
        if known - proven.keys():
            raise ServiceError("provider_ownership_unverified",
                               "Native sandbox IDs lack positive membership in the approved provider app; no termination performed",
                               {"unverified_sandbox_ids": sorted(known - proven.keys())}, 503)
        # Stop only a positively owned producer, then reconcile children twice.
        # Late or failed observations remain unknown instead of claiming cleanup.
        if proven[controller].poll() is None:
            proven[controller].terminate()
        for _ in range(2):
            run = self._run(plan)
            if metadata_values(run).get(CONTROLLER_KEY) != controller:
                raise ServiceError("provider_identity_mismatch", "Native controller identity changed during termination", status_code=503)
            known = self._native_ids(run)
            active_members = self._app_members(app)
            active = set(active_members)
            proven.update(active_members)
            if known - proven.keys():
                raise ServiceError("provider_ownership_unverified", "A child sandbox has no verified ownership in the approved provider app",
                                   {"unverified_sandbox_ids": sorted(known - proven.keys())}, 503)
            # App names are bound to a native run, but unrelated objects in the
            # same app are still not deletion/termination authorization.
            if active - known:
                raise ServiceError("cleanup_unknown", "Unattributed active sandboxes require native identity recovery", status_code=503)
            for sandbox_id in sorted(known):
                sandbox = proven[sandbox_id]
                if sandbox.poll() is None:
                    sandbox.terminate()
        final_run = self._run(plan)
        if metadata_values(final_run).get(CONTROLLER_KEY) != controller:
            raise ServiceError("provider_identity_mismatch", "Native controller identity changed during termination", status_code=503)
        return self._inspect_run(plan, final_run, app, proven)

    def rates(self) -> dict:
        workspace = self._workspace()
        return {"kind": "provider_rates", "provider": "modal", "workspace": self.config.workspace_name,
                "currency": "USD", "observed_at": datetime.now(timezone.utc).isoformat(),
                "source": "https://modal.com/docs/sdk/py/latest/Workspace#billingrates",
                "rates": serializable(dict(workspace.billing.rates())), "all_costs_bounded": False}

    def usage(self, plan: NativeModalPlan, *, start: datetime, end: datetime) -> dict:
        self._check_plan(plan)
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError("A nonempty UTC-aware billing interval is required")
        workspace = self._workspace()
        status = self.inspect(plan)
        # App-level rows may include unattributed objects; do not silently call
        # that the job's charges. Retain them separately for operator review.
        owned = set(status["verified_native_sandbox_ids"])
        rows = workspace.billing.report(start=start.astimezone(timezone.utc), end=end.astimezone(timezone.utc), resolution="h")
        selected = [row for row in rows if row.object_id in owned and row.environment_name == plan.environment_name]
        app_rows = [row for row in rows if row.object_id == status["provider_app_id"] and row.environment_name == plan.environment_name]
        return {"kind": "provider_usage", "provider": "modal", "workspace": plan.workspace_name,
                "plan_digest": plan.plan_digest, "currency": "USD", "observed_at": datetime.now(timezone.utc).isoformat(),
                "source": "https://modal.com/docs/sdk/py/latest/Workspace#billingreport",
                "requested_start": start.isoformat(), "requested_end": end.isoformat(),
                "rows": serializable(selected), "app_aggregate_rows": serializable(app_rows),
                "billing_state": "provisional" if selected or app_rows else "pending",
                "cumulative_charge": None, "invoice_final": False,
                "limitations": ["Only complete hourly intervals are returned; empty data is not zero charges.",
                                "Rows are metered usage before credits, reservations and egress allowance.",
                                "Object attribution and adjustments are not a job-scoped final invoice."]}

    def billing_summary(self, cycle: str) -> dict:
        import re
        if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", cycle):
            raise ValueError("Use an explicit YYYY-MM billing cycle")
        workspace = self._workspace()
        return {"kind": "provider_billing_summary", "provider": "modal", "workspace": self.config.workspace_name,
                "currency": "USD", "cycle": cycle, "observed_at": datetime.now(timezone.utc).isoformat(),
                "source": "https://modal.com/docs/sdk/py/latest/Workspace#billingsummary",
                "summary": serializable(workspace.billing.summary(cycle=cycle)), "invoice_final": False,
                "limitations": ["Workspace billed_cost is provider-reported; it is not allocated to this job.",
                                "The SDK exposes no invoice ID or immutable finality signal."]}
