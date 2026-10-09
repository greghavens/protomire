"""Optional, privacy-bounded experiment mirrors. ZenML remains authoritative.

This module never imports W&B. SDK work happens in a dedicated subprocess with
an empty HOME and a strict environment. Only selected scalar metrics and opaque
correlation IDs cross that boundary; recipes and user files never do.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Annotated, Literal
from uuid import UUID

import psutil

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SDK_VERSION = "0.25.0"
_SENSITIVE = re.compile(r"password|secret|token|credential|api[_-]?key|authorization|cookie", re.I)


class TrackingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["disabled", "offline", "online"] = "disabled"
    entity: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    project: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    credential_env: Literal["MODELBUILDER_WANDB_API_KEY"] = "MODELBUILDER_WANDB_API_KEY"
    metric_names: list[Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_./-]{0,127}$")]] = Field(
        default_factory=list, max_length=64, json_schema_extra={"uniqueItems": True})
    artifact_policy: Literal["none", "metrics_snapshot"] = "none"

    @field_validator("metric_names")
    @classmethod
    def safe_metric_names(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Metric names must be unique")
        for name in value:
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_./-]{0,127}", name) or _SENSITIVE.search(name):
                raise ValueError("Metric names must be bounded non-sensitive identifiers")
        return value

    @model_validator(mode="after")
    def destination(self):
        if self.mode != "disabled" and (not self.entity or not self.project):
            raise ValueError("Enabled tracking requires an explicit entity and project")
        return self


class TrackingPolicyError(ValueError):
    """A safe error whose message contains no rejected configuration values."""


def resolve_tracking(project_config: dict | None = None, run_override: dict | None = None,
                     *, online_consent: bool = False) -> dict:
    """A run override replaces (never merges with) project defaults.

    Consent is an ephemeral run-submission flag, never saved in project defaults.
    Importing a recipe cannot enable tracking because tracking is not recipe data.
    """
    try:
        config = TrackingConfig.model_validate(run_override if run_override is not None else project_config or {})
    except (ValidationError, TypeError, ValueError) as error:
        raise TrackingPolicyError("Invalid tracking configuration; use bounded fields without credential values") from error
    if config.mode == "online" and online_consent is not True:
        raise TrackingPolicyError("Online tracking requires explicit consent for this run and W&B destination")
    return config.model_dump()


def disclosure(config: dict) -> dict:
    """The exact application-level sharing contract suitable for a run plan."""
    c = TrackingConfig.model_validate(config)
    return {"provider": "wandb", "mode": c.mode, "entity": c.entity, "project": c.project,
            "metric_names": c.metric_names, "artifact_policy": c.artifact_policy,
            "shares": [] if c.mode == "disabled" else ["selected finite numeric metrics", "opaque run correlation IDs", "checkpoint training step",
                                                         "SDK protocol/runtime bookkeeping"],
            "uploads_source": False, "uploads_datasets": False, "uploads_checkpoints": False,
            "captures_console": False, "collects_system_metrics": False,
            "requires_online_consent": c.mode == "online", "authoritative_store": "zenml"}


def selected_metrics(config: TrackingConfig, metrics: dict) -> dict[str, int | float]:
    selected = {}
    for name in config.metric_names:
        value = metrics.get(name)
        if type(value) not in (int, float):
            continue
        try:
            if math.isfinite(value):
                selected[name] = value
        except OverflowError:
            continue
    return selected


def tracking_environment(config: TrackingConfig, directory: Path, source: dict | None = None) -> dict:
    """Only the configured credential goes to the isolated mirror, never to JSON."""
    source = os.environ if source is None else source
    env = {k: source[k] for k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "SSL_CERT_FILE", "SSL_CERT_DIR")
           if k in source}
    env.update(HOME=str(directory / "home"), USERPROFILE=str(directory / "home"),
               TMPDIR=str(directory / "tmp"), TMP=str(directory / "tmp"), TEMP=str(directory / "tmp"),
               WANDB_MODE=config.mode, WANDB_BASE_URL="https://api.wandb.ai",
               WANDB_CONFIG_DIR=str(directory / "config"), WANDB_CACHE_DIR=str(directory / "cache"),
               WANDB_DATA_DIR=str(directory / "data"), WANDB_DIR=str(directory), WANDB_SILENT="true",
               WANDB_CONSOLE="off", WANDB_DISABLE_CODE="true", WANDB_DISABLE_GIT="true",
               WANDB_SAVE_CODE="false", WANDB_ERROR_REPORTING="false")
    if config.mode == "online" and source.get(config.credential_env):
        env["WANDB_API_KEY"] = source[config.credential_env]
    return env


def mirror_tracking(config: dict | None, *, operation_id: str, backend_run_id: str,
                    metrics: dict, directory: str | Path, online_consent: bool = False,
                    python_executable: str | None = None, timeout: float = 45,
                    stream: bool = False) -> dict:
    """Mirror one completed canonical run without affecting training success.

    No automatic retries or offline sync. A timeout/error may leave a partial W&B
    run; the response makes that uncertainty explicit. Caller persists this
    separate tracking result, not as the canonical training status.
    """
    c = TrackingConfig.model_validate(resolve_tracking(config, online_consent=online_consent))
    result = {"provider": "wandb", "mode": c.mode, "status": "disabled", "run_id": None, "url": None}
    if c.mode == "disabled":
        return result
    # Reject free-form strings before putting correlation fields into a provider.
    operation_id, backend_run_id = str(UUID(operation_id)), str(UUID(backend_run_id))
    run_id = hashlib.sha256(f"modelbuilder:{operation_id}:{backend_run_id}".encode()).hexdigest()[:32]
    result.update(run_id=run_id)
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = tracking_environment(c, root)
    if c.mode == "online" and not env.get("WANDB_API_KEY"):
        return {**result, "status": "unavailable", "error_code": "credential_unavailable"}
    for name in ("home", "tmp", "config", "cache", "data"):
        (root / name).mkdir(exist_ok=True, mode=0o700)
    payload = {"mode": c.mode, "entity": c.entity, "project": c.project, "run_id": run_id,
               "artifact_policy": c.artifact_policy, "metrics": selected_metrics(c, metrics),
               "stream": stream, "supervisor_pid": os.getpid(),
               "correlation": {"application_operation_id": operation_id, "zenml_run_id": backend_run_id}}
    request = root / "mirror-input.json"
    request.write_text(json.dumps(payload, allow_nan=False))
    request.chmod(0o600)
    response = root / "mirror-result.json"
    response.unlink(missing_ok=True)
    worker = Path(__file__).parent / "backend" / "tracking_worker.py"
    try:
        process = subprocess.Popen([python_executable or sys.executable, str(worker), str(request), str(response)],
                                   cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            if stream:
                deadline = time.monotonic() + timeout
                finishing = False
                while process.poll() is None:
                    if not finishing and (root / "mirror-finish.json").exists():
                        finishing = True
                        deadline = min(deadline, time.monotonic() + 45)
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(process.args, timeout)
                    time.sleep(0.1)
                code = process.wait()
            else:
                code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # SDK cores can start their own session. Capture process objects
            # before killing the parent; psutil checks PID birth identity before
            # signalling to avoid killing a reused unrelated PID.
            try:
                descendants = psutil.Process(process.pid).children(recursive=True)
            except psutil.Error:
                descendants = []
            for child in reversed(descendants):
                try:
                    child.kill()
                except psutil.Error:
                    pass
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.wait()
            psutil.wait_procs(descendants, timeout=3)
            return {**result, "status": "failed", "error_code": "tracking_timeout", "remote_may_be_partial": c.mode == "online"}
        if code or not response.exists():
            return {**result, "status": "failed", "error_code": "tracking_process_failed", "remote_may_be_partial": c.mode == "online"}
        reply = json.loads(response.read_text())
        if reply.get("status") not in {"offline", "synced", "failed", "unavailable"}:
            raise ValueError("Invalid mirror response")
        # Never propagate SDK exceptions, URLs or arbitrary response fields.
        result["status"] = reply["status"]
        if result["status"] in {"failed", "unavailable"}:
            result.update(error_code="sdk_unavailable" if reply.get("status") == "unavailable" else "tracking_failed",
                          remote_may_be_partial=c.mode == "online")
        if result["status"] == "synced" and c.mode == "online":
            result["url"] = f"https://wandb.ai/{c.entity}/{c.project}/runs/{run_id}"
        if result["status"] == "offline":
            result["offline_directory"] = str(root)
        return result
    except (OSError, ValueError):
        return {**result, "status": "failed", "error_code": "tracking_process_failed", "remote_may_be_partial": c.mode == "online"}
    finally:
        # Cleanup also runs after forced termination, when SDK finally blocks
        # cannot execute. Never retain raw provider diagnostics on that path.
        for path in root.rglob("*.log"):
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
            except OSError:
                pass


def tracking_readiness(config: dict | None, python_executable: str) -> dict:
    """Read-only package metadata probe. Never import SDK, log in or use network."""
    c = TrackingConfig.model_validate(resolve_tracking(config, online_consent=True))
    result = {"provider": "wandb", "mode": c.mode, "required_sdk_version": SDK_VERSION,
              "sdk_available": None, "credential_available": None, "account_verified": False,
              "blocks_training": False}
    if c.mode == "disabled":
        return {**result, "status": "disabled"}
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL"}}
    try:
        probe = subprocess.run([python_executable, "-I", "-B", "-c",
            "from importlib.metadata import version; print(version('wandb'))"],
            env=env, capture_output=True, text=True, timeout=5)
        available = probe.returncode == 0 and probe.stdout.strip() == SDK_VERSION
    except (OSError, subprocess.TimeoutExpired):
        available = False
    credential = bool(os.environ.get(c.credential_env)) if c.mode == "online" else None
    return {**result, "sdk_available": available, "credential_available": credential,
            "status": "unavailable" if not available else (
                "credential_unavailable" if credential is False else "configured")}
