"""Real CPU checks plus explicitly mocked unsupported-hardware admission paths."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from modelbuilder.backend import placement
from modelbuilder.backend.admission import BackendError, validate_node_settings


def payload(device="cpu"):
    return {"target": placement.target_for_device(device), "recipe": {"environment": {"device": device}, "nodes": []}}


def test_explicit_device_contract_rejects_automatic_selection():
    for value in ["auto", "cuda", "cuda:-1", "cuda:01", "cuda:1000", "mps:0", None]:
        with pytest.raises(ValueError):
            placement.device_from_recipe({"environment": {"device": value}})
    assert placement.device_from_recipe({}) == "cpu"
    assert placement.target_for_device("cuda:12") == "local-cuda:12"


def test_target_must_match_versioned_recipe_device():
    body = payload()
    body["target"] = "local-mps"
    with pytest.raises(BackendError, match="versioned"):
        placement.validate_placement(None, body)


def test_cuda_profile_is_unavailable_even_with_mocked_hardware(monkeypatch):
    monkeypatch.setattr(placement, "probe", lambda *a, **k: pytest.fail("CUDA must reject before hardware probing"))
    with pytest.raises(BackendError, match="hash-locked") as error:
        placement.validate_placement(None, payload("cuda:3"), allocate=True)
    assert error.value.details["profile_status"] == "unavailable"


def test_mps_requires_reviewed_lock_before_runtime_probe(monkeypatch):
    monkeypatch.setattr(placement, "probe", lambda *a, **k: pytest.fail("Missing profile must reject before probing"))
    with pytest.raises(BackendError, match="hash-locked"):
        placement.validate_placement(None, payload("mps"))


def test_mocked_mps_profile_runtime_allocation_and_resume(monkeypatch):
    body = payload("mps")
    body["recipe"]["environment"]["lock_file"] = {"sha256": placement.MPS_LOCK_SHA256}
    calls=[]
    def fake_probe(backend, device, *, allocate=False):
        calls.append(allocate)
        return {"available": True, "device": device, "platform": "darwin", "machine": "arm64", "torch": "2.9.0", "allocation_verified": allocate}
    monkeypatch.setattr(placement, "probe", fake_probe)
    assert not placement.validate_placement(None, body)["allocation_verified"]
    result=placement.validate_placement(None, body, allocate=True)
    assert result["allocation_verified"] and not result["exact_resume"]
    assert not result["reservation"] and not result["isolation"]
    assert result["hardware_acceptance"] == "not_verified"
    assert calls == [False, True]
    body["resume_mode"] = "exact_state"
    with pytest.raises(BackendError, match="not hardware-verified"):
        placement.validate_placement(None, body)
    body["resume_mode"] = "warm_start"
    assert placement.validate_placement(None, body)["device"] == "mps"


def test_mocked_mps_runtime_rejects_wrong_platform_or_build(monkeypatch):
    body=payload("mps")
    body["recipe"]["environment"]["lock_file"]={"sha256":placement.MPS_LOCK_SHA256}
    for field, value in [("platform","linux"),("machine","x86_64"),("torch","2.9.0+cpu"),("available",False)]:
        result={"available": True,"device":"mps","platform":"darwin","machine":"arm64","torch":"2.9.0",field:value}
        monkeypatch.setattr(placement,"probe",lambda *a, **k: result)
        with pytest.raises(BackendError):
            placement.validate_placement(None,body)


def test_cpu_evaluator_environment_cannot_inherit_selected_gpu():
    base={"CUDA_VISIBLE_DEVICES":"2", "HIP_VISIBLE_DEVICES":"1", "MODELBUILDER_EXECUTION_DEVICE":"cuda:0", "PYTORCH_ENABLE_MPS_FALLBACK":"1"}
    actual=placement.placement_environment("cpu",base)
    assert actual["CUDA_VISIBLE_DEVICES"] == actual["HIP_VISIBLE_DEVICES"] == ""
    assert actual["MODELBUILDER_EXECUTION_DEVICE"] == "cpu"
    assert actual["PYTORCH_ENABLE_MPS_FALLBACK"] == "0"
    assert base["CUDA_VISIBLE_DEVICES"] == "2"


def test_resource_reservations_remain_rejected():
    for request in [{"gpu":1},{"cpu":1},{"memory_mb":1024}]:
        with pytest.raises(BackendError, match="reservations"):
            validate_node_settings({"id":"train","resource_request":request})


def test_cpu_plain_python_admission_does_not_add_torch_dependency(monkeypatch):
    monkeypatch.setattr(placement,"probe",lambda *a, **k: pytest.fail("CPU Python needs no Torch allocation probe"))
    assert placement.validate_placement(None,payload(),allocate=True)["available"]


def test_profile_hash_still_matches_frozen_cpu_and_mac_profiles():
    import hashlib
    root=Path(__file__).resolve().parents[2]
    assert hashlib.sha256((root/'requirements/profiles/backend-darwin-arm64.lock').read_bytes()).hexdigest()==placement.MPS_LOCK_SHA256
    assert 'torch==2.9.0+cpu' in (root/'requirements/backend.in').read_text()


def test_probe_timeout_is_unavailable_without_fallback(monkeypatch):
    import subprocess
    def timeout(*a,**k):
        assert a[0][1:3] == ['-I','-B']
        assert k['timeout']==20
        raise subprocess.TimeoutExpired(a[0],20)
    monkeypatch.setattr(placement.subprocess,'run',timeout)
    result=placement.probe(SimpleNamespace(python=sys.executable),'mps',allocate=True)
    assert result['available'] is False and result['device']=='mps'


def test_real_cpu_probe_forward_backward_in_configured_interpreter():
    pytest.importorskip('torch')
    result=placement.probe(SimpleNamespace(python=sys.executable),'cpu',allocate=True)
    assert result['available'] and result['allocation_verified'], result
    assert result['device']=='cpu'


def test_real_device_aware_training_cpu_checkpoint_eval_and_cancel(tmp_path):
    pytest.importorskip('torch')
    from modelbuilder.backend import fixtures
    data=fixtures.dataset({'fixture':'custom'})
    initial=fixtures.initialize(data,{'fixture':'custom'},tmp_path/'initial')
    events=[]
    def ready(**event):
        state=fixtures.load_checkpoint(event['path'])
        assert state['placement']['device']=='cpu'
        assert all(value.device.type=='cpu' for value in state['model'].values())
        events.append(event['step'])
    params={'steps':3,'_execution_device':'cpu','_checkpoint_ready':ready}
    trained=fixtures.train(data,initial,params,tmp_path/'trained')
    state=fixtures.load_checkpoint(trained)
    assert events==[1,2,3] and state['placement']['dtype']=='float32'
    assert state['accelerator_rng'] is None
    assert fixtures.evaluate(data,trained,{},tmp_path/'eval')['device']=='cpu'
    copied=placement.checkpoint_cpu(state)
    assert copied['model']['net.0.weight'].data_ptr()!=state['model']['net.0.weight'].data_ptr()
    cancel=tmp_path/'cancel.json'
    cancel.write_text('{}')
    with pytest.raises(InterruptedError):
        fixtures.train(data,initial,{'steps':3,'_execution_device':'cpu','_cancel_file':str(cancel)},tmp_path/'cancelled')
    assert fixtures.load_checkpoint(tmp_path/'cancelled')['step']==1
    assert not list((tmp_path/'cancelled').glob('*.partial'))


def test_real_unavailable_mps_is_not_silently_cpu():
    torch=pytest.importorskip('torch')
    if torch.backends.mps.is_available():
        pytest.skip('Negative-path test needs an MPS-unavailable host')
    with pytest.raises(ValueError,match='unavailable'):
        placement.runtime_device_identity('mps')


def test_accelerator_component_checkpoint_cannot_inherit_cpu_resume_claims():
    declared=['exact_state','warm_start']
    assert placement.effective_resume_modes('cpu',declared)==declared
    assert placement.effective_resume_modes('mps',declared)==['warm_start']
    assert placement.effective_resume_modes('cuda:2',declared)==['warm_start']
    assert declared==['exact_state','warm_start']


@pytest.mark.parametrize('claimed,accepted', [(['exact_state','warm_start'],False),(['warm_start'],True)])
def test_mocked_mps_component_context_and_checkpoint_resume_gate(tmp_path,monkeypatch,claimed,accepted):
    pytest.importorskip('torch')
    pytest.importorskip('zenml')
    from modelbuilder.backend.components import runtime
    from modelbuilder.backend import worker
    output=tmp_path/'output'
    component={'protocol':'modelbuilder.component/v1','role':'training',
               'capabilities':{'resume_modes':['exact_state','warm_start']}}
    node={'component':component,'outputs':[{'name':'checkpoint','type':'Path'}],'implementation_ref':{}}
    captured={}
    def function(*,inputs,parameters,context):
        captured.update(context)
        return {'checkpoint':output}
    function.__source_snapshot_root__=str(tmp_path)
    monkeypatch.setattr(worker,'context',lambda spec: ({},output))
    monkeypatch.setattr(runtime,'validate_component',lambda node: component)
    monkeypatch.setattr(runtime,'runtime_environment',lambda: {'mocked_runtime':True})
    monkeypatch.setattr(runtime,'load_callable',lambda *a: function)
    monkeypatch.setattr(placement,'runtime_device_identity',lambda device: {'device':device,'mocked_hardware':True})
    monkeypatch.setattr(runtime,'read_checkpoint',lambda path: {'identities':captured['identities'],'resume_modes':claimed})
    spec={'component_node':node,'job_dir':str(tmp_path),'node_id':'train','placement':{'device':'mps'},
          'source':{'files':[]},'source_root':str(tmp_path),'trusted_code_roots':[str(tmp_path)]}
    if accepted:
        assert runtime.dispatch({},spec)==output
    else:
        with pytest.raises(ValueError,match='accelerator exact_state is unverified'):
            runtime.dispatch({},spec)
    assert captured['device']=='mps' and captured['resume_modes']==['warm_start']
    assert captured['environment']['placement']['device']=='mps'


def test_selected_python_cut_never_allocates_for_unselected_builtin_train(monkeypatch):
    body = payload()
    body['recipe']['nodes'] = [{'id': 'python', 'kind': 'code.python'}, {'id': 'train', 'kind': 'model.train'}]
    body['execution'] = {'node_ids': ['python'], 'input_bindings': []}
    monkeypatch.setattr(placement, 'probe', lambda *a, **k: pytest.fail('Unselected training must not trigger a tensor probe'))
    selected = placement.validate_placement(None, body, allocate=True)
    assert selected['device'] == 'cpu' and not selected['allocation_verified']
    assert selected['requested_device'] == 'cpu' and selected['component_execution_observed'] is False


def test_mocked_mps_probe_does_not_claim_observed_component_execution(monkeypatch):
    body = payload('mps')
    body['recipe']['environment']['lock_file'] = {'sha256': placement.MPS_LOCK_SHA256}
    monkeypatch.setattr(placement, 'probe', lambda *a, **k: {'available': True, 'device': 'mps',
        'platform': 'darwin', 'machine': 'arm64', 'torch': '2.9.0', 'allocation_verified': True})
    result = placement.validate_placement(None, body, allocate=True)
    assert result['allocation_verified'] and result['allocation_verification_scope'] == 'tiny_tensor_probe_only'
    assert not result['component_execution_observed'] and result['hardware_acceptance'] == 'not_verified'


def test_mocked_accelerator_exact_resume_is_rejected_before_component_call(tmp_path, monkeypatch):
    pytest.importorskip('zenml')
    from modelbuilder.backend.components import runtime
    from modelbuilder.backend import worker
    component = {'protocol': 'modelbuilder.component/v1', 'role': 'training',
                 'capabilities': {'resume_modes': ['exact_state', 'warm_start']}}
    node = {'component': component, 'outputs': [], 'implementation_ref': {}}
    monkeypatch.setattr(worker, 'context', lambda spec: ({}, tmp_path / 'output'))
    monkeypatch.setattr(runtime, 'validate_component', lambda value: component)
    monkeypatch.setattr(runtime, 'runtime_environment', lambda: {'mocked_runtime': True})
    monkeypatch.setattr(placement, 'runtime_device_identity', lambda device: {'device': device})
    def checked_resume(path, mode, identities, capabilities):
        assert mode == 'exact_state' and capabilities['resume_modes'] == ['warm_start']
        raise ValueError('Requested resume mode is not supported by component and checkpoint')
    monkeypatch.setattr(runtime, 'validate_resume', checked_resume)
    monkeypatch.setattr(runtime, 'load_callable', lambda *a: pytest.fail('Must reject before loading training code'))
    spec = {'component_node': node, 'job_dir': str(tmp_path), 'node_id': 'train', 'placement': {'device': 'mps'},
            'source': {'files': []}, 'source_root': str(tmp_path), 'trusted_code_roots': [str(tmp_path)],
            'resume_path': str(tmp_path / 'prior'), 'resume_mode': 'exact_state'}
    with pytest.raises(ValueError, match='not supported'):
        runtime.dispatch({}, spec)
    assert component['capabilities']['resume_modes'] == ['exact_state', 'warm_start']


@pytest.mark.parametrize('conditional', [False, True])
def test_shared_static_and_conditional_invocation_preserves_placement_and_bound_inputs(tmp_path, monkeypatch, conditional):
    pytest.importorskip('zenml')
    from modelbuilder.backend import worker, conditional_worker
    project = SimpleNamespace(id='project')
    artifact = SimpleNamespace(id='gate-artifact', project=project, load=lambda: True)
    received = []
    class Call:
        def with_options(self, **kwargs):
            return self
        def __call__(self, **kwargs):
            received.append(kwargs)
            return artifact
    gate = {'id': 'gate', 'kind': 'component.run', 'parameters': {}, 'outputs': [{'name': 'allowed', 'type': 'bool'}]}
    train = {'id': 'train', 'kind': 'component.run', 'parameters': {}, 'outputs': [{'name': 'trained', 'type': 'Path'}]}
    if conditional:
        train['condition'] = {'source_node': 'gate', 'source_port': 'allowed', 'equals': True}
    recipe = {'nodes': [gate, train], 'edges': []}
    placement_record = {'device': 'mps', 'requested_device': 'mps', 'component_execution_observed': False}
    execution = {'node_ids': ['gate', 'train'], 'input_bindings': [{'fixture': 'explicit-selected-boundary'}]}
    prepared = {'job': tmp_path, 'request': {'placement': placement_record, 'source_root': str(tmp_path), 'run_name': 'native-name'},
                'project': project, 'recipe': recipe, 'digest': 'pinned-recipe', 'provenance': {'placement': placement_record},
                'execution': execution, 'execution_provenance': execution, 'active_recipe': recipe,
                'ordered': [gate, train], 'functions': {'gate': Call(), 'train': Call()},
                'bound_inputs': {('train', 'data'): artifact}, 'cache_identities': {}}
    captured_bindings = []
    monkeypatch.setattr(worker, 'live_evaluator', lambda *a, **kw: captured_bindings.append(kw['input_bindings']))
    if conditional:
        monkeypatch.setattr(worker, 'prepare_run', lambda job: prepared)
        monkeypatch.setattr(conditional_worker, 'Client', lambda: SimpleNamespace(get_pipeline_run=lambda *a, **k:
            SimpleNamespace(id='run', model_version=None)))
        monkeypatch.setattr(conditional_worker, 'validate_native_type', lambda *a: None)
        monkeypatch.setattr(conditional_worker, 'log_metadata', lambda *a, **k: None)
        conditional_worker.execute_conditional_recipe.entrypoint(str(tmp_path))
    else:
        worker.invoke_nodes(prepared)
    assert len(received) == 2 and received[1]['data'] is artifact
    assert all(call['spec']['placement'] == placement_record for call in received)
    assert all(call['spec']['execution'] == execution for call in received)
    assert captured_bindings == [execution['input_bindings'], execution['input_bindings']]
