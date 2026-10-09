"""Source-pinned CPU training -> HTTP -> official MCP -> live offline W&B.

Run with the service interpreter and the optional pinned tracking interpreter:
MODELBUILDER_TRACKING_TEST_PYTHON=$PWD/.venv-tracking/bin/python \
    PYTHONPATH=src .venv/bin/pytest -q tests/integration/test_live_mcp_tracking.py

No source patches, fake native IDs, vendor login, online sync, or IDE claim.
The temporal assertion uses durable checkpoint timestamps, not a slow status RPC.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from uuid import UUID

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[2]
BACKEND_PYTHON = ROOT / ".venv-backend/bin/python"
STEPS = 12
TERMINAL = {"succeeded", "failed", "cancelled"}
pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not BACKEND_PYTHON.is_file(), reason="Pinned backend is not installed")]


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def decode_offline_stream(python, path):
    """Read the real official SDK transaction file using its pinned reader."""
    script = """
import json, sys
from importlib.metadata import version
from wandb.sdk.internal.datastore import DataStore
from wandb.proto.wandb_internal_pb2 import Record
s = DataStore(); s.open_for_scan(sys.argv[1]); kinds = []; rows = []; configs = []; runs = []
while True:
    raw = s.scan_data()
    if raw is None: break
    r = Record(); r.ParseFromString(raw); kinds.append(r.WhichOneof('record_type'))
    if r.HasField('history'):
        rows.append({i.key or '.'.join(i.nested_key): json.loads(i.value_json) for i in r.history.item})
    if r.HasField('run'):
        runs.append({'run_id': r.run.run_id, 'entity': r.run.entity, 'project': r.run.project})
        configs.extend(i.key or '.'.join(i.nested_key) for i in r.run.config.update)
    if r.HasField('config'):
        configs.extend(i.key or '.'.join(i.nested_key) for i in r.config.update)
print(json.dumps({'sdk_version': version('wandb'), 'kinds': kinds, 'history': rows,
                  'config_keys': sorted(set(configs)), 'runs': runs}))
"""
    result = subprocess.run([python, "-I", "-c", script, str(path)], capture_output=True,
                            text=True, timeout=20, check=True)
    return json.loads(result.stdout)


async def test_source_pinned_mcp_live_evaluation_and_offline_tracking(tmp_path, monkeypatch):
    tracking_python = os.environ.get("MODELBUILDER_TRACKING_TEST_PYTHON")
    if not tracking_python:
        pytest.skip("Set MODELBUILDER_TRACKING_TEST_PYTHON to the pinned W&B interpreter")
    assert Path(tracking_python).is_file()
    spec = spec_from_file_location("live_mcp_harness", ROOT / "scripts/mcp_smoke.py")
    harness = module_from_spec(spec)
    spec.loader.exec_module(harness)
    directory = tmp_path / "live-mcp-training"
    directory.mkdir()
    output = ROOT / "evidence/live-mcp-tracking"
    output.mkdir(parents=True, exist_ok=True)
    transcript = []
    last_response = {}
    omitted_reads = {}
    evidence = {"schema_version": "1", "status": "RUNNING", "started_at": utc_now(),
                "transport": "official MCP SDK stdio client -> native MCP server -> OperationService HTTP",
                "mcp_sdk_version": version("mcp"), "source_changes": False,
                "scope": "source-pinned custom CPU fixture with actual ZenML and offline W&B",
                "not_verified": ["IDE GUI or assistant-host acceptance", "online W&B or account login",
                                 "kernel packet/syscall capture", "other operating systems"]}
    # Hostile inherited settings and private test metadata must not reach W&B.
    sentinels = ["SENTINEL-PRIVATE-RECIPE-DO-NOT-MIRROR", "SENTINEL-PRIVATE-CREDENTIAL"]
    monkeypatch.setenv("MODELBUILDER_TRACKING_PYTHON", tracking_python)
    monkeypatch.setenv("MODELBUILDER_BACKEND_PYTHON", str(BACKEND_PYTHON))
    monkeypatch.setenv("MODELBUILDER_SOURCE_ROOT", str(ROOT))
    monkeypatch.setenv("WANDB_MODE", "online")
    monkeypatch.setenv("WANDB_API_KEY", sentinels[1])
    monkeypatch.setenv("MODELBUILDER_WANDB_API_KEY", sentinels[1])
    monkeypatch.setenv("WANDB_BASE_URL", "https://example.invalid")
    service = harness.ServiceProcess(directory)
    identity = None
    job = None
    final_status = None
    try:
        service.start()
        async with service.mcp() as session, httpx.AsyncClient(
            base_url=service.base_url, headers={"Authorization": f"Bearer {service.owner}"},
            trust_env=False, timeout=120,
        ) as http:
            initialized = await session.initialize()
            evidence["mcp_server"] = initialized.serverInfo.model_dump(mode="json")

            async def call(name, payload, timeout=110):
                result = await asyncio.wait_for(session.call_tool(name, payload), timeout)
                body = result.structuredContent
                entry = {"observed_at": utc_now(), "tool": name, "request": payload,
                         "response": body, "is_error": result.isError}
                if name == "run_evaluations_list" and isinstance(body, dict) and "evaluations" in body:
                    # The complete final records are in summary.json. Retain
                    # the live identities/status/metrics here without repeating
                    # full evaluation contracts on every polling response.
                    entry["response_projection"] = {"schema": body["schema"], "summary": body["summary"],
                        "evaluations": [{key: row[key] for key in
                            ("evaluation_id", "checkpoint_id", "checkpoint_digest", "step", "status", "metrics")}
                            for row in body["evaluations"]]}
                    entry.pop("response")
                fingerprint = json.dumps({key: value for key, value in entry.items() if key != "observed_at"},
                                         sort_keys=True)
                if last_response.get(name) != fingerprint:
                    transcript.append(entry)
                    last_response[name] = fingerprint
                else:
                    omitted_reads[name] = omitted_reads.get(name, 0) + 1
                assert not result.isError, (name, body)
                assert isinstance(body, dict)
                assert json.loads(result.content[0].text) == body
                return body

            async def operation(name, payload):
                response = await http.post("/v1/operations/" + name, json=payload)
                assert response.status_code == 200, response.text
                return response.json()

            project = await call("project_create", {"slug": "live-mcp-tracking", "model_name": "CPU fixture",
                                                    "idempotency_key": "project"})
            draft = await call("recipe_get", {"project_id": project["project_id"],
                                              "recipe_id": project["recipe_id"]})
            manifest = read_json(ROOT / "examples/custom-pytorch/recipe.json")
            pinned_source = manifest["source"]
            for item in pinned_source["files"]:
                source = subprocess.check_output(["git", "show", f"{pinned_source['commit']}:{item['path']}"],
                                                 cwd=ROOT)
                assert hashlib.sha256(source).hexdigest() == item["sha256"], item["path"]
                assert source == (ROOT / item["path"]).read_bytes(), item["path"]
            evidence["source"] = {"commit": pinned_source["commit"], "verified_files": len(pinned_source["files"]),
                                  "pin_unchanged": True}
            manifest["description"] = sentinels[0]
            manifest["evaluation"]["live"] = {"enabled": True, "max_concurrency": 1, "timeout_seconds": 30}
            manifest.pop("digest", None)
            for node in manifest["nodes"]:
                node["timeout"] = 150
                if node["kind"] == "model.train":
                    node["parameters"].update(steps=STEPS, step_delay=2.5)
                elif node["kind"] == "dataset.synthetic":
                    node["parameters"]["count"] = 80
            recipe = await call("recipe_create", {"project_id": project["project_id"], "recipe": manifest,
                                                   "expected_revision": draft["revision"], "idempotency_key": "recipe"})
            assert recipe["source"] == pinned_source
            tracking = {"mode": "offline", "entity": "offline-test", "project": "live-mcp-acceptance",
                        "metric_names": ["validation_loss"], "artifact_policy": "none"}
            submission = {"project_id": project["project_id"], "recipe_id": recipe["recipe_id"],
                          "expected_revision": recipe["revision"], "trusted": True,
                          "tracking": tracking, "idempotency_key": "submit"}
            submitted = await call("run_submit", submission)
            assert submitted["status"] in {"submitted", "running"}, submitted
            identity = {"project_id": project["project_id"], "operation_id": submitted["operation_id"]}
            job = directory / "backend/jobs" / submitted["operation_id"]
            events = []
            cursor = 0
            live_observation = None
            deadline = time.monotonic() + 200
            # These transport reads are deliberately journal-only. Native SDK
            # status startup must not consume the window we are trying to prove.
            while time.monotonic() < deadline:
                page = await call("run_events_list", {**identity, "after": cursor, "limit": 200}, timeout=15)
                events.extend(page["events"])
                cursor = page["next_cursor"]
                evaluations = await call("run_evaluations_list", identity, timeout=15)
                completed = [row for row in evaluations["evaluations"] if row["status"] == "completed"]
                progress = read_json(job / "tracking/mirror-progress.json")
                if live_observation is None and len(completed) >= 2 and progress.get("checkpoint_step", 0) >= 2:
                    ready = [row for row in events if row["kind"] == "checkpoint.ready"]
                    if ready and max(row["step"] for row in ready) < STEPS:
                        state = read_json(job / "state.json")
                        assert state["supervisor_state"] == "running", state
                        assert state["worker_reaped"] is False
                        native = read_json(job / "native.json")
                        assert str(UUID(native["backend_run_id"])) == native["backend_run_id"]
                        assert list((job / "tracking").rglob("*.wandb"))
                        live_observation = {"observed_at": utc_now(), "sdk_acknowledgement": progress,
                                            "backend_run_id": native["backend_run_id"],
                                            "completed_evaluations": completed,
                                            "latest_checkpoint_step": max(row["step"] for row in ready),
                                            "worker_reaped": state["worker_reaped"]}
                        evidence["live_observation"] = live_observation
                if any(row["kind"] in {"run.succeeded", "run.failed", "run.cancelled"} for row in events):
                    break
                await asyncio.sleep(0.25)
            assert live_observation is not None, {"events": events, "tracking": read_json(job / "tracking.json")}
            assert any(row["kind"] == "run.succeeded" for row in events), events[-10:]
            # An acknowledgement observed before a LATER checkpoint is stronger
            # evidence of ongoing training than the control-plane status string.
            later_ready = [row for row in events if row["kind"] == "checkpoint.ready"
                           and row["step"] > live_observation["latest_checkpoint_step"]
                           and row["created_at"] > live_observation["observed_at"]]
            assert later_ready, live_observation
            completed_events = [row for row in events if row["kind"] == "evaluation.completed"]
            assert completed_events[1]["created_at"] < later_ready[0]["created_at"]
            evidence["temporal_proof"] = {"second_evaluation_completed_at": completed_events[1]["created_at"],
                                          "sdk_acknowledgement_observed_at": live_observation["observed_at"],
                                          "later_checkpoint": later_ready[0]}
            # Final SDK finish may trail the canonical terminal event slightly.
            while time.monotonic() < deadline:
                if read_json(job / "tracking.json").get("status") != "running":
                    break
                await asyncio.sleep(0.1)
            final_status = await call("run_status", identity)
            assert final_status["status"] == "succeeded", final_status
            assert final_status["backend_status"] == "completed"
            assert final_status["backend_version"] == "0.97.0"
            assert final_status["backend_run_id"] == live_observation["backend_run_id"]
            assert final_status["recipe_digest"] == recipe["digest"]
            assert final_status["recipe_id"] == recipe["recipe_id"]
            assert final_status["recipe_revision"] == recipe["revision"]
            tracked = final_status["tracking"]
            assert tracked["status"] == "offline", tracked
            assert tracked["mode"] == "offline" and tracked["url"] is None
            assert tracked["dropped_events"] == 0 and tracked["live_journal_error"] is False
            expected_run_id = hashlib.sha256(
                f"modelbuilder:{identity['operation_id']}:{final_status['backend_run_id']}".encode()).hexdigest()[:32]
            assert tracked["run_id"] == expected_run_id

            # Replay and cross-transport reads must preserve canonical identity.
            replay = await call("run_events_list", {**identity, "after": 0, "limit": 200})
            assert replay["has_more"] is False and replay["events"] == events
            assert replay == await operation("run.events.list", {**identity, "after": 0, "limit": 200})
            assert not (await call("run_events_list", {**identity, "after": cursor}))["events"]
            evaluations = await call("run_evaluations_list", identity)
            assert evaluations == await operation("run.evaluations.list", identity)
            assert evaluations["summary"]["completed"] == STEPS
            assert all(row["status"] == "completed" for row in evaluations["evaluations"])
            assert len({row["event_id"] for row in events}) == len(events)
            assert all(row["backend_run_id"] == final_status["backend_run_id"] for row in events)
            assert all(row["operation_id"] == identity["operation_id"] for row in events)
            by_id = {row["evaluation_id"]: row for row in evaluations["evaluations"]}
            for event in completed_events:
                record = by_id[event["evaluation_id"]]
                for key in ("checkpoint_id", "checkpoint_digest", "step", "metrics", "comparisons"):
                    assert event[key] == record[key]
                assert record["evaluation_records"]["validation_loss"]["treatment"]["checkpoint"] == record["checkpoint_digest"]
            assert all(row["comparisons"]["validation_loss"]["compatible"]
                       for row in evaluations["evaluations"][1:])
            artifacts = await call("run_artifacts", identity)
            assert artifacts["artifacts"]
            assert all(row["artifact_id"] and row["backend_run_id"] == final_status["backend_run_id"]
                       for row in artifacts["artifacts"])

            mirror = job / "tracking"
            mirror_input = read_json(mirror / "mirror-input.json")
            assert mirror_input["mode"] == "offline" and mirror_input["stream"] is True
            assert mirror_input["correlation"] == {"application_operation_id": identity["operation_id"],
                                                    "zenml_run_id": final_status["backend_run_id"]}
            assert mirror_input["metrics"] == {} and mirror_input["artifact_policy"] == "none"
            assert set(mirror_input) == {"mode", "entity", "project", "run_id", "artifact_policy", "metrics",
                                         "stream", "supervisor_pid", "correlation"}
            mirrored_events = [json.loads(line) for line in (mirror / "mirror-events.jsonl").read_text().splitlines()]
            mirrored_live = [row for row in mirrored_events if "checkpoint_step" in row]
            assert len(mirrored_live) == STEPS
            assert [(row["checkpoint_step"], row["metrics"]) for row in mirrored_live] == [
                (row["step"], {"validation_loss": row["metrics"]["validation_loss"]}) for row in completed_events]
            streams = list(mirror.rglob("*.wandb"))
            assert len(streams) == 1
            decoded = decode_offline_stream(tracking_python, streams[0])
            assert decoded["sdk_version"] == "0.25.0"
            assert not {"stats", "artifact", "output", "files"}.intersection(decoded["kinds"]), decoded["kinds"]
            assert set(decoded["config_keys"]) <= {"application_operation_id", "zenml_run_id", "_wandb"}
            assert decoded["runs"] and all(row["run_id"] == expected_run_id for row in decoded["runs"])
            sdk_live = [row for row in decoded["history"] if "checkpoint/step" in row and "validation_loss" in row]
            assert [(row["checkpoint/step"], row["validation_loss"]) for row in sdk_live] == [
                (row["checkpoint_step"], row["metrics"]["validation_loss"]) for row in mirrored_live]
            allowed_metrics = {"checkpoint/step", "validation_loss", "_timestamp", "_runtime", "_step"}
            assert all(set(row) <= allowed_metrics for row in decoded["history"])
            mirrored_files = []
            for path in mirror.rglob("*"):
                if path.is_file():
                    contents = path.read_bytes()
                    assert all(sentinel.encode() not in contents for sentinel in sentinels), path
                    assert b"fixtures.py" not in contents and b"GatedResidual" not in contents, path
                    assert path.suffix not in {".pt", ".bin", ".py", ".csv", ".parquet"}, path
                    assert path.name not in {"wandb-metadata.json", "requirements.txt", "output.log"}, path
                    assert path.suffix != ".log", path
                    mirrored_files.append(str(path.relative_to(mirror)))
            evidence.update(status="PASS", verified_at=utc_now(), operation_id=identity["operation_id"],
                            backend_run_id=final_status["backend_run_id"], tracking_run_id=expected_run_id,
                            final_status=final_status, evaluations=evaluations, events=events,
                            sdk_stream=decoded, mirrored_files=sorted(mirrored_files),
                            verification={"http_mcp_events_equal": True, "http_mcp_evaluations_equal": True,
                                          "cursor_replay_exact": True, "canonical_event_provenance": True,
                                          "private_sentinels_absent": True, "source_or_weight_uploads": False,
                                          "live_sdk_acknowledged_before_later_checkpoint": True})
    except BaseException as error:
        evidence.update(status="FAIL", failed_at=utc_now(), error_type=type(error).__name__, error=str(error))
        raise
    finally:
        # Keep cancellation bounded and do not strand an independent supervisor
        # when a transport/assertion fails before the canonical terminal state.
        if identity and (not final_status or final_status.get("status") not in TERMINAL):
            try:
                async with httpx.AsyncClient(base_url=service.base_url, trust_env=False, timeout=120,
                    headers={"Authorization": f"Bearer {service.owner}"}) as cleanup:
                    response = await cleanup.post("/v1/operations/run.cancel",
                                                  json={**identity, "idempotency_key": "cleanup-cancel"})
                    evidence["cleanup"] = {"http_status": response.status_code, "result": response.json()}
            except Exception as error:
                evidence["cleanup"] = {"error_type": type(error).__name__}
                if job and job.is_dir():
                    # Test-owned job's documented cooperative cancellation file.
                    (job / "cancel.json").write_text(json.dumps({"requested_at": utc_now(), "reason": "test_cleanup"}))
        service.stop()
        evidence["transcript_identical_reads_omitted"] = omitted_reads
        for path in directory.glob("*.log"):
            shutil.copy2(path, output / path.name)
        if job:
            for name in ("worker.log", "supervisor.log"):
                if (job / name).is_file():
                    shutil.copy2(job / name, output / name)
        (output / "summary.json").write_text(json.dumps(evidence, indent=2) + "\n")
        (output / "transcript.json").write_text(json.dumps(transcript, indent=2) + "\n")
