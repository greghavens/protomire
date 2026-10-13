"""Receiving-project replay admission; all reviews below are test-authored only."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.admission import BackendError
from modelbuilder.backend.execution_rights import prepare_authorization
from modelbuilder.models import Actor, ServiceError
from modelbuilder.recipe import pin_source_files
from modelbuilder.service import OperationService
from modelbuilder.store import SQLiteStore
from test_replay_bundle import replay, ROOT


class RightsBoundaryBackend(ZenMLBackend):
    """Real service/right admission, with execution deliberately stopped at launch."""
    def invoke(self, operation, payload, actor=None):
        if operation == "run.plan":
            try:
                prepare_authorization(self, payload, payload["recipe"], durable=True)
            except BackendError as error:
                return {"executable": False, "blocked_reasons": [replay._error_record(error)]}
            return {"executable": True, "blocked_reasons": []}
        if operation == "run.status":
            return {"operation_id": payload["operation_id"], "status": "failed", "worker_alive": False}
        if operation == "run.logs":
            return {"lines": ["Test process boundary; no native worker was launched"]}
        raise AssertionError(operation)

    def prepare_execution(self, operation, payload):
        prepare_authorization(self, payload, payload["recipe"], durable=True)
        return payload

    def launch_prepared(self, prepared, authorize=None):
        if authorize:
            authorize()
        return {"operation_id": prepared["operation_id"], "status": "failed", "worker_alive": False}


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    for name in ("src/modelbuilder/backend/fixtures.py", "requirements/backend.lock"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    (root / "TERMS.txt").write_text("Test-authored synthetic fixture terms. Train, evaluate and export only in isolated tests.\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Replay Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "Test authored source"], check=True)
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    recipe = json.loads((ROOT / "examples/custom-pytorch/recipe.json").read_text())
    recipe = pin_source_files(recipe, root, ["src/modelbuilder/backend/fixtures.py", "requirements/backend.lock"], commit)
    (root / "recipe.json").write_text(json.dumps(recipe))
    return root


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.setattr("modelbuilder.backend.ZenMLBackend", RightsBoundaryBackend)


def prepare(source, tmp_path):
    output = tmp_path / "prepared.json"
    result = replay.prepare_recipe(source, tmp_path / "data", Path(sys.executable), output, trusted=True)
    assert result["status"] == "BLOCKED" and result["submitted"] is False
    return output, result


def review_authored_fixture(result):
    """Use only existing authorized service APIs, never a policy JSON import."""
    prepared = result["preparation"]
    pid, root = prepared["project_id"], Path(prepared["source_root"])
    service = OperationService(db_path=Path(prepared["data_dir"]) / "application.sqlite3", source_root=root,
        source_roots={"source": {"path": str(root), "trusted": True, "project_ids": [pid]}},
        hub_config={"publication_roots": {pid: {"source": str(root)}}})
    owner = Actor("test-authored-reviewer", frozenset({"read", "modify", "execute", "publish"}))
    count = 0
    def call(operation, **payload):
        nonlocal count
        count += 1
        return service.invoke(operation, {"project_id": pid, "idempotency_key": f"fixture:{count}", **payload}, owner)
    identity = service.source_workspaces.inspect("source", pid)
    project = service.invoke("project.get", {"project_id": pid}, owner)
    project = call("project.source.bind", expected_revision=project["revision"], source_alias="source",
                   source_repository_id=identity["repository_id"], trusted=True)
    recipe = service.invoke("recipe.get", {"project_id": pid, "recipe_id": prepared["recipe_id"]}, owner)
    evidence = call("hub.license.local.inspect", source_alias="source", repo_type="source",
        source_repository_id=identity["repository_id"], source_commit=recipe["source"]["commit"],
        files=[item["path"] for item in recipe["source"]["files"]] + ["TERMS.txt"],
        evidence_document_paths=["TERMS.txt"], max_bytes=4 * 1024 * 1024, declared_license="unknown")
    context = {"purpose": "Test-authored replay regression", "commercial": False, "deployment": "isolated test",
               "conditions": ["No third-party rights review or real policy approval"]}
    policy = call("hub.license.review", evidence_id=evidence["evidence_id"], evidence_digest=evidence["evidence_digest"],
        license_document_paths=["TERMS.txt"], permitted_uses=["train", "evaluate", "export"], intended_context="Isolated tests",
        required_files=[], required_attributions=[], allowed_destination_licenses=[], gated_terms_acknowledged=False,
        reviewed_usage_context=context, confirm_review=True)
    saved = call("hub.source.policy.bind", recipe_id=recipe["recipe_id"], expected_revision=recipe["revision"],
        expected_project_revision=project["revision"], license_policy_id=policy["policy_id"],
        intended_use="train", usage_context=context)
    service.store.close()
    return saved["recipe"], policy


def test_real_backend_plan_rejects_known_unreviewed_generated_lineage(source, tmp_path, monkeypatch):
    monkeypatch.setattr(ZenMLBackend, "_require_capability", lambda self: None)
    monkeypatch.setattr("modelbuilder.backend.placement.validate_placement", lambda *a, **k: {"device": "cpu"})
    prepared_path, result = prepare(source, tmp_path)
    reason = result["plan"]["blocked_reasons"][0]
    assert reason["code"] == "license_rights_unreviewed"
    assert {item["reason"] for item in reason["details"]["unresolved"]} == {"missing_reviewed_generated_origin"}
    store = SQLiteStore(tmp_path / "data/application.sqlite3")
    assert not store.list("operation") and not store.list("hub_license_policy")
    store.close()
    assert not (tmp_path / "data/backend/jobs").exists()
    output = tmp_path / "run.json"
    with pytest.raises(ServiceError) as caught:
        replay.run_prepared(prepared_path, output, expected_revision=result["execution_revision"],
                            expected_digest=result["execution_digest"], trusted=True)
    assert caught.value.code == "license_rights_unreviewed"
    failure = json.loads(output.read_text())
    assert failure["stage"] == "plan" and failure["submitted"] is False


def test_public_pretrained_missing_review_still_blocks_all_three_resources(source, tmp_path, boundary):
    recipe = json.loads((source / "recipe.json").read_text())
    public = json.loads((ROOT / "examples/pretrained-small/recipe.json").read_text())
    recipe["resources"] = public["resources"]
    (source / "recipe.json").write_text(json.dumps(recipe))
    _, result = prepare(source, tmp_path)
    unresolved = result["plan"]["blocked_reasons"][0]["details"]["unresolved"]
    assert len(unresolved) == 3
    assert {item["reason"] for item in unresolved} == {"missing_reviewed_license_evidence"}
    assert not (tmp_path / "data/backend/jobs").exists()


def test_authorized_git_review_reopens_same_project_and_normal_dispatch(source, tmp_path, boundary):
    path, result = prepare(source, tmp_path)
    reviewed, _ = review_authored_fixture(result)
    plan = replay.plan_prepared(path, tmp_path / "plan.json", trusted=True)
    assert plan["status"] == "PREPARED" and plan["execution_allowed"] is False
    assert plan["execution_revision"] == reviewed["revision"]
    output = tmp_path / "run.json"
    with pytest.raises(RuntimeError, match="Replay failed"):
        replay.run_prepared(path, output, expected_revision=reviewed["revision"],
                            expected_digest=reviewed["digest"], trusted=True)
    failure = json.loads(output.read_text())
    assert failure["submitted"] is True and failure["run"]["status"] == "failed"
    assert [item["operation"] for item in failure["transcript"]].count("run.submit") == 1


@pytest.mark.parametrize("mutation", ["revoked", "other_project", "wrong_source", "wrong_context"])
def test_reopened_review_checks_current_project_source_and_policy(source, tmp_path, boundary, mutation):
    path, result = prepare(source, tmp_path)
    _, policy = review_authored_fixture(result)
    store = SQLiteStore(tmp_path / "data/application.sqlite3")
    modified = deepcopy(policy)
    if mutation == "revoked":
        modified["status"] = "revoked"
    elif mutation == "other_project":
        modified["project_id"] = "unrelated-project"
    elif mutation == "wrong_source":
        modified["source_origin"]["files"][0]["sha256"] = "f" * 64
    else:
        modified["reviewed_usage_context"]["commercial"] = True
    from modelbuilder.hub_rights import digest
    modified["revision"] += 1
    modified["review_digest"] = digest({k: v for k, v in modified.items() if k != "review_digest"})
    store.put("hub_license_policy", modified["policy_id"], modified["project_id"], modified, policy["revision"])
    store.close()
    plan = replay.plan_prepared(path, tmp_path / "plan.json", trusted=True)
    assert plan["status"] == "BLOCKED" and plan["submitted"] is False
    assert plan["plan"]["blocked_reasons"][0]["code"].startswith("license_")


@pytest.mark.parametrize("mutation", ["forged_preparation", "source_bytes", "recipe_parameters", "revision"])
def test_prepared_replay_rejects_identity_drift_before_submit(source, tmp_path, boundary, mutation):
    path, result = prepare(source, tmp_path)
    reviewed, _ = review_authored_fixture(result)
    revision = reviewed["revision"]
    if mutation == "forged_preparation":
        result["preparation"]["project_id"] = "unrelated-project"
        path.write_text(json.dumps(result))
    elif mutation == "source_bytes":
        (source / "src/modelbuilder/backend/fixtures.py").write_text("changed = True\n")
    elif mutation == "recipe_parameters":
        service = OperationService(db_path=tmp_path / "data/application.sqlite3", source_root=source)
        altered = deepcopy(reviewed)
        altered["nodes"][0]["parameters"]["seed"] += 1
        service.invoke("recipe.update", {"project_id": reviewed["project_id"], "recipe_id": reviewed["recipe_id"],
            "expected_revision": reviewed["revision"], "recipe": altered, "idempotency_key": "changed"},
            Actor("test-editor", frozenset({"read", "modify"})))
        service.store.close()
    else:
        revision -= 1
    output = tmp_path / "run.json"
    with pytest.raises(Exception):
        replay.run_prepared(path, output, expected_revision=revision, expected_digest=reviewed["digest"], trusted=True)
    failure = json.loads(output.read_text())
    assert failure["submitted"] is False
    assert not any(item["operation"] == "run.submit" for item in failure["transcript"])


def test_initial_trust_and_source_failures_have_structured_results(tmp_path):
    output = tmp_path / "failure.json"
    with pytest.raises(ValueError, match="trust-repository-source"):
        replay.run_recipe(tmp_path / "missing", tmp_path / "data", Path(sys.executable), output)
    assert json.loads(output.read_text())["submitted"] is False
    with pytest.raises(FileNotFoundError):
        replay.prepare_recipe(tmp_path / "missing", tmp_path / "data", Path(sys.executable), output)
    assert json.loads(output.read_text())["error"]["type"] == "FileNotFoundError"
    assert not (tmp_path / "data").exists()


def test_uncertain_submit_preserves_service_operation_and_never_claims_no_launch(source, tmp_path, boundary, monkeypatch):
    path, result = prepare(source, tmp_path)
    reviewed, _ = review_authored_fixture(result)
    def uncertain(self, prepared, authorize=None):
        if authorize:
            authorize()
        raise RuntimeError("Injected lost acknowledgment after dispatch fence")
    monkeypatch.setattr(RightsBoundaryBackend, "launch_prepared", uncertain)
    output = tmp_path / "uncertain.json"
    with pytest.raises(ServiceError):
        replay.run_prepared(path, output, expected_revision=reviewed["revision"],
                            expected_digest=reviewed["digest"], trusted=True)
    failure = json.loads(output.read_text())
    assert failure["submit_attempted"] is True and failure["submitted"] is None
    assert failure["operation_id"] and failure["dispatch_state"] == "uncertain"
    assert failure["error"]["details"]["retry_semantics"] == "same_key_returns_existing_intent"


def test_identical_explicit_retry_reconciles_one_existing_operation(source, tmp_path, boundary):
    path, result = prepare(source, tmp_path)
    reviewed, _ = review_authored_fixture(result)
    results = []
    for index in range(2):
        output = tmp_path / f"run-{index}.json"
        with pytest.raises(RuntimeError, match="Replay failed"):
            replay.run_prepared(path, output, expected_revision=reviewed["revision"],
                                expected_digest=reviewed["digest"], trusted=True)
        results.append(json.loads(output.read_text()))
    assert results[0]["operation_id"] == results[1]["operation_id"]
    assert results[0]["submission"]["idempotency_key"] == results[1]["submission"]["idempotency_key"]
    assert results[0]["submission"]["existing_operation_recorded_before_submit"] is False
    assert results[1]["submission"]["existing_operation_recorded_before_submit"] is True
    store = SQLiteStore(tmp_path / "data/application.sqlite3")
    assert len(store.list("operation")) == 1
    store.close()


def test_cli_error_is_json_without_traceback_and_persists_requested_output(tmp_path):
    output = tmp_path / "cli-failure.json"
    result = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/replay_bundle.py"), "run",
        str(tmp_path / "missing"), "--data-dir", str(tmp_path / "data"), "--backend-python", sys.executable,
        "--output", str(output)], text=True, capture_output=True)
    assert result.returncode == 1 and "Traceback" not in result.stderr
    assert json.loads(result.stderr)["status"] == "FAIL"
    assert json.loads(output.read_text())["submit_attempted"] is False


@pytest.mark.parametrize("evidence_alias", ["snapshot", "separate-evidence"] )
def test_gitfree_snapshot_public_review_reopens_same_project_and_normal_dispatch(source, tmp_path, boundary, evidence_alias):
    from modelbuilder.recipe import export_bundle, import_bundle
    from snapshot_review_fixture import review_synthetic_snapshot
    extracted = tmp_path / "imported-gitfree-source"
    import_bundle(export_bundle(json.loads((source / "recipe.json").read_text()), source_root=source), extracted)
    (extracted / "TERMS.txt").write_text("Synthetic fixture terms: train, evaluate and export isolated test-authored fixtures only.\n")
    path, prepared = prepare(extracted, tmp_path)
    assert prepared["source_review_setup"]["source_roots"]["portable-snapshot"]["kind"] == "imported_snapshot"
    assert prepared["source_review_setup"]["source_roots"]["portable-snapshot"]["trusted"] is False
    reviewed = review_synthetic_snapshot(prepared, evidence_alias=evidence_alias)["recipe"]
    plan = replay.plan_prepared(path, tmp_path / "snapshot-plan.json", trusted=True)
    assert plan["status"] == "PREPARED" and plan["plan"]["executable"]
    assert plan["execution_digest"] == reviewed["digest"]
    output = tmp_path / "snapshot-dispatch.json"
    with pytest.raises(RuntimeError, match="Replay failed"):
        replay.run_prepared(path, output, expected_revision=reviewed["revision"], expected_digest=reviewed["digest"], trusted=True)
    receipt = json.loads(output.read_text())
    assert receipt["submitted"] is True and receipt["operation_id"]
    assert not (extracted / ".git").exists()
