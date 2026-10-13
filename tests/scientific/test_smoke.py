from copy import deepcopy
import pytest
from modelbuilder.smoke import plan
from modelbuilder.execution import max_parallel_stages


def recipe():
    # Independent nested budget declares semantics; no product parameter-name edit.
    component = {'parameter_schema': {'type':'object'}, 'capabilities': {'smoke_budget': {
        'schema':'modelbuilder.smoke-budget/v1','limits':[{'id':'new-budget','unit':'examples-applied','parameter_path':['novel','limit'], 'semantics':'absolute_integer_cap','minimum':2}]}}}
    return {'project_id':'p','recipe_id':'r','revision':7,'digest':'a'*64,
            'nodes':[{'id':'train','parameters':{'novel':{'limit':100},'learning_rate':.02},'component':component}], 'edges':[], 'source':{'commit':'pinned'},'execution':{'max_parallel_stages':1}}


def limits(maximum=3):
    return [{'node_id':'train','limit_id':'new-budget','unit':'examples-applied','maximum':maximum}]


def test_smoke_only_changes_declared_bound_and_preserves_identity():
    original=recipe()
    before=deepcopy(original)
    result=plan(original,limits())
    assert original==before
    assert result['recipe_id']=='r' and result['source_recipe_revision']==7
    assert result['recipe']['nodes'][0]['parameters']=={'novel':{'limit':3},'learning_rate':.02}
    assert result['recipe']['execution']['purpose']=='smoke'
    assert result['recipe']['execution']['smoke']['parent_recipe_digest']==original['digest']
    assert result['production_eligible'] is False and result['execution_allowed'] is False
    assert max_parallel_stages(result['recipe'])==1
    assert plan(original,limits(200))['recipe']['nodes'][0]['parameters']['novel']['limit']==100


def test_unknown_budget_is_genuine_unsupported_not_guessed():
    r=recipe()
    r['nodes'][0]['component']['capabilities'].pop('smoke_budget')
    result=plan(r,limits())
    assert not result['supported'] and result['recipe'] is None
    assert result['diagnostics'][0]['code']=='smoke_budget_unsupported'


@pytest.mark.parametrize('change',['unit','minimum','missing_path','duplicate','fraction'])
def test_smoke_semantics_must_be_exact(change):
    r, requested = recipe(), limits()
    if change == 'unit':
        requested[0]['unit'] = 'epochs'
    if change == 'minimum':
        requested[0]['maximum'] = 1
    if change == 'missing_path':
        r['nodes'][0]['parameters'] = {}
    if change == 'duplicate':
        requested = requested + requested
    if change == 'fraction':
        requested[0]['maximum'] = 2.5
    with pytest.raises(Exception):
        plan(r, requested)


def test_smoke_tag_cannot_omit_lineage():
    with pytest.raises(ValueError):
        max_parallel_stages({'execution':{'purpose':'smoke'}})
