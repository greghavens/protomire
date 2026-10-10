"""One typed graph, explicit terminal conditions, no hidden selection expansion."""
import copy

import pytest
from modelbuilder.conditions import ConditionError, condition_decision, control_dependencies, validate_conditions
from modelbuilder.execution import selection
from modelbuilder.models import ServiceError
from modelbuilder.recipe import RecipeValidationError, default_recipe, normalize_recipe, recipe_digest


def recipe():
    body=default_recipe("project","recipe")
    body["source"]={"repository":"https://example.org/source.git","commit":"a"*40,"files":[]}
    body["nodes"]=[{"id":name,"kind":"code.python","implementation_ref":{"module":"harness","entrypoint":name,"commit":"a"*40},
        "inputs":[],"outputs":[{"name":"allowed" if name=="gate" else "output","type":"bool" if name=="gate" else "dict"}],
        "parameters":{},"environment_ref":"default","resource_request":{"gpu":0},"retry":{"max_attempts":1,"backoff_seconds":0},
        "timeout":30,"cache_policy":"disabled","provenance":{}} for name in ("gate","terminal")]
    body["nodes"][1]["condition"]={"source_node":"gate","source_port":"allowed","equals":True}
    return body


def test_typed_condition_is_in_one_canonical_immutable_recipe():
    body=recipe()
    before=copy.deepcopy(body)
    normalized=normalize_recipe(body)
    assert body==before and normalized["recipe_id"]==body["recipe_id"]
    assert normalized["nodes"][1]["condition"]==body["nodes"][1]["condition"]
    assert control_dependencies(normalized)==[("gate","terminal")]
    body["nodes"][1]["condition"]["equals"]=False
    assert recipe_digest(body)!=recipe_digest(before)


@pytest.mark.parametrize("change,code",[
    (lambda b:b["nodes"][1]["condition"].update(source_node="missing"),"invalid_condition_source"),
    (lambda b:b["nodes"][1]["condition"].update(source_node="terminal"),"invalid_condition_source"),
    (lambda b:b["nodes"][1]["condition"].update(source_port="missing"),"condition_type_mismatch"),
    (lambda b:b["nodes"][0]["outputs"][0].update(type="int"),"condition_type_mismatch"),
    (lambda b:b["nodes"][1].update(loop={"max_iterations":3}),"unsupported_control_flow"),
    (lambda b:b["nodes"][1].update(repeat=3),"unsupported_control_flow"),
    (lambda b:b["edges"].append({"source_node":"terminal","source_port":"output","target_node":"gate","target_port":"data"}),"unsupported_conditional_downstream"),
    (lambda b:b["evaluation"].update(live={"enabled":True}),"unsupported_conditional_live_evaluation"),
])
def test_unsupported_or_ambiguous_controls_fail_validation(change,code):
    body=recipe()
    change(body)
    with pytest.raises(RecipeValidationError) as failure:
        normalize_recipe(body)
    assert failure.value.code==code


@pytest.mark.parametrize("value",[1,0,"true",None,[],{}])
def test_gate_decision_never_coerces_truthiness(value):
    with pytest.raises(ValueError,match="actual boolean"):
        condition_decision({"equals":True},value)


def test_actual_boolean_is_compared_exactly():
    assert condition_decision({"equals":False},False)
    assert not condition_decision({"equals":True},False)


def test_condition_control_dependency_must_be_selected_explicitly():
    body=recipe()
    with pytest.raises(ServiceError) as failure:
        selection(body,{"node_ids":["terminal"],"input_bindings":[]})
    assert failure.value.code=="unselected_condition_gate"
    assert selection(body,{"node_ids":["gate","terminal"],"input_bindings":[]})["node_ids"]==["gate","terminal"]
    assert selection(body,{"node_ids":["gate"],"input_bindings":[]})["node_ids"]==["gate"]


@pytest.mark.parametrize("condition",[None,True,{}, {"source_node":"gate","source_port":"allowed","equals":1},
    {"source_node":"gate","source_port":"allowed","equals":True,"expression":"execute()"}])
def test_direct_admission_contract_rejects_expression_and_malformed_conditions(condition):
    body=recipe()
    body["nodes"][1]["condition"]=condition
    with pytest.raises(ConditionError,match="source_node, source_port"):
        validate_conditions(body)
    with pytest.raises(ServiceError):
        selection(body)
