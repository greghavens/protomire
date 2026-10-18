"""Untrusted live family reports remain bounded observations, never gate authority."""
from copy import deepcopy

import pytest
from modelbuilder.backend.live_evaluation import _validate_family_evaluation
from modelbuilder.evaluation import digest
from modelbuilder.family_evaluation import family_metric_id


def fixture():
    name = 'reasoning'
    metric = family_metric_id(name, 'accuracy')
    metadata = {'policy_id': 'sha256:'+'a'*64, 'family_id': name, 'minimum_support': 1, 'cohort_digest': digest(['one'])}
    report = {'policy_id': metadata['policy_id'], 'families': {name: {'support': 1, 'minimum_support': 1,
        'status': 'available', 'cohort_digest': metadata['cohort_digest'], 'metric_ids': {'accuracy': metric}}}}
    records = {metric: {'protocol': {'configuration': {'family': metadata}}, 'split': {'sample_ids': ['one']}, 'metric': {'name': metric}}}
    return report, records


def test_valid_and_unavailable_family_reports_are_preserved():
    report, records = fixture()
    original = deepcopy(report)
    assert _validate_family_evaluation(report, records) == original
    report['families']['missing'] = {'support': 0, 'minimum_support': 2, 'status': 'unavailable',
        'cohort_digest': digest([]), 'metric_ids': {'accuracy': family_metric_id('missing', 'accuracy')}}
    assert _validate_family_evaluation(report, records)['families']['missing']['status'] == 'unavailable'


@pytest.mark.parametrize('change', [
    {'support': True}, {'support': -1}, {'minimum_support': 0}, {'status': 'passed'},
    {'status': 'unavailable'}, {'cohort_digest': '0'*64}, {'metric_ids': {'accuracy': 'forged'}},
])
def test_inconsistent_reports_rejected(change):
    report, records = fixture()
    report['families']['reasoning'].update(change)
    with pytest.raises(ValueError, match='invalid family_evaluation'):
        _validate_family_evaluation(report, records)


def test_record_for_omitted_family_and_oversized_reports_rejected():
    report, records = fixture()
    records[next(iter(records))]['protocol']['configuration']['family']['family_id'] = 'unlisted'
    with pytest.raises(ValueError):
        _validate_family_evaluation(report, records)
    report['families'] = {str(i): {} for i in range(257)}
    with pytest.raises(ValueError):
        _validate_family_evaluation(report, {})
