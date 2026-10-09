from copy import deepcopy
import pytest
from modelbuilder.evaluation import (compare_records, digest, make_evaluation_record,
                                     validate_evaluation_record)


def evidence(value=.12, model="a", **updates):
    values = dict(dataset={"id": "forecast", "revision": "1", "content_digest": "d" * 64, "benchmark": "fixed-v1"},
        split={"id": "dev-v1", "role": "validation", "sample_ids": ["x", "y"]},
        protocol={"implementation": "forecast.evaluate", "version": "1", "configuration": {"horizon": 4}, "preprocessing": {"normalization": "none"}, "seed": 7},
        metric={"name": "rmse", "implementation": "torch.sqrt.mse", "version": "2", "parameters": {}, "aggregation": "root_mean_square", "direction": "minimize"},
        treatment={"model": model, "checkpoint": model, "calibration": None, "reference": None}, value=value)
    values.update(updates)
    return make_evaluation_record(**values)


def run(record, rid="a"):
    return {"backend_run_id": rid, "model_version_ref": rid, "status": "succeeded", "metrics": {"rmse": record["value"], "evaluation_record": record}}


def compare(a, b, contract=None):
    return compare_records([run(a), run(b, "b")], "rmse", "minimize", contract)


def reseal(record):
    record["digest"] = digest({k:v for k,v in record.items() if k != "digest"})
    return record


def test_same_benchmark_architecture_weights_are_valid_treatments():
    result = compare(evidence(), evidence(.08, "architecture-b"))
    assert result["compatible"] and result["selected_run_id"] == "b"
    assert result["comparison_digest"]


@pytest.mark.parametrize("section,key,value", [
    ("dataset", "content_digest", "different"), ("dataset", "benchmark", "benchmark-v2"),
    ("split", "sample_ids", ["x", "z"]), ("split", "role", "test"),
    ("split", "id", "dev-v2"), ("protocol", "configuration", {"horizon": 8}),
    ("protocol", "preprocessing", {"normalization": "standard"}),
    ("metric", "parameters", {"normalize": True}), ("metric", "version", "3"),
])
def test_changed_scientific_invariant_never_ranks(section, key, value):
    a, b = evidence(), evidence(.08, "b")
    b[section][key] = value
    result = compare(a, reseal(b))
    assert not result["compatible"] and result["selected_run_id"] is None
    assert result["diagnostics"][0]["field"] == section


def test_raw_calibrated_and_reference_interventions_require_explicit_contract():
    a, b = evidence(), evidence(.08, "b")
    b["treatment"].update(calibration={"digest":"temperature-artifact", "fit_split":"calibration-only"}, reference="frozen-teacher")
    reseal(b)
    assert not compare(a, b)["compatible"]
    assert compare(a, b, {"allowed_variations": ["model", "checkpoint", "calibration", "reference"]})["compatible"]


def test_unsafe_contract_cannot_allow_dataset_drift():
    assert not compare(evidence(), evidence(), {"allowed_variations": ["dataset"]})["compatible"]


def test_legacy_records_remain_intact_unknown_and_unranked():
    old = {"status":"succeeded", "backend_run_id":"old", "metrics":{"rmse":.01}}
    before = deepcopy(old)
    result = compare_records([old, run(evidence())], "rmse", "minimize")
    assert old == before and result["selected_run_id"] is None
    assert result["diagnostics"][0]["code"] == "unknown_identity"


def test_tampering_and_scalar_mismatch_are_rejected():
    a = evidence()
    a["value"] = .001
    assert validate_evaluation_record(a)
    assert not compare(a, evidence())["compatible"]
    r = run(evidence())
    r["metrics"]["rmse"] = .001
    assert not compare_records([r], "rmse", "minimize")["compatible"]


def test_requested_direction_cannot_reverse_metric_semantics():
    assert not compare_records([run(evidence())], "rmse", "maximize")["compatible"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "0.1"])
def test_nonfinite_or_nonnumeric_rejected(value):
    with pytest.raises((ValueError, TypeError)):
        evidence(value)


def test_paired_item_values_align_by_identity():
    a = evidence()
    a["metric"]["aggregation"] = "mean"
    a["predictions"] = [{"item_id":"x","value": .14}, {"item_id":"y","value":.10}]
    b = deepcopy(a)
    b["value"] = .08
    b["predictions"] = [{"item_id":"y","value":.06}, {"item_id":"x","value":.10}]
    result = compare(reseal(a), reseal(b), {"paired":True})
    assert result["compatible"]
    assert result["paired"][1]["mean_delta"] == pytest.approx(-.04)
    b["predictions"][0]["item_id"] = "z"
    assert not compare(a, reseal(b), {"paired":True})["compatible"]


def test_paired_rmse_does_not_claim_decomposable_mean():
    a = evidence(predictions=[{"item_id":"x","value":.1},{"item_id":"y","value":.2}])
    assert not compare(a, a, {"paired":True})["compatible"]


def test_record_copies_inputs_and_duplicate_items_rejected():
    split={"id":"dev", "role":"validation", "sample_ids":["x","y"]}
    record=evidence(split=split)
    split["sample_ids"].append("z")
    assert record["split"]["sample_ids"] == ["x","y"]
    split["sample_ids"] = ["x","x"]
    with pytest.raises(ValueError):
        evidence(split=split)


def test_mean_cannot_disagree_with_item_evidence():
    record = evidence()
    record["metric"]["aggregation"] = "mean"
    record["predictions"] = [{"item_id":"x","value":1.0},{"item_id":"y","value":1.0}]
    assert any(e["field"] == "value" for e in validate_evaluation_record(reseal(record)))
