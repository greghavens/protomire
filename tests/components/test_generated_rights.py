"""Generated-data seals are inert evidence, re-admitted from current authority."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from modelbuilder.artifact_rights import combine_parent_rights
from modelbuilder.backend.admission import BackendError
from modelbuilder.backend.execution_rights import bind_authority, prepare_authorization, verify_dispatch, _input_envelopes
from modelbuilder.generated_artifacts import (GENERATED_SCHEMAS, attach_lineage, dict_rights,
                                              is_protected_dict, validate_lineage)
from modelbuilder.hub_rights import digest
from modelbuilder.store import SQLiteStore
from rights_test_support import local_review, USAGE_CONTEXT


def output(schema, rights):
    return attach_lineage({"schema": schema, "rows": [{"id": "authored", "text": "synthetic"}]},
        {"prompts": {"rights": rights, "rows": [{"text": "authored"}]}},
        {"source": {"commit": "1" * 40}, "declared_artifact_rights": rights})


@pytest.mark.parametrize("schema", sorted(GENERATED_SCHEMAS))
def test_canonical_seal_exact_parents_and_no_generic_dictionary_upgrade(schema):
    _, rights = local_review()
    value = output(schema, rights)
    assert value == output(schema, rights)
    assert validate_lineage(value) == value["rights"]
    assert value["digest"] == digest({key: item for key, item in value.items() if key != "digest"})
    assert value["rights"] == combine_parent_rights(value["lineage"]["parents"])
    assert is_protected_dict(value)
    assert not is_protected_dict({"rows": [{"text": "ordinary configuration"}]})
    with pytest.raises(ValueError, match="known generated-data"):
        attach_lineage({"rows": []}, {}, {"declared_artifact_rights": rights})
    assert _input_envelopes({"settings": {"rows": []}}, {"inputs": [{"name": "settings", "type": "dict"}]}) == {}


@pytest.mark.parametrize("change", ["content", "rights", "parent", "missing", "historic", "resealed_outer"])
def test_tampered_or_unreviewed_historic_typed_outputs_fail_closed(change):
    _, rights = local_review()
    value = output("modelbuilder.generated-candidates/v1", rights)
    if change in {"content", "resealed_outer"}:
        value["rows"][0]["text"] = "changed"
    elif change == "rights":
        value["rights"]["sources"] = []
    elif change == "parent":
        value["lineage"]["parents"][0]["rights"] = None
    elif change == "missing":
        del value["rights"]
    else:
        value = {key: item for key, item in value.items() if key not in {"rights", "lineage", "digest"}}
    if change in {"historic", "resealed_outer"}:
        value["digest"] = digest({key: item for key, item in value.items() if key != "digest"})
    with pytest.raises(Exception):
        dict_rights(value)
    with pytest.raises(BackendError, match="parent rights lineage"):
        _input_envelopes({"candidates": value}, {"inputs": [{"name": "candidates", "type": "dict"}]})


@pytest.mark.parametrize("change", ["missing", "revoked", "foreign_project"])
def test_pruned_native_parent_policies_rechecked_at_admission_and_dispatch(tmp_path, change):
    policy, rights = local_review()
    value = output("modelbuilder.generated-candidates/v2", rights)
    store = SQLiteStore(tmp_path / "rights.sqlite")
    try:
        store.put("hub_license_policy", policy["policy_id"], policy["project_id"], policy)
        backend = SimpleNamespace(_execution_rights_authority=bind_authority(policy["project_id"], {policy["policy_id"]: policy}, store.path))
        node = {"id": "prepare", "kind": "component.run", "component": {"role": "dataset"},
                "provenance": {"intended_use": "generate"}, "inputs": [{"name": "candidates", "type": "dict"}]}
        # No original source importer or resources remain in this selected graph.
        recipe = {"source": {}, "nodes": [node], "resources": [], "extensions": {"usage_context": USAGE_CONTEXT}}
        payload = {"project_id": policy["project_id"], "resolved_input_bindings": [
            {"node_id": "prepare", "rights_required": True, "rights": value["rights"]}]}
        authorization = prepare_authorization(backend, payload, recipe, durable=True)
        spec = {"project_id": policy["project_id"], "source": {}, "component_node": node,
                "rights_extensions": recipe["extensions"], "rights_authorization": authorization}
        assert verify_dispatch(spec, {"candidates": value})["policy_revisions"]
        if change == "missing":
            store.connection.execute("DELETE FROM resources WHERE kind='hub_license_policy'")
        else:
            changed = deepcopy(policy)
            changed.update(revision=2, **({"status": "revoked"} if change == "revoked" else {"project_id": "foreign"}))
            changed["review_digest"] = digest({key: item for key, item in changed.items() if key != "review_digest"})
            store.put("hub_license_policy", policy["policy_id"], changed["project_id"], changed, expected_revision=1)
        with pytest.raises(BackendError, match="policy|Policy"):
            prepare_authorization(backend, payload, recipe, durable=True)
        with pytest.raises(BackendError, match="policy|Policy"):
            verify_dispatch(spec, {"candidates": value})
    finally:
        store.close()


def test_inherited_prepared_rights_survive_an_output_policy_and_license_label():
    from modelbuilder.hub import HubTransfers
    _, inherited = local_review(policy_id="inherited")
    _, declared = local_review(policy_id="output", commit="2" * 40)
    manifest = attach_lineage({"provenance": {"source": "modelbuilder.local.prepare-dataset/v1"},
                              "license": "CC0-1.0", "sources": []}, {}, {"declared_artifact_rights": inherited})
    reference = {"manifest": manifest, "version_id": digest(manifest), "content_digest": "a" * 64}
    result = HubTransfers._inherited_dataset_rights(reference, declared)
    assert {row["license_policy_id"] for row in result["sources"]} == {"inherited", "output"}
    assert digest(manifest["rights"]) in result["parent_rights_digests"]


@pytest.mark.parametrize("case", ["valid", "historic", "tampered", "generic"])
def test_native_dict_inspection_validates_known_contract_without_producer_imports(monkeypatch, tmp_path, case):
    from modelbuilder.backend import native_inputs
    _, rights = local_review()
    body = output("modelbuilder.model-judgments/v2", rights)
    if case == "historic":
        body.pop("rights")
        body.pop("lineage")
        body["digest"] = digest({key: value for key, value in body.items() if key != "digest"})
    elif case == "tampered":
        body["lineage"]["parents"][0]["rights"] = None
    elif case == "generic":
        body = {"parameters": {"seed": 123, "learning_rate": 0.125, "negative": -1.5e10}}
    directory = tmp_path / "zenml-stores" / "artifact"
    directory.mkdir(parents=True)
    (directory / "data.json").write_text(json.dumps(body))
    artifact = SimpleNamespace(uri=str(directory), data_type=SimpleNamespace(import_path="builtins.dict"),
        materializer=SimpleNamespace(import_path="zenml.materializers.built_in_materializer.BuiltInContainerMaterializer"),
        load=lambda: (_ for _ in ()).throw(AssertionError("No producer/materializer code may run")))
    monkeypatch.setattr(native_inputs, "storage_identity", lambda *_: "a" * 64)
    if case in {"historic", "tampered"}:
        with pytest.raises(BackendError, match="parent rights lineage"):
            native_inputs.artifact_rights_descriptor(artifact, tmp_path, "dict")
    else:
        result = native_inputs.artifact_rights_descriptor(artifact, tmp_path, "dict")
        assert result == ({"rights_required": True, "rights": body["rights"]} if case == "valid" else {})


def test_missing_parent_is_unresolved_even_if_other_parent_is_reviewed():
    _, rights = local_review()
    value = attach_lineage({"schema": "modelbuilder.generated-candidates/v2", "rows": []},
                           {"prompts": {"rows": []}}, {"declared_artifact_rights": rights})
    assert validate_lineage(value)["unresolved"]
    from modelbuilder.hub_rights import validate_artifact_rights
    from modelbuilder.models import ServiceError
    policy, _ = local_review()
    with pytest.raises(ServiceError, match="unresolved"):
        validate_artifact_rights(value["rights"], "generate", {policy["policy_id"]: policy}, USAGE_CONTEXT)


@pytest.mark.parametrize("case", ["missing", "oversized", "malformed", "nonfinite", "overflow", "negative_overflow", "duplicate", "non_object", "nested_materializer"])
def test_native_dict_preflight_rejects_unsafe_json_without_dispatch(tmp_path, monkeypatch, case):
    from modelbuilder.backend import native_inputs
    directory = tmp_path / "zenml-stores" / "artifact"
    directory.mkdir(parents=True)
    data = {"oversized": '{"large":"' + 'x' * 256 + '"}', "malformed": '{"bad":',
            "nonfinite": '{"value":NaN}', "overflow": '{"value":1e999}',
            "negative_overflow": '{"value":-1e999}', "duplicate": '{"schema":"a","schema":"b"}',
            "non_object": '[]'}.get(case)
    if data is not None:
        (directory / "data.json").write_text(data)
    elif case == "missing":
        (directory / "unrelated.json").write_text("{}")
    if case == "nested_materializer":
        (directory / "metadata.json").write_text(json.dumps([{"type": "malicious.Type",
            "materializer": "malicious.Loader", "path": "/must-not-read"}]))
    artifact = SimpleNamespace(uri=str(directory), data_type=SimpleNamespace(import_path="builtins.dict"),
        materializer=SimpleNamespace(import_path="zenml.materializers.built_in_materializer.BuiltInContainerMaterializer"),
        load=lambda: (_ for _ in ()).throw(AssertionError("Nested materializer ran")))
    monkeypatch.setattr(native_inputs, "MAX_NATIVE_JSON_BYTES", 128)
    with pytest.raises(BackendError, match="bounded inert data.json"):
        native_inputs.artifact_rights_descriptor(artifact, tmp_path, "dict")


def test_native_inert_json_preserves_ordinary_finite_numbers(tmp_path):
    from modelbuilder.backend.native_inputs import _native_dict
    (tmp_path / "data.json").write_text('{"positive":0.125,"negative":-1.5e10,"integer":17}')
    assert _native_dict(SimpleNamespace(uri=str(tmp_path))) == {"positive": 0.125, "negative": -1.5e10, "integer": 17}
