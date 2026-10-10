"""Public recipe bindings preserve immutable data and keep runtime authority local."""
import copy
import json
from pathlib import Path

import httpx
import pytest

from modelbuilder.api import create_app
from modelbuilder.dataset import load_portable_split
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService
from test_registry import make_manifest

OWNER = Actor("binding-owner", {"read", "modify", "execute"})


class InspectBackend:
    def __init__(self, root):
        self.root, self.dependency_paths = root, {}
        self.calls = []

    def with_dataset_dependencies(self, paths, guards):
        result = copy.copy(self)
        result.dependency_paths = dict(paths)
        result.guards = copy.deepcopy(guards)
        return result

    def invoke(self, operation, payload, actor):
        self.calls.append((operation, copy.deepcopy(payload), self.dependency_paths, self.guards))
        return {"executable": True}


@pytest.fixture
def bound(tmp_path):
    service = OperationService(tmp_path / "service.sqlite3")
    project = service.invoke("project.create", {"slug": "binding", "model_name": "Bound data", "idempotency_key": "project"}, OWNER)
    service.store.close()
    root = tmp_path / "external"
    root.mkdir()
    backend = InspectBackend(tmp_path / "runtime")
    service = OperationService(tmp_path / "service.sqlite3", backend=backend,
                               dataset_roots={project["project_id"]: {"data": str(root)}})
    version = service.invoke("dataset.register", {"project_id": project["project_id"], "manifest": make_manifest(root),
                             "idempotency_key": "register"}, OWNER)
    payload = {"project_id": project["project_id"], "recipe_id": project["recipe_id"], "expected_revision": 1,
               "dataset_id": version["dataset_id"], "dataset_revision": 1, "split_id": "train", "purpose": "training",
               "node_id": "train_data", "idempotency_key": "bind"}
    yield service, project, version, payload, root, backend
    service.store.close()


def test_binding_is_revisioned_idempotent_portable_and_does_not_execute(bound):
    service, project, version, payload, root, backend = bound
    one = service.invoke("dataset.bind", payload, OWNER)
    assert one["execution_started"] is False and not backend.calls
    assert one["recipe_id"] == project["recipe_id"] and one["revision"] == 2
    assert service.invoke("dataset.bind", payload, OWNER) == one
    recipe = one["recipe"]
    assert len(recipe["nodes"]) == 1 and len(recipe["external_dependencies"]) == 2
    assert one["binding"]["version_id"] == version["version_id"]
    assert str(root) not in json.dumps(recipe) and "sunny day" not in json.dumps(recipe)
    ref = next(item for item in recipe["external_dependencies"] if "dataset_reference" in item["metadata"])
    assert ref["metadata"]["dataset_reference"]["manifest"]["provenance"] == version["manifest"]["provenance"]
    assert ref["license"] == version["license"] and ref["redistribution_allowed"] is False
    assert service.invoke("recipe.get", {"project_id": project["project_id"], "recipe_id": project["recipe_id"], "revision": 1}, OWNER)["nodes"] == []
    with pytest.raises(ServiceError) as stale:
        service.invoke("dataset.bind", {**payload, "idempotency_key": "stale"}, OWNER)
    assert stale.value.code == "revision_conflict" and stale.value.status_code == 409
    assert stale.value.details["current"]["revision"] == 2


def test_current_version_changes_do_not_redirect_bound_revision_or_operator_maps(bound):
    service, project, version, payload, root, backend = bound
    one = service.invoke("dataset.bind", payload, OWNER)
    manifest = copy.deepcopy(version["manifest"])
    manifest["description"] = "New immutable declaration"
    service.invoke("dataset.version", {"project_id": project["project_id"], "dataset_id": version["dataset_id"],
                  "expected_revision": 1, "manifest": manifest, "idempotency_key": "next"}, OWNER)
    assert service.invoke("run.plan", {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}, OWNER)["executable"]
    assert backend.dependency_paths == {}  # request-local copy only
    _, recipe_payload, paths, guards = backend.calls[-1]
    importer = recipe_payload["recipe"]["nodes"][0]
    reference = json.loads(Path(paths[importer["parameters"]["reference_id"]]).read_text())
    assert reference["revision"] == 1 and reference["version_id"] == version["version_id"]
    consumed = load_portable_split(reference, paths, "train", purpose="training", source_guards=guards)
    assert consumed["consumption"]["version_id"] == one["binding"]["version_id"]
    assert consumed["rows"][0]["text"] == "sunny day"
    assert not list(backend.root.rglob("*.jsonl"))  # cache contains metadata only
    (root / "weather.jsonl").write_text("tampered")
    failed = service.invoke("run.plan", {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}, OWNER)
    assert not failed["executable"] and failed["blocked_reasons"][0]["code"] == "dataset_checksum_mismatch"


@pytest.mark.parametrize("mutation,code", [({"split_id": "dev"}, "dataset_split_purpose_invalid"),
    ({"target_node": "missing"}, "validation_error"), ({"target_node": "missing", "target_port": "data"}, "dataset_binding_invalid"),
    ({"filesystem_path": "/etc/passwd"}, "validation_error")])
def test_invalid_bindings_leave_recipe_unchanged(bound, mutation, code):
    service, project, _, payload, _, backend = bound
    with pytest.raises(ServiceError) as failure:
        service.invoke("dataset.bind", {**payload, **mutation}, OWNER)
    assert failure.value.code == code
    assert service.invoke("project.open", {"project_id": project["project_id"]}, OWNER)["recipe"]["revision"] == 1
    assert not backend.calls


def test_binding_scope_and_cross_project_are_enforced(bound):
    service, project, version, payload, _, _ = bound
    for actor in [Actor("reader", {"read"}), Actor("executor", {"read", "execute"}), Actor("foreign", {"modify"}, {"another-project"})]:
        with pytest.raises(ServiceError) as failure:
            service.invoke("dataset.bind", payload, actor)
        assert failure.value.status_code == 403
    other = service.invoke("project.create", {"slug": "other", "model_name": "Other", "idempotency_key": "other"}, OWNER)
    with pytest.raises(ServiceError) as foreign:
        service.invoke("dataset.bind", {**payload, "project_id": other["project_id"], "recipe_id": other["recipe_id"],
                       "idempotency_key": "foreign"}, OWNER)
    assert foreign.value.code == "not_found"
    assert service.invoke("dataset.bind", payload, Actor("modifier", {"modify"}))["revision"] == 2


def test_forged_portable_metadata_and_runtime_root_replacement_fail_closed(bound, tmp_path):
    service, project, _, payload, root, backend = bound
    bound_recipe = service.invoke("dataset.bind", payload, OWNER)["recipe"]
    request = {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}
    assert service.invoke("run.plan", request, OWNER)["executable"]
    _, _, paths, guards = backend.calls[-1]
    ref = next(item for item in bound_recipe["external_dependencies"] if "dataset_reference" in item["metadata"])
    portable = ref["metadata"]["dataset_reference"]
    displaced = tmp_path / "displaced"
    root.rename(displaced)
    root.symlink_to(displaced, target_is_directory=True)
    with pytest.raises(ServiceError):
        load_portable_split(portable, paths, "train", purpose="training", source_guards=guards)
    root.unlink()
    displaced.rename(root)
    forged = copy.deepcopy(bound_recipe)
    dependency = next(item for item in forged["external_dependencies"] if "dataset_reference" in item["metadata"])
    dependency["metadata"]["dataset_reference"]["manifest"]["license"] = "Forged rights"
    service.invoke("recipe.update", {**request, "recipe": forged, "expected_revision": 2, "idempotency_key": "forge"}, OWNER)
    failed = service.invoke("run.plan", request, OWNER)
    assert not failed["executable"] and failed["blocked_reasons"][0]["code"] == "dataset_reference_invalid"


async def test_http_and_native_gateway_expose_one_exact_binding_contract(bound):
    from modelbuilder.mcp_server import catalog_tools
    service, project, _, payload, _, _ = bound
    app = create_app(service, {"owner": {"id": OWNER.id, "scopes": sorted(OWNER.scopes)}})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"Authorization": "Bearer owner"}) as client:
        response = await client.post("/v1/operations/dataset.bind", json=payload)
        assert response.status_code == 200, response.text
        catalog = (await client.get("/v1/catalog")).json()["operations"]
        assert next(item for item in catalog if item["name"] == "dataset.bind")["scope"] == "modify"
        assert "dataset_bind" in {item.name for item in catalog_tools({"operations": catalog})[0]}
        # Idempotency is shared across the exact public HTTP request and service dispatcher.
        assert response.json() == service.invoke("dataset.bind", payload, OWNER)


def test_operator_python_selection_preserves_the_virtual_environment_symlink(tmp_path):
    from modelbuilder.backend.operator_config import load_operator_config
    base = tmp_path / "base-python"
    base.write_text("an interpreter")
    environment = tmp_path / "venv" / "bin"
    environment.mkdir(parents=True)
    python = environment / "python"
    python.symlink_to(base)
    config = tmp_path / "operator.json"
    config.write_text(json.dumps({"python_executable": "venv/bin/python"}))
    assert load_operator_config(config)["python_executable"] == str(python)
    config.write_text(json.dumps({"python_executable": str(python)}))
    assert load_operator_config(config)["python_executable"] == str(python)


@pytest.mark.parametrize("change", ["schema", "source"])
def test_rebinding_new_identity_refuses_same_benchmark_comparison(bound, change):
    import hashlib
    from modelbuilder.evaluation import compare_records, make_evaluation_record
    service, project, version, payload, root, _ = bound
    first = service.invoke("dataset.bind", {**payload, "split_id": "dev", "purpose": "evaluation"}, OWNER)
    manifest = copy.deepcopy(version["manifest"])
    if change == "schema":
        manifest["row_schema"]["properties"]["text"]["description"] = "Explicit schema change"
    else:
        target = root / "weather-v2.jsonl"
        target.write_text((root / "weather.jsonl").read_text().replace("warm afternoon", "new afternoon"))
        manifest["sources"][0].update(path=target.name, sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    second_version = service.invoke("dataset.version", {"project_id": project["project_id"], "dataset_id": version["dataset_id"],
        "expected_revision": 1, "manifest": manifest, "idempotency_key": "new-version"}, OWNER)
    second = service.invoke("dataset.bind", {**payload, "split_id": "dev", "purpose": "evaluation", "dataset_revision": 2,
        "expected_revision": 2, "idempotency_key": "rebind"}, OWNER)
    assert second["binding"]["version_id"] == second_version["version_id"] != first["binding"]["version_id"]
    assert len(second["recipe"]["external_dependencies"]) == 2  # unused generated closure retired
    records = []
    for i, binding in enumerate([first["binding"], second["binding"]]):
        evidence = make_evaluation_record(dataset=binding["dataset"], split=binding["split"],
            protocol={"implementation": "independent-weather-evaluate", "version": "1", "configuration": {}, "preprocessing": {}, "seed": 1},
            metric={"name": "accuracy", "implementation": "python.equality", "version": "1", "parameters": {}, "aggregation": "mean", "direction": "maximize"},
            treatment={"model": f"model-{i}", "checkpoint": f"checkpoint-{i}", "calibration": None, "reference": None},
            value=.5 + i * .5, predictions=[{"item_id": "dev-1", "value": .5 + i * .5}])
        records.append({"backend_run_id": f"run-{i}", "status": "succeeded",
                        "metrics": {"accuracy": evidence["value"], "evaluation_records": {"accuracy": evidence}}})
    compared = compare_records(records, "accuracy", "maximize")
    assert not compared["compatible"] and compared["selected_run_id"] is None


def test_clean_bundle_import_preserves_portable_metadata_until_explicit_local_rebind(bound, tmp_path):
    service, project, version, payload, original_root, _ = bound
    first = service.invoke("dataset.bind", payload, OWNER)
    bundle = service.invoke("recipe.export", {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}, OWNER)
    assert str(original_root) not in json.dumps(bundle) and bundle["files"] == {}
    relocated = tmp_path / "different-machine-data"
    relocated.mkdir()
    (relocated / "weather.jsonl").write_bytes((original_root / "weather.jsonl").read_bytes())
    fresh = OperationService(tmp_path / "fresh.sqlite3")
    target = fresh.invoke("project.create", {"slug": "fresh", "model_name": "Replayed", "idempotency_key": "target"}, OWNER)
    fresh.store.close()
    backend = InspectBackend(tmp_path / "fresh-runtime")
    fresh = OperationService(tmp_path / "fresh.sqlite3", backend=backend,
                             dataset_roots={target["project_id"]: {"data": str(relocated)}})
    try:
        imported = fresh.invoke("recipe.import", {"project_id": target["project_id"], "expected_revision": 1,
                                "bundle": bundle, "idempotency_key": "import"}, OWNER)
        assert imported["external_dependencies"] == first["recipe"]["external_dependencies"]
        request = {"project_id": target["project_id"], "recipe_id": target["recipe_id"]}
        blocked = fresh.invoke("run.plan", request, OWNER)
        assert not blocked["executable"] and blocked["blocked_reasons"][0]["code"] == "dataset_reference_invalid"
        registered = fresh.invoke("dataset.register", {"project_id": target["project_id"], "manifest": version["manifest"],
                                  "idempotency_key": "local-data"}, OWNER)
        rebound = fresh.invoke("dataset.bind", {**payload, **request, "expected_revision": 2,
                               "dataset_id": registered["dataset_id"], "idempotency_key": "local-bind"}, OWNER)
        assert rebound["recipe_id"] == target["recipe_id"] and rebound["revision"] == 3
        assert fresh.invoke("run.plan", request, OWNER)["executable"]
        _, _, paths, guards = backend.calls[-1]
        portable = json.loads(Path(paths[rebound["binding"]["reference_id"]]).read_text())
        consumed = load_portable_split(portable, paths, "train", purpose="training", source_guards=guards)
        assert consumed["dataset"]["content_digest"] == version["content_digest"]
        assert all(str(original_root) not in value for value in paths.values())
        assert original_root.as_posix() not in json.dumps(rebound["recipe"])
    finally:
        fresh.store.close()


@pytest.mark.parametrize("mutation", ["parameters", "reference_id", "revision", "dataset_id"])
def test_malformed_edited_dataset_bindings_fail_with_structured_plan_diagnostics(bound, mutation):
    service, project, _, payload, _, _ = bound
    recipe = service.invoke("dataset.bind", payload, OWNER)["recipe"]
    if mutation == "parameters":
        recipe["nodes"][0]["parameters"] = {"reference_id": "anything", "split_id": "train", "purpose": []}
    elif mutation == "reference_id":
        recipe["nodes"][0]["parameters"]["reference_id"] = []
    else:
        reference = next(item["metadata"]["dataset_reference"] for item in recipe["external_dependencies"]
                         if "dataset_reference" in item["metadata"])
        reference[mutation] = {}
    request = {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}
    service.invoke("recipe.update", {**request, "recipe": recipe, "expected_revision": 2, "idempotency_key": "edit"}, OWNER)
    result = service.invoke("run.plan", request, OWNER)
    assert not result["executable"]
    assert result["blocked_reasons"][0]["code"] in {"validation_error", "dataset_reference_invalid"}


def test_reference_cache_symlink_is_refused_without_reading_its_target(bound, tmp_path):
    service, project, _, payload, _, backend = bound
    result = service.invoke("dataset.bind", payload, OWNER)
    request = {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}
    assert service.invoke("run.plan", request, OWNER)["executable"]
    reference_path = Path(backend.calls[-1][2][result["binding"]["reference_id"]])
    secret = tmp_path / "unapproved"
    secret.write_text("not dataset metadata")
    reference_path.unlink()
    reference_path.symlink_to(secret)
    failed = service.invoke("run.plan", request, OWNER)
    assert not failed["executable"]
    assert failed["blocked_reasons"][0]["code"] == "dataset_reference_unavailable"
    assert secret.read_text() == "not dataset metadata"


def test_checkpoint_only_request_does_not_read_unselected_dataset_sources(bound):
    from modelbuilder.dataset_binding import resolve_backend
    service, project, _, payload, root, backend = bound
    current = service.invoke('dataset.bind', payload, OWNER)['recipe']
    current['nodes'].append({'id': 'evaluate_saved', 'kind': 'component.run'})
    # A completed artifact-bound evaluation is independent of the old data root.
    (root / 'weather.jsonl').unlink()
    result = resolve_backend(service, backend, {'project_id': project['project_id'], 'recipe': current,
        'execution': {'node_ids': ['evaluate_saved'], 'input_bindings': []}})
    assert result is backend and backend.dependency_paths == {}
    with pytest.raises(ServiceError):
        resolve_backend(service, backend, {'project_id': project['project_id'], 'recipe': current,
            'execution': {'node_ids': ['train_data'], 'input_bindings': []}})


def test_public_cut_resume_ignores_missing_unselected_dataset_file(bound, monkeypatch):
    service, project, _, payload, root, backend = bound
    current = service.invoke("dataset.bind", payload, OWNER)["recipe"]
    current["nodes"].append({"id": "saved_policy", "kind": "model.train", "implementation_ref": "builtin:model.train",
        "inputs": [], "outputs": [], "parameters": {"steps": 4}, "environment_ref": "default", "resource_request": {},
        "retry": {"max_attempts": 1, "backoff_seconds": 0}, "timeout": 30, "cache_policy": "disabled", "provenance": {}})
    common = {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}
    current = service.invoke("recipe.update", {**common, "recipe": current, "expected_revision": 2, "idempotency_key": "add-policy"}, OWNER)
    calls = []
    def invoke(operation, request, actor):
        calls.append((operation, copy.deepcopy(request)))
        return {"operation_id": request.get("operation_id"), "backend_run_id": "native-" + request.get("operation_id", "plan"), "status": "submitted"}
    monkeypatch.setattr(backend, "invoke", invoke)
    parent = service.invoke("run.submit", {**common, "expected_revision": current["revision"], "trusted": True,
        "execution": {"node_ids": ["saved_policy"], "input_bindings": []}, "idempotency_key": "policy"}, OWNER)
    (root / "weather.jsonl").unlink()
    assert not service.invoke("run.plan", common, OWNER)["executable"]
    result = service.invoke("run.resume", {"project_id": project["project_id"], "operation_id": parent["operation_id"],
        "expected_recipe_revision": current["revision"], "mode": "exact_state", "steps": 6, "trusted": True, "idempotency_key": "resume"}, OWNER)
    assert result["status"] == "submitted"
    assert calls[-1][0] == "run.resume" and calls[-1][1]["execution"] == {"node_ids": ["saved_policy"], "input_bindings": []}
