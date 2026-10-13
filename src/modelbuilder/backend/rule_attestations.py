"""Inspect completed native rule decisions without importing method/source code."""
from copy import deepcopy
from pathlib import Path
from .components import identity, read_checkpoint, read_export
from .common import read_json, recipe_provenance
from .native_inputs import storage_identity, validate_native_type
from .rule_activation import recheck


def target_descriptor(path, checkpoint_kind=None):
    path = Path(path)
    if (path/'export.manifest.json').is_file():
        descriptor = read_export(path)
        if checkpoint_kind is not None:
            raise ValueError('Checkpoint selection cannot target an export')
        return {'kind': 'export', 'descriptor_digest': identity(descriptor),
                'checkpoint_digest': descriptor['selected_checkpoint']}
    if (path/'checkpoint.manifest.json').is_file():
        descriptor = read_checkpoint(path)
        result = {'kind': 'checkpoint', 'descriptor_digest': identity(descriptor), 'checkpoint_digest': identity(descriptor), 'checkpoint_selection': None}
        if checkpoint_kind is not None:
            from .fixture_artifacts import selected_identity
            selected = selected_identity(descriptor, checkpoint_kind)
            result.update(checkpoint_digest=selected, checkpoint_selection={'kind': checkpoint_kind, **descriptor['selections'][checkpoint_kind]})
        return result
    raise ValueError('Rule target requires an explicit supported export/checkpoint descriptor')


def inspect_gate(payload, root):
    from .sdk import native_run
    from .native_invocations import output_step
    from .scientific_gate_artifacts import input_receipts
    authorization = payload['rule_authorization']
    recheck({'rule_authorization': authorization})
    selector = payload['rule_gate']
    matches = [(job, read_json(job/'request.json')) for job in (root/'jobs').glob('*')
               if read_json(job/'request.json').get('project_id') == payload['project_id']
               and read_json(job/'native.json').get('backend_run_id') == selector['backend_run_id']]
    if len(matches) != 1:
        raise ValueError('Rule gate belongs to an unknown or foreign project/run')
    job, request = matches[0]
    _, run = native_run(job)
    if not run or run.status.value != 'completed' or request.get('trusted') is not True:
        raise ValueError('Rule gate requires a completed trusted native run')
    if any(run.config.extra.get(key) != value for key, value in recipe_provenance(request['recipe']).items()):
        raise ValueError('Rule gate recipe provenance differs')
    node = next((x for x in request['recipe']['nodes'] if x['id'] == selector['node_id']), None)
    if not node or node.get('component', {}).get('id') != 'modelbuilder.rules.gate':
        raise ValueError('Selected output is not a registered native rule gate')
    invocation, step = output_step(run, request['recipe'], node['id'], 'result', selector['artifact_id'])
    if step.status.value != 'completed':
        raise ValueError('Rule gate output is cached or incomplete')
    artifact = next(a for a in step.outputs['result'] if str(a.id) == selector['artifact_id'])
    validate_native_type(artifact, 'dict')
    if str(artifact.project.id) != str(run.project.id) or storage_identity(artifact, root) != selector['content_sha256']:
        raise ValueError('Rule result project/content identity differs')
    result = artifact.load()
    policy = authorization['policy']
    if (identity(node['parameters']['policy']) != identity(policy) or result.get('policy_digest') != identity(policy)
            or result.get('digest') != identity({k:v for k,v in result.items() if k != 'digest'})
            or result.get('outcome') != 'passed'):
        raise ValueError('Rule result is failed, substituted, or differs from the active policy')
    native = result.get('native_execution', {})
    from .components.runtime import runtime_source_identity
    if native.get('runtime_source_digest') != runtime_source_identity():
        raise ValueError('Rule gate execution runtime differs; evaluate a fresh decision')
    types = {port['name']: port['type'] for port in node['inputs'] if port['name'] in step.inputs}
    receipts = input_receipts(step, root, tuple(sorted(types)), types)
    producer = {'project_id': payload['project_id'], 'backend_project_id': str(run.project.id),
                'backend_run_id': str(run.id), 'node_id': node['id'], 'invocation_id': invocation['invocation_id']}
    methods = {(rule['method']['id'], rule['method']['version']): rule['method'] for rule in policy['rules'] if rule['kind'] == 'method/v1'}
    if (native.get('producer') != producer or identity(native.get('activation')) != identity(authorization['activation'])
            or native.get('inputs') != receipts or native.get('source_digest') != identity(request['recipe']['source'])
            or native.get('evaluator') != {'component_digest': identity(node['component']), 'implementation_ref': node['implementation_ref']}
            or identity(native.get('methods')) != identity(list(methods.values()))):
        raise ValueError('Rule gate source, activation revision or native evidence changed')
    evidence = step.inputs['evidence'][0].load()
    if result['evidence_digest'] != identity(evidence) or any(identity(row['selected_options']) != identity(rule) or row['outcome'] != 'passed'
            for row, rule in zip(result.get('rules', []), policy['rules'])) or len(result.get('rules', [])) != len(policy['rules']):
        raise ValueError('Rule decision evidence/options differ')
    allowed = step.outputs.get('allowed', [])
    if len(allowed) != 1:
        raise ValueError('Rule gate has no unique native allowed output')
    validate_native_type(allowed[0], 'bool')
    if allowed[0].load() is not True:
        raise ValueError('Rule gate did not allow the action')
    target = native.get('target')
    if not isinstance(target, dict) or target.get('input_port') != 'target':
        raise ValueError('Rule gate is not bound to a native target artifact')
    from .native_exports import native_directory
    target_artifacts = step.inputs.get('target', [])
    if len(target_artifacts) != 1:
        raise ValueError('Rule gate target is missing or ambiguous')
    target_artifact = target_artifacts[0]
    receipt = next(row for row in receipts if row['input_port'] == 'target')
    with native_directory(target_artifact, root) as path:
        expected = {**receipt, **target_descriptor(path, node['parameters'].get('target_checkpoint_kind'))}
    if identity(target) != identity(expected):
        raise ValueError('Rule gate target descriptor changed')
    recheck({'rule_authorization': authorization})
    return deepcopy(target)


def verify_action(payload, root, *, kind, artifact_id, run_id, model_version_id, checkpoint_digest, checkpoint_selection=None, descriptor_digest=None):
    authorization = payload.get('rule_authorization')
    if not authorization:
        return None
    target = inspect_gate(payload, root)
    if (target['kind'] != kind or target['artifact_id'] != artifact_id or target['backend_run_id'] != run_id
            or target['checkpoint_digest'] != checkpoint_digest
            or kind == 'checkpoint' and (target.get('checkpoint_selection') != checkpoint_selection
                or descriptor_digest is not None and target['descriptor_digest'] != descriptor_digest)):
        raise ValueError('Active rule decision targets a different run/checkpoint/artifact')
    from .sdk import project_client
    client, project = project_client(payload['project_id'])
    run = client.get_pipeline_run(run_id, project=project.id)
    if (str(run.project.id) != str(project.id) or not run.model_version
            or str(run.model_version.id) != model_version_id or run.status.value != 'completed'):
        raise ValueError('Rule target model-version ownership differs')
    return target
