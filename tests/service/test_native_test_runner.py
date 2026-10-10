"""Tiny real subprocesses test CI scheduling/cleanup; no native training occurs."""
from collections import Counter
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("native_test_runner", ROOT / "scripts/native_test_runner.py")
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def serial_paths(fragment):
    line = next(line for line in (ROOT / "scripts/test_all.sh").read_text().splitlines() if fragment in line)
    return tuple(token for token in shlex.split(line) if token.startswith("tests/"))


def test_hosted_workflows_are_manual_only():
    # Owner runs CI locally; publishing source must not spend hosted minutes.
    workflows = [path for path in (ROOT / ".github/workflows").iterdir()
                 if path.suffix in {".yml", ".yaml"}]
    assert workflows
    for workflow in workflows:
        lines = workflow.read_text().splitlines()
        assert lines.count("on:") == 1, workflow.name
        start = lines.index("on:") + 1
        triggers = []
        for line in lines[start:]:
            if line and not line[0].isspace():
                break
            if line.strip():
                triggers.append(line)
        assert triggers == ["  workflow_dispatch:"], workflow.name


@pytest.mark.parametrize("suffix", [".yml", ".yaml"])
def test_manual_workflow_guard_covers_both_yaml_extensions(tmp_path, monkeypatch, suffix):
    directory = tmp_path / ".github/workflows"
    directory.mkdir(parents=True)
    workflow = directory / f"ci{suffix}"
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    workflow.write_text("name: test\non:\n  push:\njobs: {}\n")
    with pytest.raises(AssertionError):
        test_hosted_workflows_are_manual_only()
    workflow.write_text("name: test\non:\n  workflow_dispatch:\njobs: {}\n")
    test_hosted_workflows_are_manual_only()


def test_collection_selectors_are_complete_disjoint_union_of_serial_commands():
    assert runner.SERVICE_TESTS == serial_paths("-m pytest tests/service/test_real_backend.py")
    serial = serial_paths("-m pytest tests/integration/test_cpu_fixtures.py")
    assert runner.LIFECYCLE_TESTS + runner.COMPONENT_TESTS == serial
    expanded = []
    for collection in (runner.SERVICE_TESTS, runner.LIFECYCLE_TESTS, runner.COMPONENT_TESTS):
        files = [str(path.relative_to(ROOT)) for name in collection
                 for path in ((ROOT / name).rglob("test_*.py") if (ROOT / name).is_dir() else [ROOT / name])
                 if collection is runner.SERVICE_TESTS or str(path.relative_to(ROOT)) not in runner.SERVICE_ONLY_TESTS]
        assert len(files) == len(set(files))
        expanded.extend(files)
    assert all(count == 1 for count in Counter(expanded).values())
    assert "tests/integration/test_admission_live_native.py" in runner.SERVICE_TESTS
    assert expanded.count("tests/integration/test_admission_live_native.py") == 1
    assert "tests/placement/test_placement.py" in runner.COMPONENT_TESTS
    assert "tests/placement/test_unlocked_probes.py" not in expanded  # Normal service unit phase; imports TestClient.
    script = (ROOT / "scripts/test_all.sh").read_text()
    assert '${MODELBUILDER_NATIVE_TEST_PROCESSES:-1}' in script
    assert script.index('npm run test:component-http') < script.index('scripts/native_test_runner.py')
    for path in runner.SERVICE_ONLY_TESTS:
        assert path in runner.SERVICE_TESTS
        assert f"--ignore={path}" in next(line for line in script.splitlines() if "-m pytest tests/integration/test_cpu_fixtures.py" in line)
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "timeout-minutes: 40" in ci and "MODELBUILDER_NATIVE_TEST_PROCESSES=2" in ci
    assert ci.count("runs-on: ubuntu-latest") == 2  # Existing contracts and CPU jobs only.


def test_group_environment_and_junit_paths_are_isolated(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".venv-backend/bin").mkdir(parents=True)
    (root / ".venv-backend/bin/python").touch()
    temporary = tmp_path / "groups"
    temporary.mkdir()
    monkeypatch.setenv("MODELBUILDER_REQUIRE_PARQUET", "1")
    monkeypatch.setenv("MODELBUILDER_TRACKING_TEST_PYTHON", "/approved/tracking/python")
    groups = runner.build_groups(root, Path(sys.executable), temporary)
    assert [group.name for group in groups] == ["cpu-lifecycle", "integration", "cpu-components"]
    for key in ("HOME", "XDG_CACHE_HOME", "ZENML_CONFIG_PATH", "ZENML_LOCAL_STORES_PATH", "TMPDIR", "TMP", "TEMP"):
        assert len({group.environment[key] for group in groups}) == 3
    assert len({group.log for group in groups}) == len({group.xml for group in groups}) == 3
    for group in groups:
        env = group.environment
        assert env["MODELBUILDER_REQUIRE_PARQUET"] == "1"
        assert env["MODELBUILDER_TRACKING_TEST_PYTHON"] == "/approved/tracking/python"
        assert all(env[key] == "1" for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"))
        assert any(arg.startswith("--basetemp=") for arg in group.command)
        assert any(arg.startswith("cache_dir=") for arg in group.command)
        if group.name != "integration":
            assert env["MODELBUILDER_REQUIRE_LOCAL_INFERENCE"] == "1"
            assert all(f"--ignore={path}" in group.command for path in runner.SERVICE_ONLY_TESTS)
        else:
            assert ("-m", "integration") == group.command[-7:-5]


WORKER = r'''
import json,os,signal,subprocess,sys,time
from pathlib import Path
mode,name,root,delay=sys.argv[1:];root=Path(root);delay=float(delay)
def event(kind):
    with (root/'events').open('a') as stream: stream.write(json.dumps([name,kind,time.monotonic()])+'\n')
event('start')
if mode=='descendant':
    child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'],start_new_session=True)
    (root/'detached.pid').write_text(str(child.pid))
    while True:time.sleep(.1)
if mode=='fail':
    while not (root/'detached.pid').exists():time.sleep(.01)
    raise SystemExit(7)
time.sleep(delay)
event('end')
print('group output '+name)
if mode!='missing':(root/(name+'.xml')).write_text('<testsuite tests="1"/>')
'''


def launch(tmp_path, specs, *, timeout=5):
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER)
    bootstrap = tmp_path / "runner.py"
    bootstrap.write_text('''import importlib.util,json,os,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("native_test_runner",sys.argv[1]);m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
root=Path(sys.argv[2]);groups=[m.Group(name,(sys.executable,str(root/'worker.py'),mode,name,str(root),str(delay)),dict(os.environ),root/(name+'.log'),root/(name+'.xml')) for name,mode,delay in json.loads(sys.argv[3])]
raise SystemExit(m.run_groups(groups,root,root/'report.json',timeout=float(sys.argv[4])))
''')
    return subprocess.Popen([sys.executable, "-I", str(bootstrap), str(ROOT / "scripts/native_test_runner.py"),
                             str(tmp_path), json.dumps(specs), str(timeout)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)


def finish(process, tmp_path, timeout=12):
    output, error = process.communicate(timeout=timeout)
    return process.returncode, json.loads((tmp_path / "report.json").read_text()), output + error


@pytest.mark.skipif(sys.platform != "linux", reason="Optional concurrent runner uses Linux descendant containment")
def test_three_complete_groups_never_exceed_two_slots_and_keep_logs(tmp_path):
    process = launch(tmp_path, [("long", "pass", .6), ("short", "pass", .15), ("last", "pass", .1)])
    code, report, _ = finish(process, tmp_path)
    assert code == 0 and report["status"] == "completed" and report["cleanup"]["complete"]
    events = [json.loads(line) for line in (tmp_path / "events").read_text().splitlines()]
    active, high = set(), 0
    for name, kind, _ in events:
        active.add(name) if kind == "start" else active.remove(name)
        high = max(high, len(active))
    assert high == 2 and not active
    lookup = {(name, kind): timestamp for name, kind, timestamp in events}
    assert lookup['short', 'end'] <= lookup['last', 'start'] < lookup['long', 'end']
    assert all(record["status"] == "passed" for record in report["groups"])
    for name in ("long", "short", "last"):
        assert (tmp_path / (name + ".log")).read_text().strip() == 'group output ' + name


@pytest.mark.skipif(sys.platform != "linux", reason="Optional concurrent runner uses Linux descendant containment")
@pytest.mark.parametrize("mode", ["failure", "timeout", "signal"])
def test_failed_timed_out_or_interrupted_queue_kills_detached_native_descendants(tmp_path, mode):
    specs = [("native", "descendant", 0)]
    if mode == "failure":
        specs += [("failure", "fail", 0), ("unstarted", "pass", 0)]
    if mode == "failure":
        (tmp_path / "unstarted.xml").write_text('<testsuite tests="100"/>')
        (tmp_path / "unstarted.log").write_text('previous passing run')
    process = launch(tmp_path, specs, timeout=.35 if mode == "timeout" else 5)
    deadline = time.monotonic() + 4
    while not (tmp_path / "detached.pid").exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(.01)
    assert (tmp_path / "detached.pid").exists()
    child_pid = int((tmp_path / "detached.pid").read_text())
    if mode == "signal":
        process.send_signal(signal.SIGTERM)
    code, report, _ = finish(process, tmp_path)
    assert code == {"failure": 1, "timeout": 124, "signal": 143}[mode]
    assert report["cleanup"]["complete"]
    assert not Path(f"/proc/{child_pid}").exists()
    if mode == "failure":
        assert next(row for row in report["groups"] if row["name"] == "unstarted")["status"] == "not_started"
        assert not (tmp_path / "unstarted.xml").exists() and not (tmp_path / "unstarted.log").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Optional concurrent runner uses Linux descendant containment")
def test_stale_xml_cannot_mask_missing_current_receipt(tmp_path):
    (tmp_path / "stale.xml").write_text('<testsuite tests="100"/>')
    code, report, _ = finish(launch(tmp_path, [("stale", "missing", 0)]), tmp_path)
    assert code == 1 and report["groups"][0]["error"] == "missing_junit_report"
    assert not (tmp_path / "stale.xml").exists()


def test_pid_reuse_is_never_signaled(monkeypatch):
    closed = []
    monkeypatch.setattr(runner.os, "pidfd_open", lambda *_: 77, raising=False)
    monkeypatch.setattr(runner.os, "close", closed.append)
    monkeypatch.setattr(runner, "_identity", lambda _: (os.getpid(), 200))
    monkeypatch.setattr(runner.signal, "pidfd_send_signal", lambda *a: pytest.fail("a reused PID must not be signaled"), raising=False)
    runner._signal(999999, (os.getpid(), 100), signal.SIGTERM)
    assert closed == [77]


def test_unsupported_platform_has_no_concurrent_fallback(monkeypatch):
    monkeypatch.setattr(runner.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="default serial"):
        runner._subreaper()


def test_setup_failure_cannot_leave_old_success_reports(tmp_path):
    evidence = tmp_path / "evidence/tests"
    evidence.mkdir(parents=True)
    for name in ("cpu-fixtures", "cpu-lifecycle", "integration", "cpu-components"):
        (evidence / (name + ".xml")).write_text('<testsuite tests="100"/>')
        (evidence / (name + ".log")).write_text('previous passing run')
    report = evidence / "native-runner.json"
    report.write_text('{"status":"completed","exit_code":0}')
    assert runner.main(["--root", str(tmp_path)]) == 1
    assert json.loads(report.read_text())["status"] == "setup_failed"
    assert not list(evidence.glob("*.xml")) and not list(evidence.glob("*.log"))


def test_exit_after_identity_check_uses_expired_handle_without_pid_fallback(monkeypatch):
    closed, sent = [], []
    monkeypatch.setattr(runner.os, "pidfd_open", lambda *_: 77, raising=False)
    monkeypatch.setattr(runner.os, "close", closed.append)
    monkeypatch.setattr(runner, "_identity", lambda _: (os.getpid(), 100))
    monkeypatch.setattr(runner.os, "kill", lambda *a: pytest.fail("numeric PID fallback is unsafe"))
    def expired(descriptor, signum, *_):
        sent.append((descriptor, signum))
        raise ProcessLookupError("bound process exited after identity validation")
    monkeypatch.setattr(runner.signal, "pidfd_send_signal", expired, raising=False)
    runner._signal(999999, (os.getpid(), 100), signal.SIGTERM)
    assert sent == [(77, signal.SIGTERM)] and closed == [77]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pidfd containment")
def test_missing_pidfd_capability_fails_before_subreaper_or_launch(monkeypatch):
    monkeypatch.delattr(runner.os, "pidfd_open")
    monkeypatch.setattr(runner.ctypes, "CDLL", lambda *_: pytest.fail("must refuse before setting subreaper"))
    with pytest.raises(RuntimeError, match="pidfd"):
        runner._subreaper()


def test_incomplete_cleanup_preserves_live_temporary_state_in_local_receipt(tmp_path, monkeypatch):
    state = tmp_path / "active-temporary-state"
    state.mkdir()
    monkeypatch.setattr(runner.tempfile, "mkdtemp", lambda **_: str(state))
    monkeypatch.setattr(runner, "build_groups", lambda *a: [])
    def incomplete(_groups, _root, report, **_options):
        report.write_text(json.dumps({"status": "cleanup_incomplete", "cleanup": {"complete": False}}))
        return 1
    monkeypatch.setattr(runner, "run_groups", incomplete)
    monkeypatch.setattr(runner.shutil, "rmtree", lambda *_: pytest.fail("live temporary state must remain"))
    assert runner.main(["--root", str(tmp_path)]) == 1
    receipt = json.loads((tmp_path / "evidence/tests/native-runner.json").read_text())
    assert state.is_dir() and receipt["retained_temporary_directory"] == str(state)


def test_native_admission_harness_preserves_venv_invocation_path(tmp_path, monkeypatch):
    """Resolving the executable symlink discards Python's virtualenv selection."""
    spec = importlib.util.spec_from_file_location("native_admission_acceptance", ROOT / "tests/integration/test_admission_live_native.py")
    acceptance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acceptance)
    invoked = tmp_path / "approved-venv" / "bin" / "python"
    invoked.parent.mkdir(parents=True)
    invoked.symlink_to(sys.executable)
    monkeypatch.setattr(acceptance, "PYTHON", invoked)

    class CapturedBackend(Exception):
        pass

    def capture(*args, python_executable, **kwargs):
        assert python_executable == str(invoked), "The venv invocation path must retain its symlink"
        raise CapturedBackend()

    monkeypatch.setattr(acceptance, "ZenMLBackend", capture)
    with pytest.raises(CapturedBackend):
        acceptance.test_native_checkpoint_ingestion_during_held_admission(tmp_path / "fixture", monkeypatch)
