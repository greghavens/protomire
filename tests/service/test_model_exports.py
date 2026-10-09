"""Packaging security and shared HTTP/MCP dispatch contracts (no fake execution claims)."""
import base64
import hashlib
import json
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from modelbuilder.api import create_app
from modelbuilder.backend.admission import BackendError
from modelbuilder.export_packages import inspect_export, package_export, read_package, safe_file
from modelbuilder.models import Actor, OPERATIONS, ServiceError
from modelbuilder.service import OperationService


def fixture(tmp_path):
    root = tmp_path / "artifact"
    root.mkdir()
    (root / "loader.py").write_text("raise RuntimeError('Packaging must not execute this file')\n")
    (root / "weights.bin").write_bytes(b"weights" * 100)
    (root / "undeclared-secret.txt").write_text("Not part of the declared export")
    manifest = {"schema_version": "modelbuilder.export/v1", "selected_checkpoint": "selected-only",
                "loader": {"path": "loader.py", "entrypoint": "load"}, "signature": {"input": "tensor"},
                "dependencies": {"torch": "pinned"}, "source": {"commit": "committed-source"},
                "verification": {"passed": True, "atol": 0.0},
                "assets": [{"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()}
                           for name in ("loader.py", "weights.bin")]}
    (root / "export.manifest.json").write_text(json.dumps(manifest))
    provenance = {"project_id": "project", "backend_run_id": "run", "model_version_id": "version", "artifact_id": "export"}
    return root, manifest, provenance


def test_package_contains_only_verified_declared_assets_and_provenance(tmp_path):
    root, manifest, provenance = fixture(tmp_path)
    inspection = inspect_export(root, provenance)
    transfer = package_export(root, tmp_path / "cache", inspection, inspection["export_digest"])
    payload = {**provenance, "package_id": transfer["package_id"], "chunk_index": 0}
    result = read_package(tmp_path / "cache", payload)
    archive = tmp_path / "saved.zip"
    archive.write_bytes(base64.b64decode(result["data_base64"]))
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == transfer["sha256"]
    with zipfile.ZipFile(archive) as bundle:
        assert set(bundle.namelist()) == {"loader.py", "weights.bin", "export.manifest.json", "modelbuilder.provenance.json"}
        assert json.loads(bundle.read("export.manifest.json")) == manifest
        assert json.loads(bundle.read("modelbuilder.provenance.json")) == provenance


@pytest.mark.parametrize("field", ["project_id", "backend_run_id", "model_version_id", "artifact_id"])
def test_every_transfer_identity_is_checked(tmp_path, field):
    root, _, provenance = fixture(tmp_path)
    inspection = inspect_export(root, provenance)
    transfer = package_export(root, tmp_path / "cache", inspection, inspection["export_digest"])
    with pytest.raises(BackendError) as error:
        read_package(tmp_path / "cache", {**provenance, field: "other", "package_id": transfer["package_id"], "chunk_index": 0})
    assert error.value.code == "not_found"


@pytest.mark.parametrize("name", ["../secret", "/secret", "a/../../secret", "a\\secret", "C:secret", "./loader.py", "a//loader.py", "CON", "aux.txt", "dir./file", "export.manifest.json", "EXPORT.MANIFEST.JSON", "modelbuilder.provenance.json"])
def test_unsafe_asset_names_are_rejected_before_file_reads(tmp_path, name):
    root, manifest, provenance = fixture(tmp_path)
    manifest["assets"].append({"path": name, "sha256": "0" * 64})
    (root / "export.manifest.json").write_text(json.dumps(manifest))
    with pytest.raises((ValueError, OSError)):
        inspect_export(root, provenance)


def test_symlink_assets_manifest_ancestor_and_fifo_are_rejected(tmp_path):
    root, manifest, provenance = fixture(tmp_path)
    (root / "weights.bin").unlink()
    (root / "weights.bin").symlink_to(root / "loader.py")
    with pytest.raises((ValueError, OSError)):
        inspect_export(root, provenance)
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        safe_file(link / "loader.py")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular"):
        safe_file(fifo)
    (root / "export.manifest.json").rename(root / "manifest.json")
    (root / "export.manifest.json").symlink_to(root / "manifest.json")
    with pytest.raises((ValueError, OSError)):
        inspect_export(root, provenance)


def test_size_bounds_apply_before_hashing_and_same_handle_packaging_rechecks(tmp_path, monkeypatch):
    import modelbuilder.export_packages as module
    root, _, provenance = fixture(tmp_path)
    inspection = inspect_export(root, provenance)
    monkeypatch.setattr(module, "MAX_EXPORT_BYTES", 10)
    with pytest.raises(ValueError, match="16 GiB"):
        inspect_export(root, provenance)
    monkeypatch.setattr(module, "MAX_EXPORT_BYTES", 16 * 1024**3)
    (root / "weights.bin").write_bytes(b"tamper!")
    with pytest.raises(ValueError, match="changed"):
        package_export(root, tmp_path / "cache", inspection, inspection["export_digest"])
    assert not list((tmp_path / "cache").glob("*.zip"))
    assert not list((tmp_path / "cache").glob("*.partial"))


def test_descriptor_and_chunk_tampering_and_out_of_bounds_are_rejected(tmp_path):
    root, _, provenance = fixture(tmp_path)
    inspection = inspect_export(root, provenance)
    cache = tmp_path / "cache"
    transfer = package_export(root, cache, inspection, inspection["export_digest"])
    payload = {**provenance, "package_id": transfer["package_id"], "chunk_index": 0}
    with pytest.raises(BackendError):
        read_package(cache, {**payload, "chunk_index": 1})
    archive = cache / (transfer["package_id"] + ".zip")
    with archive.open("r+b") as stream:
        stream.write(b"BAD")
    with pytest.raises(BackendError, match="unsafe or changed"):
        read_package(cache, payload)
    metadata = cache / (transfer["package_id"] + ".json")
    changed = json.loads(metadata.read_text())
    changed["provenance"]["project_id"] = "other"
    metadata.write_text(json.dumps(changed))
    with pytest.raises(BackendError, match="unsafe or changed"):
        read_package(cache, {**payload, "project_id": "other"})
    with pytest.raises(BackendError):
        read_package(cache, {**payload, "package_id": "../anything"})


def test_public_operations_are_readonly_project_scoped_and_disallow_paths(tmp_path):
    service = OperationService(tmp_path / "service.sqlite")
    owner = Actor("owner", {"read", "modify", "execute"})
    project = service.invoke("project.create", {"slug": "release", "model_name": "Release", "idempotency_key": "new"}, owner)
    for name in ("model.export.list", "model.export.inspect", "model.export.package", "model.export.read"):
        assert OPERATIONS[name].scope == "read" and not OPERATIONS[name].mutating
        assert "path" not in OPERATIONS[name].input_schema["properties"]
        with pytest.raises(ServiceError) as denied:
            service.invoke(name, {"project_id": project["project_id"]}, Actor("other", {"read"}, {"different"}))
        assert denied.value.status_code == 403
    # Generic authenticated endpoint is the same dispatcher discovered by MCP.
    app = create_app(service, tokens={"reader-token": {"id": "reader", "scopes": ["read"], "project_ids": [project["project_id"]]}})
    client = TestClient(app)
    assert client.post("/v1/operations/model.export.list", json={"project_id": project["project_id"]}).status_code == 401
    catalog = client.get("/v1/catalog", headers={"Authorization": "Bearer reader-token"})
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()["granted_scopes"] == ["read"]
    response = client.post("/v1/operations/model.export.inspect", json={"project_id": project["project_id"], "artifact_id": "a", "backend_run_id": "b", "path": "/tmp/secret"}, headers={"Authorization": "Bearer reader-token"})
    assert response.status_code == 400
    service.store.close()


@pytest.mark.parametrize("unsafe", ["../escape", "/escape", "symlink", "hardlink", "device", "oversized"])
def test_native_tar_is_bounded_and_never_extracts_unsafe_entries(tmp_path, unsafe, monkeypatch):
    import io
    import tarfile
    from types import SimpleNamespace
    from modelbuilder.backend.native_exports import native_directory
    import modelbuilder.backend.native_exports as module
    root = tmp_path / "backend"
    uri = root / "zenml-stores/owned/export"
    uri.mkdir(parents=True)
    info = tarfile.TarInfo(unsafe)
    if unsafe == "symlink":
        info.type, info.linkname = tarfile.SYMTYPE, "/tmp/escape"
    elif unsafe == "hardlink":
        info.type, info.linkname = tarfile.LNKTYPE, "/tmp/escape"
    elif unsafe == "device":
        info.type = tarfile.CHRTYPE
    else:
        info.size = 100
    if unsafe == "oversized":
        monkeypatch.setattr(module, "MAX_EXPORT_BYTES", -16 * 1024**2 + 10)
    with tarfile.open(uri / "data.tar.gz", "w:gz") as archive:
        archive.addfile(info, io.BytesIO(b"a" * 100) if info.isfile() else None)
    artifact = SimpleNamespace(uri=str(uri), materializer=SimpleNamespace(module="zenml.materializers.path_materializer", attribute="PathMaterializer"))
    with pytest.raises((ValueError, OSError)):
        with native_directory(artifact, root):
            pytest.fail("Unsafe archive was accepted")
    assert not (tmp_path / "escape").exists()
