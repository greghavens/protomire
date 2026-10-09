"""Policy tests and real official-SDK offline acceptance; no credentials needed."""
import json
import os
import subprocess
from uuid import uuid4

import pytest

from modelbuilder.tracking import (
    TrackingConfig, TrackingPolicyError, disclosure, mirror_tracking,
    resolve_tracking, selected_metrics, tracking_environment,
)


def config(**changes):
    return {"mode": "offline", "entity": "offline-test", "project": "modelbuilder-tests",
            "metric_names": ["validation_loss", "accuracy"], **changes}


def test_disabled_never_starts_sdk(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled tracking must not start an SDK process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert mirror_tracking(None, operation_id="unused", backend_run_id="unused", metrics={},
                           directory=tmp_path)["status"] == "disabled"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("change", [
    {"api_key": "never-save-this"}, {"credential_env": "GITHUB_TOKEN"}, {"mode": "auto"},
    {"entity": None}, {"project": "../../../escape"}, {"artifact_policy": "all"},
    {"metric_names": ["password"]}, {"metric_names": ["loss", "loss"]},
    {"metric_names": ["metric"] * 65}, {"source": True},
])
def test_bounded_config_rejects_secrets_and_expansion(change):
    with pytest.raises(TrackingPolicyError) as exc:
        resolve_tracking(config(**change))
    assert "never-save-this" not in str(exc.value)


def test_online_requires_per_run_consent_and_override_replaces_defaults():
    with pytest.raises(TrackingPolicyError, match="consent"):
        resolve_tracking(config(mode="online"))
    assert resolve_tracking(config(mode="online"), online_consent=True)["mode"] == "online"
    assert resolve_tracking(config(mode="online"), {"mode": "disabled"})["entity"] is None
    assert disclosure(resolve_tracking(config()))["uploads_checkpoints"] is False


def test_selected_scalars_only():
    c = TrackingConfig(**config(metric_names=["loss", "accuracy", "flag", "infinite", "vector", "huge"]))
    assert selected_metrics(c, {"loss": 0.5, "accuracy": 1, "flag": True, "infinite": float("inf"),
                                "vector": [1], "huge": 10**1000, "password": "secret", "source": "code"}) == {"loss": 0.5, "accuracy": 1}


def test_environment_drops_overrides_credentials_and_login_state(tmp_path):
    src = {"WANDB_MODE": "online", "WANDB_API_KEY": "unapproved", "GITHUB_TOKEN": "secret",
           "WANDB_BASE_URL": "https://evil.invalid", "PYTHONPATH": "/untrusted", "HOME": "/old-login",
           "WANDB_SWEEP_ID": "sweep", "MODELBUILDER_WANDB_API_KEY": "approved-wandb-only", "PATH": "/bin"}
    offline = tracking_environment(TrackingConfig(**config()), tmp_path, src)
    assert not set(src).intersection(offline) - {"PATH", "HOME", "WANDB_BASE_URL", "WANDB_MODE"}
    assert offline["WANDB_MODE"] == "offline"
    assert offline["WANDB_BASE_URL"] == "https://api.wandb.ai"
    online = tracking_environment(TrackingConfig(**config(mode="online")), tmp_path, src)
    assert online["WANDB_API_KEY"] == "approved-wandb-only"
    assert "GITHUB_TOKEN" not in online


def test_missing_credential_is_safe_and_no_launch(monkeypatch, tmp_path):
    monkeypatch.delenv("MODELBUILDER_WANDB_API_KEY", raising=False)
    result = mirror_tracking(config(mode="online"), operation_id=str(uuid4()), backend_run_id=str(uuid4()),
                             metrics={}, directory=tmp_path, online_consent=True)
    assert result["error_code"] == "credential_unavailable"
    assert result["url"] is None


def test_process_error_has_no_exception_or_credentials(tmp_path):
    result = mirror_tracking(config(), operation_id=str(uuid4()), backend_run_id=str(uuid4()),
                             metrics={}, directory=tmp_path, python_executable="/absent/no-secret")
    assert result["status"] == "failed"
    assert "no-secret" not in json.dumps(result)


@pytest.mark.integration
@pytest.mark.parametrize("artifact_policy", ["none", "metrics_snapshot"])
def test_real_sdk_offline(tmp_path, artifact_policy, monkeypatch):
    python = os.environ.get("MODELBUILDER_TRACKING_TEST_PYTHON")
    if not python:
        pytest.skip("Set MODELBUILDER_TRACKING_TEST_PYTHON to the optional pinned W&B interpreter")
    # Even hostile inherited W&B settings cannot turn this into an online run.
    monkeypatch.setenv("WANDB_MODE", "online")
    monkeypatch.setenv("WANDB_API_KEY", "SENTINEL-NEVER-FORWARD")
    monkeypatch.setenv("WANDB_BASE_URL", "https://example.invalid")
    result = mirror_tracking(config(artifact_policy=artifact_policy), operation_id=str(uuid4()),
                             backend_run_id=str(uuid4()), metrics={"validation_loss": 0.25, "accuracy": 0.75,
                                "dataset": "SENTINEL-PRIVATE-DATA", "checkpoint": "SENTINEL-CHECKPOINT"},
                             directory=tmp_path, python_executable=python)
    assert result["status"] == "offline", result
    assert result["url"] is None
    records = list(tmp_path.rglob("*.wandb"))
    assert records, "Must produce actual SDK run data, not fake tracking acceptance"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert b"SENTINEL" not in data, path
    assert not list(tmp_path.rglob("wandb-metadata.json"))
    assert not list(tmp_path.rglob("requirements.txt"))
    assert not list(tmp_path.rglob("*.log"))
    assert not list(tmp_path.rglob("output.log"))
    payload = json.loads((tmp_path / "mirror-input.json").read_text())
    assert payload["metrics"] == {"validation_loss": 0.25, "accuracy": 0.75}
    assert (tmp_path / "selected-metrics.json").exists() == (artifact_policy == "metrics_snapshot")
    # Decode the real SDK transaction stream, asserting actual metrics/artifacts.
    script = '''
import json, sys
from wandb.sdk.internal.datastore import DataStore
from wandb.proto.wandb_internal_pb2 import Record
s = DataStore(); s.open_for_scan(sys.argv[1]); kinds=[]; metrics=[]
while True:
    raw=s.scan_data()
    if raw is None: break
    r=Record(); r.ParseFromString(raw); kinds.append(r.WhichOneof("record_type"))
    if r.HasField("history"):
        metrics.extend(i.key or '.'.join(i.nested_key) for i in r.history.item)
print(json.dumps({'kinds':kinds,'metrics':metrics}))
'''
    decoded = json.loads(subprocess.check_output([python, "-c", script, str(records[0])], text=True))
    assert {"validation_loss", "accuracy"}.issubset(decoded["metrics"])
    assert "stats" not in decoded["kinds"]
    assert ("artifact" in decoded["kinds"]) == (artifact_policy == "metrics_snapshot")


def test_timeout_kills_detached_sdk_descendant(monkeypatch, tmp_path):
    import sys
    import time
    import psutil

    child_file = tmp_path / "descendant.pid"
    script = ("import subprocess,sys,time; "
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],start_new_session=True); "
              f"open({str(child_file)!r},'w').write(str(p.pid)); time.sleep(120)")
    original = subprocess.Popen

    def replace_sdk(*args, **kwargs):
        return original([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", replace_sdk)
    result = mirror_tracking(config(), operation_id=str(uuid4()), backend_run_id=str(uuid4()), metrics={},
                             directory=tmp_path, timeout=0.4)
    assert result["error_code"] == "tracking_timeout"
    child_pid = int(child_file.read_text())
    deadline = time.monotonic() + 3
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        try:
            if psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.02)
    assert not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE


def test_readiness_metadata_only_and_no_credential_transmission(monkeypatch):
    from modelbuilder.tracking import tracking_readiness

    calls = []
    def probe(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="0.25.0\n", stderr="")
    monkeypatch.setattr(subprocess, "run", probe)
    monkeypatch.setenv("MODELBUILDER_WANDB_API_KEY", "SENTINEL")
    assert tracking_readiness(None, "unused")["status"] == "disabled"
    assert not calls
    result = tracking_readiness(config(mode="online"), "/operator/optional-python")
    assert result["status"] == "configured"
    assert result["account_verified"] is False
    assert result["blocks_training"] is False
    assert "SENTINEL" not in json.dumps(calls)
    assert "import wandb" not in " ".join(calls[0][0])
