# ruff: noqa: F811
"""Declared-input rights enforcement with explicit synthetic fixture decisions."""
from copy import deepcopy
import pytest

from modelbuilder.hub_rights import digest, validate_artifact_rights, validate_execution_rights
from modelbuilder.models import ServiceError
from test_transfers import context, invoke, plan, start, finish, preview, publish, reviewed_policy, USAGE, COMMIT, ASSISTANT  # noqa: F401





def envelope_policy():
    source = {"provider": "local", "repo_type": "dataset", "repo_id": "authored-fixture", "commit": "a" * 64}
    policy = {"schema_version": "modelbuilder.license-policy/v1", "status": "reviewed", "revision": 1,
              "policy_id": "policy", "reviewed_by": "owner", "reviewed_at": "2026-10-13T00:00:00Z",
              "source": source, "license_evidence_digest": "b" * 64, "permitted_uses": ["train", "evaluate", "promote", "export"],
              "source_files": [{"path": "test.bin", "sha256": "d" * 64}], "reviewed_usage_context": USAGE, "unresolved_obligations": [], "gated": False}
    policy["review_digest"] = digest(policy)
    envelope = {"schema_version": "modelbuilder.artifact-rights/v1", "sources": [{**source, "license_policy_id": "policy", "license_evidence_digest": "b" * 64}], "unresolved": [], "parent_rights_digests": []}
    return envelope, {"policy": policy}


def test_scratch_without_declared_assets_has_no_fictional_approval():
    result = validate_execution_rights({"resources": [], "external_dependencies": []})
    assert result["status"] == "no_declared_external_asset_inputs" and result["policy_ids"] == []


@pytest.mark.parametrize("kind", ["model", "dataset", "checkpoint"])
@pytest.mark.parametrize("metadata", [{}, {"rights": {}}, {"rights": {"schema_version": "modelbuilder.artifact-rights/v1", "sources": [], "unresolved": []}}, {"rights": {"schema_version": "modelbuilder.artifact-rights/v1", "sources": "forged", "unresolved": []}}, "malformed", {"license_source": "malformed", "license_policy_id": "x", "license_evidence_digest": "a" * 64}])
def test_every_registered_asset_including_local_requires_complete_review(kind, metadata):
    with pytest.raises(ServiceError) as error:
        validate_execution_rights({"resources": [{"id": "local", "kind": kind, "metadata": metadata}]})
    assert error.value.code == "license_rights_unreviewed"


def test_exact_context_source_policy_revision_and_action_are_bound():
    rights, policies = envelope_policy()
    recipe = {"resources": [{"kind": "dataset", "sha256": "d" * 64, "metadata": {"rights": rights}}], "extensions": {"usage_context": USAGE}}
    receipt = validate_execution_rights(recipe, "train", policies)
    assert receipt["status"] == "reviewed_declared_inputs" and receipt["usage_context_digest"] == digest(USAGE)
    assert validate_artifact_rights(rights, "promote", policies, USAGE)["policy_ids"] == ["policy"]
    changed = deepcopy(recipe)
    changed["extensions"]["usage_context"]["commercial"] = True
    with pytest.raises(ServiceError):
        validate_execution_rights(changed, "train", policies)
    with pytest.raises(ServiceError):
        validate_execution_rights(recipe, "generate", policies)
    altered = deepcopy(policies)
    altered["policy"]["revision"] = 2
    with pytest.raises(ServiceError):
        validate_execution_rights(recipe, "train", altered)
    altered["policy"]["review_digest"] = digest({k: v for k, v in altered["policy"].items() if k != "review_digest"})
    altered["policy"]["source"]["commit"] = "c" * 64
    altered["policy"]["review_digest"] = digest({k: v for k, v in altered["policy"].items() if k != "review_digest"})
    with pytest.raises(ServiceError):
        validate_execution_rights(recipe, "train", altered)


def test_caller_recipe_policy_map_cannot_grant_rights():
    rights, policies = envelope_policy()
    with pytest.raises(ServiceError):
        validate_execution_rights({"resources": [{"kind": "model", "metadata": {"rights": rights}}], "reviewed_license_policies": policies,
                                   "extensions": {"usage_context": USAGE}}, "train")


def test_license_evidence_retains_exact_text_card_and_immutable_hash(context):
    record = invoke(context, "hub.license.inspect", {"provider": "fake", "repo_id": "owner/model", "repo_type": "model"})
    evidence = record["evidence"]
    assert evidence["commit"] == COMMIT and evidence["card_metadata"] == {"license": "mit"}
    assert record["evidence_digest"] == digest(evidence)
    assert evidence["documents"][0]["text"].startswith("---\nlicense: mit")
    assert not evidence["permissions_inferred_from_label"]
    policy = reviewed_policy(context, record)
    assert policy == invoke(context, "hub.license.policy.get", {"policy_id": policy["policy_id"]})


def test_modify_execute_actor_cannot_mint_license_review(context):
    record = invoke(context, "hub.license.inspect", {"provider": "fake", "repo_id": "owner/model", "repo_type": "model"})
    policy = reviewed_policy(context, record)
    with pytest.raises(ServiceError) as error:
        invoke(context, "hub.license.revoke", {"policy_id": policy["policy_id"], "expected_revision": 1, "reason": "test"}, ASSISTANT)
    assert error.value.code == "forbidden"


def test_gated_access_and_terms_acknowledgment_are_separate(context):
    context[1].gated = True
    record = invoke(context, "hub.license.inspect", {"provider": "fake", "repo_id": "owner/model", "repo_type": "model"})
    with pytest.raises(ServiceError) as error:
        reviewed_policy(context, record, gated_terms_acknowledged=False)
    assert error.value.code == "license_terms_unacknowledged"
    policy = reviewed_policy(context, record)
    with pytest.raises(ServiceError) as error:
        plan(context, license_policy_id=policy["policy_id"])
    assert error.value.code == "hub_access_required"


def revoke(context, policy_id):
    return invoke(context, "hub.license.revoke", {"policy_id": policy_id, "expected_revision": 1, "reason": "Fixture policy withdrawn before dispatch"})


def test_revoked_policy_blocks_download_retry_and_preserves_history(context):
    planned = plan(context)
    policy_id = planned["payload"]["license_policy_id"]
    result = revoke(context, policy_id)
    assert result["revision"] == 2 and result["status"] == "revoked"
    assert context[0].store.get("hub_license_policy", policy_id, 1)["status"] == "reviewed"
    transfer = finish(context, start(context, planned))
    assert transfer["state"] == "failed" and transfer["error"]["code"] == "license_rights_unreviewed"
    assert not context[0].store.list("hub_asset", context[2])
    assert not any(action == "download" for action, _ in context[1].calls)


def test_revoke_while_downloading_prevents_asset_registration(context):
    provider = context[1]
    provider.block = "download"
    planned = plan(context)
    running = start(context, planned)
    assert provider.entered.wait(2)
    revoke(context, planned["payload"]["license_policy_id"])
    provider.release.set()
    assert finish(context, running)["state"] == "failed"
    assert not context[0].store.list("hub_asset", context[2])


def test_revoked_publication_policy_stops_before_upload(context):
    value = preview(context)
    revoke(context, value["payload"]["rights"]["sources"][0]["license_policy_id"])
    result = finish(context, publish(context, value))
    assert result["state"] == "failed" and not context[1].published


def test_publication_requires_full_context_and_exact_required_notices(context):
    value = preview(context)
    source = value["payload"]["rights"]["sources"][0]
    row = context[0].store.get("hub_license_policy", source["license_policy_id"])
    evidence = context[0].store.get("hub_license_evidence", row["evidence_id"])
    required = evidence["evidence"]["documents"][0]
    policy = reviewed_policy(context, evidence, required_files=[{"path": "LICENSE", "sha256": required["sha256"]}])
    asset = invoke(context, "hub.local.register", {"evidence_id": evidence["evidence_id"], "license_policy_id": policy["policy_id"]})
    with pytest.raises(ServiceError, match="preserved exactly"):
        preview(context, source_asset_ids=[asset["asset_id"]])
    with pytest.raises(ServiceError, match="usage context"):
        preview(context, source_asset_ids=[asset["asset_id"]], usage_context={**USAGE, "commercial": True})


def test_local_snapshot_rejects_changed_bytes_after_rights_review(context):
    record = invoke(context, "hub.license.local.inspect", {"source_alias": "output", "repo_type": "model", "files": ["README.md", "weights.bin"], "max_bytes": 1024, "declared_license": "mit"})
    policy = reviewed_policy(context, record)
    (context[3] / "weights.bin").write_bytes(b"changed")
    with pytest.raises(ServiceError):
        invoke(context, "hub.local.register", {"evidence_id": record["evidence_id"], "license_policy_id": policy["policy_id"]})
    assert not context[0].store.list("hub_asset", context[2])


def test_backend_receives_internal_store_snapshot_not_payload_grants(context):
    class Backend:
        def with_execution_rights(self, **kwargs):
            self.received = kwargs
            return self
    backend = Backend()
    payload = {"project_id": context[2], "recipe": {}, "reviewed_license_policies": {"forged": {}}}
    assert context[0].hub.resolve_backend(backend, payload) is backend
    assert backend.received == {"project_id": context[2], "policies": {}, "policy_store_path": context[0].store.path}
    assert "reviewed_license_policies" not in payload


def test_reviewed_dataset_asset_lineage_survives_registered_split_binding(context):
    provider = context[1]
    provider.files = {"rows.jsonl": b'{"id":"one","text":"hello"}\n', "LICENSE": b"Synthetic fixture rights for isolated tests.\n"}
    planned = plan(context, repo_type="dataset", files=["rows.jsonl"])
    asset = invoke(context, "hub.asset.get", {"asset_id": finish(context, start(context, planned))["asset_id"]})
    file = next(item for item in asset["files"] if item["path"] == "rows.jsonl")
    manifest = {"name": "rights-fixture", "benchmark": "rights", "license": "mit", "provenance": {"source": "fixture"},
                "sources": [{"id": "rows", "root_id": asset["dataset_root_alias"], "path": "rows.jsonl", "format": "jsonl", "sha256": file["sha256"]}],
                "id_column": "id", "row_schema": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}}},
                "splits": [{"id": "train", "role": "train", "source_ids": ["rows"]}]}
    registered = invoke(context, "dataset.register", {"manifest": manifest})
    recipe = invoke(context, "recipe.get")
    bound = invoke(context, "dataset.bind", {"recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"],
                    "dataset_id": registered["dataset_id"], "dataset_revision": 1, "split_id": "train", "purpose": "training", "node_id": "rows"})["recipe"]
    assert all(item["metadata"]["rights"]["sources"] == asset["rights"]["sources"] for item in bound["external_dependencies"])
    bound.setdefault("extensions", {})["usage_context"] = USAGE
    assert validate_execution_rights(bound, "train", context[0].hub.policy_snapshot(context[2]))["policy_ids"] == [planned["payload"]["license_policy_id"]]


def test_unrelated_resource_hash_cannot_borrow_another_reviewed_source_policy():
    rights, policies = envelope_policy()
    recipe = {"resources": [{"kind": "dataset", "id": "unrelated", "sha256": "e" * 64, "metadata": {"rights": rights}}], "extensions": {"usage_context": USAGE}}
    with pytest.raises(ServiceError, match="source file closure"):
        validate_execution_rights(recipe, "train", policies)


def test_verified_git_blob_materialization_adds_sha256_policy_revision_without_permissions(context):
    import hashlib
    provider = context[1]
    original = provider.call

    def call(action, payload, **kwargs):
        result = original(action, payload, **kwargs)
        if action in {"inspect", "license.inspect"}:
            for item in result["files"]:
                raw = provider.files[item["path"]]
                item["sha256"] = None
                item["git_blob_sha1"] = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw, usedforsecurity=False).hexdigest()
        return result
    provider.call = call
    planned = plan(context)
    before = invoke(context, "hub.license.policy.get", {"policy_id": planned["payload"]["license_policy_id"]})
    asset = invoke(context, "hub.asset.get", {"asset_id": finish(context, start(context, planned))["asset_id"]})
    after = invoke(context, "hub.license.policy.get", {"policy_id": before["policy_id"]})
    assert after["revision"] == before["revision"] + 1 and after["permitted_uses"] == before["permitted_uses"]
    assert after["reviewed_usage_context"] == before["reviewed_usage_context"] and not after["materialization_changes_permissions"]
    assert {item["sha256"] for item in after["verified_source_files"]} == {item["sha256"] for item in asset["files"]}
    recipe = {"resources": asset["resources"], "extensions": {"usage_context": USAGE}}
    assert validate_execution_rights(recipe, "train", context[0].hub.policy_snapshot(context[2]))["policy_revisions"][0]["revision"] == 2


def test_explicit_local_dataset_policy_binding_preserves_real_external_root(context):
    from modelbuilder.dataset_binding import reference_dependency, source_dependencies
    service, _, pid, root, _ = context
    raw = b'{"id":"one","text":"hello"}\n'
    (root / "rows.jsonl").write_bytes(raw)
    evidence = invoke(context, "hub.license.local.inspect", {"source_alias": "output", "repo_type": "dataset", "files": ["README.md", "rows.jsonl"], "max_bytes": 1024, "declared_license": "mit"})
    policy = reviewed_policy(context, evidence)
    import hashlib
    info = root.stat()
    service._dataset_registry._roots.setdefault(pid, {})["actual-local-root"] = (root, info.st_dev, info.st_ino)
    manifest = {"name": "local", "benchmark": "local", "license": "mit", "provenance": {"source": "locally-authored fixture"},
                "sources": [{"id": "rows", "root_id": "actual-local-root", "path": "rows.jsonl", "format": "jsonl", "sha256": hashlib.sha256(raw).hexdigest()}],
                "id_column": "id", "row_schema": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}}},
                "splits": [{"id": "train", "role": "train", "source_ids": ["rows"]}]}
    registered = invoke(context, "dataset.register", {"manifest": manifest})
    request = {"dataset_id": registered["dataset_id"], "dataset_revision": 1, "source_policies": {"rows": policy["policy_id"]}, "intended_use": "train", "usage_context": USAGE}
    binding = invoke(context, "hub.dataset.policy.bind", request)
    assert invoke(context, "hub.dataset.policy.bind", request) == binding
    reference = service._dataset_registry.portable_reference(pid, registered["dataset_id"], 1)
    rights = service.hub.dataset_rights(reference)
    assert rights == binding["rights"]
    assert reference["manifest"]["sources"][0]["root_id"] == "actual-local-root"
    recipe = {"resources": [reference_dependency(reference, rights), *source_dependencies(reference, rights)], "extensions": {"usage_context": USAGE}}
    assert validate_execution_rights(recipe, "train", service.hub.policy_snapshot(pid))["policy_ids"] == [policy["policy_id"]]
    other = reviewed_policy(context)
    with pytest.raises(ServiceError, match="outside the selected reviewed policy"):
        invoke(context, "hub.dataset.policy.bind", {**request, "source_policies": {"rows": other["policy_id"]}})
    with pytest.raises(ServiceError, match="every registered dataset source"):
        invoke(context, "hub.dataset.policy.bind", {**request, "source_policies": {"different": policy["policy_id"]}})
    revoke(context, policy["policy_id"])
    with pytest.raises(ServiceError, match="authoritative reviewed license policy"):
        validate_execution_rights(recipe, "train", service.hub.policy_snapshot(pid))


def test_dataset_binding_records_explicit_action_and_rejects_conflicting_rebind(context):
    provider = context[1]
    provider.files = {"rows.jsonl": b'{"id":"one","text":"hello"}\n', "LICENSE": b"Synthetic rights.\n"}
    asset = invoke(context, "hub.asset.get", {"asset_id": finish(context, start(context, plan(context, repo_type="dataset", files=["rows.jsonl"])))["asset_id"]})
    file = next(item for item in asset["files"] if item["path"] == "rows.jsonl")
    manifest = {"name": "purpose", "benchmark": "purpose", "license": "mit", "provenance": {"source": "fixture"}, "sources": [{"id": "rows", "root_id": asset["dataset_root_alias"], "path": "rows.jsonl", "format": "jsonl", "sha256": file["sha256"]}], "id_column": "id", "row_schema": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}}}, "splits": [{"id": "train", "role": "train", "source_ids": ["rows"]}, {"id": "dev", "role": "validation", "source_ids": ["rows"]}]}
    # Split overlap is prohibited, so exercise explicit training plus a conflicting declaration.
    manifest["splits"] = manifest["splits"][:1]
    registered = invoke(context, "dataset.register", {"manifest": manifest})
    recipe = invoke(context, "recipe.get")
    request = {"recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"], "dataset_id": registered["dataset_id"], "dataset_revision": 1, "split_id": "train", "purpose": "training", "node_id": "rows"}
    bound = invoke(context, "dataset.bind", request)["recipe"]
    assert bound["nodes"][0]["provenance"]["intended_use"] == "train"
    bound["nodes"][0]["provenance"]["intended_use"] = "evaluate"
    saved = invoke(context, "recipe.update", {"recipe_id": bound["recipe_id"], "recipe": bound, "expected_revision": bound["revision"]})
    with pytest.raises(ServiceError, match="conflicts"):
        invoke(context, "dataset.bind", {**request, "expected_revision": saved["revision"]})
