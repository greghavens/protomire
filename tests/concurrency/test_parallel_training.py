"""Real CPU trainers overlap before their native checkpoint artifacts join.

The user-owned harness is copied into a fresh pinned Git repository. A barrier
inside its first backward pass proves overlap without relying on elapsed time.
No pretrained models, network access, or remote compute are used.
"""
import importlib.util
from pathlib import Path
import sys

import pytest

from modelbuilder.backend.components import identity, read_checkpoint


ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv-backend/bin/python"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "linux", reason="Linux process-isolation acceptance"),
    pytest.mark.skipif(not PYTHON.is_file(), reason="Pinned native backend required"),
]


INSTRUMENTATION = r'''
def training_event(parameters, kind, name):
    import os
    directory = Path(parameters["observed_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps({"kind": kind, "name": name, "pid": os.getpid(),
                       "at": time.monotonic()}) + "\n").encode()
    fd = os.open(str(directory / "events.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)


def training_barrier(parameters, context):
    import os
    directory = Path(parameters["observed_dir"])
    name = context["node_id"]
    training_event(parameters, "backward_ready", name)
    (directory / (name + ".backward-ready")).write_text(str(os.getpid()))
    deadline = time.monotonic() + 30
    while not all((directory / (peer + ".backward-ready")).is_file()
                  for peer in ("left", "right")):
        if time.monotonic() > deadline:
            raise RuntimeError("Independent training stages did not overlap: " + name)
        time.sleep(.01)
    training_event(parameters, "barrier_passed", name)


def compare(*, inputs, parameters, context):
    training_event(parameters, "start", context["node_id"])
    comparisons = {}
    for name, checkpoint in inputs.items():
        manifest = json.loads((checkpoint / "checkpoint.manifest.json").read_text())
        model, xs, ys, config = load(checkpoint)
        initial, _, _ = setup(config)
        changed = sum(float((initial.state_dict()[key] - value).abs().sum())
                      for key, value in model.state_dict().items())
        assert changed > 0
        assert changed == manifest["metrics"]["parameter_change"]
        with torch.no_grad():
            heldout_loss = float(loss_for(model(xs[-1]), ys[-1], config))
        comparisons[name] = {
            "checkpoint_digest": hashlib.sha256(json.dumps(
                manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "identities": manifest["identities"],
            "parameter_change": changed,
            "heldout_loss": heldout_loss,
            "steps": manifest["step"],
        }
    training_event(parameters, "end", context["node_id"])
    return {"comparison": comparisons}
'''


def _helpers():
    spec = importlib.util.spec_from_file_location(
        "parallel_training_helpers", Path(__file__).with_name("test_native_parallel.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source():
    source = (ROOT / "tests/components/independent_harness.py").read_text()
    replacements = {
        '    config = parameters["architecture"]':
            '    training_event(parameters, "start", context["node_id"])\n'
            '    config = parameters["architecture"]',
        '        loss.backward()':
            '        loss.backward()\n'
            '        if i == 0:\n'
            '            training_barrier(parameters, context)',
        '    return {"trained": output}':
            '    training_event(parameters, "end", context["node_id"])\n'
            '    return {"trained": output}',
    }
    for old, new in replacements.items():
        assert source.count(old) == 1
        source = source.replace(old, new)
    return source + INSTRUMENTATION


def _component(helpers, recipe, name, role, inputs, outputs, parameters):
    node = helpers.node(recipe, name, "train" if role == "training" else "compare",
                        inputs, outputs, parameters,
                        file=(name if role == "training" else "left") + ".py")
    node["kind"] = "component.run"
    node["component"] = {
        "id": "independent.parallel." + name,
        "version": "1",
        "protocol": "modelbuilder.component/v1",
        "role": role,
        "parameter_schema": {"type": "object"},
        "capabilities": {
            "checkpoint_events": role == "training",
            "resume_modes": ["exact_state", "warm_start"] if role == "training" else [],
            "frameworks": ["pytorch"],
            "devices": ["cpu"],
            "export_formats": [],
        },
    }
    return node


def test_two_native_trainers_overlap_and_join_exact_checkpoints(tmp_path):
    helpers = _helpers()
    helpers.SOURCE = _source()
    backend, payload = helpers.setup(tmp_path, roots=2)
    recipe = payload["recipe"]
    observed = {"observed_dir": str(tmp_path / "observed")}
    recipe["nodes"] = [
        _component(helpers, recipe, name, "training", {}, {"trained": "Path"}, {
            **observed,
            "architecture": {"task": "image", "width": width, "loss_weight": 0.6},
            "learning_rate": 0.01,
            "steps": 3,
        })
        for name, width in (("left", 3), ("right", 5))
    ]
    recipe["nodes"].append(_component(
        helpers, recipe, "compare", "utility", {"left": "Path", "right": "Path"},
        {"comparison": "dict"}, observed))
    recipe["edges"] = [helpers.edge(name, "trained", "compare", name) for name in ("left", "right")]
    assert recipe["execution"] == {"max_parallel_stages": 2}
    assert all(node["cache_policy"] == "disabled" for node in recipe["nodes"])

    result, request = helpers.execute(backend, payload)
    assert result["status"] == "succeeded", result
    assert set(result["executed_node_ids"]) == {"left", "right", "compare"}
    assert result["cached_node_ids"] == []
    rows = helpers.events(tmp_path)
    helpers.assert_overlap(rows)
    ready = {row["name"]: row for row in rows if row["kind"] == "backward_ready"}
    passed = {row["name"]: row for row in rows if row["kind"] == "barrier_passed"}
    assert set(ready) == set(passed) == {"left", "right"}
    assert ready["left"]["pid"] != ready["right"]["pid"]
    assert max(row["at"] for row in ready.values()) < min(row["at"] for row in passed.values())
    training_ends = [row["at"] for row in rows if row["kind"] == "end" and row["name"] != "compare"]
    compare_start = next(row for row in rows if row["kind"] == "start" and row["name"] == "compare")
    assert max(training_ends) < compare_start["at"]
    assert compare_start["pid"] not in {row["pid"] for row in ready.values()}

    native = helpers.native_steps(backend, result)
    assert set(native) == {"left", "right", "compare"}
    comparison = helpers.data_value(result, "compare", "comparison")
    checkpoints = {}
    artifacts = {}
    job = backend.root / "jobs" / request["operation_id"]
    for name in ("left", "right"):
        artifact = artifacts[name] = helpers.data_artifact(result, name, "trained")
        checkpoints[name] = checkpoint = read_checkpoint(job / "outputs" / name)
        assert checkpoint["metrics"]["parameter_change"] > 0
        assert checkpoint["step"] == len(checkpoint["history"]) == 3
        assert comparison[name]["checkpoint_digest"] == identity(checkpoint)
        assert comparison[name]["identities"] == checkpoint["identities"]
        assert comparison[name]["parameter_change"] == checkpoint["metrics"]["parameter_change"]
        assert comparison[name]["heldout_loss"] > 0
        assert native["compare"]["inputs"][name] == [artifact["artifact_id"]]
        assert native[name]["outputs"]["trained"] == [artifact["artifact_id"]]
        assert native[name]["id"] == artifact["step_id"]
        assert artifact["backend_run_id"] == result["backend_run_id"]
    assert artifacts["left"]["artifact_id"] != artifacts["right"]["artifact_id"]
    assert artifacts["left"]["step_id"] != artifacts["right"]["step_id"]
    for key in ("architecture", "training"):
        assert checkpoints["left"]["identities"][key] != checkpoints["right"]["identities"][key]
    helpers.assert_reaped(job, {row["pid"] for row in rows if row["kind"] == "start"})
    assert helpers.reopened(backend).invoke("run.status", request)["artifacts"] == result["artifacts"]
