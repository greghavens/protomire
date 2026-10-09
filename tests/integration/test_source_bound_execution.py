"""Actual unrelated source execution and supervisor termination, no ML fixture code."""
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.common import read_json, process_group_state

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / '.venv-backend/bin/python'
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not PYTHON.is_file(), reason='Pinned backend required')]

HARNESS = '''from pathlib import Path
import json
from math_helpers import loss_gradient

def train(output_path: str) -> Path:
    # Independent model, optimizer, loss and export, unrelated to platform fixtures.
    rows = [(-2.,-5.),(-1.,-2.),(0.,1.),(1.,4.),(2.,7.)]
    weight, bias = 0., 0.
    for step in range(120):
        loss, dw, db = loss_gradient(rows, weight, bias)
        weight -= 0.1 * dw
        bias -= 0.1 * db
    state = {"architecture":"affine", "weight":weight,"bias":bias,"steps":120,"loss":loss}
    target = Path(output_path)
    target.mkdir(parents=True, exist_ok=True)
    (target / "checkpoint.json").write_text(json.dumps(state))
    reloaded = json.loads((target / "checkpoint.json").read_text())
    assert reloaded == state
    (target / "export.json").write_text(json.dumps({"weight":weight,"bias":bias,"prediction":weight*3+bias}))
    return target

def block() -> int:
    import subprocess, sys
    subprocess.run([sys.executable,"-c","import time;time.sleep(60)"], check=True)
    return 1
'''
HELPER = '''def loss_gradient(rows,w,b):
    residuals=[(w*x+b-y,x) for x,y in rows]
    n=len(rows)
    return sum(r*r for r,x in residuals)/n, 2*sum(r*x for r,x in residuals)/n, 2*sum(r for r,x in residuals)/n
'''


def setup(tmp_path, *, timeout=30, entrypoint='train'):
    source=tmp_path/'independent-source'
    source.mkdir()
    (source/'harness.py').write_text(HARNESS)
    (source/'math_helpers.py').write_text(HELPER)
    subprocess.run(['git','init','-q',str(source)],check=True)
    subprocess.run(['git','-C',str(source),'add','.'],check=True)
    subprocess.run(['git','-C',str(source),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','Independent harness'],check=True)
    commit=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
    files=[{'path':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in source.glob('*.py')]
    implementation=next(p for p in files if p['path']=='harness.py')
    project=str(uuid.uuid4())
    node={'id':'harness','kind':'code.python','implementation_ref':dict(module='harness',entrypoint=entrypoint,commit=commit,**implementation),
          'inputs':[],'outputs':[{'name':'output','type':'Path' if entrypoint=='train' else 'int'}],
          'parameters':{'output_path':str(tmp_path/'model-package')} if entrypoint=='train' else {},
          'environment_ref':'default','resource_request':{'gpu':0},'timeout':timeout,
          'retry':{'max_attempts':1,'backoff_seconds':0},'cache_policy':'disabled','provenance':{}}
    recipe={'schema_version':'0','recipe_id':str(uuid.uuid4()),'revision':1,'project_id':project,'name':'Independent affine model',
            'source':{'repository':str(source),'commit':commit,'files':files},'nodes':[node],'edges':[],
            'resources':[],'external_dependencies':[],'environment':{'python':'3.12','requirements':['zenml==0.97.0']},'evaluation':{},'outputs':[]}
    backend=ZenMLBackend(tmp_path/'state',python_executable=str(PYTHON),source_root=source,trusted_code_roots=[str(source)])
    payload={'project_id':project,'recipe':recipe,'trusted':True,'operation_id':str(uuid.uuid4()),'idempotency_key':str(uuid.uuid4()),'max_runtime_seconds':90}
    return backend,payload


def wait(backend,payload, expected=None):
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        record=backend.invoke('run.status',payload)
        if record['status'] == expected if expected else record['status'] in {'succeeded','failed','cancelled'}:
            return record
        time.sleep(.3)
    raise AssertionError(record)


def test_unrelated_source_model_loss_checkpoint_export(tmp_path):
    backend,payload=setup(tmp_path)
    plan=backend.invoke('run.plan',payload)
    assert plan['executable'],plan
    assert not (backend.root/'jobs').exists()
    backend.invoke('run.submit',payload)
    result=wait(backend,payload)
    assert result['status']=='succeeded',result
    assert result['backend_run_id'] and len(result['artifacts'])==1
    model=json.loads((tmp_path/'model-package/export.json').read_text())
    checkpoint=json.loads((tmp_path/'model-package/checkpoint.json').read_text())
    assert abs(model['prediction']-10)<1e-6
    assert checkpoint['loss']<1e-12 and checkpoint['steps']==120


def test_node_timeout_terminates_native_descendant_group(tmp_path):
    backend,payload=setup(tmp_path,timeout=.25,entrypoint='block')
    backend.invoke('run.submit',payload)
    result=wait(backend,payload, expected='cancelled')
    assert result['status']=='cancelled',result
    job=backend.root/'jobs'/payload['operation_id']
    cancellation=read_json(job/'cancel.json')
    assert cancellation['reason']=='node_timeout_exceeded'
    assert cancellation['node_id']=='harness'
    state=read_json(job/'state.json')
    assert state['termination']['confirmed'] and state['process_group_terminated']
    assert process_group_state(state['worker_pid']) is False


def test_cancellation_metadata_lag_never_reports_terminal_failure(tmp_path):
    script = r'''
import json
from pathlib import Path
from types import SimpleNamespace as NS
from modelbuilder.backend import sdk
from modelbuilder.backend.common import write_json
import sys
job=Path(sys.argv[1]);job.mkdir(exist_ok=True)
write_json(job/'request.json', {'project_id':'project','recipe':{},'submitted_at':'now'})
write_json(job/'state.json', {'worker_pid':123,'worker_identity':'test','termination':{'confirmed':True,'reason':'cancelled','process_group_terminated':True}})
write_json(job/'cancel.json', {'reason':'node_timeout_exceeded'})
run=NS(status=NS(value='failed'),config=NS(extra={}),id='native',snapshot=None,model_version=None,exception_info=None)
sdk.reconcile=lambda job:run
sdk.alive=lambda *args:False
sdk.process_group_state=lambda *args:False
sdk.termination_confirmed=lambda state:True
sdk.metadata_values=lambda run:{}
sdk.metrics_for=lambda run:{}
sdk.artifacts_for=lambda *args:[]
sdk.tracking_status=lambda *args:{}
print(json.dumps(sdk.status(job)))
'''
    result = subprocess.run([str(PYTHON), '-c', script, str(tmp_path/'race')], cwd=ROOT,
                            text=True,capture_output=True,check=True, env=ZenMLBackend(tmp_path)._env())
    assert json.loads(result.stdout)['status'] == 'cancel_requested'
