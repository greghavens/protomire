"""Versioned deterministic safeguards. No expressions, imports or action dispatch.

Harnesses own epoch execution. A decision is evidence, never execution permission.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from fractions import Fraction
from typing import Literal, TypedDict

from jsonschema import Draft202012Validator
from .evaluation import digest
from .scientific import Method


class Decision(TypedDict):
    schema_version: Literal['modelbuilder.rule-decision/v1']
    outcome: Literal['passed', 'continue', 'stop', 'blocked', 'cancelled']
    reasons: list[str]
    policy_digest: str
    evidence_digest: str
    rules: list[dict]
    digest: str


class RulePolicy(TypedDict):
    schema_version: Literal['modelbuilder.rules/v1']
    id: str
    version: str
    rules: list[dict]


def obj(properties, required=None):
    return {'type': 'object', 'additionalProperties': False, 'properties': properties,
            'required': list(properties) if required is None else required}


TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 200}
COUNT = {'type': 'integer', 'minimum': 0, 'maximum': 10**9}
NUMBER = {'type': 'number', 'minimum': 0, 'maximum': 10**15}
CONVERGENCE = obj({'id': TEXT, 'kind': {'const': 'convergence/v1'}, 'metric': TEXT,
    'direction': {'enum': ['minimize', 'maximize']}, 'criterion': {'enum': ['target', 'plateau']},
    'threshold': {'type': 'number'}, 'window': {'type': 'integer', 'minimum': 2, 'maximum': 10000},
    'insufficient_window': {'enum': ['block', 'collect_within_bounds']},
    'min_epochs': COUNT, 'max_epochs': {'type': 'integer', 'minimum': 1, 'maximum': 10**9},
    'max_steps': COUNT, 'max_runtime_seconds': NUMBER, 'max_cost': NUMBER,
    'resource_modes': obj({key: {'enum': ['active', 'not_applicable']} for key in ('steps', 'runtime_seconds', 'cost')}),
    'cost_unit': TEXT,
    'step_unit': TEXT,
    'next_epoch_steps': COUNT, 'next_epoch_seconds': NUMBER, 'next_epoch_cost': NUMBER})
REPRESENTATION = obj({'id': TEXT, 'kind': {'const': 'representation/v1'},
    'allocation': {'enum': ['proportional', 'equal']}, 'min_per_stratum': COUNT,
    'required_strata': {'type': 'array', 'maxItems': 10000, 'uniqueItems': True, 'items': TEXT},
    'max_share_error': {'type': 'number', 'minimum': 0, 'maximum': 1}})
IDENTITY = obj({'id': TEXT, 'kind': {'const': 'identity/v1'},
    'expected': {'type': 'object', 'minProperties': 1, 'maxProperties': 32,
                 'additionalProperties': {'type': ['string', 'number', 'boolean', 'null']}}})
OVERLAP = obj({'id': TEXT, 'kind': {'const': 'overlap/v1'},
    'scope': {'enum': ['cross_run', 'cross_lineage']}, 'max_overlap': COUNT})
METHOD_SCHEMA = obj({'id': TEXT, 'version': TEXT,
    'decision_domain': {'enum': ['predicate', 'continuation']},
    'implementation_ref': obj({key: TEXT for key in ('module', 'entrypoint', 'commit', 'path', 'sha256')}),
    'parameter_schema': {'type': 'object'},
    'parameter_schema_digest': {'type': 'string', 'minLength': 64, 'maxLength': 64},
    'method_digest': {'type': 'string', 'minLength': 64, 'maxLength': 64}})
CUSTOM_RULE = obj({'id': TEXT, 'kind': {'const': 'method/v1'}, 'method': METHOD_SCHEMA,
                   'parameters': {'type': 'object'}})
POLICY_SCHEMA = obj({'schema_version': {'const': 'modelbuilder.rules/v1'}, 'id': TEXT,
    'version': TEXT, 'rules': {'type': 'array', 'minItems': 1, 'maxItems': 32,
                             'items': {'oneOf': [CONVERGENCE, REPRESENTATION, IDENTITY, OVERLAP, CUSTOM_RULE]}}})
SAMPLE_SCHEMA = obj({'kind': {'const': 'rows.sample'}, 'version': {'const': '1'},
    'split_ids': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': TEXT},
    'fields': {'type': 'array', 'minItems': 1, 'maxItems': 32, 'uniqueItems': True, 'items': TEXT},
    'fraction': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1}, 'seed': TEXT,
    'allocation': {'enum': ['proportional', 'equal']}, 'min_per_stratum': COUNT,
    'required_strata': {'type': 'array', 'maxItems': 10000, 'items': {'type': 'object', 'minProperties': 1}},
    'max_share_error': {'type': 'number', 'minimum': 0, 'maximum': 1}})


def validate_policy(policy: dict) -> RulePolicy:
    digest(policy)  # rejects nonfinite values before any comparison
    Draft202012Validator(POLICY_SCHEMA).validate(policy)
    ids = [rule['id'] for rule in policy['rules']]
    if len(ids) != len(set(ids)):
        raise ValueError('Rule IDs must be unique')
    for rule in policy['rules']:
        if rule['kind'] == 'convergence/v1':
            for key in ('window', 'min_epochs', 'max_epochs', 'max_steps', 'next_epoch_steps'):
                if type(rule[key]) is not int:
                    raise ValueError('Count and window options require actual integers: ' + key)
            if rule['min_epochs'] > rule['max_epochs']:
                raise ValueError('Minimum epochs exceed maximum epochs')
            if rule['criterion'] == 'plateau' and rule['threshold'] < 0:
                raise ValueError('Plateau threshold must be nonnegative')
            for used, maximum, increment in (('steps', 'max_steps', 'next_epoch_steps'), ('runtime_seconds', 'max_runtime_seconds', 'next_epoch_seconds'), ('cost', 'max_cost', 'next_epoch_cost')):
                if rule['resource_modes'][used] == 'not_applicable' and (rule[maximum] != 0 or rule[increment] != 0):
                    raise ValueError('Not-applicable resource counters require zero bounds and projections')
        if rule['kind'] == 'method/v1':
            validate_method(rule['method'])
            Draft202012Validator(rule['method']['parameter_schema']).validate(rule['parameters'])
        if rule['kind'] == 'representation/v1' and type(rule['min_per_stratum']) is not int:
            raise ValueError('Minimum coverage requires an actual integer')
        if rule['kind'] == 'overlap/v1' and type(rule['max_overlap']) is not int:
            raise ValueError('Overlap limit requires an actual integer')
    return deepcopy(policy)


def validate_method(declaration):
    """Inert source/schema identity checks. No implementation import or execution."""
    from .recipe import IMPLEMENTATION_SCHEMA, _local_schema, _json_value
    _json_value(declaration)
    digest(declaration)
    Draft202012Validator(METHOD_SCHEMA).validate(declaration)
    Draft202012Validator(IMPLEMENTATION_SCHEMA).validate(declaration['implementation_ref'])
    _local_schema(declaration['parameter_schema'], 'rule.method.parameter_schema')
    if declaration['parameter_schema_digest'] != digest(declaration['parameter_schema']):
        raise ValueError('Rule method parameter schema identity differs')
    if declaration['method_digest'] != digest({k: v for k, v in declaration.items() if k != 'method_digest'}):
        raise ValueError('Rule method declaration identity differs')
    if (declaration['id'], declaration['version']) in {(m.id, m.version) for m in builtin_methods()}:
        raise ValueError('Custom methods cannot replace a built-in method identity')
    return deepcopy(declaration)


def _number(value):
    if type(value) not in (int, float):
        raise ValueError('Evidence needs finite numeric values')
    return Fraction(str(value))


def _sha(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def convergence(rule, facts, subject_digest):
    required = {'status', 'completed_epochs', 'steps', 'runtime_seconds', 'cost',
                'checkpoint_digest', 'expected_checkpoint_digest', 'observations', 'continuation', 'resource_units'}
    if set(facts) != required:
        raise ValueError('Convergence evidence fields differ')
    if facts['status'] == 'cancelled':
        return 'cancelled', ['cancelled'], {}
    if facts['status'] != 'completed':
        raise ValueError('Only a completed epoch is eligible for continuation')
    if facts['resource_units'] != {'epochs': 'complete_dataset_pass', 'steps': rule['step_unit'],
                                   'runtime_seconds': 'seconds', 'cost': rule['cost_unit']}:
        raise ValueError('Measured resource units differ from the selected versioned policy')
    checkpoint = facts['checkpoint_digest']
    if not _sha(checkpoint) or checkpoint != facts['expected_checkpoint_digest']:
        raise ValueError('Checkpoint identity differs or is missing')
    for key in ('completed_epochs', 'steps'):
        if type(facts[key]) is not int or facts[key] < 0:
            raise ValueError('Epoch and step counts must be nonnegative integers')
    for key in ('runtime_seconds', 'cost'):
        if _number(facts[key]) < 0:
            raise ValueError('Resource consumption must be nonnegative')
    history = facts['observations']
    if not isinstance(history, list) or not history or len(history) > 10000:
        raise ValueError('Metric evidence is missing or oversized')
    prior = 0
    for item in history:
        if (not isinstance(item, dict) or set(item) != {'epoch', 'metric', 'value', 'checkpoint_digest', 'subject_digest'}
                or type(item['epoch']) is not int or not prior < item['epoch'] <= facts['completed_epochs']
                or item['metric'] != rule['metric'] or not _sha(item['checkpoint_digest'])
                or item['subject_digest'] != subject_digest):
            raise ValueError('Metric evidence is unordered, conflicting or unidentified')
        prior = item['epoch']
        _number(item['value'])
    if prior != facts['completed_epochs'] or history[-1]['checkpoint_digest'] != checkpoint:
        raise ValueError('Latest metric does not identify the completed checkpoint')
    if any(b['epoch'] != a['epoch'] + 1 for a, b in zip(history, history[1:])):
        raise ValueError('Metric history contains missing epochs')
    if rule['criterion'] == 'target':
        latest = _number(history[-1]['value'])
        reached = latest <= _number(rule['threshold']) if rule['direction'] == 'minimize' else latest >= _number(rule['threshold'])
        measured = {'value': str(latest)}
    elif len(history) < rule['window']:
        if rule['insufficient_window'] == 'block':
            raise ValueError('Insufficient metric window')
        reached, measured = False, {'window_status': 'collecting', 'observations': len(history), 'required': rule['window']}
    else:
        tail = history[-rule['window']:]
        if [x['epoch'] for x in tail] != list(range(prior - rule['window'] + 1, prior + 1)):
            raise ValueError('Metric window contains missing epochs')
        values = [_number(x['value']) for x in tail]
        # A degrading curve is not silently declared converged. Every adjacent
        # directed improvement must be nonnegative and within the selected bound.
        improvements = [(a-b if rule['direction'] == 'minimize' else b-a) for a, b in zip(values, values[1:])]
        reached = all(0 <= x <= _number(rule['threshold']) for x in improvements)
        measured = {'directed_improvements': [str(x) for x in improvements]}
    limits = [('completed_epochs', 'max_epochs', 1), ('steps', 'max_steps', rule['next_epoch_steps']),
              ('runtime_seconds', 'max_runtime_seconds', rule['next_epoch_seconds']),
              ('cost', 'max_cost', rule['next_epoch_cost'])]
    if any(rule['resource_modes'][used] == 'not_applicable' and facts[used] != 0 for used in ('steps', 'runtime_seconds', 'cost')):
        raise ValueError('Not-applicable resource counter has measured consumption')
    active = [row for row in limits if row[0] == 'completed_epochs' or rule['resource_modes'][row[0]] == 'active']
    exhausted = [bound for used, bound, increment in active
                 if _number(facts[used]) >= _number(rule[bound]) or _number(facts[used]) + _number(increment) > _number(rule[bound])]
    overrun = [bound for used, bound, _ in limits if _number(facts[used]) > _number(rule[bound])]
    if overrun:
        return 'blocked', ['resource_bound_exceeded:' + key for key in overrun], measured
    if reached and facts['completed_epochs'] >= rule['min_epochs']:
        return 'stop', ['criterion_met'], measured
    if exhausted:
        return 'stop', ['resource_bound:' + key for key in exhausted], {**measured, 'criterion_met': False}
    continuation = facts['continuation']
    if (not isinstance(continuation, dict) or set(continuation) != {'mode', 'supported'}
            or continuation['mode'] != 'exact_state' or type(continuation['supported']) is not bool
            or continuation['supported'] is not True):
        return 'blocked', ['continuation_unsupported'], measured
    return 'continue', ['collecting_window' if measured.get('window_status') == 'collecting' else 'minimum_epochs' if reached else 'criterion_not_met'], measured


def representation(rule, facts):
    if set(facts) != {'before', 'after'}:
        raise ValueError('Representation requires before and after counts')
    before, after = facts['before'], facts['after']
    if (not isinstance(before, dict) or not before or not isinstance(after, dict)
            or set(before) != set(after) or any(type(x) is not int or x < 0 for x in [*before.values(), *after.values()])
            or any(after[key] > before[key] for key in before) or not sum(before.values()) or not sum(after.values())):
        raise ValueError('Representation counts are missing or inconsistent')
    total = sum(after.values())
    reasons, rows = [], []
    for key in rule['required_strata']:
        if before.get(key, 0) <= 0:
            reasons.append('required_stratum_absent:' + key)
        if after.get(key, 0) <= 0:
            reasons.append('required_stratum_not_retained:' + key)
    for key in sorted(before):
        target = Fraction(before[key], sum(before.values())) if rule['allocation'] == 'proportional' else Fraction(1, len(before))
        error = abs(Fraction(after[key], total) - target)
        if after[key] < rule['min_per_stratum']:
            reasons.append('minimum_coverage:' + key)
        if error > _number(rule['max_share_error']):
            reasons.append('share_error:' + key)
        rows.append({'stratum': key, 'before': before[key], 'after': after[key], 'target_share': str(target), 'share_error': str(error)})
    return ('blocked' if reasons else 'passed'), reasons or ['representation_preserved'], {'strata': rows,
        'coverage_status': 'declared' if rule['required_strata'] else 'unspecified'}


def identity_rule(rule, facts, subject):
    if set(facts) != {'actual'} or not isinstance(facts['actual'], dict):
        raise ValueError('Identity evidence requires actual identities')
    reasons = ['identity_mismatch:' + key for key in sorted(rule['expected'])
               if key not in facts['actual'] or digest(facts['actual'][key]) != digest(rule['expected'][key])]
    return ('blocked' if reasons else 'passed'), reasons or ['identities_match'], {'actual': facts['actual']}


def overlap_rule(rule, facts, subject):
    if set(facts) != {'scope', 'overlap_count', 'checked_identity_digest'} or facts['scope'] != rule['scope']:
        raise ValueError('Overlap scope differs or evidence is missing')
    if type(facts['overlap_count']) is not int or facts['overlap_count'] < 0 or not _sha(facts['checked_identity_digest']):
        raise ValueError('Overlap evidence is invalid')
    outcome = 'passed' if facts['overlap_count'] <= rule['max_overlap'] else 'blocked'
    return outcome, ['overlap_within_bound' if outcome == 'passed' else 'overlap_exceeds_bound'], facts


def builtin_methods():
    return [Method('convergence', '1', {'module': __name__, 'entrypoint': 'convergence'}, CONVERGENCE,
                   lambda rule, facts, subject: convergence(rule, facts, digest(subject))),
            Method('representation', '1', {'module': __name__, 'entrypoint': 'representation'}, REPRESENTATION,
                   lambda rule, facts, subject: representation(rule, facts)),
            Method('identity', '1', {'module': __name__, 'entrypoint': 'identity_rule'}, IDENTITY, identity_rule),
            Method('overlap', '1', {'module': __name__, 'entrypoint': 'overlap_rule'}, OVERLAP, overlap_rule)]


def method_catalog():
    return [{'id': m.id, 'version': m.version, 'implementation': m.implementation,
             'parameter_schema': m.parameter_schema, 'parameter_schema_digest': digest(m.parameter_schema)}
            for m in builtin_methods()]


def evaluate_policy(policy, evidence, *, extensions=()) -> Decision:
    policy = validate_policy(policy)
    evidence = deepcopy(evidence)
    digest(evidence)
    reports = []
    registered = {(m.id, m.version): m for m in builtin_methods()}
    for method in extensions:
        if (method.id, method.version) in registered:
            raise ValueError('Extensions cannot replace a registered method identity')
        registered[(method.id, method.version)] = method
    envelope_error = None
    envelope_fields = {'schema_version', 'subject', 'source', 'verification', 'facts'}
    if (not isinstance(evidence, dict) or set(evidence) != envelope_fields
            or evidence.get('schema_version') != 'modelbuilder.rule-evidence/v1'
            or evidence.get('verification') != 'verified'
            or not isinstance(evidence.get('source'), str) or not evidence['source']
            or not isinstance(evidence.get('subject'), dict)
            or set(evidence['subject']) != {'project_id', 'run_id', 'config_digest', 'data_digest'}
            or any(not isinstance(x, str) or not x for x in evidence['subject'].values())
            or not _sha(evidence['subject']['config_digest']) or not _sha(evidence['subject']['data_digest'])
            or not isinstance(evidence.get('facts'), dict)):
        envelope_error = 'Evidence requires a verified versioned subject and source'
    facts_map = evidence.get('facts', {}) if isinstance(evidence, dict) else {}
    facts_map = facts_map if isinstance(facts_map, dict) else {}
    if set(facts_map) != {rule['id'] for rule in policy['rules']}:
        envelope_error = 'Evidence facts must match declared rule identities'
    for rule in policy['rules']:
        facts = facts_map.get(rule['id'])
        try:
            if envelope_error:
                raise ValueError(envelope_error)
            if not isinstance(facts, dict):
                raise ValueError('Evidence is missing')
            custom = rule['kind'] == 'method/v1'
            key = (rule['method']['id'], rule['method']['version']) if custom else tuple(rule['kind'].replace('/v', '/').split('/'))
            method = registered.get(key)
            if method is None:
                raise ValueError('Unknown rule method: ' + '/'.join(key))
            if custom:
                if digest(method.implementation) != digest(rule['method']) or digest(method.parameter_schema) != rule['method']['parameter_schema_digest']:
                    raise ValueError('Registered method implementation/schema identity differs')
                Draft202012Validator(method.parameter_schema).validate(rule['parameters'])
                value = method.evaluate(facts=deepcopy(facts), parameters=deepcopy(rule['parameters']), subject=deepcopy(evidence['subject']))
                if (not isinstance(value, dict) or set(value) != {'outcome', 'reasons', 'observed'}
                        or value['outcome'] not in {'passed', 'blocked', 'continue', 'stop', 'cancelled'}
                        or not isinstance(value['reasons'], list) or not value['reasons']
                        or any(type(x) is not str or not x or len(x) > 200 for x in value['reasons'])
                        or not isinstance(value['observed'], dict)):
                    raise ValueError('Custom method result violates its typed decision contract')
                allowed = {'passed', 'blocked'} if rule['method']['decision_domain'] == 'predicate' else {'continue', 'stop', 'blocked', 'cancelled'}
                if value['outcome'] not in allowed:
                    raise ValueError('Custom method outcome differs from its declared decision domain')
                digest(value)
                outcome, reasons, observed = value['outcome'], value['reasons'], value['observed']
            else:
                outcome, reasons, observed = method.evaluate(rule, facts, evidence['subject'])
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            outcome, reasons, observed = 'blocked', ['invalid_evidence'], {'detail': str(exc)}
        reports.append({'id': rule['id'], 'kind': rule['kind'], 'outcome': outcome,
                        'reasons': reasons, 'selected_options': rule, 'observed': observed})
    outcomes = {r['outcome'] for r in reports}
    conflict = {'stop', 'continue'} <= outcomes
    outcome = ('cancelled' if 'cancelled' in outcomes else 'blocked' if 'blocked' in outcomes or conflict
               else 'stop' if 'stop' in outcomes else 'continue' if 'continue' in outcomes else 'passed')
    result = {'schema_version': 'modelbuilder.rule-decision/v1', 'outcome': outcome,
              'reasons': ['conflicting_decisions'] if conflict else [reason for row in reports for reason in row['reasons']],
              'policy_digest': digest(policy), 'evidence_digest': digest(evidence), 'rules': reports}
    return {**result, 'digest': digest(result)}


def plan_quotas(counts, total, allocation, minimum, required=()):
    """Pure bounded water filling plus largest remainders; O(strata log strata).

    Integer lower/upper bounds are constructive. The result minimizes deviation
    from the requested proportional/equal targets without per-selected-row work.
    """
    if (not counts or any(type(v) is not int or v <= 0 for v in counts.values())
            or type(total) is not int or not 0 <= total <= sum(counts.values())
            or type(minimum) is not int or minimum < 0 or allocation not in {'equal', 'proportional'}):
        raise ValueError('Invalid quota count/allocation contract')
    if set(required) - counts.keys():
        raise ValueError('A declared required stratum is absent from the input split')
    lower = {key: max(minimum, 1 if key in required else 0) for key in counts}
    if any(lower[key] > counts[key] for key in counts):
        raise ValueError('Selected minimum exceeds a stratum capacity')
    if sum(lower.values()) > total:
        raise ValueError('Selected fraction cannot meet minimum stratum coverage')
    targets = {key: (Fraction(total * count, sum(counts.values())) if allocation == 'proportional'
                     else Fraction(total, len(counts))) for key, count in counts.items()}
    events = {}
    for key in counts:
        start, end = lower[key] - targets[key], counts[key] - targets[key]
        events[start] = events.get(start, 0) + 1
        events[end] = events.get(end, 0) - 1
    positions = sorted(events)
    previous, achieved, slope = positions[0], Fraction(sum(lower.values())), 0
    shift = previous
    for position in positions:
        next_total = achieved + slope * (position - previous)
        if slope and next_total >= total:
            shift = previous + Fraction(total - achieved, slope)
            break
        achieved, previous = next_total, position
        slope += events[position]
        shift = position
    desired = {key: min(Fraction(counts[key]), max(Fraction(lower[key]), targets[key] + shift)) for key in counts}
    result = {key: int(value) for key, value in desired.items()}
    missing = total - sum(result.values())
    eligible = sorted((key for key in counts if result[key] < counts[key]), key=lambda key: (-(desired[key] - result[key]), key))
    for key in eligible[:missing]:
        result[key] += 1
    if sum(result.values()) != total or any(not lower[key] <= result[key] <= counts[key] for key in counts):
        raise ValueError('Quota planner failed its exact total/capacity contract')
    return result


def validate_sampling_spec(spec):
    """Shared eager/streaming policy checks; return canonical required keys."""
    Draft202012Validator(SAMPLE_SCHEMA).validate(spec)
    if type(spec['min_per_stratum']) is not int:
        raise ValueError('Minimum coverage requires an actual integer')
    digest(spec)
    if any(set(value) != set(spec['fields']) for value in spec['required_strata']):
        raise ValueError('Required strata must declare exactly the selected fields')
    required = [digest(value) for value in spec['required_strata']]
    if len(required) != len(set(required)):
        raise ValueError('Required strata must be unique')
    return required


def sampling_quotas(counts, spec, required):
    """Construct coverage before checking the selected representation tolerance."""
    total = int(Fraction(sum(counts.values())) * _number(spec['fraction']))
    allocation = plan_quotas(counts, total, spec['allocation'], spec['min_per_stratum'], required)
    rule = {key: spec[key] for key in ('allocation', 'min_per_stratum', 'max_share_error')}
    outcome, reasons, observed = representation({**rule, 'required_strata': required},
                                               {'before': counts, 'after': allocation})
    if outcome != 'passed':
        raise ValueError('Sampling representation blocked: ' + ','.join(reasons))
    return allocation, observed


def sample_rows(indexed, spec):
    """Largest-remainder allocation, SHA256 ranking, stable original row order."""
    required = validate_sampling_spec(spec)
    ids = [item['sample_id'] for item in indexed]
    if any(type(value) is not str or not value for value in ids) or len(set(ids)) != len(ids):
        raise ValueError('Sampling requires unique nonempty sample identities')
    groups = {}
    for item in indexed:
        if item['split_id'] not in spec['split_ids']:
            continue
        if any(field not in item['row'] or item['row'][field] is None for field in spec['fields']):
            raise ValueError('Sampling stratum evidence is missing')
        stratum = digest({key: item['row'][key] for key in spec['fields']})
        groups.setdefault(item['split_id'], {}).setdefault(stratum, []).append(item)
    if set(groups) != set(spec['split_ids']):
        raise ValueError('Sampling split is unknown or empty')
    selected, reports = set(), []
    for split, strata in sorted(groups.items()):
        counts = {key: len(rows) for key, rows in strata.items()}
        allocation, observed = sampling_quotas(counts, spec, required)
        for row in observed['strata']:
            row['stratum_values'] = {key: strata[row['stratum']][0]['row'][key] for key in spec['fields']}
        for key, rows in strata.items():
            ranked = sorted(rows, key=lambda x: (digest({'seed': spec['seed'], 'sample_id': x['sample_id']}), x['sample_id']))
            selected.update(x['sample_id'] for x in ranked[:allocation[key]])
        reports.append({'split_id': split, **observed})
    keep = [x for x in indexed if x['split_id'] not in spec['split_ids'] or x['sample_id'] in selected]
    before_sources = Counter(x.get('source_id', '(unspecified)') for x in indexed if x['split_id'] in spec['split_ids'])
    after_sources = Counter(x.get('source_id', '(unspecified)') for x in keep if x['split_id'] in spec['split_ids'])
    return keep, {'representation': reports, 'selected_sample_ids': sorted(selected),
                  'selection_digest': digest(sorted(selected)), 'policy_digest': digest(spec),
                  'source_counts': [{'source_id': key, 'before': before_sources[key], 'after': after_sources[key]} for key in sorted(before_sources)]}
