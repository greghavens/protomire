"""Family slices use ordinary immutable evaluation records and native selectors."""
from __future__ import annotations

import math
from fractions import Fraction
from .evaluation import digest
from .family_coverage import canonical_policy_identity, family_for_row, validate_policy


def family_metric_id(family_id, metric):
    """No slug collisions or delimiter ambiguity, including Unicode family IDs."""
    return 'family_' + digest({'family_id': family_id, 'metric': metric}) + '_' + metric


def measured_metrics(measured, task):
    if not measured or any(len(row) != 2 or any(not math.isfinite(v) for v in row) for row in measured):
        raise ValueError('Missing or non-finite evaluation contributions')
    if task == 'causal-lm':
        if any(row[1] <= 0 or int(row[1]) != row[1] for row in measured):
            raise ValueError('Every evaluated causal-LM item needs effective supervised tokens')
        loss = sum(row[0] for row in measured) / sum(row[1] for row in measured)
        return {'cross_entropy': loss}
    return {'cross_entropy': sum(row[0] for row in measured) / len(measured),
            'accuracy': sum(row[1] for row in measured) / len(measured)}


def family_slices(rows, sample_ids, policy):
    validate_policy(policy)
    if len(rows) != len(sample_ids) or len(sample_ids) != len(set(sample_ids)):
        raise ValueError('Family evaluation requires exact unique item support')
    groups = {family['id']: [] for family in policy['families']}
    for index, row in enumerate(rows):
        groups[family_for_row(row, policy)].append(index)
    return groups


def record_family_diagnostics(record, policy, family, metric):
    """Additional scientific obligations; missing slices can never pass."""
    metadata = record.get('protocol', {}).get('configuration', {}).get('family')
    ids = record.get('split', {}).get('sample_ids', [])
    expected = {'policy_id': canonical_policy_identity(policy), 'family_id': family['id'],
                'minimum_support': family['min_eval_support'], 'cohort_digest': digest(ids)}
    errors = []
    if not isinstance(metadata, dict) or any(metadata.get(k) != v for k, v in expected.items()):
        errors.append({'field': 'protocol.configuration.family', 'message': 'Family policy, identity or exact cohort differs'})
    if len(ids) < family['min_eval_support']:
        errors.append({'field': 'split.sample_ids', 'message': 'Required family evaluation support is insufficient'})
    if record.get('metric', {}).get('name') != family_metric_id(family['id'], metric):
        errors.append({'field': 'metric.name', 'message': 'Required family metric identity differs'})
    return errors


def evaluation_cohort_identity(dataset, split):
    return {'dataset_digest': digest(dataset), 'split_digest': digest(split),
            'sample_count': len(split['sample_ids'])}


def scientific_family_diagnostics(policy, rule, pair):
    errors = []
    benchmark = policy.get('required_benchmark')
    for side in ('baseline', 'candidate'):
        record = pair[side]['record']
        if benchmark and record.get('dataset', {}).get('benchmark') != benchmark:
            errors.append({'field': 'dataset.benchmark', 'message': 'Required benchmark label differs'})
        required = policy.get('required_evaluation')
        if required:
            actual = record.get('protocol', {}).get('configuration', {}).get('evaluation_cohort')
            consumption = record.get('provenance', {}).get('consumption', {})
            try:
                source_cohort = evaluation_cohort_identity(consumption['dataset'], consumption['split'])
            except (KeyError, TypeError):
                source_cohort = None
            declared_family_metric = any(rule['metric'] == family_metric_id(family['id'], metric)
                for family in policy.get('family_policy', {}).get('families', [])
                for metric in policy.get('family_metrics', []))
            if (actual != required or source_cohort != required
                    or not declared_family_metric and digest(record.get('split')) != required['split_digest']
                    or record.get('dataset') != consumption.get('dataset')
                    or not set(record.get('split', {}).get('sample_ids', [])) <= set(consumption.get('split', {}).get('sample_ids', []))):
                errors.append({'field': 'protocol.configuration.evaluation_cohort',
                               'message': 'Required exact full evaluation cohort is missing or differs'})
        family_policy = policy.get('family_policy')
        if family_policy:
            for family in family_policy['families']:
                for metric in policy['family_metrics']:
                    if rule['metric'] == family_metric_id(family['id'], metric):
                        errors.extend(record_family_diagnostics(record, family_policy, family, metric))
    return errors


def scientific_family_failures(policy, rule, pair):
    failures = []
    for family in policy.get('family_policy', {}).get('families', []):
        for metric, limits in family.get('metrics', {}).items():
            if rule['metric'] != family_metric_id(family['id'], metric):
                continue
            baseline, candidate = (pair[side]['record'].get('value') for side in ('baseline', 'candidate'))
            if type(candidate) not in (int, float) or type(baseline) not in (int, float) or not math.isfinite(candidate) or not math.isfinite(baseline):
                continue  # Missing/nonfinite values are blocked by record validation.
            candidate, baseline = Fraction(str(candidate)), Fraction(str(baseline))
            if 'min' in limits and candidate < Fraction(str(limits['min'])) or 'max' in limits and candidate > Fraction(str(limits['max'])):
                failures.append('Required family absolute metric threshold failed')
            if 'max_regression' in limits:
                regression = (baseline - candidate) if limits['direction'] == 'higher' else (candidate - baseline)
                if regression > Fraction(str(limits['max_regression'])):
                    failures.append('Required family regression threshold failed')
    return failures


def scientific_partition_diagnostics(policy, evidence):
    """A scalar family mapping must partition the exact admitted parent cohort."""
    family_policy = policy.get('family_policy')
    if not family_policy:
        return []
    errors = []
    rules = {rule['metric']: rule for rule in policy['rules']}
    for side in ('baseline', 'candidate'):
        seen = set()
        parent_ids = None
        for family in family_policy['families']:
            family_ids = None
            for metric in policy['family_metrics']:
                rule = rules[family_metric_id(family['id'], metric)]
                record = evidence[rule['id']][side]['record']
                ids = record.get('split', {}).get('sample_ids', [])
                if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
                    errors.append({'field': 'split.sample_ids', 'message': 'Family cohort is unavailable'})
                    continue
                current = set(ids)
                if family_ids is not None and family_ids != current:
                    errors.append({'field': 'split.sample_ids', 'message': 'Metrics disagree on the same family cohort'})
                family_ids = current
                if policy.get('required_evaluation'):
                    parent = record.get('provenance', {}).get('consumption', {}).get('split', {}).get('sample_ids', [])
                    if not isinstance(parent, list) or any(not isinstance(item, str) for item in parent):
                        errors.append({'field': 'provenance.consumption.split', 'message': 'Full evaluation parent cohort is unavailable'})
                        continue
                    if parent_ids is not None and parent_ids != set(parent):
                        errors.append({'field': 'provenance.consumption.split', 'message': 'Family records disagree on full evaluation cohort'})
                    parent_ids = set(parent)
            if seen & (family_ids or set()):
                errors.append({'field': 'split.sample_ids', 'message': 'Scalar family cohorts must be disjoint'})
            seen.update(family_ids or set())
        if policy.get('required_evaluation') and (parent_ids is None or seen != parent_ids):
            errors.append({'field': 'split.sample_ids', 'message': 'Family slices must cover the exact full evaluation cohort'})
    return errors
