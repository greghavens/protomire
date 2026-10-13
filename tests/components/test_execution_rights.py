"""Rights must fail closed at admission and before any source import/native use."""
from copy import deepcopy
import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from modelbuilder.backend.admission import BackendError
from modelbuilder.backend import execution_rights as rights
from modelbuilder.backend.components.runtime import dispatch_python
from modelbuilder.hub_rights import digest
from modelbuilder.store import SQLiteStore
from rights_test_support import local_review, local_source_review, USAGE_CONTEXT


def candidate(role="training"):
    return {"id": "train", "kind": "component.run", "inputs": [], "outputs": [], "provenance": {},
            "component": {"role": role}, "cache_policy": "disabled"}


@pytest.fixture
def fixture(tmp_path):
    store = SQLiteStore(tmp_path.resolve() / "service.sqlite3")
    policy, envelope = local_review()
    store.put("hub_license_policy", policy["policy_id"], policy["project_id"], policy)
    backend = SimpleNamespace(_execution_rights_authority=rights.bind_authority(policy["project_id"], {policy["policy_id"]: policy}, store.path))
    recipe = {"source": {"commit": "1" * 40}, "nodes": [candidate()],
              "resources": [{"id": "weights", "kind": "model", "sha256": "a" * 64, "metadata": {"rights": envelope}}],
              "extensions": {"usage_context": deepcopy(USAGE_CONTEXT)}}
    payload = {"project_id": policy["project_id"]}
    yield backend, recipe, payload, store, policy, envelope
    store.close()


def spec(fixture):
    backend, recipe, payload, _, _, _ = fixture
    authorization = rights.prepare_authorization(backend, payload, recipe, durable=True)
    return {"source": deepcopy(recipe["source"]), "component_node": deepcopy(recipe["nodes"][0]),
            "dependencies": deepcopy(recipe["resources"]), "rights_extensions": deepcopy(recipe["extensions"]),
            "rights_authorization": authorization}


def test_approved_receipt_rechecks_current_exact_asset_usage_and_is_immutable(fixture):
    backend, recipe, payload, _, policy, _ = fixture
    native = spec(fixture)
    policy["status"] = "revoked"  # Caller mutation cannot change captured internal authority.
    result = rights.verify_dispatch(native)
    assert result["requested_actions"] == ["train"]
    assert result["policy_revisions"][policy["policy_id"]]["revision"] == 1
    assert result["source_digest"] == digest(recipe["source"])
    assert result["usage_context"] == USAGE_CONTEXT
    assert "policies" not in result and "policy_store_guard" not in result
    assert rights.prepare_authorization(backend, payload, recipe)["receipt"] == native["rights_authorization"]["receipt"]


def test_no_declared_inputs_are_distinct_from_cleared_legal_rights():
    recipe = {"source": {}, "nodes": [candidate("utility")], "resources": []}
    receipt = rights.prepare_authorization(SimpleNamespace(), {"project_id": "p"}, recipe)
    assert receipt["receipt"]["scope"] == "no_declared_external_asset_inputs"
    assert receipt["receipt"]["nodes"]["train"]["admission"] == []
    assert rights.verify_dispatch({"source": {}, "component_node": recipe["nodes"][0]})["scope"] == "no_declared_external_asset_inputs"


@pytest.mark.parametrize("key", sorted(rights.RESERVED_FIELDS))
def test_payload_cannot_supply_policy_authority(fixture, key):
    backend, recipe, payload, *_ = fixture
    with pytest.raises(BackendError, match="internal service authority"):
        rights.prepare_authorization(backend, {**payload, key: {}}, recipe)


@pytest.mark.parametrize("mutation", [
    lambda r: r["resources"][0]["metadata"].clear(),
    lambda r: r["resources"][0]["metadata"]["rights"].update(sources=[]),
    lambda r: r["resources"][0]["metadata"]["rights"]["sources"][0].update(commit="2" * 40),
    lambda r: r["resources"][0]["metadata"]["rights"]["sources"][0].update(license_evidence_digest="f" * 64),
    lambda r: r["extensions"]["usage_context"].update(commercial=True),
    lambda r: r["extensions"].pop("usage_context"),
])
def test_missing_or_mismatched_provenance_never_uses_labels_as_permission(fixture, mutation):
    backend, recipe, payload, *_ = fixture
    mutation(recipe)
    recipe["resources"][0]["license"] = "apache-2.0"  # An unreviewed label grants nothing.
    with pytest.raises(BackendError) as caught:
        rights.prepare_authorization(backend, payload, recipe)
    assert caught.value.code == "license_rights_unreviewed"


def test_generic_usage_is_explicit_and_known_role_conflicts_block(fixture):
    backend, recipe, payload, *_ = fixture
    recipe["nodes"][0]["component"]["role"] = "utility"
    with pytest.raises(BackendError, match="explicit provenance.intended_use"):
        rights.prepare_authorization(backend, payload, recipe)
    recipe["nodes"][0]["provenance"]["intended_use"] = "generate"
    assert spec(fixture)["rights_authorization"]["receipt"]["nodes"]["train"]["actions"] == ["generate"]
    recipe["nodes"][0]["component"]["role"] = "training"
    with pytest.raises(BackendError) as caught:
        rights.prepare_authorization(backend, payload, recipe)
    assert caught.value.code == "license_usage_conflict"


def test_dataset_builtin_inherits_all_selected_operations(fixture):
    _, recipe, *_ = fixture
    recipe["nodes"] += [{"id": "data", "kind": "dataset.import", "provenance": {}},
                        {**candidate("evaluation"), "id": "eval"}]
    receipt = spec(fixture)["rights_authorization"]["receipt"]
    assert receipt["nodes"]["data"]["actions"] == ["evaluate", "train"]


@pytest.mark.parametrize("mutation", [
    lambda s: s["source"].update(commit="2" * 40),
    lambda s: s["component_node"]["provenance"].update(intended_use="export"),
    lambda s: s["dependencies"][0].update(sha256="b" * 64),
    lambda s: s["rights_extensions"]["usage_context"].update(commercial=True),
    lambda s: s["rights_authorization"]["policies"]["test-local-origin"].update(status="revoked"),
    lambda s: s["rights_authorization"]["receipt"].update(scope="cleared"),
])
def test_receipt_or_source_tampering_blocks_before_import(fixture, monkeypatch, mutation):
    native = spec(fixture)
    mutation(native)
    monkeypatch.setattr("modelbuilder.backend.components.runtime.load_callable", lambda *a: pytest.fail("source imported"))
    with pytest.raises(BackendError):
        dispatch_python({}, native)


@pytest.mark.parametrize("change", ["revoke", "remove", "revision", "body", "project"])
def test_policy_change_after_preflight_is_rechecked_before_import(fixture, monkeypatch, change):
    _, _, _, store, policy, _ = fixture
    native = spec(fixture)
    modified = deepcopy(policy)
    if change == "revoke":
        modified.update(status="revoked", revision=2)
    elif change == "revision":
        modified["revision"] = 2
    elif change == "body":
        modified["reviewed_by"] = "someone-else"
    if change == "remove":
        store.connection.execute("DELETE FROM resources WHERE kind='hub_license_policy'")
    elif change == "project":
        store.connection.execute("UPDATE resources SET project_id='other' WHERE kind='hub_license_policy'")
    else:
        store.connection.execute("UPDATE resources SET revision=?,body=? WHERE kind='hub_license_policy'", (modified["revision"], json.dumps(modified)))
    monkeypatch.setattr("modelbuilder.backend.components.runtime.load_callable", lambda *a: pytest.fail("source imported"))
    with pytest.raises(BackendError) as caught:
        dispatch_python({}, native)
    assert caught.value.code == "license_policy_changed"
    with pytest.raises(BackendError):
        rights.recheck_authorization(native["rights_authorization"])


def test_in_memory_review_supports_pure_plan_but_cannot_dispatch(fixture):
    backend, recipe, payload, _, policy, _ = fixture
    backend._execution_rights_authority = rights.bind_authority(policy["project_id"], {policy["policy_id"]: policy}, ":memory:")
    rights.prepare_authorization(backend, payload, recipe)
    with pytest.raises(BackendError, match="durable service policy store"):
        rights.prepare_authorization(backend, payload, recipe, durable=True)


@pytest.mark.skipif(sys.platform == "win32", reason="Open SQLite inode replacement is a POSIX acceptance case")
def test_database_file_replacement_is_not_new_authority(fixture, tmp_path):
    _, _, _, store, _, _ = fixture
    native = spec(fixture)
    replacement = tmp_path / "replacement.sqlite3"
    backup = sqlite3.connect(replacement)
    store.connection.backup(backup)
    backup.close()
    replacement.replace(store.path)
    with pytest.raises(BackendError, match="store identity changed"):
        rights.verify_dispatch(native)


def test_connected_or_resumed_checkpoint_missing_rights_blocks_before_source(fixture, tmp_path, monkeypatch):
    native = spec(fixture)
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    # A typed checkpoint without its immutable manifest is unresolved, not source-only.
    native["component_node"]["inputs"] = [{"name": "checkpoint", "type": "CheckpointArtifact"}]
    native["resume_path"] = str(checkpoint)
    monkeypatch.setattr("modelbuilder.backend.components.runtime.load_callable", lambda *a: pytest.fail("source imported"))
    with pytest.raises(BackendError):
        dispatch_python({"checkpoint": checkpoint}, native)


def test_connected_asset_cannot_bypass_generic_intended_use(fixture):
    backend, recipe, payload, _, _, envelope = fixture
    recipe["resources"] = []
    recipe["nodes"][0]["component"]["role"] = "utility"
    native = spec(fixture)
    with pytest.raises(BackendError, match="explicit provenance.intended_use"):
        rights.verify_dispatch(native, {"data": {"rights": envelope}})
    recipe["nodes"][0]["provenance"]["intended_use"] = "evaluate"
    result = rights.verify_dispatch(spec(fixture), {"data": {"rights": envelope}})
    assert result["input_rights_digests"]["data"] == digest(envelope)
    assert rights.prepare_authorization(backend, payload, recipe)["receipt"]["scope"] == "no_declared_external_asset_inputs"


def test_artifact_sdk_helper_rechecks_review_and_usage_context(fixture):
    backend, _, payload, store, policy, envelope = fixture
    receipt = rights.artifact_authorization(backend, payload["project_id"], USAGE_CONTEXT)
    decision = rights.verify_artifact_authorization(receipt, envelope, "promote")
    assert decision["admission"]["intended_use"] == "promote"
    assert decision["usage_context"] == USAGE_CONTEXT
    missing = rights.artifact_authorization(backend, payload["project_id"], None)
    with pytest.raises(BackendError, match="structured usage context"):
        rights.verify_artifact_authorization(missing, envelope, "export")
    store.connection.execute("DELETE FROM resources WHERE id=?", (policy["policy_id"],))
    with pytest.raises(BackendError) as caught:
        rights.verify_artifact_authorization(receipt, envelope, "export")
    assert caught.value.code == "license_policy_changed"


def test_governed_cache_requires_fresh_dispatch(fixture):
    backend, recipe, payload, *_ = fixture
    recipe["nodes"][0]["cache_policy"] = "enabled"
    with pytest.raises(BackendError) as caught:
        rights.prepare_authorization(backend, payload, recipe)
    assert caught.value.code == "license_cache_unavailable"


def test_owned_native_binding_rights_are_preflighted_and_bound_to_receipt(fixture):
    backend, recipe, payload, _, _, envelope = fixture
    recipe["resources"] = []
    payload["resolved_input_bindings"] = [{"node_id": "train", "input_port": "weights", "artifact_id": "a",
                                            "rights_required": True, "rights": envelope}]
    receipt = rights.prepare_authorization(backend, payload, recipe, durable=True)["receipt"]
    assert receipt["scope"] == "declared_asset_inputs"
    assert receipt["native_input_rights"]["train"] == [digest(envelope)]
    assert receipt["nodes"]["train"]["admission"][0]["policy_ids"] == ["test-local-origin"]
    payload["resolved_input_bindings"][0]["rights"] = None
    with pytest.raises(BackendError, match="complete declared license lineage"):
        rights.prepare_authorization(backend, payload, recipe)


def test_store_guard_and_project_are_bound_to_native_receipt(fixture):
    native = spec(fixture)
    native["rights_authorization"]["policy_store_guard"]["inode"] += 1
    with pytest.raises(BackendError, match="altered"):
        rights.verify_dispatch(native)
    native = spec(fixture)
    native["project_id"] = "another-project"
    with pytest.raises(BackendError, match="another project"):
        rights.verify_dispatch(native)


def test_nonpermitted_operation_blocks_even_with_same_reviewed_context(fixture):
    backend, recipe, payload, store, policy, _ = fixture
    policy["permitted_uses"] = ["evaluate"]
    policy["review_digest"] = digest({key: value for key, value in policy.items() if key != "review_digest"})
    store.connection.execute("UPDATE resources SET body=? WHERE kind='hub_license_policy'", (json.dumps(policy),))
    backend._execution_rights_authority = rights.bind_authority(policy["project_id"], {policy["policy_id"]: policy}, store.path)
    with pytest.raises(BackendError, match="does not permit"):
        rights.prepare_authorization(backend, payload, recipe)


def test_already_revoked_policy_is_not_admission(fixture):
    backend, recipe, payload, store, policy, _ = fixture
    policy.update(status="revoked", revision=2)
    policy["review_digest"] = digest({key: value for key, value in policy.items() if key != "review_digest"})
    store.connection.execute("UPDATE resources SET revision=2,body=? WHERE kind='hub_license_policy'", (json.dumps(policy),))
    backend._execution_rights_authority = rights.bind_authority(policy["project_id"], {policy["policy_id"]: policy}, store.path)
    with pytest.raises(BackendError, match="current authoritative reviewed"):
        rights.prepare_authorization(backend, payload, recipe)


@pytest.mark.skipif(sys.platform != "linux", reason="Runner v1 lifecycle accepts Linux CPU only")
def test_real_isolated_component_rechecks_revocation_before_source_import(fixture, tmp_path, monkeypatch):
    from test_runner_contract import setup, spec_for
    from modelbuilder.backend.process_execution import execute
    _, _, payload, store, _, _ = fixture
    # Parent telemetry is a unit sink; the source executes in an actual fresh child.
    records = []
    monkeypatch.setitem(sys.modules, "zenml", SimpleNamespace(log_metadata=lambda value, **_: records.append(value)))
    marker = tmp_path / "imported-source"
    code = (f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
            "def run(*, inputs, parameters, context):\n"
            "    assert context['rights_admission']['requested_actions'] == ['evaluate']\n"
            "    return {'result': {'ran': True}}\n")
    route, recipe, _ = setup(tmp_path, code=code)
    import hashlib
    asset = tmp_path / "weights.bin"
    asset.write_bytes(b"Explicit test-authored model artifact")
    sha = hashlib.sha256(asset.read_bytes()).hexdigest()
    policy, envelope = local_review(payload["project_id"], policy_id="test-revocation-model", source_files=[
        {"path": asset.name, "sha256": sha, "size_bytes": asset.stat().st_size}])
    store.put("hub_license_policy", policy["policy_id"], policy["project_id"], policy)
    route._execution_rights_authority = rights.bind_authority(policy["project_id"], {policy["policy_id"]: policy}, store.path)
    recipe["nodes"][0]["provenance"] = {"intended_use": "evaluate"}
    recipe["resources"].append({"id": "weights", "kind": "model", "sha256": sha, "metadata": {"rights": envelope}})
    route.dependency_paths["weights"] = str(asset)
    recipe["extensions"] = {"usage_context": USAGE_CONTEXT}
    native = spec_for(tmp_path, route, recipe)
    native.update(project_id=payload["project_id"], rights_extensions=recipe["extensions"],
                  rights_authorization=rights.prepare_authorization(route, payload, recipe, durable=True))
    assert execute("component", {}, native) == {"ran": True}
    assert records[-1]["rights_admission"]["requested_actions"] == ["evaluate"]
    assert marker.read_text() == "imported"
    marker.unlink()
    store.connection.execute("UPDATE resources SET revision=revision+1 WHERE kind='hub_license_policy'")
    with pytest.raises(RuntimeError, match="policy revision or status changed"):
        execute("component", {}, native)
    assert not marker.exists()


@pytest.mark.parametrize("purpose,action", [("training", "train"), ("calibration", "train"), ("evaluation", "evaluate")])
def test_explicit_dataset_purpose_maps_requested_action_without_inferring_permission(fixture, purpose, action):
    backend, recipe, payload, *_ = fixture
    recipe["nodes"] = [{"id": "data", "kind": "dataset.version.import", "parameters": {"purpose": purpose}}]
    receipt = rights.prepare_authorization(backend, payload, recipe)["receipt"]
    assert receipt["nodes"]["data"]["actions"] == [action]
    assert receipt["nodes"]["data"]["admission"][0]["intended_use"] == action
    recipe["nodes"][0]["provenance"] = {"intended_use": action}
    rights.prepare_authorization(backend, payload, recipe)
    recipe["nodes"][0]["provenance"]["intended_use"] = "export"
    with pytest.raises(BackendError) as caught:
        rights.prepare_authorization(backend, payload, recipe)
    assert caught.value.code == "license_usage_conflict"


@pytest.mark.parametrize("purpose", [None, "", "unknown", [], False])
def test_dataset_action_requires_explicit_known_structured_purpose(purpose):
    with pytest.raises(BackendError) as caught:
        rights.dataset_action(purpose)
    assert caught.value.code == "license_usage_unspecified"


def test_dataset_action_does_not_supply_unresolved_context_or_policy(fixture):
    backend, recipe, payload, *_ = fixture
    recipe["nodes"] = [{"id": "data", "kind": "dataset.version.import", "parameters": {"purpose": "training"}}]
    recipe["extensions"] = {}
    with pytest.raises(BackendError, match="structured usage context"):
        rights.prepare_authorization(backend, payload, recipe)
    recipe["extensions"] = {"usage_context": USAGE_CONTEXT}
    recipe["resources"][0]["metadata"]["rights"]["unresolved"] = [{"reason": "Review still required"}]
    with pytest.raises(BackendError, match="unresolved"):
        rights.prepare_authorization(backend, payload, recipe)


@pytest.mark.skipif(sys.platform != "linux", reason="Runner v1 lifecycle accepts Linux CPU only")
def test_reviewed_source_origin_is_exact_before_import_and_allows_new_parameters(fixture, tmp_path, monkeypatch):
    from test_runner_contract import setup, spec_for
    from modelbuilder.backend.process_execution import execute
    _, _, payload, store, model_policy, model_envelope = fixture
    records = []
    monkeypatch.setitem(sys.modules, "zenml", SimpleNamespace(log_metadata=lambda value, **_: records.append(value)))
    marker = tmp_path / "imported-source-origin"
    code = (f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
            "def run(*, inputs, parameters, context):\n"
            "    return {'result': {'offset': parameters['offset']}}\n")
    backend, recipe, _ = setup(tmp_path, code=code)
    source = backend.repository
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "Reviewed exact source"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    recipe["source"]["commit"] = recipe["nodes"][0]["implementation_ref"]["commit"] = commit
    node = recipe["nodes"][0]
    node["provenance"] = {"intended_use": "train"}
    node["parameters"] = {"offset": 0}
    node["component"]["parameter_schema"]["properties"] = {"offset": {"type": "integer"}}
    policy, envelope = local_source_review(source, recipe["source"], payload["project_id"], policy_id="test-source-origin")
    store.put("hub_license_policy", policy["policy_id"], policy["project_id"], policy)
    backend._execution_rights_authority = rights.bind_authority(policy["project_id"], {policy["policy_id"]: policy}, store.path)
    recipe["extensions"] = {"usage_context": USAGE_CONTEXT, "artifact_rights": envelope}

    def prepared(candidate):
        native = spec_for(tmp_path, backend, candidate)
        native.update(project_id=payload["project_id"], rights_extensions=candidate["extensions"],
                      rights_authorization=rights.prepare_authorization(backend, payload, candidate, durable=True))
        return native

    native = prepared(recipe)
    assert execute("component", {}, native) == {"offset": 0}
    assert marker.read_text() == "imported"
    marker.unlink()
    changed_parameters = deepcopy(recipe)
    changed_parameters["nodes"][0]["parameters"]["offset"] = 7
    changed = prepared(changed_parameters)
    assert changed["rights_authorization"]["receipt"]["digest"] != native["rights_authorization"]["receipt"]["digest"]
    assert execute("component", {}, changed) == {"offset": 7}
    assert records[-1]["rights_admission"]["policy_revisions"][policy["policy_id"]]["revision"] == 1
    marker.unlink()
    for change in (lambda source: source.update(commit="2" * 40),
                   lambda source: source["files"][0].update(sha256="f" * 64)):
        wrong_source = deepcopy(recipe)
        change(wrong_source["source"])
        with pytest.raises(BackendError) as failure:
            prepared(wrong_source)
        assert failure.value.code == "license_rights_unreviewed"
        assert not marker.exists()
    # A reviewed model is an actual input lineage, never a grant for unrelated code.
    borrowed = deepcopy(recipe)
    borrowed["extensions"]["artifact_rights"] = model_envelope
    backend._execution_rights_authority = rights.bind_authority(policy["project_id"],
        {policy["policy_id"]: policy, model_policy["policy_id"]: model_policy}, store.path)
    with pytest.raises(BackendError) as failure:
        prepared(borrowed)
    assert failure.value.code == "license_rights_unreviewed"
    assert not marker.exists()
    # Mutation after approval is caught by the actual private source snapshot boundary.
    (source / "adapter.py").write_text(code + "\n# altered after source-origin approval\n")
    with pytest.raises(RuntimeError, match="source closure changed after admission"):
        execute("component", {}, native)
    assert not marker.exists()


@pytest.mark.parametrize("feedback", [False, True])
@pytest.mark.parametrize("malformed", [False, True])
def test_known_generated_builtin_lineage_is_blocked_before_dispatch(feedback, malformed):
    recipe = {"nodes": [{"id": "data", "kind": "dataset.synthetic"},
                        {"id": "train", "kind": "model.train"}], "resources": [], "extensions": {}}
    edge = {"source_node": "data", "source_port": "data", "target_node": "train", "target_port": "data"}
    if feedback:
        recipe["bounded_iterations"] = [{"id": "repeat", "max_iterations": 2, "state": [edge]}]
    else:
        recipe["edges"] = [edge]
    if malformed:
        recipe["extensions"]["artifact_rights"] = None
    with pytest.raises(BackendError) as caught:
        rights.prepare_authorization(SimpleNamespace(), {"project_id": "test"}, recipe)
    assert caught.value.code == "license_rights_unreviewed"


def test_single_iteration_feedback_does_not_imply_an_asset_consumer():
    recipe = {"nodes": [{"id": "data", "kind": "dataset.synthetic"}], "resources": [],
              "bounded_iterations": [{"id": "once", "max_iterations": 1, "state": [
                  {"source_node": "data", "source_port": "data", "target_node": "data", "target_port": "data"}]}]}
    result = rights.prepare_authorization(SimpleNamespace(), {"project_id": "test"}, recipe)
    assert result["receipt"]["scope"] == "no_declared_external_asset_inputs"
