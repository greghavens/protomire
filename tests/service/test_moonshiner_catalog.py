"""Moonshiner discovery shares the inert HTTP/MCP component catalog."""
import httpx
import pytest

from modelbuilder.api import create_app
from modelbuilder.backend.components.moonshiner import catalog_entries
from modelbuilder.mcp_server import OperationGateway
from modelbuilder.models import Actor
from modelbuilder.service import OperationService


@pytest.mark.asyncio
async def test_moonshiner_http_mcp_catalog_parity_without_upstream(tmp_path):
    service = OperationService(tmp_path / "catalog.sqlite")
    actor = Actor("owner", {"read", "modify"})
    project = service.invoke("project.create", {"slug": "moonshiner", "model_name": "Dataset compose",
        "idempotency_key": "project"}, actor)
    payload = {"project_id": project["project_id"]}
    app = create_app(service=service, tokens={"test-only-owner": actor})
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"Authorization": "Bearer test-only-owner"}) as client:
            gateway = OperationGateway(client)
            result = await client.post("/v1/operations/component.list", json=payload)
            assert result.status_code == 200
            assert await gateway.invoke("component.list", payload) == result.json()
            entry = catalog_entries()[0]
            assert entry in result.json()["components"]
            request = {**payload, "component_id": entry["id"]}
            result = await client.post("/v1/operations/component.get", json=request)
            assert result.status_code == 200
            assert result.json() == await gateway.invoke("component.get", request) == entry
            assert "implementation_ref" not in entry
            assert entry["adapter"]["example_source"] == "examples/moonshiner/adapter.py"
    finally:
        service.store.close()


def test_catalog_accepts_ordinary_registered_dataset_target_binding(tmp_path):
    """The public dict import port and advertised Moonshiner port match exactly."""
    import hashlib
    import json
    from modelbuilder.backend.components.moonshiner import UPSTREAM_COMMIT, UPSTREAM_VERSION
    from modelbuilder.dataset import DatasetRegistry
    from modelbuilder.recipe import validate_recipe

    service = OperationService(tmp_path / "binding.sqlite")
    actor = Actor("owner", {"read", "modify"})
    project = service.invoke("project.create", {"slug": "compose-binding", "model_name": "Compose binding",
        "idempotency_key": "create"}, actor)
    project_id = project["project_id"]
    def call(operation, payload):
        return service.invoke(operation, {"project_id": project_id, **payload}, actor)
    try:
        source = tmp_path / "traces.jsonl"
        source.write_text(json.dumps({"id": "owned-row", "text": "synthetic fixture"}) + "\n")
        service._dataset_registry = DatasetRegistry(service.store, project_roots={project_id: {"data": str(tmp_path)}})
        dataset = call("dataset.register", {"idempotency_key": "register", "manifest": {
            "name": "Owned rows", "benchmark": "owned", "license": "test-only",
            "provenance": {"source": "Test-authored fixture"}, "id_column": "id",
            "sources": [{"id": "traces", "root_id": "data", "path": source.name, "format": "jsonl",
                         "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}],
            "row_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
            "splits": [{"id": "train", "role": "train", "source_ids": ["traces"]}], "transformations": []}})
        entry = call("component.get", {"component_id": "modelbuilder.moonshiner.prepare-dataset"})
        assert entry["inputs"] == [{"name": "traces", "type": "dict"}]
        parameters = {"mode": "offline-compose", "installation": {"version": UPSTREAM_VERSION,
            "reviewed_upstream_commit": UPSTREAM_COMMIT, "source_sha256": "1" * 64, "environment_sha256": "2" * 64},
            "traces_digest": "3" * 64, "teacher": {"model": "owned", "runtime": "fixture", "rights_parent_id": "teacher"},
            "limits": {"max_records": 4, "max_bytes": 1024, "timeout_seconds": 5, "max_cost_usd": 0},
            "seed": 17, "name": "Composed", "benchmark": "other", "license": "test-only", "root_id": "prepared",
            "review_note": "Binding-only fixture; no execution grant", "split_policy": "train-only",
            "label_policy": "final-assistant-content-only"}
        descriptor = {key: entry[key] for key in ("id", "version", "protocol", "role", "description", "parameter_schema", "capabilities")}
        recipe = call("recipe.get", {"recipe_id": project["recipe_id"]})
        recipe["nodes"] = [{"id": "compose", "kind": "component.run", "implementation_ref": {
            "module": "owned_adapter", "entrypoint": "run", "path": "adapter.py", "commit": "1" * 40, "sha256": "4" * 64},
            "inputs": entry["inputs"], "outputs": entry["outputs"], "component": descriptor, "parameters": parameters,
            "environment_ref": "default", "resource_request": {"gpu": 0}, "timeout": 30,
            "retry": {"max_attempts": 1, "backoff_seconds": 0}, "cache_policy": "disabled", "provenance": {}}]
        recipe["edges"] = []
        recipe["source"] = {"repository": "urn:sha256:" + "4" * 64, "commit": "1" * 40,
                            "files": [{"path": "adapter.py", "sha256": "4" * 64}]}
        recipe = call("recipe.update", {"recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"],
            "recipe": recipe, "idempotency_key": "recipe"})
        result = call("dataset.bind", {"recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"],
            "dataset_id": dataset["dataset_id"], "dataset_revision": 1, "split_id": "train", "purpose": "training",
            "node_id": "traces", "target_node": "compose", "target_port": "traces", "idempotency_key": "bind"})
        validate_recipe(result["recipe"])
        assert result["recipe"]["edges"] == [{"source_node": "traces", "source_port": "data",
                                              "target_node": "compose", "target_port": "traces"}]
        assert not result["execution_started"] and not service.store.list("operation", project_id)
    finally:
        service.hub.close()
        service.store.close()
