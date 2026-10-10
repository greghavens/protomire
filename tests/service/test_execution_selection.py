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
    def prepare_execution(self, operation, payload):
        return operation, payload

    def launch_prepared(self, prepared, authorize=None):
        if authorize:
            authorize()
        return self.invoke(*prepared, None)

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


def live_recipe():
    body = recipe()
    body["evaluation"]["live"] = {"enabled": True, "max_concurrency": 1, "timeout_seconds": 30}
    return body


def test_selected_trainer_and_explicit_evaluator_retain_live_without_export():
    from modelbuilder.backend.components import live_evaluator, validate_live_evaluation
    body = live_recipe()
    original = copy.deepcopy(body)
    execution = selection(body, {"node_ids": ["data", "initial", "train", "evaluate"], "input_bindings": []})
    view = selected_recipe(body, execution)
    validate_live_evaluation(view)
    trainer = next(node for node in view["nodes"] if node["id"] == "train")
    evaluator = live_evaluator(trainer, view)
    assert evaluator["node"]["id"] == "evaluate"
    assert evaluator["input_bindings"] == {"data": "data"}
    assert "export" not in {node["id"] for node in view["nodes"]}
    assert view["evaluation"]["live"] == body["evaluation"]["live"]
    assert body == original


@pytest.mark.parametrize("nodes,bindings", [(["data", "initial", "train"], []), (["data", "evaluate"], [binding()])])
def test_live_cuts_never_launch_unselected_trainer_or_evaluator(nodes, bindings):
    from modelbuilder.backend.components import live_evaluator, validate_live_evaluation
    view = selected_recipe(live_recipe(), selection(live_recipe(), {"node_ids": nodes, "input_bindings": bindings}))
    assert "live" not in view["evaluation"]
    validate_live_evaluation(view)
    assert all(live_evaluator(node, view) is None for node in view["nodes"])


def test_live_cut_rejects_boundary_evaluator_input_instead_of_silently_dropping_it():
    body = live_recipe()
    for node in body["nodes"]:
        if node["id"] == "evaluate":
            node["inputs"].append({"name": "calibration", "type": "Path", "required": False})
    execution = selection(body, {"node_ids": ["data", "initial", "train", "evaluate"],
                                "input_bindings": [binding("evaluate", "calibration")]})
    from modelbuilder.backend.components import validate_live_evaluation
    with pytest.raises(ValueError, match="exactly one downstream evaluator"):
        validate_live_evaluation(selected_recipe(body, execution), input_bindings=execution["input_bindings"])


def test_live_cut_rejects_missing_shared_required_input():
    from modelbuilder.backend.components import validate_live_evaluation
    body = live_recipe()
    execution = selection(body, {"node_ids": ["data", "initial", "train", "evaluate"], "input_bindings": []})
    view = selected_recipe(body, execution)
    view["edges"] = [edge for edge in view["edges"] if not (edge["target_node"] == "evaluate" and edge["target_port"] == "data")]
    with pytest.raises(ValueError, match="exactly one downstream evaluator"):
        validate_live_evaluation(view)


def test_two_explicitly_selected_live_evaluators_remain_ambiguous():
    from modelbuilder.backend.components import validate_live_evaluation
    body = live_recipe()
    evaluator = copy.deepcopy(next(node for node in body["nodes"] if node["id"] == "evaluate"))
    evaluator["id"] = "other_eval"
    body["nodes"].append(evaluator)
    body["edges"].extend([{**edge, "target_node": "other_eval"} for edge in list(body["edges"]) if edge["target_node"] == "evaluate"])
    view = selected_recipe(body, selection(body, {"node_ids": ["data", "initial", "train", "evaluate", "other_eval"], "input_bindings": []}))
    with pytest.raises(ValueError, match="exactly one downstream evaluator"):
        validate_live_evaluation(view)


@pytest.mark.parametrize("changed", [None, "backend_run_id", "source_node_id", "source_port", "artifact_id", "content_sha256"])
def test_live_cut_maps_only_identical_pinned_boundary_inputs(changed):
    from modelbuilder.backend.components import live_evaluator, validate_live_evaluation
    body = live_recipe()
    pins = [binding("train", "data"), binding("evaluate", "data"), binding("initial", "data")]
    if changed:
        pins[1][changed] = "b" * 64 if changed == "content_sha256" else "different"
    execution = selection(body, {"node_ids": ["initial", "train", "evaluate"], "input_bindings": pins})
    view = selected_recipe(body, execution)
    trainer = next(node for node in view["nodes"] if node["id"] == "train")
    if changed:
        with pytest.raises(ValueError, match="exactly one downstream evaluator"):
            validate_live_evaluation(view, input_bindings=pins)
    else:
        validate_live_evaluation(view, input_bindings=pins)
        assert live_evaluator(trainer, view, input_bindings=pins)["input_bindings"] == {"data": "data"}
        assert "data" not in {node["id"] for node in view["nodes"]}


def test_optional_live_boundary_input_is_preserved_when_explicitly_shared():
    from modelbuilder.backend.components import live_evaluator
    body = live_recipe()
    for node in body["nodes"]:
        if node["id"] in {"train", "evaluate"}:
            node["inputs"].append({"name": "calibration", "type": "Path", "required": False})
    pins = [binding("train", "calibration"), binding("evaluate", "calibration")]
    execution = selection(body, {"node_ids": ["data", "initial", "train", "evaluate"], "input_bindings": pins})
    view = selected_recipe(body, execution)
    trainer = next(node for node in view["nodes"] if node["id"] == "train")
    assert live_evaluator(trainer, view, input_bindings=pins)["input_bindings"] == {"data": "data", "calibration": "calibration"}
