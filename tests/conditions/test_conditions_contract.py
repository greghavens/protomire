"""One typed graph, explicit branch conditions, no hidden selection expansion."""
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


def test_nested_conditions_and_typed_downstream_joins_share_existing_graph():
    body = recipe()
    nested = copy.deepcopy(body['nodes'][1])
    nested.update(id='nested', inputs=[{'name': 'left', 'type': 'dict', 'required': False}])
    nested.pop('condition')
    body['nodes'].append(nested)
    body['edges'] = [{'source_node': 'terminal', 'source_port': 'output', 'target_node': 'nested', 'target_port': 'left'}]
    assert normalize_recipe(body)['edges'] == body['edges']
    body['nodes'][1]['outputs'] = [{'name': 'output', 'type': 'bool'}]
    body['nodes'][2]['inputs'] = []
    body['nodes'][2]['condition'] = {'source_node': 'terminal', 'source_port': 'output', 'equals': True}
    body['edges'] = []
    assert next(node for node in normalize_recipe(body)['nodes'] if node['id'] == 'nested')['condition'] == body['nodes'][2]['condition']


def test_mixed_data_control_cycle_still_fails():
    body = recipe()
    body['nodes'][0]['inputs'] = [{'name': 'data', 'type': 'dict'}]
    body['edges'] = [{'source_node': 'terminal', 'source_port': 'output', 'target_node': 'gate', 'target_port': 'data'}]
    with pytest.raises(RecipeValidationError) as failure:
        normalize_recipe(body)
    assert failure.value.code == 'graph_cycle'


def test_native_input_parameter_shadowing_and_optional_builtins_are_rejected():
    body = recipe()
    body['nodes'][1]['inputs'] = [{'name': 'data', 'type': 'dict', 'required': False}]
    body['nodes'][1]['parameters'] = {'data': {}}
    with pytest.raises(RecipeValidationError) as failure:
        normalize_recipe(body)
    assert failure.value.code == 'ambiguous_input_parameter'
    body['nodes'][1]['parameters'] = {}
    body['nodes'][1].update(kind='dataset.validate', implementation_ref='builtin:dataset.validate')
    with pytest.raises(RecipeValidationError) as failure:
        normalize_recipe(body)
    assert failure.value.code == 'unsupported_optional_input'


def test_skip_projection_requires_native_gate_artifact_and_a_valid_transitive_chain():
    from types import SimpleNamespace
    from modelbuilder.conditions import recorded_decisions, skipped_dependencies
    import hashlib
    body = recipe()
    downstream = copy.deepcopy(body['nodes'][1])
    downstream.update(id='join', inputs=[{'name': 'left', 'type': 'dict'}])
    downstream.pop('condition')
    body['nodes'].append(downstream)
    body['edges'] = [{'source_node': 'terminal', 'source_port': 'output', 'target_node': 'join', 'target_port': 'left'}]
    chosen = {'gate', 'terminal', 'join'}
    blocked = skipped_dependencies(body, downstream, chosen, {'terminal'})
    native = {'gate': SimpleNamespace(outputs={'allowed': [SimpleNamespace(id='native-artifact')]})}
    records = {'join': {'status': 'skipped', 'reason': 'upstream_skipped', 'condition': None,
                       'blocked_by': blocked, 'status_source': 'native_run_metadata'},
               'terminal': {'status': 'skipped', 'condition': body['nodes'][1]['condition'],
                            'gate_value': False, 'gate_value_sha256': hashlib.sha256(b'false').hexdigest(),
                            'gate_artifact_id': 'native-artifact', 'status_source': 'native_run_metadata'}}
    assert recorded_decisions(body, chosen, records, native) == records
    assert not recorded_decisions(body, chosen, records, {})
    assert not recorded_decisions(body, {'join'}, records, native)
    assert not recorded_decisions(body, {'terminal', 'join'}, records, native)
    records['terminal']['gate_value'] = True
    assert not recorded_decisions(body, chosen, records, native)


def test_live_evaluator_gate_must_be_available_before_training_without_hidden_edges():
    body = recipe()
    gate = body['nodes'][0]
    trainer = copy.deepcopy(body['nodes'][1])
    trainer.update(id='train', kind='model.train')
    evaluator = copy.deepcopy(body['nodes'][1])
    evaluator.update(id='evaluate', kind='model.evaluate', inputs=[{'name': 'checkpoint', 'type': 'dict'}])
    body['nodes'] = [gate, trainer, evaluator]
    body['edges'] = [{'source_node': 'train', 'source_port': 'output', 'target_node': 'evaluate', 'target_port': 'checkpoint'}]
    body['evaluation']['live'] = {'enabled': True}
    before = copy.deepcopy(body)
    validate_conditions(body)
    assert body == before
    trainer.pop('condition')
    with pytest.raises(ConditionError) as failure:
        validate_conditions(body)
    assert failure.value.code == 'unsupported_conditional_live_gate'
    trainer['outputs'] = [{'name': 'allowed', 'type': 'bool'}]
    evaluator['condition'] = {'source_node': 'train', 'source_port': 'allowed', 'equals': True}
    with pytest.raises(ConditionError) as failure:
        validate_conditions(body)
    assert failure.value.code == 'unsupported_conditional_live_gate'
