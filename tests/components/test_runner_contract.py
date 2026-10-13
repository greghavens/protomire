"""No-installer runner contracts and real fresh-process, framework-neutral smoke."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from modelbuilder.backend import runner
from modelbuilder.backend.components import validate_component
from modelbuilder.backend.operator_config import load_operator_config
from modelbuilder.backend.process_execution import execute


def descriptor(distribution="jsonschema", version=None):
    import importlib.metadata
    return {"protocol": runner.PROTOCOL,
        "stack": [{"id": "test-harness", "category": "training-library", "distribution": distribution,
                   "version": version or importlib.metadata.version(distribution)}],
        "launcher": {"kind": "python", "processes": 1},
        "contracts": {"model": "source-owned-native", "dataset": "native-component-inputs",
                      "checkpoint": "modelbuilder.checkpoint/v1", "evaluation": "modelbuilder.evaluation/v1"}}


def node():
    return {"id": "adapter", "kind": "component.run", "implementation_ref": {}, "inputs": [],
        "outputs": [{"name": "result", "type": "dict"}], "parameters": {}, "environment_ref": "isolated",
        "component": {"id": "test.runner", "version": "1", "protocol": "modelbuilder.component/v1",
            "role": "utility", "parameter_schema": {"type": "object", "additionalProperties": False},
            "capabilities": {"frameworks": [], "resume_modes": [], "export_formats": [], "devices": ["cpu"]},
            "runner": descriptor()}}


def setup(tmp_path, *, code=None, python=None):
    python = python or sys.executable
    profile = runner.inspect_environment(python)
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(profile, sort_keys=True))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    candidate = node()
    recipe = {"nodes": [candidate], "resources": [{"id": "isolated", "kind": "environment", "sha256": digest}],
              "environment": {"device": "cpu"}}
    backend = SimpleNamespace(dependency_paths={"isolated": str(path)},
        runner_environments={"isolated": {"python_executable": python, "profile_sha256": digest}})
    if code is not None:
        source = tmp_path / "source"
        source.mkdir()
        (source / "adapter.py").write_text(code)
        ref = {"path": "adapter.py", "sha256": hashlib.sha256(code.encode()).hexdigest(),
               "module": "test_user_runner", "entrypoint": "run", "commit": "1" * 40}
        candidate["implementation_ref"] = ref
        recipe["source"] = {"commit": ref["commit"], "files": [{"path": ref["path"], "sha256": ref["sha256"]}]}
        backend.repository = source
        backend.trusted_code_roots = [str(source)]
    return backend, recipe, profile


def spec_for(tmp_path, backend, recipe):
    candidate = recipe["nodes"][0]
    routes = runner.resolve_environments(backend, recipe)
    return {"job_dir": str(tmp_path / "job"), "node_id": candidate["id"], "component_node": candidate,
        "parameters": candidate["parameters"], "source_root": str(backend.repository), "source": recipe["source"],
        "trusted_code_roots": backend.trusted_code_roots, "placement": {"device": "cpu"},
        "environment": recipe["environment"], "dependencies": recipe["resources"],
        "dependency_paths": backend.dependency_paths, "runner_environment": routes[candidate["id"]]}


def isolated_snapshot():
    # The runtime captures before source imports in a fresh -I process. Broad
    # pytest collection may import setuptools, which exposes its vendored
    # packaging metadata on sys.path; that is deliberately not a runner profile.
    program = ("import json,sys; sys.path.insert(0,sys.argv[1]); "
               "from modelbuilder.backend.runner import environment_snapshot; "
               "print(json.dumps(environment_snapshot()))")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", program,
                             str(Path(runner.__file__).resolve().parents[2])],
                            check=True, capture_output=True, text=True, timeout=30)
    return json.loads(result.stdout)


def test_environment_snapshot_and_isolated_probe_agree():
    assert isolated_snapshot() == runner.inspect_environment(sys.executable)


def test_environment_snapshot_rejects_ambiguous_distribution_metadata(monkeypatch):
    import importlib.metadata
    distribution = importlib.metadata.distribution("jsonschema")
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: [distribution, distribution])
    with pytest.raises(ValueError, match="ambiguous duplicate distribution metadata: jsonschema"):
        runner.environment_snapshot()


def test_inert_component_descriptor_and_catalog_do_not_import_frameworks():
    validate_component(node())
    code = "from modelbuilder.backend.runner_compatibility import compatibility_catalog; import sys; assert len(compatibility_catalog()) == 16; assert not {'torch','transformers','tensorflow','jax'} & sys.modules.keys()"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("change", [
    lambda n: n.update(environment_ref="default"),
    lambda n: n["component"]["capabilities"].update(resume_modes=["exact_state"]),
    lambda n: n["component"].update(role="training"),
    lambda n: n["component"]["runner"]["stack"].append(deepcopy(n["component"]["runner"]["stack"][0])),
    lambda n: n["component"]["runner"].update(executable="/tmp/untrusted"),
    lambda n: n["component"]["runner"]["stack"][0].update(version=">=1"),
    lambda n: n["component"]["runner"]["launcher"].update(processes=True),
])
def test_descriptor_fails_closed(change):
    candidate = node()
    change(candidate)
    with pytest.raises(Exception):
        validate_component(candidate)


@pytest.mark.parametrize("mutation,match", [
    (lambda b, r, p: b.runner_environments.clear(), "operator-registered"),
    (lambda b, r, p: r["resources"][0].update(sha256="0" * 64), "operator-approved"),
    (lambda b, r, p: r["environment"].update(device="cuda:0"), "Linux CPU"),
    (lambda b, r, p: r.update(evaluation={"live": {"enabled": True}}), "live evaluation"),
    (lambda b, r, p: r["nodes"][0]["component"]["runner"]["launcher"].update(kind="accelerate"), "launchers"),
    (lambda b, r, p: r["nodes"][0]["component"]["runner"]["launcher"].update(processes=2), "launchers"),
    (lambda b, r, p: r["nodes"][0]["component"]["runner"]["stack"][0].update(version="0.0.0"), "differs"),
    (lambda b, r, p: r["nodes"][0]["component"]["runner"]["stack"][0].update(category="distributed-strategy"), "Distributed"),
])
def test_selection_is_feature_specific_and_fail_closed(tmp_path, mutation, match):
    backend, recipe, profile = setup(tmp_path)
    mutation(backend, recipe, profile)
    with pytest.raises(ValueError, match=match):
        runner.resolve_environments(backend, recipe)


@pytest.mark.parametrize("field,value", [("python", "3.12.1"), ("executable_sha256", "0" * 64),
                                         ("platform", "darwin"), ("machine", "another-machine")])
def test_environment_identity_drift_rejected(field, value):
    original = isolated_snapshot()
    changed = {**original, field: value}
    with pytest.raises(ValueError, match="differs"):
        runner.verify_snapshot(original, changed)


def test_environment_extra_missing_and_wrong_package_rejected():
    original = isolated_snapshot()
    for packages in ({**original["packages"], "unexpected-package": "1.0"},
                     {k: v for k, v in original["packages"].items() if k != "pytest"},
                     {**original["packages"], "jsonschema": "0.0.0"}):
        with pytest.raises(ValueError, match="differs"):
            runner.verify_snapshot(original, {**original, "packages": packages})


def test_operator_config_requires_receipt_and_preserves_venv_symlink(tmp_path):
    interpreter = tmp_path / "venv-python"
    interpreter.symlink_to(sys.executable)
    path = tmp_path / "operator.json"
    path.write_text(json.dumps({"runner_environments": {"torch-test": {
        "python_executable": "venv-python", "profile_sha256": "a" * 64}}}))
    result = load_operator_config(path)["runner_environments"]["torch-test"]
    assert result["python_executable"] == str(interpreter)
    assert result["profile_sha256"] == "a" * 64
    path.write_text(json.dumps({"runner_environments": {"torch-test": "venv-python"}}))
    with pytest.raises(ValueError, match="receipt|SHA256"):
        load_operator_config(path)


def test_command_is_fixed_argv_not_shell_or_recipe_module():
    command = runner.command(sys.executable, "modelbuilder.backend.process_execution", "request;touch bad", "response")
    assert command[:4] == [sys.executable, "-I", "-B", "-c"]
    assert command[-2] == "request;touch bad"
    with pytest.raises(ValueError, match="platform"):
        runner.command(sys.executable, "os", "malicious")


def test_profile_capture_does_not_overwrite(tmp_path):
    path = tmp_path / "receipt.json"
    result = subprocess.run([sys.executable, "-m", "modelbuilder.backend.runner", "--python", sys.executable,
                             "--output", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["training_executed"] is False
    before = path.read_bytes()
    again = subprocess.run([sys.executable, "-m", "modelbuilder.backend.runner", "--python", sys.executable,
                            "--output", str(path)], capture_output=True, text=True)
    assert again.returncode != 0 and path.read_bytes() == before


@pytest.mark.skipif(sys.platform != "linux", reason="Linux lifecycle acceptance")
def test_actual_fresh_process_uses_native_abi_without_importing_torch(tmp_path):
    code = '''import os,sys
from pathlib import Path
def run(*, inputs, parameters, context):
    assert context["device"] == "cpu"
    assert context["placement"]["framework_neutral"]
    assert context["environment"]["runner"]["framework_smoke"] == "not_implied"
    assert "torch" not in sys.modules
    return {"result": {"pid": os.getpid(), "value": inputs["value"] + 3, "python": sys.executable}}
'''
    backend, recipe, _ = setup(tmp_path, code=code)
    spec = spec_for(tmp_path, backend, recipe)
    result = execute("component", {"value": 4}, spec)
    assert result["value"] == 7 and result["pid"] != os.getpid()
    assert result["python"] == sys.executable
    owner = json.loads(next((tmp_path / "job/processes").glob("*/owner.json")).read_text())
    assert owner["reaped"] and owner["exit_code"] == 0
    assert owner["descendant_cleanup"]["remaining_pids"] == []


def test_preimport_recheck_blocks_changed_profile_and_source(tmp_path):
    marker = tmp_path / "imported"
    code = f'from pathlib import Path\nPath({str(marker)!r}).touch()\ndef run(**kwargs): return {{"result": {{}}}}\n'
    backend, recipe, _ = setup(tmp_path, code=code)
    spec = spec_for(tmp_path, backend, recipe)
    Path(backend.dependency_paths["isolated"]).write_text("{}")
    with pytest.raises(RuntimeError, match="changed"):
        execute("component", {}, spec)
    assert not marker.exists()


def test_source_trust_is_not_granted_by_environment_receipt(tmp_path):
    marker = tmp_path / "imported"
    code = f'from pathlib import Path\nPath({str(marker)!r}).touch()\ndef run(**kwargs): return {{"result": {{}}}}\n'
    backend, recipe, _ = setup(tmp_path, code=code)
    spec = spec_for(tmp_path, backend, recipe)
    spec["trusted_code_roots"] = []
    with pytest.raises(RuntimeError, match="trusted"):
        execute("component", {}, spec)
    assert not marker.exists()


def test_evaluation_contract_never_synthesizes_missing_evidence():
    candidate = node()
    candidate["component"]["role"] = "evaluation"
    with pytest.raises(ValueError, match="immutable"):
        runner.validate_evaluation_outputs({"result": {"accuracy": 1.0}}, candidate)
    with pytest.raises(ValueError, match="immutable"):
        runner.validate_evaluation_outputs({"result": {"evaluation_records": {"accuracy": {}}}}, candidate)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process tree acceptance")
def test_runner_cancellation_reaps_owned_descendants(tmp_path):
    import threading
    import time
    import psutil
    child_pid = tmp_path / "descendant.pid"
    code = f'''import subprocess,sys,time
from pathlib import Path
def run(*, inputs, parameters, context):
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
    Path({str(child_pid)!r}).write_text(str(child.pid))
    while True: time.sleep(.05)
'''
    backend, recipe, _ = setup(tmp_path, code=code)
    spec = spec_for(tmp_path, backend, recipe)
    cancellation = Path(spec["job_dir"]) / "cancel.json"
    def cancel():
        deadline = time.monotonic() + 10
        while not child_pid.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        cancellation.parent.mkdir(exist_ok=True, parents=True)
        cancellation.write_text("{}")
    thread = threading.Thread(target=cancel)
    thread.start()
    try:
        with pytest.raises(InterruptedError, match="cancelled"):
            execute("component", {}, spec)
    finally:
        thread.join(timeout=12)
    assert child_pid.exists()
    assert not psutil.pid_exists(int(child_pid.read_text()))
    owner = json.loads(next((tmp_path / "job/processes").glob("*/owner.json")).read_text())
    assert owner["reaped"] and not owner["remaining_observed_descendants"]


def test_changed_source_bytes_refused_before_import_in_selected_interpreter(tmp_path):
    marker = tmp_path / "imported"
    code = f'from pathlib import Path\nPath({str(marker)!r}).touch()\ndef run(**kwargs): return {{"result": {{}}}}\n'
    backend, recipe, _ = setup(tmp_path, code=code)
    spec = spec_for(tmp_path, backend, recipe)
    (backend.repository / "adapter.py").write_text(code + "\nchanged = True\n")
    with pytest.raises(RuntimeError, match="closure changed"):
        execute("component", {}, spec)
    assert not marker.exists()


def test_evaluation_scalar_and_name_must_match_immutable_record():
    from modelbuilder.evaluation import make_evaluation_record
    record = make_evaluation_record(
        dataset={"id": "data", "revision": "1", "content_digest": "digest", "benchmark": "test"},
        split={"id": "test", "role": "test", "sample_ids": ["sample"]},
        protocol={"implementation": "test", "version": "1", "configuration": {}, "preprocessing": {}, "seed": 0},
        metric={"name": "mse", "implementation": "test", "version": "1", "parameters": {},
                "aggregation": "mean", "direction": "minimize"},
        treatment={"model": "model", "checkpoint": "checkpoint", "calibration": None, "reference": None}, value=1.)
    candidate = node()
    candidate["component"]["role"] = "evaluation"
    runner.validate_evaluation_outputs({"result": {"mse": 1., "evaluation_records": {"mse": record}}}, candidate)
    for value in (0., None, True):
        with pytest.raises(ValueError, match="differs"):
            runner.validate_evaluation_outputs({"result": {"mse": value, "evaluation_records": {"mse": record}}}, candidate)
    with pytest.raises(ValueError, match="differs"):
        runner.validate_evaluation_outputs({"result": {"other": 1., "evaluation_records": {"other": record}}}, candidate)
