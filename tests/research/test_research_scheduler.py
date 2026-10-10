"""Deterministic local-provider acceptance. No private queries or live network."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import subprocess
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from modelbuilder.api import create_app
from modelbuilder.auth import TokenAuth
from modelbuilder.models import Actor, OPERATIONS, ServiceError
from modelbuilder.research import _date
from modelbuilder.research_scheduler import MAX_CONCURRENT_FETCHES, ResearchScheduler
from modelbuilder.service import OperationService

OWNER = Actor("fixture-owner", {"read", "modify", "execute"})


def invoke(service, operation, **p):
    actor = p.pop("actor", OWNER)
    if OPERATIONS[operation].mutating:
        p.setdefault("idempotency_key", str(uuid4()))
    return service.invoke(operation, p, actor)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    calls = []
    def provider(query, limit):
        calls.append((query, limit))
        return [{"title": "Fixture paper", "source_identifier": "https://openalex.org/W1",
                 "source_url": "https://openalex.org/W1"}]
    monkeypatch.setattr("modelbuilder.literature.search_openalex", provider)
    service = OperationService(tmp_path / "scheduler.sqlite3")
    auth = TokenAuth({"fixture-token": OWNER})
    service.research_authorizer = auth.authorize_research
    p = invoke(service, "project.create", slug="scheduled-research", model_name="Fixture model")
    w = invoke(service, "research.watch.create", project_id=p["project_id"], name="Public fixture",
               query="fixture calibration", interval_seconds=900)
    yield service, auth, p, w, calls
    service.store.close()


def enable(service, watch, **fields):
    return invoke(service, "research.watch.schedule_enable", project_id=watch["project_id"],
                  watch_id=watch["watch_id"], expected_revision=watch["revision"],
                  **{field: watch[field] for field in ("provider", "query", "limit", "interval_seconds")},
                  confirm_schedule=True, **fields)


def refresh(service, watch, **fields):
    return invoke(service, "research.watch.refresh", project_id=watch["project_id"],
                  watch_id=watch["watch_id"], expected_revision=watch["revision"], **fields)


def make_due(service, watch):
    with service.store.transaction():
        current = service.store.get("research_watch", watch["watch_id"])
        return service.store.put("research_watch", watch["watch_id"], watch["project_id"],
            {**current, "revision": current["revision"] + 1, "next_due_at": "2000-01-01T00:00:00+00:00"}, current["revision"])


def second_service(service, auth):
    other = OperationService(service.store.path)
    other.research_authorizer = auth.authorize_research
    return other


def test_legacy_and_new_watches_stay_manual_until_exact_opt_in(setup):
    service, auth, p, w, calls = setup
    scheduler = ResearchScheduler(service)
    assert scheduler.tick() == [] and calls == []
    with service.store.transaction():
        legacy = {key: value for key, value in w.items() if key != "schedule"}
        service.store.put("research_watch", w["watch_id"], p["project_id"], {**legacy, "revision": 2}, 1)
    assert scheduler.tick() == []
    with pytest.raises(ServiceError) as error:
        invoke(service, "research.watch.schedule_enable", project_id=p["project_id"], watch_id=w["watch_id"],
               expected_revision=2, provider="openalex", query="different query", limit=5,
               interval_seconds=900, confirm_schedule=True)
    assert error.value.code == "schedule_disclosure_changed"
    with pytest.raises(ServiceError):
        invoke(service, "research.watch.schedule_enable", project_id=p["project_id"], watch_id=w["watch_id"],
               expected_revision=2, provider="openalex", query=w["query"], limit=5, interval_seconds=900)
    saved = enable(service, service.store.get("research_watch", w["watch_id"]))
    assert saved["schedule"]["actor_id"] == OWNER.id
    assert saved["schedule"]["disclosure"] == {key: w[key] for key in ("provider", "query", "limit", "interval_seconds")}
    results = scheduler.tick()
    assert len(results) == 1 and len(calls) == 1
    assert results[0]["refresh"]["status"] == "succeeded"
    assert service.store.list("operation", p["project_id"]) == []
    assert service.store.list("research_proposal", p["project_id"]) == []
    assert service.store.get("recipe", p["recipe_id"])["revision"] == 1
    assert scheduler.tick() == []


def test_schedule_durable_restart_due_boundary_and_findings_dedup(setup, monkeypatch):
    service, auth, p, w, calls = setup
    w = enable(service, w)
    first = ResearchScheduler(service).tick()[0]
    other = second_service(service, auth)
    try:
        assert ResearchScheduler(other).tick() == []
        assert other.store.get("research_watch", w["watch_id"])["schedule"]["enabled"]
        now = _date(first["watch"]["next_due_at"])
        monkeypatch.setattr("modelbuilder.store.utcnow", lambda: (now - timedelta(microseconds=1)).isoformat())
        assert ResearchScheduler(other).tick() == []
        monkeypatch.setattr("modelbuilder.store.utcnow", lambda: now.isoformat())
        monkeypatch.setattr("modelbuilder.research_scheduler.utcnow", lambda: now.isoformat())
        result = ResearchScheduler(other).tick()[0]
        assert result["refresh"]["unchanged"] == 1 and len(calls) == 2
        assert len(other.store.list("evidence", p["project_id"])) == 1
    finally:
        other.store.close()


def test_disable_and_config_change_require_fresh_opt_in(setup):
    service, _, p, w, calls = setup
    w = enable(service, w)
    disabled = invoke(service, "research.watch.schedule_disable", project_id=p["project_id"],
                      watch_id=w["watch_id"], expected_revision=w["revision"])
    assert disabled["enabled"] and not disabled["schedule"]["enabled"]
    assert ResearchScheduler(service).tick() == []
    changed = invoke(service, "research.watch.update", project_id=p["project_id"], watch_id=w["watch_id"],
                     expected_revision=disabled["revision"], changes={"query": "new public fixture"})
    assert not changed["schedule"]["enabled"]
    w = enable(service, changed)
    changed = invoke(service, "research.watch.update", project_id=p["project_id"], watch_id=w["watch_id"],
                     expected_revision=w["revision"], changes={"interval_seconds": 1800})
    assert not changed["schedule"]["enabled"]
    assert ResearchScheduler(service).tick() == [] and not calls
    assert refresh(service, changed)["refresh"]["status"] == "succeeded"


@pytest.mark.parametrize("replacement", [None, Actor(OWNER.id, {"read"}), Actor(OWNER.id, {"modify"}, {"another-project"})])
def test_schedule_resolves_current_actor_authority_and_revokes(setup, replacement):
    service, auth, p, w, calls = setup
    enable(service, w)
    auth._tokens = {} if replacement is None else {"replacement": replacement}
    assert ResearchScheduler(service).tick() == [] and calls == []
    current = service.store.get("research_watch", w["watch_id"])
    assert not current["schedule"]["enabled"]
    assert current["schedule"]["reason"] == "authority_revoked_or_project_inactive"
    assert any(row["operation"] == "research.watch.schedule_revoked" for row in service.store.history(p["project_id"]))


def test_authority_changes_are_observed_only_when_watch_is_due(setup, monkeypatch):
    service, auth, p, w, calls = setup
    w = enable(service, w)
    now = _date(w["next_due_at"])
    due = now + timedelta(seconds=w["interval_seconds"])
    for module in ("modelbuilder.store", "modelbuilder.research", "modelbuilder.research_scheduler"):
        monkeypatch.setattr(f"{module}.utcnow", lambda: now.isoformat())
    with service.store.transaction():
        future = service.store.put("research_watch", w["watch_id"], p["project_id"],
            {**w, "revision": w["revision"] + 1, "next_due_at": due.isoformat()}, w["revision"])
    scheduler = ResearchScheduler(service)

    # No due candidate means no request and no persisted revocation.
    auth._tokens.clear()
    assert scheduler.tick() == [] and calls == []
    assert service.store.get("research_watch", w["watch_id"]) == future
    auth._tokens["restored-token"] = OWNER
    assert scheduler.tick() == [] and calls == []
    assert service.store.get("research_watch", w["watch_id"])["schedule"] == w["schedule"]

    # Restoring an unseen grant loss preserves the original opt-in at due time.
    now = due
    result = scheduler.tick()[0]
    assert result["refresh"]["status"] == "succeeded" and len(calls) == 1
    assert result["watch"]["schedule"] == w["schedule"]

    # Once absence is observed on a due watch, restoration alone cannot resume it.
    now = _date(result["watch"]["next_due_at"])
    auth._tokens.clear()
    assert scheduler.tick() == [] and len(calls) == 1
    revoked = service.store.get("research_watch", w["watch_id"])
    assert not revoked["schedule"]["enabled"]
    assert revoked["schedule"]["reason"] == "authority_revoked_or_project_inactive"
    auth._tokens["restored-token"] = OWNER
    assert scheduler.tick() == [] and len(calls) == 1
    assert service.store.get("research_watch", w["watch_id"]) == revoked
    assert sum(row["operation"] == "research.watch.schedule_revoked"
               for row in service.store.history(p["project_id"])) == 1


def test_read_only_wrong_project_and_missing_registry_cannot_schedule(setup):
    service, _, p, w, calls = setup
    for actor in (Actor("reader", {"read"}), Actor("other", {"modify"}, {"other-project"})):
        with pytest.raises(ServiceError) as error:
            enable(service, w, actor=actor)
        assert error.value.code == "forbidden"
    service.research_authorizer = None
    with pytest.raises(ServiceError) as error:
        enable(service, w)
    assert error.value.code == "scheduler_authority_unavailable" and not calls


def test_archived_project_drops_schedule_without_fetch(setup):
    service, _, p, w, calls = setup
    enable(service, w)
    invoke(service, "project.update", project_id=p["project_id"], expected_revision=p["revision"], changes={"status": "archived"})
    assert ResearchScheduler(service).tick() == [] and not calls
    assert not service.store.get("research_watch", w["watch_id"])["schedule"]["enabled"]


@pytest.mark.parametrize("operation", ["watch", "search"])
def test_slow_network_does_not_hold_store_or_sqlite_write_lock(setup, monkeypatch, operation):
    service, auth, p, w, _ = setup
    entered, release = threading.Event(), threading.Event()
    def slow(*args):
        entered.set()
        assert release.wait(5)
        return []
    monkeypatch.setattr("modelbuilder.literature.search_openalex", slow)
    other = second_service(service, auth)
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = (pool.submit(refresh, service, w) if operation == "watch" else
                       pool.submit(invoke, service, "evidence.search", project_id=p["project_id"], provider="openalex", query="public fixture"))
            assert entered.wait(2)
            # Both the shared-connection lock and a distinct SQLite writer must
            # remain free. This models live checkpoint/status record traffic.
            local = pool.submit(invoke, service, "project.get", project_id=p["project_id"])
            remote = pool.submit(invoke, other, "evidence.add", project_id=p["project_id"], title="Live status probe", source_identifier="fixture:status")
            try:
                assert local.result(timeout=1)["project_id"] == p["project_id"]
                assert remote.result(timeout=1)["title"] == "Live status probe"
            finally:
                release.set()
            result = pending.result(timeout=2)
            assert result["refresh"]["status"] == "succeeded" if operation == "watch" else result["persisted"] is False
    finally:
        other.store.close()


@pytest.mark.parametrize("change", ["disable", "query", "revoke", "archive"])
def test_inflight_disable_edit_revocation_or_archive_discards_response(setup, monkeypatch, change):
    service, auth, p, w, _ = setup
    w = enable(service, w)
    entered, release = threading.Event(), threading.Event()
    def slow(*args):
        entered.set()
        assert release.wait(5)
        return [{"title": "Must not commit", "source_url": "https://openalex.org/W9"}]
    monkeypatch.setattr("modelbuilder.literature.search_openalex", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(ResearchScheduler(service).tick)
        assert entered.wait(2)
        try:
            if change == "disable":
                invoke(service, "research.watch.schedule_disable", project_id=p["project_id"], watch_id=w["watch_id"], expected_revision=w["revision"])
            elif change == "query":
                invoke(service, "research.watch.update", project_id=p["project_id"], watch_id=w["watch_id"], expected_revision=w["revision"], changes={"query": "new fixture"})
            elif change == "revoke":
                auth._tokens.clear()
            else:
                invoke(service, "project.update", project_id=p["project_id"], expected_revision=p["revision"], changes={"status": "archived"})
        finally:
            release.set()
        assert pending.result(timeout=2)[0]["refresh"]["status"] == "discarded"
    assert service.store.list("evidence", p["project_id"]) == []
    assert service.store.list("research_finding", p["project_id"]) == []


def test_multiworker_and_manual_share_one_watch_lease(setup, monkeypatch):
    service, auth, p, w, _ = setup
    w = enable(service, w)
    entered, release, calls = threading.Event(), threading.Event(), []
    def slow(*args):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return []
    monkeypatch.setattr("modelbuilder.literature.search_openalex", slow)
    other = second_service(service, auth)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(ResearchScheduler(service).tick)
            assert entered.wait(2)
            try:
                assert ResearchScheduler(other).tick() == []
                with pytest.raises(ServiceError) as error:
                    refresh(other, w, idempotency_key="manual-overlap")
                assert error.value.code == "watch_busy"
            finally:
                release.set()
            pending.result(timeout=2)
        assert len(calls) == 1
        # A busy retry returns the intervening revision as discarded rather
        # than issuing another network call with old intent.
        retried = refresh(other, w, idempotency_key="manual-overlap")
        assert retried["refresh"]["status"] == "discarded" and len(calls) == 1
    finally:
        other.store.close()


def test_expired_crash_lease_recovers_and_late_owner_cannot_commit(setup, monkeypatch):
    service, auth, p, w, _ = setup
    w = enable(service, w)
    entered, release, calls = threading.Event(), threading.Event(), []
    def provider(*args):
        calls.append(args)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            title = "Late response"
        else:
            title = "Current response"
        return [{"title": title, "source_url": "https://openalex.org/W1"}]
    monkeypatch.setattr("modelbuilder.literature.search_openalex", provider)
    other = second_service(service, auth)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(ResearchScheduler(service).tick)
            assert entered.wait(2)
            try:
                with other.store.transaction():
                    other.store.connection.execute("UPDATE research_leases SET expires_at='2000-01-01T00:00:00+00:00'")
                fresh = ResearchScheduler(other).tick()
                assert fresh[0]["refresh"]["status"] == "succeeded"
            finally:
                release.set()
            assert pending.result(timeout=2)[0]["refresh"]["error"]["code"] == "lease_lost"
        assert [e["title"] for e in service.store.list("evidence", p["project_id"])] == ["Current response"]
    finally:
        other.store.close()


def test_shared_global_admission_limits_simultaneous_provider_requests(setup, monkeypatch):
    service, auth, p, w, _ = setup
    watches = [w] + [invoke(service, "research.watch.create", project_id=p["project_id"], name=str(i), query="fixture") for i in range(2)]
    entered, release, calls = threading.Event(), threading.Event(), []
    def slow(*args):
        calls.append(args)
        if len(calls) == MAX_CONCURRENT_FETCHES:
            entered.set()
        assert release.wait(5)
        return []
    monkeypatch.setattr("modelbuilder.literature.search_openalex", slow)
    other = second_service(service, auth)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(refresh, service, watch) for watch in watches[:2]]
            assert entered.wait(2)
            try:
                with pytest.raises(ServiceError) as error:
                    refresh(other, watches[2])
                assert error.value.code == "watch_busy" and len(calls) == 2
            finally:
                release.set()
            assert all(f.result(timeout=2)["refresh"]["status"] == "succeeded" for f in futures)
    finally:
        other.store.close()


def test_idempotency_pending_conflicts_and_retry_do_not_duplicate_fetch(setup, monkeypatch):
    service, _, p, w, calls = setup
    entered, release = threading.Event(), threading.Event()
    def slow(*args):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return []
    monkeypatch.setattr("modelbuilder.literature.search_openalex", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(refresh, service, w, idempotency_key="once")
        assert entered.wait(2)
        try:
            with pytest.raises(ServiceError) as error:
                refresh(service, w, idempotency_key="once")
            assert error.value.code == "request_in_progress"
            with pytest.raises(ServiceError) as error:
                invoke(service, "evidence.add", project_id=p["project_id"], title="Another operation", idempotency_key="once")
            assert error.value.code == "idempotency_conflict"
        finally:
            release.set()
        result = future.result(timeout=2)
    assert refresh(service, w, idempotency_key="once") == result and len(calls) == 1


def test_failure_is_redacted_persisted_and_waits_for_configured_interval(setup, monkeypatch):
    service, _, p, w, _ = setup
    enable(service, w)
    def fail(*args):
        raise RuntimeError("private query, secret or URL must never appear")
    monkeypatch.setattr("modelbuilder.literature.search_openalex", fail)
    scheduler = ResearchScheduler(service)
    result = scheduler.tick()[0]
    assert result["refresh"]["status"] == "failed"
    assert "private query" not in json.dumps(result)
    assert _date(result["watch"]["next_due_at"]) >= _date(result["refresh"]["finished_at"]) + timedelta(seconds=900)
    assert scheduler.tick() == []
    assert service.store.list("evidence", p["project_id"]) == []


def test_bounded_tick_and_shutdown_do_not_schedule_more_fetches(setup, monkeypatch):
    service, _, p, w, _ = setup
    for i in range(4):
        if i:
            w = invoke(service, "research.watch.create", project_id=p["project_id"], name=str(i), query="fixture")
        enable(service, w)
    scheduler = ResearchScheduler(service)
    assert len(scheduler.tick()) == 2
    entered, release = threading.Event(), threading.Event()
    def slow(*args):
        entered.set()
        assert release.wait(5)
        return [{"title": "Discard on stop", "source_url": "https://openalex.org/W9"}]
    monkeypatch.setattr("modelbuilder.literature.search_openalex", slow)
    scheduler.start()
    assert entered.wait(2)
    start = time.monotonic()
    assert scheduler.stop(timeout=0.05) is False
    assert time.monotonic() - start < 0.5
    release.set()
    assert scheduler.stop(timeout=2)
    assert not service.research_scheduler_running
    assert len(service.store.list("evidence", p["project_id"])) == 1
    assert any(r["status"] == "discarded" and r["error"]["code"] == "shutdown" for r in service.store.list("research_refresh", p["project_id"]))


def test_real_adapter_uses_hard_subprocess_timeout_no_credentials_or_redirects(monkeypatch):
    from modelbuilder import literature
    observed = []
    def run(command, **kwargs):
        observed.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(literature.subprocess, "run", run)
    monkeypatch.setenv("MODELBUILDER_TOKEN", "do-not-pass")
    with pytest.raises(literature.LiteratureUnavailable):
        literature.search_openalex("public fixture", 2)
    command, args = observed[0]
    assert command[1] == "-I" and command[-1].endswith("literature_fetch.py")
    assert args["timeout"] == 20 and args["stderr"] == subprocess.DEVNULL
    assert "MODELBUILDER_TOKEN" not in args["env"]
    assert json.loads(args["input"]) == {"query": "public fixture", "limit": 2}


def test_http_mcp_share_exact_enable_disable_contract_and_lifespan(setup):
    from modelbuilder.mcp_server import catalog_tools
    service, auth, p, w, calls = setup
    app = create_app(service, auth)
    with TestClient(app) as client:
        response = client.get("/v1/catalog", headers={"Authorization": "Bearer fixture-token"})
        tools, aliases = catalog_tools(response.json())
        for name in ("research.watch.schedule_enable", "research.watch.schedule_disable"):
            tool = next(tool for tool in tools if aliases[tool.name] == name)
            assert tool.inputSchema == OPERATIONS[name].input_schema
        body = {"project_id": p["project_id"], "watch_id": w["watch_id"], "expected_revision": 1,
                "idempotency_key": "http-enable", "confirm_schedule": True,
                **{field: w[field] for field in ("provider", "query", "limit", "interval_seconds")}}
        response = client.post("/v1/operations/research.watch.schedule_enable", json=body, headers={"Authorization": "Bearer fixture-token"})
        assert response.status_code == 200 and response.json()["schedule"]["enabled"]
        response = client.post("/v1/operations/research.watch.schedule_disable", json={
            "project_id": p["project_id"], "watch_id": w["watch_id"], "expected_revision": response.json()["revision"],
            "idempotency_key": "http-disable"}, headers={"Authorization": "Bearer fixture-token"})
        assert response.status_code == 200 and not response.json()["schedule"]["enabled"]
    assert not app.state.research_scheduler.thread.is_alive()


def test_request_resume_after_commit_crash_keeps_completed_batch_progress(setup, monkeypatch):
    from modelbuilder.research_scheduler import ResearchRefresh
    service, auth, p, w, calls = setup
    for i in range(3):
        invoke(service, "research.watch.create", project_id=p["project_id"], name=f"Watch {i}", query="public fixture")
    original = ResearchRefresh.refresh_one
    crashed = []
    def interrupted(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if not crashed:
            crashed.append(True)
            raise RuntimeError("simulated process exit after committed progress")
        return result
    monkeypatch.setattr(ResearchRefresh, "refresh_one", interrupted)
    with pytest.raises(RuntimeError):
        invoke(service, "research.watch.refresh_due", project_id=p["project_id"], max_watches=5, idempotency_key="recover-batch")
    assert len(calls) == 1
    other = second_service(service, auth)
    try:
        result = invoke(other, "research.watch.refresh_due", project_id=p["project_id"], max_watches=5, idempotency_key="recover-batch")
        assert result["refreshed"] == 4 and len(calls) == 4
        assert len(other.store.list("research_refresh", p["project_id"])) == 4
        assert invoke(other, "research.watch.refresh_due", project_id=p["project_id"], max_watches=5,
                      idempotency_key="recover-batch") == result
        assert len(calls) == 4
    finally:
        other.store.close()


def test_actual_provider_child_is_killed_and_reaped_at_deadline(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import psutil
    from modelbuilder import literature
    child = tmp_path / "controlled_slow_provider.py"
    pidfile = tmp_path / "child.pid"
    child.write_text(f"import os,time\nfrom pathlib import Path\nPath({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n")
    monkeypatch.setattr(literature, "Path", lambda _: SimpleNamespace(with_name=lambda _: child))
    monkeypatch.setattr(literature, "FETCH_TIMEOUT_SECONDS", 0.2)
    start = time.monotonic()
    with pytest.raises(literature.LiteratureUnavailable):
        literature.search_openalex("controlled public fixture", 1)
    assert time.monotonic() - start < 2
    assert pidfile.is_file()
    assert not psutil.pid_exists(int(pidfile.read_text()))


async def test_official_mcp_client_can_enable_read_and_disable_schedule(setup):
    import httpx
    from mcp.shared.memory import create_connected_server_and_client_session
    from modelbuilder.mcp_server import OperationGateway, create_server
    service, auth, p, w, calls = setup
    app = create_app(service, auth)
    async with httpx.AsyncClient(base_url="http://fixture", headers={"Authorization": "Bearer fixture-token"},
                                transport=httpx.ASGITransport(app=app)) as client:
        gateway = OperationGateway(client)
        server = create_server(gateway, await gateway.catalog())
        async with create_connected_server_and_client_session(server) as session:
            payload = {"project_id": p["project_id"], "watch_id": w["watch_id"], "expected_revision": 1,
                       "confirm_schedule": True, "idempotency_key": "sdk-enable",
                       **{field: w[field] for field in ("provider", "query", "limit", "interval_seconds")}}
            enabled = await session.call_tool("research_watch_schedule_enable", payload)
            assert not enabled.isError and enabled.structuredContent["schedule"]["enabled"]
            assert ResearchScheduler(service).tick()[0]["refresh"]["status"] == "succeeded"
            read = await session.call_tool("research_watch_get", {"project_id": p["project_id"], "watch_id": w["watch_id"]})
            assert not read.isError and len(read.structuredContent["refresh_history"]) == 1
            stale = await session.call_tool("research_watch_schedule_disable", {
                "project_id": p["project_id"], "watch_id": w["watch_id"], "expected_revision": 1, "idempotency_key": "sdk-stale"})
            assert stale.isError and stale.structuredContent["http_status"] == 409
            disabled = await session.call_tool("research_watch_schedule_disable", {
                "project_id": p["project_id"], "watch_id": w["watch_id"],
                "expected_revision": read.structuredContent["revision"], "idempotency_key": "sdk-disable"})
            assert not disabled.isError and not disabled.structuredContent["schedule"]["enabled"]
            assert ResearchScheduler(service).tick() == [] and len(calls) == 1


def test_prefetch_discard_and_request_progress_are_atomic_across_crash(setup, monkeypatch):
    from modelbuilder.research_scheduler import ResearchRefresh
    service, _, p, w, calls = setup
    # Capture a durable request that could not yet acquire its watch.
    with service.store.transaction():
        service.store.claim_research_watch(w["watch_id"], "busy-fixture", "2999-01-01T00:00:00+00:00", 2)
    with pytest.raises(ServiceError):
        refresh(service, w, idempotency_key="discard-once")
    invoke(service, "research.watch.update", project_id=p["project_id"], watch_id=w["watch_id"],
           expected_revision=w["revision"], changes={"enabled": False})
    original = ResearchRefresh.refresh_one
    def interrupted(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("simulated crash after pre-fetch discard")
    monkeypatch.setattr(ResearchRefresh, "refresh_one", interrupted)
    with pytest.raises(RuntimeError):
        refresh(service, w, idempotency_key="discard-once")
    assert len(service.store.list("research_refresh", p["project_id"])) == 1
    monkeypatch.setattr(ResearchRefresh, "refresh_one", original)
    result = refresh(service, w, idempotency_key="discard-once")
    assert result["refresh"]["status"] == "discarded"
    assert len(service.store.list("research_refresh", p["project_id"])) == 1 and calls == []


def test_stale_scheduler_actor_cannot_revoke_replacement_owners_schedule(setup):
    from modelbuilder.research_scheduler import ResearchRefresh
    service, auth, p, w, calls = setup
    old = enable(service, w)
    replacement = Actor("replacement-owner", {"read", "modify"}, {p["project_id"]})
    auth._tokens["replacement-token"] = replacement
    current = enable(service, old, actor=replacement)
    del auth._tokens["fixture-token"]
    result = ResearchRefresh(service).refresh_one(old, OWNER, automatic=True)
    assert result is None and calls == []
    assert service.store.get("research_watch", w["watch_id"]) == current
    assert ResearchScheduler(service).tick()[0]["refresh"]["status"] == "succeeded"


def test_process_death_leaves_recoverable_lease_without_lock_or_duplicate_result(setup, tmp_path):
    import os
    from pathlib import Path
    import sys
    service, auth, p, w, calls = setup
    enable(service, w)
    ready = tmp_path / "child-fetch-started"
    script = '''
from pathlib import Path
import sys,time
from modelbuilder.service import OperationService
from modelbuilder.auth import TokenAuth
from modelbuilder.models import Actor
from modelbuilder.research_scheduler import ResearchScheduler
import modelbuilder.literature
service = OperationService(sys.argv[1])
auth = TokenAuth({"fixture-token": Actor("fixture-owner", {"modify", "read"})})
service.research_authorizer = auth.authorize_research
def controlled_provider(*args):
    Path(sys.argv[2]).write_text("ready")
    time.sleep(30)
    return []
modelbuilder.literature.search_openalex = controlled_provider
ResearchScheduler(service).tick()
'''
    child = subprocess.Popen([sys.executable, "-c", script, service.store.path, str(ready)],
                             env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")})
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            assert child.poll() is None
            time.sleep(0.01)
        assert ready.exists()
        # A real other process is in provider I/O, but read/write traffic is free.
        assert invoke(service, "project.get", project_id=p["project_id"])["project_id"] == p["project_id"]
        assert ResearchScheduler(service).tick() == []
        child.kill()
        child.wait(timeout=3)
        other = second_service(service, auth)
        try:
            assert ResearchScheduler(other).tick() == []
            with other.store.transaction():
                other.store.connection.execute("UPDATE research_leases SET expires_at='2000-01-01T00:00:00+00:00'")
            result = ResearchScheduler(other).tick()[0]
            assert result["refresh"]["status"] == "succeeded"
            assert len(other.store.list("research_refresh", p["project_id"])) == 1
            assert len(other.store.list("evidence", p["project_id"])) == 1 and len(calls) == 1
        finally:
            other.store.close()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
