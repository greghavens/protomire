"""Actual pinned ZenML/store/OS lifecycle tests, with sanitized evidence IDs."""
import hashlib
import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

import pytest

torch = pytest.importorskip("torch", reason="Native fixture acceptance requires .venv-backend")
from modelbuilder.backend import ZenMLBackend  # noqa: E402
from modelbuilder.backend.common import alive, read_json  # noqa: E402
from modelbuilder.backend.fixtures import load_checkpoint  # noqa: E402

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def recipe(fixture="custom", steps=12, coefficient=0.0, width=12, delay=0.0):
    body = json.loads((ROOT / "examples" / ("pretrained-small" if fixture == "pretrained" else "custom-pytorch") / "recipe.json").read_text())
    # Acceptance workload source is the exact committed fixture implementation.
    path = "src/modelbuilder/backend/fixtures.py"
    body["source"]["commit"] = subprocess.check_output(["git", "log", "-1", "--format=%H", "--", path], cwd=ROOT, text=True).strip()
    body["source"]["files"] = [{"path": path, "sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest()}]
    body.pop("digest", None)
    for node in body["nodes"]:
        if node["kind"] == "model.initialize":
            node["parameters"]["width"] = width
        if node["kind"] == "model.train":
            node["parameters"].update(steps=steps, width=width, step_delay=delay)
            node["parameters"]["kl"].update(enabled=coefficient > 0, coefficient=coefficient)
    return body


def submit(backend, body, project_id=None):
    payload = {"project_id": project_id or str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "recipe": body,
               "trusted": True, "idempotency_key": str(uuid.uuid4()), "target": "local-cpu", "max_runtime_seconds": 180}
    result = backend.invoke("run.submit", payload)
    assert result["operation_id"] == payload["operation_id"]
    return payload


def wait(backend, request, predicate=None, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = backend.invoke("run.status", request)
        if predicate(result) if predicate else result["status"] in {"succeeded", "failed", "cancelled"}:
            return result
        time.sleep(0.2)
    raise AssertionError(f"Run did not reach expected state: {result}")


def save_evidence(name, value):
    target = ROOT / "evidence/backend" / f"{name}.json"
    target.write_text(json.dumps(value, indent=2))


def checkpoint(backend, request):
    return backend.invoke("model.checkpoint.get", {"project_id": request["project_id"], "operation_id": request["operation_id"]})


def same(a, b):
    if isinstance(a, torch.Tensor):
        return torch.equal(a, b)
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def test_native_custom_dag_variants_negative_and_models(tmp_path):
    backend = ZenMLBackend(tmp_path)
    project = str(uuid.uuid4())
    records = []
    for body in [recipe(steps=30), recipe(steps=30, width=20), recipe(steps=30, coefficient=5.0)]:
        request = submit(backend, body, project)
        result = wait(backend, request)
        assert result["status"] == "succeeded", result
        assert len(result["artifacts"]) == 5
        assert len({artifact["artifact_id"] for artifact in result["artifacts"]}) == 5
        assert result["metrics"]["consumed_dataset_artifact"] is True
        records.append(result)
    assert len({r["metrics"]["dataset_sha256"] for r in records}) == 1
    assert records[2]["metrics"]["validation_loss"] > records[0]["metrics"]["validation_loss"]
    versions = backend.invoke("model.version.list", {"project_id": project})["versions"]
    assert len(versions) == 3
    comparison = backend.invoke("run.compare", {"project_id": project, "run_ids": [r["backend_run_id"] for r in records],
                                                "metric": "validation_loss", "direction": "minimize"})
    assert comparison["selected_run_id"] in {r["backend_run_id"] for r in records}
    save_evidence("native-custom-variants", {"runs": records, "comparison": comparison, "versions": versions})


def test_real_failed_run_remains_queryable(tmp_path):
    backend = ZenMLBackend(tmp_path)
    body = recipe(steps=10)
    next(n for n in body["nodes"] if n["kind"] == "model.train")["parameters"]["intentional_failure_step"] = 4
    request = submit(backend, body)
    result = wait(backend, request)
    assert result["status"] == "failed" and result["backend_status"] == "failed"
    assert result["backend_run_id"]
    # Fresh adapter instance reads the native ID and failure from the same store.
    restarted = ZenMLBackend(tmp_path).invoke("run.status", request)
    assert restarted["backend_run_id"] == result["backend_run_id"]
    assert restarted["status"] == "failed"
    logs = backend.invoke("run.logs", request)["logs"]
    assert "Intentional fixture failure" in logs
    ref = checkpoint(backend, request)
    assert load_checkpoint(Path(ref["path"]))["step"] == 3
    save_evidence("native-failed", {"run": restarted, "recovery_checkpoint": ref})


def test_actual_cancel_recovered_checkpoint_exact_resume(tmp_path):
    backend = ZenMLBackend(tmp_path)
    body = recipe(steps=200, delay=0.15)
    request = submit(backend, body)
    running = wait(backend, request, lambda r: r["status"] == "running" and bool(r["backend_run_id"]))
    output = backend.root / "jobs" / request["operation_id"] / "outputs/train/checkpoint.pt"
    deadline = time.monotonic() + 20
    while not output.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    cancelled = backend.invoke("run.cancel", request)
    assert cancelled["status"] == "cancelled", cancelled
    assert cancelled["backend_status"] == "failed"  # ZenML static-run terminal enum limitation is explicit.
    assert cancelled["termination"]["reason"] == "cancelled" and cancelled["termination"]["confirmed"]
    assert cancelled["cancellation_acknowledged"] is True
    assert cancelled["worker_alive"] is False and cancelled["process_group_alive"] is False
    ref = checkpoint(backend, request)
    saved = load_checkpoint(Path(ref["path"]))
    final_steps = saved["step"] + 5
    resumed_recipe = recipe(steps=final_steps)
    resumed_request = {**request, "parent_operation_id": request["operation_id"], "operation_id": str(uuid.uuid4()),
                       "idempotency_key": str(uuid.uuid4()), "recipe": resumed_recipe, "steps": final_steps,
                       "mode": "exact_state", "checkpoint_id": ref["artifact_id"]}
    backend.invoke("run.resume", resumed_request)
    resumed = wait(backend, resumed_request)
    assert resumed["status"] == "succeeded", resumed
    full_request = submit(backend, recipe(steps=final_steps), request["project_id"])
    full = wait(backend, full_request)
    a = load_checkpoint(Path(checkpoint(backend, full_request)["path"]))
    b = load_checkpoint(Path(checkpoint(backend, resumed_request)["path"]))
    for field in ("model", "optimizer", "scheduler", "torch_rng", "python_rng", "order", "cursor", "epoch"):
        assert same(a[field], b[field]), field
    assert resumed["parent_run_id"] == cancelled["backend_run_id"]
    assert resumed["parent_checkpoint_ref"] == ref["artifact_id"]
    save_evidence("native-cancel-exact-resume", {"running": running, "cancelled": cancelled, "resumed": resumed,
                                              "uninterrupted": full, "state_parity": True})


def test_worker_sigkill_failure_is_reconciled(tmp_path):
    backend = ZenMLBackend(tmp_path)
    request = submit(backend, recipe(steps=200, delay=0.2))
    wait(backend, request, lambda r: r["status"] == "running" and bool(r["backend_run_id"]))
    job = backend.root / "jobs" / request["operation_id"]
    while not (job / "outputs/train/checkpoint.pt").exists():
        time.sleep(0.1)
    state = read_json(job / "state.json")
    assert alive(state["worker_pid"], state["worker_identity"])
    os.kill(state["worker_pid"], signal.SIGKILL)
    result = wait(backend, request)
    assert result["status"] == "failed" and result["worker_alive"] is False
    ref = checkpoint(backend, request)
    assert load_checkpoint(Path(ref["path"]))["step"] > 0
    save_evidence("native-worker-crash", {"run": result, "checkpoint": ref})


def test_pretrained_validation_selection_parent_and_export(tmp_path):
    backend = ZenMLBackend(tmp_path)
    project, records, requests = str(uuid.uuid4()), [], []
    for coefficient in (0.0, 0.2):
        request = submit(backend, recipe("pretrained", steps=18, coefficient=coefficient), project)
        result = wait(backend, request)
        assert result["status"] == "succeeded", result
        records.append(result)
        requests.append(request)
    best_index = min(range(2), key=lambda i: records[i]["metrics"]["validation_loss"])
    best, parent = records[best_index], requests[best_index]
    decision = {"selection_rule": {"metric": "validation_loss", "direction": "minimize"},
                "run_ids": [r["backend_run_id"] for r in records], "outcome": "accepted", "rationale": "Measured validation criterion"}
    promoted = backend.invoke("model.version.promote", {"project_id": project, "model_version_id": best["model_version_ref"],
                                                        "decision": decision})
    assert promoted["selected_run_id"] == best["backend_run_id"]
    final_steps = best["metrics"]["best_step"] + 3
    next_request = {**parent, "operation_id": str(uuid.uuid4()), "parent_operation_id": parent["operation_id"],
                    "idempotency_key": str(uuid.uuid4()), "recipe": recipe("pretrained", steps=final_steps,
                                  coefficient=best["metrics"]["kl"]["coefficient"]), "mode": "exact_state",
                    "steps": final_steps, "checkpoint_kind": "best", "decision": decision}
    backend.invoke("run.resume", next_request)
    child = wait(backend, next_request)
    assert child["status"] == "succeeded", child
    assert child["parent_run_id"] == best["backend_run_id"]
    lineage = backend.invoke("model.version.lineage", {"project_id": project, "model_version_id": child["model_version_ref"]})
    assert lineage["metadata"]["parent_run_id"] == best["backend_run_id"]
    assert lineage["pipeline_run_links"] and lineage["artifact_links"]
    save_evidence("native-pretrained-selection", {"runs": records, "promoted": promoted, "child": child, "lineage": lineage})
