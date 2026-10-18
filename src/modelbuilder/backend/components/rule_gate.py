"""Fail-closed source-bound rule gate for native typed control dependencies."""
from pathlib import Path
from . import PROTOCOL, identity
from ...rules import POLICY_SCHEMA, evaluate_policy


def validate_checkpoint_state(checkpoint):
    if (not isinstance(checkpoint, dict) or set(checkpoint) != {'schema_version', 'resume_modes', 'state'}
            or checkpoint['schema_version'] != 'modelbuilder.rule-checkpoint-state/v1'
            or checkpoint['resume_modes'] != ['exact_state'] or not isinstance(checkpoint['state'], dict)
            or set(checkpoint['state']) != {'model', 'optimizer', 'scheduler', 'rng', 'data_cursor'}):
        raise ValueError('rule_blocked:checkpoint_state_contract_invalid')
    for key, value in checkpoint['state'].items():
        if not isinstance(value, dict):
            raise ValueError('rule_blocked:checkpoint_state_incomplete')
        if value.get('kind') == 'no_op' and key in {'optimizer', 'scheduler', 'rng'}:
            if set(value) != {'kind', 'reason'} or type(value['reason']) is not str or not value['reason']:
                raise ValueError('rule_blocked:checkpoint_no_op_reason_required')
        elif (set(value) != {'kind', 'value'} or value['kind'] != 'value' or value['value'] is None
              or isinstance(value['value'], (dict, list, str)) and not value['value']):
            raise ValueError('rule_blocked:checkpoint_state_incomplete')
    identity(checkpoint)
    return checkpoint


def catalog_entries():
    descriptor = {'id': 'modelbuilder.rules.gate', 'version': '1', 'protocol': PROTOCOL,
        'role': 'utility', 'description': 'Evaluate user-selected deterministic safeguards; blocked evidence fails the native step.',
        'parameter_schema': {'type': 'object', 'additionalProperties': False,
                             'properties': {'policy': POLICY_SCHEMA, 'target_checkpoint_kind': {'enum': ['best', 'last']}}, 'required': ['policy']},
        'capabilities': {'devices': ['cpu'], 'resume_modes': [], 'export_formats': [], 'frameworks': ['python']}}
    return [{**descriptor, 'component_digest': identity(descriptor),
        'inputs': [{'name': 'evidence', 'type': 'dict'}, {'name': 'checkpoint', 'type': 'Path', 'required': False},
                   {'name': 'checkpoint_state', 'type': 'dict', 'required': False}, {'name': 'target', 'type': 'Path', 'required': False}],
        'outputs': [{'name': 'allowed', 'type': 'bool'}, {'name': 'done', 'type': 'bool'}, {'name': 'result', 'type': 'dict'}],
        'adapter': {'module': __name__, 'entrypoint': 'gate', 'source_binding': 'Pin a reviewed wrapper.',
                    'example_source': 'examples/rules/adapter.py'}}]


def gate(*, inputs, parameters, context):
    cancel = context.get('cancel_file')
    if cancel and Path(cancel).exists():
        raise RuntimeError('rule_cancelled')
    if any(rule['kind'] == 'family_exposure/v1' for rule in parameters['policy']['rules']):
        if 'target' not in inputs:
            raise ValueError('rule_blocked:family_exposure_native_target_required')
        from ..rule_attestations import verify_family_exposure_target
        verify_family_exposure_target(parameters, inputs['evidence'], inputs['target'])
    continuation_rules = [rule for rule in parameters['policy']['rules'] if rule['kind'] == 'convergence/v1'
                          or rule['kind'] == 'method/v1' and rule['method']['decision_domain'] == 'continuation']
    if continuation_rules:
        if ('checkpoint' in inputs) == ('checkpoint_state' in inputs):
            raise ValueError('rule_blocked:checkpoint_input_required')
        if 'checkpoint' in inputs:
            from . import read_checkpoint
            checkpoint = read_checkpoint(inputs['checkpoint'])
        else:
            checkpoint = validate_checkpoint_state(inputs['checkpoint_state'])
        actual = identity(checkpoint)
        for rule in continuation_rules:
            facts = inputs['evidence'].get('facts', {}).get(rule['id'], {})
            if actual != facts.get('checkpoint_digest'):
                raise ValueError('rule_blocked:checkpoint_input_mismatch')
            if ('expected_checkpoint_digest' in facts and actual != facts['expected_checkpoint_digest']):
                raise ValueError('rule_blocked:checkpoint_input_mismatch')
            if facts.get('status') != 'completed':
                raise ValueError('rule_blocked:completed_epoch_required')
    result = evaluate_policy(parameters['policy'], inputs['evidence'], extensions=context.get('rule_methods', ()))
    # Execution capability is a platform invariant, independent of the selected
    # method's scientific criterion. A stop requires no further resume capability.
    for decision in result['rules']:
        if decision['outcome'] != 'continue':
            continue
        continuation = inputs['evidence']['facts'][decision['id']].get('continuation')
        if (not isinstance(continuation, dict) or set(continuation) != {'mode', 'supported'}
                or continuation['mode'] != 'exact_state' or type(continuation['supported']) is not bool
                or continuation['supported'] is not True or 'exact_state' not in checkpoint['resume_modes']):
            raise ValueError('rule_blocked:continuation_unsupported')
    if result['outcome'] in {'blocked', 'cancelled'}:
        raise ValueError('rule_' + result['outcome'] + ':' + ','.join(result['reasons']))
    return {'allowed': result['outcome'] in {'passed', 'continue'},
            'done': result['outcome'] == 'stop', 'result': result}
