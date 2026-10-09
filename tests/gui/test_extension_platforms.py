"""Offline artifact selection/integrity tests, not native platform acceptance."""
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("extension_installer", ROOT / "distribution/install_extensions.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
TARGETS = ["linux-x64", "linux-arm64", "linux-armhf", "darwin-x64", "darwin-arm64", "win32-x64", "win32-arm64"]


def manifest():
    return json.loads((ROOT / "distribution/extension_manifest/extensions.lock.json").read_text())


@pytest.mark.parametrize("target", TARGETS)
def test_every_native_target_has_a_pinned_artifact(target):
    for entry in manifest()["extensions"]:
        selected = installer.select_artifact(entry, target)
        assert selected["target_platform"] in {target, "universal"}
        assert len(selected["sha256"]) == 64


def test_no_native_fallback_to_wrong_architecture():
    entry = {"id": "example.test", "version": "1", "artifacts": {"linux-x64": {"target_platform": "linux-x64"}}}
    with pytest.raises(ValueError, match="No audited"):
        installer.select_artifact(entry, "darwin-arm64")


def archive(tmp_path, target="linux-x64", name="test"):
    path = tmp_path / "sample.vsix"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("extension/package.json", json.dumps({"publisher": "example", "name": name, "version": "1"}))
        attr = '' if target == 'universal' else f' TargetPlatform="{target}"'
        z.writestr("extension.vsixmanifest", f'<PackageManifest><Metadata><Identity Id="example.test" Version="1"{attr}/></Metadata></PackageManifest>')
    return path, {"id": "example.test", "version": "1", "target_platform": target, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.mark.parametrize("target", TARGETS + ["universal"])
def test_embedded_target_and_identity_checked(tmp_path, target):
    path, entry = archive(tmp_path, target)
    installer.verify_archive(path, entry)
    entry["target_platform"] = "wrong"
    with pytest.raises(ValueError, match="platform mismatch"):
        installer.verify_archive(path, entry)


def test_hash_and_identity_checked_even_for_cached_file(tmp_path):
    path, entry = archive(tmp_path)
    entry["id"] = "someone.other"
    with pytest.raises(ValueError, match="identity mismatch"):
        installer.verify_archive(path, entry)
    entry["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        installer.verify_archive(path, entry)


def test_cache_names_include_platform(tmp_path, monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b"test"
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: Response())
    monkeypatch.setattr(installer, "verify_archive", lambda *a: None)
    entry = {"id": "example.test", "version": "1", "url": "https://open-vsx.org/example", "sha256": hashlib.sha256(b"test").hexdigest()}
    a = installer.download({**entry, "target_platform": "linux-x64"}, tmp_path)
    b = installer.download({**entry, "target_platform": "darwin-arm64"}, tmp_path)
    assert a != b


def test_cross_install_fails_before_any_download(monkeypatch):
    monkeypatch.setattr(installer, "detect_target", lambda: "linux-x64")
    monkeypatch.setattr(installer, "download", lambda *a: pytest.fail("must not download"))
    with pytest.raises(SystemExit):
        installer.main(["--target", "darwin-arm64"])


def test_configurable_paths_and_download_only(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(installer, "detect_target", lambda: "linux-x64")
    monkeypatch.setattr(installer, "download", lambda entry, cache: seen.append((entry, cache)) or cache / 'fake.vsix')
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: pytest.fail("download-only must not install"))
    installer.main(["--target", "darwin-arm64", "--download-only", "--cache-dir", str(tmp_path)])
    assert len(seen) == 4 and all(cache == tmp_path for _, cache in seen)


def test_installer_preserves_dependency_pack_exclusion(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(installer, "detect_target", lambda: "linux-x64")
    monkeypatch.setattr(installer, "download", lambda entry, cache: tmp_path / 'fake.vsix')
    monkeypatch.setattr(installer.subprocess, "run", lambda cmd, **k: commands.append(cmd))
    installer.main(["--cli", "/custom/ide", "--user-data-dir", str(tmp_path/'profile'), "--extensions-dir", str(tmp_path/'extensions')])
    assert len(commands) == 4
    assert all(command[0] == '/custom/ide' and '--do-not-include-pack-dependencies' in command for command in commands)
    assert all(str(tmp_path/'profile') in command and str(tmp_path/'extensions') in command for command in commands)


def test_installer_refuses_unaudited_license():
    entry = manifest()["extensions"][0]
    with pytest.raises(ValueError, match="license"):
        installer.select_artifact({**entry, "license": "proprietary"}, "linux-x64")


def test_default_launcher_uses_python_without_shell(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(installer, "detect_target", lambda: "linux-x64")
    monkeypatch.setattr(installer, "download", lambda entry, cache: tmp_path / "fake.vsix")
    monkeypatch.setattr(installer.subprocess, "run", lambda cmd, **kwargs: commands.append((cmd, kwargs)))
    installer.main([])
    assert all(cmd[:2] == [installer.sys.executable, str(installer.RUNTIME / "bin/modelbuilder-ide.py")] for cmd, _ in commands)
    assert all(not kwargs.get("shell") for _, kwargs in commands)


def test_bad_later_entry_prevents_all_downloads(tmp_path, monkeypatch):
    lock = manifest()
    lock["extensions"][-1]["artifacts"] = {}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(lock))
    monkeypatch.setattr(installer, "download", lambda *args: pytest.fail("must preflight all entries"))
    with pytest.raises(ValueError, match="No audited"):
        installer.main(["--download-only", "--target", "linux-x64", "--manifest", str(path)])
