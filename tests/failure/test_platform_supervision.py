"""Portable process observation must keep uncertainty distinct from termination."""
import os
import signal
import subprocess
import sys
import time
from unittest.mock import Mock

import psutil
import pytest

from modelbuilder.backend import common
from modelbuilder.backend.adapter import BackendError, ZenMLBackend


def test_native_identity_is_not_a_bare_pid():
    identity = common.process_identity(os.getpid())
    assert identity and identity.startswith("psutil:")
    assert common.alive(os.getpid(), identity)
    assert not common.alive(os.getpid(), "different-birth")


def test_empty_process_listing_is_unknown(monkeypatch):
    monkeypatch.setattr(common.os, "killpg", lambda *args: None)
    monkeypatch.setattr(common.psutil, "pids", lambda: [])
    assert common.process_group_state(100) is None
    assert common.process_group_alive(100)


@pytest.mark.parametrize("status, expected", [(psutil.STATUS_RUNNING, True), (psutil.STATUS_ZOMBIE, False)])
def test_group_observation_uses_native_process_api(monkeypatch, status, expected):
    monkeypatch.setattr(common.os, "killpg", lambda *args: None)
    monkeypatch.setattr(common.psutil, "pids", lambda: [123])
    monkeypatch.setattr(common.os, "getpgid", lambda pid: 123)
    monkeypatch.setattr(common.os, "getsid", lambda pid: 123)
    monkeypatch.setattr(common.psutil, "Process", lambda pid: Mock(status=lambda: status))
    assert common.process_group_state(123) is expected


def test_unreadable_member_prevents_death_proof(monkeypatch):
    monkeypatch.setattr(common.os, "killpg", lambda *args: None)
    monkeypatch.setattr(common.psutil, "pids", lambda: [123])
    monkeypatch.setattr(common.os, "getpgid", Mock(side_effect=PermissionError()))
    assert common.process_group_state(123) is None


def test_unreadable_original_process_prevents_death_proof(monkeypatch):
    monkeypatch.setattr(common, "runtime_identity", lambda: "test-boot")
    monkeypatch.setattr(common, "process_group_state", lambda pid: False)
    monkeypatch.setattr(common.psutil, "Process", Mock(side_effect=psutil.AccessDenied(123)))
    assert not common.termination_confirmed({"worker_pid": 123, "worker_identity": "psutil:old",
                                             "runtime_identity": "test-boot"})


def test_darwin_runtime_uses_native_boot_session(monkeypatch):
    monkeypatch.setattr(common.sys, "platform", "darwin")
    monkeypatch.setattr(common, "_darwin_boot_session", lambda: "boot-uuid")
    assert common.runtime_identity() == "darwin:boot-uuid"
    monkeypatch.setattr(common, "_darwin_boot_session", Mock(side_effect=OSError()))
    assert common.runtime_identity() is None


def test_windows_disables_only_native_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    backend = ZenMLBackend(tmp_path, python_executable=sys.executable)
    target = backend.invoke("execution.targets", {})["targets"][0]
    assert not target["available"]
    assert "ZenML" in target["unavailable_reason"]
    with pytest.raises(BackendError, match="native Windows") as error:
        backend.submit({})
    assert error.value.code == "capability_unavailable"


def test_explicit_backend_executable_resolves_path_command(tmp_path, monkeypatch):
    monkeypatch.setattr("modelbuilder.backend.adapter.shutil.which", lambda name: sys.executable)
    backend = ZenMLBackend(tmp_path, python_executable="configured-python")
    assert backend.python == sys.executable


@pytest.mark.skipif(os.name != "posix", reason="POSIX supervised backend integration")
def test_descendant_after_leader_exit_remains_live(tmp_path):
    marker = tmp_path / "child-pid"
    script = ("import subprocess,sys; "
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
              "open(sys.argv[1],'w').write(str(p.pid))")
    parent = subprocess.Popen([sys.executable, "-c", script, str(marker)], start_new_session=True)
    try:
        identity = common.process_identity(parent.pid)
        assert identity
        parent.wait(timeout=5)
        assert marker.exists()
        assert common.process_group_state(parent.pid) is True
        assert not common.termination_confirmed({"worker_pid": parent.pid, "worker_identity": identity,
                                                 "runtime_identity": common.runtime_identity()})
        os.killpg(parent.pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while common.process_group_state(parent.pid) is not False and time.monotonic() < deadline:
            time.sleep(0.01)
        assert common.process_group_state(parent.pid) is False
        assert common.termination_confirmed({"worker_pid": parent.pid, "worker_identity": identity,
                                              "runtime_identity": common.runtime_identity()})
    finally:
        try:
            os.killpg(parent.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        parent.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "linux", reason="v1 Linux journal compatibility")
def test_legacy_linux_journal_keeps_birth_identity(monkeypatch):
    pid = os.getpid()
    identity = common._legacy_linux_identity(pid)
    assert identity and common.alive(pid, identity)
    assert not common.alive(pid, str(int(identity) + 1))
    old_runtime = common.runtime_identity().removeprefix("linux:")
    assert not common.termination_confirmed({"worker_pid": pid, "worker_identity": identity,
                                             "runtime_identity": old_runtime})
