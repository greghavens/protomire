"""Train once, reuse native checkpoint for independent evaluation and export."""
import copy
import hashlib
import shutil
import subprocess
from pathlib import Path
import json
import uuid

import pytest
pytest.importorskip("zenml")
pytest.importorskip("torch")
from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.admission import BackendError
from modelbuilder.backend.common import read_json
from test_native_lifecycle import recipe, wait

pytestmark = pytest.mark.integration


def request(body, project, nodes, bindings=()):
    return {"project_id": project, "project_recipe_id": body["recipe_id"], "operation_id": str(uuid.uuid4()),
            "recipe": copy.deepcopy(body), "trusted": True, "idempotency_key": str(uuid.uuid4()),
            "target": "local-cpu", "max_runtime_seconds": 180,
            "execution": {"node_ids": list(nodes), "input_bindings": list(bindings)}}


def binding(artifact, node, port):
    descriptor = artifact["binding_descriptor"]
    return {"node_id": node, "input_port": port, **{key: descriptor[key] for key in
        ("backend_run_id", "source_node_id", "source_port", "artifact_id", "content_sha256")}}


def selector(result, node):
    entry = next(entry for entry in result["evaluations"] if entry["node_id"] == node and entry["metric"] == "validation_loss")
    return {key: entry[key] for key in ("backend_run_id", "node_id", "output_port", "artifact_id", "metric")}


def test_train_once_two_benchmarks_export_native_bindings(tmp_path, monkeypatch):
    from tests.integration.native_fixture_support import pinned_builtin_source, authorized_backend
    from tests.components.rights_test_support import local_source_review, write_reviewed_policies
    source = pinned_builtin_source(tmp_path)
    monkeypatch.setitem(recipe.__globals__, "SOURCE_ROOT", source)
    backend = ZenMLBackend(tmp_path, python_executable=str(Path(__file__).resolve().parents[2] / ".venv-backend/bin/python"),
                           source_root=source, trusted_code_roots=[str(source)])
    project = str(uuid.uuid4())
    body = recipe(steps=3)
    body.update(project_id=project, recipe_id=str(uuid.uuid4()), revision=1)
    data = copy.deepcopy(next(node for node in body["nodes"] if node["id"] == "data"))
    data["id"] = "benchmark_two"
    data["parameters"]["seed"] = 999
    evaluation = copy.deepcopy(next(node for node in body["nodes"] if node["id"] == "evaluate"))
    evaluation["id"] = "evaluate_two"
    body["nodes"].extend([data, evaluation])
    body["edges"].extend([{"source_node": "benchmark_two", "source_port": "data", "target_node": "evaluate_two", "target_port": "data"},
                          {"source_node": "train", "source_port": "checkpoint", "target_node": "evaluate_two", "target_port": "checkpoint"}])
    backend = authorized_backend(backend, {"project_id": project, "recipe": body})
    training = request(body, project, ["data", "initial", "train"])
    backend.invoke("run.submit", training)
    trained = wait(backend, training, lambda row: row["status"] == "succeeded" and row["worker_alive"] is False and row["supervisor_alive"] is False)
    assert trained["status"] == "succeeded", trained
    original = copy.deepcopy(trained)
    original_version = backend.invoke("model.version.get", {"project_id": project, "model_version_ref": trained["model_version_ref"]})
    artifacts = backend.invoke("run.artifacts", {**training, "include_binding_descriptors": True})["artifacts"]
    checkpoint = next(item for item in artifacts if item.get("node_id") == "train")
    data_artifact = next(item for item in artifacts if item.get("node_id") == "data")
    evaluation_request = request(body, project, ["data", "benchmark_two", "evaluate", "evaluate_two"],
        [binding(checkpoint, "evaluate", "checkpoint"), binding(checkpoint, "evaluate_two", "checkpoint")])
    before_jobs = sorted((backend.root / "jobs").iterdir())
    before_files = {path: path.read_bytes() for path in before_jobs[0].glob("*.json")}
    plan = backend.invoke("run.plan", copy.deepcopy(evaluation_request))
    assert plan["executable"], plan
    assert sorted((backend.root / "jobs").iterdir()) == before_jobs
    assert all(path.read_bytes() == content for path, content in before_files.items())
    evaluations = []
    for _ in range(2):
        evaluation_request["operation_id"] = str(uuid.uuid4())
        evaluation_request["idempotency_key"] = str(uuid.uuid4())
        backend.invoke("run.submit", evaluation_request)
        result = wait(backend, evaluation_request)
        assert result["status"] == "succeeded", result
        assert set(result["executed_node_ids"]) == {"data", "benchmark_two", "evaluate", "evaluate_two"}
        assert "train" not in result["executed_node_ids"]
        assert "validation_loss" not in result["metrics"]
        assert len(result["evaluations"]) == 6
        # Polling must stay lightweight and must not attribute an unverified
        # evaluator claim to a model. Explicit comparison below verifies the
        # connected checkpoint bytes and the original training owner.
        assert {entry["model_version_ref"] for entry in result["evaluations"]} == {None}
        assert all(entry["checkpoint_verification"] == {
            "verified": False, "code": "checkpoint_verification_pending",
            "message": "Checkpoint identity and ownership are verified on explicit comparison or promotion",
        } for entry in result["evaluations"])
        assert len({entry["record"]["dataset"]["content_digest"] for entry in result["evaluations"]}) == 2
        evaluations.append(result)
    compare = {"project_id": project, "run_ids": [result["backend_run_id"] for result in evaluations],
               "metric": "validation_loss", "direction": "minimize"}
    assert not backend.invoke("run.compare", compare)["compatible"]
    compatible = backend.invoke("run.compare", {**compare, "evaluations": [selector(result, "evaluate") for result in evaluations]})
    assert compatible["compatible"] and not compatible["diagnostics"], compatible
    assert compatible["selected_model_version_ref"] == trained["model_version_ref"]
    assert {entry["model_version_ref"] for entry in compatible["runs"]} == {trained["model_version_ref"]}
    assert compatible["selected_run_id"] in {result["backend_run_id"] for result in evaluations}
    incompatible = backend.invoke("run.compare", {**compare, "evaluations": [selector(evaluations[0], "evaluate"), selector(evaluations[1], "evaluate_two")]})
    assert not incompatible["compatible"] and incompatible["selected_run_id"] is None
    wrong = selector(evaluations[0], "evaluate")
    wrong["artifact_id"] = str(uuid.uuid4())
    with pytest.raises(BackendError, match="absent or ambiguous"):
        backend.invoke("run.compare", {**compare, "evaluations": [wrong, selector(evaluations[1], "evaluate")]})
    exported_request = request(body, project, ["export"], [binding(checkpoint, "export", "checkpoint"), binding(data_artifact, "export", "data")])
    backend.invoke("run.submit", exported_request)
    exported = wait(backend, exported_request)
    assert exported["status"] == "succeeded", exported
    assert exported["executed_node_ids"] == ["export"]
    assert not (backend.root / "jobs" / exported_request["operation_id"] / "outputs" / "train").exists()
    # Selection is part of the inherited idempotency identity.
    changed = copy.deepcopy(exported_request)
    changed["execution"]["node_ids"] = ["data", "initial", "train"]
    changed["execution"]["input_bindings"] = []
    with pytest.raises(BackendError, match="conflicting request"):
        backend.invoke("run.submit", changed)
    # Ownership, exact port type, and pinned native bytes fail closed.
    for kind in ("cross-project", "hash", "type", "producer"):
        bad = copy.deepcopy(exported_request)
        bad["operation_id"], bad["idempotency_key"] = str(uuid.uuid4()), str(uuid.uuid4())
        if kind == "cross-project":
            bad["project_id"] = str(uuid.uuid4())
            bad["recipe"]["project_id"] = bad["project_id"]
        elif kind == "hash":
            bad["execution"]["input_bindings"][0]["content_sha256"] = "0" * 64
        elif kind == "type":
            bad["execution"]["input_bindings"][0] = binding(data_artifact, "export", "checkpoint")
        else:
            bad["execution"]["input_bindings"][0]["source_node_id"] = "initial"
        with pytest.raises(BackendError):
            backend.invoke("run.submit", bad)
        assert not (backend.root / "jobs" / bad["operation_id"]).exists()
    failing_body = copy.deepcopy(body)
    failing_body["revision"] = 2
    next(node for node in failing_body["nodes"] if node["id"] == "benchmark_two")["parameters"]["count"] = 1
    failing_request = request(failing_body, project, ["benchmark_two", "evaluate_two"], [binding(checkpoint, "evaluate_two", "checkpoint")])
    backend.invoke("run.submit", failing_request)
    failed = wait(backend, failing_request)
    assert failed["status"] == "failed" and "train" not in failed["executed_node_ids"]
    # A source-pinned wait node holds this evaluation request open long enough
    # for actual supervised cancellation, without modifying the training source.
    source = tmp_path / "cancellation-source"
    (source / "src/modelbuilder/backend").mkdir(parents=True)
    (source / "requirements").mkdir()
    root = Path(__file__).resolve().parents[2]
    shutil.copy2(root / "src/modelbuilder/backend/fixtures.py", source / "src/modelbuilder/backend/fixtures.py")
    shutil.copy2(root / "requirements/backend.lock", source / "requirements/backend.lock")
    (source / "pause.py").write_text("import time\ndef pause() -> int:\n    time.sleep(60)\n    return 1\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Pinned cancellation source"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    cancelling_body = copy.deepcopy(body)
    cancelling_body["revision"] = 3
    digest = hashlib.sha256((source / "pause.py").read_bytes()).hexdigest()
    cancelling_body["source"].update(commit=commit, files=[
        {"path": relative, "sha256": hashlib.sha256((source / relative).read_bytes()).hexdigest()}
        for relative in ("src/modelbuilder/backend/fixtures.py", "pause.py")])
    cancelling_body["nodes"].append({"id": "pause", "kind": "code.python", "inputs": [],
        "outputs": [{"name": "value", "type": "int"}], "parameters": {}, "timeout": 90,
        "provenance": {"intended_use": "evaluate"},
        "implementation_ref": {"path": "pause.py", "module": "pause_evaluation", "entrypoint": "pause", "commit": commit, "sha256": digest}})
    # Changed test source gets its own bounded review; the original checkpoint's
    # policy remains available as inherited lineage, never relabeled as new code.
    policy, rights = local_source_review(source, cancelling_body["source"], project,
                                        policy_id="test-cancellation-origin")
    cancelling_body["extensions"]["artifact_rights"] = rights
    policies = json.loads(backend._execution_rights_authority.policies_json)
    policies[policy["policy_id"]] = policy
    policy_store = write_reviewed_policies(backend.root / "test-reviews.sqlite3", policies)
    cancelling_backend = backend.with_source_workspace(source, trusted=True, dependency_paths={}).with_execution_rights(
        project_id=project, policies=policies, policy_store_path=policy_store)
    cancelling_request = request(cancelling_body, project, ["data", "pause", "evaluate"], [binding(checkpoint, "evaluate", "checkpoint")])
    cancelling_backend.invoke("run.submit", cancelling_request)
    cancelled = cancelling_backend.invoke("run.cancel", cancelling_request)
    assert cancelled["status"] == "cancelled" and cancelled["worker_alive"] is False, cancelled
    assert "train" not in cancelled["executed_node_ids"]
    now = backend.invoke("run.status", training)
    assert now["backend_run_id"] == original["backend_run_id"] and now["status"] == "succeeded"
    assert now["metrics"] == original["metrics"]
    assert backend.invoke("model.version.get", {"project_id": project, "model_version_ref": trained["model_version_ref"]}) == original_version
    # Actual tampering, not just a bogus caller hash, is caught without loading.
    native_bytes = next(path for path in Path(checkpoint["uri"]).rglob("*") if path.is_file())
    saved_bytes = native_bytes.read_bytes()
    try:
        native_bytes.write_bytes(saved_bytes + b"tampered")
        rejected = backend.invoke("run.plan", copy.deepcopy(evaluation_request))
        assert rejected["executable"] is False
        assert rejected["blocked_reasons"][0]["code"] == "artifact_content_mismatch"
    finally:
        native_bytes.write_bytes(saved_bytes)
    assert sum("train" in read_json(path)["execution"]["node_ids"] for path in (backend.root / "jobs").glob("*/request.json")) == 1
    # Save local sanitized evidence for this isolated acceptance test.
    (tmp_path / "acceptance.json").write_text(json.dumps({"training": trained, "evaluations": evaluations,
        "export": exported, "comparison": compatible, "incompatible": incompatible, "failed": failed, "cancelled": cancelled}, indent=2))
