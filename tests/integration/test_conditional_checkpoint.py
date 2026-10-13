"""Actual selected native checkpoint evaluation through terminal conditions."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import pytest
pytest.importorskip("zenml")
pytest.importorskip("torch")
from modelbuilder.backend import ZenMLBackend
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService
from test_checkpoint_subgraph import binding, selector
from test_native_lifecycle import recipe

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def test_native_conditions_preserve_selected_checkpoint_and_explicit_comparison(tmp_path, monkeypatch):
    source = tmp_path / "source"
    for relative in ("src/modelbuilder/backend/fixtures.py", "requirements/backend.lock"):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    (source / "gate.py").write_text("def gate() -> bool:\n    return True\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Pinned gated fixture"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    files = [{"path": name, "sha256": hashlib.sha256((source / name).read_bytes()).hexdigest()}
             for name in ("src/modelbuilder/backend/fixtures.py", "gate.py")]
    monkeypatch.setitem(recipe.__globals__, "SOURCE_ROOT", source)
    body = recipe(steps=3)
    project = str(uuid.uuid4())
    body.update(project_id=project, recipe_id=str(uuid.uuid4()), revision=1)
    body["source"].update(repository="urn:sha256:" + files[0]["sha256"], commit=commit, files=files)
    body["nodes"].append({"id": "gate", "kind": "code.python", "inputs": [],
        "outputs": [{"name": "allowed", "type": "bool"}], "parameters": {}, "timeout": 30, "cache_policy": "disabled",
        "environment_ref": "default", "resource_request": {"gpu": 0}, "retry": {"max_attempts": 1, "backoff_seconds": 0}, "provenance": {"intended_use": "evaluate"},
        "implementation_ref": {**files[1], "module": "conditional_checkpoint_gate", "entrypoint": "gate", "commit": commit}})
    evaluation = next(node for node in body["nodes"] if node["id"] == "evaluate")
    evaluation["condition"] = {"source_node": "gate", "source_port": "allowed", "equals": True}
    skipped = copy.deepcopy(evaluation)
    skipped["id"] = "evaluate_skipped"
    skipped["condition"]["equals"] = False
    body["nodes"].append(skipped)
    body["edges"].extend([{**edge, "target_node": "evaluate_skipped"}
                          for edge in body["edges"] if edge["target_node"] == "evaluate"])
    backend = ZenMLBackend(tmp_path / "state", python_executable=str(ROOT / ".venv-backend/bin/python"), source_root=source, trusted_code_roots=[str(source)])
    service = OperationService(tmp_path / "service.sqlite3", backend=backend, source_root=source)
    actor = Actor("conditional-checkpoint-owner", {"read", "modify", "execute"})
    def call(operation, **payload):
        return service.invoke(operation, payload, actor)
    # Governed checkpoint evaluations recheck current reviews on every invocation.
    from tests.integration.native_fixture_support import synthetic_review, register_project_review
    policy = synthetic_review(body, project, source)
    created = call("project.create", slug="conditional-checkpoint", model_name="Conditional checkpoint", recipe=body, idempotency_key="project")
    project = created["project_id"]
    register_project_review(service, policy, project)
    common = {"project_id": project, "recipe_id": created["recipe_id"]}
    def submit(nodes, bindings=()):
        payload = {**common, "expected_revision": 1, "trusted": True,
            "execution": {"node_ids": nodes, "input_bindings": list(bindings)}}
        assert call("run.plan", **payload)["executable"]
        submitted = call("run.submit", **payload, idempotency_key=str(uuid.uuid4()))
        request = {"project_id": project, "operation_id": submitted["operation_id"]}
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            result = call("run.status", **request)
            if result["status"] in {"succeeded", "failed", "cancelled"} and not result["worker_alive"] and not result["supervisor_alive"]:
                break
            time.sleep(.2)
        assert result["status"] == "succeeded", result
        return result, request, payload
    trained, training, _ = submit(["data", "initial", "train"])
    artifacts = call("run.artifacts", **training, include_binding_descriptors=True)["artifacts"]
    checkpoint = next(item for item in artifacts if item["node_id"] == "train")
    data = next(item for item in artifacts if item["node_id"] == "data")
    original_bytes = checkpoint["binding_descriptor"]["content_sha256"]
    native_version = backend.invoke("model.version.get", {"project_id": project, "model_version_ref": trained["model_version_ref"]})
    outcomes = []
    for _ in range(2):
        result, evaluation_request, evaluation_payload = submit(["gate", "evaluate", "evaluate_skipped"],
            [binding(artifact, node, port) for node in ("evaluate", "evaluate_skipped")
             for artifact, port in ((data, "data"), (checkpoint, "checkpoint"))])
        assert set(result["executed_node_ids"]) == {"gate", "evaluate"}
        assert result["skipped_node_ids"] == ["evaluate_skipped"]
        assert {entry["node_id"] for entry in result["evaluations"]} == {"evaluate"}
        assert {entry["model_version_ref"] for entry in result["evaluations"]} == {None}
        assert all(entry["checkpoint_verification"]["code"] == "checkpoint_verification_pending" for entry in result["evaluations"])
        assert not (backend.root / "jobs" / evaluation_request["operation_id"] / "outputs/train").exists()
        assert {entry["artifact_id"] for entry in result["execution"]["input_bindings"] if entry["input_port"] == "checkpoint"} == {checkpoint["artifact_id"]}
        assert {entry["content_sha256"] for entry in result["execution"]["input_bindings"] if entry["input_port"] == "checkpoint"} == {original_bytes}
        outcomes.append(result)
    compare = {"project_id": project, "run_ids": [result["backend_run_id"] for result in outcomes], "metric": "validation_loss", "direction": "minimize",
               "evaluations": [selector(result, "evaluate") for result in outcomes]}
    compared = call("run.compare", **compare)
    assert compared["compatible"] and compared["selected_model_version_ref"] == trained["model_version_ref"], compared
    invalid = copy.deepcopy(compare)
    invalid["evaluations"][0]["node_id"] = "evaluate_skipped"
    with pytest.raises(ServiceError, match="absent or ambiguous"):
        call("run.compare", **invalid)
    for change in ("hash", "project"):
        bad = copy.deepcopy(evaluation_payload)
        if change == "hash":
            bad["execution"]["input_bindings"][1]["content_sha256"] = "0" * 64
        else:
            bad["project_id"] = str(uuid.uuid4())
            with pytest.raises(ServiceError):
                call("run.plan", **bad)
            continue
        assert not call("run.plan", **bad)["executable"]
    assert backend.invoke("run.status", training)["status"] == "succeeded"
    assert backend.invoke("model.version.get", {"project_id": project, "model_version_ref": trained["model_version_ref"]}) == native_version
    hypothesis = call("hypothesis.create", project_id=project, proposed_change="Evaluate the selected checkpoint under an explicit gate",
        expected_outcome="Eligible held-out observations only", acceptance_metric={"name": "validation_loss", "direction": "minimize"}, idempotency_key="hypothesis")
    decision = call("decision.record", **common, recipe_revision=1, hypothesis_id=hypothesis["hypothesis_id"], run_ids=compare["run_ids"],
        selection_rule={"metric": "validation_loss", "direction": "minimize", "evaluations": compare["evaluations"],
                        "comparison_contract": compared["comparison_contract"], "comparison_digest": compared["comparison_digest"]},
        outcome="accepted", rationale="Only actual conditional evaluation outputs with verified selected checkpoint ownership", idempotency_key="decision")
    before = call("project.get", project_id=project)
    with pytest.raises(ServiceError) as wrong_owner:
        call("model.version.promote", project_id=project, model_version_id=outcomes[0]["model_version_ref"], decision_id=decision["decision_id"],
            expected_revision=before["revision"], stage="staging", idempotency_key="wrong-evaluator-owner")
    assert wrong_owner.value.code == "validation_error"
    assert call("project.get", project_id=project) == before
    promotion_payload = {"project_id": project, "model_version_id": trained["model_version_ref"], "decision_id": decision["decision_id"],
        "expected_revision": before["revision"], "stage": "staging", "idempotency_key": "verified-conditional-promotion"}
    promoted = call("model.version.promote", **promotion_payload)
    assert promoted["model_version_id"] == trained["model_version_ref"]
    assert promoted["selected_run_id"] == compared["selected_run_id"]
    assert call("model.version.promote", **promotion_payload) == promoted
    (tmp_path / "acceptance.json").write_text(json.dumps({"training": trained, "evaluations": outcomes, "comparison": compared,
        "decision": decision, "promotion": promoted, "wrong_evaluator_owner_rejected": True}, indent=2))
    service.store.close()
