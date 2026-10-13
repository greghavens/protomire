"""Inert policy storage and preview on the shared authenticated operation path."""
from .evaluation import digest
from .rules import POLICY_SCHEMA, METHOD_SCHEMA, evaluate_policy, validate_policy, validate_method, method_catalog
from .models import ServiceError

GATE_SELECTOR = {'type': 'object', 'additionalProperties': False,
    'properties': {**{key: {'type': 'string', 'minLength': 1} for key in ('backend_run_id', 'node_id', 'artifact_id')},
                   'content_sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}},
    'required': ['backend_run_id', 'node_id', 'artifact_id', 'content_sha256']}


def operation_specs(spec):
    project = {'project_id': {'type': 'string', 'minLength': 1}}
    pin = {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}
    return [spec('rules.methods', 'read', 'List inert built-in and project-registered rule method declarations.', project, ('project_id',)),
            spec('rules.method.register', 'modify', 'Register a pinned source/schema method declaration without importing or executing its code.',
                 {**project, 'method': METHOD_SCHEMA}, ('project_id', 'method'), True),
            spec('rules.policy.activate', 'modify', 'Explicitly activate a pinned policy for selected operation and node-role scopes.',
                 {**project, 'policy_digest': pin, 'expected_revision': {'type': 'integer', 'minimum': 1},
                  'operations': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': {'enum': ['run.submit', 'run.resume', 'model.export.package', 'model.version.promote']}},
                  'node_roles': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': {'enum': ['training', 'export', 'model', 'dataset', 'utility', 'evaluation', 'derivation']}}},
                 ('project_id', 'policy_digest', 'expected_revision', 'operations', 'node_roles'), True),
            spec('rules.policy.deactivate', 'modify', 'Explicitly deactivate the active safeguard with optimistic project revision fencing.',
                 {**project, 'expected_revision': {'type': 'integer', 'minimum': 1}}, ('project_id', 'expected_revision'), True),
            spec('rules.policy.register', 'modify', 'Register immutable selected safeguards; does not authorize execution.',
                 {**project, 'policy': POLICY_SCHEMA}, ('project_id', 'policy'), True),
            spec('rules.policy.get', 'read', 'Read a content-addressed selected policy.',
                 {**project, 'policy_digest': pin}, ('project_id', 'policy_digest')),
            spec('rules.evaluate', 'read', 'Preview deterministic decisions from supplied evidence; preview never authorizes an action.',
                 {**project, 'policy_digest': pin, 'evidence': {'type': 'object'}},
                 ('project_id', 'policy_digest', 'evidence'))]


def dispatch(service, operation, payload):
    pid = payload['project_id']
    if operation == 'rules.policy.deactivate':
        project = service._get('project', pid, pid)
        service._revision(project, payload['expected_revision'])
        return service.store.put('project', pid, pid, {**project, 'rule_policy_activation': None, 'revision': project['revision'] + 1}, project['revision'])
    if operation == 'rules.methods':
        return {'builtin': method_catalog(), 'registered': service.store.list('rule_method', pid),
                'custom_execution': 'Trusted native rule component through run.submit; read-only preview never executes custom methods.'}
    if operation == 'rules.method.register':
        try:
            declaration = validate_method(payload['method'])
        except ValueError as error:
            raise ServiceError('invalid_rule_method', str(error)) from error
        existing = [x for x in service.store.list('rule_method', pid) if (x['method']['id'], x['method']['version']) == (declaration['id'], declaration['version'])]
        if existing:
            if existing[0]['method_digest'] != declaration['method_digest']:
                raise ServiceError('rule_method_version_conflict', 'Changing method source/schema requires a new version', status_code=409)
            return existing[0]
        return service.store.put('rule_method', pid + ':' + declaration['method_digest'], pid,
                                 {'project_id': pid, 'method_digest': declaration['method_digest'], 'method': declaration, 'revision': 1})
    if operation == 'rules.policy.register':
        try:
            policy = validate_policy(payload['policy'])
        except ValueError as error:
            raise ServiceError('invalid_rule_policy', str(error)) from error
        pin = digest(policy)
        for rule in policy['rules']:
            if rule['kind'] == 'method/v1':
                methods = service.store.list('rule_method', pid)
                if not any(x['method_digest'] == rule['method']['method_digest'] and digest(x['method']) == digest(rule['method']) for x in methods):
                    raise ServiceError('unknown_rule_method', 'Register the exact project-owned source/schema method declaration before its policy')
        key = pid + ':' + pin
        existing = [x for x in service.store.list('rule_policy', pid) if x['policy_digest'] == pin]
        if any(x['policy']['id'] == policy['id'] and x['policy']['version'] == policy['version']
               and x['policy_digest'] != pin for x in service.store.list('rule_policy', pid)):
            raise ServiceError('rule_policy_version_conflict', 'Changing selected options requires a new policy version', status_code=409)
        return existing[0] if existing else service.store.put('rule_policy', key, pid,
            {'project_id': pid, 'policy_digest': pin, 'policy': policy, 'revision': 1})
    row = service._get('rule_policy', pid + ':' + payload['policy_digest'], pid)
    if digest(row['policy']) != payload['policy_digest']:
        raise ServiceError('rule_policy_changed', 'Stored rule policy identity differs')
    if operation == 'rules.policy.get':
        return row
    if operation == 'rules.policy.activate':
        project = service._get('project', pid, pid)
        service._revision(project, payload['expected_revision'])
        activation = {'policy_digest': row['policy_digest'], 'operations': sorted(payload['operations']),
                      'node_roles': sorted(payload['node_roles']), 'revision': project['revision'] + 1}
        return service.store.put('project', pid, pid, {**project, 'rule_policy_activation': activation, 'revision': project['revision'] + 1}, project['revision'])
    if any(rule['kind'] == 'method/v1' for rule in row['policy']['rules']):
        raise ServiceError('custom_rule_execution_required', 'Custom methods require a source-pinned native gate and execute authority; read-only preview never imports their code', status_code=409)
    subject = payload['evidence'].get('subject')
    if not isinstance(subject, dict) or subject.get('project_id') != pid:
        raise ServiceError('rule_evidence_project_mismatch', 'Evidence belongs to another project')
    return evaluate_policy(row['policy'], payload['evidence'])


def enforce_activation(service, operation, project, payload):
    """No registration side effects. Only explicit activation governs actions."""
    activation = project.get('rule_policy_activation')
    actual_operation = 'run.submit' if operation == 'run.plan' else operation
    if not activation:
        return
    if actual_operation in {'model.export.package', 'model.version.promote'}:
        if actual_operation not in activation['operations']:
            return
        if not payload.get('rule_gate'):
            raise ServiceError('active_rule_decision_required', 'The active safeguard requires an exact completed native gate artifact selector', status_code=409)
        policy = service._get('rule_policy', project['project_id'] + ':' + activation['policy_digest'], project['project_id'])['policy']
        if any(rule['kind'] == 'convergence/v1' or rule['kind'] == 'method/v1' and rule['method']['decision_domain'] != 'predicate' for rule in policy['rules']):
            raise ServiceError('active_rule_action_method_unsupported', 'Standalone actions require predicate policies; continuation decisions do not authorize export or promotion')
        service._find_operation(project['project_id'], backend_run_id=payload['rule_gate']['backend_run_id'])
        from .backend.execution_rights import _store_guard
        guard = _store_guard(service.store.path)
        if guard is None:
            raise ServiceError('active_rule_authority_unavailable', 'Standalone native safeguards require a durable service store')
        payload['rule_authorization'] = {'project_id': project['project_id'], 'activation': activation,
            'policy': policy, 'store_guard': guard}
        return
    if actual_operation not in {'run.submit', 'run.resume'}:
        return
    from .execution import selection
    from .backend.execution_rights import _store_guard
    recipe = payload['recipe']
    selected = set(selection(recipe, payload.get('execution'))['node_ids'])
    roles = {'model.train': 'training', 'model.export': 'export', 'model.initialize': 'model',
             'model.evaluate': 'evaluation', 'model.checkpoint': 'training', 'model.compare': 'evaluation'}
    nodes = {node['id']: node for node in recipe['nodes']}
    protected = [node for node in nodes.values() if actual_operation in activation['operations'] and node['id'] in selected
        and (node.get('component', {}).get('role') or roles.get(node['kind'], 'dataset' if node['kind'].startswith('dataset.') else 'utility')) in activation['node_roles']
        and node.get('component', {}).get('id') != 'modelbuilder.rules.gate']
    policy = service._get('rule_policy', project['project_id'] + ':' + activation['policy_digest'], project['project_id'])['policy']
    for node in protected:
        guards = node.get('required_guards', [])
        if len(guards) != 1 or guards[0] not in selected:
            raise ServiceError('active_rule_guard_required', 'Active user-selected policy requires a selected native guard for every scoped action node', {'node_id': node['id']}, 409)
        guard = nodes[guards[0]]
        if digest(guard.get('parameters', {}).get('policy')) != digest(policy):
            raise ServiceError('active_rule_policy_mismatch', 'Scoped action guard differs from the active pinned policy', {'node_id': node['id']}, 409)
    # A durable worker recheck is mandatory even if this selection has no scoped
    # node; a future project activation change must not be silently substituted.
    store_guard = _store_guard(service.store.path)
    if store_guard is None:
        raise ServiceError('active_rule_authority_unavailable', 'Active native safeguards require a durable service store')
    payload['rule_authorization'] = {'project_id': project['project_id'], 'activation': activation, 'store_guard': store_guard}
