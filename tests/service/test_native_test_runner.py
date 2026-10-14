"""Tiny real subprocesses test CI scheduling/cleanup; no native training occurs."""
import ast
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
    assert runner.CONCURRENCY_TESTS == serial_paths("-m pytest tests/concurrency")
    expanded = []
    for collection in (runner.SERVICE_TESTS, runner.LIFECYCLE_TESTS, runner.COMPONENT_TESTS, runner.CONCURRENCY_TESTS):
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
    # Dormant hosted configuration is unchanged; its budget does not cover the local allowance.
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
    monkeypatch.delenv("MODELBUILDER_REQUIRE_CUDA", raising=False)
    monkeypatch.delenv("MODELBUILDER_REQUIRE_TRAINER_DEVICE", raising=False)
    monkeypatch.delenv("MODELBUILDER_REQUIRE_MLX", raising=False)
    groups = runner.build_groups(root, Path(sys.executable), temporary)
    assert [group.name for group in groups] == ["cpu-lifecycle", "integration", "cpu-components", "cpu-concurrency"]
    for key in ("HOME", "XDG_CACHE_HOME", "ZENML_CONFIG_PATH", "ZENML_LOCAL_STORES_PATH", "TMPDIR", "TMP", "TEMP"):
        assert len({group.environment[key] for group in groups}) == 4
    assert len({group.log for group in groups}) == len({group.xml for group in groups}) == 4
    for group in groups:
        env = group.environment
        assert env["MODELBUILDER_REQUIRE_PARQUET"] == "1"
        assert env["MODELBUILDER_TRACKING_TEST_PYTHON"] == "/approved/tracking/python"
        assert "MODELBUILDER_REQUIRE_CUDA" not in env
        assert "MODELBUILDER_REQUIRE_TRAINER_DEVICE" not in env
        assert "MODELBUILDER_REQUIRE_MLX" not in env
        assert all(env[key] == "1" for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"))
        assert any(arg.startswith("--basetemp=") for arg in group.command)
        assert any(arg.startswith("cache_dir=") for arg in group.command)
        if group.name != "integration":
            assert env["MODELBUILDER_REQUIRE_LOCAL_INFERENCE"] == "1"
            assert env["MODELBUILDER_REQUIRE_TRAINER"] == "1"
            assert all(f"--ignore={path}" in group.command for path in runner.SERVICE_ONLY_TESTS)
        arguments = group.command[3:]
        if group.name in {"integration", "cpu-concurrency"}:
            assert arguments.count("-m") == 1
            assert arguments[arguments.index("-m") + 1] == "integration"
        else:
            assert "-m" not in arguments
    concurrency = groups[-1]
    assert concurrency.command[0] == str(root / ".venv-backend/bin/python")
    assert all("tests/concurrency" not in group.command for group in groups[:-1])
    assert runner.MAX_SECONDS == 2700


@pytest.mark.parametrize("timeout", [-1, 0, 2701])
def test_queue_deadline_outside_revised_bound_refuses_before_containment(tmp_path, monkeypatch, timeout):
    monkeypatch.setattr(runner, "_subreaper", lambda: pytest.fail("Invalid budget must not start containment"))
    with pytest.raises(ValueError, match="at most 2700 seconds"):
        runner.run_groups([], tmp_path, tmp_path / "report.json", timeout=timeout)


@pytest.fixture
def empty_budget_runner(monkeypatch):
    # Budget-only checks run in pytest, which can already own unrelated children.
    # Real containment is exercised separately in a dedicated runner subprocess.
    monkeypatch.setattr(runner, "_subreaper", lambda: None)
    cleanups = []
    def cleanup(processes):
        assert processes == []
        cleanups.append(processes)
        return {"complete": True, "signaled_processes": 0}
    monkeypatch.setattr(runner, "_cleanup", cleanup)
    return cleanups


@pytest.mark.parametrize("timeout", [None, 1, 2700])
def test_queue_default_and_exact_deadline_boundaries_are_recorded(tmp_path, empty_budget_runner, timeout):
    # Empty selections exercise budget validation/reporting without launching jobs.
    options = {} if timeout is None else {"timeout": timeout}
    report = tmp_path / "report.json"
    assert runner.run_groups([], tmp_path, report, **options) == 0
    receipt = json.loads(report.read_text())
    assert receipt["deadline_seconds"] == (2700 if timeout is None else timeout)
    assert receipt["concurrency"] == 2 and receipt["cleanup"]["complete"]
    assert empty_budget_runner == [[]]


@pytest.mark.parametrize("arguments,expected,code", [
    ([], 2700, 0), (["--timeout-seconds", "1"], 1, 0),
    (["--timeout-seconds", "2700"], 2700, 0), (["--timeout-seconds", "2701"], None, 1),
])
def test_cli_default_and_explicit_deadline_obey_revised_bound(
        tmp_path, monkeypatch, empty_budget_runner, arguments, expected, code):
    monkeypatch.setattr(runner, "build_groups", lambda *_args: [])
    assert runner.main(["--root", str(tmp_path), *arguments]) == code
    receipt = json.loads((tmp_path / "evidence/tests/native-runner.json").read_text())
    assert empty_budget_runner == ([] if code else [[]])
    if code:
        assert receipt["status"] == "setup_failed" and receipt["error"] == "ValueError"
    else:
        assert receipt["deadline_seconds"] == expected and receipt["cleanup"]["complete"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux native runner resource-tracker isolation")
@pytest.mark.parametrize("entrypoint", ["function", "cli"])
def test_budget_only_checks_preserve_pytest_resource_tracker(
        tmp_path, monkeypatch, empty_budget_runner, entrypoint):
    from multiprocessing import resource_tracker
    resource_tracker.ensure_running()
    original_pid = resource_tracker._resource_tracker._pid
    if entrypoint == "function":
        assert runner.run_groups([], tmp_path, tmp_path / "report.json") == 0
    else:
        monkeypatch.setattr(runner, "build_groups", lambda *_args: [])
        assert runner.main(["--root", str(tmp_path)]) == 0
    resource_tracker.ensure_running()
    assert resource_tracker._resource_tracker._pid == original_pid
    assert empty_budget_runner == [[]]


def test_new_native_features_are_selected_and_backend_cannot_import_service_transports():
    assert "tests/iterations/test_native_iterations.py" in runner.SERVICE_TESTS
    assert "tests/iterations/test_native_iterations.py" in runner.SERVICE_ONLY_TESTS
    assert "tests/components/test_runner_native.py" in runner.SERVICE_TESTS
    assert "tests/components/test_runner_native.py" in runner.SERVICE_ONLY_TESTS
    files = {str(path.relative_to(ROOT)) for name in runner.LIFECYCLE_TESTS + runner.COMPONENT_TESTS + runner.CONCURRENCY_TESTS
             for path in ((ROOT / name).rglob("test_*.py") if (ROOT / name).is_dir() else [ROOT / name])
             if str(path.relative_to(ROOT)) not in runner.SERVICE_ONLY_TESTS}
    assert {
        "tests/components/test_transformers_training.py", "tests/components/test_transformers_inference.py",
        "tests/components/test_runner_contract.py", "tests/components/test_runner_accelerate.py",
        "tests/components/test_execution_rights.py",
        "tests/components/test_local_inference.py", "tests/placement/test_cuda_execution.py",
        "tests/components/test_mlx_boundaries.py", "tests/integration/test_mlx_service.py",
        "tests/concurrency/test_native_parallel.py", "tests/concurrency/test_parallel_lifecycle.py",
        "tests/concurrency/test_parallel_training.py", "tests/concurrency/test_native_crash_cleanup.py",
        "tests/concurrency/test_native_snapshot_paths.py", "tests/concurrency/test_process_execution.py",
    } <= files
    assert not set(runner.SERVICE_ONLY_TESTS) & files
    for filename in files:
        for node in ast.walk(ast.parse((ROOT / filename).read_text())):
            modules = ([entry.name for entry in node.names] if isinstance(node, ast.Import) else
                       [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert all(module != "fastapi.testclient" and module.split(".")[0] not in {"httpx", "mcp"}
                       for module in modules), filename


@pytest.mark.skipif(not (ROOT / ".venv-backend/bin/python").is_file(), reason="Pinned backend required for real native collection")
def test_actual_concurrency_collection_partitions_native_and_light_without_duplicates():
    def collect(python, marker=None):
        arguments = [str(python), "-m", "pytest", *runner.CONCURRENCY_TESTS,
                     "--collect-only", "-q", "-p", "no:cacheprovider"]
        if marker:
            arguments.extend(["-m", marker])
        result = subprocess.run(arguments, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        nodeids = [line for line in result.stdout.splitlines()
                   if line.startswith("tests/concurrency/") and "::" in line]
        assert nodeids and len(nodeids) == len(set(nodeids))
        return set(nodeids)

    backend = ROOT / ".venv-backend/bin/python"
    all_cases = collect(backend)
    native = collect(backend, "integration")
    light = collect(sys.executable, "not integration and not portability and not gui")
    assert len(native) >= 18  # Frozen G11 matrix; future native cases remain included.
    assert not native & light
    assert all_cases == native | light
    assert {nodeid.split("::", 1)[0] for nodeid in native} == {
        "tests/concurrency/test_native_parallel.py", "tests/concurrency/test_parallel_lifecycle.py",
        "tests/concurrency/test_parallel_training.py", "tests/concurrency/test_native_crash_cleanup.py",
        "tests/concurrency/test_native_snapshot_paths.py", "tests/concurrency/test_process_execution.py",
    }
    assert all(nodeid.startswith("tests/concurrency/test_process_execution.py::") for nodeid in light)


def test_explicit_hardware_requirements_are_preserved_without_becoming_defaults(tmp_path, monkeypatch):
    (tmp_path / ".venv-backend/bin").mkdir(parents=True)
    (tmp_path / ".venv-backend/bin/python").touch()
    temporary = tmp_path / "groups"
    temporary.mkdir()
    monkeypatch.setenv("MODELBUILDER_REQUIRE_CUDA", "1")
    monkeypatch.setenv("MODELBUILDER_CUDA_TEST_DEVICE", "cuda:1")
    monkeypatch.setenv("MODELBUILDER_REQUIRE_TRAINER_DEVICE", "cuda:1")
    monkeypatch.setenv("MODELBUILDER_REQUIRE_MLX", "1")
    monkeypatch.setenv("MODELBUILDER_MLX_PYTHON", "/operator/approved-mlx/bin/python")
    for group in runner.build_groups(tmp_path, Path(sys.executable), temporary):
        assert group.environment["MODELBUILDER_REQUIRE_CUDA"] == "1"
        assert group.environment["MODELBUILDER_CUDA_TEST_DEVICE"] == "cuda:1"
        assert group.environment["MODELBUILDER_REQUIRE_TRAINER_DEVICE"] == "cuda:1"
        assert group.environment["MODELBUILDER_REQUIRE_MLX"] == "1"
        assert group.environment["MODELBUILDER_MLX_PYTHON"] == "/operator/approved-mlx/bin/python"


AGGREGATE_SPY = r'''
import json, os, subprocess, sys, types
from pathlib import Path
arguments = sys.argv[1:]
with Path(os.environ["AGGREGATE_SPY_LOG"]).open("a") as stream:
    stream.write(json.dumps({"executable": sys.argv[0], "args": arguments,
        "environment": {key: value for key, value in os.environ.items() if key.startswith("MODELBUILDER_")
            or key.lower() in {"npm_config_offline", "npm_config_update_notifier", "npm_config_audit", "npm_config_fund"}}}) + "\n")
if arguments == ["-"]:
    module = types.ModuleType("modelbuilder.research_documents")
    ready = os.environ.get("SPY_DOCUMENT_TOOLS", "1") == "1"
    module.document_capabilities = lambda: {"available": ready, "ocr_available": ready, "tools": {"pdfinfo": ready}}
    sys.modules["modelbuilder"] = types.ModuleType("modelbuilder")
    sys.modules[module.__name__] = module
    subprocess.run = lambda *a, **kw: types.SimpleNamespace(returncode=0,
        stdout="eng\nosd\n" if os.environ.get("SPY_OCR_ENGLISH", "1") == "1" else "osd\n")
    exec(compile(sys.stdin.read(), "test_all.sh:document-preflight", "exec"))
if arguments[:1] == ["-c"] and os.environ.get("SPY_TRAINER", "1") != "1":
    raise SystemExit(1)
if arguments == ["run", "test:research-document-http"]:
    if os.environ.get("SPY_RESEARCH_FAIL") == "1":
        raise SystemExit(3)
    Path(os.environ["MODELBUILDER_RESEARCH_DOCUMENT_REPORT"]).write_text('{"status":"PASS","scope":"test double"}')
if "tests/test_remote_modal_sdk.py" in arguments and os.environ.get("SPY_MODAL_FAIL") == "1":
    raise SystemExit(4)
for argument in arguments:
    if argument.startswith("--junitxml="):
        Path(argument.split("=", 1)[1]).write_text('<testsuite tests="1"/>')
'''


def aggregate_harness(tmp_path, *, integration=True, modal=True, processes=1, **changes):
    """Run the real shell control flow with command spies, never native training."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/test_all.sh").write_text((ROOT / "scripts/test_all.sh").read_text())
    package = root / "packages/ide_feature"
    package.mkdir(parents=True)
    (package / "package-lock.json").write_text("{}")
    executables = [".venv/bin/python", ".venv-backend/bin/python", "bin/npm"]
    if modal:
        executables.append(".venv-remote-modal/bin/python")
    for filename in executables:
        path = root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!{sys.executable}\n" + AGGREGATE_SPY)
        path.chmod(0o755)
    evidence = root / "evidence/tests"
    evidence.mkdir(parents=True)
    (evidence / "research-document-http.json").write_text('{"status":"PASS","scope":"stale"}')
    (evidence / "remote-modal-sdk.xml").write_text('<testsuite tests="100"/>')
    (evidence / "cpu-concurrency.xml").write_text('<testsuite tests="100"/>')
    (evidence / "cpu-concurrency.log").write_text('previous passing concurrency run')
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("MODELBUILDER_", "SPY_"))}
    environment.update(PATH=str(root / "bin") + os.pathsep + os.environ["PATH"],
        AGGREGATE_SPY_LOG=str(root / "commands.jsonl"), MODELBUILDER_RUN_INTEGRATION=str(int(integration)),
        MODELBUILDER_NATIVE_TEST_PROCESSES=str(processes), **changes)
    result = subprocess.run(["bash", "scripts/test_all.sh"], cwd=root, env=environment,
                            capture_output=True, text=True, timeout=30)
    commands = [json.loads(line) for line in (root / "commands.jsonl").read_text().splitlines()]
    return result, root, commands


@pytest.mark.parametrize("integration", [False, True])
@pytest.mark.parametrize("inherited", ["absent", "lowercase", "uppercase"])
def test_local_ci_npm_invocations_inherit_offline_maintenance_settings(tmp_path, monkeypatch, integration, inherited):
    expected = {"npm_config_offline": "true", "npm_config_update_notifier": "false",
                "npm_config_audit": "false", "npm_config_fund": "false"}
    for key in expected:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.upper(), raising=False)
    conflicting = {} if inherited == "absent" else {
        key.upper() if inherited == "uppercase" else key: "false" if value == "true" else "true"
        for key, value in expected.items()}
    result, root, commands = aggregate_harness(tmp_path, integration=integration, processes=2, **conflicting)
    assert result.returncode == 0, result.stdout + result.stderr
    npm = [row for row in commands if row["executable"] == str(root / "bin/npm")]
    assert [row["args"] for row in npm] == [
        ["run", "check"], ["run", "compile"], ["test"], ["run", "test:service"],
        ["run", "test:workspace-http"], ["run", "test:source-pin-http"], ["run", "test:history-http"],
        *([["run", "test:research-document-http"], ["run", "test:component-http"]] if integration else [])]
    for row in npm:
        assert {key: row["environment"].get(key) for key in expected} == expected
        assert not any(key.upper() in row["environment"] for key in expected)


@pytest.mark.parametrize("processes", [1, 2])
def test_aggregate_runs_research_and_offline_sdk_before_native_with_separate_interpreters(tmp_path, processes):
    result, root, commands = aggregate_harness(tmp_path, processes=processes)
    assert result.returncode == 0, result.stdout + result.stderr
    research = next(row for row in commands if row["args"] == ["run", "test:research-document-http"])
    modal = next(row for row in commands if "tests/test_remote_modal_sdk.py" in row["args"])
    service = next(row for row in commands if "--ignore=tests/test_remote_modal_sdk.py" in row["args"])
    assert service["executable"] == str(root / ".venv/bin/python")
    assert research["environment"]["MODELBUILDER_GUI_PYTHON"] == service["executable"]
    assert modal["executable"] == str(root / ".venv-remote-modal/bin/python")
    assert modal["environment"]["MODELBUILDER_REQUIRE_MODAL_SDK"] == "1"
    assert "MODELBUILDER_REQUIRE_MODAL_SDK" not in service["environment"]
    assert all("MODELBUILDER_REQUIRE_CUDA" not in row["environment"] for row in commands)
    assert all("MODELBUILDER_REQUIRE_MLX" not in row["environment"] for row in commands)
    assert commands.index(research) < commands.index(modal)
    if processes == 1:
        backend = next(row for row in commands if "tests/integration/test_cpu_fixtures.py" in row["args"])
        assert backend["executable"] == str(root / ".venv-backend/bin/python")
        assert backend["environment"]["MODELBUILDER_REQUIRE_TRAINER"] == "1"
        assert backend["environment"]["MODELBUILDER_REQUIRE_LOCAL_INFERENCE"] == "1"
        assert "tests/integration/test_mlx_service.py" in backend["args"]
        assert all(f"--ignore={path}" in backend["args"] for path in runner.SERVICE_ONLY_TESTS)
        integration = next(row for row in commands if "tests/iterations/test_native_iterations.py" in row["args"])
        assert integration["executable"] == service["executable"]
        assert "MODELBUILDER_REQUIRE_TRAINER" not in integration["environment"]
        assert commands.index(modal) < commands.index(backend)
        concurrency = [row for row in commands if "tests/concurrency" in row["args"]]
        assert len(concurrency) == 1
        assert concurrency[0]["executable"] == backend["executable"]
        assert concurrency[0]["environment"]["MODELBUILDER_REQUIRE_TRAINER"] == "1"
        assert concurrency[0]["environment"]["MODELBUILDER_REQUIRE_LOCAL_INFERENCE"] == "1"
        assert concurrency[0]["args"][3:5] == ["-m", "integration"]
        assert commands.index(backend) < commands.index(concurrency[0])
    else:
        queue = next(row for row in commands if "scripts/native_test_runner.py" in row["args"])
        assert commands.index(modal) < commands.index(queue)


@pytest.mark.parametrize("missing", ["SPY_DOCUMENT_TOOLS", "SPY_OCR_ENGLISH"])
def test_missing_research_dependencies_block_integration_and_replace_stale_pass(tmp_path, missing):
    result, root, commands = aggregate_harness(tmp_path, **{missing: "0"})
    assert result.returncode == 2 and "BLOCKED: Research" in result.stderr
    receipt = json.loads((root / "evidence/tests/research-document-http.json").read_text())
    assert receipt["status"] == "BLOCKED" and not receipt["ocr_english_data_verified"]
    assert not any(row["args"] == ["run", "test:research-document-http"] for row in commands)
    assert not any("tests/integration/test_cpu_fixtures.py" in row["args"] for row in commands)
    assert not (root / "evidence/tests/remote-modal-sdk.xml").exists()
    assert not (root / "evidence/tests/cpu-concurrency.xml").exists()
    assert not (root / "evidence/tests/cpu-concurrency.log").exists()


@pytest.mark.parametrize("required", [False, True])
def test_absent_optional_modal_sdk_is_reported_without_stale_acceptance(tmp_path, required):
    result, root, commands = aggregate_harness(tmp_path, modal=False, MODELBUILDER_REQUIRE_MODAL_SDK=str(int(required)))
    assert result.returncode == (2 if required else 0), result.stdout + result.stderr
    log = (root / "evidence/tests/remote-modal-sdk.log").read_text()
    assert ("BLOCKED:" if required else "NOT RUN:") in log
    assert not (root / "evidence/tests/remote-modal-sdk.xml").exists()
    assert not any("tests/test_remote_modal_sdk.py" in row["args"] for row in commands)


@pytest.mark.parametrize("failure,code", [("SPY_TRAINER", 2), ("SPY_RESEARCH_FAIL", 3), ("SPY_MODAL_FAIL", 4)])
def test_acceptance_dependency_or_stage_failure_cannot_reach_native_queue(tmp_path, failure, code):
    result, root, commands = aggregate_harness(tmp_path, processes=2, **{failure: "0" if failure == "SPY_TRAINER" else "1"})
    assert result.returncode == code, result.stdout + result.stderr
    assert not any("scripts/native_test_runner.py" in row["args"] for row in commands)
    assert not any(row["args"] == ["run", "test:component-http"] for row in commands)
    assert not (root / "evidence/tests/remote-modal-sdk.xml").exists()
    assert not (root / "evidence/tests/cpu-concurrency.xml").exists()
    assert not (root / "evidence/tests/cpu-concurrency.log").exists()
    if failure == "SPY_TRAINER":
        assert not (root / "evidence/tests/research-document-http.json").exists()


def test_light_aggregate_does_not_claim_or_require_native_dependencies(tmp_path):
    result, _root, commands = aggregate_harness(tmp_path, integration=False, modal=False, SPY_DOCUMENT_TOOLS="0", SPY_TRAINER="0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any(row["args"] == ["-"] or "-c" in row["args"] for row in commands)
    assert not any(row["args"] == ["run", "test:research-document-http"] for row in commands)
    assert not any("tests/test_remote_modal_sdk.py" in row["args"] for row in commands)


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
if mode=='wait_last':
    while not any(json.loads(line)[:2]==['last','start'] for line in (root/'events').read_text().splitlines()):time.sleep(.01)
time.sleep(delay)
event('end')
print('group output '+name)
if mode!='missing':(root/(name+'.xml')).write_text('<testsuite tests="1"/>')
'''


def launch(tmp_path, specs, *, timeout=5, continue_on_failure=False):
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER)
    bootstrap = tmp_path / "runner.py"
    bootstrap.write_text('''import importlib.util,json,os,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("native_test_runner",sys.argv[1]);m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
root=Path(sys.argv[2]);groups=[m.Group(name,(sys.executable,str(root/'worker.py'),mode,name,str(root),str(delay)),dict(os.environ),root/(name+'.log'),root/(name+'.xml')) for name,mode,delay in json.loads(sys.argv[3])]
raise SystemExit(m.run_groups(groups,root,root/'report.json',timeout=float(sys.argv[4]),continue_on_failure=sys.argv[5]=='1'))
''')
    return subprocess.Popen([sys.executable, "-I", str(bootstrap), str(ROOT / "scripts/native_test_runner.py"),
                             str(tmp_path), json.dumps(specs), str(timeout), str(int(continue_on_failure))], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)


def finish(process, tmp_path, timeout=12):
    output, error = process.communicate(timeout=timeout)
    return process.returncode, json.loads((tmp_path / "report.json").read_text()), output + error


@pytest.mark.skipif(sys.platform != "linux", reason="Optional concurrent runner uses Linux descendant containment")
def test_four_complete_groups_never_exceed_two_slots_and_keep_logs(tmp_path):
    process = launch(tmp_path, [("long", "wait_last", .1), ("short", "pass", .15), ("next", "pass", .1), ("last", "pass", .1)])
    code, report, _ = finish(process, tmp_path)
    assert code == 0 and report["status"] == "completed" and report["cleanup"]["complete"]
    events = [json.loads(line) for line in (tmp_path / "events").read_text().splitlines()]
    active, high = set(), 0
    for name, kind, _ in events:
        active.add(name) if kind == "start" else active.remove(name)
        high = max(high, len(active))
    assert high == 2 and not active
    lookup = {(name, kind): timestamp for name, kind, timestamp in events}
    assert lookup['short', 'end'] <= lookup['next', 'start'] < lookup['next', 'end'] <= lookup['last', 'start'] < lookup['long', 'end']
    assert all(record["status"] == "passed" for record in report["groups"])
    for name in ("long", "short", "next", "last"):
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
    for name in ("cpu-fixtures", "cpu-lifecycle", "integration", "cpu-components", "cpu-concurrency"):
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


def test_scientific_native_node_ids_have_exact_serial_concurrent_routes():
    native_path = "tests/scientific/test_native_derivation.py"
    tensor_path = "tests/scientific/test_derivation.py"
    for path in (native_path, tensor_path):
        assert runner.COMPONENT_TESTS.count(path) == 1
        assert serial_paths("-m pytest tests/integration/test_cpu_fixtures.py").count(path) == 1
        assert path not in runner.SERVICE_TESTS and path not in runner.SERVICE_ONLY_TESTS
    tree = ast.parse((ROOT / native_path).read_text())
    definitions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")}
    plain = "test_actual_native_blend_and_evaluation_owner"
    compared = "test_cross_run_derived_comparison_policy_promotion_and_integrity"
    assert set(definitions) == {plain, compared}
    for node in definitions.values():
        assert any(isinstance(marker, ast.Attribute) and marker.attr == "integration" for marker in node.decorator_list)
    marker = next(item for item in definitions[compared].decorator_list if isinstance(item, ast.Call) and getattr(item.func, "attr", None) == "parametrize")
    cases = ast.literal_eval(marker.args[1])
    node_ids = {f"{native_path}::{plain}", *(f"{native_path}::{compared}[{'-'.join(map(str, case))}]" for case in cases)}
    assert node_ids == {
        f"{native_path}::{plain}", f"{native_path}::{compared}[False-False]",
        f"{native_path}::{compared}[True-False]", f"{native_path}::{compared}[False-True]",
    }


@pytest.mark.skipif(sys.platform != "linux", reason="Concurrent runner requires Linux containment")
def test_diagnostic_collection_runs_remaining_groups_but_keeps_failure(tmp_path):
    process = launch(tmp_path, [("broken", "missing", 0), ("slow", "pass", .3),
                               ("next", "pass", 0), ("last", "pass", 0)], continue_on_failure=True)
    code, report, _ = finish(process, tmp_path)
    assert code == 1 and report["status"] == "group_failed"
    assert report["continue_on_failure"] is True and report["cleanup"]["complete"]
    states = {row["name"]: row["status"] for row in report["groups"]}
    assert states == {"broken": "failed", "slow": "passed", "next": "passed", "last": "passed"}


@pytest.mark.skipif(sys.platform != "linux", reason="Concurrent runner requires Linux containment")
def test_diagnostic_collection_still_obeys_deadline_and_reaps_children(tmp_path):
    process = launch(tmp_path, [("native", "descendant", 0), ("broken", "missing", 0)],
                     timeout=.4, continue_on_failure=True)
    code, report, _ = finish(process, tmp_path)
    assert code == 124 and report["status"] == "deadline_exceeded"
    assert report["cleanup"]["complete"]
    assert not Path(f"/proc/{int((tmp_path / 'detached.pid').read_text())}").exists()


def test_atomic_receipt_orders_file_sync_replace_and_directory_sync(tmp_path, monkeypatch):
    import stat
    report = tmp_path / "report.json"
    report.write_text('{"old": true}')
    calls = []
    real_sync, real_replace = runner.os.fsync, runner.os.replace
    def sync(descriptor):
        calls.append("directory_sync" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file_sync")
        real_sync(descriptor)
    def replace(source, destination):
        assert Path(source).parent == report.parent
        assert json.loads(report.read_text()) == {"old": True}
        assert json.loads(Path(source).read_text()) == {"new": True}
        calls.append("replace")
        real_replace(source, destination)
    monkeypatch.setattr(runner.os, "fsync", sync)
    monkeypatch.setattr(runner.os, "replace", replace)
    runner._write_report(report, {"new": True})
    assert calls == ["file_sync", "replace", "directory_sync"]
    assert json.loads(report.read_text()) == {"new": True}
    assert sorted(path.name for path in tmp_path.iterdir()) == ["report.json"]


@pytest.mark.parametrize("phase", ["serialization", "file_sync", "replace", "directory_sync"])
def test_atomic_receipt_failure_never_leaves_partial_json(tmp_path, monkeypatch, phase):
    report = tmp_path / "report.json"
    original = {"finalized": False, "groups": [{"name": "completed", "returncode": 0}]}
    runner._write_report(report, original)
    replacement = {"finalized": False, "groups": [{"name": "completed", "returncode": 7}]}
    real_sync = runner.os.fsync
    calls = 0
    def sync(descriptor):
        nonlocal calls
        calls += 1
        if phase == ("file_sync" if calls == 1 else "directory_sync"):
            raise OSError("injected receipt I/O failure")
        real_sync(descriptor)
    def replace(*_args):
        raise OSError("injected replacement failure")
    if phase == "serialization":
        replacement["invalid"] = float("nan")
    elif phase == "replace":
        monkeypatch.setattr(runner.os, "replace", replace)
    else:
        monkeypatch.setattr(runner.os, "fsync", sync)
    with pytest.raises((ValueError, OSError)):
        runner._write_report(report, replacement)
    # Once replace succeeds, a later directory-fsync failure cannot roll back.
    assert json.loads(report.read_text()) == (replacement if phase == "directory_sync" else original)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["report.json"]


def test_concurrent_receipt_reader_sees_only_complete_documents(tmp_path):
    from threading import Event, Thread
    report = tmp_path / "report.json"
    runner._write_report(report, {"generation": 0, "rows": [0] * 2000})
    stop, observations, errors = Event(), [], []
    def read():
        while not stop.is_set():
            try:
                value = json.loads(report.read_text())
                assert value["rows"] == [value["generation"]] * 2000
                observations.append(value["generation"])
            except Exception as error:
                errors.append(error)
                break
    thread = Thread(target=read)
    thread.start()
    try:
        for generation in range(1, 25):
            runner._write_report(report, {"generation": generation, "rows": [generation] * 2000})
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and observations and not errors
    assert json.loads(report.read_text())["generation"] == 24


def test_junit_identity_is_bounded_diagnostic_and_contains_no_xml(tmp_path, monkeypatch):
    import hashlib
    path = tmp_path / "group.xml"
    assert runner._junit_evidence(path) == {"status": "missing"}
    content = b'<testsuite tests="1"><failure>private test content</failure></testsuite>'
    path.write_bytes(content)
    assert runner._junit_evidence(path) == {
        "status": "present", "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    monkeypatch.setattr(runner, "MAX_JUNIT_EVIDENCE_BYTES", 8)
    value = runner._junit_evidence(path)
    assert value == {"status": "present", "sha256": None, "hash_unavailable": "size_limit", "size_bytes": len(content)}
    assert "private" not in json.dumps(value)


def test_heartbeat_preserves_completed_groups_and_marks_unfinished_state(tmp_path, monkeypatch):
    from types import SimpleNamespace
    clock, snapshots = [0.0], []
    monkeypatch.setattr(runner, "time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda _seconds: clock.__setitem__(0, clock[0] + 31)))
    monkeypatch.setattr(runner, "_subreaper", lambda: None)
    monkeypatch.setattr(runner, "_cleanup", lambda _processes: {"complete": True, "signaled_processes": 0})
    class Process:
        def __init__(self, command, **_kwargs):
            self.name, self.pid = command[0], {"fast": 100, "slow": 101, "pending": 102}[command[0]]
            self.returncode = None
            (tmp_path / (self.name + ".xml")).write_text('<testsuite tests="1"/>')
        def poll(self):
            if self.name == "fast" or clock[0] >= 62:
                self.returncode = 0
            return self.returncode
    monkeypatch.setattr(runner, "subprocess", SimpleNamespace(Popen=Process, DEVNULL=-1, STDOUT=-2))
    original = runner._write_report
    def write(path, value):
        snapshots.append(json.loads(json.dumps(value)))
        original(path, value)
    monkeypatch.setattr(runner, "_write_report", write)
    groups = [runner.Group(name, (name,), {}, tmp_path / (name + ".log"), tmp_path / (name + ".xml"))
              for name in ("fast", "slow", "pending")]
    assert runner.run_groups(groups, tmp_path, tmp_path / "report.json", timeout=200) == 0
    heartbeats = [item for item in snapshots if item.get("elapsed_seconds") in {0, 31}
                  and item["status"] == "running"
                  and item["groups"][0].get("junit", {}).get("status") == "present"]
    assert {item["elapsed_seconds"] for item in heartbeats} == {0, 31}
    assert all(not item["finalized"] and item["exit_code"] is None and item["cleanup"] is None for item in heartbeats)
    assert all(item["groups"][0] == heartbeats[0]["groups"][0] for item in heartbeats)
    assert any(item["groups"][-1]["status"] == "not_started" for item in heartbeats)
    assert any(item["groups"][1]["status"] == "running" for item in heartbeats)
    assert len({item["run_id"] for item in snapshots}) == 1
    assert all(item["updated_at"].endswith("+00:00") for item in snapshots)
    final = snapshots[-1]
    assert final["finalized"] and final["exit_code"] == 0 and final["cleanup"]["complete"]
    assert all(row["status"] == "passed" for row in final["groups"])


@pytest.mark.skipif(sys.platform != "linux", reason="Actual Linux supervisor interruption")
@pytest.mark.parametrize("returncode", [0, 7])
def test_killed_supervisor_keeps_completed_evidence_without_final_acceptance(tmp_path, returncode):
    import hashlib
    # The actual tiny group exits and is reaped before the cleanup seam pauses.
    # Killing this supervisor therefore leaves no fixture worker to orphan.
    bootstrap = tmp_path / "pause_before_cleanup.py"
    bootstrap.write_text('''import importlib.util,os,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location("progress_runner",sys.argv[1]);m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
root=Path(sys.argv[2])
def cleanup(processes):
    assert all(process.poll() is not None for process in processes)
    (root/'before-cleanup').write_text('all test children exited and reaped')
    while True: time.sleep(.1)
m._cleanup=cleanup
code="from pathlib import Path;Path('completed.xml').write_text('<testsuite tests=\\\"1\\\"/>')"
code += ';raise SystemExit(' + sys.argv[3] + ')'
group=m.Group('completed',(sys.executable,'-c',code),dict(os.environ),root/'completed.log',root/'completed.xml')
raise SystemExit(m.run_groups([group],root,root/'report.json',timeout=5))
''')
    process = subprocess.Popen([sys.executable, "-I", str(bootstrap), str(ROOT / "scripts/native_test_runner.py"), str(tmp_path), str(returncode)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "before-cleanup").is_file() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert (tmp_path / "before-cleanup").is_file(), process.poll()
        before = (tmp_path / "report.json").read_bytes()
        process.kill()
        output, error = process.communicate(timeout=5)
        assert process.returncode == -signal.SIGKILL, output + error
        assert (tmp_path / "report.json").read_bytes() == before
        receipt = json.loads(before)
        assert receipt["status"] == "finalizing" and not receipt["finalized"]
        assert receipt["exit_code"] is None and receipt["cleanup"] is None
        row = receipt["groups"][0]
        assert row["status"] == ("passed" if returncode == 0 else "failed") and row["returncode"] == returncode
        assert row["junit"]["sha256"] == hashlib.sha256((tmp_path / "completed.xml").read_bytes()).hexdigest()
        assert row["junit"]["size_bytes"] == (tmp_path / "completed.xml").stat().st_size
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_progress_io_failure_does_not_change_cleanup_or_exit(tmp_path, monkeypatch, empty_budget_runner):
    original = runner._write_report
    calls = 0
    def write(path, value):
        nonlocal calls
        calls += 1
        if value["status"] == "finalizing":
            raise OSError("injected temporary progress write failure")
        original(path, value)
    monkeypatch.setattr(runner, "_write_report", write)
    report = tmp_path / "report.json"
    assert runner.run_groups([], tmp_path, report) == 0
    assert empty_budget_runner == [[]] and calls == 3
    assert json.loads(report.read_text())["finalized"] is True


def test_final_receipt_failure_cannot_skip_cleanup_or_handler_restoration(tmp_path, monkeypatch, empty_budget_runner):
    original = runner._write_report
    handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    def write(path, value):
        if value["finalized"]:
            raise OSError("injected final receipt failure")
        original(path, value)
    monkeypatch.setattr(runner, "_write_report", write)
    report = tmp_path / "report.json"
    with pytest.raises(OSError):
        runner.run_groups([], tmp_path, report)
    assert empty_budget_runner == [[]]
    assert {signum: signal.getsignal(signum) for signum in handlers} == handlers
    assert json.loads(report.read_text())["finalized"] is False


def test_cli_finalization_error_preserves_progress_and_unconfirmed_state(tmp_path, monkeypatch):
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(runner.tempfile, "mkdtemp", lambda **_kwargs: str(temporary))
    monkeypatch.setattr(runner, "build_groups", lambda *_args: [])
    completed = {"name": "completed", "pid": 123, "status": "passed", "returncode": 0,
                 "junit": {"status": "present", "sha256": "a" * 64, "size_bytes": 30}}
    def fail(_groups, _root, report, **kwargs):
        runner._write_report(report, {**runner._receipt_fields(kwargs["run_id"]), "status": "finalizing",
                                     "groups": [completed, {"name": "pending", "status": "not_started"}]})
        raise RuntimeError("private error details must not be copied into the receipt")
    monkeypatch.setattr(runner, "run_groups", fail)
    assert runner.main(["--root", str(tmp_path)]) == 1
    receipt = json.loads((tmp_path / "evidence/tests/native-runner.json").read_text())
    assert receipt["status"] == "runner_error" and receipt["error"] == "RuntimeError"
    assert receipt["groups"][0] == completed and receipt["groups"][1]["status"] == "not_started"
    assert not receipt["finalized"] and receipt["exit_code"] is None and receipt["cleanup"] is None
    assert receipt["retained_temporary_directory"] == str(temporary) and temporary.is_dir()
    assert "private error" not in json.dumps(receipt)


@pytest.mark.skipif(sys.platform != "linux", reason="Nonblocking FIFO evidence on native runner")
def test_junit_fifo_cannot_block_queue_cleanup(tmp_path):
    path = tmp_path / "group.xml"
    os.mkfifo(path)
    code = '''import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('fifo_runner',sys.argv[1]);m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
assert m._junit_evidence(Path(sys.argv[2])) == {'status':'unavailable','error':'nonregular_file'}
'''
    result = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT / "scripts/native_test_runner.py"), str(path)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("error_write_fails", [False, True])
def test_unpersisted_launch_state_cannot_be_mistaken_for_setup_failure(tmp_path, monkeypatch, error_write_fails):
    from types import SimpleNamespace
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(runner.tempfile, "mkdtemp", lambda **_kwargs: str(temporary))
    group = runner.Group("completed", ("worker",), {}, tmp_path / "group.log", tmp_path / "group.xml")
    monkeypatch.setattr(runner, "build_groups", lambda *_args: [group])
    monkeypatch.setattr(runner, "_subreaper", lambda: None)
    class Process:
        pid = 321
        returncode = 0
        def __init__(self, *_args, **_kwargs):
            group.xml.write_text('<testsuite tests="1"/>')
        def poll(self):
            return self.returncode
    monkeypatch.setattr(runner, "subprocess", SimpleNamespace(Popen=Process, DEVNULL=-1, STDOUT=-2))
    def cleanup(_processes):
        raise RuntimeError("injected unconfirmed cleanup")
    monkeypatch.setattr(runner, "_cleanup", cleanup)
    original = runner._write_report
    def write(path, value):
        if value["status"] in {"running", "finalizing"}:
            raise OSError("all progress writes fail after initial snapshot")
        if error_write_fails and value["status"] == "runner_error" and "retained_temporary_directory" not in value:
            raise OSError("error report unavailable until retention write")
        original(path, value)
    monkeypatch.setattr(runner, "_write_report", write)
    handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    if error_write_fails:
        with pytest.raises(OSError, match="until retention write"):
            runner.main(["--root", str(tmp_path)])
    else:
        assert runner.main(["--root", str(tmp_path)]) == 1
    receipt = json.loads((tmp_path / "evidence/tests/native-runner.json").read_text())
    assert receipt["status"] == "runner_error" and not receipt["finalized"]
    assert receipt["cleanup"] is None and receipt["exit_code"] is None
    assert receipt["groups"][0]["status"] == "passed" and receipt["groups"][0]["returncode"] == 0
    assert receipt["groups"][0]["junit"]["status"] == "present"
    assert temporary.is_dir() and receipt["retained_temporary_directory"] == str(temporary)
    assert {signum: signal.getsignal(signum) for signum in handlers} == handlers


@pytest.mark.skipif(sys.platform != "linux", reason="Actual tiny Linux queue receipt lifecycle")
def test_running_snapshot_replaces_stale_acceptance_and_preserves_completed_work_on_signal(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"run_id": "old-run", "finalized": True, "exit_code": 0,
                                  "cleanup": {"complete": True}, "groups": [{"name": "old", "status": "passed"}]}))
    process = launch(tmp_path, [("fast", "pass", 0), ("slow", "pass", 3), ("waiting", "pass", 3)])
    try:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and process.poll() is None:
            receipt = json.loads(report.read_text())
            rows = {row["name"]: row for row in receipt.get("groups", [])}
            if rows.get("fast", {}).get("junit", {}).get("status") == "present":
                break
            time.sleep(.01)
        assert rows["fast"]["status"] == "passed" and rows["fast"]["returncode"] == 0
        assert any(row["status"] in {"running", "not_started", "starting"} for name, row in rows.items() if name != "fast")
        assert receipt["run_id"] != "old-run" and "old" not in rows
        assert not receipt["finalized"] and receipt["exit_code"] is None and receipt["cleanup"] is None
        process.send_signal(signal.SIGTERM)
        code, final, _ = finish(process, tmp_path)
        assert code == 143 and final["status"] == "interrupted" and final["finalized"]
        assert final["cleanup"]["complete"] and final["run_id"] == receipt["run_id"]
        assert next(row for row in final["groups"] if row["name"] == "fast") == rows["fast"]
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=12)


def test_progress_and_stderr_failures_cannot_bypass_cleanup(tmp_path, monkeypatch, empty_budget_runner):
    from types import SimpleNamespace
    original = runner._write_report
    handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    def write(path, value):
        if value["status"] == "finalizing":
            raise OSError("receipt unavailable")
        original(path, value)
    def broken(*_args):
        raise BrokenPipeError("closed diagnostic pipe")
    monkeypatch.setattr(runner, "_write_report", write)
    monkeypatch.setattr(runner.sys, "stderr", SimpleNamespace(write=broken, flush=broken))
    with pytest.raises(BrokenPipeError):
        runner.run_groups([], tmp_path, tmp_path / "report.json")
    assert empty_budget_runner == [[]]
    assert {signum: signal.getsignal(signum) for signum in handlers} == handlers
    assert json.loads((tmp_path / "report.json").read_text())["finalized"] is False


def test_post_replace_final_sync_failure_requires_successful_supervisor_exit(
        tmp_path, monkeypatch, empty_budget_runner):
    original = runner._write_report
    real_sync = runner.os.fsync
    def write(path, value):
        if not value["finalized"]:
            return original(path, value)
        calls = 0
        def sync(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("final directory fsync failed after replacement")
            real_sync(descriptor)
        with monkeypatch.context() as patch:
            patch.setattr(runner.os, "fsync", sync)
            original(path, value)
    monkeypatch.setattr(runner, "_write_report", write)
    report = tmp_path / "report.json"
    with pytest.raises(OSError, match="after replacement"):
        runner.run_groups([], tmp_path, report)
    assert empty_budget_runner == [[]]
    # This is a fully formed replacement, but the supervisor did not succeed.
    # Consumers need both actual process exit success and the final receipt.
    receipt = json.loads(report.read_text())
    assert receipt["finalized"] and receipt["exit_code"] == 0 and receipt["cleanup"]["complete"]
