"""Packaging command contracts, not a real Flatpak build or installation claim."""
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'distribution'))
import flatpak_package as pack  # noqa: E402
from tests.distribution.test_single_app import payload as registered_payload, app_fixture
import single_app

payload = registered_payload


def profile():
    return {'app_id': 'io.github.greghavens.Modelbuilder', 'branch': 'development',
            'runtime': 'org.freedesktop.Platform', 'sdk': 'org.freedesktop.Sdk',
            'base': 'org.electronjs.Electron2.BaseApp', 'runtime_version': '25.08',
            'commits': {key: str(index) * 64 for index, key in enumerate(['runtime', 'sdk', 'base'], 1)}}


@pytest.mark.parametrize('change', [None, {}, {'app_id': '../escape'}, {'runtime_version': '--override'}, {'commits': {}}])
def test_invalid_profile_rejected(change):
    value = change if change is None or change == {} else {**profile(), **change}
    with pytest.raises(ValueError):
        pack.validate_profile(value)


def test_missing_tool_is_explicit_no_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(pack.shutil, 'which', lambda _: None)
    with pytest.raises(ValueError, match='not installed'):
        pack.package_flatpak(tmp_path, tmp_path, 'linux-x64', tmp_path/'app', profile())
    assert not (tmp_path/'app.flatpak').exists()


def test_staged_mandatory_core_bundle_and_permissions(payload, tmp_path, monkeypatch):
    monkeypatch.setattr(single_app, 'detect_target', lambda: 'linux-x64')
    source = app_fixture(tmp_path/'app', 'linux-x64')
    staged = tmp_path/'staged'
    single_app.stage(source, payload, staged, 'linux-x64')
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == 'info':
            key = next(key for key in ['runtime', 'sdk', 'base'] if command[-1].startswith(profile()[key]+'//'))
            return SimpleNamespace(stdout=profile()['commits'][key]+'\n')
        if command[1] == 'build-init':
            files = Path(command[5])/'files'
            (files/'bin').mkdir(parents=True)
            (files/'bin/zypak-wrapper').write_text('fixture')
        if command[1] == 'build-bundle':
            Path(command[4]).write_bytes(b'unit-test-bundle-not-an-installable-flatpak')
        return SimpleNamespace(stdout='')
    monkeypatch.setattr(pack.subprocess, 'run', run)
    monkeypatch.setattr(pack.shutil, 'which', lambda _: '/usr/bin/flatpak')
    result = single_app.package(staged, tmp_path/'output', 'linux-x64', flatpak_profile=profile())
    assert result['format'] == 'flatpak' and result['native_acceptance'] == 'not_run'
    assert not result['signed'] and not result['published']
    core = tmp_path/'output/flatpak-build/files/lib/modelbuilder/resources/app/extensions/modelbuilder-workbench/resources/core'
    assert (core/'runtime.json').is_file() and (core/'managed_core.py').is_file()
    launcher = (tmp_path/'output/flatpak-build/files/bin/modelbuilder').read_text()
    assert 'zypak-wrapper' in launcher and '--no-sandbox' not in launcher
    assert not any('install' in command[1] or 'remote-add' in command for command in calls)
    assert '--filesystem=host' not in pack.PERMISSIONS and '--device=all' not in pack.PERMISSIONS
    assert '--filesystem=home' in pack.PERMISSIONS  # Explicit IDE project access, documented.


def test_runtime_commit_drift_blocks_before_build(tmp_path, monkeypatch):
    calls=[]
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout='f'*64)
    monkeypatch.setattr(pack.subprocess, 'run', run)
    with pytest.raises(ValueError, match='commit lock'):
        pack.package_flatpak(tmp_path, tmp_path, 'linux-x64', tmp_path/'app', deepcopy(profile()), tool='/usr/bin/flatpak')
    assert all(command[1] == 'info' for command in calls)
