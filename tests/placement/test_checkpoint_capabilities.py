"""Read-side capability tests with inert files and a controlled native SDK.

No pipeline, framework tensor operation, native store, or accelerator is used.
The original manifest identity is preserved separately from effective modes.
"""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

pytest.importorskip("zenml")

from modelbuilder.backend import sdk  # noqa: E402
from modelbuilder.backend.admission import BackendError  # noqa: E402
from modelbuilder.backend.common import read_json, write_json  # noqa: E402
from modelbuilder.backend.components import identity  # noqa: E402


MODES = ["exact_state", "warm_start"]


def saved_request(device, *, component):
    node = {"id": "train", "kind": "component.run" if component else "model.train",
            "outputs": [{"name": "checkpoint", "type": "Path"}]}
    if component:
        node["component"] = {"role": "training"}
    request = {"recipe": {"nodes": [node], "environment": {}}}
    if device is not None:
        request["recipe"]["environment"]["device"] = device
        request.update(target="local-" + device, placement={"device": device})
    return request


def inert_checkpoint(path, *, component, kind="last", modes=None):
    path.mkdir(parents=True, exist_ok=True)
    if not component:
        raw = b"Inert fixture bytes; never unpickled or executed."
        (path / ("best.pt" if kind == "best" else "checkpoint.pt")).write_bytes(raw)
        return hashlib.sha256(raw).hexdigest(), None
    raw = b"Inert component weights; never materialized by a framework."
    (path / "weights.bin").write_bytes(raw)
    descriptor = {"schema_version": "modelbuilder.checkpoint/v1", "purpose": kind, "step": 2,
                  "resume_modes": list(MODES if modes is None else modes),
                  "state": ["model", "optimizer", "scheduler", "rng", "data_cursor"],
                  "identities": dict.fromkeys(("architecture", "source", "dependencies", "training", "environment"), "pinned"),
                  "assets": [{"path": "weights.bin", "sha256": hashlib.sha256(raw).hexdigest()}]}
    (path / "checkpoint.manifest.json").write_text(json.dumps(descriptor))
    return identity(descriptor), descriptor


@pytest.mark.parametrize("device", [None, "cpu", "mps", "cuda:2"])
@pytest.mark.parametrize("component", [False, True])
@pytest.mark.parametrize("kind", ["last", "best"])
@pytest.mark.parametrize("recovery", [False, True])
def test_checkpoint_read_reports_effective_modes_without_rewriting_manifest(
        tmp_path, monkeypatch, device, component, kind, recovery):
    job, path = tmp_path / "job", tmp_path / "checkpoint"
    request = saved_request(device, component=component)
    write_json(job / "request.json", request)
    digest, descriptor = inert_checkpoint(path, component=component, kind=kind)
    artifact_id = str(uuid4())
    artifact = NS(id=artifact_id, load=lambda: path)
    selected = {"artifact_id": artifact_id, "backend_run_id": "owned-run",
                **({"port": "recovery_checkpoint"} if recovery else {"node_id": "train", "port": "checkpoint"})}
    monkeypatch.setattr(sdk, "reconcile", lambda _job: object())
    monkeypatch.setattr(sdk, "artifacts_for", lambda _job, _run: [selected])
    monkeypatch.setattr(sdk, "Client", lambda: NS(get_artifact_version=lambda _id: artifact))
    before = {p.name: p.read_bytes() for p in path.iterdir()}

    result = sdk.checkpoint(job, kind)

    assert result["resume_modes"] == (MODES if device in {None, "cpu"} else ["warm_start"])
    assert result["sha256"] == digest
    assert result["artifact_id"] == artifact_id
    assert read_json(job / "request.json") == request
    assert {p.name: p.read_bytes() for p in path.iterdir()} == before
    if component:
        assert result["descriptor"] == descriptor
        assert result["sha256"] == identity(result["descriptor"])
        assert result["descriptor_scope"] == "immutable_component_declaration_not_effective_capabilities"


@pytest.mark.parametrize("device", ["mps", "cuda:0"])
@pytest.mark.parametrize("modes", [[], ["exact_state"], ["warm_start"]])
def test_effective_capabilities_do_not_invent_warm_start(device, modes):
    original = deepcopy(modes)
    assert sdk._checkpoint_resume_modes(saved_request(device, component=True), modes) == [
        mode for mode in modes if mode != "exact_state"]
    assert modes == original


@pytest.mark.parametrize("change", ["placement", "target", "invalid_device", "invalid_placement"])
def test_inconsistent_saved_placement_cannot_advertise_cpu_exact_resume(change):
    request = saved_request("cpu", component=False)
    if change == "placement":
        request["placement"]["device"] = "mps"
    elif change == "target":
        request["target"] = "local-mps"
    elif change == "invalid_device":
        request["recipe"]["environment"]["device"] = "auto"
    else:
        request["placement"] = None
    with pytest.raises(BackendError) as error:
        sdk._checkpoint_resume_modes(request, MODES)
    assert error.value.code == "dependency_mismatch"


@pytest.mark.parametrize("device", [None, "cpu", "mps", "cuda:1"])
@pytest.mark.parametrize("component", [False, True])
def test_failed_run_reconciliation_cannot_restore_accelerator_exact_claim(
        tmp_path, monkeypatch, device, component):
    job = tmp_path / "job"
    request = saved_request(device, component=component)
    write_json(job / "request.json", request)
    write_json(job / "state.json", {"worker_reaped": True})
    path = job / "outputs" / "train"
    digest, descriptor = inert_checkpoint(path, component=component)
    artifact_id = str(uuid4())
    artifact = NS(id=artifact_id, name="recovered", uri=str(path), load=lambda: path)
    run = NS(id="owned-run", status=sdk.ExecutionStatus.FAILED, model_version=None, steps={},
             run_metadata={"local_supervisor_termination": {"confirmed": True}})
    client = NS(get_pipeline_run=lambda *_args, **_kwargs: run,
                get_artifact_version=lambda _id: artifact)
    monkeypatch.setattr(sdk, "Client", lambda: client)
    monkeypatch.setattr(sdk, "native_run", lambda _job: (client, run))
    monkeypatch.setattr(sdk, "alive", lambda *_args: False)
    monkeypatch.setattr(sdk, "process_group_state", lambda *_args: False)
    monkeypatch.setattr(sdk, "termination_confirmed", lambda _state: True)
    saved, metadata = [], []
    def save(source, **kwargs):
        saved.append((source, kwargs))
        return artifact
    monkeypatch.setattr(sdk, "save_artifact", save)
    monkeypatch.setattr(sdk, "log_metadata", lambda *args, **kwargs: metadata.append((args, kwargs)))

    result = sdk.checkpoint(job, "last")

    assert len(saved) == 1 and saved[0][0] == path
    assert result["port"] == "recovery_checkpoint"
    assert result["resume_modes"] == (MODES if device in {None, "cpu"} else ["warm_start"])
    assert result["sha256"] == digest
    assert read_json(job / "recovery.json") == {"artifact_id": artifact_id}
    if component:
        assert result["descriptor"] == descriptor
    assert sdk.checkpoint(job, "last")["resume_modes"] == result["resume_modes"]
    assert len(saved) == 1  # Reading existing recovery must not duplicate recovery.


def test_loop_scope_suppresses_exact_resume_without_rewriting_checkpoint_modes():
    request = saved_request('cpu', component=True)
    request['recipe']['bounded_iterations'] = [{'id': 'repeat', 'node_ids': ['train'], 'max_iterations': 2, 'state': []}]
    declared = ['exact_state', 'warm_start']
    assert sdk._checkpoint_resume_modes(request, declared) == ['warm_start']
    assert declared == ['exact_state', 'warm_start']
    request['execution'] = {'node_ids': ['other'], 'input_bindings': []}
    assert sdk._checkpoint_resume_modes(request, declared) == ['exact_state', 'warm_start']
