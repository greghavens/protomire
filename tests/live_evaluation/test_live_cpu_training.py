"""Actual Torch checkpoint evaluations are visible before actual CPU training ends."""
from __future__ import annotations

import threading
import time

import pytest

torch = pytest.importorskip("torch", reason="Real CPU live evaluation requires the backend environment")

from modelbuilder.backend import fixtures  # noqa: E402
from modelbuilder.backend.live_evaluation import LiveEvaluationCoordinator, list_evaluations, list_events  # noqa: E402

pytestmark = pytest.mark.integration


def test_two_real_checkpoint_evaluations_arrive_while_training_is_running(tmp_path):
    job = tmp_path / "cpu-run"
    data = fixtures.dataset({"fixture": "custom", "count": 80})
    initial = fixtures.initialize(data, {"fixture": "custom", "width": 4}, tmp_path / "initial")
    coordinator = LiveEvaluationCoordinator(job, "train", {"kind": "fixture", "data": data, "parameters": {}},
                                          max_concurrency=1, timeout_seconds=30)
    failures = []

    def train():
        try:
            fixtures.train(data, initial, {"steps": 8, "step_delay": 0.8, "checkpoint_interval_steps": 2,
                                           "_checkpoint_ready": coordinator.checkpoint_ready}, tmp_path / "training")
        except BaseException as error:
            failures.append(error)

    trainer = threading.Thread(target=train)
    trainer.start()
    observed = []
    try:
        deadline = time.monotonic() + 20
        while trainer.is_alive() and time.monotonic() < deadline:
            records = list_evaluations(job)
            completed = [record for record in records["evaluations"] if record["status"] == "completed"]
            if len(completed) >= 2:
                observed = completed
                assert trainer.is_alive()
                break
            time.sleep(0.025)
        assert len(observed) >= 2, list_evaluations(job)
        assert {row["step"] for row in observed}.issuperset({2, 4})
        for record in observed:
            assert record["metrics"]["validation_loss"] > 0
            assert record["evaluation_records"]["validation_loss"]["treatment"]["checkpoint"] == record["checkpoint_digest"]
        assert observed[1]["comparisons"]["validation_loss"]["compatible"]
    finally:
        trainer.join(timeout=30)
        coordinator.close(cancel=bool(failures) or trainer.is_alive())
    assert not trainer.is_alive()
    assert not failures
    assert list_evaluations(job)["summary"]["completed"] == 4
    page = list_events(job)
    assert len({row["event_id"] for row in page["events"]}) == len(page["events"])
    assert not list_events(job, after=page["next_cursor"])["events"]
