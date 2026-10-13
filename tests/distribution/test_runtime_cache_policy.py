"""Managed code inventories stay exact; mutable caches belong to private state."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'distribution'))
import managed_core as core  # noqa: E402
from modelbuilder.backend import ZenMLBackend  # noqa: E402
from modelbuilder.backend.process_execution import isolated_command  # noqa: E402


def test_strict_runtime_inventory_never_ignores_new_bytecode_or_arbitrary_files(tmp_path):
    payload=tmp_path/'payload'
    payload.mkdir()
    (payload/'verified.py').write_text('VALUE=1\n')
    manifest={'files':{'verified.py':hashlib.sha256((payload/'verified.py').read_bytes()).hexdigest()}}
    core.verify_runtime(payload,manifest)
    for name in ['__pycache__/verified.cpython-312.pyc','unexpected.py','extra.txt']:
        path=payload/name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'inert unsealed fixture')
        with pytest.raises(core.CoreError,match='unlisted'):
            core.verify_runtime(payload,manifest)
        path.unlink()


def test_private_cache_locations_do_not_retarget_os_keyring_home(tmp_path,monkeypatch):
    state=core.private_directory(tmp_path/'state')
    monkeypatch.setenv('HOME','/fixture/os-user')
    env=core.runtime_environment(state)
    assert env['HOME']=='/fixture/os-user'
    assert env['PYTHONDONTWRITEBYTECODE']=='1'
    for field in ['XDG_CACHE_HOME','HF_HOME','TORCH_HOME','TMPDIR','TEMP','TMP']:
        assert Path(env[field]).is_relative_to(state)
    assert Path(env['TMPDIR']).is_dir()
    backend=ZenMLBackend(state,python_executable=sys.executable)
    env=backend._env()
    for field in ['HOME','XDG_CACHE_HOME','HF_HOME','TORCH_HOME','TMPDIR']:
        assert Path(env[field]).is_relative_to(state/'backend')
    assert env['PYTHONDONTWRITEBYTECODE']=='1'


def test_isolated_owned_child_does_not_create_bytecode_without_environment_fallback(tmp_path):
    module=tmp_path/'inert_module.py'
    module.write_text('VALUE=42\n')
    command=isolated_command('unused')
    assert command[:4]==[sys.executable,'-I','-B','-c']
    code='import sys,json;sys.path.insert(0,sys.argv[1]);import inert_module;print(json.dumps({"disabled":sys.dont_write_bytecode,"value":inert_module.VALUE}))'
    result=subprocess.run([*command[:3],'-c',code,str(tmp_path)],capture_output=True,text=True,check=True,env={})
    assert json.loads(result.stdout)=={'disabled':True,'value':42}
    assert not (tmp_path/'__pycache__').exists()


@pytest.mark.parametrize('relative',[
    'distribution/managed_core.py','src/modelbuilder/backend/adapter.py','src/modelbuilder/backend/supervisor.py',
    'src/modelbuilder/backend/process_execution.py','src/modelbuilder/backend/components/mlx_bridge.py',
    'src/modelbuilder/backend/components/mlx_components.py','src/modelbuilder/backend/components/transformers_training.py'])
def test_owned_interpreter_launch_vectors_explicitly_disable_bytecode(relative):
    tree=ast.parse((ROOT/relative).read_text())
    found=0
    for node in ast.walk(tree):
        if not isinstance(node,ast.List):
            continue
        strings=[item.value for item in node.elts if isinstance(item,ast.Constant) and isinstance(item.value,str)]
        if '-I' in strings or '-P' in strings or ('-m' in strings and any(text.startswith('modelbuilder.') for text in strings)):
            assert '-B' in strings,(relative,strings)
            found+=1
    assert found,relative
