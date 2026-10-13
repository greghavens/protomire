"""Offline package recipe tests. Fake payloads/tools do not establish native support."""
import json
import plistlib
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "distribution"))
import single_app as packaging  # noqa: E402
import build_runtime as builder  # noqa: E402


@pytest.fixture
def payload(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    for name in ["python", "backend", "hub-python", "git", "source.bundle", "notices/LICENSE"]:
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("explicit inert unit-test fixture")
    for name in ["managed_core.py", "windows_storage.py", "reviewed_requests.py", "reviewed_mcp.py", "managed_hub.py", "hub_credentials.py"]:
        shutil.copyfile(ROOT / "distribution" / name, root / name)
    value = {"schema_version": 1, "target": "linux-x64", "sealed": True, "relocation_probe": "passed", "application_version": "2.7.4", "python_version": "3.12.14", "service_python": "python",
             "backend_python": "backend", "git_executable": "git", "source_bundle": "source.bundle"}
    lock = packaging.lock_path(ROOT, "hub", "linux-x64")
    (root / "profiles").mkdir()
    shutil.copyfile(lock, root / "profiles" / lock.name)
    value.update(hub_python="hub-python", hub_status="bundled", hub_profile="profiles/" + lock.name, hub_lock_sha256=packaging.digest(lock))
    trainer = ROOT / "requirements/profiles/trainer.lock"
    shutil.copyfile(trainer, root / "profiles/trainer.lock")
    value["trainer_profile"] = {"status": "bundled", "path": "profiles/trainer.lock", "sha256": packaging.digest(trainer),
                                "backend_lock_sha256": packaging.digest(packaging.lock_path(ROOT, "backend", "linux-x64"))}
    value["files"] = packaging.inventory(root)
    (root / "runtime.json").write_text(json.dumps(value))
    return root


def app_fixture(root, target):
    root.mkdir()
    app_root = root / "resources/app"
    if target.startswith("darwin-"):
        app_root = root / "Modelbuilder IDE.app/Contents/Resources/app"
    feature = app_root / "extensions/modelbuilder-workbench"
    (feature / "out").mkdir(parents=True)
    for name in ["package.json", "out/extension.js", "out/managedCore.js", "out/nativeReview.js"]:
        (feature / name).write_text("inert fixture")
    (app_root / "product.json").write_text(json.dumps({"applicationName": "modelbuilder-ide", "updateUrl": "https://invalid.example"}))
    if target.startswith("darwin-"):
        (app_root.parents[1] / "Info.plist").write_bytes(plistlib.dumps({"LSMinimumSystemVersion": "11.0"}))
    return root


def retarget(payload, target):
    value = json.loads((payload / "runtime.json").read_text())
    value["target"] = target
    if packaging.backend_reason(target):
        value["trainer_profile"] = {"status": "unsupported_target", "reason": packaging.backend_reason(target)}
    else:
        value["trainer_profile"]["backend_lock_sha256"] = packaging.digest(packaging.lock_path(ROOT, "backend", target))
    previous = payload / value["hub_profile"]
    previous.unlink()
    lock = packaging.lock_path(ROOT, "hub", target)
    shutil.copyfile(lock, payload / "profiles" / lock.name)
    value.update(hub_profile="profiles/" + lock.name, hub_lock_sha256=packaging.digest(lock))
    value["files"] = packaging.inventory(payload)
    if target.startswith("darwin-"):
        value["minimum_os"] = "26.0"
    (payload / "runtime.json").write_text(json.dumps(value))


def test_plan_reuses_exact_native_upstream_target_and_does_not_claim_artifact():
    for target, extension in [("linux-x64", "AppImage"), ("darwin-arm64", "dmg"), ("win32-x64", "exe")]:
        result = packaging.plan(target)
        assert result["upstream_task"] == "vscode-" + target + "-min"
        assert result["format"] == extension
        assert result["build_status"] == "not_built"
    assert "does not support native Windows" in packaging.plan("win32-x64")["native_local_backend"]
    assert "no official macOS Intel wheel" in packaging.plan("darwin-x64")["native_local_backend"]


def test_staging_embeds_mandatory_verified_runtime_and_disables_updater(payload, tmp_path):
    app = app_fixture(tmp_path / "upstream", "linux-x64")
    output = tmp_path / "staged"
    report = packaging.stage(app, payload, output, "linux-x64")
    assert report["staged"] and not report["installer_built"]
    root = packaging.application_root(output, "linux-x64")
    product = json.loads((root / "product.json").read_text())
    assert product["modelbuilderBundledCore"] is True
    assert "updateUrl" not in product
    assert (root / "extensions/modelbuilder-workbench/resources/core/managed_core.py").is_file()
    assert report["native_acceptance"] == "not_run"


def test_staging_rejects_missing_host_integration(payload, tmp_path):
    app = app_fixture(tmp_path / "upstream", "linux-x64")
    (packaging.application_root(app, "linux-x64") / "extensions/modelbuilder-workbench/out/managedCore.js").unlink()
    with pytest.raises(ValueError, match="compiled mandatory"):
        packaging.stage(app, payload, tmp_path / "output", "linux-x64")
    assert not (tmp_path / "output").exists()


def test_staging_rejects_wrong_target_or_unlisted_file(payload, tmp_path):
    with pytest.raises(ValueError, match="exact native"):
        packaging.validate_payload(payload, "darwin-arm64")
    (payload / "unexpected.py").write_text("do not execute me")
    with pytest.raises(ValueError, match="unlisted"):
        packaging.validate_payload(payload, "linux-x64")


def test_inventory_rejects_external_symlink(payload, tmp_path):
    (tmp_path / "outside").write_text("external")
    (payload / "escape").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="outside"):
        packaging.inventory(payload)


def test_application_not_built_cannot_be_packaged(tmp_path, monkeypatch):
    monkeypatch.setattr(packaging, "detect_target", lambda: "linux-x64")
    app = app_fixture(tmp_path / "upstream", "linux-x64")
    with pytest.raises(ValueError, match="staged single-app"):
        packaging.package(app, tmp_path / "output", "linux-x64")


def test_cross_native_packaging_is_not_claimed(payload, tmp_path, monkeypatch):
    monkeypatch.setattr(packaging, "detect_target", lambda: "linux-x64")
    with pytest.raises(ValueError, match="native target"):
        packaging.package(tmp_path, tmp_path / "output", "darwin-arm64")


def test_linux_appimage_recipe_uses_verified_tool_and_preserves_sandbox(payload, tmp_path, monkeypatch):
    monkeypatch.setattr(packaging, "detect_target", lambda: "linux-x64")
    app = app_fixture(tmp_path / "upstream", "linux-x64")
    staged = tmp_path / "staged"
    packaging.stage(app, payload, staged, "linux-x64")
    tool = tmp_path / "appimagetool"
    tool.write_text("inert packager fixture")
    with pytest.raises(ValueError, match="reviewed appimagetool"):
        packaging.package(staged, tmp_path / "bad", "linux-x64", tool, "0" * 64)
    calls = []
    def fake_run(command, **options):
        calls.append((command, options))
        Path(command[-1]).write_text("inert artifact fixture")
    monkeypatch.setattr(packaging.subprocess, "run", fake_run)
    result = packaging.package(staged, tmp_path / "output", "linux-x64", tool, packaging.digest(tool))
    assert Path(result["artifact"]).name == "modelbuilder-ide-2.7.4-linux-x64-dev-unsigned.AppImage"
    assert result["native_acceptance"] == "not_run" and not result["signed"] and not result["published"]
    launcher = (tmp_path / "output/Modelbuilder.AppDir/AppRun").read_text()
    assert '"$@"' in launcher and "--no-sandbox" not in launcher and "python" not in launcher
    assert len(calls) == 1 and calls[0][1]["env"]["ARCH"] == "x86_64"


def test_mac_dmg_recipe_uses_native_bundle_and_hdiutil_verify(payload, tmp_path, monkeypatch):
    retarget(payload, "darwin-arm64")
    monkeypatch.setattr(packaging, "detect_target", lambda: "darwin-arm64")
    app = app_fixture(tmp_path / "upstream", "darwin-arm64")
    staged = tmp_path / "staged"
    packaging.stage(app, payload, staged, "darwin-arm64")
    calls = []
    def fake_run(command, **options):
        calls.append(command)
        if command[:2] == ["hdiutil", "create"]:
            Path(command[-1]).write_text("inert disk fixture")
    monkeypatch.setattr(packaging.subprocess, "run", fake_run)
    result = packaging.package(staged, tmp_path / "output", "darwin-arm64")
    assert result["artifact"].endswith(".dmg") and result["native_acceptance"] == "not_run"
    assert calls[0][:2] == ["hdiutil", "create"] and calls[1][:2] == ["hdiutil", "verify"]
    assert (tmp_path / "output/dmg-root/Applications").is_symlink()


def test_input_integrity_is_checked_before_execution_or_staging(tmp_path):
    file = tmp_path / "python.tar.gz"
    file.write_text("corrupt fixture")
    with pytest.raises(ValueError, match="SHA256"):
        builder.verified_input(tmp_path, {"path": file.name, "sha256": "0" * 64})
    assert builder.verified_input(tmp_path, {"path": file.name, "sha256": packaging.digest(file)}) == file


def test_external_venv_is_rejected_as_a_bundled_python(payload, monkeypatch):
    class Result:
        stdout = json.dumps({"version": [3, 12, 14], "prefix": str(payload), "base_prefix": "/usr/system/python",
                             "modules": {"modelbuilder.api": str(payload / "python")}})
    monkeypatch.setattr(packaging.subprocess, "run", lambda *args, **kwargs: Result())
    with pytest.raises(ValueError, match="self-contained"):
        packaging.probe_python(payload, "python", ["modelbuilder.api"], "3.12.14")


def test_artifact_name_tracks_manifest_version_and_rejects_path_injection():
    product = {"applicationName": "new-model-app", "modelbuilderVersion": "7.8.9-preview.2"}
    assert packaging.artifact_name(product, "linux-x64") == "new-model-app-7.8.9-preview.2-linux-x64-dev-unsigned"
    for version in [None, "1.2", "../1.2.3", "1.2.3/escape"]:
        with pytest.raises(ValueError, match="semantic version"):
            packaging.artifact_name({**product, "modelbuilderVersion": version}, "linux-x64")
    with pytest.raises(ValueError, match="unsafe"):
        packaging.artifact_name({**product, "applicationName": "../other"}, "linux-x64")


def test_python_patch_version_mismatch_is_rejected(payload, monkeypatch):
    class Result:
        stdout = json.dumps({"version": [3, 12, 13], "prefix": str(payload), "base_prefix": str(payload),
                             "modules": {"modelbuilder.api": str(payload / "python")}})
    monkeypatch.setattr(packaging.subprocess, "run", lambda *args, **kwargs: Result())
    with pytest.raises(ValueError, match="exact declared pin"):
        packaging.probe_python(payload, "python", ["modelbuilder.api"], "3.12.14")


def test_package_requires_moved_payload_probe(payload):
    manifest = json.loads((payload / "runtime.json").read_text())
    manifest.pop("relocation_probe")
    (payload / "runtime.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="moved-payload"):
        packaging.validate_payload(payload, "linux-x64")


def sealable_fixture(payload):
    manifest = json.loads((payload / "runtime.json").read_text())
    manifest["source_commit"] = "a" * 40
    for field in ["service_python_provenance", "backend_python_provenance", "hub_python_provenance", "git_provenance"]:
        manifest[field] = {"source_url": "https://vendor.invalid/archive", "archive_sha256": "a" * 64, "notice": "notices/LICENSE"}
    (payload / "git-helpers").mkdir()
    (payload / "runtime.json").write_text(json.dumps(manifest))


def test_seal_probes_only_after_original_location_disappears_and_restores(payload, monkeypatch):
    sealable_fixture(payload)
    probes = []
    monkeypatch.setattr(packaging, "detect_target", lambda: "linux-x64")
    def fake_probe(moved, executable, modules, version):
        assert not payload.exists()
        assert " relocated " in str(moved)
        assert version == "3.12.14"
        probes.append(executable)
        return {"version": [3, 12, 14], "modules": {"modelbuilder.hub_sdk_worker": "worker.py"}}
    monkeypatch.setattr(packaging, "probe_python", fake_probe)
    monkeypatch.setattr(packaging, "probe_hub", lambda moved, manifest, worker: {"offline_worker": "passed"})
    monkeypatch.setattr(packaging, "probe_trainer", lambda moved, manifest: {"native_execution": "requires_native_acceptance"})
    monkeypatch.setattr(packaging.subprocess, "check_output", lambda command, **kwargs: str(Path(command[0]).parent / "git-helpers") + "\n")
    def fake_restore(git, archive, destination, commit, shallow):
        assert not payload.exists() and " relocated " in str(archive)
        assert not destination.exists() and shallow is None
        return {"source_commit": commit, "fsck": "passed", "refs": {"refs/heads/shipping": commit}}
    monkeypatch.setattr(packaging, "restore_source", fake_restore)
    result = packaging.seal_runtime(payload, "linux-x64")
    assert payload.exists() and probes == ["python", "backend"] and result["sealed"]
    manifest = json.loads((payload / "runtime.json").read_text())
    assert manifest["relocation_probe"] == "passed"
    assert manifest["native_dependency_closure"] == "requires_clean_machine_acceptance"
    assert "backend_python_provenance" in manifest


def test_failed_relocation_probe_restores_original_payload(payload, monkeypatch):
    sealable_fixture(payload)
    monkeypatch.setattr(packaging, "detect_target", lambda: "linux-x64")
    def fail(*args):
        assert not payload.exists()
        raise ValueError("fixture relocation failure")
    monkeypatch.setattr(packaging, "probe_python", fail)
    with pytest.raises(ValueError, match="relocation failure"):
        packaging.seal_runtime(payload, "linux-x64")
    assert payload.is_dir()
    assert not list(payload.parent.glob("runtime relocated *"))


def test_windows_reuses_upstream_user_recipe_with_derived_version(tmp_path, monkeypatch):
    source = tmp_path / "upstream"
    compiler = source / "node_modules/innosetup/bin/ISCC.exe"
    compiler.parent.mkdir(parents=True)
    compiler.write_text("inert fixture")
    recipe = source / "build/win32/code.iss"
    recipe.parent.mkdir(parents=True)
    recipe.write_text("inert fixture")
    app = app_fixture(tmp_path / "app", "win32-x64")
    product = json.loads((ROOT / "distribution/overlay/product.json").read_text())
    product["modelbuilderVersion"] = "9.4.2"
    (packaging.application_root(app, "win32-x64") / "product.json").write_text(json.dumps(product))
    output = tmp_path / "installer"
    output.mkdir()
    monkeypatch.setattr(packaging, "build_plan", lambda *args: {"source": str(source), "pin": {"commit": "a" * 40}})
    monkeypatch.setattr(packaging.subprocess, "check_output", lambda *args, **kwargs: "a" * 40)
    commands = []
    def fake_run(command, **kwargs):
        commands.append(command)
        (output / "setup.exe").write_text("inert fixture")
    monkeypatch.setattr(packaging.subprocess, "run", fake_run)
    artifact = packaging.windows_installer(app, output, "win32-x64")
    assert artifact.name == "setup.exe"
    assert commands[0][:2] == [str(compiler), str(recipe)]
    assert "/dVersion=9.4.2" in commands[0] and "/dInstallTarget=user" in commands[0]
    assert all("/dSign" not in arg for arg in commands[0])


def test_macos_floor_comes_from_installed_wheel_tags_and_updates_bundle(tmp_path, payload):
    native = payload / "python-libs/mlx_metal.dist-info/WHEEL"
    native.parent.mkdir(parents=True)
    native.write_text("Wheel-Version: 1.0\nTag: cp312-cp312-macosx_26_0_arm64\n")
    assert packaging.macos_floor(payload, "11.0") == "26.0"
    with pytest.raises(ValueError, match="explicit minimum_os"):
        packaging.macos_floor(payload, None)
    retarget(payload, "darwin-arm64")
    manifest = json.loads((payload / "runtime.json").read_text())
    manifest["files"] = packaging.inventory(payload)
    (payload / "runtime.json").write_text(json.dumps(manifest))
    app = app_fixture(tmp_path / "upstream", "darwin-arm64")
    output = tmp_path / "staged"
    packaging.stage(app, payload, output, "darwin-arm64")
    info = plistlib.loads((packaging.application_root(output, "darwin-arm64").parents[1] / "Info.plist").read_bytes())
    assert info["LSMinimumSystemVersion"] == "26.0"


def test_optional_mlx_floor_does_not_raise_base_application_minimum(payload, tmp_path):
    retarget(payload, "darwin-arm64")
    optional = payload / "mlx-python/lib/site-packages/mlx_metal.dist-info/WHEEL"
    optional.parent.mkdir(parents=True)
    optional.write_text("Tag: cp312-cp312-macosx_26_0_arm64\n")
    (payload / "mlx-python/python").write_text("inert optional interpreter")
    required = payload / "backend-libs/torch.dist-info/WHEEL"
    required.parent.mkdir(parents=True)
    required.write_text("Tag: cp312-none-macosx_14_0_arm64\n")
    manifest = json.loads((payload / "runtime.json").read_text())
    manifest.update(minimum_os="14.0", mlx_minimum_os="26.0", mlx_python="mlx-python/python")
    manifest["files"] = packaging.inventory(payload)
    (payload / "runtime.json").write_text(json.dumps(manifest))
    assert packaging.macos_floor(payload, "11.0", ("mlx-python",)) == "14.0"
    app = app_fixture(tmp_path / "app", "darwin-arm64")
    output = tmp_path / "staged"
    packaging.stage(app, payload, output, "darwin-arm64")
    info = plistlib.loads((packaging.application_root(output, "darwin-arm64").parents[1] / "Info.plist").read_bytes())
    assert info["LSMinimumSystemVersion"] == "14.0"


def test_hub_profile_exact_target_selection_and_armhf_fail_closed(payload):
    from dependency_profiles import HUB_TARGETS, lock_path
    assert len(HUB_TARGETS) == 6
    for target in HUB_TARGETS:
        path = lock_path(ROOT, 'hub', target)
        assert path.name == f'hub-{target}.lock' and path.is_file()
    with pytest.raises(ValueError, match='Hub/keyring'):
        lock_path(ROOT, 'hub', 'linux-armhf')
    value = json.loads((payload / 'runtime.json').read_text())
    with pytest.raises(ValueError, match='exact native target'):
        packaging.validate_hub_profile(payload, value, 'darwin-arm64')
    value.pop('hub_python')
    value['hub_status'] = 'unsupported_target'
    assert packaging.validate_hub_profile(payload, value, 'linux-armhf') is None


def test_hub_payload_omission_and_tampered_profile_fail_closed(payload):
    value = json.loads((payload / 'runtime.json').read_text())
    for field in ['hub_python', 'hub_status', 'hub_profile', 'hub_lock_sha256']:
        missing = {key: item for key, item in value.items() if key != field}
        with pytest.raises((ValueError, packaging.CoreError)):
            packaging.validate_hub_profile(payload, missing, 'linux-x64')
    (payload / value['hub_profile']).write_text('keyring==0.0.0\n')
    with pytest.raises(ValueError, match='reviewed hash lock'):
        packaging.validate_hub_profile(payload, value, 'linux-x64')


def test_builder_requires_hub_before_mutating_staging(tmp_path, monkeypatch):
    inputs = tmp_path / 'inputs.json'
    inputs.write_text(json.dumps({'target':'linux-x64'}))
    monkeypatch.setattr(builder, 'detect_target', lambda:'linux-x64')
    with pytest.raises(ValueError, match='Hub/keyring worker'):
        builder.build_runtime(inputs, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


def test_hub_sealed_helper_changes_are_rejected(payload):
    (payload / 'managed_hub.py').write_text('modified helper')
    value = json.loads((payload / 'runtime.json').read_text())
    value['files'] = packaging.inventory(payload)
    (payload / 'runtime.json').write_text(json.dumps(value))
    with pytest.raises(ValueError, match='stale'):
        packaging.validate_payload(payload, 'linux-x64')


def test_builder_installs_target_hub_closure_offline_without_application_wheel(tmp_path, monkeypatch):
    import zipfile
    archive = tmp_path / 'reviewed-python.tar.gz'
    archive.write_text('inert native input fixture')
    wheel = tmp_path / 'modelbuilder-8.2.1-py3-none-any.whl'
    with zipfile.ZipFile(wheel, 'w') as output:
        output.writestr('modelbuilder-8.2.1.dist-info/METADATA', 'Metadata-Version: 2.1\nName: modelbuilder\nVersion: 8.2.1\n')
    python = {'path':str(archive),'sha256':packaging.digest(archive),'source_url':'https://vendor.invalid/reviewed','executable':'bin/python','notice':'notices/LICENSE','version':'3.12.14'}
    application = {'path':str(wheel),'sha256':packaging.digest(wheel)}
    (tmp_path / 'wheelhouse').mkdir()
    (tmp_path / 'notices').mkdir()
    (tmp_path / 'notices/LICENSE').write_text('inert notices fixture')
    values = {'target':'linux-x64','service':{'python':python,'wheel':application},'backend':{'python':python,'wheel':application},
              'hub':{'python':python},'git':python,'source_bundle':{'path':str(archive),'sha256':packaging.digest(archive)},
              'source_commit':'a'*40,'wheelhouse':str(tmp_path/'wheelhouse'),'notices':str(tmp_path/'notices')}
    inputs = tmp_path / 'inputs.json'
    inputs.write_text(json.dumps(values))
    monkeypatch.setattr(builder,'detect_target',lambda:'linux-x64')
    def extract(base,item,destination):
        path=destination/'bin/python'
        path.parent.mkdir(parents=True)
        path.write_text('inert executable fixture')
        return path
    monkeypatch.setattr(builder,'extract_input',extract)
    calls=[]
    monkeypatch.setattr(builder.subprocess,'run',lambda command,**kwargs:calls.append(command))
    monkeypatch.setattr(builder,'seal_runtime',lambda root,target:json.loads((root/'runtime.json').read_text()))
    manifest=builder.build_runtime(inputs,tmp_path/'output')
    trainer=[command for command in calls if command[-1].endswith('trainer.lock')]
    assert len(trainer)==1 and 'backend-python' in trainer[0][0]
    assert all(flag in trainer[0] for flag in ['-B','--no-index','--no-deps','--only-binary=:all:','--require-hashes'])
    assert manifest['trainer_profile']['status']=='bundled'
    assert manifest['trainer_profile']['sha256']==packaging.digest(ROOT/'requirements/profiles/trainer.lock')
    assert manifest['hub_status']=='bundled' and manifest['hub_profile']=='profiles/hub-linux-x64.lock'
    assert manifest['hub_python_provenance']['version']=='3.12.14'
    hub=[command for command in calls if 'hub-python' in command[0]]
    assert len(hub)==2  # locked dependencies and pip check, no modelbuilder wheel
    assert all(flag in hub[0] for flag in ['--no-index','--only-binary=:all:','--require-hashes','--isolated'])
    assert hub[0][-1].endswith('hub-linux-x64.lock')
    assert packaging.digest(tmp_path/'output'/manifest['hub_profile'])==manifest['hub_lock_sha256']


def test_mandatory_hub_os_floor_is_preserved_while_optional_mlx_is_separate():
    assert builder.required_macos_floor({"service":"11.0", "backend":"14.0", "hub":"15.0", "mlx":"26.0"}) == "15.0"


def test_trainer_overlay_required_exact_and_unsupported_targets_explicit(payload):
    value=json.loads((payload/'runtime.json').read_text())
    assert packaging.validate_trainer_profile(payload,value,'linux-x64')['status']=='bundled'
    missing={**value,'trainer_profile':{}}
    with pytest.raises(ValueError,match='exact overlay'):
        packaging.validate_trainer_profile(payload,missing,'linux-x64')
    wrong={**value,'trainer_profile':{**value['trainer_profile'],'backend_lock_sha256':'0'*64}}
    with pytest.raises(ValueError,match='complete target backend'):
        packaging.validate_trainer_profile(payload,wrong,'linux-x64')
    for target in ['win32-x64','win32-arm64','darwin-x64','linux-armhf']:
        assert packaging.plan(target)['trainer_profile']['status']=='unsupported_target'
        with pytest.raises(ValueError,match='unsupported local-backend'):
            packaging.validate_trainer_profile(payload,value,target)


def test_trainer_probe_runs_from_moved_prefix_and_rejects_wrong_installed_overlay(payload,monkeypatch):
    value=json.loads((payload/'runtime.json').read_text())
    calls=[]
    def probe(root,python,modules,version):
        calls.append((root,python,modules,version))
        return {'modules':dict.fromkeys(modules,'fixture.py')}
    monkeypatch.setattr(packaging,'probe_python',probe)
    import re
    versions={}
    for lock in [packaging.lock_path(ROOT,'backend','linux-x64'),ROOT/'requirements/profiles/trainer.lock']:
        versions.update(dict(re.findall(r'^([A-Za-z0-9_.-]+)==([A-Za-z0-9.+!-]+)(?:\s|$)',lock.read_text(),re.M)))
    class Result:
        stdout=json.dumps({name:{'version':version,'root':str(payload)} for name,version in versions.items()})
    def run(command,**kwargs):
        assert command[:3]==[str(payload/'backend'),'-I','-B']
        return Result()
    monkeypatch.setattr(packaging.subprocess,'run',run)
    assert packaging.probe_trainer(payload,value)['packages']['accelerate']=='1.11.0'
    assert calls[0][2]==['accelerate','transformers.trainer']
    versions['accelerate']='0.0.0'
    Result.stdout=json.dumps({name:{'version':version,'root':str(payload)} for name,version in versions.items()})
    with pytest.raises(ValueError,match='sealed hash locks'):
        packaging.probe_trainer(payload,value)
