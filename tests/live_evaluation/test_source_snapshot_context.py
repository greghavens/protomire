"""Live evaluators receive the same pinned source context as normal components."""
import hashlib
import json
from pathlib import Path

from modelbuilder.backend import live_evaluation as live
from modelbuilder.backend.components import runtime
from test_live_evaluation import checkpoint, evaluator


def test_live_context_reads_pinned_snapshot_after_checkout_changes(tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    configured = evaluator(tmp_path)
    source = tmp_path / "source"
    config = source / "config.txt"
    config.write_text("1.0")
    code = source / "harness.py"
    code.write_text(code.read_text().replace(
        'value = state["value"]',
        'value = float((Path(context["source_root"]) / "config.txt").read_text())'))
    configured["spec"]["component_node"]["implementation_ref"]["sha256"] = hashlib.sha256(code.read_bytes()).hexdigest()
    configured["spec"]["source"]["files"] = [
        {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in (code, config)
    ]
    published = live._snapshot(job, "train", checkpoint(tmp_path), 1, "last", "component")
    record = {"schema": live.SCHEMA, "evaluation_id": "source-context-probe",
              "checkpoint_id": published["checkpoint_id"], "checkpoint_digest": published["checkpoint_digest"],
              "node_id": "train", "step": 1, "status": "running", "metrics": {}, "evaluation_records": {}}
    with live._connect(job, create=True, write=True) as connection:
        connection.execute(
            "INSERT INTO evaluations (evaluation_id,checkpoint_id,node_id,step,status,record,request) VALUES (?,?,?,?,?,?,?)",
            (record["evaluation_id"], published["checkpoint_id"], "train", 1, "running", json.dumps(record),
             json.dumps({"checkpoint": published, "evaluator": live._encode(configured)})))
    original = runtime.load_callable

    def edit_checkout_after_staging(*args):
        function = original(*args)
        assert (Path(function.__source_snapshot_root__) / "config.txt").read_text() == "1.0"
        config.write_text("99.0")
        return function

    monkeypatch.setattr(runtime, "load_callable", edit_checkout_after_staging)
    live._evaluate(job, record["evaluation_id"])
    result = live.list_evaluations(job)["evaluations"][0]
    assert result["status"] == "completed"
    assert result["metrics"] == {"loss": 1.0}
    assert config.read_text() == "99.0"
