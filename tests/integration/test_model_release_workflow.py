"""Real shared-service selection, native promotion and saved-package fresh reload."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import time
import zipfile

import pytest

from modelbuilder.backend import ZenMLBackend
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration


def test_two_independent_runs_decision_promotion_save_fresh_reload(tmp_path):
    backend_python = ROOT / ".venv-backend/bin/python"
    if not backend_python.is_file():
        pytest.skip("Pinned native backend required")
    owner = Actor("owner", {"read", "modify", "execute"})
    service = OperationService(tmp_path / "service.sqlite", ZenMLBackend(tmp_path, python_executable=str(backend_python), source_root=ROOT), source_root=ROOT)
    def call(op, **payload):
        return service.invoke(op, payload, owner)
    path = "src/modelbuilder/backend/fixtures.py"
    commit = subprocess.check_output(["git", "log", "-1", "--format=%H", "--", path], cwd=ROOT, text=True).strip()
    assert subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=ROOT) == (ROOT / path).read_bytes()
    body = json.loads((ROOT / "examples/custom-pytorch/recipe.json").read_text())
    body["source"].update(commit=commit, files=[{"path": path, "sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest()}])
    body.pop("digest", None)
    next(node for node in body["nodes"] if node["kind"] == "model.train")["parameters"]["steps"] = 3
    project = call("project.create", slug="release", model_name="Release model", recipe=body, idempotency_key="project")
    project_id = project["project_id"]
    request = {"project_id": project_id, "recipe_id": project["recipe_id"]}
    runs = []
    recipe = call("recipe.get", **request)
    for index in range(3):
        if index:
            recipe = copy.deepcopy(recipe)
            next(node for node in recipe["nodes"] if node["kind"] == "model.train")["parameters"]["steps"] = 5
            if index == 2:
                next(node for node in recipe["nodes"] if node["kind"] == "dataset.synthetic")["parameters"]["seed"] = 2345
            recipe = call("recipe.update", **request, recipe=recipe, expected_revision=recipe["revision"], idempotency_key=f"recipe-{index}")
        submitted = call("run.submit", **request, expected_revision=recipe["revision"], trusted=True, idempotency_key=f"run-{index}")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            run = call("run.status", project_id=project_id, operation_id=submitted["operation_id"])
            if run["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.5)
        assert run["status"] == "succeeded", run
        assert not run.get("parent_run_id")
        runs.append(run)
    common = {"project_id": project_id, "metric": "validation_loss", "direction": "minimize"}
    ids = [run["backend_run_id"] for run in runs[:2]]
    comparison = call("run.compare", **common, run_ids=ids)
    assert comparison["compatible"] and comparison["selected_run_id"] in ids
    assert comparison["configuration_diffs"][0]["diff"] and len(comparison["run_provenance"]) == 2
    winner = next(run for run in runs if run["backend_run_id"] == comparison["selected_run_id"])
    hypothesis = call("hypothesis.create", project_id=project_id, proposed_change="Train for five steps", expected_outcome="Lower validation loss", acceptance_metric={"name": "validation_loss", "direction": "minimize"}, idempotency_key="hypothesis")
    decision = call("decision.record", **request, recipe_revision=winner["recipe_revision"], hypothesis_id=hypothesis["hypothesis_id"], run_ids=ids,
                    selection_rule={"metric": "validation_loss", "direction": "minimize", "comparison_contract": comparison["comparison_contract"], "comparison_digest": comparison["comparison_digest"]},
                    outcome="accepted", rationale="Measured common validation protocol", idempotency_key="decision")
    promoted = call("model.version.promote", project_id=project_id, model_version_id=winner["model_version_ref"], decision_id=decision["decision_id"], expected_revision=project["revision"], stage="staging", idempotency_key="promote")
    assert promoted["selected_run_id"] == winner["backend_run_id"]
    with pytest.raises(ServiceError) as stale:
        call("model.version.promote", project_id=project_id, model_version_id=winner["model_version_ref"], decision_id=decision["decision_id"], expected_revision=project["revision"], stage="staging", idempotency_key="stale-promote")
    assert stale.value.status_code == 409
    selection = {"project_id": project_id, "model_version_id": promoted["model_version_id"]}
    exports = call("model.export.list", **selection)
    assert len(exports["exports"]) == 1, exports
    descriptor = exports["exports"][0]
    identity = {key: descriptor["provenance"][key] for key in ("project_id", "backend_run_id", "model_version_id", "artifact_id")}
    assert descriptor["manifest"]["selected_checkpoint"] == descriptor["provenance"]["selected_checkpoints"][0]["identity"]
    inspected = call("model.export.inspect", **identity)
    assert inspected == descriptor
    reader = Actor("reader", {"read"}, {project_id})
    transfer = service.invoke("model.export.package", {**identity, "expected_export_digest": descriptor["export_digest"]}, reader)
    target = tmp_path / "saved-model.zip"
    with target.open("wb") as output:
        for index in range(len(transfer["chunk_sha256"])):
            chunk = service.invoke("model.export.read", {**identity, "package_id": transfer["package_id"], "chunk_index": index}, reader)
            output.write(base64.b64decode(chunk["data_base64"]))
    assert hashlib.sha256(target.read_bytes()).hexdigest() == transfer["sha256"]
    extracted = tmp_path / "reloaded"
    with zipfile.ZipFile(target) as archive:
        archive.extractall(extracted)
    assert json.loads((extracted / "modelbuilder.provenance.json").read_text())["selection"]["selection_decision"]["decision_id"] == decision["decision_id"]
    # Fresh Python imports only the saved loader, not the repository's model code.
    script = '''import importlib.util,json,sys,torch
from pathlib import Path
root=Path(sys.argv[1]); spec=importlib.util.spec_from_file_location('saved_loader',root/'loader.py'); module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
model=module.load(root); x=torch.randn(240,6,generator=torch.Generator().manual_seed(1234))[-30:]
with torch.no_grad(): actual=model(x)
expected=torch.tensor(json.loads(sys.argv[2])); difference=float((actual-expected).abs().max());print(json.dumps({'max_abs_difference':difference}));assert difference==0.0
'''
    result = subprocess.run([str(backend_python), "-I", "-c", script, str(extracted), json.dumps(winner["metrics"]["heldout_logits"])], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    other = call("project.create", slug="other-release", model_name="Other", idempotency_key="other")
    for operation, payload in [("model.export.inspect", identity), ("model.export.package", {**identity, "expected_export_digest": descriptor["export_digest"]}), ("model.export.read", {**identity, "package_id": transfer["package_id"], "chunk_index": 0})]:
        with pytest.raises(ServiceError):
            call(operation, **{**payload, "project_id": other["project_id"]})
    with pytest.raises(ServiceError) as wrong_version:
        call("model.export.inspect", **{**identity, "model_version_id": runs[2]["model_version_ref"]})
    assert wrong_version.value.code == "not_found"
    incompatible = call("run.compare", **common, run_ids=[runs[0]["backend_run_id"], runs[2]["backend_run_id"]])
    assert not incompatible["compatible"] and incompatible["selected_run_id"] is None
    bad_decision = call("decision.record", **request, recipe_revision=runs[0]["recipe_revision"], hypothesis_id=hypothesis["hypothesis_id"], run_ids=[runs[0]["backend_run_id"], runs[2]["backend_run_id"]], selection_rule={"metric": "validation_loss", "direction": "minimize"}, outcome="accepted", rationale="Legacy caller bypasses comparison UI; promotion must still reject", idempotency_key="legacy-decision")
    with pytest.raises(ServiceError) as mismatch:
        call("model.version.promote", project_id=project_id, model_version_id=runs[0]["model_version_ref"], decision_id=bad_decision["decision_id"], expected_revision=project["revision"]+1, stage="staging", idempotency_key="mismatch")
    assert mismatch.value.code == "incompatible_evaluation"
    evidence = {"project_id": project_id, "independent_run_ids": ids, "comparison_digest": comparison["comparison_digest"], "decision_id": decision["decision_id"], "promoted_version_id": promoted["model_version_id"], "package_sha256": transfer["sha256"], "selected_checkpoint": descriptor["manifest"]["selected_checkpoint"], "fresh_process_reload": json.loads(result.stdout), "incompatible_evaluation_no_winner_or_promotion": True}
    (tmp_path / "release-evidence.json").write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence))
    service.store.close()
