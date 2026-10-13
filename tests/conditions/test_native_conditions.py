"""Actual native terminal conditions; no source import/call for false branches."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

import pytest
from modelbuilder.backend import ZenMLBackend

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv-backend/bin/python"
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not PYTHON.is_file(), reason="Pinned backend required")]
SOURCE = '''from pathlib import Path
import time

def gate(value: bool) -> bool:
    return value

def target(marker: str, delay: float) -> dict:
    with Path(marker).open("a") as f: f.write("called\\n")
    time.sleep(delay)
    return {"measurement": 3.0}
'''


def setup(tmp_path, value=True, delay=0, timeout=30, extra_source=""):
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.py").write_text(SOURCE + extra_source)
    # Separate module makes skipped source import observable, not only invocation.
    (source / "never.py").write_text('raise RuntimeError("false branch source must never be imported")\ndef never() -> dict:\n    return {}\n')
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Pinned conditional harness"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    files = [{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in source.glob("*.py")]
    nodes=[]
    for name in ("gate", "target", "never"):
        reference=next(item for item in files if item["path"] == ("never.py" if name == "never" else "source.py"))
        nodes.append({"id": name,"kind":"code.python","implementation_ref":{**reference,"module":"never" if name == "never" else "condition_harness","entrypoint":name,"commit":commit},
            "inputs":[],"outputs":[{"name":"allowed" if name == "gate" else "metrics","type":"bool" if name == "gate" else "dict"}],
            "parameters":{"value":value} if name == "gate" else ({"marker":str(tmp_path / "called"),"delay":delay} if name == "target" else {}),
            "environment_ref":"default","resource_request":{"gpu":0},"retry":{"max_attempts":1,"backoff_seconds":0},
            "timeout":timeout if name == "target" else 30,"cache_policy":"enabled" if name == "gate" else "disabled","provenance":{}})
    nodes[1]["condition"]={"source_node":"gate","source_port":"allowed","equals":True}
    nodes[2]["condition"]={"source_node":"gate","source_port":"allowed","equals":False if value else True}
    project=str(uuid.uuid4())
    recipe={"schema_version":"0","recipe_id":str(uuid.uuid4()),"revision":1,"project_id":project,"name":"Native conditional terminal stages",
        "source":{"repository":str(source),"commit":commit,"files":files},"nodes":nodes,"edges":[],"resources":[],"external_dependencies":[],
        "environment":{"python":"3.12","requirements":["zenml==0.97.0"]},"evaluation":{},"outputs":[]}
    backend=ZenMLBackend(tmp_path / "state",python_executable=str(PYTHON),source_root=source,trusted_code_roots=[str(source)])
    return backend,{"project_id":project,"recipe":recipe,"trusted":True,"max_runtime_seconds":120}


def execute(backend,payload):
    request={**copy.deepcopy(payload),"operation_id":str(uuid.uuid4()),"idempotency_key":str(uuid.uuid4())}
    plan=backend.invoke("run.plan",request)
    assert plan["executable"],plan
    backend.invoke("run.submit",request)
    deadline=time.monotonic()+150
    while time.monotonic()<deadline:
        result=backend.invoke("run.status",request)
        if result["status"] in {"succeeded","failed","cancelled"} and not result["worker_alive"]:
            return result,request
        time.sleep(.2)
    raise AssertionError(result)


@pytest.mark.parametrize("allowed",[True,False])
def test_actual_native_branch_cache_and_no_false_import(tmp_path,allowed):
    backend,payload=setup(tmp_path,allowed)
    original_gate = None
    original_run = None
    for repeat in range(2):
        result,request=execute(backend,payload)
        assert result["status"] == "succeeded",result
        assert set(result["executed_node_ids"]) == ({"gate","target"} if allowed else {"gate"})
        assert set(result["skipped_node_ids"]) == ({"never"} if allowed else {"target","never"})
        assert result["conditional_execution"]["target"]["gate_value"] is allowed
        assert result["node_statuses"]["target"] == ("completed" if allowed else "skipped")
        assert result["cached_node_ids"] == (["gate"] if repeat else [])
        gate = next(artifact for artifact in result["artifacts"] if artifact["node_id"] == "gate")
        assert result["conditional_execution"]["target"]["gate_artifact_id"] == gate["artifact_id"]
        if repeat:
            assert gate["artifact_id"] == original_gate
            assert gate["cache"]["reused"] is True
            assert gate["cache"]["producer_backend_run_id"] == original_run
        else:
            original_gate, original_run = gate["artifact_id"], result["backend_run_id"]
        assert not any(a["node_id"] in result["skipped_node_ids"] for a in result["artifacts"])
        log=(backend.root / "jobs" / request["operation_id"] / "worker.log").read_text()
        assert "false branch source must never be imported" not in log
    if allowed:
        assert (tmp_path / "called").read_text().splitlines() == ["called","called"]
    else:
        assert not (tmp_path / "called").exists()
        assert result["evaluations"] == []
    # Exact subgraphs do not quietly add the gate or any ancestors.
    plan=backend.invoke("run.plan",{**payload,"execution":{"node_ids":["target"],"input_bindings":[]}})
    assert not plan["executable"] and plan["blocked_reasons"][0]["code"] == "unselected_condition_gate"
    (tmp_path / "acceptance.json").write_text(json.dumps(result,indent=2))


def test_native_conditional_stage_deadline_terminates_worker(tmp_path):
    backend,payload=setup(tmp_path,True,delay=5,timeout=.15)
    result,request=execute(backend,payload)
    assert result["status"] == "cancelled" and not result["worker_alive"],result
    assert result["conditional_execution"]["target"]["status"] == "eligible"
    assert "target" not in result["skipped_node_ids"]
    cancel=json.loads((backend.root / "jobs" / request["operation_id"] / "cancel.json").read_text())
    assert cancel["reason"] == "node_timeout_exceeded" and cancel["node_id"] == "target"


def test_explicit_cancel_stops_eligible_conditional_stage(tmp_path):
    backend, payload = setup(tmp_path, True, delay=30, timeout=60)
    request = {**payload, "operation_id": str(uuid.uuid4()), "idempotency_key": str(uuid.uuid4())}
    backend.invoke("run.submit", request)
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not (tmp_path / "called").exists():
            time.sleep(.1)
        assert (tmp_path / "called").exists(), "Conditional target never started"
        result = backend.invoke("run.cancel", request)
        assert result["status"] == "cancelled" and result["worker_alive"] is False, result
        assert result["conditional_execution"]["target"]["status"] == "eligible"
        assert "target" not in result["skipped_node_ids"]
        assert not any(item["node_id"] == "target" for item in result["artifacts"])
        (tmp_path / "acceptance.json").write_text(json.dumps(result, indent=2))
    finally:
        backend.invoke("run.cancel", request)


BRANCH_SOURCE = '''
def branch_gate() -> bool:
    return True

def alternate() -> dict:
    return {"measurement": 7.0}

def required(value: dict) -> dict:
    return value

def join(left=None, right=None, fail=False, wrong_type=False, allow_empty=False) -> dict:
    if fail:
        raise ValueError("deliberate native join failure")
    if wrong_type:
        return 5
    if allow_empty and left is None and right is None:
        return {"measurement": 0.0, "present": "none"}
    assert (left is None) != (right is None)
    return {"measurement": (left or right)["measurement"], "present": "left" if left is not None else "right"}

def component_join(inputs, parameters, context):
    assert set(inputs) in ({"left"}, {"right"})
    return {"joined": join(**inputs)}
'''


def branch_setup(tmp_path, allowed, *, component=False, fail=False, wrong_type=False):
    backend, payload = setup(tmp_path, allowed, extra_source=BRANCH_SOURCE)
    recipe = payload['recipe']
    gate, primary, never = recipe['nodes']
    gate['cache_policy'] = 'disabled'
    branch = copy.deepcopy(gate)
    branch.update(id='branch_gate', parameters={}, cache_policy='disabled',
                  condition={'source_node': 'gate', 'source_port': 'allowed', 'equals': True})
    branch['implementation_ref']['entrypoint'] = 'branch_gate'
    primary['condition'] = {'source_node': 'branch_gate', 'source_port': 'allowed', 'equals': True}
    # This module must remain unimported when the nested control source is skipped.
    never['condition'] = {'source_node': 'branch_gate', 'source_port': 'allowed', 'equals': False}
    alternate = copy.deepcopy(primary)
    alternate.update(id='alternate', parameters={}, condition={'source_node': 'gate', 'source_port': 'allowed', 'equals': False})
    alternate['implementation_ref']['entrypoint'] = 'alternate'
    required = copy.deepcopy(primary)
    required.update(id='required', parameters={}, inputs=[{'name': 'value', 'type': 'dict'}])
    required.pop('condition')
    required['implementation_ref']['entrypoint'] = 'required'
    join = copy.deepcopy(required)
    join.update(id='join', inputs=[{'name': name, 'type': 'dict', 'required': False} for name in ('left', 'right')],
                parameters={'fail': fail, 'wrong_type': wrong_type}, outputs=[{'name': 'joined', 'type': 'dict'}])
    join['implementation_ref']['entrypoint'] = 'join'
    if component:
        join.update(kind='component.run', parameters={}, component={
            'id': 'test.optional_join', 'version': '1', 'protocol': 'modelbuilder.component/v1', 'role': 'utility',
            'parameter_schema': {'type': 'object', 'additionalProperties': False, 'properties': {}},
            'capabilities': {'devices': ['cpu'], 'resume_modes': [], 'export_formats': [], 'frameworks': ['python']}})
        join['implementation_ref']['entrypoint'] = 'component_join'
    final = copy.deepcopy(required)
    final.update(id='final')
    recipe['nodes'] = [gate, branch, primary, never, alternate, required, join, final]
    recipe['edges'] = [
        {'source_node': source, 'source_port': port, 'target_node': target, 'target_port': target_port}
        for source, port, target, target_port in [
            ('target', 'metrics', 'required', 'value'), ('required', 'metrics', 'join', 'left'),
            ('alternate', 'metrics', 'join', 'right'), ('join', 'joined', 'final', 'value')]]
    return backend, payload


@pytest.mark.parametrize('allowed,component', [(True, False), (False, False), (False, True)])
def test_native_nested_branches_required_skip_optional_join_and_recovery(tmp_path, allowed, component):
    backend, payload = branch_setup(tmp_path, allowed, component=component)
    result, request = execute(backend, payload)
    assert result['status'] == 'succeeded', result
    skipped = {'never', 'alternate'} if allowed else {'branch_gate', 'target', 'never', 'required'}
    assert set(result['skipped_node_ids']) == skipped
    assert set(result['executed_node_ids']) == {node['id'] for node in payload['recipe']['nodes']} - skipped
    joined = next(artifact for artifact in result['artifacts'] if artifact['node_id'] == 'join')
    assert json.loads((Path(joined['uri']) / 'data.json').read_text()) == {
        'measurement': 3.0 if allowed else 7.0, 'present': 'left' if allowed else 'right'}
    omitted = 'right' if allowed else 'left'
    kept = 'left' if allowed else 'right'
    inputs = result['conditional_inputs']['join']
    assert [edge['target_port'] for edge in inputs['omitted_inputs']] == [omitted]
    assert set(inputs['input_artifact_ids']) == {kept}
    source = 'required' if allowed else 'alternate'
    assert inputs['input_artifact_ids'][kept] == next(artifact['artifact_id'] for artifact in result['artifacts'] if artifact['node_id'] == source)
    assert not any(artifact['node_id'] in skipped for artifact in result['artifacts'])
    if not allowed:
        assert result['conditional_execution']['required']['reason'] == 'upstream_skipped'
        assert result['conditional_execution']['target']['blocked_by'] == [
            {'kind': 'condition', 'source_node': 'branch_gate', 'source_port': 'allowed'}]
    # Inspect the actual ZenML step inputs, not only the adapter's report.
    script = '''import json,sys
from zenml.client import Client
run=Client().get_pipeline_run(sys.argv[1])
print(json.dumps({key:str(artifacts[0].id) for key,artifacts in run.steps['join'].inputs.items() if key != 'spec' and len(artifacts)==1}))
'''
    native = subprocess.run([str(PYTHON), '-c', script, result['backend_run_id']], env=backend._env(),
                            capture_output=True, text=True, check=True)
    arguments = json.loads(native.stdout.strip().splitlines()[-1])
    assert arguments[kept] == inputs['input_artifact_ids'][kept] and omitted not in arguments
    restored = ZenMLBackend(tmp_path / 'state', python_executable=str(PYTHON), source_root=backend.repository,
                           trusted_code_roots=backend.trusted_code_roots)
    recovered = restored.invoke('run.status', request)
    for key in ('backend_run_id', 'conditional_execution', 'conditional_inputs', 'artifacts', 'node_statuses'):
        assert recovered[key] == result[key]
    log = (backend.root / 'jobs' / request['operation_id'] / 'worker.log').read_text()
    assert 'false branch source must never be imported' not in log
    (tmp_path / 'acceptance.json').write_text(json.dumps(result, indent=2))


@pytest.mark.parametrize('failure', ['exception', 'type'])
def test_native_join_failure_never_mislabels_pending_downstream_as_skipped(tmp_path, failure):
    backend, payload = branch_setup(tmp_path, False, fail=failure == 'exception', wrong_type=failure == 'type')
    result, request = execute(backend, payload)
    assert result['status'] == 'failed', result
    assert result['node_statuses']['join'] == 'failed'
    assert 'join' not in result['skipped_node_ids'] and 'final' not in result['skipped_node_ids']
    assert 'final' not in result['executed_node_ids']
    assert not any(artifact['node_id'] in {'join', 'final'} for artifact in result['artifacts'])
    assert not result['worker_alive']
    (tmp_path / 'acceptance.json').write_text(json.dumps(result, indent=2))


@pytest.mark.parametrize('selected_cut', [False, True])
def test_native_optional_join_without_any_branch_has_no_placeholder_inputs(tmp_path, selected_cut):
    backend, payload = branch_setup(tmp_path, False)
    nodes = {node['id']: node for node in payload['recipe']['nodes']}
    nodes['alternate']['condition']['equals'] = True
    nodes['join']['parameters']['allow_empty'] = True
    if selected_cut:
        payload['execution'] = {'node_ids': ['join', 'final'], 'input_bindings': []}
    result, _ = execute(backend, payload)
    assert result['status'] == 'succeeded', result
    assert set(result['executed_node_ids']) == ({'join', 'final'} if selected_cut else {'gate', 'join', 'final'})
    joined = next(artifact for artifact in result['artifacts'] if artifact['node_id'] == 'join')
    assert json.loads((Path(joined['uri']) / 'data.json').read_text()) == {'measurement': 0.0, 'present': 'none'}
    if selected_cut:
        assert result['skipped_node_ids'] == [] and result['conditional_inputs'] == {}
    else:
        assert result['conditional_inputs']['join']['input_artifact_ids'] == {}
        assert {edge['target_port'] for edge in result['conditional_inputs']['join']['omitted_inputs']} == {'left', 'right'}


async def test_official_mcp_native_branch_join_matches_http_and_persisted_recipe(tmp_path):
    import asyncio
    import httpx
    from mcp.shared.memory import create_connected_server_and_client_session
    from modelbuilder.api import create_app
    from modelbuilder.auth import TokenAuth
    from modelbuilder.mcp_server import OperationGateway, create_server
    from modelbuilder.models import Actor
    from modelbuilder.service import OperationService
    backend, payload = branch_setup(tmp_path, False)
    payload['recipe']['source']['repository'] = 'https://example.org/native-branch-source.git'
    target = next(node for node in payload['recipe']['nodes'] if node['id'] == 'target')
    target['implementation_ref']['entrypoint'] = 'alternate'
    target['parameters'] = {}
    owner = Actor('branch-owner', {'read', 'modify', 'execute'})
    service = OperationService(tmp_path / 'service.sqlite', backend=backend, source_root=backend.repository)
    app = create_app(service, TokenAuth({'branch-test-token': owner}))
    submitted = None
    try:
        async with httpx.AsyncClient(base_url='http://test', headers={'Authorization': 'Bearer branch-test-token'},
                                     transport=httpx.ASGITransport(app=app)) as client:
            gateway = OperationGateway(client)
            async with create_connected_server_and_client_session(create_server(gateway, await gateway.catalog())) as session:
                async def call(name, **request):
                    response = await session.call_tool(name, request)
                    assert not response.isError, response.structuredContent
                    return response.structuredContent
                project = await call('project_create', slug='native-branches', model_name='One conditional model',
                                     recipe=payload['recipe'], idempotency_key='project')
                reference = {'project_id': project['project_id'], 'recipe_id': project['recipe_id']}
                recipe = await call('recipe_get', **reference)
                run_payload = {**reference, 'expected_revision': recipe['revision'], 'trusted': True, 'idempotency_key': 'branch-run'}
                plan = await call('run_plan', **{key: value for key, value in run_payload.items() if key != 'idempotency_key'})
                assert plan['executable'], plan
                submitted = await call('run_submit', **run_payload)
                assert await call('run_submit', **run_payload) == submitted
                run_ref = {'project_id': project['project_id'], 'operation_id': submitted['operation_id']}
                deadline = time.monotonic() + 150
                while time.monotonic() < deadline:
                    result = await call('run_status', **run_ref)
                    if result['status'] in {'succeeded', 'failed', 'cancelled'} and not result['worker_alive']:
                        break
                    await asyncio.sleep(.2)
                assert result['status'] == 'succeeded', result
                http = await client.post('/v1/operations/run.status', json=run_ref)
                assert http.status_code == 200, http.text
                http_result = http.json()
                for key in ('backend_run_id', 'conditional_execution', 'conditional_inputs', 'artifacts', 'node_statuses'):
                    assert http_result[key] == result[key]
                assert await call('recipe_get', **reference) == recipe
                assert result['recipe_id'] == project['recipe_id'] and result['recipe_digest'] == recipe['digest']
                assert set(result['skipped_node_ids']) == {'branch_gate', 'target', 'never', 'required'}
                assert result['conditional_inputs']['join']['input_artifact_ids']['right']
                (tmp_path / 'acceptance.json').write_text(json.dumps(result, indent=2))
    finally:
        if submitted:
            from modelbuilder.backend.common import read_json
            if not read_json(backend.root / 'jobs' / submitted['operation_id'] / 'state.json').get('worker_reaped'):
                backend.invoke('run.cancel', {'project_id': project['project_id'], 'operation_id': submitted['operation_id']})
        service.store.close()


def live_branch_setup(tmp_path, gate_value, evaluate, cancel):
    source = tmp_path / 'live-source'
    source.mkdir()
    fixture = source / 'src/modelbuilder/backend/fixtures.py'
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes((ROOT / 'src/modelbuilder/backend/fixtures.py').read_bytes())
    lock = source / 'requirements/backend.lock'
    lock.parent.mkdir()
    lock.write_bytes((ROOT / 'requirements/backend.lock').read_bytes())
    gate = source / 'gate.py'
    gate.write_text('def gate(value: bool) -> bool:\n    return value\n')
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-qm', 'Pinned conditional live fixture'], check=True)
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    recipe = json.loads((ROOT / 'examples/custom-pytorch/recipe.json').read_text())
    recipe.pop('digest', None)
    files = [{'path': str(path.relative_to(source)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in (fixture, gate)]
    recipe['source'].update(commit=commit, files=files)
    project = str(uuid.uuid4())
    recipe.update(project_id=project, recipe_id=str(uuid.uuid4()))
    recipe['evaluation']['live'] = {'enabled': True, 'max_concurrency': 1, 'timeout_seconds': 30}
    gate_ref = next(ref for ref in files if ref['path'] == 'gate.py')
    recipe['nodes'].append({'id': 'gate', 'kind': 'code.python',
        'implementation_ref': {**gate_ref, 'module': 'live_gate', 'entrypoint': 'gate', 'commit': commit},
        'inputs': [], 'outputs': [{'name': 'allowed', 'type': 'bool'}], 'parameters': {'value': gate_value},
        'environment_ref': 'default', 'resource_request': {'gpu': 0}, 'timeout': 30,
        'retry': {'max_attempts': 1, 'backoff_seconds': 0}, 'cache_policy': 'disabled', 'provenance': {'intended_use': 'train'}})
    for node in recipe['nodes']:
        if node['id'] in {'train', 'evaluate'}:
            node['condition'] = {'source_node': 'gate', 'source_port': 'allowed',
                                 'equals': True if node['id'] == 'train' else evaluate}
        if node['id'] == 'train':
            node['parameters'].update(steps=30 if cancel else (12 if evaluate and gate_value else 3),
                                      step_delay=1.2 if evaluate and gate_value else 0)
    backend = ZenMLBackend(tmp_path / 'state', python_executable=str(PYTHON), source_root=source, trusted_code_roots=[str(source)])
    payload = {'project_id': project, 'recipe': recipe, 'trusted': True, 'max_runtime_seconds': 150,
               'execution': {'node_ids': ['data', 'initial', 'gate', 'train', 'evaluate'], 'input_bindings': []}}
    from tests.integration.native_fixture_support import authorized_backend
    return authorized_backend(backend, payload), payload


@pytest.mark.parametrize('gate_value,evaluate,cancel', [(True, True, False), (True, False, False), (False, True, False), (True, True, True)])
def test_native_conditional_live_evaluation_honors_gate_before_first_checkpoint(tmp_path, gate_value, evaluate, cancel):
    from modelbuilder.backend.common import read_json
    backend, payload = live_branch_setup(tmp_path, gate_value, evaluate, cancel)
    request = {**payload, 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4())}
    plan = backend.invoke('run.plan', request)
    assert plan['executable'], plan
    backend.invoke('run.submit', request)
    job = backend.root / 'jobs' / request['operation_id']
    seen, observed_step, events = [], None, []
    try:
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            if gate_value and evaluate:
                from modelbuilder.backend.live_evaluation import list_evaluations, list_events
                completed = [row for row in list_evaluations(job)['evaluations'] if row['status'] == 'completed']
                events = list_events(job)['events']
                ready = [row['step'] for row in events if row['kind'] == 'checkpoint.ready']
                if completed and ready and max(ready) < (30 if cancel else 12):
                    seen, observed_step = completed, max(ready)
                    if cancel:
                        result = backend.invoke('run.cancel', request)
                        break
            if read_json(job / 'state.json').get('worker_reaped'):
                result = backend.invoke('run.status', request)
                break
            time.sleep(.1)
        else:
            raise AssertionError('Conditional live native run did not terminate')
        assert result['status'] == ('cancelled' if cancel else 'succeeded'), result
        from modelbuilder.backend.live_evaluation import list_evaluations, list_events
        evaluations = list_evaluations(job)['evaluations']
        if not gate_value or not evaluate:
            assert evaluations == [] and list_evaluations(job)['checkpoints'] == []
            assert not (job / 'live_evaluation/evaluations').exists()
            assert not (job / 'live_evaluation/checkpoints').exists()
            assert not any(event['kind'].startswith(('checkpoint.', 'evaluation.')) for event in list_events(job)['events'])
            assert 'evaluate' in result['skipped_node_ids']
            assert ('train' in result['skipped_node_ids']) is (not gate_value)
        else:
            assert seen and observed_step is not None, (evaluations, events)
            for row in seen:
                assert row['evaluation_records']['validation_loss']['treatment']['checkpoint'] == row['checkpoint_digest']
                from modelbuilder.backend.components import read_checkpoint
                snapshot = job / 'live_evaluation/checkpoints' / row['checkpoint_id']
                manifest = read_checkpoint(snapshot)
                assert not manifest['rights']['unresolved']
                assert manifest['rights']['sources'] == payload['recipe']['extensions']['artifact_rights']['sources']
                assert hashlib.sha256((snapshot / 'checkpoint.pt').read_bytes()).hexdigest() == row['checkpoint_digest']
            assert result['conditional_execution']['evaluate']['status'] == 'eligible'
            later = list_events(job)['events']
            if not cancel:
                assert any(row['kind'] == 'checkpoint.ready' and row['step'] > observed_step for row in later)
                assert result['node_statuses']['evaluate'] == 'completed'
            else:
                assert not result['worker_alive'] and 'evaluate' not in result['skipped_node_ids']
                assert not any(row['status'] in {'queued', 'running'} for row in evaluations)
        (tmp_path / 'acceptance.json').write_text(json.dumps({'run': result, 'early_evaluations': seen, 'evaluations': evaluations}, indent=2))
    finally:
        if not read_json(job / 'state.json').get('worker_reaped'):
            backend.invoke('run.cancel', request)


@pytest.mark.parametrize('change', ['missing_rights', 'revoked'])
def test_builtin_live_subprocess_blocks_unreviewed_or_revoked_snapshot(tmp_path, change):
    """Use the actual evaluator process; no mock of dispatch or Torch deserialization."""
    script = '''import json,sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], str(Path(sys.argv[1]) / 'src')]
from tests.live_evaluation.test_fixture_snapshot_rights import fixture
from modelbuilder.backend.live_evaluation import LiveEvaluationCoordinator, list_evaluations
from modelbuilder.store import SQLiteStore
from modelbuilder.hub_rights import digest
root, descriptor, policy, spec = fixture(Path(sys.argv[2]))
change = sys.argv[3]
if change == 'missing_rights':
    descriptor.pop('rights')
    (root / 'checkpoint.manifest.json').write_text(json.dumps(descriptor))
else:
    policy.update(status='revoked', revision=2)
    policy['review_digest'] = digest({key:value for key,value in policy.items() if key != 'review_digest'})
    store = SQLiteStore(spec['rights_authorization']['policy_store_guard']['path'])
    store.put('hub_license_policy', policy['policy_id'], policy['project_id'], policy, expected_revision=1)
    store.close()
job = Path(sys.argv[2]) / 'native-evaluator'
coordinator = LiveEvaluationCoordinator(job, 'train', {'kind':'fixture', 'spec':spec, 'data':{}, 'parameters':{}}, timeout_seconds=30)
try:
    coordinator.checkpoint_ready(root, step=2, purpose='last')
finally:
    coordinator.close()
rows = list_evaluations(job)['evaluations']
assert len(rows) == 1 and rows[0]['status'] == 'failed', rows
expected = 'no reviewed rights lineage' if change == 'missing_rights' else 'reviewed policy revision or status changed'
assert expected in rows[0]['error']['message'], rows
assert rows[0]['metrics'] == {} and rows[0]['evaluation_records'] == {}, rows
assert not list((job / 'evaluations').glob('*/output/result.json'))
print(json.dumps(rows[0]))
'''
    completed = subprocess.run([str(PYTHON), '-I', '-B', '-c', script, str(ROOT), str(tmp_path), change],
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert json.loads(completed.stdout.splitlines()[-1])['status'] == 'failed'
