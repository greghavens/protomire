"""Prepared native selection evidence for a short, guarded promotion commit.

Only preparation reads checkpoint bytes. Apply re-fetches native ownership and
checks secure inode/ctime witnesses; changed content, replacement, re-materialized
metrics, request provenance, or native inputs require fresh preparation. This is
an integrity guard within the trusted local runtime, not protection from an OS
administrator or authentication of arbitrary evaluator claims.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from .adapter import BackendError
from .common import read_json, write_json
from .components import identity
from .native_inputs import storage_fingerprint
from ..export_packages import safe_file

MAX_PREPARATION_SECONDS = 300
MAX_PREPARED_BYTES = 16 * 1024**2


def _snapshot(client, project, payload, root):
    run_ids = payload.get("candidate_run_ids", payload.get("decision", {}).get("run_ids", []))
    if not run_ids or len(run_ids) != len(set(run_ids)):
        raise BackendError("validation_error", "Promotion requires distinct native candidate runs")
    snapshots = []
    run_ids = list(run_ids)
    if payload.get("scientific_gate") and payload["scientific_gate"]["backend_run_id"] not in run_ids:
        run_ids.append(payload["scientific_gate"]["backend_run_id"])
    for run_id in run_ids:
        if len(run_ids) > 1000:
            raise BackendError("capability_unavailable", "Promotion lineage exceeds its native run bound")
        run = client.get_pipeline_run(run_id, project=project.id, include_full_metadata=True)
        if str(run.project.id) != str(project.id) or run.status.value != "completed":
            raise BackendError("validation_error", "Selection candidates must be completed runs in this project")
        jobs = [job for job in (root / "jobs").glob("*") if
                read_json(job / "request.json").get("project_id") == payload["project_id"] and
                read_json(job / "native.json").get("backend_run_id") == str(run.id)]
        if len(jobs) != 1:
            raise BackendError("incompatible_evaluation", "Candidate has no unique owned immutable request")
        request = read_json(jobs[0] / "request.json")
        for binding in request.get("resolved_input_bindings", []):
            if not binding.get("backend_run_id"):
                raise BackendError("incompatible_evaluation", "Checkpoint parent lacks a native producer run identity")
            if binding["backend_run_id"] not in run_ids:
                run_ids.append(binding["backend_run_id"])
        nodes = {node["id"]: node for node in request["recipe"]["nodes"]}
        steps, artifacts = {}, {}
        from .native_invocations import invocations
        for invocation, step in invocations(run, request["recipe"]):
            node_id = invocation["canonical_node_id"]
            steps[invocation["invocation_id"]] = {side: {port: [str(artifact.id) for artifact in versions] for port, versions in getattr(step, side, {}).items()}
                              for side in ("inputs", "outputs")}
            steps[invocation["invocation_id"]]["invocation"] = invocation
            node = nodes.get(node_id, {})
            evaluator = node.get("kind") == "model.evaluate" or node.get("component", {}).get("role") in {"evaluation", "scientific_gate"}
            for side in ("inputs", "outputs"):
                for port, versions in getattr(step, side, {}).items():
                    for artifact in versions:
                        # All native checkpoint inputs/outputs and evaluation
                        # dictionaries used by selection receive a witness.
                        path = artifact.data_type.import_path in {"pathlib.Path", "pathlib.PosixPath", "pathlib.WindowsPath"}
                        if not path and not (side == "outputs" and (port == "metrics" or evaluator)) and not (evaluator and side == "inputs" and node.get("component", {}).get("role") == "scientific_gate"):
                            continue
                        if str(artifact.project.id) != str(project.id):
                            raise BackendError("incompatible_evaluation", "Selection artifact belongs to another project")
                        witness = {"uri": artifact.uri, "type": artifact.data_type.import_path,
                            "materializer": artifact.materializer.import_path, "fingerprint": storage_fingerprint(artifact, root)}
                        if str(artifact.id) in artifacts and artifacts[str(artifact.id)] != witness:
                            raise BackendError("comparison_changed", "Native artifact identity has conflicting materializations")
                        artifacts[str(artifact.id)] = witness
        snapshots.append({"run_id": str(run.id), "project_id": str(run.project.id),
            "model_version_ref": str(run.model_version.id) if run.model_version else None,
            "native_provenance": getattr(getattr(run, "config", None), "extra", {}),
            "request": identity(request), "steps": steps, "artifacts": artifacts})
    return snapshots


def prepare(payload, root: Path):
    from .sdk import project_client, find_version, promotion_comparison
    client, project = project_client(payload["project_id"])
    version = find_version(client, project, payload)
    before = _snapshot(client, project, payload, root)
    comparison = promotion_comparison(client, project, version, payload, root)
    if _snapshot(client, project, payload, root) != before:
        raise BackendError("comparison_changed", "Native evidence changed during checkpoint verification; compare again")
    token = uuid.uuid4().hex
    body = {"payload_digest": identity(payload), "comparison": comparison, "snapshot": before,
            "project_id": payload["project_id"], "model_version_id": str(version.id), "prepared_at": time.time()}
    serialized_bytes = 0
    for chunk in json.JSONEncoder(sort_keys=True, indent=2).iterencode(body):
        serialized_bytes += len(chunk.encode())
        if serialized_bytes > MAX_PREPARED_BYTES:
            raise BackendError("capability_unavailable", "Prepared native evidence exceeds its serialized byte limit; compare fewer candidates")
    write_json(root / "promotion-evidence" / (token + ".json"), body)
    return {"token": token, "digest": identity(body)}


def prepared_comparison(payload, root: Path, handle):
    from .sdk import project_client, find_version
    if not isinstance(handle, dict) or set(handle) != {"token", "digest"} or not re.fullmatch(r"[0-9a-f]{32}", str(handle.get("token"))):
        raise BackendError("comparison_changed", "Invalid prepared native promotion evidence")
    try:
        with safe_file(root / "promotion-evidence" / (handle["token"] + ".json"), max_bytes=MAX_PREPARED_BYTES) as stream:
            body = json.load(stream)
        if (identity(body) != handle["digest"] or identity(payload) != body["payload_digest"]
                or body["project_id"] != payload["project_id"]
                or not 0 <= time.time() - body["prepared_at"] <= MAX_PREPARATION_SECONDS):
            raise ValueError("Prepared evidence changed, expired, or belongs to another request")
        client, project = project_client(payload["project_id"])
        version = find_version(client, project, payload)
        if str(version.id) != body["model_version_id"] or _snapshot(client, project, payload, root) != body["snapshot"]:
            raise ValueError("Native input bytes, ownership, or source provenance changed after preparation")
        from .execution_rights import verify_artifact_authorization
        for rights in body["comparison"].get("checkpoint_rights", {}).get(body["comparison"]["selected_run_id"], []):
            verify_artifact_authorization(payload.get("_execution_rights_authorization"), rights, "promote")
        return body["comparison"]
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise BackendError("comparison_changed", "Prepared native selection evidence is no longer valid", {"reason": str(error)}) from error


def apply_promotion(client, project, version, payload, comparison, root: Path):
    """Reconcile a lost reply by observed native state, never blindly repeat it."""
    import fcntl
    from .sdk import find_version, log_metadata, metadata_values, version_record
    action = identity({key: value for key, value in payload.items() if key not in {"request_id", "_prepared_promotion"}})
    directory = root / "promotion-attempts"
    directory.mkdir(exist_ok=True)
    path = directory / (action + ".json")
    stage = payload.get("stage", "staging")
    selected_value = next(row["value"] for row in comparison["runs"] if row["backend_run_id"] == comparison["selected_run_id"])
    with (directory / (action + ".lock")).open("a") as lease:
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX)
        if path.exists():
            current = find_version(client, project, payload)
            current_stage = getattr(current.stage, "value", current.stage)
            if (str(current.id) == str(version.id) and current_stage == stage
                    and metadata_values(current).get("selection_commit_digest") == action):
                return {**version_record(current), "selected_run_id": comparison["selected_run_id"], "selection_value": selected_value}
            raise BackendError("promotion_reconciliation_required",
                "A previous native promotion attempt has an uncertain outcome; inspect the native version before making a new decision",
                {"model_version_id": str(version.id), "intended_stage": stage, "selection_commit_digest": action})
        # Commit intent precedes the first native write. An interrupted/failed
        # write is never assumed absent merely because a reply was lost.
        write_json(path, {"phase": "applying", "model_version_id": str(version.id), "stage": stage,
                          "comparison_digest": comparison["comparison_digest"], "selection_commit_digest": action})
        decision = payload.get("decision", {})
        try:
            log_metadata({"selection_rule": {"metric": comparison["metric"], "direction": comparison["direction"],
                                             "comparison_contract": comparison["comparison_contract"]},
                          "selection_value": selected_value, "selection_comparison_digest": comparison["comparison_digest"],
                          "selection_evaluation_digests": comparison["evaluation_digests"],
                          "selection_candidate_run_ids": payload.get("candidate_run_ids", decision.get("run_ids", [])),
                          "selection_decision": decision, "selection_commit_digest": action}, model_version_id=version.id)
            updated = client.update_model_version(version.model.id, version.id, stage=stage, force=True, project=project.id)
            result = {**version_record(updated), "selected_run_id": comparison["selected_run_id"], "selection_value": selected_value}
            write_json(path, {"phase": "applied", "result": result, "selection_commit_digest": action})
            return result
        except Exception as error:
            raise BackendError("promotion_reconciliation_required",
                "Native promotion did not return a confirmed receipt; retry the same request to reconcile observed native state",
                {"model_version_id": str(version.id), "intended_stage": stage, "selection_commit_digest": action}) from error
