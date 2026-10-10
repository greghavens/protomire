"""Unlocked dataset/device preparation followed by checked atomic dispatch."""
from __future__ import annotations

import copy

from .dataset import materialization_session, recheck_materialized_sources
from .models import ServiceError

OPERATIONS = {"dataset.register", "dataset.version", "dataset.validate", "dataset.preview", "dataset.bind",
              "run.plan", "run.submit", "run.resume"}


def snapshot(service, operation, payload):
    project_id = payload["project_id"]
    registry = service._dataset_registry
    project = service._get("project", project_id, project_id)
    state = {"project": project, "roots": copy.deepcopy(registry._roots.get(project_id, {}))}
    declarations, enriched = [], None
    if operation.startswith("dataset."):
        if operation in {"dataset.register", "dataset.version"}:
            declarations.append(payload["manifest"])
        if operation == "dataset.version":
            state["dataset"] = registry.get(project_id, payload["dataset_id"])
            service._revision(state["dataset"], payload["expected_revision"])
        elif operation in {"dataset.preview", "dataset.bind"} or (
                operation == "dataset.validate" and "dataset_id" in payload and "manifest" not in payload):
            revision = payload.get("dataset_revision") if operation == "dataset.bind" else payload.get("revision")
            state["dataset"] = registry.get(project_id, payload["dataset_id"], revision)
            declarations.append(state["dataset"]["manifest"])
        elif operation == "dataset.validate" and "manifest" in payload and "dataset_id" not in payload and "revision" not in payload:
            declarations.append(payload["manifest"])
        if operation == "dataset.bind":
            state["recipe"] = service._current_recipe(project_id, payload["recipe_id"])
            service._revision(state["recipe"], payload["expected_revision"])
    else:
        from .backend.placement import backend_identity
        state["policy"] = copy.deepcopy(service._policy("policy.get", payload))
        state["source_settings"] = copy.deepcopy(service.source_workspaces.aliases)
        state["require_source_binding"] = service.require_source_binding
        state["backend"] = backend_identity(service.backend)
        state["backend_instance"] = id(service.backend)
        from .execution import continuation_selection, selection
        recipe = service._current_recipe(project_id, payload.get("recipe_id"))
        state["current_recipe"] = recipe
        if operation == "run.resume":
            service._revision(recipe, payload["expected_recipe_revision"])
            parent = service._find_operation(project_id, payload.get("operation_id"), payload.get("backend_run_id"))
            state["parent"] = {key: parent.get(key) for key in ("operation_id", "backend_run_id", "recipe_id", "recipe_revision",
                "recipe_digest", "execution", "source_binding")}
            previous = service._get("recipe", parent["recipe_id"], project_id, parent["recipe_revision"])
            state["parent_recipe"] = previous
            mode = payload.get("mode", payload.get("resume_mode"))
            if mode not in {"exact_state", "warm_start"} or payload.get("trusted") is not True:
                return state, declarations, None
            recipe = recipe if mode == "warm_start" else previous
            execution = continuation_selection(previous, recipe, parent.get("execution"), mode)
            enriched = {**payload, "parent_operation_id": parent["operation_id"]}
        else:
            if "expected_revision" in payload:
                service._revision(recipe, payload["expected_revision"])
            if operation == "run.submit" and payload.get("trusted") is not True:
                return state, declarations, None
            execution = selection(recipe, payload.get("execution"))
            enriched = dict(payload)
        enriched.update(recipe=recipe, execution=execution)
        # Immutable explicit versions may be superseded without changing their
        # pinned contents; recipe/source/project state itself must stay exact.
        state["datasets"] = []
        selected = set(execution["node_ids"])
        declared = {item["id"]: item for item in recipe.get("resources", []) + recipe.get("external_dependencies", [])}
        for node in recipe["nodes"]:
            if node["id"] not in selected or node["kind"] != "dataset.version.import":
                continue
            reference_id = node.get("parameters", {}).get("reference_id")
            if not isinstance(reference_id, str):
                continue
            reference = declared.get(reference_id, {}).get("metadata", {}).get("dataset_reference")
            if (isinstance(reference, dict) and reference.get("project_id") == project_id
                    and isinstance(reference.get("dataset_id"), str)
                    and type(reference.get("revision")) is int and reference["revision"] > 0):
                try:
                    record = registry.get(project_id, reference.get("dataset_id"), reference.get("revision"))
                except (ServiceError, TypeError):
                    continue  # The ordinary admission path returns its existing structured diagnostic.
                state["datasets"].append(record)
    return state, declarations, enriched


def prepare(service, operation, payload, actor, payload_hash, *, reserved=False):
    with service.store.transaction():
        service._require_current_authority(operation, actor, payload["project_id"])
        if "idempotency_key" in payload and not reserved:
            previous = service.store.idempotent_result(actor.id, payload["idempotency_key"], operation, payload_hash)
            if previous is not None:
                service._check_idempotent_recipe_identity(operation, previous)
                return None, {}, previous, {}
        state, declarations, enriched = snapshot(service, operation, payload)
    from .backend.placement import probe_session
    entries, probes = {}, {}
    with materialization_session(entries, compute=True), probe_session(probes, compute=True):
        for declaration in declarations:
            try:
                service._dataset_registry._materialize(payload["project_id"], declaration)
            except ServiceError:
                pass  # Replay the same error inside normal validate/mutation semantics.
        if enriched is not None and service.backend is not None:
            from .backend.adapter import BackendError
            from .backend.admission import validate_dataset_import
            from .dataset_binding import resolve_backend
            try:
                backend = service._source_backend(operation, enriched)
                backend = resolve_backend(service, backend, enriched)
                from .backend.adapter import ZenMLBackend
                from .backend.placement import backend_identity, device_from_recipe, target_for_device, validate_placement
                target = (target_for_device(device_from_recipe(enriched["recipe"])) if operation == "run.resume"
                          else payload.get("target", "local-cpu"))
                enriched["target"] = target
                # Only a currently authorized, explicitly trusted, policy-permitted
                # execution request may prepare a tensor allocation. Plan is read-only.
                if (isinstance(backend, ZenMLBackend) and enriched.get("trusted") is True
                        and target in state["policy"]["allowed_targets"]):
                    service._require_recipe_review(payload["project_id"])
                    prepared_backend_identity = backend_identity(backend)
                    validate_placement(backend, enriched)
                    if operation in {"run.submit", "run.resume"}:
                        # A prior metadata probe or dataset decode may have been
                        # slow. Fence authority and settings again before the
                        # separately authorized tiny allocation, never inside it.
                        # Re-resolve the approved request checkout and dataset
                        # bytes after metadata discovery, outside the writer lock.
                        # The base backend may point at a different repository.
                        recheck_materialized_sources(entries)
                        refreshed = service._source_backend(operation, copy.deepcopy(enriched))
                        refreshed = resolve_backend(service, refreshed, enriched)
                        if backend_identity(refreshed) != prepared_backend_identity:
                            raise ServiceError("placement_preparation_changed", "Prepared source or interpreter identity changed before allocation; retry explicitly", status_code=409)
                        with service.store.transaction():
                            service._require_current_authority(operation, actor, payload["project_id"])
                            if snapshot(service, operation, payload)[0] != state:
                                raise ServiceError("placement_preparation_changed", "Authority or execution settings changed before the allocation probe; retry explicitly", status_code=409)
                        validate_placement(backend, enriched, allocate=True)
                selected = set(enriched["execution"]["node_ids"])
                for node in enriched["recipe"]["nodes"]:
                    if node["id"] in selected and node["kind"] == "dataset.version.import":
                        validate_dataset_import(backend, enriched, node)
            except (ServiceError, BackendError):
                pass  # Plan retains blocked_reasons; submit retains its refusal.
    recheck_materialized_sources(entries)
    return state, entries, None, probes
