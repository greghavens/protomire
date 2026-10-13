"""Abnormal native body exit cancels the run and reaps its orphaned work."""
import importlib.util
import json
from pathlib import Path
import sys

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.integration, pytest.mark.skipif(sys.platform != 'linux', reason='Linux subreaper acceptance')]


def test_hard_exit_aborts_sibling_and_supervisor_reaps_orphan(tmp_path, monkeypatch):
    source = ROOT / 'tests/concurrency/test_native_parallel.py'
    spec = importlib.util.spec_from_file_location('parallel_hard_crash_harness', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'SOURCE', module.SOURCE.replace(
        '        time.sleep(delay)',
        '        if name == "left": os._exit(23)\n        time.sleep(delay)'))
    backend, payload = module.setup(tmp_path, roots=2, torch=False, delay=60, spawn_child=True)
    result, request = module.execute(backend, payload)
    assert result['status'] == 'cancelled' and not result['worker_alive'], result
    job = backend.root / 'jobs' / request['operation_id']
    cancellation = json.loads((job / 'cancel.json').read_text())
    assert cancellation['reason'] == 'stage_process_cleanup_unproven'
    children = [record['child_pid'] for record in module.events(tmp_path) if record['kind'] == 'child']
    assert len(children) == 2 and all(not psutil.pid_exists(pid) for pid in children), children
    owners = [json.loads(path.read_text()) for path in (job / 'processes').glob('*/owner.json')]
    assert all(owner['direct_child_reaped'] for owner in owners)
    assert next(owner for owner in owners if owner['node_id'] == 'left')['exit_code'] == 23
    state = json.loads((job / 'state.json').read_text())
    assert state['supervisor_subreaper'] is True
    assert set(state['adopted_children_reaped']) & set(children)
    assert state['process_group_terminated'] is True
