"""Packaging command contracts, not a real Flatpak build or installation claim."""
from copy import deepcopy
import json
import base64
import hashlib
import os
from pathlib import Path
import subprocess
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
            (files/'bin/zypak-wrapper.sh').write_text('fixture')
            (files/'bin/zypak-wrapper.sh').chmod(0o755)
            (files/'bin/zypak-wrapper').symlink_to('/app/bin/zypak-wrapper.sh')
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
    desktop = (tmp_path/'output/flatpak-build/files/share/applications/io.github.greghavens.Modelbuilder.desktop').read_text()
    assert 'Name=Protomire\n' in desktop
    assert 'Exec=modelbuilder %F\n' in desktop
    assert 'Icon=io.github.greghavens.Modelbuilder\n' in desktop
    assert 'StartupWMClass=io.github.greghavens.Modelbuilder\n' in desktop
    resources = core.parents[3]
    product = json.loads((resources/'product.json').read_text())
    assert product['nameShort'] == product['nameLong'] == 'Protomire'
    assert product['applicationName'] == 'modelbuilder-ide'
    native_package = json.loads((resources/'package.json').read_text())
    assert native_package['name'] == 'Modelbuilder'  # Native credential identity.
    assert native_package['desktopName'] == 'io.github.greghavens.Modelbuilder.desktop'
    assert (resources/'resources/linux/code.png').read_bytes() == (pack.ASSETS/'icons/light-tile/protomire-512.png').read_bytes()
    assert (resources/'out/media/protomire-icon.png').read_bytes() == (pack.ASSETS/'icons/light-tile/protomire-32.png').read_bytes()
    stylesheet = (resources/'out/vs/workbench/workbench.desktop.main.css').read_text()
    assert 'code-icon.svg' not in stylesheet
    assert stylesheet.count('url("../../media/protomire-icon.png")') == 5
    assert stylesheet.count('url("../../media/protomire-watermark.png")') == 4
    assert 'letterpress-dark.svg' not in stylesheet
    assert (resources/'out/media/protomire-watermark.png').read_bytes() == (pack.ASSETS/'icons/light-tile/protomire-512.png').read_bytes()
    assert product['checksums']['vs/workbench/workbench.desktop.main.css'] == base64.b64encode(hashlib.sha256(stylesheet.encode()).digest()).decode().rstrip('=')
    assert product['checksums']['untouched-fixture.js'] == 'preserve-this-integrity-receipt'
    assert (resources/'protomire-brand/InterTight-OFL.txt').read_bytes() == (pack.ASSETS/'fonts/OFL.txt').read_bytes()
    for size in (16, 24, 32, 48, 64, 96, 128, 192, 256, 512):
        exported = tmp_path/f'output/flatpak-build/files/share/icons/hicolor/{size}x{size}/apps/io.github.greghavens.Modelbuilder.png'
        assert exported.read_bytes() == (pack.ASSETS/f'icons/light-tile/protomire-{size}.png').read_bytes()
    assert not (tmp_path/'output/flatpak-build/files/share/icons/hicolor/1024x1024').exists()
    assert 'zypak-wrapper' in launcher and '--no-sandbox' not in launcher
    assert '--ozone-platform-hint=auto' in launcher
    assert '--password-store=gnome-libsecret' in launcher and '--password-store=basic' not in launcher
    assert any(command[1:] == ['build', str(tmp_path/'output/flatpak-build'), '/usr/bin/test', '-x', '/app/bin/zypak-wrapper'] for command in calls)
    assert not any('install' in command[1] or 'remote-add' in command for command in calls)
    assert '--filesystem=host' not in pack.PERMISSIONS and '--device=all' not in pack.PERMISSIONS
    assert '--filesystem=home' in pack.PERMISSIONS  # Explicit IDE project access, documented.


def test_workbench_branding_is_idempotent_and_rejects_unknown_or_mixed_css(tmp_path):
    from branding import apply_linux_branding
    source = app_fixture(tmp_path/'app', 'linux-x64')/'resources/app'
    apply_linux_branding(source, profile()['app_id'])
    css = source/'out/vs/workbench/workbench.desktop.main.css'
    first = css.read_bytes()
    apply_linux_branding(source, profile()['app_id'])
    assert css.read_bytes() == first
    css.write_text(css.read_text().replace('protomire-icon.png', 'code-icon.svg', 1))
    mixed = css.read_bytes()
    product_path = source/'product.json'
    product = json.loads(product_path.read_text())
    product['checksums']['vs/workbench/workbench.desktop.main.css'] = base64.b64encode(hashlib.sha256(mixed).digest()).decode().rstrip('=')
    product_path.write_text(json.dumps(product))
    with pytest.raises(ValueError, match='Unrecognized pinned workbench icon'):
        apply_linux_branding(source, profile()['app_id'])
    assert css.read_bytes() == mixed


@pytest.mark.parametrize('change', ['missing-map', 'missing-entry', 'changed-bytes'])
def test_branding_refuses_unverified_input_instead_of_disabling_integrity(tmp_path, change):
    from branding import apply_linux_branding
    source = app_fixture(tmp_path/'app', 'linux-x64')/'resources/app'
    product_path = source/'product.json'
    product = json.loads(product_path.read_text())
    if change == 'missing-map':
        product.pop('checksums')
    elif change == 'missing-entry':
        product['checksums'].pop('vs/workbench/workbench.desktop.main.css')
    else:
        css = source/'out/vs/workbench/workbench.desktop.main.css'
        css.write_text(css.read_text()+'\n.unreviewed { display:none; }\n')
    product_path.write_text(json.dumps(product))
    before = product_path.read_bytes()
    with pytest.raises(ValueError, match='input integrity checksum mismatch'):
        apply_linux_branding(source, profile()['app_id'])
    assert product_path.read_bytes() == before


def test_runtime_commit_drift_blocks_before_build(tmp_path, monkeypatch):
    calls=[]
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout='f'*64)
    monkeypatch.setattr(pack.subprocess, 'run', run)
    with pytest.raises(ValueError, match='commit lock'):
        pack.package_flatpak(tmp_path, tmp_path, 'linux-x64', tmp_path/'app', deepcopy(profile()), tool='/usr/bin/flatpak')
    assert all(command[1] == 'info' for command in calls)


@pytest.mark.parametrize('settings,args,expected', [
    ({}, [], ['--user-data-dir=/fixture home/.config/Modelbuilder']),
    ({'XDG_CONFIG_HOME': '/private config'}, ['project with spaces'], ['--user-data-dir=/private config/Modelbuilder', 'project with spaces']),
    ({}, ['--user-data-dir=/explicit profile'], ['--user-data-dir=/explicit profile']),
    ({}, ['--user-data-dir', '/explicit profile'], ['--user-data-dir', '/explicit profile']),
    ({'VSCODE_APPDATA': '/app data'}, ['--user-data-dir=/lower priority'], ['--user-data-dir=/app data/Modelbuilder']),
    ({'VSCODE_APPDATA': '/app data'}, ['--user-data-dir', '/lower priority', '--', 'folder'], ['--user-data-dir=/app data/Modelbuilder', '--', 'folder']),
    ({}, ['--', '--user-data-dir=folder-name'], ['--user-data-dir=/fixture home/.config/Modelbuilder', '--', '--user-data-dir=folder-name']),
    ({'VSCODE_PORTABLE': '/portable', 'VSCODE_APPDATA': '/app data'}, ['project'], ['project']),
])
def test_renamed_launcher_preserves_profile_precedence_and_argument_boundaries(tmp_path, settings, args, expected):
    source = (Path(pack.__file__).parent/'assets/flatpak-launcher.sh').read_text()
    # Substitute only the final Electron transport with an argv/env recorder;
    # execute the actual shell profile selection with normal argument parsing.
    recorder = tmp_path/'record.py'
    recorder.write_text('import json, os, sys; print(json.dumps({"args":sys.argv[1:], "appdata":os.environ.get("VSCODE_APPDATA")}))')
    import shlex
    command = 'exec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(recorder)) + ' "$@"'
    script = tmp_path/'launcher.sh'
    marker = 'exec /app/bin/zypak-wrapper '
    assert source.count(marker) == 1
    script.write_text(source[:source.index(marker)] + command + '\n')
    env = {k:v for k,v in os.environ.items() if k not in {'VSCODE_PORTABLE', 'VSCODE_APPDATA', 'XDG_CONFIG_HOME'}}
    env.update(HOME='/fixture home', **settings)
    result = json.loads(subprocess.check_output(['/bin/sh', str(script), *args], env=env, text=True))
    assert result['args'] == expected
    assert result['appdata'] == (settings.get('VSCODE_APPDATA') if settings.get('VSCODE_PORTABLE') else None)


def test_missing_executable_electron_wrapper_still_blocks_packaging(tmp_path, monkeypatch):
    def run(command, **kwargs):
        if command[1] == 'info':
            key = next(key for key in ['runtime', 'sdk', 'base'] if command[-1].startswith(profile()[key]+'//'))
            return SimpleNamespace(stdout=profile()['commits'][key]+'\n')
        if command[1] == 'build-init':
            (Path(command[5])/'files').mkdir(parents=True)
        if command[1] == 'build':
            raise pack.subprocess.CalledProcessError(1, command)
        return SimpleNamespace(stdout='')
    monkeypatch.setattr(pack.subprocess, 'run', run)
    with pytest.raises(ValueError, match='lacks zypak-wrapper'):
        pack.package_flatpak(tmp_path/'app', tmp_path/'output', 'linux-x64', tmp_path/'app', profile(), tool='/usr/bin/flatpak')
    assert not (tmp_path/'app.flatpak').exists()
