"""Bounded startup contention without process-local locks or lost history."""
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import sqlite3
import time

import pytest

from modelbuilder import store as store_module
from modelbuilder.store import SQLiteStore


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.now += duration


class StartupConnection:
    def __init__(self, errors, stage="journal"):
        self.errors = iter(errors)
        self.stage = stage
        self.attempts = 0
        self.closed = False
        self.statements = []

    def attempt(self, stage):
        if self.stage == stage:
            self.attempts += 1
            error = next(self.errors, None)
            if error:
                raise error

    def execute(self, statement):
        self.statements.append(statement)
        if statement == "PRAGMA journal_mode=WAL":
            self.attempt("journal")
        return self

    def fetchone(self):
        return ("wal",)

    def executescript(self, script):
        assert "CREATE TABLE IF NOT EXISTS versions" in script
        self.attempt("schema")

    def close(self):
        self.closed = True


def sqlite_error(code, message="synthetic SQLite startup error"):
    error = sqlite3.OperationalError(message)
    if code is not None:
        error.sqlite_errorcode = code
    return error


def stub_connection(monkeypatch, connection):
    def connect(*args, **kwargs):
        # Native blocking must not add a second timeout to the outer deadline.
        assert kwargs["timeout"] == 0
        return connection
    monkeypatch.setattr(store_module.sqlite3, "connect", connect)
    clock = Clock()
    monkeypatch.setattr(store_module, "time", clock)
    return clock


@pytest.mark.parametrize("stage", ["journal", "schema"])
@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY_RECOVERY])
def test_startup_retries_only_sqlite_contention_and_restores_normal_timeout(monkeypatch, stage, code):
    connection = StartupConnection([sqlite_error(code), sqlite_error(code)], stage)
    clock = stub_connection(monkeypatch, connection)
    store = SQLiteStore(":memory:")
    assert connection.attempts == 3
    assert clock.sleeps == [0.005, 0.01]
    assert connection.statements[-1] == "PRAGMA busy_timeout=30000"
    assert not connection.closed
    store.close()


@pytest.mark.parametrize("code", [None, sqlite3.SQLITE_ERROR, sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_READONLY])
def test_non_lock_startup_errors_are_not_swallowed_or_retried(monkeypatch, code):
    error = sqlite_error(code, "database is locked")  # Message text is not authority for retry.
    connection = StartupConnection([error])
    clock = stub_connection(monkeypatch, connection)
    with pytest.raises(sqlite3.OperationalError) as raised:
        SQLiteStore(":memory:")
    assert raised.value is error
    assert connection.attempts == 1
    assert clock.sleeps == []
    assert connection.closed


def test_startup_contention_deadline_is_bounded_and_closes_failed_connection(monkeypatch):
    error = sqlite_error(sqlite3.SQLITE_BUSY)
    connection = StartupConnection([error] * 100)
    clock = stub_connection(monkeypatch, connection)
    monkeypatch.setattr(store_module, "_STARTUP_TIMEOUT_SECONDS", 0.04)
    with pytest.raises(sqlite3.OperationalError) as raised:
        SQLiteStore(":memory:")
    assert raised.value is error
    assert clock.now == pytest.approx(0.04)
    assert len(clock.sleeps) == 4
    assert connection.closed


def test_real_exclusive_lock_retries_until_released(tmp_path):
    path = tmp_path / "held.sqlite3"
    with sqlite3.connect(path) as locker:
        locker.execute("CREATE TABLE preserved(value TEXT)")
        locker.execute("INSERT INTO preserved VALUES ('history')")
        locker.commit()
        locker.execute("BEGIN EXCLUSIVE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(SQLiteStore, path)
            try:
                time.sleep(0.15)
                assert not future.done()
            finally:
                locker.rollback()
            store = future.result(timeout=5)
    try:
        assert store.connection.execute("SELECT value FROM preserved").fetchone()[0] == "history"
        assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    finally:
        store.close()


def test_real_contention_timeout_leaves_legacy_database_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "timed-out.sqlite3"
    monkeypatch.setattr(store_module, "_STARTUP_TIMEOUT_SECONDS", 0.08)
    with sqlite3.connect(path) as locker:
        locker.execute("CREATE TABLE preserved(value TEXT)")
        locker.execute("INSERT INTO preserved VALUES ('history')")
        locker.commit()
        locker.execute("BEGIN EXCLUSIVE")
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError) as raised:
            SQLiteStore(path)
        duration = time.monotonic() - start
        assert raised.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
        assert 0.07 <= duration < 2
        locker.rollback()
        assert locker.execute("SELECT value FROM preserved").fetchone()[0] == "history"
        assert locker.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='resources'").fetchone()[0] == 0
    recovered = SQLiteStore(path)
    recovered.close()


def open_in_process(path, barrier, index):
    barrier.wait(timeout=15)
    store = SQLiteStore(path)
    try:
        with store.transaction():
            store.put("startup_probe", str(index), "probe", {"revision": 1, "index": index})
    finally:
        store.close()


@pytest.mark.parametrize("round_number", range(3))
@pytest.mark.parametrize("legacy", [False, True], ids=["new", "delete-journal"])
def test_six_processes_initialize_same_database_without_losing_history(tmp_path, round_number, legacy):
    path = tmp_path / f"process-open-{round_number}.sqlite3"
    if legacy:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE preserved(value TEXT)")
            connection.execute("INSERT INTO preserved VALUES ('history')")
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(6)
    processes = [context.Process(target=open_in_process, args=(path, barrier, index)) for index in range(6)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
        assert all(process.exitcode == 0 for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
    store = SQLiteStore(path)
    try:
        assert len(store.list("startup_probe")) == 6
        assert store.connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 6
        assert store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        if legacy:
            assert store.connection.execute("SELECT value FROM preserved").fetchone()[0] == "history"
    finally:
        store.close()
