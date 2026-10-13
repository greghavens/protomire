"""Moved real SDK fixture, not a standalone CPython/installer acceptance claim.

Set MODELBUILDER_TEST_HUB_PYTHON to a locally installed hashlocked worker profile.
Only the native-CPython self-containment gate is mocked; actual dependency closure
and worker subprocess launch use the moved fixture and perform no network I/O.
"""
import os
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'distribution'))
import single_app as packaging  # noqa: E402


def test_actual_sdk_worker_and_locked_closure_launch_after_fixture_move(tmp_path, monkeypatch):
    configured = os.environ.get('MODELBUILDER_TEST_HUB_PYTHON')
    if not configured:
        pytest.skip('Explicit locally installed hashlocked Hub worker fixture required')
    original = tmp_path / 'original-payload'
    python_root = Path(configured).parent.parent
    shutil.copytree(python_root, original / 'hub', symlinks=False)
    lock = packaging.lock_path(ROOT, 'hub', 'linux-x64')
    (original / 'profiles').mkdir()
    shutil.copyfile(lock, original / 'profiles' / lock.name)
    shutil.copyfile(ROOT / 'src/modelbuilder/hub_sdk_worker.py', original / 'worker.py')
    moved = tmp_path / 'moved payload with spaces'
    original.rename(moved)
    manifest = {'hub_python':'hub/bin/python','hub_profile':'profiles/'+lock.name,'python_version':'3.12.14'}
    def native_gate(root, executable, modules, version):
        assert root == moved and not original.exists()
        return {'native_python_gate':'explicit_external_fixture_not_acceptance'}
    monkeypatch.setattr(packaging, 'probe_python', native_gate)
    result = packaging.probe_hub(moved, manifest, 'worker.py')
    assert result['offline_worker'] == 'passed'
    assert result['locked_packages']['keyring'] == '25.6.0'
    assert len(result['locked_packages']) == 23
    # A changed locked version fails even when all modules remain importable.
    profile = moved / manifest['hub_profile']
    profile.write_text(profile.read_text().replace('requests==2.34.2', 'requests==0.0.0'))
    with pytest.raises(ValueError, match='closure differs'):
        packaging.probe_hub(moved, manifest, 'worker.py')
