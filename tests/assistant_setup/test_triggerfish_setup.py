"""Inactive Triggerfish project preparation. No vendor executable is invoked."""
import json
import shutil
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'distribution'))
import assistant_project as project  # noqa: E402
import assistant_setup as setup  # noqa: E402


@pytest.fixture
def command(tmp_path):
    entrypoint = tmp_path / 'managed_core.py'
    shutil.copyfile(ROOT / "distribution/managed_core.py", entrypoint)
    return {'command': sys.executable, 'args': ['-I', str(entrypoint), 'mcp', '--runtime-root', str(tmp_path),
                                               '--state-dir', str(tmp_path / 'private'), '--project-id', 'p']}


def prepare(tmp_path, command):
    return project.preview_project('triggerfish', tmp_path, 'p', command=command)


def test_inactive_bundle_exact_command_supported_paths_no_secrets_and_rollback(tmp_path, command, monkeypatch):
    monkeypatch.setenv('MODELBUILDER_TOKEN', 'never-service-secret')
    monkeypatch.setenv('TRIGGERFISH_DATA_DIR', str(tmp_path / 'existing-global'))
    global_dir = tmp_path / 'existing-global'
    global_dir.mkdir()
    global_config = global_dir / 'triggerfish.yaml'
    global_config.write_text('private: never-existing-secret\n')
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    report, files = prepare(tmp_path, command)
    assert {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    plan = report['plan']
    assert plan['capability'] == 'inactive_compatibility_bundle'
    assert plan['vendor_acceptance'] == 'UNVERIFIED'
    assert plan['entry'] == {**command, 'args': [*command['args'], '--mcp-profile', 'triggerfish'], 'enabled': False}
    assert 'classification' not in plan['entry'] and 'env' not in plan['entry']
    assert all(f['path'].startswith('.modelbuilder/triggerfish/') for f in files)
    assert not any(Path(f['path']).name == 'triggerfish.yaml' for f in files)
    assert 'never-' not in json.dumps([report, files])
    with pytest.raises(setup.SetupError):
        project.apply_project(report, files, '0' * 64)
    installed = project.apply_project(report, files, report['approval'])
    assert installed['status'] == 'prepared' and installed['transport'] == 'UNVERIFIED'
    assert global_config.read_text() == 'private: never-existing-secret\n'
    fragment = json.loads((tmp_path / '.modelbuilder/triggerfish/mcp.fragment.yaml').read_text())
    assert fragment == {'mcp': {'servers': {'modelbuilder': plan['entry']}}}
    assert project.apply_project(report, files, report['approval'])['status'] == 'unchanged'
    repeat, _ = prepare(tmp_path, command)
    assert repeat['plan']['change'] == 'unchanged'
    # Reuse the shared source-scoped reference content, without rewriting schemas.
    skill = tmp_path / '.modelbuilder/triggerfish/skill-template/modelbuilder-project'
    assert (skill / 'references/operation-catalog.json').read_bytes() == (ROOT / 'distribution/assistant_templates/v1/operation-catalog.json').read_bytes()
    assert 'arguments_json' in (skill / 'SKILL.md').read_text()
    rb, changes = project.preview_rollback('triggerfish', tmp_path)
    project.rollback_project(rb, changes, rb['approval'])
    assert global_config.read_text() == 'private: never-existing-secret\n'
    assert not (skill / 'SKILL.md').exists()
    assert not list(tmp_path.rglob('*.lock'))


def test_preparation_conflicts_project_retarget_and_owner_edits_fail_closed(tmp_path, command):
    report, files = prepare(tmp_path, command)
    stale = tmp_path / files[0]['path']
    stale.parent.mkdir(parents=True)
    stale.write_text('owner content')
    with pytest.raises(setup.SetupError, match='changed'):
        project.apply_project(report, files, report['approval'])
    assert stale.read_text() == 'owner content'
    with pytest.raises(setup.SetupError) as conflict:
        prepare(tmp_path, command)
    assert conflict.value.code == 'config_conflict'
    stale.unlink()
    report, files = prepare(tmp_path, command)
    project.apply_project(report, files, report['approval'])
    changed = {**command, 'args': [*command['args'][:-1], 'another']}
    with pytest.raises(setup.SetupError) as retarget:
        project.preview_project('triggerfish', tmp_path, 'another', command=changed)
    assert retarget.value.code == 'receipt_conflict'
    stale.write_text('owner edits')
    with pytest.raises(setup.SetupError) as edited:
        project.preview_rollback('triggerfish', tmp_path)
    assert edited.value.code == 'managed_file_changed' and stale.read_text() == 'owner edits'


def test_supported_allowlist_is_reported_without_workaround(tmp_path, command):
    python = tmp_path / 'python3.12'
    python.symlink_to(sys.executable)
    report, _ = prepare(tmp_path, {**command, 'command': str(python)})
    assert report['plan']['command_allowlist'] == 'blocked_basename'
    assert any('allowedCommands' in item for item in report['plan']['warnings'])
    assert report['plan']['entry']['command'] == str(python)
    python = tmp_path / 'python'
    python.symlink_to(sys.executable)
    report, _ = prepare(tmp_path, {**command, 'command': str(python)})
    assert report['plan']['command_allowlist'] == 'compatible_basename'
    assert report['plan']['activation'].startswith('blocked')


def test_triggerfish_requires_managed_command_and_rejects_symlink(tmp_path, command):
    with pytest.raises(setup.SetupError) as missing:
        project.preview_project('triggerfish', tmp_path, 'p', python=sys.executable, service_url='http://127.0.0.1:8765')
    assert missing.value.code == 'managed_runtime_required'
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / '.modelbuilder').symlink_to(outside, target_is_directory=True)
    with pytest.raises(setup.SetupError):
        prepare(tmp_path, command)
    assert list(outside.iterdir()) == []


def test_existing_vendor_data_root_does_not_gain_autoloaded_skills(tmp_path, command, monkeypatch):
    vendor_root = tmp_path / '.modelbuilder/triggerfish'
    vendor_root.mkdir(parents=True)
    (vendor_root / 'triggerfish.yaml').write_text('owner provider config; never copy')
    monkeypatch.setenv('TRIGGERFISH_DATA_DIR', str(vendor_root))
    report, files = prepare(tmp_path, command)
    assert 'owner provider config' not in json.dumps([report, files])
    project.apply_project(report, files, report['approval'])
    assert not (vendor_root / 'skills').exists()
    assert not (vendor_root / 'workspaces').exists()
    assert (vendor_root / 'triggerfish.yaml').read_text() == 'owner provider config; never copy'
    assert (vendor_root / 'skill-template/modelbuilder-project/SKILL.md').is_file()


def test_current_native_bytecode_suppressed_vector_is_preserved(tmp_path, command):
    current = {**command, 'args': [command['args'][0], '-B', *command['args'][1:]]}
    report, _ = prepare(tmp_path, current)
    assert report['plan']['entry']['args'] == [*current['args'], '--mcp-profile', 'triggerfish']
    assert not (tmp_path / '__pycache__').exists()
    assert not (tmp_path / 'private').exists()


def test_stale_runtime_fails_before_preparation_without_leases_or_secret_environment(tmp_path, command):
    entrypoint = Path(command['args'][1])
    entrypoint.write_text("import argparse, os\nassert 'MODELBUILDER_TOKEN' not in os.environ\nargparse.ArgumentParser().parse_args()\n")
    with pytest.raises(setup.SetupError) as stale:
        prepare(tmp_path, command)
    assert stale.value.code == 'managed_runtime_stale'
    assert not (tmp_path / '.modelbuilder').exists()
    assert not (tmp_path / 'private').exists()


def test_stale_check_never_executes_caller_supplied_code_or_accepts_comment_marker(tmp_path, command):
    entrypoint = Path(command['args'][1])
    marker = tmp_path / 'executed'
    entrypoint.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('must not run')\n# parser.add_argument('--mcp-profile', choices=['native', 'triggerfish'])\n")
    with pytest.raises(setup.SetupError) as stale:
        prepare(tmp_path, command)
    assert stale.value.code == 'managed_runtime_stale'
    assert not marker.exists() and not (tmp_path / '.modelbuilder').exists()
    # A declaration only permits an inert bundle, never execution or trust.
    entrypoint.write_text(entrypoint.read_text() + "parser.add_argument('--mcp-profile', choices=['native', 'triggerfish'])\n")
    report, _ = prepare(tmp_path, command)
    assert report['plan']['capability'] == 'inactive_compatibility_bundle'
    assert not marker.exists()


@pytest.mark.parametrize('flag', ['-c', '-m', '-X', '-s'])
def test_extra_interpreter_flags_are_not_accepted(tmp_path, command, flag):
    changed = {**command, 'args': [command['args'][0], flag, *command['args'][1:]]}
    with pytest.raises(setup.SetupError) as invalid:
        prepare(tmp_path, changed)
    assert invalid.value.code == 'invalid_managed_command'
