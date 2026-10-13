"""Builtin live snapshots preserve exact weight identity and reviewed parent evidence."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from modelbuilder.backend.admission import BackendError
from modelbuilder.backend.components import read_checkpoint
from modelbuilder.backend.execution_rights import verify_dispatch
from modelbuilder.backend.fixture_artifacts import checkpoint_manifest
from modelbuilder.backend.live_evaluation import _snapshot, verify_snapshot
from modelbuilder.hub_rights import digest
from modelbuilder.store import SQLiteStore
from tests.components.rights_test_support import USAGE_CONTEXT, authorize_test_recipe, local_review


def fixture(tmp_path):
    root = tmp_path / "mutable"
    root.mkdir()
    files = []
    for name, content in (("checkpoint.pt", b"authored last fixture state"), ("best.pt", b"authored best fixture state")):
        (root / name).write_bytes(content)
        files.append({"path": name, "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)})
    policy, rights = local_review(source_files=files)
    state = {"step": 2, "fixture": "custom", "width": 4, "seed": 1, "dataset_sha256": "a" * 64}
    descriptor = checkpoint_manifest(root, state, rights)
    node = {"id": "evaluate", "kind": "model.evaluate", "inputs": [{"name": "checkpoint", "type": "Path"}]}
    source = {"files": []}
    recipe = {"source": source, "nodes": [node], "resources": [], "extensions": {"usage_context": USAGE_CONTEXT}}
    spec = {"source": source, "component_node": node, "dependencies": [],
            **authorize_test_recipe(tmp_path, recipe, {policy["policy_id"]: policy})}
    return root, descriptor, policy, spec


@pytest.mark.parametrize("purpose", ["last", "best"])
def test_fixture_snapshot_keeps_complete_manifest_and_selected_weight_sha(tmp_path, purpose):
    root, descriptor, policy, spec = fixture(tmp_path)
    published = _snapshot(tmp_path / "job", "train", root, 2, purpose, "fixture")
    snapshot = verify_snapshot(published)
    assert read_checkpoint(snapshot) == descriptor
    assert {asset["path"] for asset in published["assets"]} == {"checkpoint.pt", "best.pt", "checkpoint.manifest.json"}
    assert published["checkpoint_digest"] == descriptor["selections"][purpose]["sha256"]
    assert published["purpose"] == purpose and published["step"] == 2
    admission = verify_dispatch(spec, {"checkpoint": snapshot})
    assert admission["policy_revisions"][policy["policy_id"]]["revision"] == 1
    # Producer mutation after publication cannot rewrite the queued snapshot.
    (root / "checkpoint.manifest.json").write_text("{}")
    assert read_checkpoint(verify_snapshot(published))["rights"] == descriptor["rights"]


@pytest.mark.parametrize("change", ["missing_manifest", "asset_tamper", "wrong_step", "wrong_selection"])
def test_fixture_snapshot_rejects_missing_or_inconsistent_producer_contract(tmp_path, change):
    root, descriptor, _, _ = fixture(tmp_path)
    step = 2
    if change == "missing_manifest":
        (root / "checkpoint.manifest.json").unlink()
    elif change == "asset_tamper":
        (root / "best.pt").write_bytes(b"changed weight bytes")
    elif change == "wrong_step":
        step = 3
    else:
        descriptor["selections"]["last"]["sha256"] = "f" * 64
        (root / "checkpoint.manifest.json").write_text(json.dumps(descriptor))
    with pytest.raises((ValueError, FileNotFoundError)):
        _snapshot(tmp_path / "job", "train", root, step, "last", "fixture")
    assert not (tmp_path / "job/live_evaluation/checkpoints").exists()


@pytest.mark.parametrize("change", ["missing_rights", "unresolved", "revoked"])
def test_fixture_snapshot_does_not_authorize_absent_or_revoked_lineage(tmp_path, change):
    root, descriptor, policy, spec = fixture(tmp_path)
    if change == "missing_rights":
        descriptor.pop("rights")
    elif change == "unresolved":
        descriptor["rights"]["unresolved"] = [{"artifact_id": "unreviewed-parent", "reason": "Missing evidence"}]
    (root / "checkpoint.manifest.json").write_text(json.dumps(descriptor))
    published = _snapshot(tmp_path / "job", "train", root, 2, "last", "fixture")
    if change == "revoked":
        policy = deepcopy(policy)
        policy.update(status="revoked", revision=2)
        policy["review_digest"] = digest({key: value for key, value in policy.items() if key != "review_digest"})
        store = SQLiteStore(Path(spec["rights_authorization"]["policy_store_guard"]["path"]))
        try:
            store.put("hub_license_policy", policy["policy_id"], policy["project_id"], policy, expected_revision=1)
        finally:
            store.close()
    with pytest.raises(BackendError) as rejected:
        verify_dispatch(spec, {"checkpoint": verify_snapshot(published)})
    assert rejected.value.code == ("license_policy_changed" if change == "revoked" else "license_rights_unreviewed")


def test_snapshot_manifest_tamper_is_detected_even_when_weights_match(tmp_path):
    root, descriptor, _, _ = fixture(tmp_path)
    published = _snapshot(tmp_path / "job", "train", root, 2, "last", "fixture")
    manifest = Path(published["path"]) / "checkpoint.manifest.json"
    manifest.chmod(0o644)
    descriptor["rights"]["sources"][0]["license_policy_id"] = "invented-review"
    manifest.write_text(json.dumps(descriptor))
    with pytest.raises(ValueError, match="integrity mismatch"):
        verify_snapshot(published)
