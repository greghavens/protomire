from copy import deepcopy
import hashlib
import json
import tarfile
from types import SimpleNamespace as NS

import pytest
pytest.importorskip("zenml")
from modelbuilder.backend import sdk
from modelbuilder.backend.sdk import metrics_for, evaluation_outputs, compare_evaluations
from modelbuilder.backend.admission import BackendError
from modelbuilder.backend.common import recipe_provenance, write_json
from modelbuilder.backend.components import identity
from modelbuilder.backend.native_inputs import storage_identity
from test_identity import evidence


def checkpoint(tmp_path, run_id, *, fixture=False):
    directory = tmp_path / ("checkpoint-" + run_id)
    directory.mkdir(parents=True)
    (directory / "weights").write_bytes(run_id.encode())
    descriptor = {"schema_version": "modelbuilder.checkpoint/v1", "purpose": "selected", "step": 1,
        "resume_modes": [], "identities": dict.fromkeys(("architecture", "source", "dependencies", "training", "environment"), "pinned"),
        "assets": [{"path": "weights", "sha256": hashlib.sha256(run_id.encode()).hexdigest()}]}
    if fixture:
        (directory / "best.pt").write_bytes(b"best" + run_id.encode())
        (directory / "checkpoint.pt").write_bytes(b"last" + run_id.encode())
        digest = hashlib.sha256((directory / "best.pt").read_bytes()).hexdigest()
    else:
        (directory / "checkpoint.manifest.json").write_text(json.dumps(descriptor))
        digest = identity(descriptor)
    store = tmp_path / "zenml-stores" / run_id
    store.mkdir(parents=True)
    with tarfile.open(store / "data.tar.gz", "w:gz") as archive:
        for file in directory.iterdir():
            archive.add(file, arcname=file.name)
    artifact = NS(id="checkpoint-" + run_id, project=NS(id="native-project"), uri=str(store),
        data_type=NS(import_path="pathlib.PosixPath"),
        materializer=NS(import_path="zenml.materializers.path_materializer.PathMaterializer",
                        module="zenml.materializers.path_materializer", attribute="PathMaterializer"))
    return artifact, digest


def native(tmp_path, run_id, copies=2, *, bound=False, fixture=False):
    ck, digest = checkpoint(tmp_path, run_id, fixture=fixture)
    record = evidence(treatment={"model": run_id, "checkpoint": digest, "calibration": None, "reference": None})
    measured = {"rmse": record["value"], "evaluation_record": record}
    def artifact(index):
        directory = tmp_path / "zenml-stores" / f"{run_id}-metrics-{index}"
        directory.mkdir()
        (directory / "data.json").write_text(json.dumps(measured))
        return NS(id=f"{run_id}-artifact-{index}", uri=str(directory), project=NS(id="native-project"), data_type=NS(import_path="builtins.dict"),
            materializer=NS(import_path="zenml.materializers.built_in_materializer.BuiltInContainerMaterializer"),
            load=lambda: deepcopy(measured))
    trainer = {"id": "train", "kind": "model.train"}
    evaluators = [{"id": f"evaluation-{index}", "kind": "model.evaluate" if fixture else "component.run",
        "component": {"role": "evaluation"}, "inputs": [{"name": "checkpoint", "type": "Path"}]} for index in range(copies)]
    recipe = {"recipe_id": "recipe", "revision": 1, "nodes": [trainer, *evaluators],
              "edges": [{"source_node": "train", "source_port": "checkpoint", "target_node": node["id"], "target_port": "checkpoint"} for node in evaluators]}
    bindings = [{"node_id": node["id"], "input_port": "checkpoint", "artifact_id": ck.id,
        "producer_role": "training", "producer_model_version_ref": "producer-" + run_id,
        "declared_type": "Path", "content_sha256": storage_identity(ck, tmp_path)} for node in evaluators] if bound else []
    request = {"recipe": recipe, "root": str(tmp_path), "project_id": "project", "resolved_input_bindings": bindings}
    extra = {**recipe_provenance(recipe), "application_project_id": "project",
             "execution": {"node_ids": [node["id"] for node in (evaluators if bound else recipe["nodes"])], "input_bindings": deepcopy(bindings)}}
    steps = {node["id"]: NS(inputs={"checkpoint": [ck]}, outputs={"metrics": [artifact(index)]}) for index, node in enumerate(evaluators)}
    if not bound:
        steps["train"] = NS(inputs={}, outputs={"checkpoint": [ck]})
    run = NS(id=run_id, project=NS(id="native-project"), model_version=NS(id="model-" + run_id),
             config=NS(extra=extra), steps=steps, status=sdk.ExecutionStatus.COMPLETED)
    return run, request, measured


def status(native):
    run, request, _ = native
    entries = evaluation_outputs(run, request)
    return {"backend_run_id": run.id, "status": "succeeded", "metrics": metrics_for(run, entries),
            "evaluations": entries, "model_version_ref": "model-" + run.id}


def selector(record, index=0):
    return {key: record["evaluations"][index][key] for key in ("backend_run_id", "node_id", "output_port", "artifact_id", "metric")}


def test_identical_duplicate_native_outputs_remain_ambiguous(tmp_path):
    records = [status(native(tmp_path, "a")), status(native(tmp_path, "b"))]
    assert all("rmse" not in row["metrics"] for row in records)
    payload = {"metric": "rmse", "direction": "minimize"}
    assert not compare_evaluations(records, payload)["compatible"]
    explicit = {**payload, "evaluations": [selector(row) for row in records]}
    result = compare_evaluations(records, explicit)
    assert result["compatible"]
    assert result["selected_evaluation"] == explicit["evaluations"][0]
    assert result["evaluations"] == explicit["evaluations"]


@pytest.mark.parametrize("field", ["artifact_id", "node_id", "output_port", "metric", "backend_run_id"])
def test_selector_mismatch_is_denied(field, tmp_path):
    records = [status(native(tmp_path, "a")), status(native(tmp_path, "b"))]
    selectors = [selector(row) for row in records]
    selectors[0][field] = "wrong"
    with pytest.raises(BackendError):
        compare_evaluations(records, {"metric": "rmse", "direction": "minimize", "evaluations": selectors})


def test_same_run_or_multiple_selectors_cannot_be_silently_collapsed(tmp_path):
    record = status(native(tmp_path, "a"))
    with pytest.raises(BackendError):
        compare_evaluations([record, record], {"metric": "rmse", "direction": "minimize", "evaluations": [selector(record), selector(record, 1)]})


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("fixture", [False, True])
def test_correct_checkpoint_identity_and_actual_owner_pass(tmp_path, bound, fixture):
    record = status(native(tmp_path, "a", copies=1, bound=bound, fixture=fixture))
    result = compare_evaluations([record], {"metric": "rmse"})
    assert result["compatible"], result
    assert result["selected_model_version_ref"] == ("producer-a" if bound else "model-a")
    assert record["evaluations"][0]["checkpoint_verification"]["verified"] is True


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_stale_other_checkpoint_cannot_compare_or_promote(tmp_path, monkeypatch, bound, explicit):
    value = native(tmp_path, "a", copies=1, bound=bound)
    run, request, measured = value
    # A canonical, valid evidence record can still be stale or misattributed.
    measured["evaluation_record"] = evidence(treatment={"model": "B", "checkpoint": "unrelated-B", "calibration": None, "reference": None})
    record = status(value)
    other_value = native(tmp_path, "b", copies=1)
    other_record = status(other_value)
    assert record["metrics"]["rmse"] == measured["rmse"]  # inspection remains available
    assert record["evaluations"][0]["model_version_ref"] is None
    payload = {"metric": "rmse", **({"evaluations": [selector(record), selector(other_record)]} if explicit else {})}
    result = compare_evaluations([record, other_record], payload)
    assert not result["compatible"] and result["selected_model_version_ref"] is None
    assert result["diagnostics"][-1]["code"] == "evaluation_checkpoint_unverified"
    assert "comparison_digest" not in result
    assert all("delta_from_selected" not in row for row in result["runs"])
    job = tmp_path / "jobs" / "a"
    write_json(job / "request.json", request)
    write_json(job / "native.json", {"backend_run_id": "a"})
    write_json(tmp_path / "jobs/b/request.json", other_value[1])
    write_json(tmp_path / "jobs/b/native.json", {"backend_run_id": "b"})
    mutations = []
    client = NS(get_pipeline_run=lambda rid, **kwargs: run if rid == "a" else other_value[0],
        update_model_version=lambda *args, **kwargs: mutations.append("promote"))
    monkeypatch.setattr(sdk, "project_client", lambda _: (client, run.project))
    monkeypatch.setattr(sdk, "find_version", lambda *args: NS(id="producer-a" if bound else "model-a"))
    monkeypatch.setattr(sdk, "log_metadata", lambda *args, **kwargs: mutations.append("metadata"))
    with pytest.raises(BackendError) as error:
        sdk.invoke("model.version.promote", {**payload, "project_id": "project", "candidate_run_ids": ["a", "b"]}, tmp_path)
    assert error.value.code == "incompatible_evaluation"
    assert not mutations


@pytest.mark.parametrize("damage", ["foreign_project", "wrong_artifact", "changed_binding", "changed_storage", "unsupported", "legacy"])
def test_unproven_checkpoint_keeps_evidence_but_no_winner(tmp_path, damage):
    value = native(tmp_path, "a", copies=1, bound=True)
    run, request, measured = value
    ck = run.steps["evaluation-0"].inputs["checkpoint"][0]
    if damage == "foreign_project":
        ck.project.id = "other"
    elif damage == "wrong_artifact":
        ck.id = "other"
    elif damage == "changed_binding":
        request["resolved_input_bindings"][0]["producer_model_version_ref"] = "other"
    elif damage == "changed_storage":
        with open(ck.uri + "/data.tar.gz", "ab") as file:
            file.write(b"changed")
    elif damage == "unsupported":
        ck.materializer.import_path = "untrusted.CustomMaterializer"
    else:
        request.clear()
    record = status(value)
    assert record["evaluations"][0]["record"] == measured["evaluation_record"]
    assert not compare_evaluations([record], {"metric": "rmse"})["compatible"]


def test_multiple_checkpoints_use_matching_port_not_other_bound_teacher(tmp_path):
    value = native(tmp_path, "a", copies=1)
    run, request, _ = value
    other, _ = checkpoint(tmp_path, "teacher")
    request["recipe"]["nodes"][1]["inputs"].append({"name": "teacher", "type": "Path"})
    binding = {"node_id": "evaluation-0", "input_port": "teacher", "artifact_id": other.id, "producer_role": "training",
        "producer_model_version_ref": "teacher-model", "content_sha256": storage_identity(other, tmp_path)}
    request["resolved_input_bindings"].append(binding)
    run.config.extra.update(recipe_provenance(request["recipe"]))
    run.config.extra["execution"]["input_bindings"] = deepcopy(request["resolved_input_bindings"])
    run.steps["evaluation-0"].inputs["teacher"] = [other]
    result = compare_evaluations([status(value)], {"metric": "rmse"})
    assert result["compatible"] and result["selected_model_version_ref"] == "model-a"
    # Identical checkpoint bytes associated with different versions are ambiguous.
    original = run.steps["evaluation-0"].inputs["checkpoint"][0]
    run.steps["evaluation-0"].inputs["teacher"] = [original]
    binding.update(artifact_id=original.id, content_sha256=storage_identity(original, tmp_path))
    run.config.extra["execution"]["input_bindings"] = deepcopy(request["resolved_input_bindings"])
    result = compare_evaluations([status(value)], {"metric": "rmse"})
    assert not result["compatible"] and "multiple model owners" in result["diagnostics"][-1]["message"]


def test_status_evidence_path_does_not_open_large_checkpoints(tmp_path, monkeypatch):
    value = native(tmp_path, "a", copies=1)
    run, request, measured = value
    ck = run.steps["evaluation-0"].inputs["checkpoint"][0]
    with open(ck.uri + "/data.tar.gz", "wb") as stream:
        stream.truncate(8 * 1024**3)  # sparse; no large allocation
    def forbidden(*args, **kwargs):
        pytest.fail("Polling must not read checkpoint storage")
    monkeypatch.setattr("modelbuilder.backend.evaluation_binding.storage_identity", forbidden)
    monkeypatch.setattr("modelbuilder.backend.evaluation_binding.native_directory", forbidden)
    entries = evaluation_outputs(run, request, verify_checkpoints=False)
    assert entries[0]["record"] == measured["evaluation_record"]
    assert entries[0]["model_version_ref"] is None
    assert entries[0]["checkpoint_verification"]["code"] == "checkpoint_verification_pending"


def test_supported_legacy_full_run_does_not_require_new_execution_metadata(tmp_path):
    value = native(tmp_path, "a", copies=1)
    value[0].config.extra.pop("execution")
    assert compare_evaluations([status(value)], {"metric": "rmse"})["compatible"]


def test_malformed_treatment_remains_inspectable_and_unranked(tmp_path):
    value = native(tmp_path, "a", copies=1)
    value[2]["evaluation_record"]["treatment"] = None
    record = status(value)
    assert record["evaluations"][0]["record"]["treatment"] is None
    assert not compare_evaluations([record], {"metric": "rmse"})["compatible"]


@pytest.mark.parametrize("bound_calibration", [False, True])
def test_utility_calibration_path_does_not_poison_checkpoint_attribution(tmp_path, bound_calibration):
    value = native(tmp_path, "a", copies=1)
    run, request, _ = value
    calibration, _ = checkpoint(tmp_path, "calibration")
    evaluator = request["recipe"]["nodes"][1]
    evaluator["inputs"].append({"name": "calibration", "type": "Path"})
    run.steps["evaluation-0"].inputs["calibration"] = [calibration]
    if bound_calibration:
        request["resolved_input_bindings"].append({"node_id": "evaluation-0", "input_port": "calibration",
            "artifact_id": calibration.id, "producer_role": "utility", "content_sha256": storage_identity(calibration, tmp_path)})
        run.config.extra["execution"]["input_bindings"] = deepcopy(request["resolved_input_bindings"])
    else:
        request["recipe"]["nodes"].append({"id": "calibrate", "kind": "component.run", "component": {"role": "utility"}})
        request["recipe"]["edges"].append({"source_node": "calibrate", "source_port": "calibration",
            "target_node": "evaluation-0", "target_port": "calibration"})
        run.steps["calibrate"] = NS(inputs={}, outputs={"calibration": [calibration]})
        run.config.extra["execution"]["node_ids"].append("calibrate")
    run.config.extra.update(recipe_provenance(request["recipe"]))
    result = compare_evaluations([status(value)], {"metric": "rmse"})
    assert result["compatible"] and result["selected_model_version_ref"] == "model-a"
    # An actual training checkpoint failure must not disappear behind calibration.
    run.steps["evaluation-0"].inputs["checkpoint"][0].project.id = "foreign"
    assert not compare_evaluations([status(value)], {"metric": "rmse"})["compatible"]
