"""Request-local admission and durable dispatch fencing.

Only the original live request can launch. Persisted intents are correlation,
not authorization; retries/restarts read them and never adopt their ownership.
The dispatch fence linearizes authorization/cancellation before the OS launch.
There is deliberately no cross-store exactly-once promise.
"""
from __future__ import annotations

import copy
import json
import threading

from .models import ServiceError, new_id, utcnow
from .tracking import disclosure

_PREPARERS = threading.BoundedSemaphore(4)
_READS = {"run.status", "run.logs", "run.artifacts", "run.evaluations.list", "run.events.list"}


def _put(service, kind, old, changes):
    body = {**old, **changes, "revision": old["revision"] + 1}
    service.store.put(kind, old["operation_id"], old["project_id"], body, old["revision"])
    return body


def _intent(service, operation_id):
    try:
        return service.store.get("dispatch_intent", operation_id)
    except ServiceError as error:
        if error.code != "not_found":
            raise
        return None


def _receipt(service, previous):
    if previous.get("operation_id"):
        try:
            current = service.store.get("operation", previous["operation_id"])
            # Native liveness/metrics in an older receipt are stale observations.
            return previous if current["revision"] == previous.get("revision") else current
        except ServiceError:
            pass
    return previous


def _record(enriched):
    recipe = enriched["recipe"]
    return {"operation_id": enriched["operation_id"], "project_id": enriched["project_id"],
            "backend_run_id": None, "recipe_id": recipe["recipe_id"],
            "recipe_revision": recipe["revision"], "recipe_digest": recipe["digest"],
            "created_at": utcnow(), "last_reconciled_at": utcnow(), "revision": 1,
            "tracking_config": enriched["tracking"], "source_binding": None, "source_workspace": None,
            "execution": copy.deepcopy(enriched.get("execution")), "status": "pending", "errors": [],
            "dispatch_state": "preparing", "dispatch_retry_policy": "reconcile_only; never automatically relaunch this key"}


def _backend_error(error):
    if isinstance(error, ServiceError):
        return error
    return ServiceError(getattr(error, "code", "backend_error"),
                        str(error) if hasattr(error, "code") else "Execution backend could not complete the operation",
                        getattr(error, "details", {}),
                        {"capability_unavailable": 503, "not_found": 404, "forbidden": 403,
                         "conflict": 409}.get(getattr(error, "code", ""), 422 if hasattr(error, "code") else 502))


def _recheck(service, op, payload, actor, state):
    from .dataset_preparation import snapshot
    service._require_current_authority(op, actor, payload["project_id"])
    if snapshot(service, op, payload)[0] != state:
        raise ServiceError("run_preparation_changed", "Execution authority, revision, source or settings changed during preparation; retry explicitly", status_code=409)


def invoke(service, op, payload, actor, payload_hash, request_id):
    if op == "run.list":
        with service.store.transaction():
            service._require_current_authority(op, actor, payload["project_id"])
            service._get("project", payload["project_id"], payload["project_id"])
            records = service.store.list("operation", payload["project_id"])
        return {"runs": [_read(service, "run.status", {"project_id": payload["project_id"],
                         "operation_id": row["operation_id"]}, actor) for row in records]}
    if op in _READS:
        return _read(service, op, payload, actor)
    if op == "run.cancel":
        return _cancel(service, payload, actor, payload_hash, request_id)
    if op != "run.plan":
        with service.store.transaction():
            service._require_current_authority(op, actor, payload["project_id"])
            previous = service.store.idempotent_result(actor.id, payload["idempotency_key"], op, payload_hash)
            if previous is not None:
                return _receipt(service, previous)
    if not _PREPARERS.acquire(blocking=False):
        raise ServiceError("admission_busy", "Local admission preparation capacity is busy; retry explicitly", status_code=503)
    try:
        result = _prepare(service, op, copy.deepcopy(payload), actor, payload_hash, request_id)
    except Exception:
        with service.store.transaction():
            service._require_current_authority(op, actor, payload["project_id"])
            service._get("project", payload["project_id"], payload["project_id"])
        raise
    finally:
        _PREPARERS.release()
    # The launch action was authorized at its fence. A revoked caller still must
    # not receive private reply data after the unlocked launch/reconciliation.
    with service.store.transaction():
        service._require_current_authority(op, actor, payload["project_id"])
        if op != "run.plan":
            service._find_operation(payload["project_id"], result["operation_id"])
    return result


def _prepare(service, op, payload, actor, payload_hash, request_id):
    from .dataset_preparation import prepare, snapshot
    from .dataset import materialization_session, recheck_materialized_sources
    from .backend.placement import probe_session
    from .dataset_binding import resolve_backend
    mutating = op != "run.plan"
    owner = new_id()
    operation_id = None
    fenced = False
    launch_entered = False
    authorization_denied = False
    with service.store.transaction():
        service._require_current_authority(op, actor, payload["project_id"])
        if mutating:
            previous = service.store.idempotent_result(actor.id, payload["idempotency_key"], op, payload_hash)
            if previous is not None:
                return _receipt(service, previous)
        enriched = service._execution(op, payload, actor, prepare_only=True)
        state = snapshot(service, op, payload)[0]
        if mutating:
            operation_id = enriched["operation_id"]
            record = _record(enriched)
            service.store.put("operation", operation_id, payload["project_id"], record)
            service.store.put("dispatch_intent", operation_id, payload["project_id"], {
                "operation_id": operation_id, "project_id": payload["project_id"], "revision": 1,
                "owner": owner, "phase": "preparing", "cancel_requested": False,
                "actor_id": actor.id, "operation": op, "payload_hash": payload_hash,
                "restart_policy": "reconcile_only_no_implicit_launch"})
            # Reserve the ordinary global actor/key namespace before any slow work.
            service.store.save_idempotent(actor.id, payload["idempotency_key"], op, payload_hash, record)
            service.store.audit(actor_id=actor.id, operation=op, request_id=request_id,
                                outcome="reserved", project_id=payload["project_id"], target_id=operation_id)
    try:
        dataset_state, values, _, probes = prepare(service, op, payload, actor, payload_hash, reserved=mutating)
        if dataset_state != state:
            raise ServiceError("run_preparation_changed", "Execution changed before preparation", status_code=409)
        with service.store.transaction():
            _recheck(service, op, payload, actor, state)
        with materialization_session(values, compute=False), probe_session(probes, compute=False):
            if not mutating:
                result = service._call_backend(op, enriched, actor)
            else:
                backend = service._source_backend(op, enriched)
                if backend is None:
                    raise ServiceError("capability_unavailable", "No execution backend has been configured", status_code=503)
                backend = resolve_backend(service, backend, enriched)
                backend = service.hub.resolve_backend(backend, enriched)
                method = getattr(backend, "prepare_execution", None)
                if not callable(method):
                    raise ServiceError("capability_unavailable", "Execution backend requires separated preparation and fenced dispatch", status_code=503)
                prepared = method(op, enriched)
        recheck_materialized_sources(values)
        with service.store.transaction():
            _recheck(service, op, payload, actor, state)
            if not mutating:
                return {**result, "tracking": disclosure(enriched["tracking"])}
            intent = _intent(service, operation_id)
            if intent["owner"] != owner or intent["phase"] != "preparing" or intent["cancel_requested"]:
                return service.store.get("operation", operation_id)
            _put(service, "dispatch_intent", intent, {"phase": "ready"})
            record = service.store.get("operation", operation_id)
            _put(service, "operation", record, {"dispatch_state": "ready",
                 "source_binding": copy.deepcopy(enriched.get("source_binding")),
                 "source_workspace": copy.deepcopy(enriched.get("source_workspace"))})

        def authorize():
            nonlocal fenced, authorization_denied
            # Called under the backend's per-job launch lock, never its initialize lock.
            # No Git, hashing, subprocess, allocation or materialization occurs here.
            try:
                with service.store.transaction():
                    _recheck(service, op, payload, actor, state)
                    intent = _intent(service, operation_id)
                    if intent["owner"] != owner or intent["phase"] != "ready" or intent["cancel_requested"]:
                        raise ServiceError("dispatch_cancelled", "Dispatch was cancelled or lost ownership", status_code=409)
                    if op == "run.resume":
                        recipe = enriched["recipe"]
                        service.store.put("recipe", recipe["recipe_id"], payload["project_id"], recipe,
                                          payload["expected_recipe_revision"])
                        # The accepted continuation revision shares the dispatch fence.
                        # It remains immutable history if OS launch later becomes uncertain.
                    _put(service, "dispatch_intent", intent, {"phase": "dispatching"})
                    record = service.store.get("operation", operation_id)
                    _put(service, "operation", record, {"dispatch_state": "dispatching"})
                fenced = True
            except Exception:
                authorization_denied = True
                raise

        launch_entered = True
        result = backend.launch_prepared(prepared, authorize=authorize)
        service._check_run_result(result, enriched)
        with service.store.transaction():
            intent = _intent(service, operation_id)
            cancel = intent["cancel_requested"]
            record = service.store.get("operation", operation_id)
            merged = _merge(service, record, result, cancel=cancel)
            _put(service, "dispatch_intent", intent, {"phase": merged["dispatch_state"]})
            service.store.connection.execute("UPDATE idempotency SET result=? WHERE actor_id=? AND key=?",
                (json.dumps(merged, allow_nan=False), actor.id, payload["idempotency_key"]))
            service.store.audit(actor_id=actor.id, operation=op, request_id=request_id, outcome="succeeded",
                                project_id=payload["project_id"], target_id=operation_id)
        if cancel:
            # Cancellation committed after the dispatch fence must still reach the process.
            return _finish_cancel(service, backend, merged, actor)
        return merged
    except Exception as exc:
        error = _backend_error(exc)
        if operation_id is not None:
            with service.store.transaction():
                intent = _intent(service, operation_id)
                record = service.store.get("operation", operation_id)
                if record["status"] == "cancelled" and error.code == "dispatch_cancelled":
                    return record
                if intent and intent["owner"] == owner and record["status"] not in {"succeeded", "failed", "cancelled"}:
                    uncertain = fenced or (launch_entered and not authorization_denied)
                    phase = "uncertain" if uncertain else "failed"
                    _put(service, "dispatch_intent", intent, {"phase": phase})
                    _put(service, "operation", record, {"status": "unknown" if uncertain else "failed",
                         "dispatch_state": phase, "errors": [{"code": error.code, "message": error.message}]})
                    error.details = {**error.details, "operation_id": operation_id, "dispatch_state": phase,
                                     "retryable": False, "retry_semantics": "same_key_returns_existing_intent"}
        raise error from exc


def _merge(service, record, result, *, cancel=False):
    if result.get("operation_id", record["operation_id"]) != record["operation_id"]:
        raise ServiceError("backend_contract_error", "Backend changed operation identity", status_code=502)
    known, returned = record.get("backend_run_id"), result.get("backend_run_id")
    if known and returned and known != returned:
        raise ServiceError("backend_contract_error", "Backend changed an authoritative run identity", status_code=502)
    if record["status"] in {"succeeded", "failed", "cancelled"} and result.get("status") != record["status"]:
        return record  # An older unlocked RPC/launch reply cannot regress a durable terminal observation.
    service._check_run_result(result, record, record)
    status = result.get("status", "unknown")
    if cancel and status in {"pending", "submitted", "running", "unknown"}:
        status = "cancel_requested"
    stored = _put(service, "operation", record, {
        "backend_run_id": result.get("backend_run_id") or record.get("backend_run_id"),
        **({"execution": copy.deepcopy(result["execution"])} if "execution" in result and record.get("dispatch_state") in {"ready", "dispatching"} else {}),
        "status": status, "last_reconciled_at": utcnow(),
        **({"dispatch_state": "uncertain" if result.get("launch_state") == "uncertain" else "dispatched"}
           if "dispatch_state" in record else {}),
        "errors": result.get("errors") or ([result["error"]] if result.get("error") else [])})
    return {**result, **stored}



def _read(service, op, payload, actor):
    with service.store.transaction():
        service._require_current_authority(op, actor, payload["project_id"])
        record = service._find_operation(payload["project_id"], payload.get("operation_id"), payload.get("backend_run_id"))
        intent = _intent(service, record["operation_id"])
        if intent and intent["phase"] in {"preparing", "ready", "failed", "cancelled"}:
            if op == "run.status":
                return record
            if op == "run.events.list":
                return {"operation_id": record["operation_id"], "events": [], "next_cursor": payload.get("after", 0), "has_more": False}
            if op == "run.evaluations.list":
                from .backend.live_evaluation import SCHEMA, STATUSES
                result = {"schema": SCHEMA, "evaluations": [], "checkpoints": [], "summary": dict.fromkeys(STATUSES, 0)}
                if payload.get("limit") is not None:
                    result.update(limit=payload["limit"], total_evaluations=0, truncated=False)
                return result
            if op == "run.logs":
                return {"operation_id": record["operation_id"], "logs": "", "cursor": "0"}
            return {"operation_id": record["operation_id"], "artifacts": []}
        enriched = {**payload, "operation_id": record["operation_id"], "backend_run_id": record.get("backend_run_id")}
    try:
        result = service._call_backend(op, enriched, actor)
    except ServiceError as error:
        with service.store.transaction():
            service._require_current_authority(op, actor, payload["project_id"])
            service._find_operation(payload["project_id"], record["operation_id"], payload.get("backend_run_id"))
        if intent and intent["phase"] in {"dispatching", "uncertain"} and error.code == "not_found" and op == "run.status":
            with service.store.transaction():
                service._require_current_authority(op, actor, payload["project_id"])
                latest = service._find_operation(payload["project_id"], record["operation_id"], payload.get("backend_run_id"))
                if latest["status"] in {"succeeded", "failed", "cancelled"}:
                    return latest
                return {**latest, "status": "unknown", "dispatch_state": "uncertain"}
        raise
    if op != "run.status":
        with service.store.transaction():
            service._require_current_authority(op, actor, payload["project_id"])
            service._find_operation(payload["project_id"], record["operation_id"], payload.get("backend_run_id"))
        return result
    service._check_run_result(result, enriched, record)
    with service.store.transaction():
        service._require_current_authority(op, actor, payload["project_id"])
        latest = service._find_operation(payload["project_id"], record["operation_id"], payload.get("backend_run_id"))
        intent = _intent(service, record["operation_id"])
        if latest["status"] in {"succeeded", "failed", "cancelled"} and latest["revision"] != record["revision"]:
            return latest
        merged = _merge(service, latest, result, cancel=bool(intent and intent["cancel_requested"]))
        if intent and intent["phase"] != merged.get("dispatch_state", intent["phase"]):
            _put(service, "dispatch_intent", intent, {"phase": merged["dispatch_state"]})
        return merged


def _cancel(service, payload, actor, payload_hash, request_id):
    with service.store.transaction():
        service._require_current_authority("run.cancel", actor, payload["project_id"])
        previous = service.store.idempotent_result(actor.id, payload["idempotency_key"], "run.cancel", payload_hash)
        if previous is not None:
            return _receipt(service, previous)
        record = service._find_operation(payload["project_id"], payload.get("operation_id"), payload.get("backend_run_id"))
        intent = _intent(service, record["operation_id"])
        if record["status"] in {"succeeded", "failed", "cancelled"}:
            service.store.save_idempotent(actor.id, payload["idempotency_key"], "run.cancel", payload_hash, record)
            return record
        prevented = bool(intent and intent["phase"] in {"preparing", "ready", "cancelled", "failed"})
        if intent:
            _put(service, "dispatch_intent", intent, {"cancel_requested": True,
                 "phase": "cancelled" if prevented else intent["phase"]})
        record = _put(service, "operation", record, {"status": "cancelled" if prevented else "cancel_requested",
                      **({"dispatch_state": "cancelled", "launch_prevented": True} if prevented else {})})
        service.store.save_idempotent(actor.id, payload["idempotency_key"], "run.cancel", payload_hash, record)
        service.store.audit(actor_id=actor.id, operation="run.cancel", request_id=request_id, outcome="succeeded",
                            project_id=payload["project_id"], target_id=record["operation_id"])
    if prevented:
        return record
    return _finish_cancel(service, service.backend, record, actor, payload)


def _finish_cancel(service, backend, record, actor, payload=None):
    try:
        result = backend.invoke("run.cancel", {**(payload or {}), "project_id": record["project_id"],
                               "operation_id": record["operation_id"], "backend_run_id": record.get("backend_run_id")}, actor)
    except Exception as error:
        with service.store.transaction():
            service._require_current_authority("run.cancel", actor, record["project_id"])
            service._find_operation(record["project_id"], record["operation_id"])
        if getattr(error, "code", None) == "not_found":
            # Launch may be between the committed fence and request persistence;
            # a concurrent native reconciliation may already have finished meanwhile.
            with service.store.transaction():
                service._require_current_authority("run.cancel", actor, record["project_id"])
                return service._find_operation(record["project_id"], record["operation_id"])
        raise _backend_error(error) from error
    service._check_run_result(result, record)
    with service.store.transaction():
        service._require_current_authority("run.cancel", actor, record["project_id"])
        latest = service._find_operation(record["project_id"], record["operation_id"])
        if latest["status"] in {"succeeded", "failed", "cancelled"}:
            return latest
        merged = _merge(service, latest, result, cancel=True)
        intent = _intent(service, record["operation_id"])
        if intent and intent["phase"] != merged.get("dispatch_state", intent["phase"]):
            _put(service, "dispatch_intent", intent, {"phase": merged["dispatch_state"]})
        return merged
