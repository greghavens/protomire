"""Durable queue, verified publication and real subprocess failure boundaries."""
from __future__ import annotations

import hashlib
import json
import time

import psutil
import pytest

from modelbuilder.backend.common import write_json
from modelbuilder.backend.live_evaluation import (
    LiveEvaluationCoordinator, _connect, _snapshot, list_evaluations, list_events,
    reconcile_evaluations, verify_snapshot,
)

HARNESS = '''from pathlib import Path
import json
import subprocess
import sys
import time
from modelbuilder.evaluation import make_evaluation_record

def evaluate(*, inputs, parameters, context):
    state = json.loads((inputs["checkpoint"] / "state.json").read_text())
    if parameters.get("child"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        (Path(context["output_dir"]) / "child.pid").write_text(str(child.pid))
    time.sleep(parameters.get("delay", 0))
    if parameters.get("fail_step") == state["step"]:
        raise RuntimeError("intentional evaluation failure")
    value = state["value"]
    record = make_evaluation_record(
        dataset={"id":"test", "revision":"1", "content_digest":"abc", "benchmark":"test/v1"},
        split={"id":"validation", "role":"validation", "sample_ids":["a", "b"]},
        protocol={"implementation":"test.evaluate", "version":"1", "configuration":{}, "preprocessing":{}, "seed":1},
        metric={"name":"loss", "implementation":"mean", "version":"1", "parameters":{}, "aggregation":"mean", "direction":"minimize"},
        treatment={"model":"small-cpu", "checkpoint":context["checkpoint_digest"] if not parameters.get("wrong_digest") else "wrong", "calibration":None, "reference":None},
        value=value)
    return {"metrics":{"loss":value, "evaluation_records":{"loss":record}}}
'''


def evaluator(tmp_path, **parameters):
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    path = source / "harness.py"
    path.write_text(HARNESS)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"kind": "component", "checkpoint_port": "checkpoint", "inputs": {}, "spec": {
        "parameters": parameters, "source_root": str(source), "trusted_code_roots": [str(source)],
        "source": {"files": [{"path": "harness.py", "sha256": digest}]},
        "component_node": {"id": "eval", "outputs": [{"name": "metrics", "type": "dict"}],
                           "component": {"protocol": "modelbuilder.component/v1"},
                           "implementation_ref": {"path": "harness.py", "sha256": digest,
                                                  "module": "live_test_harness", "entrypoint": "evaluate"}}}}


def checkpoint(tmp_path, step=1, value=1.0):
    root = tmp_path / "mutable"
    root.mkdir(exist_ok=True)
    write_json(root / "state.json", {"step": step, "value": value})
    write_json(root / "checkpoint.manifest.json", {
        "schema_version": "modelbuilder.checkpoint/v1", "step": step, "purpose": "last",
        "resume_modes": ["warm_start"], "state": ["model"],
        "identities": {k: "test" for k in ("architecture", "source", "dependencies", "training", "environment")},
        "assets": [{"path": "state.json", "sha256": hashlib.sha256((root / "state.json").read_bytes()).hexdigest()}]})
    return root


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.025)
    raise AssertionError("Timed out waiting for actual evaluator process state")


def test_empty_read_is_side_effect_free(tmp_path):
    assert list_events(tmp_path) == {"events": [], "next_cursor": 0, "has_more": False}
    assert list_evaluations(tmp_path)["summary"]["completed"] == 0
    assert not (tmp_path / "live_evaluation").exists()
    for kwargs in ({"after": -1}, {"after": True}, {"limit": 1001}):
        with pytest.raises(ValueError):
            list_events(tmp_path, **kwargs)


def test_publication_rejects_partial_and_symlink_assets(tmp_path):
    root = checkpoint(tmp_path)
    (root / "state.json").write_text("half-written")
    with pytest.raises(ValueError, match="integrity mismatch"):
        _snapshot(tmp_path / "job", "train", root, 1, "last", "component")
    root = checkpoint(tmp_path)
    state = (root / "state.json").read_text()
    outside = tmp_path / "outside.json"
    outside.write_text(state)
    (root / "state.json").unlink()
    (root / "state.json").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink|escapes"):
        _snapshot(tmp_path / "job", "train", root, 1, "last", "component")


def test_completed_queue_dedup_reconnect_comparison_and_immutable_copy(tmp_path):
    job = tmp_path / "job"
    manager = LiveEvaluationCoordinator(job, "train", evaluator(tmp_path, delay=0.05))
    try:
        source = checkpoint(tmp_path)
        first = manager.checkpoint_ready(source, 1)
        assert manager.checkpoint_ready(source, 1)["evaluation_id"] == first["evaluation_id"]
        wait_for(lambda: list_evaluations(job)["summary"]["completed"] == 1)
        # Original training state continues changing after publication.
        manager.checkpoint_ready(checkpoint(tmp_path, 2, 0.75), 2)
        wait_for(lambda: list_evaluations(job)["summary"]["completed"] == 2)
        assert manager.thread.is_alive()  # No drain/parent completion needed.
        with _connect(job) as connection:
            published = json.loads(connection.execute("SELECT record FROM checkpoints WHERE checkpoint_id=?", (first["checkpoint_id"],)).fetchone()[0])
        assert json.loads((verify_snapshot(published) / "state.json").read_text())["value"] == 1
    finally:
        manager.close()
    data = list_evaluations(job)
    assert data["summary"] == {"queued": 0, "running": 0, "completed": 2, "failed": 0, "canceled": 0}
    second = data["evaluations"][1]
    assert second["comparisons"]["loss"]["compatible"]
    assert second["comparisons"]["loss"]["delta_from_previous"] == -0.25
    assert second["comparisons"]["loss"]["improved"]
    all_events, cursor = [], 0
    while True:
        page = list_events(job, after=cursor, limit=3)
        all_events.extend(page["events"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert len(all_events) == 8
    assert len({event["event_id"] for event in all_events}) == 8
    assert list_events(job, after=cursor)["events"] == []
    assert list_events(job, after=0)["events"] == all_events
    assert all("path" not in record for record in data["checkpoints"])
    # Recreating coordinator cannot re-evaluate or send duplicate events.
    reconnect = LiveEvaluationCoordinator(job, "train", evaluator(tmp_path, delay=0.05))
    reconnect.checkpoint_ready(checkpoint(tmp_path, 2, 0.75), 2)
    reconnect.close()
    assert list_events(job)["events"] == all_events


def test_failure_is_durable_and_does_not_stop_later_checkpoints(tmp_path):
    job = tmp_path / "job"
    manager = LiveEvaluationCoordinator(job, "train", evaluator(tmp_path, fail_step=1, delay=0.05))
    try:
        for step in range(1, 4):
            manager.checkpoint_ready(checkpoint(tmp_path, step, 1 / step), step)
        deadline = time.monotonic() + 20
        while not all(r["status"] in {"completed", "failed"} for r in list_evaluations(job)["evaluations"]):
            assert time.monotonic() < deadline, list_evaluations(job)
            assert list_evaluations(job)["summary"]["running"] <= 1
            time.sleep(0.01)
    finally:
        manager.close()
    data = list_evaluations(job)
    assert data["summary"]["failed"] == 1 and data["summary"]["completed"] == 2
    assert "intentional evaluation failure" in data["evaluations"][0]["error"]["message"]
    assert sum(e["kind"] == "evaluation.failed" for e in list_events(job)["events"]) == 1


def test_wrong_evidence_digest_fails_without_training_exception(tmp_path):
    job = tmp_path / "job"
    manager = LiveEvaluationCoordinator(job, "train", evaluator(tmp_path, wrong_digest=True))
    manager.checkpoint_ready(checkpoint(tmp_path), 1)
    manager.close()
    result = list_evaluations(job)["evaluations"][0]
    assert result["status"] == "failed"
    assert "published checkpoint digest" in result["error"]["message"]


def test_cancel_kills_evaluator_descendants_and_cancels_queue(tmp_path):
    job = tmp_path / "job"
    manager = LiveEvaluationCoordinator(job, "train", evaluator(tmp_path, delay=60, child=True))
    for step in range(1, 4):
        manager.checkpoint_ready(checkpoint(tmp_path, step), step)
    pid_path = wait_for(lambda: next((job / "live_evaluation").glob("evaluations/*/output/child.pid"), None))
    pid = int(pid_path.read_text())
    assert psutil.pid_exists(pid)
    manager.close(cancel=True)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    data = list_evaluations(job)
    assert data["summary"]["canceled"] == 3
    assert not data["summary"]["running"]
    assert not manager.processes


def test_timeout_is_separate_from_training_failure(tmp_path):
    job = tmp_path / "job"
    manager = LiveEvaluationCoordinator(job, "train", evaluator(tmp_path, delay=60), timeout_seconds=0.2)
    manager.checkpoint_ready(checkpoint(tmp_path), 1)
    manager.close()
    result = list_evaluations(job)["evaluations"][0]
    assert result["status"] == "failed" and result["error"]["code"] == "evaluation_timeout"
    assert not manager.processes


def test_durable_queued_work_survives_coordinator_restart(tmp_path, monkeypatch):
    job = tmp_path / "job"
    with monkeypatch.context() as patch:
        patch.setattr(LiveEvaluationCoordinator, "_run", lambda self: None)
        first = LiveEvaluationCoordinator(job, "train", evaluator(tmp_path))
        queued = first.checkpoint_ready(checkpoint(tmp_path), 1)
        first.close()
    assert list_evaluations(job)["summary"]["queued"] == 1
    restarted = LiveEvaluationCoordinator(job, "train", evaluator(tmp_path))
    restarted.close()
    assert list_evaluations(job)["evaluations"][0]["evaluation_id"] == queued["evaluation_id"]
    assert list_evaluations(job)["summary"]["completed"] == 1
    assert len(list_events(job)["events"]) == 4


def test_abandoned_running_reconciles_once_without_pid_reuse(tmp_path, monkeypatch):
    job = tmp_path / "job"
    with monkeypatch.context() as patch:
        patch.setattr(LiveEvaluationCoordinator, "_run", lambda self: None)
        manager = LiveEvaluationCoordinator(job, "train", evaluator(tmp_path))
        record = manager.checkpoint_ready(checkpoint(tmp_path), 1)
        manager._claim()
        manager.close()
    with _connect(job, write=True) as connection:
        connection.execute("UPDATE evaluations SET owner_identity='psutil:wrong-birth-identity'")
    assert reconcile_evaluations(job)["summary"]["failed"] == 1
    assert reconcile_evaluations(job)["summary"]["failed"] == 1
    assert sum(e["kind"] == "evaluation.failed" for e in list_events(job)["events"]) == 1
    assert list_evaluations(job)["evaluations"][0]["evaluation_id"] == record["evaluation_id"]


def test_ready_marker_cannot_rebind_published_snapshot(tmp_path):
    record = _snapshot(tmp_path / "job", "train", checkpoint(tmp_path), 1, "last", "component")
    from pathlib import Path
    root = Path(record["path"])
    path = root / "state.json"
    path.chmod(0o644)
    path.write_text('{"step":1,"value":99}')
    marker = root / "ready.json"
    ready = json.loads(marker.read_text())
    asset = next(a for a in ready["assets"] if a["path"] == "state.json")
    asset.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size=path.stat().st_size)
    from modelbuilder.backend.live_evaluation import _digest
    ready["snapshot_digest"] = _digest(ready["assets"])
    marker.chmod(0o644)
    marker.write_text(json.dumps(ready))
    with pytest.raises(ValueError, match="marker identity"):
        verify_snapshot(record)


def test_source_mutation_during_copy_is_not_published(tmp_path, monkeypatch):
    from modelbuilder.backend import live_evaluation
    source = checkpoint(tmp_path)
    copy = live_evaluation.shutil.copyfileobj

    def mutate(reader, writer):
        copy(reader, writer)
        (source / "state.json").write_text("changed while publishing")

    monkeypatch.setattr(live_evaluation.shutil, "copyfileobj", mutate)
    with pytest.raises(ValueError, match="changed during publication"):
        _snapshot(tmp_path / "job", "train", source, 1, "last", "component")
    parent = tmp_path / "job" / "live_evaluation" / "checkpoints"
    assert not list(parent.iterdir())


def test_parent_terminal_events_require_native_evidence_and_deduplicate(tmp_path):
    from modelbuilder.backend.live_evaluation import record_run_terminal
    job = tmp_path / "job"
    result = {"operation_id": "job", "backend_run_id": "native-run", "status": "succeeded", "backend_status": "completed"}
    assert not record_run_terminal(job, {**result, "backend_run_id": None, "worker_exit_code": 0})
    assert not record_run_terminal(job, {**result, "backend_status": "running", "worker_exit_code": 0})
    assert not record_run_terminal(job, {**result, "status": "cancel_requested"})
    assert not record_run_terminal(job, {**result, "status": "cancelled", "backend_status": "failed"})
    assert not job.exists()
    assert record_run_terminal(job, result)
    assert not record_run_terminal(job, result)
    assert len(list_events(job)["events"]) == 1
    assert list_events(job)["events"][0]["kind"] == "run.succeeded"
    cancelled = {"operation_id": "cancelled-job", "backend_run_id": "native-cancelled", "status": "cancelled",
                 "backend_status": "failed", "termination_record_source": "native_run_metadata", "worker_alive": False,
                 "process_group_alive": False, "termination": {"reason": "cancelled", "confirmed": True, "process_group_terminated": True}}
    assert record_run_terminal(tmp_path / "cancelled-job", cancelled)
    assert list_events(tmp_path / "cancelled-job")["events"][0]["kind"] == "run.cancelled"
