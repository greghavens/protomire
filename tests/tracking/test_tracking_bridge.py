import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from modelbuilder.backend.common import write_json
from modelbuilder.backend.tracking_bridge import finish_tracking, tracking_status, training_environment


def test_credentials_not_forwarded_to_training(monkeypatch):
    monkeypatch.setenv("WANDB_API_KEY", "unapproved")
    monkeypatch.setenv("MODELBUILDER_WANDB_API_KEY", "approved-for-mirror-only")
    monkeypatch.setenv("MODELBUILDER_TRACKING_PYTHON", "/tracking/python")
    env = training_environment()
    assert not any(k.startswith(("WANDB_", "MODELBUILDER_WANDB_")) for k in env)
    assert "MODELBUILDER_TRACKING_PYTHON" not in env
    assert os.environ["MODELBUILDER_WANDB_API_KEY"] == "approved-for-mirror-only"


def test_failed_training_not_mirrored(tmp_path):
    write_json(tmp_path / "request.json", {"tracking": {"mode": "offline"}})
    result = finish_tracking(tmp_path, SimpleNamespace(status=SimpleNamespace(value="failed")))
    assert result["status"] == "not_mirrored"
    assert not (tmp_path / "tracking").exists()


def test_disabled_training_works_without_sdk(tmp_path):
    write_json(tmp_path / "request.json", {"operation_id": str(uuid4())})
    assert finish_tracking(tmp_path, None)["status"] == "disabled"


def test_interrupted_never_reported_synced(tmp_path):
    write_json(tmp_path / "tracking.json", {"status": "running", "mode": "online", "url": None})
    assert tracking_status(tmp_path, {})["status"] == "interrupted"
    assert tracking_status(tmp_path, {})["remote_may_be_partial"] is True


def test_live_event_filter_axis_dedup_and_backpressure(tmp_path, monkeypatch):
    import json
    import sys
    from modelbuilder.backend.tracking_bridge import LiveTracking

    write_json(tmp_path / "request.json", {"operation_id": str(uuid4()), "tracking": {
        "mode": "offline", "entity": "test", "project": "test", "metric_names": ["loss"]}})
    tracker = LiveTracking(tmp_path)
    tracker.directory.mkdir()
    (tracker.directory / "mirror-events.jsonl").write_text("")
    tracker.thread = object()  # Policy test only; real SDK exercised below.
    events = [
        {"cursor": 1, "event_id": "a", "kind": "evaluation.completed", "step": 20,
         "metrics": {"loss": 0.4, "secret": "never-send"}},
        {"cursor": 2, "event_id": "b", "kind": "evaluation.completed", "step": 10,
         "metrics": {"loss": 0.5}},
        {"cursor": 3, "event_id": "b", "kind": "evaluation.completed", "step": 10,
         "metrics": {"loss": 0.5}},
    ]
    monkeypatch.setitem(sys.modules, "modelbuilder.backend.live_evaluation",
                        SimpleNamespace(list_events=lambda *a, **k: {"events": events}))
    tracker.poll()
    records = [json.loads(line) for line in (tracker.directory / "mirror-events.jsonl").read_text().splitlines()]
    assert [r["checkpoint_step"] for r in records] == [20, 10]  # completion order does not relabel training step
    assert all(r["metrics"] in ({"loss": 0.4}, {"loss": 0.5}) for r in records)
    tracker.sequence = 10000
    tracker._append({"step": 10000, "metrics": {"loss": 0.1}})
    assert tracker.dropped_events == 1
    tracker._append({"finish": True, "exit_code": 0})  # control cannot be dropped by backpressure
    assert (tracker.directory / "mirror-finish.json").exists()


@pytest.mark.integration
def test_real_live_sdk_offline(tmp_path, monkeypatch):
    import json
    import subprocess
    import time
    from modelbuilder.backend.tracking_bridge import LiveTracking
    from modelbuilder.backend.live_evaluation import _connect, _event

    python = os.environ.get("MODELBUILDER_TRACKING_TEST_PYTHON")
    if not python:
        pytest.skip("Optional pinned SDK interpreter required")
    monkeypatch.setenv("MODELBUILDER_TRACKING_PYTHON", python)
    write_json(tmp_path / "request.json", {"operation_id": str(uuid4()), "tracking": {
        "mode": "offline", "entity": "offline-test", "project": "test-live", "metric_names": ["loss"]}})
    write_json(tmp_path / "native.json", {"backend_run_id": str(uuid4())})
    def emit(step, loss):
        with _connect(tmp_path, create=True, write=True) as connection:
            _event(connection, tmp_path, "evaluation.completed", {"checkpoint_id": str(uuid4()),
                "evaluation_id": str(uuid4()), "step": step, "metrics": {"loss": loss}})
    emit(20, 0.4)
    tracker = LiveTracking(tmp_path)
    tracker.poll()
    deadline = time.monotonic() + 10
    while not list(tracker.directory.rglob("*.wandb")) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert list(tracker.directory.rglob("*.wandb")), "SDK creates real local run before training finishes"
    assert tracker.thread.is_alive(), "Tracking must remain open during training"
    emit(10, 0.5)
    tracker.poll()
    result = tracker.finish(SimpleNamespace(status=SimpleNamespace(value="failed")))
    assert result["status"] == "offline", result
    assert result["dropped_events"] == 0
    script = '''
import json, sys
from wandb.sdk.internal.datastore import DataStore
from wandb.proto.wandb_internal_pb2 import Record
s=DataStore(); s.open_for_scan(sys.argv[1]); rows=[]
while True:
    raw=s.scan_data()
    if raw is None: break
    r=Record(); r.ParseFromString(raw)
    if r.HasField('history'):
        rows.append({i.key or '.'.join(i.nested_key):json.loads(i.value_json) for i in r.history.item})
print(json.dumps(rows))
'''
    path = list(tracker.directory.rglob("*.wandb"))[0]
    rows = json.loads(subprocess.check_output([python, "-c", script, str(path)], text=True))
    assert [row["checkpoint/step"] for row in rows if "loss" in row] == [20, 10]
    assert [row["loss"] for row in rows if "loss" in row] == [0.4, 0.5]
