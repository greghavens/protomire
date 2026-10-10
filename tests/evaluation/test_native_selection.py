from copy import deepcopy
from types import SimpleNamespace

import pytest
pytest.importorskip("zenml")
from modelbuilder.backend.sdk import metrics_for, evaluation_outputs, compare_evaluations
from modelbuilder.backend.admission import BackendError
from test_identity import evidence


def native(run_id, copies=2):
    record = evidence()
    measured = {"rmse": record["value"], "evaluation_record": record}
    def artifact(index):
        return SimpleNamespace(id=f"{run_id}-artifact-{index}", data_type=SimpleNamespace(import_path="builtins.dict"),
            materializer=SimpleNamespace(import_path="zenml.materializers.built_in_materializer.BuiltInContainerMaterializer"),
            load=lambda: deepcopy(measured))
    return SimpleNamespace(id=run_id, model_version=SimpleNamespace(id="model-" + run_id),
        steps={f"evaluation-{index}": SimpleNamespace(outputs={"metrics": [artifact(index)]}) for index in range(copies)})


def status(run):
    entries = evaluation_outputs(run)
    return {"backend_run_id": run.id, "status": "succeeded", "metrics": metrics_for(run, entries),
            "evaluations": entries, "model_version_ref": "model-" + run.id}


def selector(record, index=0):
    return {key: record["evaluations"][index][key] for key in ("backend_run_id", "node_id", "output_port", "artifact_id", "metric")}


def test_identical_duplicate_native_outputs_remain_ambiguous():
    records = [status(native("a")), status(native("b"))]
    assert all("rmse" not in row["metrics"] for row in records)
    payload = {"metric": "rmse", "direction": "minimize"}
    assert not compare_evaluations(records, payload)["compatible"]
    explicit = {**payload, "evaluations": [selector(row) for row in records]}
    result = compare_evaluations(records, explicit)
    assert result["compatible"]
    assert result["selected_evaluation"] == explicit["evaluations"][0]
    assert result["evaluations"] == explicit["evaluations"]


@pytest.mark.parametrize("field", ["artifact_id", "node_id", "output_port", "metric", "backend_run_id"])
def test_selector_mismatch_is_denied(field):
    records = [status(native("a")), status(native("b"))]
    selectors = [selector(row) for row in records]
    selectors[0][field] = "wrong"
    with pytest.raises(BackendError):
        compare_evaluations(records, {"metric": "rmse", "direction": "minimize", "evaluations": selectors})


def test_same_run_or_multiple_selectors_cannot_be_silently_collapsed():
    record = status(native("a"))
    with pytest.raises(BackendError):
        compare_evaluations([record, record], {"metric": "rmse", "direction": "minimize", "evaluations": [selector(record), selector(record, 1)]})
