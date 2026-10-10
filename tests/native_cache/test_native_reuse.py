"""Real native cache/retry and provenance, independent of model fixtures."""
import copy
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from modelbuilder.backend import ZenMLBackend

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv-backend/bin/python"
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not PYTHON.is_file(), reason="Pinned native backend required")]

SOURCE = '''from pathlib import Path
import json

def prepare(marker: str) -> dict:
    path = Path(marker)
    with path.open("a") as file: file.write("prepare\\n")
    return {"rows": [1, 2, 3], "source": "independent"}

def transform(data: dict, marker: str) -> dict:
    with Path(marker).open("a") as file: file.write("transform\\n")
    return {"rows": [value * 2 for value in data["rows"]]}

def component_transform(inputs, parameters, context):
    return {"data": transform(inputs["data"], parameters["marker"])}

def consume(data: dict, ready: str, wait: bool = False) -> dict:
    if wait:
        import time
        while not Path(ready).is_file(): time.sleep(.1)
    if not Path(ready).is_file(): raise RuntimeError("deliberate downstream failure")
    return {"total": sum(data["rows"])}
'''


def setup(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "harness.py").write_text(SOURCE)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Independent value harness"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    digest = hashlib.sha256((source / "harness.py").read_bytes()).hexdigest()
    project = str(uuid.uuid4())
    nodes=[]
    for name in ("prepare", "transform", "consume"):
        nodes.append({"id": name, "kind": "code.python", "implementation_ref": {
            "module": "independent_values", "entrypoint": name, "path": "harness.py", "sha256": digest, "commit": commit},
            "inputs": [] if name == "prepare" else [{"name": "data", "type": "dict"}],
            "outputs": [{"name": "data", "type": "dict"}],
            "parameters": {"ready": str(tmp_path / "ready")} if name == "consume" else {"marker": str(tmp_path / "calls")},
            "environment_ref": "default", "resource_request": {"gpu": 0}, "timeout": 30,
            "retry": {"max_attempts": 1, "backoff_seconds": 0},
            "cache_policy": "disabled" if name == "consume" else "enabled", "provenance": {}})
    recipe = {"schema_version": "0", "recipe_id": str(uuid.uuid4()), "revision": 1, "project_id": project,
        "name": "Native value reuse", "source": {"repository": str(source), "commit": commit,
            "files": [{"path": "harness.py", "sha256": digest}]}, "nodes": nodes,
        "edges": [{"source_node": a, "source_port": "data", "target_node": b, "target_port": "data"}
                  for a, b in [("prepare", "transform"), ("transform", "consume")]],
        "resources": [], "external_dependencies": [], "environment": {"python": "3.12", "requirements": ["zenml==0.97.0"]},
        "evaluation": {}, "outputs": []}
    backend = ZenMLBackend(tmp_path / "state", python_executable=str(PYTHON), source_root=source, trusted_code_roots=[str(source)])
    return backend, {"project_id": project, "project_recipe_id": recipe["recipe_id"], "recipe": recipe,
        "trusted": True, "max_runtime_seconds": 120}


def execute(backend, payload):
    payload = {**copy.deepcopy(payload), "operation_id": str(uuid.uuid4()), "idempotency_key": str(uuid.uuid4())}
    plan=backend.invoke("run.plan", payload)
    assert plan["executable"], plan
    backend.invoke("run.submit", payload)
    deadline=time.monotonic()+150
    while time.monotonic()<deadline:
        status=backend.invoke("run.status", payload)
        if status["status"] in {"succeeded", "failed", "cancelled"} and not status["worker_alive"]:
            return status, payload
        time.sleep(.2)
    raise AssertionError(status)


def test_retry_reuses_completed_stages_with_original_native_provenance(tmp_path):
    backend, payload=setup(tmp_path)
    failed, first=execute(backend, payload)
    assert failed["status"] == "failed", failed
    assert failed["cached_node_ids"] == []
    assert failed["node_statuses"] == {"prepare": "completed", "transform": "completed", "consume": "failed"}
    assert (tmp_path/"calls").read_text().splitlines() == ["prepare", "transform"]
    (tmp_path/"ready").touch()
    replay, second=execute(backend, payload)
    assert replay["status"] == "succeeded", replay
    assert set(replay["cached_node_ids"]) == {"prepare", "transform"}
    assert replay["node_statuses"]["consume"] == "completed"
    assert (tmp_path/"calls").read_text().splitlines() == ["prepare", "transform"]
    for name in ("prepare", "transform"):
        before=next(a for a in failed["artifacts"] if a["node_id"] == name)
        after=next(a for a in replay["artifacts"] if a["node_id"] == name)
        assert before["artifact_id"] == after["artifact_id"]
        assert after["backend_run_id"] == replay["backend_run_id"]
        assert after["cache"] == {"reused": True, "original_step_run_id": before["step_id"],
            "producer_backend_run_id": failed["backend_run_id"], "producer_step_run_id": before["step_id"]}
    artifacts=backend.invoke("run.artifacts", {**second, "include_binding_descriptors": True})["artifacts"]
    cached=next(a for a in artifacts if a["node_id"] == "transform")
    assert cached["binding_descriptor"]["cache"]["producer_backend_run_id"] == failed["backend_run_id"]
    # Replayed output can be explicitly bound through its completed consumer run.
    descriptor=cached["binding_descriptor"]
    selected={**payload,"execution":{"node_ids":["consume"],"input_bindings":[{
        "node_id":"consume", "input_port":"data", **{key:descriptor[key] for key in
        ("backend_run_id","source_node_id","source_port","artifact_id","content_sha256")}}]}}
    selected_result,_=execute(backend, selected)
    assert selected_result["status"] == "succeeded", selected_result
    assert selected_result["executed_node_ids"] == ["consume"]
    assert selected_result["execution"]["input_bindings"][0]["cache"] == cached["cache"]
    # An exact immutable revision change invalidates reuse, even unchanged data.
    revision=copy.deepcopy(payload)
    revision["recipe"]["revision"] = 2
    revised,_=execute(backend, revision)
    assert revised["status"] == "succeeded" and revised["cached_node_ids"] == [], revised
    assert (tmp_path/"calls").read_text().splitlines() == ["prepare", "transform"] * 2
    # Foreign project never reuses artifacts, even from the same backend store.
    foreign=copy.deepcopy(payload)
    foreign["project_id"] = foreign["recipe"]["project_id"] = str(uuid.uuid4())
    other,_=execute(backend, foreign)
    assert other["status"] == "succeeded" and other["cached_node_ids"] == [], other
    # Native bytes are checked against SHA256 logged by the original value step.
    original=next(a for a in failed["artifacts"] if a["node_id"] == "prepare")
    path=Path(original["uri"])/"data.json"
    path.write_text('{"rows":[999],"source":"tampered"}')
    corrupt, request=execute(backend, payload)
    assert corrupt["status"] == "failed", corrupt
    log=(backend.root/"jobs"/request["operation_id"]/"worker.log").read_text()
    assert "recorded SHA256" in log
    assert (tmp_path/"calls").read_text().splitlines() == ["prepare", "transform"] * 3
    (tmp_path/"acceptance.json").write_text(json.dumps({"failed":failed,"replay":replay,"selected":selected_result,
        "revision":revised,"foreign":other,"corrupt":corrupt},indent=2))


def test_cancelled_pipeline_reuses_source_component_value_stages(tmp_path):
    backend, payload = setup(tmp_path)
    transform = payload["recipe"]["nodes"][1]
    transform["kind"] = "component.run"
    transform["implementation_ref"]["entrypoint"] = "component_transform"
    transform["component"] = {"id": "independent.transform", "version": "1", "protocol": "modelbuilder.component/v1",
        "role": "dataset", "parameter_schema": {"type": "object", "properties": {"marker": {"type": "string"}}, "required": ["marker"], "additionalProperties": False},
        "capabilities": {"resume_modes": [], "export_formats": [], "frameworks": ["python"]}}
    payload["recipe"]["nodes"][2]["parameters"]["wait"] = True
    request = {**copy.deepcopy(payload), "operation_id": str(uuid.uuid4()), "idempotency_key": str(uuid.uuid4())}
    backend.invoke("run.submit", request)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        current = backend.invoke("run.status", request)
        if current.get("node_statuses", {}).get("consume") == "running":
            break
        assert current["status"] not in {"failed", "cancelled"}, current
        time.sleep(.2)
    else:
        raise AssertionError(current)
    cancelled = backend.invoke("run.cancel", request)
    assert cancelled["status"] == "cancelled" and not cancelled["worker_alive"], cancelled
    assert (tmp_path / "calls").read_text().splitlines() == ["prepare", "transform"]
    (tmp_path / "ready").touch()
    replay, _ = execute(backend, payload)
    assert replay["status"] == "succeeded", replay
    assert set(replay["cached_node_ids"]) == {"prepare", "transform"}
    assert (tmp_path / "calls").read_text().splitlines() == ["prepare", "transform"]
    assert all(artifact["cache"]["producer_backend_run_id"] == cancelled["backend_run_id"]
               for artifact in replay["artifacts"] if artifact["node_id"] in {"prepare", "transform"})
