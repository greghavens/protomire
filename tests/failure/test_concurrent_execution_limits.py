"""Attempt-owned deadlines remain safe under native submit-thread overlap."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
import os
import sys
import threading
from types import SimpleNamespace

import pytest

from modelbuilder.backend import common, execution_limits as limits
from modelbuilder.iterations import invocation_identity


def records(job):
    return [common.read_json(path) for path in sorted((job / "active-nodes").glob("*.json"))]


@pytest.fixture
def clock(monkeypatch):
    value = [100.0]
    monkeypatch.setattr(limits.time, "monotonic", lambda: value[0])
    return value


@pytest.fixture
def native_hooks(tmp_path, monkeypatch):
    current = ContextVar("test_native_step")
    monkeypatch.setitem(sys.modules, "zenml", SimpleNamespace(get_step_context=current.get))
    monkeypatch.setenv("MODELBUILDER_JOB_DIR", str(tmp_path))
    token = limits._hook_attempts.set(())
    diagnostics = []
    monkeypatch.setattr(os, "write", lambda fd, message: diagnostics.append((fd, message)))

    def exit_process(code):
        raise SystemExit(code)

    monkeypatch.setattr(os, "_exit", exit_process)
    yield current, diagnostics
    limits._hook_attempts.reset(token)


def context(name, step_id=None, extra=None, version=1):
    return SimpleNamespace(
        step_name=name,
        step_run=SimpleNamespace(id=step_id or f"step-{name}", version=version,
                                 config=SimpleNamespace(extra=extra or {})),
        model_version=None,
        pipeline_run=SimpleNamespace(id="run", project=SimpleNamespace(id="project")),
    )


def test_active_node_threads_own_independent_deadlines(tmp_path):
    started = threading.Barrier(3)
    finish = [threading.Event(), threading.Event()]

    def execute(index):
        with limits.active_node(tmp_path, f"node-{index}", 60):
            started.wait(timeout=10)
            assert finish[index].wait(timeout=10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute, index) for index in range(2)]
        try:
            started.wait(timeout=10)
            active = records(tmp_path)
            assert {row["node_id"] for row in active} == {"node-0", "node-1"}
            assert len({row["token"] for row in active}) == 2
            assert len({row["owner_thread_id"] for row in active}) == 2
            finish[0].set()
            futures[0].result(timeout=10)
            assert [row["node_id"] for row in records(tmp_path)] == ["node-1"]
            assert limits.node_timeout(tmp_path) is None
        finally:
            for event in finish:
                event.set()
            for future in futures:
                future.result(timeout=10)
    assert records(tmp_path) == []


def test_same_invocation_attempts_cannot_overwrite_each_other(tmp_path):
    with limits.active_node(tmp_path, "same", 60):
        first = records(tmp_path)[0]
        with limits.active_node(tmp_path, "same", 60):
            assert len(records(tmp_path)) == 2
        assert records(tmp_path) == [first]
    assert records(tmp_path) == []


def test_healthy_attempt_cleans_up_when_sibling_expired(tmp_path, clock):
    with limits.active_node(tmp_path, "slow", 1):
        with limits.active_node(tmp_path, "healthy", 60):
            clock[0] = 102.0
        assert [row["node_id"] for row in records(tmp_path)] == ["slow"]
    expired = limits.node_timeout(tmp_path)
    assert expired["node_id"] == expired["canonical_node_id"] == expired["invocation_id"] == "slow"
    with pytest.raises(TimeoutError):
        with limits.active_node(tmp_path, "next", 60):
            pytest.fail("Expired evidence must prevent new work")
    assert records(tmp_path) == [expired]


def test_timeout_selects_earliest_deadline_then_stable_invocation(tmp_path, clock):
    with limits.active_node(tmp_path, "z-last", 5):
        with limits.active_node(tmp_path, "b-first", 2):
            with limits.active_node(tmp_path, "a-first", 2):
                clock[0] = 110.0
                assert limits.node_timeout(tmp_path)["node_id"] == "a-first"
    assert len(records(tmp_path)) == 3
    assert limits.node_timeout(tmp_path)["node_id"] == "a-first"


def test_stale_owner_cannot_clear_replaced_record(tmp_path, clock):
    with limits.active_node(tmp_path, "node", 60):
        record = records(tmp_path)[0]
        replacement = {**record, "owner_pid": record["owner_pid"] + 1}
        common.write_json(limits._attempt_path(tmp_path, record["token"]), replacement)
    assert records(tmp_path) == [replacement]


def test_legacy_deadline_is_still_enforced(tmp_path, clock):
    legacy = {"node_id": "old", "started_monotonic": 90.0, "timeout_seconds": 1, "token": "legacy"}
    common.write_json(tmp_path / "active-node.json", legacy)
    assert limits.node_timeout(tmp_path) == legacy
    with pytest.raises(TimeoutError):
        with limits.active_node(tmp_path, "new", 60):
            pass
    assert common.read_json(tmp_path / "active-node.json") == legacy


def test_supervisor_tolerates_concurrent_completed_record_removal(tmp_path, monkeypatch):
    with limits.active_node(tmp_path, "node", 60):
        original = limits.read_json

        def disappearing(path):
            if path.parent.name == "active-nodes":
                path.unlink(missing_ok=True)
            return original(path)

        monkeypatch.setattr(limits, "read_json", disappearing)
        assert limits.node_timeout(tmp_path) is None


def test_native_hooks_end_only_their_context_and_retry_token(tmp_path, native_hooks, clock):
    current, _ = native_hooks
    common.write_json(tmp_path / "request.json", {"recipe": {"nodes": [{"id": "node", "timeout": 60}]}})
    first, second = context("node", "attempt-1"), context("node", "attempt-2", version=2)
    current.set(first)
    limits.deadline_start()
    first_record = records(tmp_path)[0]
    current.set(second)
    limits.deadline_start()
    assert len(records(tmp_path)) == 2
    current.set(first)
    limits.deadline_end()
    limits.deadline_end()  # A duplicate end must not consume the newer attempt.
    second_record = records(tmp_path)[0]
    assert second_record["step_run_id"] == "attempt-2"
    assert second_record["step_run_version"] == 2
    current.set(second)
    limits.deadline_end()
    assert records(tmp_path) == []
    clock[0] += 120  # Retry backoff must not count toward active execution time.
    limits.deadline_start()
    third_record = records(tmp_path)[0]
    assert third_record["token"] not in {first_record["token"], second_record["token"]}
    assert third_record["started_monotonic"] == clock[0]
    limits.deadline_end()
    assert records(tmp_path) == []


def test_native_hook_iteration_identity_and_expired_sibling(tmp_path, native_hooks, clock):
    current, _ = native_hooks
    block = {"id": "loop", "node_ids": ["node"], "max_iterations": 3, "state": []}
    recipe = {"nodes": [{"id": "node", "timeout": 1}, {"id": "other", "timeout": 60}],
              "bounded_iterations": [block]}
    common.write_json(tmp_path / "request.json", {"recipe": recipe})
    identity = invocation_identity("node", block, 2)
    first = context(identity["invocation_id"], extra=identity)
    second = context("other")
    current.set(first)
    limits.deadline_start()
    current.set(second)
    limits.deadline_start()
    clock[0] += 2
    limits.deadline_end()
    current.set(first)
    limits.deadline_end()
    record = limits.node_timeout(tmp_path)
    assert {key: record[key] for key in identity} == identity
    assert records(tmp_path) == [record]
    current.set(second)
    with pytest.raises(SystemExit) as error:
        limits.deadline_start()
    assert error.value.code == 124
    assert records(tmp_path) == [record]


def test_native_hook_threads_keep_independent_owners(tmp_path, native_hooks, monkeypatch):
    current, _ = native_hooks
    common.write_json(tmp_path / "request.json", {"recipe": {"nodes": [
        {"id": "first", "timeout": 60}, {"id": "second", "timeout": 60}]}})
    writes = threading.Barrier(2)
    started = threading.Barrier(3)
    finish = [threading.Event(), threading.Event()]
    replace = common.os.replace
    temporary_names = []

    def simultaneous_replace(source, destination):
        if destination.name == "native.json":
            temporary_names.append(source.name)
            writes.wait(timeout=10)
        return replace(source, destination)

    monkeypatch.setattr(common.os, "replace", simultaneous_replace)

    def execute(index, name):
        current.set(context(name))
        limits.deadline_start()
        started.wait(timeout=10)
        assert finish[index].wait(timeout=10)
        limits.deadline_end()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute, index, name) for index, name in enumerate(("first", "second"))]
        try:
            started.wait(timeout=10)
            assert len(records(tmp_path)) == 2
            assert len(set(temporary_names)) == 2
            assert common.read_json(tmp_path / "native.json")["backend_run_id"] == "run"
            finish[0].set()
            futures[0].result(timeout=10)
            assert [row["node_id"] for row in records(tmp_path)] == ["second"]
        finally:
            for event in finish:
                event.set()
            for future in futures:
                future.result(timeout=10)
    assert records(tmp_path) == []
    assert not list(tmp_path.rglob("*.tmp"))


def test_end_hook_cleanup_failure_is_fail_closed(tmp_path, native_hooks, monkeypatch):
    current, diagnostics = native_hooks
    common.write_json(tmp_path / "request.json", {"recipe": {"nodes": [{"id": "node", "timeout": 60}]}})
    current.set(context("node"))
    limits.deadline_start()

    def fail_cleanup(*args):
        raise OSError("deadline cleanup unavailable")

    monkeypatch.setattr(limits, "_finish_attempt", fail_cleanup)
    with pytest.raises(SystemExit) as error:
        limits.deadline_end()
    assert error.value.code == 70
    assert len(records(tmp_path)) == 1
    assert diagnostics[-1][0] == 2 and b"deadline cleanup unavailable" in diagnostics[-1][1]


def test_duplicate_start_hook_is_fail_closed_without_replacing_owner(tmp_path, native_hooks):
    current, diagnostics = native_hooks
    common.write_json(tmp_path / "request.json", {"recipe": {"nodes": [{"id": "node", "timeout": 60}]}})
    current.set(context("node"))
    limits.deadline_start()
    original = records(tmp_path)
    with pytest.raises(SystemExit) as error:
        limits.deadline_start()
    assert error.value.code == 70
    assert b"already owns a deadline" in diagnostics[-1][1]
    assert records(tmp_path) == original
    limits.deadline_end()


def test_shared_json_writes_use_complete_unique_temporary_files(tmp_path, monkeypatch):
    destination = tmp_path / "shared.json"
    ready = threading.Barrier(2)
    replace = common.os.replace
    observed = []

    def synchronized_replace(source, target):
        observed.append((source, common.read_json(source)))
        ready.wait(timeout=10)
        return replace(source, target)

    monkeypatch.setattr(common.os, "replace", synchronized_replace)
    values = [{"value": "a" * 10000}, {"value": "b" * 10000}]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda value: common.write_json(destination, value), values))
    assert len({path for path, _ in observed}) == 2
    assert all(value in values for _, value in observed)
    assert common.read_json(destination) in values
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("failure", ["serialization", "replacement"])
def test_failed_atomic_write_retains_previous_value_and_cleans_temporary(tmp_path, monkeypatch, failure):
    destination = tmp_path / "shared.json"
    common.write_json(destination, {"original": True})

    def fail_replace(*args):
        raise OSError("replace failed")

    value = {"bad": object()} if failure == "serialization" else {"replacement": True}
    if failure == "replacement":
        monkeypatch.setattr(common.os, "replace", fail_replace)
    with pytest.raises(TypeError if failure == "serialization" else OSError):
        common.write_json(destination, value)
    assert common.read_json(destination) == {"original": True}
    assert not list(tmp_path.glob("*.tmp"))
