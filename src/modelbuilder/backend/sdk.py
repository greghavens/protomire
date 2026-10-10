"""ZenML 0.97 public SDK bridge and authoritative native-state reconciliation."""
from __future__ import annotations
import fcntl
import os
import sys
import traceback
from pathlib import Path
from uuid import UUID

import zenml
from zenml import log_metadata, save_artifact
from zenml.client import Client
from zenml.enums import ExecutionStatus
from zenml.models import ModelVersionArtifactRequest, PipelineRunUpdate

from .adapter import BackendError
from ..evaluation import compare_records
from .tracking_bridge import tracking_status
from .common import alive, now, process_group_state, recipe_provenance, termination_confirmed, read_json, write_json

if zenml.__version__ != "0.97.0":
    raise RuntimeError("The execution adapter requires exactly ZenML 0.97.0")


def project_client(project_id: str):
    client = Client()
    try:
        project = client.get_project(f"modelbuilder-{project_id}")
    except KeyError as error:
        raise BackendError("not_found", "Project has no native ZenML records") from error
    os.environ["ZENML_ACTIVE_PROJECT_ID"] = str(project.id)
    return client, project


def metadata_values(resource) -> dict:
    return {k: getattr(v, "value", v) for k, v in resource.run_metadata.items()}


def native_run(job: Path):
    request = read_json(job / "request.json")
    client, project = project_client(request["project_id"])
    native = read_json(job / "native.json")
    try:
        run = client.get_pipeline_run(native.get("backend_run_id") or request["run_name"],
                                      project=project.id, include_full_metadata=True)
    except KeyError:
        return client, None
    if str(run.project.id) != str(project.id):
        raise BackendError("not_found", "Run does not belong to this project")
    write_json(job / "native.json", {"backend_run_id": str(run.id), "model_version_ref": str(run.model_version.id) if run.model_version else None,
                                     "backend_project_id": str(project.id)})
    return client, run


def reconcile(job: Path):
    with (job / "reconcile.lock").open("a") as lease:
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX)
        return _reconcile(job)


def _reconcile(job: Path):
    state = read_json(job / "state.json")
    worker_alive = True if alive(state.get("worker_pid"), state.get("worker_identity")) else process_group_state(state.get("worker_pid"))
    supervisor_alive = alive(state.get("supervisor_pid"), state.get("supervisor_identity"))
    try:
        client, run = native_run(job)
    except BackendError as error:
        if error.code != "not_found":
            raise
        client, run = None, None
    terminated = worker_alive is False and termination_confirmed(state)
    if not run:
        # There may be no native run yet (for example, a trusted module is still
        # importing). Keep IDs null, but preserve positive process termination
        # evidence in the supervisor journal instead of waiting forever.
        if terminated and not read_json(job / "native.json").get("backend_run_id") and not state.get("termination"):
            state["termination"] = {"confirmed": True, "reason": "cancelled" if (job / "cancel.json").exists() else "worker_failure",
                                    "worker_pid": state["worker_pid"], "worker_identity": state["worker_identity"],
                                    "runtime_identity": state.get("runtime_identity"), "process_group_terminated": True,
                                    "confirmed_at": state.get("worker_terminated_at", now()),
                                    "proof_source": "local_os_identity_and_group_observation"}
            write_json(job / "state.json", state)
        return None
    if terminated:
        local_termination = state.get("termination", {})
        cancelled = (local_termination.get("reason") == "cancelled" if local_termination.get("confirmed") is True
                     else (job / "cancel.json").exists())
        # ZenML 0.97 static pipelines derive FAILED from an interrupted step;
        # CANCELLED is not a representable terminal static-pipeline status.
        # Preserve that native state, and record verified runtime termination
        # through native metadata instead of forging a competing run record.
        if run.status not in {ExecutionStatus.COMPLETED, ExecutionStatus.CACHED}:
            if run.status != ExecutionStatus.FAILED:
                client.zen_store.update_run(run.id, PipelineRunUpdate(status=ExecutionStatus.FAILED, status_reason=(
                    "Local supervisor confirmed cancellation and process-group termination" if cancelled else
                    "Local worker terminated without completed pipeline")))
            termination = local_termination if local_termination.get("confirmed") is True else {
                "reason": "cancelled" if cancelled else "worker_failure", "confirmed": True,
                "worker_pid": state.get("worker_pid"), "process_group_terminated": True,
                "confirmed_at": state.get("worker_terminated_at", now()), "proof_source": "local_os_identity_and_group_observation"}
            if metadata_values(run).get("local_supervisor_termination", {}).get("confirmed") is not True:
                log_metadata({"local_supervisor_termination": termination}, run_id_name_or_prefix=run.id)
            run = client.get_pipeline_run(run.id, include_full_metadata=True)
        if not (job / "recovery.json").exists():
            request = read_json(job / "request.json")
            for node in request["recipe"]["nodes"]:
                source = job / "outputs" / node["id"]
                recovered = None
                if node["kind"] == "model.train" and (source / "checkpoint.pt").is_file():
                    recovered = source
                elif node["kind"] == "component.run" and node.get("component", {}).get("role") == "training" and source.is_dir():
                    from .components import read_checkpoint
                    valid = []
                    for manifest in source.rglob("checkpoint.manifest.json"):
                        try:
                            if not manifest.resolve().is_relative_to(source.resolve()):
                                continue
                            descriptor = read_checkpoint(manifest.parent)
                            if descriptor["purpose"] in {"last", "resumable"}:
                                valid.append((descriptor["step"], manifest.parent))
                        except (ValueError, KeyError, OSError, TypeError):
                            continue  # Interrupted/unready artifacts are never promoted to recovery.
                    if valid:
                        recovered = max(valid, key=lambda item: item[0])[1]
                if recovered is not None and run.status != ExecutionStatus.COMPLETED:
                    artifact = save_artifact(recovered, name=f"recovery-{job.name}", include_visualizations=False,
                                             user_metadata={"parent_run_id": str(run.id), "recovery_after_termination": True})
                    if run.model_version:
                        client.zen_store.create_model_version_artifact_link(ModelVersionArtifactRequest(
                            model_version=run.model_version.id, artifact_version=artifact.id))
                    log_metadata({"recovery_checkpoint_ref": str(artifact.id)}, run_id_name_or_prefix=run.id)
                    write_json(job / "recovery.json", {"artifact_id": str(artifact.id)})
        if not supervisor_alive and not state.get("worker_reaped"):
            state.update(supervisor_state="recovered", worker_termination_confirmed=True, recovered_at=now())
            write_json(job / "state.json", state)
    return run


def artifacts_for(job: Path, run) -> list[dict]:
    artifacts = []
    if run:
        for step_name, step_run in run.steps.items():
            for port, versions in step_run.outputs.items():
                for artifact in versions:
                    artifacts.append({"artifact_id": str(artifact.id), "name": artifact.name, "step_id": str(step_run.id),
                                      "node_id": step_name, "port": port, "uri": artifact.uri, "backend_run_id": str(run.id)})
        recovery = read_json(job / "recovery.json")
        if recovery:
            artifact = Client().get_artifact_version(UUID(recovery["artifact_id"]))
            artifacts.append({"artifact_id": str(artifact.id), "name": artifact.name, "port": "recovery_checkpoint",
                              "uri": artifact.uri, "backend_run_id": str(run.id)})
    return artifacts


def evaluation_outputs(run, request=None, *, verify_checkpoints=True) -> list[dict]:
    """Keep each native evaluation output distinct, even with duplicate names."""
    if not run:
        return []
    from ..evaluation import evaluation_for
    from .native_inputs import validate_native_type
    request = request or {}
    from .evaluation_binding import checkpoint_candidates, pending_verification, verify_checkpoint
    declared = {node["id"]: node for node in request.get("recipe", {}).get("nodes", [])}
    rows = []
    for node_id, step_run in run.steps.items():
        node = declared.get(node_id, {})
        evaluation_node = node.get("kind") == "model.evaluate" or node.get("component", {}).get("role") == "evaluation"
        candidates, errors = [], []
        if verify_checkpoints:
            try:
                candidates, errors = checkpoint_candidates(run, request, node_id)
            except (BackendError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                errors = [str(error)]
        for output_port, versions in step_run.outputs.items():
            if output_port != "metrics" and not evaluation_node:
                continue
            for artifact in versions:
                if artifact.data_type.import_path != "builtins.dict":
                    continue
                validate_native_type(artifact, "dict")
                measured = artifact.load()
                if not isinstance(measured, dict):
                    continue
                names = set(measured.get("evaluation_records", {}))
                single = measured.get("evaluation_record")
                if isinstance(single, dict) and isinstance(single.get("metric"), dict):
                    names.add(single["metric"].get("name"))
                for metric in sorted(name for name in names if isinstance(name, str)):
                    record = evaluation_for(measured, metric)
                    if record:
                        verification, model_version, checkpoint_bindings = (verify_checkpoint(record, candidates, errors)
                            if verify_checkpoints else (pending_verification(), None,
                                [binding for binding in request.get("resolved_input_bindings", []) if binding["node_id"] == node_id]))
                        rows.append({"backend_run_id": str(run.id), "node_id": node_id, "output_port": output_port,
                            "artifact_id": str(artifact.id), "metric": metric, "value": measured.get(metric, record.get("value")),
                            "evaluation_digest": record.get("digest"), "record": record,
                            "checkpoint_bindings": checkpoint_bindings, "model_version_ref": model_version,
                            "checkpoint_verification": verification})
    return rows


def metrics_for(run, evaluations=None) -> dict:
    if not run:
        return {}
    evaluations = evaluation_outputs(run) if evaluations is None else evaluations
    metrics, owners = {}, {}
    for node_id, step_run in run.steps.items():
        for output_port, versions in step_run.outputs.items():
            if output_port == "metrics" and len(versions) == 1:
                measured = versions[0].load()
                if isinstance(measured, dict):
                    for key, value in measured.items():
                        if key not in {"evaluation_record", "evaluation_records", "evaluation_catalog", "evaluation_conflicts"}:
                            owners.setdefault(key, []).append(node_id)
                            metrics[key] = value
    # Never flatten duplicate output metrics, even if their values happen to agree.
    for key, nodes in owners.items():
        if len(nodes) > 1:
            metrics.pop(key, None)
    grouped = {}
    for entry in evaluations:
        grouped.setdefault(entry["metric"], []).append(entry)
    metrics["evaluation_records"] = {name: entries[0]["record"] for name, entries in grouped.items() if len(entries) == 1}
    for name, entries in grouped.items():
        if len(entries) == 1:
            metrics[name] = entries[0]["value"]
        else:
            metrics.pop(name, None)
    metrics["evaluation_conflicts"] = {name: [entry["node_id"] for entry in entries] for name, entries in grouped.items() if len(entries) > 1}
    metrics["evaluation_catalog"] = [{key: value for key, value in entry.items() if key not in {"record", "checkpoint_bindings"}} for entry in evaluations]
    return metrics


def select_evaluations(records, selectors, metric):
    if selectors is None:
        result = []
        for record in records:
            matching = [entry for entry in record.get("evaluations", []) if entry["metric"] == metric]
            result.append({**record, "model_version_ref": matching[0].get("model_version_ref"),
                           "checkpoint_verification": matching[0].get("checkpoint_verification")} if len(matching) == 1 else record)
        return result
    from jsonschema import Draft202012Validator
    from ..execution import EVALUATIONS_SCHEMA
    if list(Draft202012Validator(EVALUATIONS_SCHEMA).iter_errors(selectors)):
        raise BackendError("invalid_evaluation_selection", "Evaluation selectors must identify native run/node/output/artifact and metric")
    requested = {entry["backend_run_id"]: entry for entry in selectors}
    if len(requested) != len(selectors) or set(requested) != {record["backend_run_id"] for record in records} or len(records) != len(requested):
        raise BackendError("invalid_evaluation_selection", "Select exactly one evaluation for each distinct compared run")
    result = []
    for record in records:
        wanted = requested[record["backend_run_id"]]
        if wanted["metric"] != metric:
            raise BackendError("invalid_evaluation_selection", "Selected evaluation metric differs from comparison metric")
        matched = [entry for entry in record.get("evaluations", []) if all(entry.get(key) == value for key, value in wanted.items())]
        if len(matched) != 1:
            raise BackendError("invalid_evaluation_selection", "Selected evaluation is absent or ambiguous in the requested native run")
        entry = matched[0]
        result.append({**record, "model_version_ref": entry.get("model_version_ref"),
                       "metrics": {metric: entry["value"], "evaluation_records": {metric: entry["record"]}},
                       "selected_evaluation": wanted, "checkpoint_verification": entry.get("checkpoint_verification")})
    return result


def compare_evaluations(records, payload):
    metric, direction = payload.get("metric", "validation_loss"), payload.get("direction", "minimize")
    chosen = select_evaluations(records, payload.get("evaluations"), metric)
    result = compare_records(chosen, metric, direction, payload.get("comparison_contract"))
    for record in chosen:
        verification = record.get("checkpoint_verification") or {}
        if verification.get("verified") is not True:
            result["diagnostics"].append({"code": "evaluation_checkpoint_unverified", "field": "treatment.checkpoint",
                "message": verification.get("message", "Native checkpoint identity and ownership are unverified; reevaluate or select verified evidence"),
                "backend_run_id": record["backend_run_id"]})
    if result["diagnostics"]:
        result.update(compatible=False, selected_run_id=None, selected_model_version_ref=None)
        result.pop("comparison_digest", None)
        for row in result["runs"]:
            row.pop("delta_from_selected", None)
    if payload.get("evaluations") is not None:
        result["evaluations"] = [record["selected_evaluation"] for record in chosen]
        result["selected_evaluation"] = next((record["selected_evaluation"] for record in chosen if record["backend_run_id"] == result.get("selected_run_id")), None)
        if result.get("compatible"):
            from ..evaluation import digest
            result["comparison_digest"] = digest({key: value for key, value in result.items() if key != "comparison_digest"})
    return result


def status(job: Path, *, verify_evaluations=False) -> dict:
    request, state = read_json(job / "request.json"), read_json(job / "state.json")
    run = reconcile(job)
    state = read_json(job / "state.json")
    worker_alive = True if alive(state.get("worker_pid"), state.get("worker_identity")) else process_group_state(state.get("worker_pid"))
    native_status = run.status.value if run else None
    identity = recipe_provenance(request["recipe"])
    if run:
        native_identity = getattr(getattr(run, "config", None), "extra", {})
        identity.update({key: native_identity[key] for key in identity if key in native_identity})
    terminal = {"completed": "succeeded", "cached": "succeeded", "failed": "failed", "cancelled": "unknown"}
    termination = metadata_values(run).get("local_supervisor_termination", {}) if run else state.get("termination", {})
    if native_status in {"completed", "cached"}:
        current = "succeeded"
    elif termination.get("reason") == "cancelled" and termination.get("confirmed") is True and termination.get("process_group_terminated") is True and termination_confirmed(state) and worker_alive is False:
        current = "cancelled"
    elif (native_status == "failed" and termination.get("reason") == "worker_failure"
          and termination.get("confirmed") is True and termination.get("process_group_terminated") is True
          and termination_confirmed(state) and worker_alive is False):
        # A later cancel request cannot rewrite an already established failure.
        current = "failed"
    elif (job / "cancel.json").exists():
        # Native termination metadata can lag the freshly reread supervisor
        # journal. Do not expose a transient terminal failure in that window.
        current = "cancel_requested"
    elif native_status in terminal:
        current = terminal[native_status]
    elif worker_alive:
        current = "running" if run else "submitted"
    elif state.get("worker_pid"):
        current = "failed" if not run else "unknown"
    else:
        current = "submitted"
    evaluations = evaluation_outputs(run, request, verify_checkpoints=verify_evaluations)
    execution = request.get("execution") or {"node_ids": [node["id"] for node in request["recipe"]["nodes"]], "input_bindings": []}
    # Never report cancellation while the actual worker remains alive.
    if current == "cancelled" and worker_alive:
        current = "cancel_requested"
    return {"operation_id": job.name, "project_id": request["project_id"], "backend": "zenml", "backend_version": zenml.__version__,
            "backend_run_id": str(run.id) if run else None, "backend_snapshot_id": str(run.snapshot.id) if run and getattr(run, "snapshot", None) else None,
            "backend_status": native_status, "status": current, **identity,
            "model_version_ref": str(run.model_version.id) if run and run.model_version else None,
            "worker_alive": worker_alive, "worker_pid": state.get("worker_pid"), "worker_exit_code": state.get("worker_exit_code"),
            "supervisor_alive": alive(state.get("supervisor_pid"), state.get("supervisor_identity")),
            "process_group_alive": process_group_state(state.get("worker_pid")),
            "submitted_at": request["submitted_at"], "last_reconciled_at": now(),
            "metrics": metrics_for(run, evaluations), "evaluations": evaluations, "artifacts": artifacts_for(job, run),
            "execution": {**execution, "input_bindings": request.get("resolved_input_bindings", [])},
            "executed_node_ids": [node for node, step in run.steps.items() if step.status.value in {"running", "completed", "cached", "failed"}] if run else [],
            "tracking": tracking_status(job, request, current),
            "parent_run_id": request.get("parent_run_id"), "parent_checkpoint_ref": request.get("parent_checkpoint_ref"),
            "parent_recipe_id": request.get("parent_recipe_id"), "parent_recipe_revision": request.get("parent_recipe_revision"),
            "parent_recipe_digest": request.get("parent_recipe_digest"),
            "resume_mode": request.get("resume_mode"), "termination": termination,
            "runtime_status_source": "verified_local_supervisor",
            "termination_record_source": "native_run_metadata" if run and termination else ("supervisor_journal" if termination else None),
            "error": (str(run.exception_info) if run and run.exception_info else
                      state.get("submission_error") or ("Worker terminated before creating a native run; inspect run.logs" if current == "failed" and not run else None)),
            "log_available": (job / "worker.log").exists(), "executed_recipe_digest": request.get("executed_recipe_digest") or request["recipe"].get("digest") }


def checkpoint(job: Path, kind: str) -> dict:
    run = reconcile(job)
    if not run:
        raise BackendError("not_found", "Native run has no checkpoint")
    request = read_json(job / "request.json")
    training_nodes = {node["id"]: node for node in request["recipe"]["nodes"]
                      if node["kind"] == "model.train" or
                      (node["kind"] == "component.run" and node.get("component", {}).get("role") == "training")}
    training_ids = set(training_nodes)
    candidates = [a for a in artifacts_for(job, run) if a["port"] == "recovery_checkpoint" or
                  (a.get("node_id") in training_ids and (a["port"] == "checkpoint" or
                   any(port["name"] == a["port"] and port["type"] == "Path"
                       for port in training_nodes[a["node_id"]].get("outputs", []))))]
    if not candidates:
        raise BackendError("not_found", "No completed native checkpoint or recovered checkpoint exists")
    mismatches = []
    for selected in reversed(candidates):
        artifact = Client().get_artifact_version(UUID(selected["artifact_id"]))
        path = artifact.load()
        if not isinstance(path, (Path, str)):
            continue
        if (Path(path) / "checkpoint.manifest.json").is_file():
            from .components import read_checkpoint, identity
            descriptor = read_checkpoint(Path(path))
            purposes = {"last": {"last", "resumable"}, "best": {"best", "selected"}}
            if descriptor["purpose"] not in purposes.get(kind, {kind}):
                mismatches.append(descriptor["purpose"])
                continue
            return {**selected, "path": str(path), "checkpoint_kind": kind, "sha256": identity(descriptor),
                    "resume_modes": descriptor["resume_modes"], "descriptor": descriptor}
        if training_nodes.get(selected.get("node_id"), {}).get("kind") == "component.run":
            continue  # Other declared Path outputs are not checkpoints.
        expected_file = Path(path) / ("best.pt" if kind == "best" else "checkpoint.pt")
        if not expected_file.is_file():
            continue
        import hashlib
        return {**selected, "path": str(path), "checkpoint_kind": kind,
                "sha256": hashlib.sha256(expected_file.read_bytes()).hexdigest(),
                "resume_modes": ["exact_state", "warm_start"]}
    raise BackendError("capability_unavailable", "No verified checkpoint supports the requested purpose", {"requested": kind, "available_purposes": mismatches})


def version_record(version) -> dict:
    return {"model_version_id": str(version.id), "model_version_ref": str(version.id), "name": version.name,
            "number": version.number, "model_id": str(version.model.id), "stage": version.stage,
            "metadata": metadata_values(version), "backend_project_id": str(version.project.id)}


def find_version(client, project, payload):
    identity = payload.get("model_version_id") or payload.get("model_version_ref")
    if not identity:
        raise BackendError("validation_error", "model_version_id is required")
    try:
        version = client.get_model_version(model_version_name_or_number_or_id=UUID(identity), project=project.id)
    except (KeyError, ValueError) as error:
        raise BackendError("not_found", "Model version does not belong to this project") from error
    if str(version.project.id) != str(project.id) or version.model.name != f"model-{payload['project_id']}":
        raise BackendError("not_found", "Model version does not belong to this project")
    return version


def promotion_comparison(client, project, version, payload, root):
    decision = payload.get("decision", {})
    rule = decision.get("selection_rule", {})
    metric = payload.get("metric", rule.get("metric", "validation_loss"))
    direction = payload.get("direction", rule.get("direction", "minimize"))
    run_ids = payload.get("candidate_run_ids", decision.get("run_ids", []))
    records = []
    for identity in run_ids:
        candidate = client.get_pipeline_run(identity, project=project.id)
        if str(candidate.project.id) != str(project.id) or candidate.status != ExecutionStatus.COMPLETED:
            raise BackendError("validation_error", "Selection candidates must be completed runs in this project")
        job = next((job for job in (root / "jobs").glob("*") if
                    read_json(job / "request.json").get("project_id") == payload["project_id"] and
                    read_json(job / "native.json").get("backend_run_id") == str(candidate.id)), None)
        request = read_json(job / "request.json") if job else {}
        evaluations = evaluation_outputs(candidate, request) if request else []
        measured = metrics_for(candidate, evaluations) if request else metrics_for(candidate)
        if metric not in measured and not payload.get("evaluations", rule.get("evaluations")):
            raise BackendError("validation_error", "Declared validation metric is absent")
        records.append({"backend_run_id": str(candidate.id),
                        "model_version_ref": str(candidate.model_version.id) if candidate.model_version else None,
                        "status": "succeeded", "metrics": measured, "evaluations": evaluations})
    if not records:
        raise BackendError("validation_error", "Promotion requires the actual candidate run IDs and a measured selection rule")
    comparison = compare_evaluations(records, {"metric": metric, "direction": direction,
        "comparison_contract": payload.get("comparison_contract", rule.get("comparison_contract")),
        "evaluations": payload.get("evaluations", rule.get("evaluations"))})
    if not comparison["compatible"]:
        raise BackendError("incompatible_evaluation", "Promotion requires scientifically comparable evaluation evidence", comparison)
    if rule.get("comparison_digest") and rule["comparison_digest"] != comparison["comparison_digest"]:
        raise BackendError("comparison_changed", "Decision evaluation evidence changed; record a fresh decision")
    if comparison["selected_model_version_ref"] != str(version.id):
        raise BackendError("validation_error", "Requested model version does not win the declared measured validation criterion")
    return comparison


def invoke(operation: str, payload: dict, root: Path) -> dict:
    if operation == "backend.validate":
        import importlib.metadata
        import platform
        from packaging.requirements import Requirement
        environment = payload["environment"]
        declared_python = environment.get("python", "3.12").split(".")
        if platform.python_version().split(".")[:len(declared_python)] != declared_python:
            raise BackendError("dependency_mismatch", "Backend Python version differs from declared environment")
        resolved = {}
        for text in environment.get("requirements", []):
            requirement = Requirement(text)
            if requirement.marker and not requirement.marker.evaluate():
                continue
            if requirement.url or len(list(requirement.specifier)) != 1 or next(iter(requirement.specifier)).operator != "==":
                raise BackendError("dependency_mismatch", "Execution environment requirements must be exactly pinned")
            try:
                version = importlib.metadata.version(requirement.name)
            except importlib.metadata.PackageNotFoundError as error:
                raise BackendError("dependency_unavailable", "Required backend distribution is absent", {"package": requirement.name}) from error
            if version not in requirement.specifier:
                raise BackendError("dependency_mismatch", "Backend distribution differs from declared recipe", {"package": requirement.name, "actual": version})
            resolved[requirement.name] = version
        client = Client()
        active_stack = client.active_stack
        return {"python": platform.python_version(), "packages": resolved, "verified": True, "stack_id": str(active_stack.id)}
    if operation == "model.version.promotion.prepare":
        from .promotion import prepare
        return prepare(payload, root)
    if operation.startswith("model.export."):
        from .native_exports import invoke as export_operation
        return export_operation(operation, payload, root)
    if operation == "run.status":
        return status(Path(payload["job_dir"]))
    if operation == "run.artifacts":
        job = Path(payload["job_dir"])
        run = reconcile(job)
        artifacts = artifacts_for(job, run)
        if payload.get("include_binding_descriptors"):
            from .native_inputs import output_descriptor
            for item in artifacts:
                if item.get("node_id"):
                    descriptor, _ = output_descriptor(job, run, item["node_id"], item["port"], item["artifact_id"])
                    item["binding_descriptor"] = descriptor
        return {"backend_run_id": str(run.id) if run else None, "artifacts": artifacts}
    if operation == "run.compare":
        records = [status(Path(job), verify_evaluations=True) for job in payload["job_dirs"]]
        return compare_evaluations(records, payload)
    if operation == "model.checkpoint.get":
        if payload.get("job_dir"):
            result = checkpoint(Path(payload["job_dir"]), payload.get("checkpoint_kind", "last"))
            requested = payload.get("checkpoint_ref") or payload.get("checkpoint_id")
            if requested and requested != result["artifact_id"]:
                raise BackendError("dependency_mismatch", "Requested checkpoint identity differs from resolved checkpoint")
            return result
        identity = payload.get("backend_run_id")
        for job in (root / "jobs").iterdir():
            request = read_json(job / "request.json")
            native = read_json(job / "native.json")
            if request.get("project_id") == payload["project_id"] and (native.get("backend_run_id") == identity or
                    native.get("model_version_ref") in {payload.get("model_version_ref"), payload.get("model_version_id")} - {None}):
                result = checkpoint(job, payload.get("checkpoint_kind", "last"))
                requested = payload.get("checkpoint_ref") or payload.get("checkpoint_id")
                if requested and requested != result["artifact_id"]:
                    raise BackendError("dependency_mismatch", "Requested checkpoint identity differs from resolved checkpoint")
                return result
        raise BackendError("not_found", "Checkpoint not found in this project")
    try:
        client, project = project_client(payload["project_id"])
    except BackendError as error:
        if operation == "model.version.list" and error.code == "not_found":
            return {"versions": []}
        raise
    if operation == "model.version.list":
        versions = client.list_model_versions(model_name_or_id=f"model-{payload['project_id']}", project=project.id, hydrate=True, size=1000).items
        return {"versions": [version_record(v) for v in versions]}
    version = find_version(client, project, payload)
    if operation == "model.version.get":
        return version_record(version)
    if operation == "model.version.lineage":
        links = client.list_model_version_pipeline_run_links(model_version_id=version.id, size=1000).items
        artifact_links = client.list_model_version_artifact_links(model_version_id=version.id, size=1000).items
        return {**version_record(version), "pipeline_run_links": [x.model_dump(mode="json") for x in links],
                "artifact_links": [x.model_dump(mode="json") for x in artifact_links]}
    if operation == "model.version.promote":
        from .promotion import prepared_comparison, prepare
        handle = payload.get("_prepared_promotion")
        clean = {key: value for key, value in payload.items() if key != "_prepared_promotion"}
        if handle is None:
            handle = prepare(clean, root)
        comparison = prepared_comparison(clean, root, handle)
        from .promotion import apply_promotion
        return apply_promotion(client, project, version, clean, comparison, root)
    raise BackendError("capability_unavailable", f"Unsupported native operation {operation}")


if __name__ == "__main__":
    request_path = Path(sys.argv[1])
    request = read_json(request_path)
    try:
        result = invoke(request["operation"], request["payload"], Path(request["root"]))
    except Exception as error:
        traceback.print_exc()
        result = {"error": {"code": getattr(error, "code", "backend_error"), "message": str(error),
                            "details": getattr(error, "details", {})}}
    write_json(Path(request["result"]), result)
