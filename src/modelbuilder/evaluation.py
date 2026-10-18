"""Immutable evaluation evidence and fail-closed scientific comparisons.

Records are evidence produced by execution, never inferred from mutable recipes.
A missing historical record stays unknown; reevaluation creates new evidence.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from numbers import Real

SCHEMA = "modelbuilder.evaluation/v1"
TREATMENTS = {"model", "checkpoint", "architecture", "objective", "calibration", "reference"}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _finite(value: object) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def validate_evaluation_record(record: dict) -> list[dict]:
    """Return diagnostics without modifying or upgrading historical evidence."""
    errors = []
    def fail(field, message):
        errors.append({"field": field, "message": message})
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        return [{"field": "schema", "message": "Missing or unsupported evaluation identity; reevaluate on a pinned protocol"}]
    for section, required in {
        "dataset": ("id", "revision", "content_digest", "benchmark"),
        "split": ("id", "role", "sample_ids"),
        "protocol": ("implementation", "version", "configuration", "preprocessing", "seed"),
        "metric": ("name", "implementation", "version", "parameters", "aggregation", "direction"),
        "treatment": ("model", "checkpoint", "calibration", "reference"),
    }.items():
        value = record.get(section)
        if not isinstance(value, dict):
            fail(section, "Required identity object is absent")
            continue
        for key in required:
            if key not in value:
                fail(f"{section}.{key}", "Required identity field is absent (use explicit null only for absent calibration/reference)")
        for key in required:
            if key in {"calibration", "reference", "seed"}:
                continue
            if value.get(key) is None or value.get(key) == "":
                fail(f"{section}.{key}", "Identity must be specified")
    split = record.get("split", {})
    ids = split.get("sample_ids", []) if isinstance(split, dict) else []
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not i for i in ids):
        fail("split.sample_ids", "Nonempty string sample identities are required")
    elif len(ids) != len(set(ids)):
        fail("split.sample_ids", "Sample identities must be unique")
    for section, fields in {"dataset": ("id", "revision", "content_digest", "benchmark"),
                            "split": ("id", "role"),
                            "protocol": ("implementation", "version"),
                            "metric": ("name", "implementation", "version", "aggregation")}.items():
        obj = record.get(section)
        if isinstance(obj, dict):
            for field in fields:
                if not isinstance(obj.get(field), str) or not obj[field].strip():
                    fail(f"{section}.{field}", "A nonempty string identity is required")
    for section, fields in {"protocol": ("configuration", "preprocessing"), "metric": ("parameters",)}.items():
        obj = record.get(section)
        if isinstance(obj, dict):
            for field in fields:
                if not isinstance(obj.get(field), dict):
                    fail(f"{section}.{field}", "An explicit configuration object is required")
    metric = record.get("metric", {})
    if isinstance(metric, dict) and metric.get("direction") not in {"minimize", "maximize"}:
        fail("metric.direction", "Must be minimize or maximize")
    if not _finite(record.get("value")):
        fail("value", "Metric must be a finite scalar")
    predictions = record.get("predictions")
    if predictions is not None:
        if not isinstance(predictions, list) or any(not isinstance(p, dict) or not isinstance(p.get("item_id"), str) for p in predictions):
            fail("predictions", "Predictions must be item-addressed records")
        else:
            prediction_ids = [p["item_id"] for p in predictions]
            if len(set(prediction_ids)) != len(prediction_ids) or set(prediction_ids) != set(i for i in (ids if isinstance(ids, list) else []) if isinstance(i, str)):
                fail("predictions", "Prediction identities must exactly cover the evaluation sample set")
            if (isinstance(metric, dict) and metric.get("aggregation") == "mean" and predictions
                    and all(_finite(p.get("value")) for p in predictions) and _finite(record.get("value"))):
                measured = sum(p["value"] for p in predictions) / len(predictions)
                if not math.isclose(measured, record["value"], rel_tol=1e-6, abs_tol=1e-8):
                    fail("value", "Mean metric disagrees with recorded per-item values")
            if isinstance(metric, dict) and metric.get("aggregation") in {"token_weighted_mean", "exp_token_weighted_mean"}:
                valid = predictions and all(_finite(p.get("negative_log_likelihood_sum")) and p["negative_log_likelihood_sum"] >= 0
                    and type(p.get("token_count")) is int and p["token_count"] > 0 for p in predictions)
                if not valid:
                    fail("predictions", "Token-weighted metrics require finite loss sums and positive supervised token counts")
                elif _finite(record.get("value")):
                    measured = sum(p["negative_log_likelihood_sum"] for p in predictions) / sum(p["token_count"] for p in predictions)
                    if metric["aggregation"] == "exp_token_weighted_mean":
                        try:
                            measured = math.exp(measured)
                        except OverflowError:
                            measured = float("inf")
                    if not math.isclose(measured, record["value"], rel_tol=1e-6, abs_tol=1e-8):
                        fail("value", "Token-weighted metric disagrees with recorded per-item contributions")
    family = record.get("protocol", {}).get("configuration", {}).get("family") if isinstance(record.get("protocol"), dict) and isinstance(record.get("protocol", {}).get("configuration"), dict) else None
    if family is not None:
        if not isinstance(predictions, list):
            fail("predictions", "Family evaluation requires exact item-addressed measured contributions")
        if (not isinstance(family, dict) or type(family.get("minimum_support")) is not int
                or family["minimum_support"] < 1 or len(ids) < family["minimum_support"]
                or family.get("cohort_digest") != digest(ids)
                or not isinstance(family.get("policy_id"), str) or not family["policy_id"].startswith("sha256:")
                or not isinstance(family.get("family_id"), str) or not family["family_id"]):
            fail("protocol.configuration.family", "Family record requires pinned policy, cohort and sufficient exact item support")
    try:
        expected = digest({k: v for k, v in record.items() if k != "digest"})
        if record.get("digest") != expected:
            fail("digest", "Evaluation evidence digest is absent or differs from content")
    except (ValueError, TypeError):
        fail("digest", "Evaluation evidence must be finite JSON")
    return errors


def make_evaluation_record(*, dataset: dict, split: dict, protocol: dict, metric: dict,
                           treatment: dict, value: float, predictions: list | None = None,
                           provenance: dict | None = None) -> dict:
    record = deepcopy({"schema": SCHEMA, "dataset": dataset, "split": split, "protocol": protocol,
                       "metric": metric, "treatment": treatment, "value": value})
    if predictions is not None:
        record["predictions"] = deepcopy(predictions)
    if provenance is not None:
        record["provenance"] = deepcopy(provenance)
    record["digest"] = digest(record)
    errors = validate_evaluation_record(record)
    if errors:
        raise ValueError(f"Invalid evaluation evidence: {errors}")
    return record


def evaluation_for(metrics: dict, metric: str) -> dict | None:
    collection = metrics.get("evaluation_records", {})
    record = collection.get(metric) if isinstance(collection, dict) else None
    if record is None:
        record = metrics.get("evaluation_record")
    return record if isinstance(record, dict) and isinstance(record.get("metric"), dict) and record["metric"].get("name") == metric else None


def compare_records(records: list[dict], metric: str, direction: str,
                    comparison_contract: dict | None = None) -> dict:
    """Compare run-shaped records; return no winner when evidence is incompatible.

    Model and checkpoint are the ordinary experimental variables. Other treatment
    dimensions must be deliberately named. Data/metric/protocol drift is never an
    allowed intervention: reevaluate against a common benchmark first.
    """
    contract = deepcopy(comparison_contract or {"allowed_variations": ["model", "checkpoint"]})
    allowed = contract.get("allowed_variations", ["model", "checkpoint"])
    diagnostics = []
    def issue(code, field, message, run_id=None):
        diagnostics.append({"code": code, "field": field, "message": message, "backend_run_id": run_id})
    if not isinstance(allowed, list) or any(not isinstance(x, str) or x not in TREATMENTS for x in allowed):
        issue("invalid_contract", "allowed_variations", "Only declared treatment identities may vary")
        allowed = []
    if set(contract) - {"allowed_variations", "paired"} or not isinstance(contract.get("paired", False), bool):
        issue("invalid_contract", "comparison_contract", "Unknown contract fields or non-boolean paired flag")
    if direction not in {"minimize", "maximize"}:
        issue("invalid_direction", "direction", "Direction must be minimize or maximize")
    if not records:
        issue("missing_runs", "runs", "At least one evaluated run is required")
    evidence = []
    rows = []
    for run in records:
        rid = run.get("backend_run_id")
        metrics = run.get("metrics", {})
        value = metrics.get(metric)
        rows.append({"backend_run_id": rid, "model_version_ref": run.get("model_version_ref"), "value": value})
        if run.get("status") != "succeeded" or not _finite(value):
            issue("missing_measurement", metric, "A successful run with a finite metric is required", rid)
        if metric in metrics.get("evaluation_conflicts", {}):
            issue("ambiguous_evaluation", metric, "Multiple distinct native evaluations publish this metric; select an explicit evaluation", rid)
        record = evaluation_for(metrics, metric)
        if record is None:
            issue("unknown_identity", "evaluation", "Historical measurement lacks immutable identity; retain it and reevaluate on a pinned common protocol", rid)
            continue
        errors = validate_evaluation_record(record)
        for error in errors:
            issue("invalid_identity", error["field"], error["message"], rid)
        if errors:
            continue
        evidence.append((rid, record))
        if record["value"] != value:
            issue("measurement_mismatch", "value", "Scalar differs from recorded evaluation evidence", rid)
        if record["metric"]["direction"] != direction:
            issue("direction_mismatch", "metric.direction", "Requested direction contradicts metric definition", rid)
    if evidence:
        first = evidence[0][1]
        for rid, record in evidence[1:]:
            for field in ("dataset", "split", "protocol", "metric"):
                # Item order is part of protocol, too: order-sensitive evaluators
                # cannot silently reorder a benchmark merely to obtain a match.
                if record[field] != first[field]:
                    changed = [k for k in set(record[field]) | set(first[field]) if record[field].get(k) != first[field].get(k)]
                    issue("incompatible_evaluation", field, f"Changed {field} fields: {', '.join(sorted(changed))}; reevaluate using one common protocol", rid)
            for key in set(first["treatment"]) | set(record["treatment"]):
                if record["treatment"].get(key) != first["treatment"].get(key) and key not in allowed:
                    issue("undeclared_intervention", f"treatment.{key}", f"Declare {key} as an intended experimental variation", rid)
    result = {"metric": metric, "direction": direction, "compatible": not diagnostics,
              "comparison_contract": contract, "diagnostics": diagnostics,
              "selected_run_id": None, "selected_model_version_ref": None, "runs": rows,
              "evaluation_digests": {rid: e["digest"] for rid, e in evidence}}
    if contract.get("paired") and not diagnostics:
        base = evidence[0][1]
        pairs = []
        for rid, record in evidence:
            pred = record.get("predictions")
            if not pred or any(not _finite(p.get("value")) for p in pred):
                issue("paired_data_unavailable", "predictions", "Paired comparison requires finite per-item metric values on matching item IDs", rid)
                continue
            if record["metric"]["aggregation"] != "mean":
                issue("paired_aggregation_unsupported", "metric.aggregation", "Mean item deltas require a decomposable mean metric", rid)
                continue
            baseline = base.get("predictions", [])
            lookup = {p["item_id"]: p.get("value") for p in baseline}
            if len(lookup) != len(pred) or any(not _finite(lookup.get(p["item_id"])) for p in pred):
                continue
            deltas = [p["value"] - lookup[p["item_id"]] for p in pred]
            pairs.append({"backend_run_id": rid, "baseline_run_id": evidence[0][0], "count": len(deltas),
                          "mean_delta": sum(deltas) / len(deltas), "item_deltas": dict(zip([p["item_id"] for p in pred], deltas))})
        result["paired"] = pairs
        result["compatible"] = not diagnostics
    if diagnostics:
        return result
    selected = (max if direction == "maximize" else min)(rows, key=lambda r: r["value"])
    for row in rows:
        row["delta_from_selected"] = row["value"] - selected["value"]
    result.update(selected_run_id=selected["backend_run_id"], selected_model_version_ref=selected["model_version_ref"])
    result["comparison_digest"] = digest({k: v for k, v in result.items() if k != "comparison_digest"})
    return result


def fixture_evaluation_records(data: dict, checkpoint, parameters: dict, metrics: dict) -> dict:
    """Record new fixture executions accurately; never migrate old runs by guesswork."""
    from pathlib import Path
    import torch
    from .backend import fixtures
    selected = parameters.get("checkpoint_kind", "best") == "best"
    ck = fixtures.load_checkpoint(checkpoint, selected=selected)
    checkpoint_hash = hashlib.sha256((Path(checkpoint) / ("best.pt" if selected else "checkpoint.pt")).read_bytes()).hexdigest()
    dataset = {"id": f"fixture:{data['fixture']}", "revision": data["sha256"],
               "content_digest": fixtures.verify_data(data), "benchmark": f"fixture:{data['fixture']}:v1"}
    protocol = {"implementation": "modelbuilder.backend.fixtures.evaluate", "version": "1",
                "configuration": {"task": "binary_classification"},
                "preprocessing": {"fixture": data["fixture"], "model_revision": data.get("model_revision")}, "seed": None}
    treatment = {"model": {"fixture": ck["fixture"], "width": ck["width"], "training_contract": ck["training_contract"],
                           "training_reference_digest": digest({k: v.tolist() for k, v in ck["reference"].items()}),
                           "training_seed": ck["seed"]},
                 "checkpoint": checkpoint_hash, "calibration": None, "reference": None}
    result = {}
    for name, split_name, implementation, direction in (
        ("validation_loss", "validation", "torch.nn.functional.cross_entropy", "minimize"),
        ("accuracy", "heldout", "torch.argmax.equality.mean", "maximize"),
        ("brier", "heldout", "torch.binary_brier.mean", "minimize"),
    ):
        result[name] = make_evaluation_record(dataset=dataset,
            split={"id": split_name, "role": "validation" if split_name == "validation" else "test",
                   "sample_ids": [str(i) for i in data[f"{split_name}_ids"]]},
            protocol=protocol,
            metric={"name": name, "implementation": implementation, "version": torch.__version__,
                    "parameters": {"reduction": "mean"}, "aggregation": "mean", "direction": direction},
            treatment=treatment, value=metrics[name], provenance={"checkpoint_kind": "best" if selected else "last"})
    # best_validation_loss is historical training selection, not the freshly
    # evaluated selected checkpoint. Do not relabel it as evaluation evidence.
    return result
