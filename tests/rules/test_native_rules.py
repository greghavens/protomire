"""Actual native artifacts and repeated optimization; no provider or model download."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

import pytest
from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.components.rule_gate import catalog_entries
from modelbuilder.execution import selection
from modelbuilder.models import ServiceError
from test_rules import policy

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / '.venv-backend/bin/python'
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not PYTHON.is_file(), reason='Pinned native backend required')]
SOURCE = '''from zenml import get_step_context
from modelbuilder.evaluation import digest
from modelbuilder.backend.components.rule_gate import gate

def seed() -> dict:
    ctx = get_step_context()
    subject = {'project_id': ctx.pipeline_run.config.extra['application_project_id'],
               'run_id': str(ctx.pipeline_run.id), 'config_digest': ctx.pipeline_run.config.extra['recipe_digest'],
               'data_digest': digest([[-1., -2.], [1., 2.]])}
    return {'weight': 0., 'epoch': 0, 'observations': [], 'subject': subject}

def update(previous: dict, marker: str, corrupt: bool):
    # Independent test harness: one gradient update across the same two examples
    # constitutes one epoch. All state is carried by an actual native dict input.
    weight = previous['weight'] - .2 * (2 * (previous['weight'] - 2))
    epoch = previous['epoch'] + 1
    checkpoint_state = {'schema_version': 'modelbuilder.rule-checkpoint-state/v1', 'resume_modes': ['exact_state'],
        'state': {'model': {'kind': 'value', 'value': weight}, 'data_cursor': {'kind': 'value', 'value': 0},
                  'optimizer': {'kind': 'value', 'value': {'learning_rate': .2}},
                  'scheduler': {'kind': 'no_op', 'reason': 'constant learning rate'},
                  'rng': {'kind': 'no_op', 'reason': 'deterministic workload uses no RNG'}}}
    checkpoint = digest(checkpoint_state)
    observations = previous['observations'] + [{'epoch': epoch, 'metric': 'validation_error',
        'value': (weight - 2)**2, 'checkpoint_digest': checkpoint, 'subject_digest': digest(previous['subject'])}]
    state = {'weight': weight, 'epoch': epoch, 'observations': observations, 'subject': previous['subject']}
    facts = {'status': 'completed', 'resource_units': {'epochs': 'complete_dataset_pass', 'steps': 'synthetic_work_units', 'runtime_seconds': 'seconds', 'cost': 'local-free'}, 'completed_epochs': epoch, 'steps': epoch * 10,
        'runtime_seconds': epoch, 'cost': 0, 'checkpoint_digest': checkpoint,
        'expected_checkpoint_digest': '0'*64 if corrupt else checkpoint,
        'continuation': {'mode': 'exact_state', 'supported': True}, 'observations': observations}
    evidence = {'schema_version': 'modelbuilder.rule-evidence/v1', 'subject': previous['subject'],
        'source': 'independent-gradient-harness/v1', 'verification': 'verified', 'facts': {'curve': facts}}
    with open(marker, 'a') as stream: stream.write(str(epoch) + '\\n')
    return state, evidence, checkpoint_state

def finish(state: dict) -> dict:
    return state
'''


def setup(tmp_path, *, corrupt=False, maximum=10):
    source = tmp_path / 'source'
    source.mkdir()
    path = source / 'harness.py'
    path.write_text(SOURCE)
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-qm', 'Independent synthetic rule harness'], check=True)
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    ref = {'path': 'harness.py', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    def node(name, inputs, outputs, parameters):
        return {'id': name, 'kind': 'code.python', 'implementation_ref': {**ref, 'module': 'rule_harness', 'entrypoint': name, 'commit': commit},
            'inputs': [{'name': key, 'type': value} for key, value in inputs.items()],
            'outputs': [{'name': key, 'type': value} for key, value in outputs.items()], 'parameters': parameters,
            'environment_ref': 'default', 'resource_request': {'gpu': 0}, 'retry': {'max_attempts': 1, 'backoff_seconds': 0},
            'timeout': 30, 'cache_policy': 'disabled', 'provenance': {}}
    seed = node('seed', {}, {'state': 'dict'}, {})
    update = node('update', {'previous': 'dict'}, {'state': 'dict', 'evidence': 'dict', 'checkpoint_state': 'dict'},
                  {'marker': str(tmp_path / 'epochs'), 'corrupt': corrupt})
    finish = node('finish', {'state': 'dict'}, {'final': 'dict'}, {})
    entry = catalog_entries()[0]
    descriptor = {key: value for key, value in entry.items() if key not in {'component_digest', 'inputs', 'outputs', 'adapter'}}
    guard = node('guard', {'evidence': 'dict', 'checkpoint_state': 'dict'}, {'allowed': 'bool', 'done': 'bool', 'result': 'dict'},
                 {'policy': policy(criterion='target', threshold=.01, max_epochs=maximum, min_epochs=1)})
    guard.update(kind='component.run', component=descriptor)
    guard['implementation_ref']['entrypoint'] = 'gate'
    nodes = [seed, update, guard, finish]
    project = str(uuid.uuid4())
    recipe = {'schema_version': '0', 'recipe_id': str(uuid.uuid4()), 'project_id': project, 'revision': 1,
        'name': 'Native deterministic rules', 'source': {'repository': str(source), 'commit': commit, 'files': [ref]},
        'nodes': nodes, 'edges': [
            {'source_node': 'seed', 'source_port': 'state', 'target_node': 'update', 'target_port': 'previous'},
            {'source_node': 'update', 'source_port': 'evidence', 'target_node': 'guard', 'target_port': 'evidence'},
            {'source_node': 'update', 'source_port': 'checkpoint_state', 'target_node': 'guard', 'target_port': 'checkpoint_state'},
            {'source_node': 'update', 'source_port': 'state', 'target_node': 'finish', 'target_port': 'state'}],
        'bounded_iterations': [{'id': 'epochs', 'node_ids': ['update', 'guard'], 'max_iterations': 10,
            'state': [{'source_node': 'update', 'source_port': 'state', 'target_node': 'update', 'target_port': 'previous'}],
            'until': {'source_node': 'guard', 'source_port': 'done', 'equals': True}}],
        'environment': {'python': '3.12', 'requirements': ['zenml==0.97.0']},
        'evaluation': {}, 'resources': [], 'external_dependencies': [], 'outputs': []}
    backend = ZenMLBackend(tmp_path / 'native', python_executable=str(PYTHON), source_root=source, trusted_code_roots=[str(source)])
    return backend, {'project_id': project, 'recipe': recipe, 'trusted': True, 'max_runtime_seconds': 180,
                     'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4())}


def execute(backend, request):
    plan = backend.invoke('run.plan', request)
    assert plan['executable'], plan
    backend.invoke('run.submit', request)
    deadline = time.monotonic() + 200
    while time.monotonic() < deadline:
        result = backend.invoke('run.status', request)
        if result['status'] in {'succeeded', 'failed', 'cancelled'} and not result['worker_alive']:
            (backend.root.parent / 'native-rule-receipt.json').write_text(json.dumps(result, indent=2))
            return result
        time.sleep(.2)
    raise AssertionError(result)


@pytest.mark.parametrize('maximum,count,reason', [(10, 6, 'criterion_met'), (1, 1, 'resource_bound:max_epochs')])
def test_actual_native_epochs_and_decision_artifacts(tmp_path, maximum, count, reason):
    backend, request = setup(tmp_path, maximum=maximum)
    result = execute(backend, request)
    assert result['status'] == 'succeeded', result
    assert (tmp_path / 'epochs').read_text().splitlines() == [str(i) for i in range(1, count+1)]
    assert len(result['bounded_iterations']['epochs']['iterations']) == count
    retried = backend.invoke('run.submit', request)
    assert retried['backend_run_id'] == result['backend_run_id']
    assert (tmp_path / 'epochs').read_text().splitlines() == [str(i) for i in range(1, count+1)]
    # Load actual ZenML artifacts, not fabricated backend output dictionaries.
    script = '''import json,sys
from zenml.client import Client
run=Client().get_pipeline_run(sys.argv[1])
values=[a.load() for s in run.steps.values() for a in s.outputs.get('result',[]) if s.config.extra.get('canonical_node_id')=='guard']
print(json.dumps(values))
'''
    reader = tmp_path / 'read_native_artifacts.py'
    reader.write_text(script)
    response = subprocess.run([str(PYTHON), str(reader), result['backend_run_id']],
        env=backend._env(), capture_output=True, text=True, check=True)
    values = json.loads(response.stdout.strip().splitlines()[-1])
    assert len(values) == count
    stopped = [x for x in values if x['outcome'] == 'stop']
    assert len(stopped) == 1 and stopped[0]['reasons'] == [reason]
    for value in values:
        native = value['native_execution']
        assert native['producer']['backend_run_id'] == result['backend_run_id']
        assert native['inputs'][0]['artifact_id']
        assert value['digest'] == hashlib.sha256(json.dumps({k:v for k,v in value.items() if k!='digest'}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def test_actual_native_missing_checkpoint_blocks_next_epoch(tmp_path):
    backend, request = setup(tmp_path, corrupt=True)
    result = execute(backend, request)
    assert result['status'] == 'failed', result
    assert (tmp_path / 'epochs').read_text().splitlines() == ['1']


def test_selected_subgraph_cannot_omit_required_guard(tmp_path):
    _, request = setup(tmp_path)
    recipe = copy.deepcopy(request['recipe'])
    recipe.pop('bounded_iterations')
    action = next(x for x in recipe['nodes'] if x['id'] == 'finish')
    action['required_guards'] = ['guard']
    action['condition'] = {'source_node': 'guard', 'source_port': 'allowed', 'equals': True}
    with pytest.raises(ServiceError) as error:
        selection(recipe, {'node_ids': ['finish'], 'input_bindings': []})
    assert error.value.code == 'required_guard_omitted'
