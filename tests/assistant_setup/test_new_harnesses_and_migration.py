"""Real local setup/rollback and read-only migration inventory; no vendor inference."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'distribution'))
import assistant_project as project  # noqa: E402
import assistant_setup as setup  # noqa: E402
from tests.assistant_setup.test_project_setup import command as registered_command  # noqa: E402

command = registered_command
SKILL = ROOT/'distribution/assistant_templates/v1/skills/modelbuilder-migrate-repo'
spec = importlib.util.spec_from_file_location('migration_inventory', SKILL/'scripts/inventory_repo.py')
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


@pytest.mark.parametrize('client', ['opencode', 'kilocode'])
def test_json_harness_setup_preserves_other_settings_and_rolls_back(tmp_path, command, client):
    config = tmp_path/project.CONFIG_PATHS[client]
    config.parent.mkdir(parents=True, exist_ok=True)
    original = {'model': 'owner-model', 'permission': {'bash': 'ask'}, 'mcp': {'other': {'type': 'local', 'command': ['other']}}}
    config.write_text(json.dumps(original))
    report, files = project.preview_project(client, tmp_path, 'project-1', command=command)
    assert json.loads(config.read_text()) == original
    project.apply_project(report, files, report['approval'])
    data = json.loads(config.read_text())
    assert data['model'] == original['model'] and data['permission'] == original['permission']
    assert data['mcp']['other'] == original['mcp']['other']
    assert data['mcp']['modelbuilder'] == {'type': 'local', 'command': [command['command'], *command['args']], 'enabled': True, 'timeout': 30000}
    root = tmp_path/project.SKILL_ROOTS[client]
    assert (root/'modelbuilder-project/SKILL.md').is_file()
    assert (root/'modelbuilder-migrate-repo/scripts/inventory_repo.py').read_bytes() == (SKILL/'scripts/inventory_repo.py').read_bytes()
    repeat, _ = project.preview_project(client, tmp_path, 'project-1', command=command)
    assert repeat['plan']['change'] == 'unchanged'
    data['new_owner_setting'] = True
    config.write_text(json.dumps(data))
    rb, changes = project.preview_rollback(client, tmp_path)
    project.rollback_project(rb, changes, rb['approval'])
    assert json.loads(config.read_text()) == {**original, 'new_owner_setting': True}
    assert not (root/'modelbuilder-migrate-repo/SKILL.md').exists()


@pytest.mark.parametrize('client,path', [('opencode','opencode.jsonc'), ('kilocode','kilo.json'), ('kilocode','.kilocode/mcp.json')])
def test_alternate_configs_are_not_silently_shadowed(tmp_path, command, client, path):
    existing = tmp_path/path
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text('// owner config with comments')
    with pytest.raises(setup.SetupError, match='alternate or legacy'):
        project.preview_project(client, tmp_path, 'project-1', command=command)
    assert existing.read_text() == '// owner config with comments'
    assert not (tmp_path/project.CONFIG_PATHS[client]).exists()


@pytest.mark.parametrize('client', ['codex','claude','opencode','kilocode'])
def test_all_managed_harnesses_ship_same_migration_skill(client):
    files = project.template_files(client)
    prefix = project.SKILL_ROOTS[client]+'/modelbuilder-migrate-repo/'
    assert files[prefix+'SKILL.md'] == (SKILL/'SKILL.md').read_bytes()
    assert files[prefix+'references/project-mapping.md'] == (SKILL/'references/project-mapping.md').read_bytes()


def test_inventory_never_imports_repo_code_and_reports_omissions(tmp_path):
    (tmp_path/'train.py').write_text("raise RuntimeError('must not run')")
    (tmp_path/'tokenizer.json').write_text('{}')
    (tmp_path/'.env').write_text('secret')
    (tmp_path/'weights.safetensors').write_bytes(b'x'*32)
    (tmp_path/'alias.py').symlink_to(tmp_path/'train.py')
    before = {p.name:p.read_bytes() for p in tmp_path.iterdir() if not p.is_symlink()}
    report = scanner.inventory(tmp_path,max_hash_bytes=31)
    entries = {item['path']:item for item in report['files']}
    assert not report['execution_allowed'] and not report['roles_verified']
    assert '.env' not in entries and 'alias.py' not in entries
    assert 'tokenizer.json' in entries
    assert entries['weights.safetensors']['sha256'] is None
    assert report['skipped_counts']['sensitive'] == 1
    assert report['skipped_counts']['symlink_or_special'] == 1
    assert {p.name:p.read_bytes() for p in tmp_path.iterdir() if not p.is_symlink()} == before
    assert scanner.inventory(tmp_path,max_files=1)['truncated']
    assert scanner.inventory(tmp_path,max_hash_bytes=31) == deepcopy(report)


@pytest.mark.parametrize('client', ['claude','codex','opencode','kilocode'])
def test_flatpak_config_launcher_works_inside_and_on_host(tmp_path, command, monkeypatch, client):
    import os
    import subprocess
    app_id = 'io.github.greghavens.Modelbuilder'
    packaged_profile = tmp_path/'flatpak-profile.json'
    packaged_profile.write_text(json.dumps({'app_id': app_id}))
    monkeypatch.setattr(project, 'FLATPAK_PROFILE', packaged_profile)
    monkeypatch.setenv('FLATPAK_ID', app_id)
    # An inert native entrypoint records its vector; it does not start any service.
    Path(command['args'][1]).write_text('import json,sys; print(json.dumps(sys.argv[1:]))\n')
    workspace = tmp_path/'repo with spaces'
    workspace.mkdir()
    report, files = project.preview_project(client, workspace, 'project-1', command=command)
    project.apply_project(report, files, report['approval'])
    entry = report['plan']['entry']
    vector = entry['command'] if client in project.JSON_CLIENTS else [entry['command'],*entry['args']]
    assert vector[0] == '/bin/sh'
    native = subprocess.run(vector,capture_output=True,text=True,check=True)
    assert json.loads(native.stdout) == command['args'][2:]
    binary = tmp_path/'bin'
    binary.mkdir()
    flatpak = binary/'flatpak'
    flatpak.write_text('#!'+sys.executable+'\nimport json,sys; print(json.dumps(sys.argv[1:]))\n')
    flatpak.chmod(0o755)
    env={key:value for key,value in os.environ.items() if key != 'FLATPAK_ID'}
    env['PATH']=str(binary)+os.pathsep+env.get('PATH','')
    host=subprocess.run(vector,env=env,capture_output=True,text=True,check=True)
    assert json.loads(host.stdout) == ['run','--command=modelbuilder-mcp',app_id,'--state-dir',str(tmp_path/'private-state'),'--project-id','project-1']
    repeat, _=project.preview_project(client,workspace,'project-1',command=command)
    assert repeat['plan']['change']=='unchanged'
    rb, changes=project.preview_rollback(client,workspace)
    project.rollback_project(rb,changes,rb['approval'])
    assert not (workspace/f'.modelbuilder/assistant-setup/{client}-mcp.sh').exists()


def test_flatpak_identity_mismatch_never_writes_config(tmp_path,command,monkeypatch):
    path=tmp_path/'profile.json'
    path.write_text(json.dumps({'app_id':'org.example.Wrong'}))
    monkeypatch.setenv('FLATPAK_ID','org.example.Expected')
    monkeypatch.setattr(project,'FLATPAK_PROFILE',path)
    with pytest.raises(setup.SetupError,match='identity'):
        project.preview_project('claude',tmp_path,'project-1',command=command)
    assert not (tmp_path/'.mcp.json').exists()
