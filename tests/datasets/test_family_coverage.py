"""Final split invariants on real pinned files, both eager and disk-backed."""
from copy import deepcopy
import hashlib
import json

import pytest

from modelbuilder.dataset import DatasetRegistry, _version_record, consumption, load_portable_split
from modelbuilder.dataset_streaming import StreamingConsumption, iter_stream_rows, validate_stream_data
from modelbuilder.models import ServiceError


def policy():
    return {"schema_version": "modelbuilder.family-policy.v1", "mapping": {"version": "v1", "field": "family"},
            "families": [{"id": family, "min_rows": 1, "min_exposure": 1, "min_eval_support": 1} for family in ("a", "b")]}


def fixture(tmp_path, mode, transforms=(), train=None):
    rows = train if train is not None else [{"id": "a", "family": "a", "text": "one"}, {"id": "b", "family": "b", "text": "two"}]
    rows = rows + [{"id": "eval", "text": "evaluation without family"}]
    path = tmp_path / "data.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"name": "families", "benchmark": "test", "license": "CC0", "provenance": {"source": "test"},
                "sources": [{"id": "data", "root_id": "data", "path": path.name, "sha256": sha, "format": "jsonl"}],
                "id_column": "id", "row_schema": {"type": "object", "properties": {
                    "id": {"type": "string"}, "family": {"type": "string"}, "text": {"type": "string"}}, "required": ["id"]},
                "splits": [{"id": "train", "role": "train", "sample_ids": [row["id"] for row in rows[:-1]]},
                           {"id": "validation", "role": "validation", "sample_ids": ["eval"]}],
                "transformations": list(transforms), "family_policy": policy(), "access_mode": mode}
    registry = DatasetRegistry(None, project_roots={"project": {"data": tmp_path}})
    return registry, manifest, path


@pytest.mark.parametrize("mode", ["eager", "streaming"])
@pytest.mark.parametrize("transform,train", [
    ({"kind": "rows.drop_missing", "fields": ["text"]}, [{"id": "a", "family": "a", "text": "one"}, {"id": "b", "family": "b", "text": ""}]),
    ({"kind": "rows.deduplicate", "fields": ["text"]}, [{"id": "a", "family": "a", "text": "same"}, {"id": "b", "family": "b", "text": "same"}]),
    ({"kind": "columns.select", "fields": ["id", "text"]}, None),
])
def test_final_transform_cannot_destroy_required_family(tmp_path, mode, transform, train):
    sampling = {"kind": "rows.sample", "version": "1", "split_ids": ["train"], "fields": ["family"],
                "fraction": 1, "seed": "family-test", "allocation": "proportional", "min_per_stratum": 1,
                "required_strata": [{"family": "a"}, {"family": "b"}], "max_share_error": 0}
    registry, manifest, _ = fixture(tmp_path, mode, [sampling, transform], train)
    with pytest.raises(ServiceError) as error:
        registry._materialize("project", manifest)
    assert error.value.code == "dataset_family_coverage_invalid"


@pytest.mark.parametrize("mode", ["eager", "streaming"])
@pytest.mark.parametrize("family", [None, "unknown", 0, [], ""])
def test_missing_unknown_or_nonscalar_final_family_fails(tmp_path, mode, family):
    registry, manifest, _ = fixture(tmp_path, mode, train=[{"id": "a", "family": "a", "text": "one"}, {"id": "b", "family": family, "text": "two"}])
    # Permit any JSON family value to exercise the family contract, not row typing.
    manifest["row_schema"]["properties"].pop("family")
    with pytest.raises(ServiceError) as error:
        registry._materialize("project", manifest)
    assert error.value.code == "dataset_family_coverage_invalid"


@pytest.mark.parametrize("mode", ["eager", "streaming"])
def test_final_report_portable_consumption_and_eval_scope(tmp_path, mode):
    registry, manifest, path = fixture(tmp_path, mode)
    materialized = registry._materialize("project", manifest)
    record = _version_record("project", "dataset", 1, *materialized)
    report = record["splits"][0]["final_dataset_family_coverage"]
    assert report["status"] == "passed" and report["counts"] == {"a": 1, "b": 1}
    assert report["scope"] == "final_dataset_split"
    assert "final_dataset_family_coverage" not in record["splits"][1]
    reference = {key: record[key] for key in ("schema", "project_id", "dataset_id", "revision", "version_id", "content_digest", "manifest")}
    source = manifest["sources"][0]
    dependency = "dataset-" + source["sha256"]
    reference["dependencies"] = [{"id": dependency, "sha256": source["sha256"], "kind": "dataset", "license": manifest["license"], "source_id": "data", "required": True}]
    data = load_portable_split(reference, {dependency: path}, "train", purpose="training")
    assert data["family_policy"] == policy()
    assert data["final_dataset_family_coverage"] == report
    if mode == "streaming":
        assert record["lineage"]["source_row_count"] == 3
        validate_stream_data(data)
        receipt = StreamingConsumption(data)
        for row in iter_stream_rows(data):
            receipt.add(row)
        assert receipt.snapshot()["final_dataset_family_coverage"] == report
        receipt.close()
        tampered = deepcopy(data)
        tampered["final_dataset_family_coverage"]["counts"]["b"] = 20
        with pytest.raises(ServiceError):
            validate_stream_data(tampered)
        tampered = deepcopy(data)
        tampered.pop("family_policy")
        with pytest.raises(ServiceError):
            validate_stream_data(tampered)
    else:
        assert data["consumption"]["final_dataset_family_coverage"] == report
        assert consumption(record, "train", ["a"], purpose="training")["final_dataset_family_coverage"] == report
    evaluation = load_portable_split(reference, {dependency: path}, "validation", purpose="evaluation")
    assert "family_policy" not in evaluation
    assert "final_dataset_family_coverage" not in evaluation


@pytest.mark.parametrize("mode", ["eager", "streaming"])
def test_zero_quota_is_explicit_waiver_and_no_policy_unchanged(tmp_path, mode):
    registry, manifest, _ = fixture(tmp_path, mode, train=[{"id": "a", "family": "a", "text": "one"}])
    manifest["family_policy"]["families"][1]["min_rows"] = 0
    materialized = registry._materialize("project", manifest)
    assert materialized[2][0]["final_dataset_family_coverage"]["counts"]["b"] == 0
    manifest.pop("family_policy")
    assert "final_dataset_family_coverage" not in registry._materialize("project", manifest)[2][0]


@pytest.mark.parametrize("mode", ["eager", "streaming"])
def test_each_training_split_must_satisfy_policy(tmp_path, mode):
    registry, manifest, _ = fixture(tmp_path, mode)
    manifest["splits"][0]["sample_ids"] = ["a"]
    manifest["splits"].append({"id": "other_train", "role": "train", "sample_ids": ["b"]})
    with pytest.raises(ServiceError) as error:
        registry._materialize("project", manifest)
    assert error.value.code == "dataset_family_coverage_invalid"


@pytest.mark.parametrize("mode", ["eager", "streaming"])
def test_removed_unknown_family_does_not_pollute_final_counts(tmp_path, mode):
    registry, manifest, _ = fixture(tmp_path, mode, [{"kind": "rows.drop_missing", "fields": ["text"]}],
        train=[{"id": "a", "family": "a", "text": "one"}, {"id": "b", "family": "b", "text": "two"},
               {"id": "bad", "family": "unknown", "text": ""}])
    materialized = registry._materialize("project", manifest)
    assert materialized[2][0]["final_dataset_family_coverage"]["counts"] == {"a": 1, "b": 1}
