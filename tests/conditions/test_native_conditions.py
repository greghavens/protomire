"""Actual native terminal conditions; no source import/call for false branches."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

import pytest
from modelbuilder.backend import ZenMLBackend

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv-backend/bin/python"
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not PYTHON.is_file(), reason="Pinned backend required")]
SOURCE = '''from pathlib import Path
import time

def gate(value: bool) -> bool:
    return value

def target(marker: str, delay: float) -> dict:
    with Path(marker).open("a") as f: f.write("called\\n")
    time.sleep(delay)
    return {"measurement": 3.0}
'''


def setup(tmp_path, value=True, delay=0, timeout=30):
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.py").write_text(SOURCE)
    # Separate module makes skipped source import observable, not only invocation.
    (source / "never.py").write_text('raise RuntimeError("false branch source must never be imported")\ndef never() -> dict:\n    return {}\n')
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Pinned conditional harness"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    files = [{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in source.glob("*.py")]
    nodes=[]
    for name in ("gate", "target", "never"):
        reference=next(item for item in files if item["path"] == ("never.py" if name == "never" else "source.py"))
        nodes.append({"id": name,"kind":"code.python","implementation_ref":{**reference,"module":"never" if name == "never" else "condition_harness","entrypoint":name,"commit":commit},
            "inputs":[],"outputs":[{"name":"allowed" if name == "gate" else "metrics","type":"bool" if name == "gate" else "dict"}],
            "parameters":{"value":value} if name == "gate" else ({"marker":str(tmp_path / "called"),"delay":delay} if name == "target" else {}),
            "environment_ref":"default","resource_request":{"gpu":0},"retry":{"max_attempts":1,"backoff_seconds":0},
            "timeout":timeout if name == "target" else 30,"cache_policy":"enabled" if name == "gate" else "disabled","provenance":{}})
    nodes[1]["condition"]={"source_node":"gate","source_port":"allowed","equals":True}
    nodes[2]["condition"]={"source_node":"gate","source_port":"allowed","equals":False if value else True}
    project=str(uuid.uuid4())
    recipe={"schema_version":"0","recipe_id":str(uuid.uuid4()),"revision":1,"project_id":project,"name":"Native conditional terminal stages",
        "source":{"repository":str(source),"commit":commit,"files":files},"nodes":nodes,"edges":[],"resources":[],"external_dependencies":[],
        "environment":{"python":"3.12","requirements":["zenml==0.97.0"]},"evaluation":{},"outputs":[]}
    backend=ZenMLBackend(tmp_path / "state",python_executable=str(PYTHON),source_root=source,trusted_code_roots=[str(source)])
    return backend,{"project_id":project,"recipe":recipe,"trusted":True,"max_runtime_seconds":120}


def execute(backend,payload):
    request={**copy.deepcopy(payload),"operation_id":str(uuid.uuid4()),"idempotency_key":str(uuid.uuid4())}
    plan=backend.invoke("run.plan",request)
    assert plan["executable"],plan
    backend.invoke("run.submit",request)
    deadline=time.monotonic()+150
    while time.monotonic()<deadline:
        result=backend.invoke("run.status",request)
        if result["status"] in {"succeeded","failed","cancelled"} and not result["worker_alive"]:
            return result,request
        time.sleep(.2)
    raise AssertionError(result)


@pytest.mark.parametrize("allowed",[True,False])
def test_actual_native_branch_cache_and_no_false_import(tmp_path,allowed):
    backend,payload=setup(tmp_path,allowed)
    original_gate = None
    original_run = None
    for repeat in range(2):
        result,request=execute(backend,payload)
        assert result["status"] == "succeeded",result
        assert set(result["executed_node_ids"]) == ({"gate","target"} if allowed else {"gate"})
        assert set(result["skipped_node_ids"]) == ({"never"} if allowed else {"target","never"})
        assert result["conditional_execution"]["target"]["gate_value"] is allowed
        assert result["node_statuses"]["target"] == ("completed" if allowed else "skipped")
        assert result["cached_node_ids"] == (["gate"] if repeat else [])
        gate = next(artifact for artifact in result["artifacts"] if artifact["node_id"] == "gate")
        assert result["conditional_execution"]["target"]["gate_artifact_id"] == gate["artifact_id"]
        if repeat:
            assert gate["artifact_id"] == original_gate
            assert gate["cache"]["reused"] is True
            assert gate["cache"]["producer_backend_run_id"] == original_run
        else:
            original_gate, original_run = gate["artifact_id"], result["backend_run_id"]
        assert not any(a["node_id"] in result["skipped_node_ids"] for a in result["artifacts"])
        log=(backend.root / "jobs" / request["operation_id"] / "worker.log").read_text()
        assert "false branch source must never be imported" not in log
    if allowed:
        assert (tmp_path / "called").read_text().splitlines() == ["called","called"]
    else:
        assert not (tmp_path / "called").exists()
        assert result["evaluations"] == []
    # Exact subgraphs do not quietly add the gate or any ancestors.
    plan=backend.invoke("run.plan",{**payload,"execution":{"node_ids":["target"],"input_bindings":[]}})
    assert not plan["executable"] and plan["blocked_reasons"][0]["code"] == "unselected_condition_gate"
    (tmp_path / "acceptance.json").write_text(json.dumps(result,indent=2))


def test_native_conditional_stage_deadline_terminates_worker(tmp_path):
    backend,payload=setup(tmp_path,True,delay=5,timeout=.15)
    result,request=execute(backend,payload)
    assert result["status"] == "cancelled" and not result["worker_alive"],result
    assert result["conditional_execution"]["target"]["status"] == "eligible"
    assert "target" not in result["skipped_node_ids"]
    cancel=json.loads((backend.root / "jobs" / request["operation_id"] / "cancel.json").read_text())
    assert cancel["reason"] == "node_timeout_exceeded" and cancel["node_id"] == "target"


def test_explicit_cancel_stops_eligible_conditional_stage(tmp_path):
    backend, payload = setup(tmp_path, True, delay=30, timeout=60)
    request = {**payload, "operation_id": str(uuid.uuid4()), "idempotency_key": str(uuid.uuid4())}
    backend.invoke("run.submit", request)
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not (tmp_path / "called").exists():
            time.sleep(.1)
        assert (tmp_path / "called").exists(), "Conditional target never started"
        result = backend.invoke("run.cancel", request)
        assert result["status"] == "cancelled" and result["worker_alive"] is False, result
        assert result["conditional_execution"]["target"]["status"] == "eligible"
        assert "target" not in result["skipped_node_ids"]
        assert not any(item["node_id"] == "target" for item in result["artifacts"])
        (tmp_path / "acceptance.json").write_text(json.dumps(result, indent=2))
    finally:
        backend.invoke("run.cancel", request)
