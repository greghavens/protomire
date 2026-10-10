import copy
import json
from pathlib import Path

import pytest
from modelbuilder.execution import selection, selected_recipe
from modelbuilder.models import Actor, OPERATIONS, ServiceError
from modelbuilder.service import OperationService

ROOT = Path(__file__).resolve().parents[2]


def recipe():
    return json.loads((ROOT / "examples/custom-pytorch/recipe.json").read_text())


def binding(node="evaluate", port="checkpoint"):
    return {"node_id": node, "input_port": port, "backend_run_id": "producer", "source_node_id": "train",
            "source_port": "checkpoint", "artifact_id": "native-artifact", "content_sha256": "a" * 64}


def test_exact_cut_does_not_expand_ancestors_or_mutate_recipe():
    body = recipe()
    before = copy.deepcopy(body)
    result = selection(body, {"node_ids": ["evaluate", "data"], "input_bindings": [binding()]})
    assert set(result["node_ids"]) == {"evaluate", "data"}
    assert "train" not in result["node_ids"]
    assert body == before
    view = selected_recipe(body, result)
    assert {node["id"] for node in view["nodes"]} == {"evaluate", "data"}
    assert all(edge["source_node"] != "train" for edge in view["edges"])


@pytest.mark.parametrize("execution", [
    {"node_ids": ["evaluate"], "input_bindings": [binding()]},
    {"node_ids": ["data", "evaluate", "train", "initial"], "input_bindings": [binding()]},
    {"node_ids": ["data", "evaluate"], "input_bindings": [binding(), binding()]},
    {"node_ids": ["data", "evaluate"], "input_bindings": [binding("missing")]},
    {"node_ids": ["data", "evaluate"], "input_bindings": [binding(port="missing")]},
    {"node_ids": ["missing"], "input_bindings": []},
    {"node_ids": ["data", "data"], "input_bindings": []},
    {"node_ids": [], "input_bindings": []},
])
def test_selection_rejects_unknown_duplicate_missing_or_conflicting_inputs(execution):
    with pytest.raises(ServiceError):
        selection(recipe(), execution)


def test_public_api_mcp_contract_share_exact_selection_schema():
    from jsonschema import Draft202012Validator
    spec = OPERATIONS["run.submit"].input_schema
    good = {"project_id": "project", "recipe_id": "recipe", "expected_revision": 3,
            "idempotency_key": "eval", "execution": {"node_ids": ["evaluate", "data"], "input_bindings": [binding()]}}
    Draft202012Validator(spec).validate(good)
    bad = copy.deepcopy(good)
    bad["execution"]["input_bindings"][0]["path"] = "/someone/else/checkpoint"
    assert list(Draft202012Validator(spec).iter_errors(bad))


class Backend:
    def __init__(self):
        self.calls = []
    def invoke(self, operation, payload, actor):
        self.calls.append((operation, copy.deepcopy(payload)))
        return {"operation_id": payload.get("operation_id"), "project_id": payload["project_id"],
                "backend_run_id": "run-" + payload.get("operation_id", "plan"), "status": "submitted"}


def test_service_checks_project_owned_input_before_backend_and_recipe_unchanged(tmp_path):
    backend = Backend()
    service = OperationService(tmp_path / "db", backend=backend)
    actor = Actor("test", {"read", "modify", "execute"})
    try:
        project = service.invoke("project.create", {"slug": "selected", "model_name": "Selected", "recipe": recipe(), "idempotency_key": "create"}, actor)
        base = {"project_id": project["project_id"], "recipe_id": project["recipe_id"], "expected_revision": 1, "trusted": True}
        original = service._current_recipe(project["project_id"])
        execution = {"node_ids": ["evaluate", "data"], "input_bindings": [binding()]}
        with pytest.raises(ServiceError, match="No matching run"):
            service.invoke("run.submit", {**base, "execution": execution, "idempotency_key": "bad"}, actor)
        assert not backend.calls
        parent = service.invoke("run.submit", {**base, "idempotency_key": "train"}, actor)
        execution["input_bindings"][0]["backend_run_id"] = parent["backend_run_id"]
        result = service.invoke("run.submit", {**base, "execution": execution, "idempotency_key": "eval"}, actor)
        assert result["recipe_revision"] == 1
        assert service._current_recipe(project["project_id"]) == original
        assert backend.calls[-1][1]["recipe"] == original
        assert backend.calls[-1][1]["execution"]["input_bindings"][0]["artifact_id"] == "native-artifact"
    finally:
        service.store.close()


def test_selected_admission_drops_only_unselected_generated_dataset_closures():
    body = recipe()
    body['nodes'].extend([
        {'id': 'dataset_a', 'kind': 'dataset.version.import', 'parameters': {'reference_id': 'ref-a'}, 'inputs': [], 'outputs': []},
        {'id': 'dataset_b', 'kind': 'dataset.version.import', 'parameters': {'reference_id': 'ref-b'}, 'inputs': [], 'outputs': []},
    ])
    body['external_dependencies'] = [
        {'id': 'ref-a', 'metadata': {'dataset_reference': {'dependencies': [{'id': 'source-a'}]}}},
        {'id': 'source-a', 'metadata': {'dataset_source': True}},
        {'id': 'ref-b', 'metadata': {'dataset_reference': {'dependencies': [{'id': 'source-b'}]}}},
        {'id': 'source-b', 'metadata': {'dataset_source': True}},
        {'id': 'explicit-component-resource', 'metadata': {}},
    ]
    original = copy.deepcopy(body)
    view = selected_recipe(body, {'node_ids': ['dataset_a'], 'input_bindings': []})
    assert {item['id'] for item in view['external_dependencies']} == {'ref-a', 'source-a', 'explicit-component-resource'}
    assert view['source'] == original['source']  # Entire immutable authored closure stays pinned.
    assert body == original
