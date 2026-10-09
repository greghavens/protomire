import base64
import hashlib
import io
import tarfile

import pytest

from modelbuilder.recipe import (
    RecipeValidationError, default_recipe, export_bundle, import_bundle,
    normalize_recipe,
)


def recipe():
    result = default_recipe("p", "r")
    result["source"]["commit"] = "a" * 40
    return result


def archive(tmp_path, name, data=b"data", kind=None, linkname=""):
    target = tmp_path / "attack.tar"
    with tarfile.open(target, "w") as handle:
        member = tarfile.TarInfo(name)
        if kind is not None:
            member.type = kind
        member.linkname = linkname
        member.size = len(data)
        handle.addfile(member, io.BytesIO(data))
    return target


@pytest.mark.parametrize("path", ["../escape.py", "/tmp/escape.py", "files/../../escape.py", "files\\..\\escape.py", "files/%2e%2e/escape.py", "files/.ssh/id_rsa", "files/.env", "C:/escape.py"])
def test_tar_traversal_rejected_before_any_writes(tmp_path, path):
    target = archive(tmp_path, path)
    destination = tmp_path / "out"
    with pytest.raises(RecipeValidationError) as error:
        import_bundle(target, destination)
    assert error.value.code == "unsafe_path"
    assert not destination.exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.DIRTYPE])
def test_symlinks_devices_and_directories_are_rejected(tmp_path, kind):
    target = archive(tmp_path, "files/link.py", kind=kind, linkname="outside")
    with pytest.raises(RecipeValidationError) as error:
        import_bundle(target)
    assert error.value.code == "unsafe_archive_member"


def test_recipe_tamper_detected():
    bundle = export_bundle(recipe())
    bundle["recipe"]["name"] = "tampered"
    with pytest.raises(RecipeValidationError) as error:
        import_bundle(bundle)
    assert error.value.code == "recipe_hash_mismatch"


def test_source_tamper_detected(tmp_path):
    original = b"print('safe')\n"
    (tmp_path / "training.py").write_bytes(original)
    body = recipe()
    body["source"]["files"] = [{"path": "training.py", "sha256": hashlib.sha256(original).hexdigest()}]
    bundle = export_bundle(body, source_root=tmp_path)
    bundle["files"]["training.py"]["content"] = base64.b64encode(b"print('changed')\n").decode()
    with pytest.raises(RecipeValidationError) as error:
        import_bundle(bundle)
    assert error.value.code == "file_hash_mismatch"


def test_inventory_tamper_detected(tmp_path):
    (tmp_path / "training.py").write_bytes(b"pass\n")
    body = recipe()
    body["source"]["files"] = [{"path": "training.py", "sha256": hashlib.sha256(b"pass\n").hexdigest()}]
    bundle = export_bundle(body, source_root=tmp_path)
    bundle["files"] = {}
    with pytest.raises(RecipeValidationError) as error:
        import_bundle(bundle)
    assert error.value.code == "bundle_hash_mismatch"


@pytest.mark.parametrize("value", ["/home/alice/checkpoint.pt", "C:\\Users\\alice\\checkpoint.pt", "~/private.key"])
def test_no_absolute_host_paths(value):
    body = recipe()
    body["evaluation"]["checkpoint"] = value
    with pytest.raises(RecipeValidationError) as error:
        normalize_recipe(body)
    assert error.value.code == "absolute_path"


@pytest.mark.parametrize("key", ["api_key", "password", "client_secret", "authorization", "access_token", "aws_secret_access_key"])
def test_secret_fields_rejected(key):
    body = recipe()
    body["evaluation"][key] = "do-not-export-this"
    with pytest.raises(RecipeValidationError) as error:
        export_bundle(body)
    assert error.value.code == "secret_detected"


@pytest.mark.parametrize("url", ["https://user:secret@example.org/repo", "https://example.org/repo?api_key=secret", "file:///etc/passwd", "javascript:alert(1)", "http://example.org/repo"])
def test_credential_and_executable_or_local_urls_rejected(url):
    body = recipe()
    body["source"]["repository"] = url
    with pytest.raises(RecipeValidationError):
        normalize_recipe(body)


def test_source_symlink_cannot_read_outside_root(tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("private_value = 1\n")
    source = tmp_path / "source"
    source.mkdir()
    (source / "link.py").symlink_to(outside)
    body = recipe()
    body["source"]["files"] = [{"path": "link.py", "sha256": hashlib.sha256(outside.read_bytes()).hexdigest()}]
    with pytest.raises(RecipeValidationError) as error:
        export_bundle(body, source_root=source)
    assert error.value.code == "unsafe_symlink"


def test_no_secret_source_export(tmp_path):
    data = b'api_key = "this-is-a-real-looking-secret"\n'
    (tmp_path / "training.py").write_bytes(data)
    body = recipe()
    body["source"]["files"] = [{"path": "training.py", "sha256": hashlib.sha256(data).hexdigest()}]
    with pytest.raises(RecipeValidationError) as error:
        export_bundle(body, source_root=tmp_path)
    assert error.value.code == "secret_detected"


def test_duplicate_json_keys_rejected(tmp_path):
    blob = b'{"bundle_version":"0","bundle_version":"1"}'
    target = archive(tmp_path, "manifest.json", blob)
    with pytest.raises(RecipeValidationError) as error:
        import_bundle(target)
    assert error.value.code == "duplicate_json_key"


def test_duplicate_tar_members_rejected(tmp_path):
    target = tmp_path / "duplicate.tar"
    with tarfile.open(target, "w") as handle:
        for _ in range(2):
            info = tarfile.TarInfo("manifest.json")
            info.size = 2
            handle.addfile(info, io.BytesIO(b"{}"))
    with pytest.raises(RecipeValidationError) as error:
        import_bundle(target)
    assert error.value.code == "duplicate_archive_member"


def test_json_nan_and_infinity_rejected():
    for value in (float("nan"), float("inf")):
        body = recipe()
        body["evaluation"]["metric"] = value
        with pytest.raises(RecipeValidationError) as error:
            normalize_recipe(body)
        assert error.value.code == "invalid_json"


def test_import_does_not_import_python_or_spawn_or_fetch(tmp_path, monkeypatch):
    import builtins
    import subprocess
    import urllib.request
    body = recipe()
    data = b"from pathlib import Path\nPath('EXECUTED').write_text('bad')\n"
    (tmp_path / "attack.py").write_bytes(data)
    body["source"]["files"] = [{"path": "attack.py", "sha256": hashlib.sha256(data).hexdigest()}]
    bundle = export_bundle(body, source_root=tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("Import may never execute or access network")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    original_import = builtins.__import__
    def checked_import(name, *args, **kwargs):
        if name == "attack":
            forbidden()
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", checked_import)
    result = import_bundle(bundle, tmp_path / "inert")
    assert result["execution_allowed"] is False
    assert not (tmp_path / "inert/EXECUTED").exists()


def test_remote_parameter_schema_is_not_fetched(monkeypatch):
    body = recipe()
    body["nodes"] = [{"id": "data", "kind": "dataset.synthetic", "implementation_ref": "builtin:dataset.synthetic",
                       "inputs": [], "outputs": [], "parameters": {}, "parameter_schema": {"$ref": "https://example.org/schema"},
                       "environment_ref": "default", "resource_request": {}, "retry": {"max_attempts": 1, "backoff_seconds": 0},
                       "timeout": 30, "cache_policy": "disabled", "provenance": {}}]
    with pytest.raises(RecipeValidationError) as error:
        normalize_recipe(body)
    assert error.value.code == "unsafe_schema"


@pytest.mark.parametrize("schema", [{"$ref": "#"}, {"pattern": "^(a+)+$"}, {"patternProperties": {".*": {}}}])
def test_recursive_or_regex_parameter_schemas_fail_closed(schema):
    body = recipe()
    body["nodes"] = [{"id": "data", "kind": "dataset.synthetic", "implementation_ref": "builtin:dataset.synthetic",
                       "inputs": [], "outputs": [], "parameters": {}, "parameter_schema": schema,
                       "environment_ref": "default", "resource_request": {}, "retry": {"max_attempts": 1, "backoff_seconds": 0},
                       "timeout": 30, "cache_policy": "disabled", "provenance": {}}]
    with pytest.raises(RecipeValidationError) as error:
        normalize_recipe(body)
    assert error.value.code == "unsafe_schema"


def test_compressed_archives_are_not_auto_decompressed(tmp_path):
    target = tmp_path / "compressed.tar.gz"
    with tarfile.open(target, "w:gz") as handle:
        info = tarfile.TarInfo("manifest.json")
        info.size = 2
        handle.addfile(info, io.BytesIO(b"{}"))
    with pytest.raises(RecipeValidationError) as error:
        import_bundle(target)
    assert error.value.code == "invalid_archive"


def test_declared_oversized_member_rejected_before_read(tmp_path):
    target = tmp_path / "huge.tar"
    info = tarfile.TarInfo("manifest.json")
    info.size = 10**10
    target.write_bytes(info.tobuf())
    with pytest.raises(RecipeValidationError) as error:
        import_bundle(target)
    assert error.value.code == "bundle_limit"


def test_destination_symlink_does_not_redirect_writes(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(RecipeValidationError) as error:
        import_bundle(export_bundle(recipe()), link / "new")
    assert error.value.code == "unsafe_symlink"
    assert list(real.iterdir()) == []
