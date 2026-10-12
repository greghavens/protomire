import copy
import json
from pathlib import Path

import pytest
from modelbuilder.iterations import invocation_identity
from modelbuilder.recipe import RECIPE_SCHEMA, RecipeValidationError, normalize_recipe, recipe_digest, default_recipe
from modelbuilder.execution import selection
from modelbuilder.models import ServiceError


def recipe():
    body = default_recipe('project', 'recipe')
    body['source'] = {'repository': 'https://example.org/source.git', 'commit': 'a' * 40, 'files': []}
    def node(name, inputs, outputs):
        return {'id': name, 'kind': 'code.python', 'implementation_ref': {'module': 'source', 'entrypoint': name, 'commit': 'a' * 40},
            'inputs': [{'name': key, 'type': value} for key, value in inputs.items()],
            'outputs': [{'name': key, 'type': value} for key, value in outputs.items()], 'parameters': {},
            'environment_ref': 'default', 'resource_request': {'gpu': 0}, 'retry': {'max_attempts': 1, 'backoff_seconds': 0},
            'timeout': 30, 'cache_policy': 'disabled', 'provenance': {}}
    body['nodes'] = [node('seed', {}, {'value': 'int'}), node('increment', {'value': 'int'}, {'value': 'int', 'done': 'bool'}), node('final', {'value': 'int'}, {'result': 'int'})]
    body['edges'] = [{'source_node': source, 'source_port': 'value', 'target_node': target, 'target_port': 'value'} for source, target in [('seed', 'increment'), ('increment', 'final')]]
    body['bounded_iterations'] = [{'id': 'refine', 'node_ids': ['increment'], 'max_iterations': 4,
        'state': [{'source_node': 'increment', 'source_port': 'value', 'target_node': 'increment', 'target_port': 'value'}],
        'until': {'source_node': 'increment', 'source_port': 'done', 'equals': True}}]
    return body


def test_canonical_schema_digest_and_transport_equality():
    original = recipe()
    normalized = normalize_recipe(original)
    assert normalized['bounded_iterations'] == original['bounded_iterations']
    modified = copy.deepcopy(original)
    modified['bounded_iterations'][0]['max_iterations'] = 3
    assert recipe_digest(original) != recipe_digest(modified)
    for path in ['schemas/recipe.v0.schema.json', 'packages/ide_feature/schemas/recipe.v0.schema.json']:
        assert json.loads(Path(path).read_text()) == RECIPE_SCHEMA
    assert selection(original)['node_ids'] == ['seed', 'increment', 'final']


@pytest.mark.parametrize('change,code', [
    (lambda body: body['bounded_iterations'][0].update(max_iterations=0), 'invalid_recipe'),
    (lambda body: body['bounded_iterations'][0].update(max_iterations=True), 'invalid_recipe'),
    (lambda body: body['bounded_iterations'][0].update(node_ids=['absent']), 'invalid_iteration_membership'),
    (lambda body: body['bounded_iterations'].append(copy.deepcopy(body['bounded_iterations'][0])), 'invalid_iteration_membership'),
    (lambda body: body['bounded_iterations'][0]['state'][0].update(source_port='done'), 'iteration_state_type_mismatch'),
    (lambda body: body['bounded_iterations'][0]['until'].update(source_port='value'), 'iteration_until_type_mismatch'),
    (lambda body: body['bounded_iterations'][0]['state'][0].update(resume_mode='warm_start'), 'invalid_iteration_warm_start'),
    (lambda body: body['nodes'][1].update(cache_policy='enabled'), 'iteration_cache_unsupported'),
    (lambda body: body['nodes'][1].update(repeat=3), 'unsupported_control_flow'),
    (lambda body: body.update(bounded_iterations=[{'id': 'bad', 'node_ids': ['seed', 'final'], 'max_iterations': 2, 'state': []}]), 'iteration_boundary_cycle'),
])
def test_invalid_control_never_runs(change, code):
    body = recipe()
    change(body)
    with pytest.raises(RecipeValidationError) as error:
        normalize_recipe(body)
    assert error.value.code == code


def test_partial_selection_and_native_identity():
    body = recipe()
    body['bounded_iterations'][0]['node_ids'].append('final')
    with pytest.raises(ServiceError) as error:
        selection(body, {'node_ids': ['increment'], 'input_bindings': []})
    assert error.value.code == 'partial_iteration_selection'
    block = body['bounded_iterations'][0]
    first = invocation_identity('increment', block, 1)
    assert first['invocation_id'] != invocation_identity('increment', block, 2)['invocation_id']
    assert first['canonical_node_id'] == 'increment' and first['max_iterations'] == 4


def test_native_invocation_identity_rejects_forged_node_index_or_bound():
    from types import SimpleNamespace
    from modelbuilder.backend.native_invocations import invocation
    body = recipe()
    block = body['bounded_iterations'][0]
    info = invocation_identity('increment', block, 2)
    def step(extra):
        return SimpleNamespace(config=SimpleNamespace(extra=extra))
    assert invocation(info['invocation_id'], step(info), body) == info
    for extra in [{**info, 'canonical_node_id': 'seed'}, {**info, 'iteration': 1}, {**info, 'iteration': True}, {**info, 'max_iterations': 99}, {}]:
        with pytest.raises(ValueError, match='immutable canonical'):
            invocation(info['invocation_id'], step(extra), body)
    with pytest.raises(ValueError):
        invocation('increment', step(info), body)


def test_optional_initial_state_and_explicit_training_warm_start_contract():
    body = recipe()
    block = body['bounded_iterations'][0]
    body['nodes'][1]['inputs'][0]['required'] = False
    body['edges'].pop(0)
    assert normalize_recipe(body)['bounded_iterations']
    block['state'][0]['resume_mode'] = 'warm_start'
    with pytest.raises(RecipeValidationError) as error:
        normalize_recipe(body)
    assert error.value.code == 'invalid_iteration_warm_start'


def test_iteration_member_order_does_not_change_digest_or_initial_mapping():
    body = recipe()
    body['bounded_iterations'][0]['node_ids'].append('final')
    reverse = copy.deepcopy(body)
    reverse['bounded_iterations'][0]['node_ids'].reverse()
    assert recipe_digest(body) == recipe_digest(reverse)
