"""Bounded-memory hashes of immutable operator-resolved dependency files.

Open every pathname component without following links and hash through that
descriptor. Reject file mutation or pathname replacement during verification.
This is a read-time witness, not a promise that the file stays immutable later.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat

READ_BYTES = 1024 * 1024
MAX_DEPTH = 128


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode


def _signature(info):
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def dependency_sha256(path: str | Path) -> str:
    """Hash at most the initial file size plus one byte with fixed-size reads."""
    if (os.name != "posix" or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd or os.stat not in os.supports_follow_symlinks
            or any(not hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"))):
        raise ValueError("Secure descriptor-relative dependency reads are unavailable")
    path = Path(path).absolute()
    parts = path.parts[1:]
    if not parts or len(parts) > MAX_DEPTH or any(part in {".", ".."} for part in parts):
        raise ValueError("Dependency path is invalid or exceeds its depth limit")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    handles, ancestors = [], []
    try:
        parent = os.open(path.anchor, directory_flags)
        handles.append(parent)
        for name in parts[:-1]:
            child = os.open(name, directory_flags, dir_fd=parent)
            handles.append(child)
            ancestors.append((parent, name, _identity(os.fstat(child))))
            parent = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        handles.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 0:
            raise ValueError("Dependency must be a regular file")
        digest, size = hashlib.sha256(), 0
        while chunk := os.read(descriptor, min(READ_BYTES, before.st_size + 1 - size)):
            size += len(chunk)
            if size > before.st_size:
                raise ValueError("Dependency changed while being read")
            digest.update(chunk)
        if (size != before.st_size or _signature(os.fstat(descriptor)) != _signature(before)
                or _signature(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)) != _signature(before)):
            raise ValueError("Dependency changed while being read")
        for directory, name, expected in ancestors:
            if _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) != expected:
                raise ValueError("Dependency directory changed while being read")
        return digest.hexdigest()
    except OSError as error:
        raise ValueError("Dependency is missing, unsafe, or unreadable") from error
    finally:
        for descriptor in reversed(handles):
            os.close(descriptor)
