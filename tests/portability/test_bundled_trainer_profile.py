"""Explicit Trainer overlay is part of environment/source/replay identity."""
import copy
import hashlib
import json
import importlib.util
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from modelbuilder.backend.admission import BackendError, environment_profiles, verify_environment
from modelbuilder.backend.trainer_profile import TRAINER_PROFILE_PATH, TRAINER_PROFILE_SHA256
from modelbuilder.recipe import default_recipe, export_bundle, import_bundle, normalize_recipe, recipe_digest

ROOT=Path(__file__).resolve().parents[2]


def declared(root):
    recipe=default_recipe('project','recipe')
    backend='requirements/profiles/backend-linux-x64.lock'
    recipe['environment'].update(lock_file={'path':backend,'sha256':hashlib.sha256((root/backend).read_bytes()).hexdigest()},
                                 trainer_profile={'path':TRAINER_PROFILE_PATH,'sha256':TRAINER_PROFILE_SHA256})
    return recipe


def test_trainer_reference_exports_both_locks_and_survives_portable_import():
    assert hashlib.sha256((ROOT/TRAINER_PROFILE_PATH).read_bytes()).hexdigest()==TRAINER_PROFILE_SHA256
    recipe=declared(ROOT)
    bundle=export_bundle(recipe,source_root=ROOT)
    assert set(bundle['files'])=={recipe['environment']['lock_file']['path'],TRAINER_PROFILE_PATH}
    imported=import_bundle(bundle)
    assert imported['recipe']['environment']['trainer_profile']==recipe['environment']['trainer_profile']
    assert imported['recipe']['environment']['lock_file']==recipe['environment']['lock_file']
    changed=copy.deepcopy(recipe)
    changed['environment']['trainer_profile']['sha256']='0'*64
    assert recipe_digest(changed)!=recipe_digest(recipe)
    changed['environment']['trainer_profile']['path']='../escape.lock'
    with pytest.raises(ValueError):
        normalize_recipe(changed)


def test_admission_verifies_both_exact_locks_and_binds_receipt(monkeypatch):
    recipe=declared(ROOT)
    calls=[]
    class Result:
        returncode=0
        stdout=json.dumps({'verified':True,'python':'3.12.14','packages':{'accelerate':'1.11.0'}})
    def run(command,**options):
        calls.append(json.loads(options['input']))
        assert command[1:3]==['-I','-B']
        return Result()
    monkeypatch.setattr('modelbuilder.backend.admission.subprocess.run',run)
    receipt=verify_environment(SimpleNamespace(repository=ROOT,python=sys.executable),recipe)
    assert 'accelerate==1.11.0' in calls[0]['requirements']
    assert any(item.startswith('torch==') for item in calls[0]['requirements'])
    assert receipt['lock_profiles']=={field:recipe['environment'][field] for field in ['lock_file','trainer_profile']}
    for change in ['missing_base','wrong_overlay']:
        changed=copy.deepcopy(recipe)
        if change=='missing_base':
            del changed['environment']['lock_file']
        else:
            changed['environment']['trainer_profile']['sha256']='f'*64
        with pytest.raises(BackendError):
            verify_environment(SimpleNamespace(repository=ROOT,python=sys.executable),changed)


def test_profile_catalog_exposes_overlay_without_substituting_backend_lock():
    profiles=environment_profiles(SimpleNamespace(repository=ROOT))
    trainer=[item for item in profiles if item.get('variant')=='trainer']
    assert {item['target_platform'] for item in trainer}=={'linux-x64','linux-arm64','darwin-arm64'}
    for item in trainer:
        env=item['environment']
        assert env['lock_file']['path']==f"requirements/profiles/backend-{item['target_platform']}.lock"
        assert env['trainer_profile']['sha256']==TRAINER_PROFILE_SHA256
        assert item['native_execution']=='submission_required'


@pytest.mark.parametrize("kind", ["service", "backend"])
def test_trainer_helper_and_both_locks_belong_to_portable_source_and_wheel(tmp_path, kind):
    spec = importlib.util.spec_from_file_location("trainer_replay", ROOT / "scripts/replay_bundle.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    inventory = replay.source_inventory(ROOT)
    assert "src/modelbuilder/backend/trainer_profile.py" in inventory
    assert TRAINER_PROFILE_PATH in inventory
    assert "requirements/profiles/backend-linux-x64.lock" in inventory
    source = tmp_path / "source"
    source.mkdir()
    shutil.copytree(ROOT / "src/modelbuilder", source / "src/modelbuilder",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "requirements/profiles", source / "requirements/profiles")
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    recipe = declared(source)
    recipe["source"]["commit"] = "a" * 40
    paths = [*source.rglob("*.py"), *(source / path for path in replay.PACKAGE_DATA),
             source / "pyproject.toml", source / TRAINER_PROFILE_PATH,
             source / recipe["environment"]["lock_file"]["path"]]
    recipe["source"]["files"] = [{"path": str(path.relative_to(source)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted(paths)]
    (source / "recipe.json").write_text(json.dumps(recipe))
    wheel = replay.build_wheel(source, tmp_path / "wheels", kind)
    with zipfile.ZipFile(wheel["wheel"]) as archive:
        assert archive.read("modelbuilder/backend/trainer_profile.py") == (
            ROOT / "src/modelbuilder/backend/trainer_profile.py").read_bytes()
