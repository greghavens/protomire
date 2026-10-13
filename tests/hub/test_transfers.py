"""Deterministic fake-provider transfers over tiny real files; no Hub/network/login."""
from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path
import threading
import time
from uuid import uuid4

import httpx
import pytest

from modelbuilder.api import create_app
from modelbuilder.auth import TokenAuth
from modelbuilder.hub import TERMINAL, HubTransfers, fingerprint, safe_name
from modelbuilder.hub_provider import HubCancelled
from modelbuilder.mcp_server import OperationGateway, catalog_tools
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService

OWNER = Actor("owner", {"read", "modify", "execute", "publish"})
ASSISTANT = Actor("assistant", {"read", "modify", "execute"})
COMMIT = "a" * 40
RESULT = "b" * 40
USAGE = {"purpose": "Deterministic synthetic fixture validation", "commercial": False, "deployment": "isolated test", "conditions": ["Tiny authored fixtures only; no external upload"]}
FILES = {"README.md": b"---\nlicense: mit\n---\nSynthetic test fixture. Permission to use these tiny authored test bytes for isolated tests is granted. No real model is included.\n", "weights.bin": b"tiny test bytes"}


def record(name, data):
    return {"path": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "git_blob_sha1": None}


class Provider:
    def __init__(self):
        self.files = dict(FILES)
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = None
        self.corrupt = False
        self.error_after_publish = False
        self.published = False
        self.parent = COMMIT
        self.gated = False
        self.private = True
        self.exists = True

    def validate_repository(self, repo_id, repo_type):
        assert repo_id and repo_type in {"model", "dataset"}

    def immutable_revision(self, revision):
        return isinstance(revision, str) and len(revision) == 40

    def capabilities(self):
        return {"id": "fake", "repo_types": ["model", "dataset"], "download": True, "publish": True}

    def call(self, action, p, *, cancelled=lambda: False):
        self.calls.append((action, deepcopy(p)))
        if self.block == action:
            self.entered.set()
            while not self.release.wait(.01):
                if cancelled():
                    raise HubCancelled()
        if action == "card.inspect":
            match = re.search(r"(?m)^license: (.+)$", p["card"])
            return {"license": match[1] if match else None}
        if action == "search":
            return {"items": [{"repo_id": "owner/model", "repo_type": p["repo_type"]}]}
        if action == "license.inspect":
            return {**self.call("inspect", p), "card_metadata": {"license": "mit"},
                    "documents": [{**record(k, v), "text": v.decode(), "source_url": "https://example.invalid/source/" + k}
                                  for k, v in self.files.items() if k in {"README.md", "LICENSE"}]}
        if action in {"inspect", "destination"}:
            return {"exists": self.exists, "repo_id": p["repo_id"], "repo_type": p["repo_type"], "commit": self.parent,
                    "private": self.private, "gated": self.gated, "license": "mit",
                    "access_state": "gated" if self.gated else "available", "files": [record(k, v) for k, v in self.files.items()]}
        if action == "download":
            output = []
            for item in p["files"]:
                assert p["commit"] == COMMIT
                path = Path(p["staging_dir"]) / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"bad" if self.corrupt else self.files[item["path"]])
                output.append({"path": item["path"], "local_path": str(path)})
            return {"files": output}
        if action == "publish":
            assert p["parent_commit"] == self.parent
            for item in p["files"]:
                assert fingerprint(Path(p["snapshot_dir"]) / item["path"], item["size_bytes"])["sha256"] == item["sha256"]
            self.published = True
            if self.error_after_publish:
                raise RuntimeError("sensitive upstream detail must never be returned")
            return {"verified": True, "commit": RESULT, "url": "https://huggingface.co/owner/model/tree/" + RESULT}
        if action == "reconcile":
            return {"verified": self.published, "commit": RESULT if self.published else None}
        raise AssertionError(action)


@pytest.fixture
def context(tmp_path):
    provider = Provider()
    service = OperationService(tmp_path / "state.sqlite", hub_providers={"fake": provider})
    project = service.invoke("project.create", {"slug": "hub", "model_name": "Hub model", "idempotency_key": "project"}, OWNER)
    pid = project["project_id"]
    root = tmp_path / "source"
    root.mkdir()
    for name, data in FILES.items():
        (root / name).write_bytes(data)
    config = {"accounts": {"publisher": {"keyring_service": "test", "username": "opaque", "project_ids": [pid]}},
              "publication_roots": {pid: {"output": str(root)}}}
    service.hub = HubTransfers(service, providers={"fake": provider}, config=config)
    yield service, provider, pid, root, config
    provider.release.set()
    service.hub.close()
    service.store.close()


def invoke(context, op, payload=None, actor=OWNER):
    service, _, pid, *_ = context
    from modelbuilder.models import OPERATIONS
    data = {"project_id": pid, **(payload or {})}
    if op == "recipe.get":
        data.setdefault("recipe_id", service._current_recipe(pid)["recipe_id"])
    if OPERATIONS[op].mutating:
        data.setdefault("idempotency_key", str(uuid4()))
    return service.invoke(op, data, actor)


def reviewed_policy(context, evidence=None, **extra):
    evidence = evidence or invoke(context, "hub.license.inspect", {"provider": "fake", "repo_id": "owner/model", "repo_type": "model"})
    return invoke(context, "hub.license.review", {"evidence_id": evidence["evidence_id"], "evidence_digest": evidence["evidence_digest"],
        "license_document_paths": [doc["path"] for doc in evidence["evidence"]["documents"]], "permitted_uses": ["download", "train", "evaluate", "generate", "export", "publish", "promote"],
        "intended_context": "Explicit deterministic fixture-only review; no legal interpretation", "reviewed_usage_context": USAGE,
        "required_files": [], "required_attributions": [], "allowed_destination_licenses": ["mit"],
        "gated_terms_acknowledged": True, "confirm_review": True, **extra})


def plan(context, **extra):
    payload = {"provider": "fake", "repo_id": "owner/model", "repo_type": "model", "files": list(FILES), "max_bytes": 1024, **extra}
    if "license_policy_id" not in payload:
        evidence = invoke(context, "hub.license.inspect", {key: payload[key] for key in ("provider", "repo_id", "repo_type")})
        payload["license_policy_id"] = reviewed_policy(context, evidence)["policy_id"]
    return invoke(context, "hub.download.plan", payload)


def start(context, planned=None):
    planned = planned or plan(context)
    return invoke(context, "hub.download.start", {"plan_id": planned["plan_id"], "plan_digest": planned["plan_digest"], "accept_license": True})


def finish(context, transfer):
    for _ in range(400):
        current = invoke(context, "hub.transfer.get", {"transfer_id": transfer["transfer_id"]})
        if current["state"] in TERMINAL:
            return current
        time.sleep(.01)
    raise AssertionError("Transfer failed to finish")


def preview(context, **extra):
    if "asset_id" not in extra and "source_asset_ids" not in extra:
        evidence = invoke(context, "hub.license.local.inspect", {"source_alias": "output", "repo_type": "model", "files": list(FILES), "max_bytes": 1024, "declared_license": "mit"})
        policy = reviewed_policy(context, evidence)
        asset = invoke(context, "hub.local.register", {"evidence_id": evidence["evidence_id"], "license_policy_id": policy["policy_id"]})
        extra["source_asset_ids"] = [asset["asset_id"]]
    return invoke(context, "hub.publish.preview", {"usage_context": USAGE, "provider": "fake", "repo_id": "owner/model", "repo_type": "model",
                  "account_alias": "publisher", "source_alias": "output", "files": list(FILES), "max_bytes": 1024,
                  "visibility": "private", "license": "mit", **extra})


def publish(context, value):
    approval = invoke(context, "hub.publish.approve", {"preview_id": value["preview_id"], "payload_digest": value["payload_digest"], "confirm_publication": True})
    return invoke(context, "hub.publish.start", {"preview_id": value["preview_id"], "payload_digest": value["payload_digest"], "approval_id": approval["approval_id"]})


def test_verified_download_is_pinned_registered_after_completion_and_optional(context):
    service, provider, pid, _, _ = context
    original = invoke(context, "recipe.get")
    provider.block = "download"
    planned = plan(context)
    running = start(context, planned)
    assert provider.entered.wait(1)
    assert invoke(context, "hub.asset.list")["assets"] == []
    assert invoke(context, "recipe.get") == original
    provider.parent = "c" * 40
    provider.release.set()
    done = finish(context, running)
    assert done["state"] == "succeeded"
    asset = invoke(context, "hub.asset.get", {"asset_id": done["asset_id"]})
    assert asset["commit"] == COMMIT and len(asset["files"]) == 2
    assert not list((service.hub.root / "assets" / done["asset_id"]).rglob(".cache"))
    assert all(not path.is_symlink() for path in (service.hub.root / "assets" / done["asset_id"]).rglob("*"))
    bound = invoke(context, "hub.asset.bind", {"asset_id": done["asset_id"], "expected_revision": original["revision"]})
    assert bound["recipe_id"] == original["recipe_id"] and bound["nodes"] == original["nodes"]
    assert bound["resources"] == asset["resources"]
    assert "publisher" not in json.dumps(asset["resources"])
    assert len(service.store.list("recipe", pid)) == 1


def test_download_hash_failure_and_retry(context):
    _, provider, *_ = context
    provider.corrupt = True
    transfer = start(context)
    failed = finish(context, transfer)
    assert failed["state"] == "failed" and failed["error"]["code"] == "hub_hash_mismatch"
    assert not invoke(context, "hub.asset.list")["assets"]
    provider.corrupt = False
    retried = invoke(context, "hub.transfer.retry", {"transfer_id": transfer["transfer_id"]})
    assert finish(context, retried)["state"] == "succeeded"


def test_cancel_download_is_durable_and_retry_safe(context):
    _, provider, *_ = context
    provider.block = "download"
    transfer = start(context)
    assert provider.entered.wait(1)
    requested = invoke(context, "hub.transfer.cancel", {"transfer_id": transfer["transfer_id"]})
    assert requested["cancel_requested"]
    assert finish(context, transfer)["state"] == "cancelled"
    provider.release.set()
    assert finish(context, invoke(context, "hub.transfer.retry", {"transfer_id": transfer["transfer_id"]}))["state"] == "succeeded"


def test_budget_gated_unknown_file_and_digest_fail_before_download(context):
    _, provider, *_ = context
    with pytest.raises(ServiceError, match="byte budget"):
        plan(context, max_bytes=1)
    provider.gated = True
    with pytest.raises(ServiceError, match="access"):
        plan(context)
    provider.gated = False
    with pytest.raises(ServiceError, match="absent"):
        plan(context, files=["missing"])
    planned = plan(context)
    with pytest.raises(ServiceError, match="digest"):
        invoke(context, "hub.download.start", {"plan_id": planned["plan_id"], "plan_digest": "f" * 64, "accept_license": True})
    assert all(action != "download" for action, _ in provider.calls)


@pytest.mark.parametrize("name", ["../escape", "/abs", "a\\b", "C:/x", "a/../b", "a//b", "NUL", "name.", ".git/config", ".cache/token", "a\nfile"])
def test_unsafe_paths(name):
    with pytest.raises(ServiceError):
        safe_name(name)


def test_publication_preview_exact_payload_publisher_only_and_one_use(context):
    service, provider, pid, root, _ = context
    value = preview(context)
    assert not provider.published
    assert value["payload"]["visibility"] == "private" and value["payload"]["deletes"] == []
    assert value["payload"]["parent_commit"] == COMMIT
    assert all(item["effect"] == "unchanged" for item in value["payload"]["change_effects"])
    with pytest.raises(ServiceError) as error:
        invoke(context, "hub.publish.approve", {"preview_id": value["preview_id"], "payload_digest": value["payload_digest"], "confirm_publication": True}, ASSISTANT)
    assert error.value.code == "forbidden"
    approval = invoke(context, "hub.publish.approve", {"preview_id": value["preview_id"], "payload_digest": value["payload_digest"], "confirm_publication": True})
    (root / "weights.bin").write_bytes(b"local source changed after preview")
    payload = {"preview_id": value["preview_id"], "payload_digest": value["payload_digest"], "approval_id": approval["approval_id"], "idempotency_key": "one-publication"}
    done = finish(context, invoke(context, "hub.publish.start", payload))
    assert done["state"] == "succeeded" and done["result"]["commit"] == RESULT
    assert invoke(context, "hub.publish.start", payload)["state"] == "succeeded"
    assert len([action for action, _ in provider.calls if action == "publish"]) == 1
    with pytest.raises(ServiceError, match="expired, used"):
        invoke(context, "hub.publish.start", {**payload, "idempotency_key": "different"})


def test_tampered_snapshot_never_transmits(context):
    service, provider, *_ = context
    value = preview(context)
    (service.hub.root / "publications" / value["preview_id"] / "weights.bin").write_bytes(b"oops")
    result = finish(context, publish(context, value))
    assert result["state"] == "failed" and not provider.published


def test_uncertain_upload_only_reconciles_no_blind_retry(context):
    _, provider, *_ = context
    provider.error_after_publish = True
    result = finish(context, publish(context, preview(context)))
    assert result["state"] == "needs_reconciliation"
    assert "sensitive upstream" not in json.dumps(result)
    with pytest.raises(ServiceError, match="uncertain publication"):
        invoke(context, "hub.transfer.retry", {"transfer_id": result["transfer_id"]})
    reconciled = invoke(context, "hub.publish.reconcile", {"transfer_id": result["transfer_id"]})
    assert reconciled["state"] == "succeeded" and reconciled["result"]["commit"] == RESULT


def test_publish_cancel_after_dispatch_is_uncertain(context):
    _, provider, *_ = context
    provider.block = "publish"
    transfer = publish(context, preview(context))
    assert provider.entered.wait(1)
    invoke(context, "hub.transfer.cancel", {"transfer_id": transfer["transfer_id"]})
    assert finish(context, transfer)["state"] == "needs_reconciliation"


def test_license_visibility_symlinks_and_project_isolation(context, tmp_path):
    _, _, _, root, _ = context
    with pytest.raises(ServiceError, match="visibility"):
        preview(context, visibility="public")
    with pytest.raises(ServiceError, match="license"):
        preview(context, license="apache-2.0")
    (root / "link").symlink_to(root / "weights.bin")
    with pytest.raises((ServiceError, OSError)):
        preview(context, files=["README.md", "link"])
    project = context[0].invoke("project.create", {"slug": "other", "model_name": "Other", "idempotency_key": "other"}, OWNER)
    with pytest.raises(ServiceError, match="alias"):
        invoke(context, "hub.publish.preview", {"project_id": project["project_id"], "usage_context": USAGE, "provider": "fake", "repo_id": "owner/model", "repo_type": "model",
                  "account_alias": "publisher", "source_alias": "output", "files": list(FILES), "max_bytes": 1024, "visibility": "private", "license": "mit"})


def test_dataset_asset_registers_safe_root_and_survives_restart(context):
    service, provider, pid, _, config = context
    provider.files = {"rows.jsonl": b'{"id":"one","text":"hello"}\n', "LICENSE": b"Synthetic authored test fixture license.\n"}
    planned = plan(context, repo_type="dataset", files=["rows.jsonl"])
    result = finish(context, start(context, planned))
    asset = invoke(context, "hub.asset.get", {"asset_id": result["asset_id"]})
    manifest = {"name": "tiny", "benchmark": "tiny", "license": "mit", "provenance": {"source": "test", "source_revision": COMMIT},
                "sources": [{"id": "rows", "root_id": asset["dataset_root_alias"], "path": "rows.jsonl", "format": "jsonl", "sha256": next(item["sha256"] for item in asset["files"] if item["path"] == "rows.jsonl")}],
                "id_column": "id", "row_schema": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}}},
                "splits": [{"id": "train", "role": "train", "source_ids": ["rows"]}]}
    registered = invoke(context, "dataset.register", {"manifest": manifest})
    assert registered["row_count"] == 1
    restarted = OperationService(service.store.path, hub_config=config, hub_providers={"fake": provider})
    try:
        assert restarted.invoke("dataset.validate", {"project_id": pid, "dataset_id": registered["dataset_id"]}, OWNER)["valid"]
    finally:
        restarted.store.close()


def test_revocation_during_metadata_or_download_stops_registration(context):
    service, provider, pid, *_ = context
    authority = {"allowed": True}
    service.operation_authorizer = lambda *args: authority["allowed"]
    provider.block = "download"
    transfer = start(context)
    assert provider.entered.wait(1)
    authority["allowed"] = False
    time.sleep(.08)
    provider.release.set()
    service.hub._threads[transfer["transfer_id"]].join(1)
    authority["allowed"] = True
    assert finish(context, transfer)["state"] == "cancelled"
    assert service.store.list("hub_asset", pid) == []


def test_interrupted_download_recovery_requires_explicit_retry(context):
    service, _, pid, *_ = context
    planned = plan(context)
    row = {"project_id": pid, "actor_id": OWNER.id, "revision": 1, "transfer_id": str(uuid4()), "kind": "download", "state": "running", "phase": "downloading",
           "plan_id": planned["plan_id"], "attempt": 1, "verified_bytes": 0, "total_bytes": planned["payload"]["total_bytes"], "completed_files": [], "cancel_requested": False}
    service.store.put("hub_transfer", row["transfer_id"], pid, row)
    assert invoke(context, "hub.transfer.get", {"transfer_id": row["transfer_id"]})["state"] == "interrupted"
    assert finish(context, invoke(context, "hub.transfer.retry", {"transfer_id": row["transfer_id"]}))["state"] == "succeeded"


@pytest.mark.asyncio
async def test_http_mcp_catalog_share_real_operations_and_denial(context):
    service, _, pid, *_ = context
    auth = TokenAuth({"test-owner": OWNER, "test-assistant": ASSISTANT})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service, auth)), base_url="http://localhost", headers={"Authorization": "Bearer test-assistant"}) as client:
        gateway = OperationGateway(client)
        catalog = await gateway.catalog()
        tools, aliases = catalog_tools(catalog)
        assert "hub_download_plan" in aliases and "hub_publish_approve" in aliases
        assert next(tool for tool in tools if tool.name == "hub_publish_approve").inputSchema["properties"]["payload_digest"]
        result = await gateway.invoke("hub.search", {"project_id": pid, "provider": "fake", "repo_type": "model", "query": "tiny"})
        assert result["items"][0]["repo_id"] == "owner/model"
        denied = await client.post("/v1/operations/hub.publish.approve", json={"project_id": pid, "preview_id": "unknown", "payload_digest": "a" * 64, "confirm_publication": True, "idempotency_key": "x"})
        assert denied.status_code == 403
        invalid = await client.post("/v1/operations/hub.search", json={"project_id": pid, "provider": "fake", "repo_type": "model", "query": "tiny", "token": "must-never-be-accepted"})
        assert invalid.status_code == 400


def test_repository_comparison_and_portable_manifest_receipt_exclude_accounts(context):
    _, provider, _, root, _ = context
    (root / "weights.bin").write_bytes(b"changed local weights")
    (root / "extra.parquet").write_bytes(b"tiny inert parquet placeholder")
    provider.files["remote-only.txt"] = b"remote"
    compared = invoke(context, "hub.repository.compare", {"provider": "fake", "repo_id": "owner/model", "repo_type": "model", "source_alias": "output", "max_bytes": 1024})
    assert {item["path"]: item["state"] for item in compared["changes"]} == {"README.md": "unchanged", "weights.bin": "modified", "extra.parquet": "added", "remote-only.txt": "remote_only"}
    assert compared["commit"] == COMMIT and not compared["deletion_supported"]
    downloaded = finish(context, start(context))
    manifest = invoke(context, "hub.asset.manifest", {"asset_id": downloaded["asset_id"]})
    assert manifest["schema_version"] == "modelbuilder.hub-asset/v1"
    published = finish(context, publish(context, preview(context)))
    receipt = invoke(context, "hub.publish.receipt", {"transfer_id": published["transfer_id"]})
    assert receipt["commit"] == RESULT and receipt["verified"]
    assert "account_alias" not in json.dumps([manifest, receipt]) and "snapshot_dir" not in json.dumps(receipt)


def test_approval_wrong_actor_digest_and_expiry_cannot_dispatch(context):
    service, provider, pid, *_ = context
    value = preview(context)
    with pytest.raises(ServiceError, match="digest"):
        invoke(context, "hub.publish.approve", {"preview_id": value["preview_id"], "payload_digest": "f" * 64, "confirm_publication": True})
    approval = invoke(context, "hub.publish.approve", {"preview_id": value["preview_id"], "payload_digest": value["payload_digest"], "confirm_publication": True})
    p = {"preview_id": value["preview_id"], "payload_digest": value["payload_digest"], "approval_id": approval["approval_id"]}
    with pytest.raises(ServiceError, match="another actor"):
        invoke(context, "hub.publish.start", p, Actor("different", OWNER.scopes))
    with service.store.transaction():
        service.hub._put("hub_approval", approval["approval_id"], approval, expires_at="2000-01-01T00:00:00+00:00")
    with pytest.raises(ServiceError, match="expired"):
        invoke(context, "hub.publish.start", p)
    assert not provider.published


def test_second_service_does_not_misclassify_live_download_as_interrupted(context):
    service, provider, _, _, config = context
    provider.block = "download"
    transfer = start(context)
    assert provider.entered.wait(1)
    second = OperationService(service.store.path, hub_config=config, hub_providers={"fake": provider})
    try:
        current = second.invoke("hub.transfer.get", {"project_id": context[2], "transfer_id": transfer["transfer_id"]}, OWNER)
        assert current["state"] == "running"
    finally:
        second.store.close()
    provider.release.set()
    assert finish(context, transfer)["state"] == "succeeded"


def test_source_symlink_ancestor_and_oversize_never_publish(context, tmp_path):
    service, provider, _, root, _ = context
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not approved")
    (root / "nested").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ServiceError):
        preview(context, files=["README.md", "nested/secret.txt"])
    with pytest.raises(ServiceError):
        preview(context, max_bytes=1)
    assert not provider.published


def test_resumes_completed_verified_files_without_redownloading(context):
    _, provider, *_ = context
    original = provider.call
    count = [0]

    def fail_second(action, p, **kwargs):
        if action == "download":
            count[0] += 1
            if count[0] == 2:
                raise RuntimeError("transient")
        return original(action, p, **kwargs)
    provider.call = fail_second
    transfer = start(context)
    failed = finish(context, transfer)
    assert failed["state"] == "failed" and len(failed["completed_files"]) == 1
    provider.call = original
    before = len([1 for action, _ in provider.calls if action == "download"])
    completed = finish(context, invoke(context, "hub.transfer.retry", {"transfer_id": transfer["transfer_id"]}))
    assert completed["state"] == "succeeded"
    assert len([1 for action, _ in provider.calls if action == "download"]) - before == 1


def test_staged_publication_rejects_changed_revision_before_approval(context):
    _, provider, *_ = context
    with pytest.raises(ServiceError, match="changed after staging"):
        preview(context, expected_remote_commit="d" * 40)
    assert not provider.published


def test_existing_remote_card_allows_staging_only_training_file(context):
    value = preview(context, files=["weights.bin"])
    assert value["license_source"] == "existing_remote_card" and value["card_preview"] == ""
    assert [item["path"] for item in value["payload"]["files"]] == ["weights.bin"]
    assert finish(context, publish(context, value))["state"] == "succeeded"


def test_new_repo_requires_explicit_creation_and_card_license(context):
    _, provider, *_ = context
    provider.exists = False
    with pytest.raises(ServiceError, match="explicit new-repository"):
        preview(context)
    with pytest.raises(ServiceError, match="README.md"):
        preview(context, create_repo=True, files=["weights.bin"])
    result = preview(context, create_repo=True)
    assert result["payload"]["create_repo"] and not provider.published


def test_git_blob_only_provider_hash_is_verified_and_asset_gets_sha256(context):
    _, provider, *_ = context
    original = provider.call

    def git_files(action, p, **kwargs):
        result = original(action, p, **kwargs)
        if action == "inspect":
            for item in result["files"]:
                data = provider.files[item["path"]]
                item["sha256"] = None
                item["git_blob_sha1"] = hashlib.sha1(f"blob {len(data)}\0".encode() + data, usedforsecurity=False).hexdigest()
        return result
    provider.call = git_files
    result = finish(context, start(context))
    assert result["state"] == "succeeded"
    assert all(len(item["sha256"]) == 64 for item in invoke(context, "hub.asset.get", {"asset_id": result["asset_id"]})["files"])


def test_custom_provider_has_explicit_non_huggingface_repository_identity(context):
    _, provider, *_ = context
    original = provider.call
    provider.immutable_revision = lambda value: value == "generation:7345"

    def custom(action, p, **kwargs):
        result = original(action, p, **kwargs)
        if action == "inspect":
            result["commit"] = "generation:7345"
        return result
    provider.call = custom
    inspected = invoke(context, "hub.inspect", {"provider": "fake", "repo_type": "model", "repo_id": "org.collection.model"})
    assert inspected["commit"] == "generation:7345"
    planned = plan(context, repo_id="org.collection.model")
    assert planned["payload"]["commit"] == "generation:7345"


def test_portable_manifest_matches_canonical_schema(context):
    from jsonschema import Draft202012Validator
    manifest = invoke(context, "hub.asset.manifest", {"asset_id": finish(context, start(context))["asset_id"]})
    schema = json.loads((Path(__file__).parents[2] / "schemas" / "hub-asset.v1.schema.json").read_text())
    Draft202012Validator(schema).validate(manifest)


def test_failed_or_discarded_publication_snapshot_is_removed(context):
    service, _, *_ = context
    for _ in range(3):
        with pytest.raises(ServiceError):
            preview(context, license="apache-2.0")
    assert list((service.hub.root / "publications").iterdir()) == []
    value = preview(context)
    invoke(context, "hub.publish.discard", {"preview_id": value["preview_id"]})
    assert not (service.hub.root / "publications" / value["preview_id"]).exists()
    with pytest.raises(ServiceError, match="expired or was discarded"):
        publish(context, value)


def test_sdk_attempt_directory_is_exclusive_private_and_not_reused(context):
    _, provider, *_ = context
    original = provider.call
    attempts = []

    def observed(action, p, **kwargs):
        if action == "download":
            root = Path(p["staging_dir"])
            assert not list(root.iterdir())
            assert root.stat().st_mode & 0o777 == 0o700
            attempts.append(root)
        return original(action, p, **kwargs)
    provider.call = observed
    assert finish(context, start(context))["state"] == "succeeded"
    assert len(set(attempts)) == 2 and all(not path.exists() for path in attempts)
