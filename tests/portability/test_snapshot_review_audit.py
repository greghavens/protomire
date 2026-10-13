"""Test-only replay manifest audit cannot disguise scientific drift as rights."""

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "snapshot_review_harness", Path(__file__).with_name("reproduce_snapshot_review.py"))
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def manifest_path(run, name):
    return Path(run["data_dir"]) / "backend/jobs" / run["run"]["operation_id"] / "outputs" / name


def replace_manifest(run, name, value, *, raw=False, reseal=True):
    path = manifest_path(run, name)
    data = value.encode() if raw else json.dumps(value, indent=2).encode()
    path.write_bytes(data)
    if reseal:
        run["output_sha256"][name] = digest(data)


@pytest.fixture
def runs(tmp_path):
    result = []
    for role in ("origin", "replay"):
        run = {"data_dir": str(tmp_path / role), "run": {"operation_id": role + "-operation"},
               "output_sha256": {"payload.bin": digest(b"same numerical state")}}
        for name in harness.REVIEW_MANIFESTS:
            path = manifest_path(run, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            replace_manifest(run, name, {"step": 1, "model": {"sha256": "a" * 64},
                "measurements": [0.25, 0.5], "rights": {"policy_id": role, "parents": [role + "-parent"]}})
        result.append(run)
    return result


def test_rights_only_audit_preserves_originals_and_strict_failure(runs):
    records = deepcopy(runs)
    before = {str(path): path.read_bytes() for run in runs for name in harness.REVIEW_MANIFESTS
              for path in [manifest_path(run, name)]}
    assert runs[0]["output_sha256"] != runs[1]["output_sha256"]  # Strict byte comparison still fails.
    audit = harness.audit_review_provenance(*runs)
    assert audit["status"] == "PASS" and all(audit["checks"].values())
    assert audit["allowed_difference_field"] == "/rights"
    for row in audit["manifests"].values():
        assert row["different_json_paths"] == ["/rights/parents/0", "/rights/policy_id"]
        for original in row["originals"].values():
            assert original["json_text"].encode() == before[original["path"]]
            assert digest(before[original["path"]]) == original["sha256"]
            assert original["rights"] == original["manifest"]["rights"]
    assert runs == records
    assert all(Path(path).read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize("change,pointer", [
    ("step", "/step"), ("model", "/model/sha256"), ("measurement", "/measurements/1"),
    ("boolean", "/step"), ("rights-prefix", "/rights_extra"), ("nested-rights", "/model/rights"),
])
def test_non_rights_drift_in_expected_manifest_names_is_rejected(runs, change, pointer):
    name = "train/checkpoint.manifest.json"
    value = json.loads(manifest_path(runs[1], name).read_text())
    if change == "step":
        value["step"] = 2
    elif change == "model":
        value["model"]["sha256"] = "b" * 64
    elif change == "measurement":
        value["measurements"][1] = 0.6
    elif change == "boolean":
        value["step"] = True
    elif change == "rights-prefix":
        value["rights_extra"] = "not the allowed field"
    else:
        value["model"]["rights"] = {"unrelated": True}
    replace_manifest(runs[1], name, value)
    audit = harness.audit_review_provenance(*runs)
    assert audit["status"] == "FAIL"
    assert audit["checks"]["only_expected_manifests_differ"]
    assert not audit["checks"]["differences_confined_to_rights"]
    assert pointer in audit["manifests"][name]["different_json_paths"]


@pytest.mark.parametrize("raw", [
    '{"step":1,"step":2,"rights":{}}', '{"step":NaN,"rights":{}}',
    '{"step":1e999,"rights":{}}', '{"step":1}', '{"step":1,"rights":null}', 'not JSON',
])
def test_malformed_or_missing_explicit_rights_cannot_pass(runs, raw):
    name = "train/checkpoint.manifest.json"
    replace_manifest(runs[1], name, raw, raw=True)
    audit = harness.audit_review_provenance(*runs)
    assert audit["status"] == "FAIL" and audit["errors"]
    assert audit["manifests"][name]["originals"]["replay"]["json_text"] == raw


def test_changed_bytes_after_receipt_are_rejected(runs):
    name = "train/checkpoint.manifest.json"
    value = json.loads(manifest_path(runs[1], name).read_text())
    value["rights"]["policy_id"] = "changed after hashing"
    replace_manifest(runs[1], name, value, reseal=False)
    audit = harness.audit_review_provenance(*runs)
    assert audit["status"] == "FAIL" and not audit["checks"]["original_manifests_verified"]


def test_sub_float_precision_non_rights_drift_is_rejected(runs):
    name = "train/checkpoint.manifest.json"
    for run, number in zip(runs, ("9007199254740992.0", "9007199254740993.0")):
        replace_manifest(run, name, '{"step":' + number + ',"rights":{"policy_id":"' +
                         run["run"]["operation_id"] + '"}}', raw=True)
    audit = harness.audit_review_provenance(*runs)
    assert audit["status"] == "FAIL"
    assert "/step" in audit["manifests"][name]["different_json_paths"]
    json.dumps(audit)  # Exact comparison must still produce a JSON-serializable receipt.


def test_additional_output_path_is_not_hidden_by_expected_manifest_differences(runs):
    runs[1]["output_sha256"]["unexpected.bin"] = digest(b"extra output")
    audit = harness.audit_review_provenance(*runs)
    assert audit["status"] == "FAIL" and not audit["checks"]["same_output_paths"]
    assert "unexpected.bin" in audit["different_output_paths"]
