"""Host-independent selectors and bootstrap contracts; no foreign-platform runtime claims."""
import json
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import bootstrap  # noqa: E402
from scripts.dependency_profiles import BACKEND_TARGETS, UV_PLATFORMS, backend_reason, lock_path, venv_python  # noqa: E402


@pytest.mark.parametrize('target', sorted(UV_PLATFORMS) + ['linux-armhf'])
def test_service_profile_is_hash_locked(target):
    text = lock_path(ROOT, 'service', target).read_text()
    pins = re.findall(r'^([\w-]+)==([^\s]+)', text, flags=re.M)
    assert len(pins) >= 70
    assert ('psutil', '7.2.2') in pins
    assert text.count('--hash=sha256:') >= len(pins)


@pytest.mark.parametrize('target', sorted(BACKEND_TARGETS))
def test_backend_profile_retains_pins(target):
    text = lock_path(ROOT, 'backend', target).read_text()
    assert 'zenml==0.97.0' in text
    assert 'transformers==4.57.1' in text
    assert f'torch==2.9.0{"+cpu" if target.startswith("linux-") else " "}' in text
    assert '--hash=sha256:' in text


@pytest.mark.parametrize('target', ['win32-x64', 'win32-arm64', 'darwin-x64', 'linux-armhf'])
def test_unsupported_backend_has_specific_reason(target):
    assert backend_reason(target)
    with pytest.raises(ValueError, match='ZenML|PyTorch'):
        lock_path(ROOT, 'backend', target)
    assert lock_path(ROOT, 'service', target).is_file()


def test_windows_venv_path_with_spaces():
    assert venv_python(Path('a directory'), 'win32-arm64') == Path('a directory/Scripts/python.exe')
    assert venv_python(Path('a directory'), 'darwin-arm64') == Path('a directory/bin/python')


def test_windows_backend_failure_does_not_skip_service(monkeypatch, capsys, tmp_path):
    calls = []
    monkeypatch.setattr(bootstrap, 'detect_target', lambda: 'win32-x64')
    monkeypatch.setattr(bootstrap, 'run', lambda command, **kwargs: calls.append(command))
    code = bootstrap.main(['--backend', '--skip-frontend', '--service-venv', str(tmp_path / 'service'),
                           '--backend-venv', str(tmp_path / 'backend')])
    assert code == 2
    assert len(calls) == 4
    assert '--require-hashes' in calls[1]
    assert 'Scripts/python.exe' in str(calls[1][0])
    assert 'ZenML' in capsys.readouterr().err


def test_plan_does_not_install(monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, 'run', lambda *a, **kw: pytest.fail('plan executed a subprocess'))
    assert bootstrap.main(['--plan']) == 0
    assert json.loads(capsys.readouterr().out)['python'] == '3.12.14'


def test_shared_venv_is_rejected(tmp_path):
    with pytest.raises(ValueError, match='separate'):
        bootstrap.main(['--plan', '--service-venv', str(tmp_path), '--backend-venv', str(tmp_path)])
