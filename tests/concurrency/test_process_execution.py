"""Inert process ABI and fresh-interpreter isolation contract."""
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from modelbuilder.backend.process_execution import encode, decode, execute

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / '.venv-backend/bin/python'


def test_inert_codec_preserves_typed_outputs_and_tag_like_user_keys():
    value = ({'path': ['path', '/ordinary/data'], 'nested': [True, None, 3.5]}, Path('/file'))
    assert decode(json.loads(json.dumps(encode(value)))) == value
    with pytest.raises(TypeError):
        encode(object())
    with pytest.raises(TypeError):
        encode({1: 'non-string'})


@pytest.mark.integration
@pytest.mark.skipif(not PYTHON.is_file(), reason='Pinned backend required')
def test_fresh_interpreter_excludes_ambient_python_paths_and_reaps(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    implementation = source / 'component.py'
    implementation.write_text('''import os,sys
from pathlib import Path

def run(output: str):
    target = Path(output)
    target.write_text('real child output')
    return {'pid': os.getpid(), 'path': sys.path, 'version': sys.version_info[:2]}, target
''')
    reference = {'path': 'component.py', 'sha256': hashlib.sha256(implementation.read_bytes()).hexdigest()}
    spec = {'job_dir': str(tmp_path / 'job'), 'node_id': 'isolated', 'parameters': {},
        'component_node': {'implementation_ref': {**reference, 'module': 'owned_source', 'entrypoint': 'run'}},
        'source_root': str(source), 'trusted_code_roots': [str(source)], 'source': {'files': [reference]}}
    # Use the exact virtualenv interpreter path, without resolving its symlink.
    monkeypatch.setattr(sys, 'executable', str(PYTHON))
    monkeypatch.setenv('PYTHONPATH', '/poison/import/path')
    monkeypatch.setenv('PYTHONHOME', '/poison/python/home')
    result, path = execute('python', {'output': str(tmp_path / 'result')}, spec)
    assert result['pid'] != os.getpid()
    assert '/poison/import/path' not in result['path']
    assert path.read_text() == 'real child output'
    records = list((tmp_path / 'job/processes').glob('*/owner.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record['reaped'] and record['exit_code'] == 0
    assert not records[0].with_name('request.json').exists()
    assert not records[0].with_name('response.json').exists()


def test_cancel_publication_is_first_writer_wins(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from modelbuilder.backend.process_execution import publish_cancellation
    first = {'reason': 'user_requested'}
    publish_cancellation(tmp_path, first)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: publish_cancellation(tmp_path, {'reason': 'later', 'index': index}), range(40)))
    assert json.loads((tmp_path / 'cancel.json').read_text()) == first
    assert not list(tmp_path.glob('.cancel*'))


@pytest.mark.integration
@pytest.mark.skipif(not PYTHON.is_file(), reason='Pinned backend required')
def test_successful_trusted_body_cannot_leave_spawned_children(tmp_path, monkeypatch):
    import psutil
    implementation = tmp_path / 'component.py'
    implementation.write_text('''import subprocess,sys,time

def run():
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    return {'pid': process.pid}
''')
    reference = {'path': implementation.name, 'sha256': hashlib.sha256(implementation.read_bytes()).hexdigest()}
    spec = {'job_dir': str(tmp_path / 'job'), 'node_id': 'spawn', 'parameters': {},
        'component_node': {'implementation_ref': {**reference, 'module': 'owned_source', 'entrypoint': 'run'}},
        'source_root': str(tmp_path), 'trusted_code_roots': [str(tmp_path)], 'source': {'files': [reference]}}
    monkeypatch.setattr(sys, 'executable', str(PYTHON))
    result = execute('python', {}, spec)
    assert not psutil.pid_exists(result['pid'])
    record = json.loads(next((tmp_path / 'job/processes').glob('*/owner.json')).read_text())
    assert record['descendant_cleanup']['observed_pids'] == [result['pid']]
    assert record['descendant_cleanup']['remaining_pids'] == []


@pytest.mark.integration
@pytest.mark.skipif(not PYTHON.is_file(), reason='Pinned backend required')
def test_ownership_write_failure_still_reaps_child(tmp_path, monkeypatch):
    from modelbuilder.backend import process_execution
    original = process_execution.write_json
    failed = False
    def write(path, value):
        nonlocal failed
        if path.name == 'owner.json' and not failed:
            failed = True
            raise OSError('simulated ownership journal failure')
        return original(path, value)
    monkeypatch.setattr(process_execution, 'write_json', write)
    monkeypatch.setattr(sys, 'executable', str(PYTHON))
    with pytest.raises(OSError, match='simulated ownership'):
        execute('python', {}, {'job_dir': str(tmp_path), 'node_id': 'ownership'})
    record = json.loads(next((tmp_path / 'processes').glob('*/owner.json')).read_text())
    assert record['reaped'] and record['direct_child_reaped']
    assert record['exit_code'] is not None
    assert json.loads((tmp_path / 'cancel.json').read_text())['reason'] == 'stage_process_cleanup_unproven'


def test_adapter_cancel_preserves_existing_deadline_identity(tmp_path, monkeypatch):
    from modelbuilder.backend import ZenMLBackend
    from modelbuilder.backend.common import write_json
    from modelbuilder.backend.process_execution import publish_cancellation
    backend = ZenMLBackend(tmp_path / 'state')
    monkeypatch.setattr(backend, '_require_capability', lambda: None)
    states = iter([{'status': 'running', 'worker_alive': True},
                   {'status': 'cancelled', 'worker_alive': False}])
    monkeypatch.setattr(backend, '_execution_status', lambda job, request: next(states))
    job = tmp_path / 'job'
    reason = {'requested_at': 'original', 'reason': 'node_timeout_exceeded',
              'node_id': 'left', 'invocation_id': 'left', 'token': 'original-attempt'}
    publish_cancellation(job, reason)
    write_json(job / 'state.json', {'worker_reaped': True})
    result = backend.cancel(job, {})
    assert result['cancellation_acknowledged']
    assert json.loads((job / 'cancel.json').read_text()) == reason


def test_preexisting_cancellation_does_not_spawn_attempt(tmp_path, monkeypatch):
    from modelbuilder.backend import process_execution
    process_execution.publish_cancellation(tmp_path, {'reason': 'user_requested'})
    def forbidden(*args, **kwargs):
        raise AssertionError('Cancelled attempt must not spawn a new interpreter')
    monkeypatch.setattr(process_execution.subprocess, 'Popen', forbidden)
    with pytest.raises(InterruptedError, match='before trusted stage process startup'):
        execute('python', {}, {'job_dir': str(tmp_path), 'node_id': 'queued'})
    assert not (tmp_path / 'processes').exists()
