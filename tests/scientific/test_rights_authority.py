"""Real authority-store and package-byte checks; no provider or model execution."""
from copy import deepcopy
import hashlib
import json
import uuid
import pytest
from modelbuilder.backend.adapter import ZenMLBackend
from modelbuilder.backend.admission import BackendError
from modelbuilder.backend.common import write_json
from modelbuilder.backend.execution_rights import artifact_authorization, verify_artifact_authorization
from modelbuilder.evaluation import digest
from modelbuilder.export_packages import inspect_export, package_export
from modelbuilder.store import SQLiteStore
from .license_fixture import RIGHTS, USAGE, policy


def authority(tmp_path):
    store = SQLiteStore(tmp_path / "service.sqlite")
    review = policy("project")
    store.put("hub_license_policy", review["policy_id"], "project", review)
    backend = ZenMLBackend(tmp_path).with_execution_rights(project_id="project", policies={review["policy_id"]: review}, policy_store_path=store.path)
    return store, review, backend


def test_adapter_private_rights_context_cannot_be_supplied_by_operation_caller(tmp_path, monkeypatch):
    store, review, backend = authority(tmp_path)
    calls = []
    monkeypatch.setattr(backend, "_rpc", lambda op, payload: calls.append((op, payload)) or {})
    payload = {"project_id": "project", "_rights_usage_context": USAGE}
    backend.prepare_promotion(payload)
    assert calls[0][1]["_execution_rights_authorization"]["policies"] == {review["policy_id"]: review}
    with pytest.raises(BackendError, match="internal backend"):
        backend.invoke("model.version.promote", {**payload, "_execution_rights_authorization": calls[0][1]["_execution_rights_authorization"]})
    store.close()


@pytest.mark.parametrize("damage", ["missing", "unresolved", "wrong_context", "unreviewed", "changed_evidence"])
def test_no_actual_artifact_use_without_exact_current_review(tmp_path, damage):
    store, review, backend = authority(tmp_path)
    rights, usage = deepcopy(RIGHTS), deepcopy(USAGE)
    if damage == "missing":
        rights = None
    elif damage == "unresolved":
        rights["unresolved"] = [{"artifact_id": "other-parent", "reason": "Unknown rights"}]
    elif damage == "wrong_context":
        usage["commercial"] = True
    elif damage == "unreviewed":
        rights["sources"][0]["license_policy_id"] = "caller-invented"
    else:
        rights["sources"][0]["license_evidence_digest"] = "f" * 64
    with pytest.raises(BackendError):
        verify_artifact_authorization(artifact_authorization(backend, "project", usage), rights, "promote")
    store.close()


def test_export_chunk_rechecks_current_authority_and_bound_review_before_bytes(tmp_path):
    store, review, backend = authority(tmp_path)
    job = backend.root / "jobs" / str(uuid.uuid4())
    write_json(job / "request.json", {"project_id": "project", "recipe": {"extensions": {"usage_context": USAGE}}})
    write_json(job / "native.json", {"backend_run_id": "run", "model_version_ref": "version"})
    root = tmp_path / "artifact"
    root.mkdir()
    (root / "loader.py").write_text("raise AssertionError('never execute loader during packaging')")
    (root / "weights.bin").write_bytes(b"independent synthetic weights")
    manifest = {"schema_version": "modelbuilder.export/v1", "selected_checkpoint": "chosen", "rights": RIGHTS,
                "loader": {"path": "loader.py", "entrypoint": "load"}, "signature": {"input": "tensor"}, "dependencies": {"python": "3.12"}, "source": {"commit": "fixture"},
                "verification": {"passed": True, "atol": 0.0}, "assets": [{"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()} for name in ("loader.py", "weights.bin")]}
    (root / "export.manifest.json").write_text(json.dumps(manifest))
    provenance = {"project_id": "project", "backend_run_id": "run", "model_version_id": "version", "artifact_id": "export", "rights": RIGHTS, "rights_usage_context": USAGE}
    descriptor = inspect_export(root, provenance)
    receipt = artifact_authorization(backend, "project", USAGE)
    admission = verify_artifact_authorization(receipt, RIGHTS, "export")["admission"]
    package = package_export(root, backend.root / "export-packages", descriptor, descriptor["export_digest"], rights_admission=admission)
    payload = {**{key: provenance[key] for key in ("project_id", "backend_run_id", "model_version_id", "artifact_id")}, "package_id": package["package_id"], "chunk_index": 0}
    assert backend.invoke("model.export.read", payload)["size_bytes"] > 0
    changed = {**review, "revision": 2, "status": "revoked"}
    changed["review_digest"] = digest({key: value for key, value in changed.items() if key != "review_digest"})
    store.put("hub_license_policy", review["policy_id"], "project", changed, 1)
    with pytest.raises(BackendError) as error:
        backend.invoke("model.export.read", payload)
    assert error.value.code == "license_policy_changed"
    store.close()
