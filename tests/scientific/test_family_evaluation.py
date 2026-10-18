"""Family obligations use existing scientific evidence and native target guards."""
from copy import deepcopy

import pytest

from modelbuilder.evaluation import digest, validate_evaluation_record
from modelbuilder.family_coverage import canonical_policy_identity
from modelbuilder.family_evaluation import family_metric_id, measured_metrics, family_slices
from modelbuilder.scientific import evaluate_policy, validate_policy
from modelbuilder.backend.scientific_gate_artifacts import validate_result
from tests.scientific.test_policy import policy as base_policy, record as base_record


def family_policy(ids=('first', 'second')):
    return {'schema_version': 'modelbuilder.family-policy.v1', 'mapping': {'version': '1', 'field': 'family'},
            'families': [{'id': key, 'min_rows': 0, 'min_exposure': 1, 'min_eval_support': 2} for key in ids]}


def policy():
    p = base_policy()
    p.update(family_policy=family_policy(), family_metrics=['cross_entropy'], required_benchmark='heldout/v1')
    prototype = p['rules'][0]
    p['rules'] = [{**deepcopy(prototype), 'id': family['id'], 'metric': family_metric_id(family['id'], 'cross_entropy')}
                  for family in p['family_policy']['families']]
    return p


def record(p, family, value, checkpoint):
    r = base_record(family_metric_id(family, 'cross_entropy'), value, n=2, checkpoint=checkpoint)
    r['split']['sample_ids'] = [family + str(i) for i in range(2)]
    for index, prediction in enumerate(r['predictions']):
        prediction['item_id'] = r['split']['sample_ids'][index]
    r['protocol']['configuration']['family'] = {'policy_id': canonical_policy_identity(p['family_policy']),
        'family_id': family, 'minimum_support': 2, 'cohort_digest': digest(r['split']['sample_ids'])}
    r['digest'] = digest({k: v for k, v in r.items() if k != 'digest'})
    return r


def evidence(p, values=(.5, .5)):
    return {family: {'baseline': {'record': record(p, family, 1., 'a')},
                     'candidate': {'record': record(p, family, value, 'b')}}
            for family, value in zip(('first', 'second'), values)}


def test_family_regression_not_hidden_by_other_family_win():
    p = policy()
    e = evidence(p, (.01, 1.1))
    assert evaluate_policy(p, e)['outcome'] == 'failed'
    assert evaluate_policy(p, evidence(p))['passed']


@pytest.mark.parametrize('change', ['missing', 'cohort', 'policy', 'benchmark', 'support'])
def test_family_missing_or_incompatible_evidence_blocks(change):
    p = policy()
    e = evidence(p)
    r = e['second']['candidate']['record']
    if change == 'missing':
        e['second']['candidate']['record'] = {}
    elif change == 'cohort':
        r['split']['sample_ids'] = ['x', 'y']
    elif change == 'policy':
        r['protocol']['configuration']['family']['policy_id'] = 'sha256:' + 'f' * 64
    elif change == 'benchmark':
        r['dataset']['benchmark'] = 'quick-subset/v1'
    else:
        r['protocol']['configuration']['family']['minimum_support'] = 3
    r['digest'] = digest({k: v for k, v in r.items() if k != 'digest'})
    result = evaluate_policy(p, e)
    assert result['outcome'] == 'unknown' and not result['passed']


def test_family_rule_inventory_never_truncated_or_aggregate_only():
    p = policy()
    p['rules'].pop()
    with pytest.raises(ValueError, match='every required family'):
        validate_policy(p)
    p['family_policy'] = family_policy(tuple(str(i) for i in range(33)))
    with pytest.raises(ValueError, match='32 rules'):
        validate_policy(p)


def test_absolute_family_obligation_and_forged_pass_fail():
    p = policy()
    p['family_policy']['families'][1]['metrics'] = {'cross_entropy': {'max': .4}}
    e = evidence(p)
    result = evaluate_policy(p, e)
    assert result['outcome'] == 'failed'
    result['rules'][1]['outcome'] = 'passed'
    result.update(outcome='passed', passed=True)
    result['digest'] = digest({k: v for k, v in result.items() if k != 'digest'})
    with pytest.raises(ValueError, match='family metric obligations'):
        validate_result(result, p, e)


def test_token_weighting_collision_safe_ids_and_actual_support():
    assert measured_metrics([[2., 1.], [9., 9.]], 'causal-lm')['cross_entropy'] == 1.1
    with pytest.raises(ValueError, match='supervised tokens'):
        measured_metrics([[0., 0.]], 'causal-lm')
    assert family_metric_id('a/b', 'accuracy') != family_metric_id('a_b', 'accuracy')
    p = family_policy()
    assert family_slices([{'family': 'first'}, {'family': 'second'}], ['a', 'b'], p) == {'first': [0], 'second': [1]}
    with pytest.raises(ValueError):
        family_slices([{'family': 'first'}, {'family': 'second'}], ['a', 'a'], p)


def test_token_weighted_record_recomputes_contributions():
    r = base_record(n=2)
    r['metric']['aggregation'] = 'token_weighted_mean'
    r['predictions'] = [{'item_id': '0', 'negative_log_likelihood_sum': 2., 'token_count': 1},
                        {'item_id': '1', 'negative_log_likelihood_sum': 9., 'token_count': 9}]
    r['value'] = 1.1
    r['digest'] = digest({k: v for k, v in r.items() if k != 'digest'})
    assert not validate_evaluation_record(r)
    r['value'] = 1.5  # An incorrect average of per-row losses.
    r['digest'] = digest({k: v for k, v in r.items() if k != 'digest'})
    assert any('Token-weighted' in error['message'] for error in validate_evaluation_record(r))


def test_named_benchmark_subset_cannot_satisfy_exact_full_cohort():
    from modelbuilder.family_evaluation import evaluation_cohort_identity
    p = policy()
    e = evidence(p)
    reference = e['first']['baseline']['record']
    full_split = {**reference['split'], 'sample_ids': ['first0', 'first1', 'second0', 'second1']}
    p['required_evaluation'] = evaluation_cohort_identity(reference['dataset'], full_split)
    for pair in e.values():
        for side in ('baseline', 'candidate'):
            r = pair[side]['record']
            r['protocol']['configuration']['evaluation_cohort'] = deepcopy(p['required_evaluation'])
            r['provenance'] = {'consumption': {'dataset': deepcopy(r['dataset']), 'split': deepcopy(full_split)}}
            r['digest'] = digest({k: v for k, v in r.items() if k != 'digest'})
    assert evaluate_policy(p, e)['passed']
    for pair in e.values():
        r = pair['candidate']['record']
        # Same benchmark label; different admitted parent cohort. Even a copied
        # expected protocol identity cannot disguise this truncated source.
        r['provenance']['consumption']['split']['sample_ids'] = ['0']
        r['digest'] = digest({k: v for k, v in r.items() if k != 'digest'})
    assert evaluate_policy(p, e)['outcome'] == 'unknown'


def test_overlapping_family_slices_block_even_with_same_benchmark_label():
    p = policy()
    e = evidence(p)
    for side in ('baseline', 'candidate'):
        r = e['second'][side]['record']
        r['split']['sample_ids'] = deepcopy(e['first'][side]['record']['split']['sample_ids'])
        for index, prediction in enumerate(r['predictions']):
            prediction['item_id'] = r['split']['sample_ids'][index]
        r['protocol']['configuration']['family']['cohort_digest'] = digest(r['split']['sample_ids'])
        r['digest'] = digest({k: v for k, v in r.items() if k != 'digest'})
    result = evaluate_policy(p, e)
    assert result['outcome'] == 'unknown'
    for row in result['rules']:
        row['outcome'] = 'passed'
    result.update(outcome='passed', passed=True)
    result['digest'] = digest({k: v for k, v in result.items() if k != 'digest'})
    with pytest.raises(ValueError, match='incompatible or missing'):
        validate_result(result, p, e)


def test_aggregate_cannot_claim_family_metadata_to_bypass_full_cohort():
    from modelbuilder.family_evaluation import evaluation_cohort_identity
    from tests.scientific.test_policy import evidence as base_evidence
    p = base_policy()
    e = base_evidence(n=2)
    reference = e['loss']['baseline']['record']
    p['required_evaluation'] = evaluation_cohort_identity(reference['dataset'], reference['split'])
    for pair in e.values():
        for side in ('baseline', 'candidate'):
            r = pair[side]['record']
            r['protocol']['configuration']['evaluation_cohort'] = deepcopy(p['required_evaluation'])
            r['provenance'] = {'consumption': {'dataset': deepcopy(r['dataset']), 'split': deepcopy(r['split'])}}
            r['split']['sample_ids'] = ['0']
            r['predictions'] = [r['predictions'][0]]
            r['protocol']['configuration']['family'] = {'family_id': 'pretend', 'policy_id': 'sha256:' + 'a' * 64,
                'minimum_support': 1, 'cohort_digest': digest(['0'])}
            r['digest'] = digest({k: v for k, v in r.items() if k != 'digest'})
    assert evaluate_policy(p, e)['outcome'] == 'unknown'


def test_family_component_schemas_are_admitted_by_native_recipe_contract():
    from modelbuilder.backend.components import component_catalog
    from modelbuilder.recipe import _local_schema
    for component in component_catalog():
        if component['id'] in {'modelbuilder.scientific.gate', 'modelbuilder.rules.gate', 'modelbuilder.transformers.evaluate'}:
            _local_schema(component['parameter_schema'], component['id'])
    p = policy()
    p['required_evaluation'] = {'dataset_digest': 'G' * 64, 'split_digest': '0' * 64, 'sample_count': 4}
    with pytest.raises(ValueError, match='lowercase SHA256'):
        validate_policy(p)
