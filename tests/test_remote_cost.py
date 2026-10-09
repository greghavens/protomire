"""LOCAL state-machine/accounting contracts. These are NOT remote acceptance tests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
from threading import Barrier

import pytest
from pydantic import ValidationError

from modelbuilder.models import Actor, ServiceError
from modelbuilder.remote import AccountBinding, BudgetPolicy, CostLedger, LocalCostSimulator, Money, RemoteCostService, SpendConsent
from modelbuilder.remote.contracts import ProviderObservation, Quote
from modelbuilder.remote.ledger import digest
from modelbuilder.remote.provider import diagnostics

OWNER = Actor(id="owner", scopes=frozenset({"read", "modify", "execute", "spend"}))
ACCOUNT = "acct_" + "a" * 32
BINDING = AccountBinding(provider_id="local-simulator", account_ref=ACCOUNT,
                         zenml_stack_id="simulation-stack", zenml_orchestrator_id="simulation-orchestrator",
                         target_id="simulation-cpu")
RECIPE = "a" * 64


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 9, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def usd(amount):
    return Money(currency="USD", minor_units=amount)


def policies(ledger, amount=100, project="p", account_amount=None):
    for scope in ("account", "project"):
        ledger.put_policy(OWNER, BudgetPolicy(policy_id=scope + "-" + project, scope=scope,
                          project_id=project if scope == "project" else None,
                          provider_id=BINDING.provider_id, account_ref=ACCOUNT,
                          maximum_total=usd(account_amount if scope == "account" and account_amount is not None else amount),
                          max_runtime_seconds=600), expected_revision=None, request_id="policy-" + scope + "-" + project)


@pytest.fixture
def system(tmp_path):
    clock = Clock()
    ledger = CostLedger(tmp_path / "cost.sqlite", clock=clock)
    policies(ledger)
    simulator = LocalCostSimulator(BINDING, usd(30), clock=clock)
    service = RemoteCostService(ledger, simulator=simulator)
    yield ledger, service, simulator, clock
    ledger.close()


def quote(service, key="quote", runtime=120, actor=OWNER, project="p"):
    result = service.quote_simulated(actor, project, RECIPE, runtime, key)
    return SpendConsent(execute_and_spend=True, quote_id=result["quote"]["quote_id"], quote_digest=result["quote_digest"],
                        maximum_total=Money.model_validate(result["quote"]["maximum_total"]), max_runtime_seconds=runtime)


def reserve(service, key="reserve", runtime=120, actor=OWNER, project="p"):
    return service.reserve_simulated(actor, project, quote(service, key="q-" + key, runtime=runtime, actor=actor, project=project), key)


def balance(ledger):
    result = ledger.balances(OWNER, "p", BINDING.provider_id, ACCOUNT)[0]
    return {key: int(value) if key.endswith("_minor_units") else value for key, value in result.items()}


def assert_error(code, call):
    with pytest.raises(ServiceError) as caught:
        call()
    assert caught.value.code == code


@pytest.mark.parametrize("bad", [1.2, "12", True, -1, 9_007_199_254_740_992])
def test_money_rejects_inexact_or_unsupported_amounts(bad):
    with pytest.raises(ValidationError):
        Money(currency="USD", minor_units=bad)


@pytest.mark.parametrize("bad", [1, "true", False, None])
def test_consent_is_explicit_boolean(bad):
    with pytest.raises(ValidationError):
        SpendConsent(execute_and_spend=bad, quote_id="q", quote_digest=RECIPE,
                     maximum_total=usd(30), max_runtime_seconds=120)


def test_quote_durable_attributed_exact_and_immutable(system):
    ledger, service, _, clock = system
    consent = quote(service)
    result = ledger.get_quote(OWNER, "p", consent.quote_id)
    parsed = Quote.model_validate(result["quote"])
    assert parsed.evidence_kind == "simulation"
    assert parsed.rate_source.startswith("simulation://")
    assert parsed.rate_observed_at == clock()
    assert parsed.binding.account_ref == ACCOUNT
    assert sum(c.upper_bound_minor for c in parsed.components) == 30
    assert result["quote_digest"] == digest(result["quote"])
    assert result["spending_enforced"] is False
    corrupted = parsed.model_copy(update={"rate_source": "changed"})
    assert_error("conflict", lambda: ledger._record_quote(OWNER, corrupted, "different-request"))


def test_quote_requires_all_total_cost_categories(system):
    _, service, _, _ = system
    consent = quote(service)
    payload = service.ledger.get_quote(OWNER, "p", consent.quote_id)["quote"]
    payload["components"] = payload["components"][:1]
    with pytest.raises(ValidationError):
        Quote.model_validate(payload)


def test_cap_exhaustion_atomic_and_duplicate_request(system):
    ledger, service, simulator, _ = system
    consent = quote(service)
    first = service.reserve_simulated(OWNER, "p", consent, "one")
    assert service.reserve_simulated(OWNER, "p", consent, "one") == first
    reserve(service, "two")
    reserve(service, "three")
    assert balance(ledger)["exposure_minor_units"] == 90
    assert_error("budget_exceeded", lambda: reserve(service, "four"))
    assert simulator.submit_calls == 0
    assert len(ledger.list_reservations(OWNER, "p")) == 3
    assert len([e for e in ledger.history(OWNER, "p") if e["operation"] == "cost.reserve"]) == 3


def test_duplicate_request_cannot_change_consent(system):
    _, service, _, _ = system
    consent = quote(service)
    service.reserve_simulated(OWNER, "p", consent, "one")
    changed = consent.model_copy(update={"maximum_total": usd(31)})
    assert_error("idempotency_conflict", lambda: service.reserve_simulated(OWNER, "p", changed, "one"))


def test_concurrent_connections_cannot_overbook(system):
    ledger, service, _, clock = system
    consent = quote(service)
    path = ledger.store.path
    barrier = Barrier(12)

    def submit(i):
        other = CostLedger(path, clock=clock)
        other_service = RemoteCostService(other, simulator=LocalCostSimulator(BINDING, usd(30), clock=clock))
        try:
            barrier.wait(timeout=20)
            try:
                return other_service.reserve_simulated(OWNER, "p", consent, "parallel-" + str(i))["reservation_id"]
            except ServiceError as error:
                return error.code
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(submit, range(12)))
    assert results.count("budget_exceeded") == 9
    assert len(set(r for r in results if r != "budget_exceeded")) == 3
    assert balance(ledger)["exposure_minor_units"] == 90


def _process_reserve(args):
    path, consent_json, index = args
    clock = Clock()
    ledger = CostLedger(path, clock=clock)
    service = RemoteCostService(ledger, simulator=LocalCostSimulator(BINDING, usd(30), clock=clock))
    try:
        return service.reserve_simulated(OWNER, "p", SpendConsent.model_validate_json(consent_json), "process-" + str(index))["reservation_id"]
    except ServiceError as error:
        return error.code
    finally:
        ledger.close()


def test_separate_processes_cannot_overbook(system):
    ledger, service, _, _ = system
    consent = quote(service)
    with get_context("spawn").Pool(6) as pool:
        results = pool.map(_process_reserve, [(ledger.store.path, consent.model_dump_json(), i) for i in range(12)])
    assert results.count("budget_exceeded") == 9
    assert balance(ledger)["exposure_minor_units"] == 90


def test_stale_quotes_rejected_before_reservation_or_launch(system):
    ledger, service, simulator, clock = system
    simulator.quote_lifetime_seconds = 10
    consent = quote(service)
    reservation = service.reserve_simulated(OWNER, "p", consent, "one")
    clock.advance(10)
    assert_error("stale_quote", lambda: service.reserve_simulated(OWNER, "p", consent, "two"))
    assert_error("stale_quote", lambda: service.launch_simulated(OWNER, "p", reservation["reservation_id"]))
    assert simulator.submit_calls == 0
    # An already accepted idempotent request still returns its original receipt.
    assert service.reserve_simulated(OWNER, "p", consent, "one") == reservation
    assert balance(ledger)["held_minor_units"] == 30


@pytest.mark.parametrize("scopes", [{"read", "execute"}, {"read", "spend"}, {"read"}, {"modify"}])
def test_execute_and_spend_both_required(system, scopes):
    _, service, simulator, _ = system
    consent = quote(service)
    actor = Actor(id="limited", scopes=frozenset(scopes))
    assert_error("forbidden", lambda: service.reserve_simulated(actor, "p", consent, "one"))
    assert simulator.submit_calls == 0


def test_policy_requires_spend_scope_and_project_identity(system):
    ledger, service, _, _ = system
    actor = Actor(id="limited", scopes=frozenset({"read", "modify", "spend"}), project_ids=frozenset({"p"}))
    account_policy = BudgetPolicy.model_validate(balance(ledger)["policy"])
    assert_error("forbidden", lambda: ledger.put_policy(actor, account_policy, expected_revision=1, request_id="bad"))
    consent = quote(service)
    assert_error("forbidden", lambda: service.reserve_simulated(Actor(id="other", scopes=OWNER.scopes,
                                                                   project_ids=frozenset({"other"})), "p", consent, "bad"))
    # A project-scoped reader does not receive account-wide balances or history.
    visible = ledger.balances(actor, "p", BINDING.provider_id, ACCOUNT)
    assert len(visible) == 1 and visible[0]["policy"]["scope"] == "project"


def test_account_cap_shared_across_projects(system):
    ledger, service, _, _ = system
    ledger.put_policy(OWNER, BudgetPolicy(policy_id="project-q", scope="project", project_id="q", provider_id=BINDING.provider_id,
                                         account_ref=ACCOUNT, maximum_total=usd(100), max_runtime_seconds=600),
                      expected_revision=None, request_id="policy-q")
    reserve(service, "p-one")
    reserve(service, "q-one", project="q")
    reserve(service, "q-two", project="q")
    assert_error("budget_exceeded", lambda: reserve(service, "p-two"))


def test_cancel_never_started_releases_but_submitted_does_not(system):
    ledger, service, simulator, _ = system
    first = reserve(service, "one")
    cancelled = ledger.cancel(OWNER, "p", first["reservation_id"], "cancel-one")
    assert cancelled["cleanup_state"] == "confirmed"
    assert balance(ledger)["exposure_minor_units"] == 0
    second = reserve(service, "two")
    service.launch_simulated(OWNER, "p", second["reservation_id"])
    cancelled = ledger.cancel(OWNER, "p", second["reservation_id"], "cancel-two")
    assert cancelled["execution_state"] == "stop_requested"
    assert cancelled["cleanup_state"] == "required"
    assert balance(ledger)["held_minor_units"] == 30
    assert simulator.stop_calls == 0


def test_cleanup_failure_and_provisional_billing_retain_hold(system):
    ledger, service, simulator, _ = system
    reservation = reserve(service)
    service.launch_simulated(OWNER, "p", reservation["reservation_id"])
    ledger.cancel(OWNER, "p", reservation["reservation_id"], "cancel")
    simulator.cleanup_available = False
    result = service.tick_simulated()
    assert result[0]["status"] == "cleanup_unknown"
    assert balance(ledger)["held_minor_units"] == 30
    simulator.cleanup_available = True
    result = service.tick_simulated()
    assert result[0]["cleanup_state"] == "confirmed"
    assert result[0]["billing_state"] == "provisional"
    assert balance(ledger)["held_minor_units"] == 30
    simulator.set_cumulative_charge(reservation["submit_key"], 7, final=True)
    settled = service.reconcile_simulated(OWNER, "p", reservation["reservation_id"])
    assert settled["billing_state"] == "final"
    assert balance(ledger)["actual_minor_units"] == 7
    assert balance(ledger)["held_minor_units"] == 0


def test_delayed_charges_reopen_debt_and_block_new_admission(system):
    ledger, service, simulator, _ = system
    reservation = reserve(service)
    service.launch_simulated(OWNER, "p", reservation["reservation_id"])
    ledger.cancel(OWNER, "p", reservation["reservation_id"], "cancel")
    simulator.set_cumulative_charge(reservation["submit_key"], 5, final=True)
    service.tick_simulated()
    assert balance(ledger)["exposure_minor_units"] == 5
    simulator.set_cumulative_charge(reservation["submit_key"], 110, final=True)
    service.reconcile_simulated(OWNER, "p", reservation["reservation_id"])
    assert balance(ledger)["debt_minor_units"] == 10
    assert balance(ledger)["authorization_overrun_minor_units"] == 80
    assert_error("budget_exceeded", lambda: reserve(service, "late"))
    assert [e for e in ledger.history(OWNER, "p") if e["operation"] == "cost.reconcile"][-1]["details"]["late_charge_above_reservation"] is True


def test_authorization_overrun_blocks_even_with_remaining_account_budget(system):
    ledger, service, simulator, _ = system
    reservation = reserve(service)
    service.launch_simulated(OWNER, "p", reservation["reservation_id"])
    ledger.cancel(OWNER, "p", reservation["reservation_id"], "cancel")
    simulator.set_cumulative_charge(reservation["submit_key"], 31, final=True)
    service.tick_simulated()
    assert balance(ledger)["debt_minor_units"] == 0
    assert balance(ledger)["authorization_overrun_minor_units"] == 1
    assert_error("budget_exceeded", lambda: reserve(service, "after-overrun"))


def test_lost_worker_and_restart_preserve_hold_and_stop_intent(system):
    ledger, service, simulator, clock = system
    reservation = reserve(service)
    service.launch_simulated(OWNER, "p", reservation["reservation_id"])
    path = ledger.store.path
    clock.advance(61)
    fresh = CostLedger(path, clock=clock)
    try:
        restarted = RemoteCostService(fresh, simulator=simulator)
        due = fresh.recover_due()
        assert due[0]["stop_reason"] == "worker_lease_lost"
        assert balance(fresh)["held_minor_units"] == 30
        assert_error("invalid_transition", lambda: fresh._heartbeat(reservation["reservation_id"]))
        results = restarted.tick_simulated()
        assert results[0]["execution_state"] == "stopped"
        assert results[0]["cleanup_state"] == "confirmed"
        assert results[0]["billing_state"] == "provisional"
        assert balance(fresh)["held_minor_units"] == 30
    finally:
        fresh.close()


def test_runtime_deadline_cannot_be_extended_by_heartbeats(system):
    ledger, service, simulator, clock = system
    reservation = reserve(service, runtime=70)
    service.launch_simulated(OWNER, "p", reservation["reservation_id"])
    clock.advance(50)
    ledger._heartbeat(reservation["reservation_id"])
    clock.advance(20)
    due = ledger.recover_due()
    assert due[0]["stop_reason"] == "maximum_runtime"
    service.tick_simulated()
    assert simulator.stop_calls == 1
    assert balance(ledger)["held_minor_units"] == 30


def test_ambiguous_submit_recovers_same_job_without_resubmit(system):
    ledger, service, simulator, _ = system
    reservation = reserve(service)
    simulator.fail_after_submission = True
    assert_error("submission_unknown", lambda: service.launch_simulated(OWNER, "p", reservation["reservation_id"]))
    assert balance(ledger)["held_minor_units"] == 30
    recovered = service.launch_simulated(OWNER, "p", reservation["reservation_id"])
    assert recovered["provider_job_id"].startswith("simulation-job-")
    assert simulator.submit_calls == 1
    assert service.launch_simulated(OWNER, "p", reservation["reservation_id"]) == recovered
    assert simulator.submit_calls == 1


def test_unknown_recovery_never_releases_or_resubmits(system):
    ledger, service, simulator, clock = system
    reservation = reserve(service)
    ledger._claim_submission(reservation["reservation_id"])
    clock.advance(61)
    results = service.tick_simulated()
    assert results[0]["hold_retained"] is True
    assert balance(ledger)["held_minor_units"] == 30
    assert simulator.submit_calls == 0


def test_observation_order_idempotence_and_mixed_evidence(system):
    ledger, service, simulator, _ = system
    reservation = reserve(service)
    launched = service.launch_simulated(OWNER, "p", reservation["reservation_id"])
    observation = simulator.observe(launched)
    first = ledger._apply_observation(observation)
    assert ledger._apply_observation(observation) == first
    conflict = observation.model_copy(update={"cumulative_charge": usd(1)})
    assert_error("provider_event_conflict", lambda: ledger._apply_observation(conflict))
    fake_invoice = observation.model_copy(update={"sequence": 2, "evidence_kind": "provider_invoice"})
    assert_error("evidence_mismatch", lambda: ledger._apply_observation(fake_invoice))
    second = simulator.observe(launched)
    ledger._apply_observation(second)
    assert_error("stale_observation", lambda: ledger._apply_observation(observation))


def test_cleanup_observation_requires_compute_and_storage_evidence(system):
    _, service, simulator, _ = system
    reservation = reserve(service)
    launched = service.launch_simulated(OWNER, "p", reservation["reservation_id"])
    payload = simulator.observe(launched).model_dump(mode="json")
    payload.update(execution_state="stopped", cleanup_state="confirmed", idle_resources_stopped=True,
                   storage_resources_deleted=False)
    with pytest.raises(ValidationError):
        ProviderObservation.model_validate(payload)


def test_final_usage_cannot_masquerade_as_provider_invoice(system):
    _, service, simulator, _ = system
    reservation = reserve(service)
    launched = service.launch_simulated(OWNER, "p", reservation["reservation_id"])
    payload = simulator.observe(launched).model_dump(mode="json")
    payload.update(evidence_kind="provider_usage", billing_state="final")
    with pytest.raises(ValidationError):
        ProviderObservation.model_validate(payload)


def test_policy_revisions_cannot_reset_spend_history(system):
    ledger, service, _, _ = system
    reserve(service)
    original = BudgetPolicy.model_validate(balance(ledger)["policy"])
    update = original.model_copy(update={"maximum_total": usd(20), "revision": 2})
    ledger.put_policy(OWNER, update, expected_revision=1, request_id="lower-cap")
    assert balance(ledger)["debt_minor_units"] == 10
    assert_error("budget_exceeded", lambda: reserve(service, "next"))
    assert_error("conflict", lambda: ledger.put_policy(OWNER, original, expected_revision=1, request_id="stale-policy"))


def test_production_dispatch_fail_closed_even_for_spend_owner(system):
    ledger, service, simulator, _ = system
    consent = quote(service)
    assert_error("remote_disabled", lambda: service.submit_remote(OWNER, "p", consent, "launch-production"))
    assert simulator.submit_calls == 0
    assert ledger.list_reservations(OWNER, "p") == []
    report = service.providers(OWNER, "p")
    assert report["remote_acceptance"] == "BLOCKED"
    assert report["remote_execution_enabled"] is False
    assert report["verified_remote_capabilities"] == []
    assert report["spending_enforced"] is False
    assert diagnostics()["configured"] is False


def test_arbitrary_adapter_cannot_be_registered(system):
    ledger, _, _, _ = system
    class FakeProvider:
        simulation = True
    with pytest.raises(ValueError):
        RemoteCostService(ledger, simulator=FakeProvider())


def test_unconfigured_service_does_not_generate_rates(system):
    ledger, _, _, _ = system
    service = RemoteCostService(ledger)
    assert_error("simulation_disabled", lambda: quote(service))


def test_quote_currency_cannot_bypass_policy_units(system):
    ledger, service, simulator, _ = system
    simulator.synthetic_maximum_total = Money(currency="EUR", minor_units=30)
    consent = quote(service)
    assert_error("currency_mismatch", lambda: service.reserve_simulated(OWNER, "p", consent, "wrong-currency"))
    assert ledger.list_reservations(OWNER, "p") == []


def test_policy_disabled_after_reservation_blocks_launch(system):
    ledger, service, simulator, _ = system
    reservation = reserve(service)
    policy = BudgetPolicy.model_validate(balance(ledger)["policy"])
    disabled = policy.model_copy(update={"enabled": False, "revision": 2})
    ledger.put_policy(OWNER, disabled, expected_revision=1, request_id="disable")
    assert_error("policy_disabled", lambda: service.launch_simulated(OWNER, "p", reservation["reservation_id"]))
    assert simulator.submit_calls == 0
    assert balance(ledger)["held_minor_units"] == 30


def test_never_started_expiration_releases_exactly_once(system):
    ledger, service, simulator, clock = system
    reservation = reserve(service, runtime=10)
    clock.advance(10)
    assert ledger.recover_due() == []
    assert ledger.recover_due() == []
    result = ledger.get_reservation(OWNER, "p", reservation["reservation_id"])
    assert result["stop_reason"] == "expired_before_submission"
    assert balance(ledger)["held_minor_units"] == 0
    assert simulator.submit_calls == 0
    assert len([e for e in ledger.history(OWNER, "p") if e["operation"] == "cost.reservation.expire"]) == 1


def test_completed_job_with_unresolved_cleanup_is_retried(system):
    ledger, service, simulator, _ = system
    reservation = reserve(service)
    service.launch_simulated(OWNER, "p", reservation["reservation_id"])
    simulator.jobs[reservation["submit_key"]]["execution_state"] = "succeeded"
    service.reconcile_simulated(OWNER, "p", reservation["reservation_id"])
    result = service.tick_simulated()
    assert result[0]["cleanup_state"] == "confirmed"
    assert balance(ledger)["held_minor_units"] == 30


def test_reopened_cleanup_or_billing_restores_hold_and_freezes_admission(system):
    ledger, service, simulator, _ = system
    reservation = reserve(service)
    service.launch_simulated(OWNER, "p", reservation["reservation_id"])
    ledger.cancel(OWNER, "p", reservation["reservation_id"], "cancel")
    simulator.set_cumulative_charge(reservation["submit_key"], 5, final=True)
    service.tick_simulated()
    assert balance(ledger)["held_minor_units"] == 0
    job = simulator.jobs[reservation["submit_key"]]
    job.update(cleanup_state="unknown", billing_final=False)
    reconciled = service.reconcile_simulated(OWNER, "p", reservation["reservation_id"])
    assert set(reconciled["enforcement_alerts"]) == {"billing_reopened", "cleanup_reopened"}
    assert balance(ledger)["held_minor_units"] == 25
    assert balance(ledger)["enforcement_alert_count"] == 1
    assert_error("budget_exceeded", lambda: reserve(service, "after-reopened"))


def test_decreasing_charge_does_not_refund_budget(system):
    ledger, service, simulator, _ = system
    reservation = reserve(service)
    service.launch_simulated(OWNER, "p", reservation["reservation_id"])
    simulator.set_cumulative_charge(reservation["submit_key"], 20)
    service.reconcile_simulated(OWNER, "p", reservation["reservation_id"])
    simulator.set_cumulative_charge(reservation["submit_key"], 1)
    assert_error("charge_regression", lambda: service.reconcile_simulated(OWNER, "p", reservation["reservation_id"]))
    assert balance(ledger)["actual_minor_units"] == 20


def test_wrong_account_or_job_observation_cannot_release_hold(system):
    ledger, service, simulator, _ = system
    reservation = reserve(service)
    launched = service.launch_simulated(OWNER, "p", reservation["reservation_id"])
    observation = simulator.observe(launched)
    for change in ({"account_ref": "acct_" + "b" * 32}, {"provider_job_id": "another-job"}):
        forged = observation.model_copy(update=change)
        assert_error("provider_identity_mismatch", lambda: ledger._apply_observation(forged))
    assert balance(ledger)["held_minor_units"] == 30


def test_quote_snapshot_survives_ledger_restart(system):
    ledger, service, _, clock = system
    consent = quote(service)
    first = service.reserve_simulated(OWNER, "p", consent, "once")
    reopened = CostLedger(ledger.store.path, clock=clock)
    try:
        assert reopened.get_quote(OWNER, "p", consent.quote_id)["quote_digest"] == consent.quote_digest
        assert reopened._reserve(OWNER, "p", consent, "once") == first
        assert balance(reopened)["held_minor_units"] == 30
    finally:
        reopened.close()


def test_quote_request_is_idempotent_across_controller_restart(system):
    ledger, service, _, clock = system
    consent = quote(service)
    original = ledger.get_quote(OWNER, "p", consent.quote_id)
    clock.advance(30)
    restarted = RemoteCostService(ledger, simulator=LocalCostSimulator(BINDING, usd(99), clock=clock))
    same = restarted.quote_simulated(OWNER, "p", RECIPE, 120, "quote")
    assert same == original
    assert_error("idempotency_conflict", lambda: restarted.quote_simulated(OWNER, "p", RECIPE, 121, "quote"))


def test_concurrent_duplicate_reservation_creates_one_hold(system):
    ledger, service, _, clock = system
    consent = quote(service)
    barrier = Barrier(8)

    def submit(_):
        other = CostLedger(ledger.store.path, clock=clock)
        try:
            barrier.wait(timeout=20)
            return other._reserve(OWNER, "p", consent, "same-concurrent-request")
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert len({item["reservation_id"] for item in results}) == 1
    assert balance(ledger)["held_minor_units"] == 30
    assert len(ledger.list_reservations(OWNER, "p")) == 1


@pytest.mark.parametrize("charge", [30, 31])
def test_observed_cap_exhaustion_immediately_requests_stop(system, charge):
    ledger, service, simulator, _ = system
    reservation = reserve(service)
    service.launch_simulated(OWNER, "p", reservation["reservation_id"])
    simulator.set_cumulative_charge(reservation["submit_key"], charge)
    observed = service.reconcile_simulated(OWNER, "p", reservation["reservation_id"])
    assert observed["execution_state"] == "stop_requested"
    assert observed["stop_reason"] == "reservation_total_reached"
    service.tick_simulated()
    assert simulator.stop_calls == 1
    assert balance(ledger)["exposure_minor_units"] == charge


def test_tick_reconciles_final_then_delayed_bill_without_repeating_cleanup(system):
    ledger, service, simulator, _ = system
    reservation = reserve(service)
    service.launch_simulated(OWNER, "p", reservation["reservation_id"])
    ledger.cancel(OWNER, "p", reservation["reservation_id"], "cancel")
    service.tick_simulated()
    assert simulator.cleanup_calls == 1
    assert balance(ledger)["held_minor_units"] == 30
    simulator.set_cumulative_charge(reservation["submit_key"], 5, final=True)
    service.tick_simulated()
    assert balance(ledger)["held_minor_units"] == 0
    assert balance(ledger)["actual_minor_units"] == 5
    simulator.set_cumulative_charge(reservation["submit_key"], 110, final=True)
    service.tick_simulated()
    assert balance(ledger)["debt_minor_units"] == 10
    assert simulator.cleanup_calls == 1


def test_aggregate_late_charges_preserve_exact_decimal_wire_values(system):
    from modelbuilder.remote.contracts import BudgetBalance, MAX_MONEY
    ledger, service, simulator, _ = system
    reservations = [reserve(service, "overflow-" + str(i)) for i in range(3)]
    for reservation in reservations:
        service.launch_simulated(OWNER, "p", reservation["reservation_id"])
    for reservation in reservations:
        simulator.set_cumulative_charge(reservation["submit_key"], MAX_MONEY)
        service.reconcile_simulated(OWNER, "p", reservation["reservation_id"])
    raw = ledger.balances(OWNER, "p", BINDING.provider_id, ACCOUNT)[0]
    BudgetBalance.model_validate(raw)
    assert raw["actual_minor_units"] == str(3 * MAX_MONEY)
    assert raw["exposure_minor_units"] == "27021597764222973"
    assert raw["debt_minor_units"] == str(3 * MAX_MONEY - 100)
    assert isinstance(raw["actual_minor_units"], str)
    assert_error("budget_exceeded", lambda: reserve(service, "after-overflow"))


def test_stale_adapter_binding_does_not_block_other_cleanup(system):
    ledger, service, simulator, clock = system
    old = reserve(service, "old")
    service.launch_simulated(OWNER, "p", old["reservation_id"])
    ledger.cancel(OWNER, "p", old["reservation_id"], "cancel-old")
    changed_binding = BINDING.model_copy(update={"target_id": "new-simulation-target"})
    new_simulator = LocalCostSimulator(changed_binding, usd(30), clock=clock)
    replacement = RemoteCostService(ledger, simulator=new_simulator)
    current = reserve(replacement, "current")
    replacement.launch_simulated(OWNER, "p", current["reservation_id"])
    ledger.cancel(OWNER, "p", current["reservation_id"], "cancel-current")
    outcomes = replacement.tick_simulated()
    assert any(item.get("status") == "cleanup_unknown" and item["reservation_id"] == old["reservation_id"] for item in outcomes)
    assert new_simulator.stop_calls == 1
    assert new_simulator.cleanup_calls == 1
    assert ledger.get_reservation(OWNER, "p", current["reservation_id"])["cleanup_state"] == "confirmed"
    assert ledger.get_reservation(OWNER, "p", old["reservation_id"])["cleanup_state"] == "required"
    assert balance(ledger)["held_minor_units"] == 60
