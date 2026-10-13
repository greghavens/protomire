# ruff: noqa: F811
"""Offline exact-byte local/source review with synthetic authored terms only."""
from copy import deepcopy
import hashlib
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from modelbuilder.api import create_app
from modelbuilder.hub_rights import digest, validate_execution_rights
from modelbuilder.models import ServiceError
from modelbuilder.source_workspace import SourceWorkspaces
from test_transfers import context, invoke, reviewed_policy, OWNER, ASSISTANT, USAGE  # noqa: F401


def capture(context, **changes):
    return invoke(context, "hub.license.local.inspect", {
        "source_alias": "output", "repo_type": "model", "files": ["weights.bin", "custom-terms.txt"],
        "evidence_document_paths": ["custom-terms.txt"], "max_bytes": 4096, "declared_license": "unknown", **changes})


def terms(context):
    raw = b"Synthetic owner-authored fixture terms: train, evaluate and export these fixture bytes in isolated tests only. Preserve this text.\n"
    (context[3] / "custom-terms.txt").write_bytes(raw)
    return raw


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def source_context(context):
    service, _, pid, root, _ = context
    terms(context)
    (root / "model.py").write_text("raise RuntimeError('Rights review must never import this file')\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Synthetic source")
    service.source_workspaces = SourceWorkspaces({"authored-source": {"path": str(root), "trusted": False, "project_ids": [pid]}})
    identity = service.source_workspaces.inspect("authored-source", pid)
    project = invoke(context, "project.get")
    project = invoke(context, "project.source.bind", {"expected_revision": project["revision"], "source_alias": "authored-source", "source_repository_id": identity["repository_id"], "trusted": True})
    recipe = invoke(context, "recipe.get")
    fields = {"recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"], "expected_project_revision": project["revision"], "source_repository_id": identity["repository_id"], "commit": git(root, "rev-parse", "HEAD"), "paths": ["model.py"]}
    preview = invoke(context, "recipe.source.pin.preview", fields)
    recipe = invoke(context, "recipe.source.pin", {**fields, "review_digest": preview["review_digest"], "confirm_pin": True})["recipe"]
    payload = {"repo_type": "source", "files": ["model.py", "custom-terms.txt"], "source_repository_id": identity["repository_id"], "source_commit": fields["commit"]}
    evidence = capture(context, **payload)
    policy = reviewed_policy(context, evidence, permitted_uses=["train", "evaluate", "export"], required_files=[{"path": "custom-terms.txt", "sha256": hashlib.sha256(terms(context)).hexdigest()}])
    request = {"recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"], "expected_project_revision": project["revision"], "license_policy_id": policy["policy_id"], "intended_use": "train", "usage_context": USAGE}
    return evidence, policy, request


def test_explicit_custom_terms_capture_exact_utf8_with_unknown_label_and_no_inference(context):
    raw = terms(context)
    evidence = capture(context)
    document = evidence["evidence"]["documents"][0]
    assert document["path"] == "custom-terms.txt" and document["text"].encode() == raw
    assert document["sha256"] == hashlib.sha256(raw).hexdigest()
    assert evidence["evidence"]["declared_license"] == "unknown"
    assert evidence["evidence"]["permissions_inferred_from_label"] is False
    policy = reviewed_policy(context, evidence, permitted_uses=["train"], gated_terms_acknowledged=False)
    assert policy["permitted_uses"] == ["train"] and policy["source"]["repo_type"] == "model"
    with pytest.raises(ServiceError, match="does not permit"):
        invoke(context, "hub.local.register", {"evidence_id": evidence["evidence_id"], "license_policy_id": policy["policy_id"]})
    assert not context[1].calls


@pytest.mark.parametrize("case", ["missing", "outside", "blank", "binary", "symlink"])
def test_bad_local_terms_never_become_reviewed_policy(context, case):
    terms(context)
    payload = {}
    if case == "missing":
        payload = {"files": ["weights.bin"], "evidence_document_paths": ["custom-terms.txt"]}
    elif case == "outside":
        payload = {"files": ["../private.txt"], "evidence_document_paths": ["../private.txt"]}
    elif case == "blank":
        (context[3] / "custom-terms.txt").write_text(" \n")
    elif case == "binary":
        (context[3] / "custom-terms.txt").write_bytes(b"\xff\xfe")
    else:
        (context[3] / "custom-terms.txt").unlink()
        (context[3] / "custom-terms.txt").symlink_to(context[3] / "README.md")
    with pytest.raises(ServiceError):
        evidence = capture(context, **payload)
        reviewed_policy(context, evidence)
    assert not context[0].store.list("hub_license_policy", context[2])


def test_no_license_text_remains_explicit_and_unreviewed(context):
    evidence = invoke(context, "hub.license.local.inspect", {"source_alias": "output", "repo_type": "model", "files": ["weights.bin"], "max_bytes": 4096, "declared_license": "unknown"})
    assert evidence["evidence"]["documents"] == []
    with pytest.raises(ServiceError, match="label alone"):
        reviewed_policy(context, evidence, license_document_paths=["weights.bin"])


def test_one_bounded_source_review_binds_future_scratch_without_model_asset(context):
    evidence, policy, request = source_context(context)
    result = invoke(context, "hub.source.policy.bind", request)
    assert result["future_outputs_only"] and not result["execution_allowed"] and not result["assets_registered"]
    assert result["recipe"]["resources"] == [] and result["recipe"]["external_dependencies"] == []
    assert result["source_origin"]["files"] == result["recipe"]["source"]["files"]
    assert result["source_origin"]["commit"] == result["recipe"]["source"]["commit"]
    assert result["rights"]["sources"][0]["repo_type"] == "source"
    admitted = validate_execution_rights(result["recipe"], "train", context[0].hub.policy_snapshot(context[2]))
    assert admitted["policy_ids"] == [policy["policy_id"]]
    assert not context[0].store.list("hub_asset", context[2]) and not context[1].calls
    with pytest.raises(ServiceError, match="not a model or dataset"):
        invoke(context, "hub.local.register", {"evidence_id": evidence["evidence_id"], "license_policy_id": policy["policy_id"]})


@pytest.mark.parametrize("change", ["commit", "hash", "extra", "missing", "forged_policy", "context"])
def test_current_admission_rejects_source_edits_and_forged_origin(context, change):
    _, policy, request = source_context(context)
    recipe = invoke(context, "hub.source.policy.bind", request)["recipe"]
    policies = context[0].hub.policy_snapshot(context[2])
    altered = deepcopy(recipe)
    if change == "commit":
        altered["source"]["commit"] = "f" * 40
    elif change == "hash":
        altered["source"]["files"][0]["sha256"] = "f" * 64
    elif change == "extra":
        altered["source"]["files"].append({"path": "unreviewed.py", "sha256": "f" * 64})
    elif change == "missing":
        altered["source"]["files"] = []
    elif change == "forged_policy":
        altered["reviewed_license_policies"] = policies
        altered["extensions"]["artifact_rights"]["sources"][0]["license_policy_id"] = "caller-forged"
    else:
        altered["extensions"]["usage_context"]["commercial"] = True
    with pytest.raises(ServiceError):
        validate_execution_rights(altered, "train", policies)
    assert context[0].hub.policy_snapshot(context[2])[policy["policy_id"]] == policy


def test_parameter_only_recipe_change_retains_origin_but_revocation_blocks_it(context):
    _, policy, request = source_context(context)
    recipe = invoke(context, "hub.source.policy.bind", request)["recipe"]
    recipe["description"] = "Adjusted experiment documentation without changing code or use"
    recipe["extensions"]["experiment_parameters"] = {"learning_rate": 0.0005}
    saved = invoke(context, "recipe.update", {"recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"], "recipe": recipe})
    assert validate_execution_rights(saved, "train", context[0].hub.policy_snapshot(context[2]))["policy_ids"] == [policy["policy_id"]]
    invoke(context, "hub.license.revoke", {"policy_id": policy["policy_id"], "expected_revision": policy["revision"], "reason": "Synthetic owner revocation"})
    with pytest.raises(ServiceError):
        validate_execution_rights(saved, "train", context[0].hub.policy_snapshot(context[2]))


@pytest.mark.parametrize("change", ["recipe_revision", "project_revision", "source_bytes", "terms_bytes", "missing_source", "wrong_policy", "context"])
def test_source_binding_rechecks_exact_current_identity_before_writing(context, change):
    _, _, request = source_context(context)
    if change == "recipe_revision":
        request["expected_revision"] -= 1
    elif change == "project_revision":
        request["expected_project_revision"] -= 1
    elif change == "source_bytes":
        (context[3] / "model.py").write_text("changed = True\n")
    elif change == "terms_bytes":
        (context[3] / "custom-terms.txt").write_text("Changed terms after review")
    elif change == "missing_source":
        context[0].source_workspaces.aliases["authored-source"]["project_ids"] = []
    elif change == "wrong_policy":
        request["license_policy_id"] = reviewed_policy(context, capture(context))["policy_id"]
    else:
        request["usage_context"] = {**USAGE, "commercial": True}
    before = invoke(context, "recipe.get")
    with pytest.raises(ServiceError):
        invoke(context, "hub.source.policy.bind", request)
    assert invoke(context, "recipe.get") == before


def test_source_bind_preserves_unresolved_parents_and_existing_resources(context):
    _, _, request = source_context(context)
    recipe = invoke(context, "recipe.get")
    unresolved = {"schema_version": "modelbuilder.artifact-rights/v1", "sources": [], "unresolved": [{"resource_id": "unknown-parent", "reason": "missing_terms"}], "parent_rights_digests": ["a" * 64]}
    recipe.setdefault("extensions", {})["artifact_rights"] = unresolved
    saved = invoke(context, "recipe.update", {"recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"], "recipe": recipe})
    result = invoke(context, "hub.source.policy.bind", {**request, "expected_revision": saved["revision"]})
    assert result["rights"]["unresolved"] == unresolved["unresolved"]
    assert set(result["rights"]["parent_rights_digests"]) == {"a" * 64, digest(unresolved)}
    with pytest.raises(ServiceError, match="unresolved"):
        validate_execution_rights(result["recipe"], "train", context[0].hub.policy_snapshot(context[2]))


def test_shared_http_and_mcp_expose_source_contract_and_forbid_unapproved_policy(context):
    terms(context)
    client = TestClient(create_app(context[0], {"fixture-assistant": ASSISTANT}))
    response = client.post("/v1/operations/hub.license.local.inspect", json={"project_id": context[2], "idempotency_key": "http", "source_alias": "output", "repo_type": "model", "files": ["custom-terms.txt", "weights.bin"], "evidence_document_paths": ["custom-terms.txt"], "max_bytes": 4096, "declared_license": "unknown"}, headers={"Authorization": "Bearer fixture-assistant"})
    assert response.status_code == 200 and response.json()["evidence"]["documents"][0]["path"] == "custom-terms.txt"
    from modelbuilder.mcp_server import catalog_tools
    names = {tool.name for tool in catalog_tools({"operations": context[0].catalog(ASSISTANT)})[0]}
    assert "hub_source_policy_bind" in names
    with pytest.raises(ServiceError) as caught:
        invoke(context, "hub.license.review", {"evidence_id": response.json()["evidence_id"]}, ASSISTANT)
    assert caught.value.code == "forbidden"


def test_real_native_review_queue_keeps_terms_unreviewed_until_exact_owner_approval(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "distribution"))
    import managed_core as core
    from modelbuilder.hub import HubTransfers
    app = core.make_application(tmp_path / "app", tmp_path, {}, "a" * 64)
    service, queue, auth = app.state.service, app.state.review_queue, app.state.auth
    owner = auth.authenticate("Bearer " + "a" * 64)
    try:
        project = service.invoke("project.create", {"slug": "native-local", "model_name": "Native local fixture", "idempotency_key": "create"}, owner)
        pid = project["project_id"]
        root = tmp_path / "local"
        root.mkdir()
        (root / "unusual-terms.txt").write_text("Synthetic owner-authored bytes; isolated test use only.")
        service.hub.close()
        service.hub = HubTransfers(service, config={"publication_roots": {pid: {"fixture": str(root)}}}, providers={})
        evidence = service.invoke("hub.license.local.inspect", {"project_id": pid, "source_alias": "fixture", "repo_type": "dataset", "files": ["unusual-terms.txt"], "evidence_document_paths": ["unusual-terms.txt"], "max_bytes": 1024, "declared_license": "unknown", "idempotency_key": "inspect"}, owner)
        body = {"project_id": pid, "evidence_id": evidence["evidence_id"], "evidence_digest": evidence["evidence_digest"], "license_document_paths": ["unusual-terms.txt"], "permitted_uses": ["train"], "intended_context": "Synthetic local review", "reviewed_usage_context": USAGE, "required_files": [], "required_attributions": [], "allowed_destination_licenses": [], "gated_terms_acknowledged": False, "confirm_review": True, "idempotency_key": str(uuid4())}
        with pytest.raises(ServiceError) as caught:
            service.invoke("hub.license.review", body, owner)
        assert caught.value.code == "forbidden"
        pending = queue.submit(owner, pid, "hub.license.review", body)
        assert not service.store.list("hub_license_policy", pid)
        decision = queue.decide(owner, pending["id"], pending["review_digest"], "approve")
        assert decision["status"] == "succeeded"
        assert decision["result"]["license_evidence_digest"] == evidence["evidence_digest"]
        assert decision["approved_by"] == owner.id and not queue.grants.active()
    finally:
        service.hub.close()
        service.store.close()


def test_recipe_update_cannot_turn_an_unrelated_model_review_into_source_authority(context):
    source_context(context)
    policy = reviewed_policy(context, capture(context))
    recipe = invoke(context, "recipe.get")
    recipe.setdefault("extensions", {}).update({"usage_context": USAGE, "artifact_rights": {
        "schema_version": "modelbuilder.artifact-rights/v1", "sources": [{**policy["source"], "license_evidence_digest": policy["license_evidence_digest"], "license_policy_id": policy["policy_id"]}], "unresolved": [], "parent_rights_digests": []}})
    saved = invoke(context, "recipe.update", {"recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"], "recipe": recipe})
    with pytest.raises(ServiceError, match="Direct recipe origin"):
        validate_execution_rights(saved, "train", context[0].hub.policy_snapshot(context[2]))


def test_raw_recipe_update_preserves_old_receipt_but_cannot_admit_changed_source(context):
    _, _, request = source_context(context)
    result = invoke(context, "hub.source.policy.bind", {**request, "idempotency_key": "fixed-origin-bind"})
    recipe = deepcopy(result["recipe"])
    recipe["source"]["files"][0]["sha256"] = "f" * 64
    saved = invoke(context, "recipe.update", {"recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"], "recipe": recipe})
    with pytest.raises(ServiceError, match="Pinned source files changed"):
        validate_execution_rights(saved, "train", context[0].hub.policy_snapshot(context[2]))
    assert invoke(context, "hub.source.policy.bind", {**request, "idempotency_key": "fixed-origin-bind"}) == result
    assert invoke(context, "recipe.get")["revision"] == saved["revision"]


@pytest.mark.parametrize("change", ["repository", "commit", "missing_file", "root"])
def test_source_capture_rejects_unrelated_repository_commit_or_partial_source(context, change, tmp_path):
    evidence, _, _ = source_context(context)
    origin = evidence["evidence"]["source_origin"]
    fields = {"repo_type": "source", "files": ["model.py", "custom-terms.txt"], "source_repository_id": origin["repository_id"], "source_commit": origin["commit"]}
    if change == "repository":
        fields["source_repository_id"] = "sha256:" + "f" * 64
    elif change == "commit":
        fields["source_commit"] = "f" * 40
    elif change == "missing_file":
        fields["files"] = ["custom-terms.txt"]
    else:
        other = tmp_path / "other"
        other.mkdir()
        for name in fields["files"]:
            (other / name).write_bytes((context[3] / name).read_bytes())
        context[0].hub.publication_roots[context[2]]["output"] = str(other)
    before = len(context[0].store.list("hub_license_evidence", context[2]))
    with pytest.raises(ServiceError):
        capture(context, **fields)
    assert len(context[0].store.list("hub_license_evidence", context[2])) == before


def test_changed_code_rereview_reports_explicit_origin_replacement_without_dropping_history(context):
    _, _, request = source_context(context)
    old = invoke(context, "hub.source.policy.bind", request)
    root = context[3]
    (root / "model.py").write_text("raise RuntimeError('Revised source must not execute in review')\n")
    git(root, "add", "model.py")
    git(root, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Revised source")
    fields = {"recipe_id": old["recipe_id"], "expected_revision": old["revision"], "expected_project_revision": request["expected_project_revision"], "source_repository_id": old["source_origin"]["repository_id"], "commit": git(root, "rev-parse", "HEAD"), "paths": ["model.py"]}
    preview = invoke(context, "recipe.source.pin.preview", fields)
    current = invoke(context, "recipe.source.pin", {**fields, "review_digest": preview["review_digest"], "confirm_pin": True})["recipe"]
    evidence = capture(context, repo_type="source", files=["model.py", "custom-terms.txt"], source_repository_id=fields["source_repository_id"], source_commit=fields["commit"])
    policy = reviewed_policy(context, evidence)
    with pytest.raises(ServiceError) as caught:
        invoke(context, "hub.source.policy.bind", {**request, "expected_revision": current["revision"], "license_policy_id": policy["policy_id"]})
    assert caught.value.code == "license_source_replacement_required"
    assert invoke(context, "recipe.get") == current
    assert current["extensions"]["artifact_rights"] == old["rights"]


def replacement_context(context):
    _, _, request = source_context(context)
    old = invoke(context, "hub.source.policy.bind", request)
    root = context[3]
    (root / "model.py").write_text("raise RuntimeError('Changed code is never executed during replacement review')\n")
    git(root, "add", "model.py")
    git(root, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Changed reviewed source")
    fields = {"recipe_id": old["recipe_id"], "expected_revision": old["revision"], "expected_project_revision": request["expected_project_revision"], "source_repository_id": old["source_origin"]["repository_id"], "commit": git(root, "rev-parse", "HEAD"), "paths": ["model.py"]}
    preview = invoke(context, "recipe.source.pin.preview", fields)
    current = invoke(context, "recipe.source.pin", {**fields, "review_digest": preview["review_digest"], "confirm_pin": True})["recipe"]
    evidence = capture(context, repo_type="source", files=["model.py", "custom-terms.txt"], source_repository_id=fields["source_repository_id"], source_commit=fields["commit"])
    policy = reviewed_policy(context, evidence)
    replacement = {**request, "expected_revision": current["revision"], "expected_recipe_digest": current["digest"], "license_policy_id": policy["policy_id"], "replace_source_policy_ids": [old["policy_id"]], "confirm_replace": True}
    return old, current, policy, replacement


def test_explicit_replacement_handles_normal_code_evolution_and_retains_history(context):
    old, current, policy, request = replacement_context(context)
    result = invoke(context, "hub.source.policy.replace", request)
    assert result["replaced_direct_source_policy_ids"] == [old["policy_id"]]
    assert result["rights"]["sources"] == [{**policy["source"], "license_evidence_digest": policy["license_evidence_digest"], "license_policy_id": policy["policy_id"]}]
    assert result["prior_rights_digest"] == digest(old["rights"])
    assert digest(old["rights"]) in result["rights"]["parent_rights_digests"]
    assert context[0].store.get("recipe", old["recipe_id"], old["revision"])["extensions"]["artifact_rights"] == old["rights"]
    assert validate_execution_rights(result["recipe"], "train", context[0].hub.policy_snapshot(context[2]))["policy_ids"] == [policy["policy_id"]]
    assert result["recipe"]["resources"] == current["resources"]


@pytest.mark.parametrize("change", ["digest", "revision", "unknown_policy", "new_policy", "missing_confirmation", "nonpublisher", "changed_terms"])
def test_replacement_denies_stale_forged_or_unapproved_old_new_decision(context, change):
    _, _, policy, request = replacement_context(context)
    actor = OWNER
    if change == "digest":
        request["expected_recipe_digest"] = "f" * 64
    elif change == "revision":
        request["expected_revision"] -= 1
    elif change == "unknown_policy":
        request["replace_source_policy_ids"] = ["unrelated-policy"]
    elif change == "new_policy":
        request["replace_source_policy_ids"] = [policy["policy_id"]]
    elif change == "missing_confirmation":
        request.pop("confirm_replace")
    elif change == "nonpublisher":
        actor = ASSISTANT
    else:
        (context[3] / "custom-terms.txt").write_text("Changed terms")
    before = invoke(context, "recipe.get")
    with pytest.raises(ServiceError):
        invoke(context, "hub.source.policy.replace", request, actor)
    assert invoke(context, "recipe.get") == before


def test_replacement_preserves_actual_input_parents_and_unresolved_entries(context):
    old, current, _, request = replacement_context(context)
    current["extensions"]["artifact_rights"]["unresolved"].append({"reason": "unresolved-actual-parent"})
    parent = {"id": "actual-parent", "kind": "checkpoint", "url": "urn:sha256:" + "f" * 64, "sha256": "f" * 64, "license": "fixture-only", "metadata": {"rights": deepcopy(old["rights"])}}
    current["resources"].append(parent)
    saved = invoke(context, "recipe.update", {"recipe_id": current["recipe_id"], "expected_revision": current["revision"], "recipe": current})
    result = invoke(context, "hub.source.policy.replace", {**request, "expected_revision": saved["revision"], "expected_recipe_digest": saved["digest"]})
    assert result["recipe"]["resources"] == saved["resources"]
    assert result["rights"]["unresolved"] == [{"reason": "unresolved-actual-parent"}]
    with pytest.raises(ServiceError, match="unresolved"):
        validate_execution_rights(result["recipe"], "train", context[0].hub.policy_snapshot(context[2]))


def test_managed_native_replacement_discloses_exact_old_new_policies_and_does_not_mutate_until_approved(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "distribution"))
    import managed_core as core
    from modelbuilder.hub import HubTransfers
    from modelbuilder.models import Actor
    app = core.make_application(tmp_path / "app", tmp_path, {}, "a" * 64)
    service, queue = app.state.service, app.state.review_queue
    owner = app.state.auth.authenticate("Bearer " + "a" * 64)
    project = service.invoke("project.create", {"slug": "source-replacement", "model_name": "Source replacement fixture", "idempotency_key": "create"}, owner)
    root = tmp_path / "source"
    root.mkdir()
    (root / "weights.bin").write_bytes(b"fixture")
    service.hub.close()
    config = {"publication_roots": {project["project_id"]: {"output": str(root)}}}
    service.hub = HubTransfers(service, providers={}, config=config)
    app.state.auth._tokens["synthetic-publisher-fixture"] = OWNER
    fixture = (service, None, project["project_id"], root, config)
    try:
        old, current, policy, request = replacement_context(fixture)
        payload = {"project_id": project["project_id"], "idempotency_key": "native-replace", **request}
        consumer = Actor("desktop-assistant:fixture", {"read"}, project_ids={project["project_id"]})
        pending = queue.submit(consumer, project["project_id"], "hub.source.policy.replace", payload)
        disclosure = pending["disclosure"]
        assert disclosure["current_recipe"]["digest"] == current["digest"]
        assert disclosure["replaced_direct_source_policies"][0]["policy_id"] == old["policy_id"]
        assert disclosure["new_source_policy"]["policy_id"] == policy["policy_id"]
        assert invoke(fixture, "recipe.get") == current
        with pytest.raises(ServiceError):
            service.invoke("hub.source.policy.replace", payload, consumer)
        result = queue.decide(owner, pending["id"], pending["review_digest"], "approve")
        assert result["status"] == "succeeded", result
        assert result["result"]["replaced_direct_source_policy_ids"] == [old["policy_id"]]
        assert not queue.grants.active()
    finally:
        service.hub.close()
        service.store.close()
