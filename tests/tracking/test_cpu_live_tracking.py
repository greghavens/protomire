"""Real CPU fixture + real live evaluator + real optional offline W&B SDK.

This verifies the framework hook/mirror before training ends, not source-bound
ZenML admission; the full generic-component replay is a separate gate.
"""
import os
import threading
import time
from uuid import uuid4

import pytest

pytest.importorskip("torch", reason="Run this integration in .venv-backend")
from modelbuilder.backend import fixtures  # noqa: E402
from modelbuilder.backend.common import read_json, write_json  # noqa: E402
from modelbuilder.backend.live_evaluation import LiveEvaluationCoordinator  # noqa: E402
from modelbuilder.backend.tracking_bridge import LiveTracking  # noqa: E402

pytestmark = pytest.mark.integration


def test_real_cpu_evaluations_reach_offline_sdk_before_training_ends(tmp_path, monkeypatch):
    python = os.environ.get("MODELBUILDER_TRACKING_TEST_PYTHON")
    if not python:
        pytest.skip("Optional pinned W&B interpreter required")
    monkeypatch.setenv("MODELBUILDER_TRACKING_PYTHON", python)
    job = tmp_path / "job"
    write_json(job / "request.json", {"operation_id": str(uuid4()), "max_runtime_seconds": 60, "tracking": {
        "mode": "offline", "entity": "offline-test", "project": "cpu-live", "metric_names": ["validation_loss"]}})
    # Correlation UUID is synthetic here; no ZenML execution claim is made.
    write_json(job / "native.json", {"backend_run_id": str(uuid4())})
    data = fixtures.dataset({"fixture": "custom", "count": 80})
    initial = fixtures.initialize(data, {"fixture": "custom", "width": 4}, tmp_path / "initial")
    coordinator = LiveEvaluationCoordinator(job, "train", {"kind": "fixture", "data": data, "parameters": {}},
                                          max_concurrency=1, timeout_seconds=30)
    failures = []
    tracker = LiveTracking(job)

    def train():
        try:
            fixtures.train(data, initial, {"steps": 12, "step_delay": 0.8, "checkpoint_interval_steps": 2,
                "_checkpoint_ready": coordinator.checkpoint_ready}, tmp_path / "training")
        except BaseException as error:
            failures.append(error)

    trainer = threading.Thread(target=train)
    trainer.start()
    observed = None
    try:
        deadline = time.monotonic() + 25
        while trainer.is_alive() and time.monotonic() < deadline:
            tracker.poll()
            progress = read_json(job / "tracking" / "mirror-progress.json")
            if (progress.get("checkpoint_step") or 0) >= 4:
                observed = progress
                assert trainer.is_alive()
                assert tracker.thread.is_alive()
                break
            time.sleep(0.025)
        assert observed is not None, read_json(job / "tracking.json")
    finally:
        trainer.join(timeout=40)
        coordinator.close(cancel=bool(failures) or trainer.is_alive())
        # Close SDK without claiming a canonical ZenML success in this fixture-level test.
        result = tracker.finish(None)
    assert not trainer.is_alive()
    assert not failures
    assert result["status"] == "offline", result
    assert result["url"] is None
    assert result["dropped_events"] == 0
    assert list((job / "tracking").rglob("*.wandb"))
