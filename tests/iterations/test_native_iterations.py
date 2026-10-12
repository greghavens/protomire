"""Real ZenML invocation identities, typed feedback, terminal states and checkpoints."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

import pytest
from modelbuilder.backend import ZenMLBackend
from modelbuilder.iterations import invocation_identity

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / '.venv-backend/bin/python'
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not PYTHON.is_file(), reason='Pinned backend required')]
SOURCE = '''from pathlib import Path
import time

def seed() -> int:
    return 0

def increment(value: int, stop: int, fail: int, delay: float, marker: str):
    value += 1
    if marker:
        with Path(marker).open('a') as stream: stream.write(str(value) + '\\n')
    if value == fail: raise ValueError('deliberate iteration failure')
    time.sleep(delay)
    return value, value >= stop

def final(value: int) -> int:
    return value
'''


def setup(tmp_path, stop=99, fail=-1, delay=0, timeout=30):
    source = tmp_path / 'source'
    source.mkdir()
    path = source / 'harness.py'
    path.write_text(SOURCE)
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'Pinned iteration fixture'], check=True)
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    reference = {'path': 'harness.py', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    def node(name, inputs, outputs, parameters):
        return {'id': name, 'kind': 'code.python', 'implementation_ref': {**reference, 'module': 'iteration_harness', 'entrypoint': name, 'commit': commit},
            'inputs': [{'name': key, 'type': value} for key, value in inputs.items()],
            'outputs': [{'name': key, 'type': value} for key, value in outputs.items()], 'parameters': parameters,
            'environment_ref': 'default', 'resource_request': {'gpu': 0}, 'retry': {'max_attempts': 1, 'backoff_seconds': 0},
            'timeout': timeout if name == 'increment' else 30, 'cache_policy': 'disabled', 'provenance': {}}
    nodes = [node('seed', {}, {'value': 'int'}, {}), node('increment', {'value': 'int'}, {'value': 'int', 'done': 'bool'},
        {'stop': stop, 'fail': fail, 'delay': delay, 'marker': str(tmp_path / 'called')}), node('final', {'value': 'int'}, {'result': 'int'}, {})]
    project = str(uuid.uuid4())
    recipe = {'schema_version': '0', 'recipe_id': str(uuid.uuid4()), 'project_id': project, 'name': 'Native bounded feedback', 'revision': 1,
        'source': {'repository': str(source), 'commit': commit, 'files': [reference]}, 'nodes': nodes,
        'edges': [{'source_node': source, 'source_port': 'value', 'target_node': target, 'target_port': 'value'} for source, target in [('seed', 'increment'), ('increment', 'final')]],
        'bounded_iterations': [{'id': 'refine', 'node_ids': ['increment'], 'max_iterations': 3,
            'state': [{'source_node': 'increment', 'source_port': 'value', 'target_node': 'increment', 'target_port': 'value'}],
            'until': {'source_node': 'increment', 'source_port': 'done', 'equals': True}}],
        'environment': {'python': '3.12', 'requirements': ['zenml==0.97.0']}, 'evaluation': {}, 'outputs': [], 'resources': [], 'external_dependencies': []}
    backend = ZenMLBackend(tmp_path / 'state', python_executable=str(PYTHON), source_root=source, trusted_code_roots=[str(source)])
    return backend, {'project_id': project, 'recipe': recipe, 'trusted': True, 'max_runtime_seconds': 120}


def execute(backend, payload):
    request = {**copy.deepcopy(payload), 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4())}
    plan = backend.invoke('run.plan', request)
    assert plan['executable'], plan
    backend.invoke('run.submit', request)
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        result = backend.invoke('run.status', request)
        if result['status'] in {'succeeded', 'failed', 'cancelled'} and not result['worker_alive']:
            (backend.root.parent / 'acceptance.json').write_text(json.dumps(result, indent=2))
            return result, request
        time.sleep(.2)
    raise AssertionError(result)


def native_inputs(backend, result):
    script = '''import json,sys
from zenml.client import Client
run=Client().get_pipeline_run(sys.argv[1])
print(json.dumps({name:{'id':str(step.id),'extra':step.config.extra,'inputs':{port:[str(a.id) for a in values] for port,values in step.inputs.items()},'outputs':{port:[str(a.id) for a in values] for port,values in step.outputs.items()}} for name,step in run.steps.items()}))
'''
    response = subprocess.run([str(PYTHON), '-c', script, result['backend_run_id']], env=backend._env(), capture_output=True, text=True, check=True)
    return json.loads(response.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize('stop,count,reason', [(99, 3, 'bound'), (2, 2, 'until')])
def test_native_bound_early_exit_exact_state_lineage_and_reopen(tmp_path, stop, count, reason):
    backend, payload = setup(tmp_path, stop=stop)
    result, request = execute(backend, payload)
    assert result['status'] == 'succeeded', result
    assert (tmp_path / 'called').read_text().splitlines() == [str(index) for index in range(1, count + 1)]
    repeated = [artifact for artifact in result['artifacts'] if artifact['node_id'] == 'increment' and artifact['port'] == 'value']
    assert len(repeated) == count and len({artifact['artifact_id'] for artifact in repeated}) == count
    assert all(artifact['canonical_node_id'] == 'increment' for artifact in repeated)
    rows = result['bounded_iterations']['refine']['iterations']
    assert len(rows) == count and rows[-1]['stop_reason'] == reason
    native = native_inputs(backend, result)
    previous = native['seed']['outputs']['value'][0]
    for index in range(1, count + 1):
        info = invocation_identity('increment', payload['recipe']['bounded_iterations'][0], index)
        step = native[info['invocation_id']]
        assert all(step['extra'][key] == value for key, value in info.items())
        assert step['inputs']['value'] == [previous]
        previous = step['outputs']['value'][0]
    assert native['final']['inputs']['value'] == [previous]
    artifacts = backend.invoke('run.artifacts', {**request, 'include_binding_descriptors': True})['artifacts']
    assert all(artifact['binding_descriptor']['artifact_id'] == artifact['artifact_id'] for artifact in artifacts)
    restored = ZenMLBackend(tmp_path / 'state', python_executable=str(PYTHON), source_root=backend.repository, trusted_code_roots=backend.trusted_code_roots)
    fresh = restored.invoke('run.status', request)
    for key in ('backend_run_id', 'artifacts', 'bounded_iterations', 'invocation_statuses'):
        assert fresh[key] == result[key]


def test_failed_iteration_preserves_completed_native_artifacts_without_downstream(tmp_path):
    backend, payload = setup(tmp_path, fail=2)
    result, request = execute(backend, payload)
    assert result['status'] == 'failed', result
    assert len([artifact for artifact in result['artifacts'] if artifact['node_id'] == 'increment' and artifact['port'] == 'value']) == 1
    assert result['node_statuses']['increment'] == 'failed'
    assert 'final' not in result['node_statuses'] and not result['skipped_node_ids']
    assert result['bounded_iterations']['refine']['iterations'][-1]['status'] == 'failed'


def test_iteration_timeout_uses_canonical_node_and_unique_invocation(tmp_path):
    backend, payload = setup(tmp_path, delay=4, timeout=.1)
    result, request = execute(backend, payload)
    assert result['status'] == 'cancelled' and result['worker_alive'] is False, result
    cancellation = json.loads((backend.root / 'jobs' / request['operation_id'] / 'cancel.json').read_text())
    assert cancellation['reason'] == 'node_timeout_exceeded' and cancellation['node_id'] == 'increment'
    assert cancellation['invocation_id'] == invocation_identity('increment', payload['recipe']['bounded_iterations'][0], 1)['invocation_id']
    assert not any(artifact['node_id'] == 'increment' for artifact in result['artifacts'])


def training_setup(tmp_path, *, delay=0):
    backend, payload = setup(tmp_path)
    source = tmp_path / 'source'
    harness = (ROOT / 'tests/components/independent_harness.py').read_text()
    # Keep production training delegated to the pinned user framework harness.
    # Only expose invocation/resume observations for this acceptance test.
    harness = harness.replace('    config = parameters["architecture"]\n    model, xs, ys = setup(config)',
        '    config = parameters["architecture"]\n    if context.get("job_dir"):\n        with (Path(context["job_dir"]) / "training-calls.jsonl").open("a") as stream:\n            stream.write(json.dumps({"invocation": context.get("invocation"), "resume_mode": context.get("resume_mode"), "resume": context.get("resume")}) + "\\n")\n    model, xs, ys = setup(config)')
    harness = harness.replace('        time.sleep(parameters.get("step_delay", 0))',
        '        if parameters.get("hold_second") and context.get("invocation", {}).get("iteration") == 2 and i == 0:\n            time.sleep(60)\n        time.sleep(parameters.get("step_delay", 0))')
    (source / 'harness.py').write_text(harness)
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'Independent PyTorch training loop fixture'], check=True)
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    reference = {'path': 'harness.py', 'sha256': hashlib.sha256((source / 'harness.py').read_bytes()).hexdigest()}
    body = payload['recipe']
    body['source'].update(commit=commit, files=[reference])
    nodes = []
    for name, role, inputs, outputs, parameters in [
        ('train', 'training', [{'name': 'previous', 'type': 'Path', 'required': False}], [{'name': 'trained', 'type': 'Path'}],
         {'architecture': {'task': 'image', 'width': 2, 'loss_weight': .6}, 'learning_rate': .01, 'steps': 2, 'step_delay': delay}),
        ('evaluate', 'evaluation', [{'name': 'trained', 'type': 'Path'}], [{'name': 'metrics', 'type': 'dict'}], {}),
    ]:
        node = copy.deepcopy(body['nodes'][0])
        node.update(id=name, kind='component.run', inputs=inputs, outputs=outputs, parameters=parameters,
            implementation_ref={**reference, 'module': 'iteration_training', 'entrypoint': name, 'commit': commit},
            component={'id': 'test.' + name, 'version': '1', 'protocol': 'modelbuilder.component/v1', 'role': role,
                'parameter_schema': {'type': 'object'}, 'capabilities': {'devices': ['cpu'], 'resume_modes': ['exact_state', 'warm_start'] if name == 'train' else [], 'frameworks': ['pytorch'], 'export_formats': []}})
        nodes.append(node)
    body['nodes'] = nodes
    body['edges'] = [{'source_node': 'train', 'source_port': 'trained', 'target_node': 'evaluate', 'target_port': 'trained'}]
    body['bounded_iterations'] = [{'id': 'improve', 'node_ids': ['train', 'evaluate'], 'max_iterations': 2,
        'state': [{'source_node': 'train', 'source_port': 'trained', 'target_node': 'train', 'target_port': 'previous', 'resume_mode': 'warm_start'}]}]
    return backend, payload


def test_native_training_evaluation_feedback_exact_checkpoint_selectors_and_warm_start(tmp_path):
    from modelbuilder.backend.admission import BackendError
    backend, payload = training_setup(tmp_path)
    result, request = execute(backend, payload)
    assert result['status'] == 'succeeded', result
    checkpoints = sorted([artifact for artifact in result['artifacts'] if artifact['node_id'] == 'train'], key=lambda artifact: artifact['iteration'])
    assert len(checkpoints) == 2 and len(result['evaluations']) == 2
    assert {row['node_id'] for row in result['evaluations']} == {'evaluate'}
    assert {row['iteration'] for row in result['evaluations']} == {1, 2}
    assert 'loss' not in result['metrics'], 'Repeated measurements must never flatten to a guessed last metric'
    calls = [json.loads(line) for line in (backend.root / 'jobs' / request['operation_id'] / 'training-calls.jsonl').read_text().splitlines()]
    assert calls[0]['resume_mode'] is None and calls[1]['resume_mode'] == 'warm_start'
    assert calls[1]['resume']['artifact_id'] == checkpoints[0]['artifact_id']
    native = native_inputs(backend, result)
    assert 'previous' not in native[checkpoints[0]['invocation_id']]['inputs']
    assert native[checkpoints[1]['invocation_id']]['inputs']['previous'] == [checkpoints[0]['artifact_id']]
    selected = backend.invoke('model.checkpoint.get', {**request, 'checkpoint_ref': checkpoints[0]['artifact_id'], 'checkpoint_kind': 'last'})
    assert selected['artifact_id'] == checkpoints[0]['artifact_id'] and selected['iteration'] == 1
    with pytest.raises(BackendError) as error:
        backend.invoke('model.checkpoint.get', request)
    assert error.value.code == 'ambiguous_checkpoint'
    script = '''import json,sys
from pathlib import Path
from modelbuilder.backend.sdk import status
result=status(Path(sys.argv[1]),verify_evaluations=True)
print(json.dumps(result['evaluations']))
'''
    inspection = tmp_path / 'inspect_evaluations.py'
    inspection.write_text(script)
    response = subprocess.run([str(PYTHON), str(inspection), str(backend.root / 'jobs' / request['operation_id'])], env=backend._env(), capture_output=True, text=True, check=True)
    evaluations = json.loads(response.stdout.strip().splitlines()[-1])
    assert all(row['checkpoint_verification']['verified'] for row in evaluations), evaluations
    for row in evaluations:
        assert row['checkpoint_verification']['inputs'][0]['artifact_id'] == checkpoints[row['iteration'] - 1]['artifact_id']
    continuation = {**payload, 'parent_operation_id': request['operation_id'], 'checkpoint_ref': checkpoints[0]['artifact_id'],
                    'mode': 'exact_state', 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4())}
    with pytest.raises(BackendError) as error:
        backend.invoke('run.resume', continuation)
    assert error.value.code == 'unsupported_iteration_resume'
    continuation.update(mode='warm_start', operation_id=str(uuid.uuid4()), idempotency_key=str(uuid.uuid4()))
    launched = backend.invoke('run.resume', continuation)
    resumed_request = {**continuation, 'operation_id': launched['operation_id']}
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        resumed = backend.invoke('run.status', resumed_request)
        if resumed['status'] in {'succeeded', 'failed', 'cancelled'} and resumed['worker_alive'] is False:
            break
        time.sleep(.2)
    assert resumed['status'] == 'succeeded', resumed
    assert resumed['parent_checkpoint_ref'] == checkpoints[0]['artifact_id'] and resumed['resume_mode'] == 'warm_start'
    assert resumed['parent_invocation']['invocation_id'] == checkpoints[0]['invocation_id']
    resumed_calls = [json.loads(line) for line in (backend.root / 'jobs' / launched['operation_id'] / 'training-calls.jsonl').read_text().splitlines()]
    assert len(resumed_calls) == 2 and all(call['resume_mode'] == 'warm_start' for call in resumed_calls)
    assert resumed_calls[0]['invocation']['iteration'] == 1


def test_cancelled_iteration_recovers_its_exact_partial_training_checkpoint(tmp_path):
    backend, payload = training_setup(tmp_path)
    payload["recipe"]["nodes"][0]["parameters"]["hold_second"] = True
    request = {**payload, 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4())}
    backend.invoke('run.submit', request)
    block = payload['recipe']['bounded_iterations'][0]
    interrupted = invocation_identity('train', block, 2)
    job = backend.root / 'jobs' / request['operation_id']
    try:
        deadline = time.monotonic() + 100
        manifest = job / 'outputs' / interrupted['invocation_id'] / 'checkpoints' / '00000001' / 'checkpoint.manifest.json'
        while time.monotonic() < deadline and not manifest.is_file():
            time.sleep(.1)
        assert manifest.is_file()
        result = backend.invoke('run.cancel', request)
        assert result['status'] == 'cancelled' and result['worker_alive'] is False, result
        recovery = [artifact for artifact in result['artifacts'] if artifact['port'] == 'recovery_checkpoint']
        assert len(recovery) == 1 and recovery[0]['invocation_id'] == interrupted['invocation_id'] and recovery[0]['iteration'] == 2
        selected = backend.invoke('model.checkpoint.get', {**request, 'checkpoint_ref': recovery[0]['artifact_id']})
        assert selected['descriptor']['step'] == 1 and selected['artifact_id'] == recovery[0]['artifact_id']
        assert len(result['evaluations']) == 1 and result['evaluations'][0]['iteration'] == 1
        assert len([artifact for artifact in result['artifacts'] if artifact['node_id'] == 'train' and artifact['port'] == 'trained']) == 1
        restored = ZenMLBackend(tmp_path / 'state', python_executable=str(PYTHON), source_root=backend.repository, trusted_code_roots=backend.trusted_code_roots)
        assert restored.invoke('run.status', request)['artifacts'] == result['artifacts']
    finally:
        backend.invoke('run.cancel', request)


def test_final_iteration_skip_never_leaks_prior_body_output(tmp_path):
    backend, payload = setup(tmp_path, stop=2)
    body = payload['recipe']
    copy_node = copy.deepcopy(body['nodes'][2])
    copy_node.update(id='copy', outputs=[{'name': 'value', 'type': 'int'}],
                     condition={'source_node': 'increment', 'source_port': 'done', 'equals': False})
    body['nodes'].append(copy_node)
    body['edges'] = [body['edges'][0],
        {'source_node': 'increment', 'source_port': 'value', 'target_node': 'copy', 'target_port': 'value'},
        {'source_node': 'copy', 'source_port': 'value', 'target_node': 'final', 'target_port': 'value'}]
    body['bounded_iterations'][0]['node_ids'].append('copy')
    result, request = execute(backend, payload)
    assert result['status'] == 'succeeded', result
    assert set(result['skipped_node_ids']) == {'copy', 'final'}
    assert result['node_statuses']['copy'] == 'skipped' and result['node_statuses']['final'] == 'skipped'
    history = [row for row in result['invocation_statuses'].values() if row['canonical_node_id'] == 'copy']
    assert [row['status'] for row in sorted(history, key=lambda row: row['iteration'])] == ['completed', 'skipped']
    assert len([artifact for artifact in result['artifacts'] if artifact['node_id'] == 'copy']) == 1
    assert not any(artifact['node_id'] == 'final' for artifact in result['artifacts'])


def test_selected_cut_can_bind_an_exact_earlier_iteration_artifact(tmp_path):
    backend, payload = setup(tmp_path)
    result, request = execute(backend, payload)
    assert result['status'] == 'succeeded', result
    artifacts = backend.invoke('run.artifacts', {**request, 'include_binding_descriptors': True})['artifacts']
    original = next(artifact for artifact in artifacts if artifact['node_id'] == 'increment' and artifact['port'] == 'value' and artifact['iteration'] == 1)
    descriptor = original['binding_descriptor']
    binding = {key: descriptor[key] for key in ('backend_run_id', 'source_node_id', 'source_port', 'artifact_id', 'content_sha256')}
    selected = {**payload, 'execution': {'node_ids': ['final'], 'input_bindings': [{**binding, 'node_id': 'final', 'input_port': 'value'}]}}
    cut, cut_request = execute(backend, selected)
    assert cut['status'] == 'succeeded' and cut['executed_node_ids'] == ['final'], cut
    native = native_inputs(backend, cut)
    assert native['final']['inputs']['value'] == [original['artifact_id']]
    assert cut['execution']['input_bindings'][0]['iteration'] == 1


async def test_official_mcp_iteration_matches_http_editor_schema_and_canonical_recipe(tmp_path):
    import asyncio
    import httpx
    from mcp.shared.memory import create_connected_server_and_client_session
    from modelbuilder.api import create_app
    from modelbuilder.auth import TokenAuth
    from modelbuilder.mcp_server import OperationGateway, create_server
    from modelbuilder.models import Actor
    from modelbuilder.service import OperationService
    from modelbuilder.recipe import RECIPE_SCHEMA
    backend, payload = setup(tmp_path, stop=2)
    payload['recipe']['source']['repository'] = 'https://example.org/iteration-source.git'
    payload['recipe']['nodes'][1]['parameters']['marker'] = ''
    owner = Actor('iteration-owner', {'read', 'modify', 'execute'})
    service = OperationService(tmp_path / 'service.sqlite', backend=backend, source_root=backend.repository)
    app = create_app(service, TokenAuth({'iteration-test-token': owner}))
    submitted = None
    try:
        async with httpx.AsyncClient(base_url='http://test', headers={'Authorization': 'Bearer iteration-test-token'},
                                     transport=httpx.ASGITransport(app=app)) as client:
            gateway = OperationGateway(client)
            catalog = await gateway.catalog()
            async with create_connected_server_and_client_session(create_server(gateway, catalog)) as session:
                async def call(name, **request):
                    response = await session.call_tool(name, request)
                    assert not response.isError, response.structuredContent
                    return response.structuredContent
                tools = await session.list_tools()
                by_name = {tool.name: tool for tool in tools.tools}
                for spec in catalog['operations']:
                    assert by_name[spec['name'].replace('.', '_')].inputSchema == spec['input_schema']
                assert json.loads((ROOT / 'packages/ide_feature/schemas/recipe.v0.schema.json').read_text()) == RECIPE_SCHEMA
                project = await call('project_create', slug='native-iterations', model_name='One iterative model', recipe=payload['recipe'], idempotency_key='project')
                reference = {'project_id': project['project_id'], 'recipe_id': project['recipe_id']}
                recipe = await call('recipe_get', **reference)
                assert recipe['bounded_iterations'] == payload['recipe']['bounded_iterations']
                run_payload = {**reference, 'expected_revision': recipe['revision'], 'trusted': True, 'idempotency_key': 'iteration-run'}
                plan = await call('run_plan', **{key: value for key, value in run_payload.items() if key != 'idempotency_key'})
                assert plan['executable'], plan
                submitted = await call('run_submit', **run_payload)
                assert await call('run_submit', **run_payload) == submitted
                run_ref = {'project_id': project['project_id'], 'operation_id': submitted['operation_id']}
                deadline = time.monotonic() + 150
                while time.monotonic() < deadline:
                    result = await call('run_status', **run_ref)
                    if result['status'] in {'succeeded', 'failed', 'cancelled'} and result['worker_alive'] is False:
                        break
                    await asyncio.sleep(.2)
                assert result['status'] == 'succeeded', result
                response = await client.post('/v1/operations/run.status', json=run_ref)
                assert response.status_code == 200
                http_result = response.json()
                for key in ('backend_run_id', 'bounded_iterations', 'invocation_statuses', 'artifacts', 'node_statuses'):
                    assert http_result[key] == result[key]
                assert await call('recipe_get', **reference) == recipe
                assert result['recipe_digest'] == recipe['digest'] and result['recipe_id'] == project['recipe_id']
                assert len(result['bounded_iterations']['refine']['iterations']) == 2
                (tmp_path / 'mcp-acceptance.json').write_text(json.dumps(result, indent=2))
    finally:
        if submitted:
            backend.invoke('run.cancel', {'project_id': project['project_id'], 'operation_id': submitted['operation_id']})
        service.store.close()
