"""Dedicated official W&B SDK process. Invoked by tracking.mirror_tracking only."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
import sys


def mirror(payload: dict, directory: Path) -> dict:
    try:
        import wandb
    except ImportError:
        return {"status": "unavailable"}
    if wandb.__version__ != "0.25.0":
        return {"status": "unavailable"}
    run = None
    try:
        # Privacy switches prefixed x_ are pinned-SDK internals; checked by real
        # offline tests. Refuse other SDK versions rather than silently weaken.
        settings = wandb.Settings(
            base_url="https://api.wandb.ai", mode=payload["mode"],
            console="off", silent=True, disable_code=True, disable_git=True,
            save_code=False, disable_job_creation=True, sagemaker_disable=True,
            x_disable_meta=True, x_disable_stats=True, x_disable_machine_info=True,
            x_save_requirements=False, x_disable_viewer=True, x_log_level=50,
            init_timeout=15, login_timeout=5, x_service_wait=10,
            x_graphql_retry_max=0, x_file_stream_retry_max=0, x_file_transfer_retry_max=0,
            ignore_globs=["*.log", "logs/*", "code/*", "requirements.txt", "wandb-metadata.json"],
        )
        run = wandb.init(entity=payload["entity"], project=payload["project"],
                         id=payload["run_id"], name=payload["run_id"],
                         config=payload["correlation"], dir=str(directory),
                         settings=settings, save_code=False, sync_tensorboard=False,
                         reinit="create_new")
        final_metrics = dict(payload["metrics"])
        exit_code = 0
        if payload.get("stream"):
            run.define_metric("checkpoint/step")
            run.define_metric("*", step_metric="checkpoint/step", step_sync=False)
            events = directory / "mirror-events.jsonl"
            offset = 0
            finished = False
            while not finished:
                if os.getppid() != payload["supervisor_pid"]:
                    raise RuntimeError("Tracking supervisor stopped")
                if events.exists():
                    with events.open() as source:
                        source.seek(offset)
                        while True:
                            line = source.readline()
                            if not line or not line.endswith("\n"):
                                break
                            offset = source.tell()
                            event = json.loads(line)
                            if event.get("finish") is True:
                                finished = True
                                exit_code = event.get("exit_code", 0)
                                break
                            values = dict(event["metrics"])
                            if event.get("checkpoint_step") is not None:
                                values["checkpoint/step"] = event["checkpoint_step"]
                            run.log(values, step=event["step"])
                            progress = directory / "mirror-progress.json"
                            temporary = progress.with_suffix(".tmp")
                            temporary.write_text(json.dumps({"sequence": event["step"],
                                                             "checkpoint_step": event.get("checkpoint_step")}))
                            temporary.replace(progress)
                            final_metrics.update(event["metrics"])
                if not finished:
                    time.sleep(0.1)
        else:
            run.log(final_metrics, step=0)
        if payload["artifact_policy"] == "metrics_snapshot":
            snapshot = directory / "selected-metrics.json"
            snapshot.write_text(json.dumps(final_metrics, sort_keys=True, allow_nan=False))
            artifact = wandb.Artifact("selected-metrics-" + payload["run_id"], type="metrics")
            artifact.add_file(str(snapshot), name="selected-metrics.json")
            run.log_artifact(artifact)
        run.finish(exit_code=exit_code, quiet=True)
        return {"status": "offline" if payload["mode"] == "offline" else "synced"}
    except Exception:
        if run is not None:
            try:
                run.finish(exit_code=1, quiet=True)
            except Exception:
                pass
        return {"status": "failed"}
    finally:
        # SDK diagnostic text is intentionally not exposed or retained. It can
        # contain provider error text, environment paths or credential context.
        for path in directory.rglob("*.log"):
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)


if __name__ == "__main__":
    request, response = map(Path, sys.argv[1:3])
    response.write_text(json.dumps(mirror(json.loads(request.read_text()), request.parent)))
