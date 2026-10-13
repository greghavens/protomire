"""Plan and submit share read-only executable admission, not optimistic warnings."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from modelbuilder.backend.adapter import ZenMLBackend
from modelbuilder.backend.admission import BackendError, validate_execution, validate_node_settings, verify_environment
from modelbuilder.backend.operator_config import load_operator_config

ROOT = Path(__file__).resolve().parents[2]
BACKEND_PYTHON = ROOT / '.venv-backend/bin/python'


@pytest.fixture
def source(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    code = source / 'unrelated.py'
    code.write_text('def calculate(value: int) -> int:\n    return value * 3\n')
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    digest = hashlib.sha256(code.read_bytes()).hexdigest()
    body = {'recipe_id': 'recipe', 'project_id': 'project', 'revision': 1,
            'source': {'commit': commit, 'files': [{'path': 'unrelated.py', 'sha256': digest}]},
            'environment': {'python': '.'.join(map(str,sys.version_info[:2])), 'requirements': []},
            'nodes': [{'id': 'calculate', 'kind': 'code.python', 'implementation_ref': {'module': 'unrelated', 'entrypoint': 'calculate', 'path': 'unrelated.py', 'sha256': digest, 'commit': commit},
                       'environment_ref': 'default', 'resource_request': {'gpu': 0}, 'cache_policy': 'disabled', 'timeout': 5,
                       'parameters': {'value': 4}, 'retry': {'max_attempts': 1, 'backoff_seconds': 0}}]}
    backend = ZenMLBackend(tmp_path / 'state', python_executable=sys.executable, source_root=source, trusted_code_roots=[str(source)])
    return backend, {'project_id': 'project', 'recipe': body, 'trusted': True}


def test_valid_source_plan_has_no_journal_or_user_code_side_effects(source):
    backend, payload = source
    before = sorted(str(path) for path in backend.root.rglob('*'))
    assert validate_execution(backend, payload) == payload['recipe']
    assert sorted(str(path) for path in backend.root.rglob('*')) == before


@pytest.mark.parametrize('field,value', [('cache_policy', 'enabled'), ('environment_ref', 'other'),
    ('resource_request', {'cpu': 1}), ('resource_request', {'memory_mb': 1024}), ('resource_request', {'gpu': 1}),
    ('retry', {'max_attempts': 2, 'backoff_seconds': 0.5}), ('retry', {'max_attempts': 1, 'backoff_seconds': 1})])
def test_ignored_execution_settings_are_rejected(field, value):
    with pytest.raises(BackendError):
        validate_node_settings({'id': 'node', field: value})


@pytest.mark.parametrize('mutate,code', [
    (lambda p: p.update(trusted=False), 'untrusted_execution_blocked'),
    (lambda p: p.update(target='remote'), 'capability_unavailable'),
    (lambda p: p['recipe']['nodes'][0].update(kind='training.kl_anchor'), 'capability_unavailable'),
    (lambda p: p['recipe']['nodes'][0]['implementation_ref'].update(commit='1'*40), 'dependency_mismatch'),
    (lambda p: p['recipe']['environment'].update(requirements=['absent-modelbuilder-package==0.0.1']), 'dependency_unavailable'),
    (lambda p: p['recipe']['environment'].update(requirements=['pip>=1']), 'dependency_mismatch'),
    (lambda p: p['recipe']['environment'].update(python='3.9'), 'dependency_mismatch'),
    (lambda p: p['recipe'].update(resources=[{'id':'missing', 'sha256':'0'*64}]), 'dependency_unavailable'),
])
def test_shared_admission_failures(source, mutate, code):
    backend, payload = source
    mutate(payload)
    with pytest.raises(BackendError) as failure:
        validate_execution(backend, payload)
    assert failure.value.code == code


def test_changed_source_and_untrusted_root_fail(source):
    backend, payload = source
    backend.trusted_code_roots = []
    with pytest.raises(BackendError, match='trusted source root'):
        validate_execution(backend, payload)
    backend.trusted_code_roots = [str(backend.repository)]
    (backend.repository / 'unrelated.py').write_text('raise RuntimeError("must not import")')
    with pytest.raises(BackendError, match='changed'):
        validate_execution(backend, payload)


def test_target_markers_are_evaluated_in_backend_interpreter(source):
    backend, payload = source
    payload['recipe']['environment']['requirements'] = ['missing-package==1; sys_platform == "never"']
    assert verify_environment(backend, payload['recipe'])['verified']


def test_config_is_explicit_relative_and_strict(tmp_path):
    path = tmp_path / 'operator.json'
    path.write_text(json.dumps({'source_root': 'source', 'trusted_code_roots': ['source'], 'dependency_paths': {'data': 'data.json'}}))
    assert load_operator_config(path) == {'source_root': str(tmp_path/'source'), 'trusted_code_roots':[str(tmp_path/'source')], 'dependency_paths':{'data':str(tmp_path/'data.json')}}
    path.write_text('{"allow_untrusted":true}')
    with pytest.raises(ValueError):
        load_operator_config(path)


def test_plan_and_submit_report_identical_admission_failure(source):
    backend, payload = source
    payload['recipe']['nodes'][0]['cache_policy'] = 'enabled'
    plan = backend.invoke('run.plan', payload)
    assert plan['executable'] is False
    with pytest.raises(BackendError) as failure:
        backend.invoke('run.submit', payload)
    assert plan['blocked_reasons'][0] == {'code': failure.value.code, 'message': failure.value.message, 'details': failure.value.details}
    assert not (backend.root/'jobs').exists()
    assert not (backend.root/'requests').exists()


def test_executable_plan_and_capability_discovery(source):
    backend, payload = source
    plan = backend.invoke('run.plan', payload)
    assert plan['executable'] and plan['blocked_reasons'] == []
    supported = backend.invoke('execution.targets', {'project_id':'project'})['capabilities']
    assert 'training.kl_anchor' not in supported['executable_kinds']
    assert 'training.kl_anchor' in supported['unsupported_schema_kinds']
    assert supported['arbitrary_python']['configured'] is True


def test_api_factory_loads_operator_source_and_dependencies(tmp_path, monkeypatch):
    from modelbuilder.api import create_app
    config = tmp_path/'operator.json'
    config.write_text(json.dumps({'source_root':'source', 'trusted_code_roots':['source'], 'dependency_paths':{'data':'data.json'}}))
    monkeypatch.setenv('MODELBUILDER_OPERATOR_CONFIG', str(config))
    monkeypatch.setenv('MODELBUILDER_DB_PATH', str(tmp_path/'state/db.sqlite'))
    app = create_app(tokens={})
    backend = app.state.service.backend
    assert backend.repository == tmp_path/'source'
    assert backend.trusted_code_roots == [str(tmp_path/'source')]
    assert backend.dependency_paths == {'data':str(tmp_path/'data.json')}


def test_plan_requires_explicit_trust_without_granting_it(source):
    backend, payload = source
    payload.pop('trusted')
    plan = backend.invoke('run.plan', payload)
    assert not plan['executable']
    assert plan['blocked_reasons'][0]['code'] == 'untrusted_execution_blocked'
    assert 'trusted' not in payload


def test_dataset_roots_are_operator_owned_project_scoped_paths(tmp_path):
    config = tmp_path/'operator.json'
    config.write_text(json.dumps({'dataset_roots':{'project-1':{'raw':'data'}}}))
    assert load_operator_config(config)['dataset_roots'] == {'project-1':{'raw':str(tmp_path/'data')}}
    config.write_text(json.dumps({'dataset_roots':{'project-1':{'../escape':'data'}}}))
    with pytest.raises(ValueError):
        load_operator_config(config)


def test_live_evaluation_without_checkpoint_bridge_is_blocked(source):
    backend, payload = source
    payload['recipe']['evaluation'] = {'live':{'enabled':True}}
    plan = backend.invoke('run.plan', payload)
    assert not plan['executable']
    assert plan['blocked_reasons'][0]['details']['field'] == 'evaluation.live'


@pytest.mark.parametrize('settings', [{'enabled':'yes'}, {'enabled':False,'max_concurrency':99}, {'enabled':True,'timeout_seconds':0}])
def test_live_settings_are_typed_during_plan(source, settings):
    backend, payload = source
    payload['recipe']['evaluation'] = {'live':settings}
    plan = backend.invoke('run.plan', payload)
    assert not plan['executable']
    assert plan['blocked_reasons'][0]['details']['field'] == 'evaluation.live'


@pytest.mark.parametrize('settings', [None, [], {'max_parallel_stages': True}, {'max_parallel_stages': 1.0},
    {'max_parallel_stages': 0}, {'max_parallel_stages': 9}, {'max_parallel_stages': '2'}, {'concurrency': 2}])
def test_stage_settings_rejected_identically_before_plan_or_submit_side_effects(source, settings):
    backend, payload = source
    payload['recipe']['execution'] = settings
    plan = backend.invoke('run.plan', payload)
    assert not plan['executable']
    with pytest.raises(BackendError) as failure:
        backend.invoke('run.submit', payload)
    assert plan['blocked_reasons'][0] == {'code': failure.value.code, 'message': failure.value.message, 'details': failure.value.details}
    assert failure.value.details['field'] == 'execution.max_parallel_stages'
    assert not (backend.root / 'jobs').exists()
    assert not (backend.root / 'requests').exists()


@pytest.mark.parametrize('settings', [{}, {'max_parallel_stages': 1}, {'max_parallel_stages': 2}, {'max_parallel_stages': 8}])
def test_valid_stage_settings_are_read_only_and_follow_platform_admission(source, settings):
    backend, payload = source
    payload['recipe']['execution'] = settings
    before = json.dumps(payload, sort_keys=True)
    if settings.get('max_parallel_stages', 1) > 1 and sys.platform != 'linux':
        with pytest.raises(BackendError) as failure:
            validate_execution(backend, payload)
        assert failure.value.code == 'capability_unavailable'
    else:
        assert validate_execution(backend, payload) == payload['recipe']
    assert json.dumps(payload, sort_keys=True) == before


@pytest.mark.parametrize('platform,device', [('darwin', 'cpu'), ('darwin', 'mps'), ('linux', 'cuda:0'), ('win32', 'cpu')])
def test_unverified_parallel_platforms_and_accelerators_fail_closed_without_changing_sequential(platform, device, monkeypatch):
    from modelbuilder.backend import admission
    monkeypatch.setattr(admission, 'sys', SimpleNamespace(platform=platform))
    body = {'environment': {'device': device}, 'execution': {'max_parallel_stages': 2}}
    with pytest.raises(BackendError) as failure:
        admission.validate_recipe_settings(body)
    assert failure.value.code == 'capability_unavailable'
    assert failure.value.details['field'] == 'execution.max_parallel_stages'
    assert 'verification' in failure.value.message
    body['execution']['max_parallel_stages'] = 1
    assert admission.validate_recipe_settings(body) == 1
    body.pop('execution')
    assert admission.validate_recipe_settings(body) == 1


@pytest.mark.parametrize('platform,supported', [('linux', True), ('darwin', False), ('win32', False)])
def test_parallel_capability_matches_ordinary_platform_gate(platform, supported, monkeypatch):
    from modelbuilder.backend import admission
    monkeypatch.setattr(admission, 'sys', SimpleNamespace(platform=platform))
    result = admission.capabilities(SimpleNamespace(trusted_code_roots=[]))
    settings = result['recipe_settings']['execution']['max_parallel_stages']
    assert result['control_flow']['independent_stage_concurrency'] is supported
    assert settings['supported_maximum'] == (8 if supported else 1)
    assert settings['default'] == 1 and settings['maximum'] == 8
    assert settings['devices'] == ['cpu'] and settings['platforms'] == ['linux']
    assert settings['live_evaluation'] is True and settings['iteration_live_evaluation'] is False
    assert result['arbitrary_python']['isolation'] is False


def test_parallel_platform_rejection_is_shared_by_ordinary_plan_and_submit(source, monkeypatch):
    from modelbuilder.backend import admission
    backend, payload = source
    monkeypatch.setattr(admission, 'sys', SimpleNamespace(platform='darwin'))
    payload['recipe']['execution'] = {'max_parallel_stages': 2}
    plan = backend.invoke('run.plan', payload)
    assert plan['executable'] is False
    assert plan['capabilities']['control_flow']['independent_stage_concurrency'] is False
    with pytest.raises(BackendError) as failure:
        backend.invoke('run.submit', payload)
    assert failure.value.code == 'capability_unavailable'
    assert plan['blocked_reasons'][0] == {'code': failure.value.code, 'message': failure.value.message, 'details': failure.value.details}
    assert not (backend.root / 'jobs').exists()
    assert not (backend.root / 'requests').exists()
