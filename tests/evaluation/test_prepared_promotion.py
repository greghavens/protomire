"""Real checkpoint archives and native-response doubles exercise the phase guard."""
import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
pytest.importorskip("zenml")
from modelbuilder.backend import sdk, promotion
from modelbuilder.backend.admission import BackendError
from modelbuilder.backend.common import write_json
from modelbuilder.backend.native_inputs import storage_fingerprint
from test_native_selection import native


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    values = [native(tmp_path, name, copies=1) for name in ("a", "b")]
    for run, request, _ in values:
        write_json(tmp_path / "jobs" / run.id / "request.json", request)
        write_json(tmp_path / "jobs" / run.id / "native.json", {"backend_run_id": run.id})
    runs = {value[0].id: value[0] for value in values}
    project = NS(id="native-project")
    version = NS(id="model-a", model=NS(id="model"), stage=None, run_metadata={})
    mutations = []
    def update(*args, **kwargs):
        mutations.append((args, kwargs))
        version.stage = kwargs["stage"]
        return version
    client = NS(get_pipeline_run=lambda rid, **kwargs: runs[rid], update_model_version=update)
    monkeypatch.setattr(sdk, "project_client", lambda _: (client, project))
    monkeypatch.setattr(sdk, "find_version", lambda *args: version)
    monkeypatch.setattr(sdk, "version_record", lambda value: {"model_version_id": value.id})
    monkeypatch.setattr(sdk, "log_metadata", lambda metadata, **kwargs: version.run_metadata.update(metadata))
    payload = {"project_id": "project", "model_version_id": "model-a", "candidate_run_ids": ["a", "b"],
               "metric": "rmse", "direction": "minimize", "stage": "staging"}
    handle = promotion.prepare(payload, tmp_path)
    return tmp_path, values, payload, handle, mutations


def test_prepared_native_commit_does_not_reread_checkpoint_bytes(prepared, monkeypatch):
    root, _, payload, handle, mutations = prepared
    def forbidden(*args, **kwargs):
        pytest.fail("Controlled promotion apply must not hash/materialize checkpoint bytes")
    monkeypatch.setattr("modelbuilder.backend.native_inputs._hash_regular_file", forbidden)
    monkeypatch.setattr("modelbuilder.backend.evaluation_binding.native_directory", forbidden)
    result = sdk.invoke("model.version.promote", {**payload, "_prepared_promotion": handle}, root)
    assert result["model_version_id"] == "model-a"
    assert result["selected_run_id"] == "a"
    assert len(mutations) == 1


@pytest.mark.parametrize("damage", ["checkpoint_same_size", "metric", "input", "request", "project", "payload", "handle", "expiry"])
def test_changed_preparation_cannot_mutate_native_version(prepared, monkeypatch, damage):
    root, values, payload, handle, mutations = prepared
    run, request, _ = values[0]
    step = run.steps["evaluation-0"]
    if damage == "checkpoint_same_size":
        path = Path(step.inputs["checkpoint"][0].uri) / "data.tar.gz"
        before = path.stat()
        with path.open("r+b") as stream:
            old = stream.read(1)
            stream.seek(0)
            stream.write(bytes([old[0] ^ 1]))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif damage == "metric":
        (Path(step.outputs["metrics"][0].uri) / "data.json").write_text("{}")
    elif damage == "input":
        step.inputs["checkpoint"][0].id = "replacement"
    elif damage == "request":
        request["recipe"]["revision"] += 1
        write_json(root / "jobs/a/request.json", request)
    elif damage == "project":
        run.project.id = "other"
    elif damage == "payload":
        payload["stage"] = "production"
    elif damage == "handle":
        handle["digest"] = "0" * 64
    else:
        now = promotion.time.time()
        monkeypatch.setattr(promotion.time, "time", lambda: now + promotion.MAX_PREPARATION_SECONDS + 1)
    with pytest.raises(BackendError):
        sdk.invoke("model.version.promote", {**payload, "_prepared_promotion": handle}, root)
    assert not mutations


def test_prepare_rejects_native_mutation_during_verification(prepared, monkeypatch):
    root, values, payload, _, mutations = prepared
    original = sdk.promotion_comparison
    def change_after_read(*args, **kwargs):
        result = original(*args, **kwargs)
        path = Path(values[0][0].steps["evaluation-0"].outputs["metrics"][0].uri) / "data.json"
        path.write_text("{}")
        return result
    monkeypatch.setattr(sdk, "promotion_comparison", change_after_read)
    with pytest.raises(BackendError, match="changed during"):
        promotion.prepare(payload, root)
    assert not mutations


def test_fingerprint_rejects_symlink_without_reading(prepared):
    root, values, _, _, _ = prepared
    artifact = values[0][0].steps["evaluation-0"].outputs["metrics"][0]
    path = Path(artifact.uri) / "data.json"
    path.unlink()
    path.symlink_to(root / "jobs/a/request.json")
    with pytest.raises(BackendError):
        storage_fingerprint(artifact, root)


def test_native_stage_apply_is_safe_to_retry_after_lost_reply(prepared, monkeypatch):
    root, _, payload, handle, mutations = prepared
    client, _ = sdk.project_client("project")
    original = client.update_model_version
    def lost_reply(*args, **kwargs):
        result = original(*args, **kwargs)
        if len(mutations) == 1:
            raise RuntimeError("reply lost after native stage update")
        return result
    client.update_model_version = lost_reply
    with pytest.raises(BackendError) as error:
        sdk.invoke("model.version.promote", {**payload, "_prepared_promotion": handle}, root)
    assert error.value.code == "promotion_reconciliation_required"
    result = sdk.invoke("model.version.promote", {**payload, "_prepared_promotion": handle}, root)
    assert result["model_version_id"] == "model-a"
    assert len(mutations) == 1  # observe identical native stage/selection; never repeat writes


def test_uncertain_native_state_is_not_blindly_retried(prepared, monkeypatch):
    root, _, payload, handle, mutations = prepared
    client, _ = sdk.project_client("project")
    def fail_before_stage(*args, **kwargs):
        raise RuntimeError("Native mutation outcome unavailable")
    client.update_model_version = fail_before_stage
    for _ in range(2):
        with pytest.raises(BackendError) as error:
            sdk.invoke("model.version.promote", {**payload, "_prepared_promotion": handle}, root)
        assert error.value.code == "promotion_reconciliation_required"
    assert not mutations


def test_oversized_preparation_is_rejected_before_writing_handle(prepared, monkeypatch):
    root, _, payload, _, mutations = prepared
    original = set((root / "promotion-evidence").iterdir())
    monkeypatch.setattr(promotion, "MAX_PREPARED_BYTES", 128)
    with pytest.raises(BackendError, match="serialized byte limit"):
        promotion.prepare(payload, root)
    assert set((root / "promotion-evidence").iterdir()) == original
    assert not mutations
