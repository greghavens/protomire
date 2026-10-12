"""Local safety/accounting tests; NOT real provider or funded acceptance."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import platform
import subprocess
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from modelbuilder.models import ServiceError
from modelbuilder.remote.contracts import AccountBinding, Money
from modelbuilder.remote.modal_contract import ModalOperatorConfig, NativeModalPlan, NativeSubmissionApproval, TerminationApproval
from modelbuilder.remote.modal_journal import ModalJournal
from modelbuilder.remote.modal_provider import ModalProviderAdapter
from modelbuilder.remote.modal_runtime import ModalRuntime, serializable
from modelbuilder.remote.provider import Submission, diagnostics

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)
RUN = UUID("00000000-0000-0000-0000-000000000003")
BINDING = AccountBinding(provider_id="modal", account_ref="acct_" + "a" * 32,
                         zenml_stack_id="00000000-0000-0000-0000-000000000001",
                         zenml_orchestrator_id="00000000-0000-0000-0000-000000000002", target_id="modal-test-cpu")


def config(**kw):
    return ModalOperatorConfig(binding=BINDING, workspace_name="approved-workspace", environment_name="staging", **kw)


def plan(**kw):
    body = dict(project_id="p", binding=BINDING, workspace_name="approved-workspace", environment_name="staging",
                submit_key="1" * 64, backend_run_id=RUN, native_project_id=UUID(int=4), snapshot_id=UUID(int=5),
                snapshot_digest="2" * 64, recipe_digest="3" * 64, source_digest="4" * 64,
                environment_lock_digest="5" * 64, runtime_environment_digest="6" * 64, native_settings_digest="7" * 64,
                image_references=("registry.example/image@sha256:" + "8" * 64,),
                max_runtime_seconds=120, artifact_store_id=UUID(int=6), created_at=NOW)
    return NativeModalPlan(**(body | kw))


def approval(p):
    return NativeSubmissionApproval(execute_and_spend=True, plan_digest=p.plan_digest,
                                    maximum_total=Money(currency="USD", minor_units=100), max_runtime_seconds=120,
                                    actor_id="operator", request_id="approved-test-request")


def termination(p):
    return TerminationApproval(action="terminate_owned_sandboxes", plan_digest=p.plan_digest,
                               backend_run_id=p.backend_run_id, workspace_name=p.workspace_name,
                               environment_name=p.environment_name, actor_id="operator", request_id="stop-1")


def test_no_network_or_sdk_import_on_construction_or_financial_gate(tmp_path, monkeypatch):
    runtime = ModalRuntime(config(allow_provider_reads=True, allow_termination=True, allow_native_submission=True))
    monkeypatch.setattr(runtime, "_sdk", lambda: pytest.fail("SDK must not be touched"))
    journal = ModalJournal(tmp_path / "journal.sqlite")
    provider = ModalProviderAdapter(runtime.config, journal, runtime=runtime)
    for fn in (lambda: provider.quote("p", "a" * 64, 10, "q"), lambda: provider.submit({"maximum_total": 100}),
               lambda: provider._dispatch_admitted(plan(), None, approval(plan()))):
        with pytest.raises(ServiceError, match="all-in total-charge") as caught:
            fn()
        assert caught.value.code == "provider_total_bound_unavailable"
    assert provider.declared_capabilities() == frozenset()
    assert diagnostics()["remote_execution_enabled"] is False
    assert diagnostics()["available_implementations"][0]["provider_id"] == "modal"
    assert journal.store.connection.execute("SELECT count(*) FROM modal_submissions").fetchone()[0] == 0
    journal.close()


@pytest.mark.parametrize("image", ["ubuntu:latest", "registry/x:v1", "registry/x@sha256:short", "bad\n@sha256:" + "a" * 64])
def test_plan_rejects_mutable_or_malformed_images(image):
    with pytest.raises(ValidationError):
        plan(image_references=(image,))


def test_plan_digest_binds_source_environment_native_identity():
    p = plan()
    for key, value in (("source_digest", "f" * 64), ("environment_lock_digest", "e" * 64),
                       ("runtime_environment_digest", "d" * 64), ("native_settings_digest", "b" * 64),
                       ("backend_run_id", UUID(int=8)), ("snapshot_id", UUID(int=9)), ("max_runtime_seconds", 121)):
        assert plan(**{key: value}).plan_digest != p.plan_digest


def test_journal_claim_is_durable_and_cross_connection_atomic(tmp_path):
    path = tmp_path / "journal.sqlite"
    p = plan()
    first = ModalJournal(path)
    first.bind(p)

    def claim(_):
        journal = ModalJournal(path)
        try:
            return journal.claim_once(p)
        finally:
            journal.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(claim, range(8))) == 1
    first.close()
    restarted = ModalJournal(path)
    assert restarted.claim_once(p) is False
    assert restarted.get(p.submit_key)["state"] == "submission_unknown"
    with pytest.raises(ServiceError):
        restarted.bind(plan(source_digest="f" * 64))
    with pytest.raises(ServiceError):
        restarted.bind(plan(submit_key="e" * 64))
    restarted.close()


def test_unknown_native_submit_never_relaunches_and_recovers_exact_id(tmp_path, monkeypatch):
    runtime = ModalRuntime(config())
    journal = ModalJournal(tmp_path / "journal.sqlite")
    provider = ModalProviderAdapter(runtime.config, journal, runtime=runtime)
    p = plan()
    calls = []

    def lost(*args):
        calls.append(args)
        raise TimeoutError("Lost response after a provider side effect")

    monkeypatch.setattr(runtime, "_submit_native", lost)
    monkeypatch.setattr(runtime, "recover", lambda p: None)
    for _ in range(2):
        with pytest.raises(ServiceError) as caught:
            provider._native_transaction(p, None, approval(p))
        assert caught.value.code == "submission_unknown"
    assert len(calls) == 1
    actual = Submission(provider_job_id="sb-controller", backend_run_id=str(RUN))
    monkeypatch.setattr(runtime, "recover", lambda p: actual)
    assert provider._native_transaction(p, None, approval(p)) == actual
    assert len(calls) == 1
    assert journal.get(p.submit_key)["submission"] == actual.model_dump(mode="json")
    monkeypatch.setattr(runtime, "recover", lambda p: Submission(provider_job_id="sb-other", backend_run_id=str(RUN)))
    with pytest.raises(ServiceError) as caught:
        provider.recover(p.submit_key)
    assert caught.value.code == "provider_identity_mismatch"
    journal.close()


def test_read_config_and_workspace_identity_fail_closed(monkeypatch):
    runtime = ModalRuntime(config())
    with pytest.raises(ServiceError) as caught:
        runtime.rates()
    assert caught.value.code == "provider_reads_disabled"
    runtime = ModalRuntime(config(allow_provider_reads=True))
    workspace = SimpleNamespace(name="wrong-workspace", hydrate=lambda **kw: None)
    monkeypatch.setattr(runtime, "_sdk", lambda: SimpleNamespace(Workspace=SimpleNamespace(from_context=lambda **kw: workspace)))
    with pytest.raises(ServiceError) as caught:
        runtime.rates()
    assert caught.value.code == "provider_identity_mismatch"


def test_money_evidence_is_lossless_signed_and_never_fabricated(monkeypatch):
    runtime = ModalRuntime(config())
    @dataclass
    class Summary:
        billed_cost: Decimal
        adjustments: dict
    workspace = SimpleNamespace(billing=SimpleNamespace(
        rates=lambda: {"cpu": Decimal("0.0000000000123456789")},
        summary=lambda **kw: Summary(Decimal("1.2345678901"), {"credits": Decimal("-1.0000000001")})))
    monkeypatch.setattr(runtime, "_workspace", lambda: workspace)
    rates = runtime.rates()
    assert rates["rates"]["cpu"] == "0.0000000000123456789"
    assert rates["all_costs_bounded"] is False
    summary = runtime.billing_summary("2026-09")
    assert summary["summary"]["billed_cost"] == "1.2345678901"
    assert summary["summary"]["adjustments"]["credits"] == "-1.0000000001"
    assert summary["invoice_final"] is False
    for bad in (Decimal("NaN"), Decimal("Infinity"), 1.2):
        with pytest.raises(ValueError):
            serializable(bad)


def test_empty_usage_is_pending_not_zero_and_excludes_unrelated_rows(monkeypatch):
    runtime = ModalRuntime(config())
    p = plan()
    @dataclass
    class Row:
        object_id: str
        environment_name: str
        cost: Decimal
    rows = []
    calls = []
    def report(**kw):
        calls.append(kw)
        return rows
    monkeypatch.setattr(runtime, "_workspace", lambda: SimpleNamespace(billing=SimpleNamespace(report=report)))
    monkeypatch.setattr(runtime, "inspect", lambda p: {"verified_native_sandbox_ids": ["sb-owned"], "provider_app_id": "ap-owned"})
    result = runtime.usage(p, start=NOW, end=NOW.replace(hour=2))
    assert result["billing_state"] == "pending" and result["cumulative_charge"] is None
    assert result["invoice_final"] is False
    rows.extend([Row("sb-owned", "staging", Decimal("1.00000001")), Row("sb-unrelated", "staging", Decimal("99")),
                 Row("sb-owned", "other", Decimal("22")), Row("ap-owned", "staging", Decimal("3"))])
    result = runtime.usage(p, start=NOW, end=NOW.replace(hour=2))
    assert len(result["rows"]) == 1 and len(result["app_aggregate_rows"]) == 1
    assert result["rows"][0]["cost"] == "1.00000001"
    assert result["cumulative_charge"] is None
    assert calls[0]["resolution"] == "h"


def test_termination_needs_exact_approval_before_network(monkeypatch):
    runtime = ModalRuntime(config(allow_termination=True))
    monkeypatch.setattr(runtime, "_run", lambda p: pytest.fail("No provider read before authorization"))
    p = plan()
    for changed in ({"plan_digest": "f" * 64}, {"workspace_name": "another"}, {"backend_run_id": UUID(int=99)}):
        with pytest.raises(ServiceError) as caught:
            runtime.terminate(p, termination(p).model_copy(update=changed))
        assert caught.value.code == "provider_termination_disabled"


def test_termination_stops_controller_first_and_refuses_unknown_children(monkeypatch):
    runtime = ModalRuntime(config(allow_termination=True))
    p = plan()
    run = SimpleNamespace(run_metadata={"modal_orchestration_sandbox_id": "sb-controller"}, steps={})
    calls = []
    sandbox = SimpleNamespace(object_id="sb-controller", poll=lambda: None, terminate=lambda: calls.append("controller"))
    sdk = SimpleNamespace(Sandbox=SimpleNamespace(from_id=lambda *a, **kw: sandbox,
        list=lambda **kw: [sandbox, SimpleNamespace(object_id="sb-unattributed")]))
    monkeypatch.setattr(runtime, "_run", lambda p: run)
    monkeypatch.setattr(runtime, "_app", lambda p: SimpleNamespace(app_id="ap-owned"))
    monkeypatch.setattr(runtime, "_sdk", lambda: sdk)
    with pytest.raises(ServiceError) as caught:
        runtime.terminate(p, termination(p))
    assert caught.value.code == "cleanup_unknown"
    assert calls == ["controller"]


@pytest.mark.parametrize("foreign_role", ["controller", "child"])
def test_wrong_app_native_metadata_never_authorizes_termination(monkeypatch, foreign_role):
    """Regression: even a completed from_id result cannot prove app membership."""
    runtime = ModalRuntime(config(allow_termination=True))
    p = plan()
    calls = []
    controller = "sb-unrelated" if foreign_role == "controller" else "sb-controller"
    run = SimpleNamespace(run_metadata={"modal_orchestration_sandbox_id": controller}, steps={})
    if foreign_role == "child":
        run.run_metadata["modal_step_sandbox_id_train"] = "sb-unrelated"
    owned = SimpleNamespace(object_id="sb-controller", poll=lambda: None, terminate=lambda: calls.append("owned"))
    unrelated = SimpleNamespace(object_id="sb-unrelated", poll=lambda: 0, terminate=lambda: calls.append("UNRELATED"))
    listings = []
    def list_owned(**kwargs):
        listings.append(kwargs)
        return [] if foreign_role == "controller" else [owned]
    def from_id(*args, **kwargs):
        calls.append("unscoped-lookup")
        return unrelated
    sdk = SimpleNamespace(Sandbox=SimpleNamespace(from_id=from_id, list=list_owned))
    monkeypatch.setattr(runtime, "_run", lambda p: run)
    monkeypatch.setattr(runtime, "_app", lambda p: SimpleNamespace(app_id="ap-approved"))
    monkeypatch.setattr(runtime, "_sdk", lambda: sdk)
    with pytest.raises(ServiceError) as caught:
        runtime.terminate(p, termination(p))
    assert caught.value.code == "provider_ownership_unverified"
    assert calls == []
    assert listings == [{"app_id": "ap-approved", "client": None}]


def test_owned_cancellation_keeps_positive_membership_after_active_list_disappearance(monkeypatch):
    runtime = ModalRuntime(config(allow_termination=True))
    p = plan()
    calls = []
    class Sandbox:
        def __init__(self, identifier):
            self.object_id, self.code = identifier, None
        def poll(self):
            return self.code
        def terminate(self):
            calls.append(self.object_id)
            self.code = 137
    members = [Sandbox("sb-controller"), Sandbox("sb-child")]
    run = SimpleNamespace(run_metadata={"modal_orchestration_sandbox_id": "sb-controller",
                                        "modal_step_sandbox_id_train": "sb-child"}, steps={})
    sdk = SimpleNamespace(Sandbox=SimpleNamespace(
        from_id=lambda *a, **kw: pytest.fail("An unscoped ID lookup is not ownership proof"),
        list=lambda **kw: [member for member in members if member.code is None]))
    monkeypatch.setattr(runtime, "_run", lambda p: run)
    monkeypatch.setattr(runtime, "_app", lambda p: SimpleNamespace(app_id="ap-approved"))
    monkeypatch.setattr(runtime, "_sdk", lambda: sdk)
    result = runtime.terminate(p, termination(p))
    assert calls == ["sb-controller", "sb-child"]
    assert result["idle_resources_stopped"] is True
    assert result["verified_native_sandbox_ids"] == ["sb-child", "sb-controller"]
    assert result["cleanup_state"] == "unknown" and result["storage_resources_deleted"] is False
    # A new observation lacks that call-local positive ownership evidence.
    later = runtime.inspect(p)
    assert later["execution_state"] == "unknown" and later["idle_resources_stopped"] is False


def test_foreign_metadata_cannot_be_recovered_polled_or_attributed_to_billing(monkeypatch):
    runtime = ModalRuntime(config())
    p = plan()
    run = SimpleNamespace(run_metadata={"modal_orchestration_sandbox_id": "sb-foreign"}, steps={})
    sdk = SimpleNamespace(Sandbox=SimpleNamespace(
        from_id=lambda *a, **kw: pytest.fail("Foreign metadata ID must never be queried"), list=lambda **kw: []))
    monkeypatch.setattr(runtime, "_run", lambda p: run)
    monkeypatch.setattr(runtime, "_app", lambda p: SimpleNamespace(app_id="ap-approved"))
    monkeypatch.setattr(runtime, "_sdk", lambda: sdk)
    assert runtime.recover(p) is None
    status = runtime.inspect(p)
    assert status["provider_job_id"] is None and status["execution_state"] == "unknown"
    assert status["verified_native_sandbox_ids"] == []
    assert status["unverified_native_sandbox_ids"] == ["sb-foreign"]
    assert status["sandbox_return_codes"] == {}
    @dataclass
    class Row:
        object_id: str
        environment_name: str
        cost: Decimal
    workspace = SimpleNamespace(billing=SimpleNamespace(report=lambda **kw: [Row("sb-foreign", "staging", Decimal("99"))]))
    monkeypatch.setattr(runtime, "_workspace", lambda: workspace)
    evidence = runtime.usage(p, start=NOW, end=NOW.replace(hour=2))
    assert evidence["rows"] == [] and evidence["cumulative_charge"] is None


def test_no_invoice_or_storage_receipt_is_forged(tmp_path):
    journal = ModalJournal(tmp_path / "journal.sqlite")
    provider = ModalProviderAdapter(config(), journal)
    p = plan()
    journal.bind(p)
    with pytest.raises(ServiceError) as caught:
        provider.observe({"submit_key": p.submit_key, "binding": BINDING.model_dump(mode="json"), "project_id": "p"})
    assert caught.value.code == "provider_billing_attribution_unknown"
    with pytest.raises(ServiceError) as caught:
        provider.cleanup(Submission(provider_job_id="sb-owned", backend_run_id=str(RUN)), "cleanup")
    assert caught.value.code == "provider_storage_cleanup_unverified"
    journal.close()


def test_image_source_proof_is_a_preflight_blocker_before_provider_calls(tmp_path, monkeypatch):
    runtime = ModalRuntime(config(allow_provider_reads=True, allow_native_submission=True))
    monkeypatch.setattr(runtime, "_native_submission_bridge", lambda *a: pytest.fail("Image attestation gate bypass"))
    p = plan(local_build_inputs_digest="d" * 64)
    with pytest.raises(ServiceError) as caught:
        runtime._submit_native(p, None, approval(p))
    assert caught.value.code == "image_source_linkage_unverified"
    journal = ModalJournal(tmp_path / "journal.sqlite")
    provider = ModalProviderAdapter(runtime.config, journal, runtime=runtime)
    assert provider.preflight(p)["admissible"] is False
    assert provider.preflight(p)["image_source_linkage"] == "unverified"
    journal.close()


def test_local_build_inputs_reuse_canonical_source_and_lock_verification(tmp_path):
    from modelbuilder import recipe as recipes
    from modelbuilder.backend.admission import BackendError
    from modelbuilder.remote.modal_build_inputs import verify_local_build_inputs
    from modelbuilder.source_workspace import SourceWorkspaces
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "model.py").write_text("raise AssertionError('Source must never be imported during preflight')\n")
    (root / "requirements.lock").write_text("pytest==8.3.5\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-qm", "Pinned build inputs"], check=True)
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    p = {"project_id": str(UUID(int=10)), "recipe_id": str(UUID(int=11)), "revision": 1}
    source = SourceWorkspaces({"source": {"path": str(root), "trusted": True, "project_ids": [p["project_id"]]}})
    p["source_binding"] = source.bind("source", p["project_id"], "owner", True, source.inspect("source", p["project_id"])["repository_id"])
    recipe = recipes.default_recipe(p["project_id"], p["recipe_id"])
    recipe["source"] = {"repository": "https://example.invalid/fixture.git", "commit": commit,
                        "files": [{"path": "model.py", "sha256": hashlib.sha256((root / "model.py").read_bytes()).hexdigest()}]}
    recipe["environment"] = {"python": platform.python_version(), "requirements": ["pytest==8.3.5"],
                             "lock_file": {"path": "requirements.lock", "sha256": hashlib.sha256((root / "requirements.lock").read_bytes()).hexdigest()}}
    recipe["digest"] = recipes.recipe_digest(recipe)
    backend = SimpleNamespace(repository=root, python=sys.executable)
    verified = verify_local_build_inputs(workspaces=source, project=p, recipe=recipe, backend=backend)
    assert verified.recipe_digest == recipe["digest"]
    assert verified.environment_lock_digest == recipe["environment"]["lock_file"]["sha256"]
    assert not hasattr(verified, "image_source_linkage_verified")
    (root / "requirements.lock").write_text("pytest==0.0.1\n")
    with pytest.raises(BackendError):
        verify_local_build_inputs(workspaces=source, project=p, recipe=recipe, backend=backend)
    (root / "model.py").write_text("changed source")
    with pytest.raises(ServiceError) as caught:
        verify_local_build_inputs(workspaces=source, project=p, recipe=recipe, backend=backend)
    assert caught.value.code == "source_unverified"
