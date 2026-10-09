"""Verified local export snapshots and bounded transfer; never a run registry.

Only the native SDK resolves artifacts. This module receives an already owned
export directory and packages exactly its declared bytes, without executing it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import zipfile

from .backend.admission import BackendError
from .backend.components import validate_export_manifest

CHUNK_BYTES = 4 * 1024 * 1024
MAX_EXPORT_BYTES = 16 * 1024**3
MAX_DESCRIPTOR_BYTES = 4 * 1024 * 1024
RESERVED = {"export.manifest.json", "modelbuilder.provenance.json"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def safe_file(path: Path):
    """Reject symlink ancestors and nonregular files before bounded reads."""
    absolute = path.absolute()
    parent = os.open(absolute.anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for component in absolute.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                            | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(absolute.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
    finally:
        os.close(parent)
    stream = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        stream.close()
        raise ValueError("Export asset must be a regular file")
    return stream


def inspect_export(root: Path, provenance: dict):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Export root must be a regular directory")
    # Structural and content verification use only bounded, safely opened handles.
    with safe_file(root / "export.manifest.json") as stream:
        raw = stream.read(MAX_DESCRIPTOR_BYTES + 1)
    if len(raw) > MAX_DESCRIPTOR_BYTES:
        raise ValueError("Export manifest is oversized")
    manifest = json.loads(raw)
    canonical(manifest)  # Reject nonfinite JSON.
    validate_export_manifest(manifest)
    assets = manifest.get("assets", [])
    if not isinstance(assets, list) or not 1 <= len(assets) <= 10000:
        raise ValueError("Export must declare 1 through 10000 assets")
    sizes, total, names = {}, 0, set()
    for asset in assets:
        name = asset.get("path", "")
        relative = PurePosixPath(name)
        if (not name or str(relative) != name or relative.is_absolute() or ".." in relative.parts
                or "\\" in name or ":" in name or any(ord(c) < 32 for c in name)
                or len(name.encode()) > 1024 or any(len(part.encode()) > 255 or part.endswith((".", " "))
                   or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in relative.parts)
                or name.casefold() in names or name.casefold() in RESERVED):
            raise ValueError("Export asset path is unsafe, duplicate or reserved")
        names.add(name.casefold())
        with safe_file(root / name) as stream:
            size = os.fstat(stream.fileno()).st_size
            if total + size > MAX_EXPORT_BYTES:
                raise ValueError("Export exceeds the 16 GiB package limit")
            actual, read = hashlib.sha256(), 0
            while chunk := stream.read(CHUNK_BYTES):
                read += len(chunk)
                if read > size:
                    raise ValueError("Export asset changed or exceeds its size bound")
                actual.update(chunk)
            if read != size or actual.hexdigest() != asset.get("sha256"):
                raise ValueError("Export asset integrity mismatch")
        total += size
        sizes[name] = size
    descriptor = {"manifest": manifest, "provenance": provenance,
                  "assets": [{**asset, "size_bytes": sizes[asset["path"]]} for asset in manifest["assets"]],
                  "total_asset_bytes": total}
    return {**descriptor, "export_digest": digest(descriptor), "verified": True}


def package_export(root: Path, cache: Path, descriptor: dict, expected_digest: str):
    if descriptor["export_digest"] != expected_digest:
        raise BackendError("export_changed", "Export identity changed; inspect it again before saving")
    cache.mkdir(parents=True, exist_ok=True)
    if any(part.is_symlink() for part in [cache, *cache.parents]):
        raise ValueError("Export package cache cannot contain symlinks")
    fd, temporary = tempfile.mkstemp(prefix="export-", suffix=".partial", dir=cache)
    os.close(fd)
    path = Path(temporary)
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for asset in descriptor["assets"]:
                with safe_file(root / asset["path"]) as source, archive.open(asset["path"], "w", force_zip64=True) as target:
                    actual, size = hashlib.sha256(), 0
                    while chunk := source.read(CHUNK_BYTES):
                        size += len(chunk)
                        if size > asset["size_bytes"]:
                            raise ValueError("Export asset changed while packaging")
                        actual.update(chunk)
                        target.write(chunk)
                if size != asset["size_bytes"] or actual.hexdigest() != asset["sha256"]:
                    raise ValueError("Export asset changed while packaging")
            archive.writestr("export.manifest.json", canonical(descriptor["manifest"]))
            archive.writestr("modelbuilder.provenance.json", canonical(descriptor["provenance"]))
        chunks, full = [], hashlib.sha256()
        with safe_file(path) as source:
            while chunk := source.read(CHUNK_BYTES):
                full.update(chunk)
                chunks.append(hashlib.sha256(chunk).hexdigest())
        transfer = {"schema_version": "modelbuilder.export-transfer/v1", "export_digest": expected_digest,
                    "provenance": descriptor["provenance"], "size_bytes": path.stat().st_size,
                    "sha256": full.hexdigest(), "chunk_bytes": CHUNK_BYTES, "chunk_sha256": chunks}
        identity = digest(transfer)
        os.replace(path, cache / f"{identity}.zip")
        metadata = cache / f"{identity}.json"
        # Atomic metadata means readers never observe a partially written descriptor.
        fd, tempmeta = tempfile.mkstemp(prefix="metadata-", suffix=".partial", dir=cache)
        with os.fdopen(fd, "wb") as out:
            out.write(canonical(transfer))
        os.replace(tempmeta, metadata)
        return {**transfer, "package_id": identity, "filename": f"model-{identity[:16]}.zip"}
    finally:
        path.unlink(missing_ok=True)


def read_package(cache: Path, payload: dict):
    identity = payload["package_id"]
    if not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise BackendError("validation_error", "Invalid package identity")
    try:
        with safe_file(cache / f"{identity}.json") as source:
            raw = source.read(MAX_DESCRIPTOR_BYTES + 1)
        if len(raw) > MAX_DESCRIPTOR_BYTES:
            raise ValueError("Oversized transfer descriptor")
        transfer = json.loads(raw)
        if digest(transfer) != identity:
            raise ValueError("Transfer descriptor integrity mismatch")
        provenance = transfer["provenance"]
        for field in ("project_id", "backend_run_id", "model_version_id", "artifact_id"):
            if payload.get(field) != provenance.get(field):
                raise BackendError("not_found", "Package does not belong to this project, run, version and artifact")
        index = payload["chunk_index"]
        if type(index) is not int or not 0 <= index < len(transfer["chunk_sha256"]):
            raise BackendError("validation_error", "Chunk index is out of bounds")
        with safe_file(cache / f"{identity}.zip") as source:
            if os.fstat(source.fileno()).st_size != transfer["size_bytes"]:
                raise ValueError("Package size changed")
            source.seek(index * CHUNK_BYTES)
            chunk = source.read(CHUNK_BYTES)
        actual = hashlib.sha256(chunk).hexdigest()
        if actual != transfer["chunk_sha256"][index]:
            raise ValueError("Package chunk integrity mismatch")
        return {"package_id": identity, "chunk_index": index, "sha256": actual,
                "size_bytes": len(chunk), "data_base64": base64.b64encode(chunk).decode()}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise BackendError("export_integrity_error", "Export package is missing, unsafe or changed") from error
