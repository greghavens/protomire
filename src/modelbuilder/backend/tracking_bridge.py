"""Supervised post-run mirror of selected canonical ZenML results."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from modelbuilder.tracking import mirror_tracking
from .common import alive, read_json, write_json


def training_environment() -> dict:
    """Tracking credentials belong only to the dedicated mirror process."""
    return {key: value for key, value in os.environ.items()
            if not key.startswith(("WANDB_", "MODELBUILDER_WANDB_")) and key != "MODELBUILDER_TRACKING_PYTHON"}


def tracking_status(job: Path, request: dict, canonical_status: str | None = None) -> dict:
    saved = read_json(job / "tracking.json")
    if saved:
        if saved.get("status") == "running":
            state = read_json(job / "state.json")
            if not alive(state.get("supervisor_pid"), state.get("supervisor_identity")):
                return {**saved, "status": "interrupted", "error_code": "tracking_supervisor_stopped",
                        "remote_may_be_partial": saved.get("mode") == "online"}
        return saved
    mode = request.get("tracking", {}).get("mode", "disabled")
    return {"provider": "wandb", "mode": mode,
            "status": "disabled" if mode == "disabled" else (
                "not_mirrored" if canonical_status in {"failed", "cancelled"} else "pending"),
            "run_id": None, "url": None}


def finish_tracking(job: Path, run) -> dict:
    """One bounded best-effort mirror after successful canonical reconciliation.

    No implicit replay on service reads/restarts. A partial or failed mirror must
    be reviewed before a user explicitly resubmits a distinct training run.
    """
    request = read_json(job / "request.json")
    previous = read_json(job / "tracking.json")
    if previous:
        return previous
    config = request.get("tracking", {"mode": "disabled"})
    if config.get("mode", "disabled") == "disabled":
        result = tracking_status(job, request)
    elif run is None or run.status.value not in {"completed", "cached"}:
        result = {"provider": "wandb", "mode": config["mode"], "status": "not_mirrored",
                  "run_id": None, "url": None, "reason": "canonical_run_not_succeeded"}
    else:
        from .sdk import metrics_for
        write_json(job / "tracking.json", {"provider": "wandb", "mode": config["mode"], "status": "running",
                                            "run_id": None, "url": None})
        try:
            result = mirror_tracking(config, operation_id=request["operation_id"], backend_run_id=str(run.id),
                                     metrics=metrics_for(run), directory=job / "tracking",
                                     online_consent=request.get("tracking_online_consent") is True,
                                     python_executable=os.environ.get("MODELBUILDER_TRACKING_PYTHON"))
        except Exception:
            result = {"provider": "wandb", "mode": config["mode"], "status": "failed", "run_id": None,
                      "url": None, "error_code": "tracking_failed", "remote_may_be_partial": config["mode"] == "online"}
    write_json(job / "tracking.json", result)
    return result


class LiveTracking:
    """A bounded SDK mirror thread; only sanitized scalars reach its event spool.

    Training never waits for an SDK network call. The coordinator polls the local
    event journal; no training component receives tracking credentials or an SDK.
    """

    def __init__(self, job: Path):
        self.job = job
        self.request = read_json(job / "request.json")
        self.config = self.request.get("tracking", {"mode": "disabled"})
        self.cursor = 0
        self.sequence = 0
        self.thread = None
        self.result = None
        self.directory = job / "tracking"
        self.blocked = False
        self.dropped_events = 0
        self.checkpoint_step = None
        self.seen = set()

    def _append(self, event: dict) -> None:
        import json
        path = self.directory / "mirror-events.jsonl"
        data = json.dumps(event, allow_nan=False) + "\n"
        if event.get("finish") is not True and (self.sequence >= 10000 or path.stat().st_size + len(data) > 8 * 1024 * 1024):
            self.dropped_events += 1
            return
        with path.open("a") as output:
            output.write(data)
        if event.get("finish") is True:
            write_json(self.directory / "mirror-finish.json", {"finish": True})

    def _start(self, native_id: str) -> None:
        import threading
        self.directory.mkdir(exist_ok=True, mode=0o700)
        (self.directory / "mirror-events.jsonl").write_text("")
        (self.directory / "mirror-finish.json").unlink(missing_ok=True)
        write_json(self.job / "tracking.json", {"provider": "wandb", "mode": self.config["mode"],
                                                "status": "running", "run_id": None, "url": None})

        def execute():
            try:
                self.result = mirror_tracking(self.config, operation_id=self.request["operation_id"],
                    backend_run_id=native_id, metrics={}, directory=self.directory,
                    online_consent=self.request.get("tracking_online_consent") is True,
                    python_executable=os.environ.get("MODELBUILDER_TRACKING_PYTHON"), stream=True,
                    timeout=float(self.request.get("max_runtime_seconds", 600)) + 60)
            except Exception:
                self.result = {"provider": "wandb", "mode": self.config["mode"], "status": "failed",
                               "run_id": None, "url": None, "error_code": "tracking_failed",
                               "remote_may_be_partial": self.config["mode"] == "online"}
            write_json(self.job / "tracking.json", self.result)
        self.thread = threading.Thread(target=execute, daemon=True, name="optional-wandb-mirror")
        self.thread.start()

    def poll(self) -> None:
        from modelbuilder.tracking import TrackingConfig, selected_metrics
        if self.blocked or self.config.get("mode", "disabled") == "disabled":
            return
        try:
            if self.thread is None:
                native_id = read_json(self.job / "native.json").get("backend_run_id")
                if not native_id:
                    return
                self._start(native_id)
            if self.result is not None:
                return
            from .live_evaluation import list_events
            page = list_events(self.job, after=self.cursor, limit=200)
            config = TrackingConfig.model_validate(self.config)
            for event in page["events"]:
                identity = event.get("event_id", event["cursor"])
                if event.get("kind") == "evaluation.completed" and identity not in self.seen:
                    metrics = selected_metrics(config, event.get("metrics", {}))
                    checkpoint_step = event.get("step")
                    if metrics and type(checkpoint_step) is int and checkpoint_step >= 0:
                        self.checkpoint_step = checkpoint_step
                        self._append({"step": self.sequence, "checkpoint_step": checkpoint_step, "metrics": metrics})
                        self.sequence += 1
                    if len(self.seen) < 10000:
                        self.seen.add(identity)
                self.cursor = event["cursor"]
        except sqlite3.OperationalError:
            # Busy/temporarily unavailable local WAL readers retry at the next
            # supervisor tick from the same cursor; no remote replay is started.
            return
        except Exception:
            # A malformed journal/config or missing optional event helper must
            # not crash the training supervisor or expose raw exception text.
            self.blocked = True

    def finish(self, run) -> dict:
        self.poll()
        if self.thread is None:
            return finish_tracking(self.job, run)
        succeeded = run is not None and run.status.value in {"completed", "cached"}
        if self.result is None:
            try:
                from modelbuilder.tracking import TrackingConfig, selected_metrics
                if succeeded:
                    from .sdk import metrics_for
                    metrics = selected_metrics(TrackingConfig.model_validate(self.config), metrics_for(run))
                    if metrics:
                        self._append({"step": self.sequence, "metrics": metrics})
                self._append({"finish": True, "exit_code": 0 if succeeded else 1})
            except Exception:
                self._append({"finish": True, "exit_code": 1})
        # SDK finish normally completes immediately offline. The worker has its
        # own overall watchdog; the supervisor stays responsible until it exits.
        self.thread.join()
        result = self.result or {"provider": "wandb", "mode": self.config["mode"], "status": "failed",
                                 "run_id": None, "url": None, "error_code": "tracking_failed"}
        result["dropped_events"] = self.dropped_events
        result["live_journal_error"] = self.blocked
        write_json(self.job / "tracking.json", result)
        return result
