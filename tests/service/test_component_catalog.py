"""Shared service contracts stay inert, project-scoped and fail closed."""
import pytest

from modelbuilder.models import Actor, OPERATIONS, ServiceError
from modelbuilder.service import OperationService


@pytest.fixture
def setup(tmp_path):
    service = OperationService(tmp_path / 'catalog.sqlite')
    actor = Actor('owner', frozenset({'read', 'modify'}))
    project = service.invoke('project.create', {'slug': 'components', 'model_name': 'Components',
                                              'idempotency_key': 'project'}, actor)
    yield service, actor, {'project_id': project['project_id']}
    service.store.close()


def test_catalog_never_requires_execution_backend(setup):
    service, actor, payload = setup
    result = service.invoke('component.list', payload, actor)
    assert result['recipe_revision'] == 1
    assert result['components'][0]['id'] == 'modelbuilder.examples'
    entry = service.invoke('component.get', {**payload, 'component_id': 'modelbuilder.examples'}, actor)
    assert entry == result['components'][0]
    with pytest.raises(ServiceError, match='not declared'):
        service.invoke('component.get', {**payload, 'component_id': 'absent'}, actor)


def test_invalid_component_returns_diagnostics_not_execution(setup):
    service, actor, payload = setup
    result = service.invoke('component.validate', {**payload, 'node': {}}, actor)
    assert result['valid'] is False
    assert result['diagnostics'][0]['code'] == 'invalid_component'


def test_evaluation_identity_validation_inert_and_unknown(setup):
    service, actor, payload = setup
    result = service.invoke('evaluation.validate', {**payload, 'record': {}}, actor)
    assert result['valid'] is False
    assert result['diagnostics'][0]['field'] == 'schema'


def test_read_ops_enforce_project_scope(setup):
    service, _, payload = setup
    actor = Actor('outsider', frozenset({'read'}), frozenset({'unrelated'}))
    for operation, extra in [('component.list', {}), ('component.get', {'component_id': 'modelbuilder.examples'}),
                             ('component.validate', {'node': {}}), ('evaluation.validate', {'record': {}})]:
        with pytest.raises(ServiceError) as error:
            service.invoke(operation, {**payload, **extra}, actor)
        assert error.value.code == 'forbidden'


def test_live_read_contracts_and_plan_trust_are_shared():
    for name in ['run.evaluations.list', 'run.events.list']:
        assert OPERATIONS[name].scope == 'read'
        assert not OPERATIONS[name].mutating
    assert OPERATIONS['run.events.list'].input_schema['properties']['limit']['maximum'] == 200
    assert OPERATIONS['run.plan'].input_schema['properties']['trusted'] == {'type': 'boolean'}
    assert 'comparison_contract' in OPERATIONS['run.compare'].input_schema['properties']


def test_decision_records_executed_revisions_and_rejects_unexecuted(setup):
    service, actor, payload = setup
    recipe = service.invoke('project.open', payload, actor)['recipe']
    hypothesis = service.invoke('hypothesis.create', {**payload, 'proposed_change': 'Change architecture',
        'expected_outcome': 'Lower held-out loss', 'acceptance_metric': {'name': 'loss'},
        'idempotency_key': 'hypothesis'}, actor)
    service.store.put('operation', 'op', payload['project_id'], {
        'operation_id': 'op', 'project_id': payload['project_id'], 'backend_run_id': 'native-run',
        'recipe_id': recipe['recipe_id'], 'recipe_revision': 1, 'recipe_digest': recipe['digest'],
        'revision': 1})
    changed = service.invoke('recipe.update', {**payload, 'recipe_id': recipe['recipe_id'],
        'expected_revision': 1, 'recipe': {**recipe, 'name': 'Not executed'}, 'idempotency_key': 'recipe'}, actor)
    request = {**payload, 'hypothesis_id': hypothesis['hypothesis_id'], 'recipe_id': recipe['recipe_id'],
        'recipe_revision': changed['revision'], 'run_ids': ['native-run'],
        'selection_rule': {'metric': 'loss', 'direction': 'minimize'}, 'outcome': 'inconclusive',
        'rationale': 'Retain actual evidence', 'idempotency_key': 'bad-decision'}
    with pytest.raises(ServiceError, match='not executed'):
        service.invoke('decision.record', request, actor)
    result = service.invoke('decision.record', {**request, 'recipe_revision': 1,
                                               'idempotency_key': 'valid-decision'}, actor)
    assert result['run_provenance'] == [{'backend_run_id': 'native-run', 'operation_id': 'op',
        'recipe_id': recipe['recipe_id'], 'recipe_revision': 1, 'recipe_digest': recipe['digest']}]
