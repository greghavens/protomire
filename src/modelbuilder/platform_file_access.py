"""Windows handle-relative dataset/export reads without following reparse points.

POSIX dataset reads stay in dataset.py. Windows has no Python ``dir_fd`` open;
this adapter uses the documented native NtCreateFile directory-handle contract.
It is deliberately fail-closed on filesystems lacking handle identity queries.
"""
from __future__ import annotations

import ctypes
import io
from dataclasses import dataclass
import os
from pathlib import PurePosixPath, PureWindowsPath
import re
import sys

from .models import ServiceError

# Fixed-width Windows ABI types also let contract tests run on non-Windows hosts.
_ULONG = ctypes.c_uint32
_LONG = ctypes.c_int32
_HANDLE = ctypes.c_void_p


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_uint16), ("MaximumLength", ctypes.c_uint16),
                ("Buffer", ctypes.c_void_p)]


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Length", _ULONG), ("RootDirectory", _HANDLE),
                ("ObjectName", ctypes.POINTER(_UNICODE_STRING)), ("Attributes", _ULONG),
                ("SecurityDescriptor", ctypes.c_void_p), ("SecurityQualityOfService", ctypes.c_void_p)]


class _IO_STATUS_UNION(ctypes.Union):
    _fields_ = [("Status", _LONG), ("Pointer", ctypes.c_void_p)]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [("Result", _IO_STATUS_UNION), ("Information", ctypes.c_size_t)]


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_uint64), ("FileId", ctypes.c_ubyte * 16)]


class _FILE_BASIC_INFO(ctypes.Structure):
    _fields_ = [("CreationTime", ctypes.c_int64), ("LastAccessTime", ctypes.c_int64),
                ("LastWriteTime", ctypes.c_int64), ("ChangeTime", ctypes.c_int64),
                ("FileAttributes", _ULONG)]


class _FILE_STANDARD_INFO(ctypes.Structure):
    _fields_ = [("AllocationSize", ctypes.c_int64), ("EndOfFile", ctypes.c_int64),
                ("NumberOfLinks", _ULONG), ("DeletePending", ctypes.c_ubyte),
                ("Directory", ctypes.c_ubyte)]


FILE_SHARE_READ = 0x1
FILE_OPEN = 0x1
FILE_DIRECTORY_FILE = 0x1
FILE_SYNCHRONOUS_IO_NONALERT = 0x20
FILE_NON_DIRECTORY_FILE = 0x40
FILE_OPEN_REPARSE_POINT = 0x00200000
FILE_OPEN_NO_RECALL = 0x00400000
FILE_READ_DATA = 0x1
FILE_LIST_DIRECTORY = 0x1
FILE_TRAVERSE = 0x20
FILE_READ_ATTRIBUTES = 0x80
SYNCHRONIZE = 0x00100000
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_TYPE_DISK = 0x1
OBJ_CASE_INSENSITIVE = 0x40
OBJ_DONT_REPARSE = 0x1000


@dataclass(frozen=True)
class WindowsRoot:
    """An authorized root identity, captured by a native handle at startup."""

    path: str
    identity: tuple[int, bytes]


@dataclass(frozen=True)
class _FileInfo:
    identity: tuple[int, bytes]
    size: int
    modified: int
    changed: int
    attributes: int
    directory: bool
    delete_pending: bool = False


class _WindowsAPI:
    """Small, typed Windows ABI boundary; no handles escape the bounded reader."""

    def __init__(self, *, kernel32=None, ntdll=None):
        if kernel32 is None or ntdll is None:
            if os.name != "nt":
                raise OSError("Native Windows file access is unavailable")
            # Explicit system DLL loading avoids any package dependency. winmode
            # restricts resolution to System32 (LOAD_LIBRARY_SEARCH_SYSTEM32).
            kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x800)
            ntdll = ctypes.WinDLL("ntdll.dll", use_last_error=True, winmode=0x800)
        self._kernel32, self._ntdll = kernel32, ntdll
        self._configure(ntdll.NtCreateFile, _LONG, [ctypes.POINTER(_HANDLE), _ULONG,
            ctypes.POINTER(_OBJECT_ATTRIBUTES), ctypes.POINTER(_IO_STATUS_BLOCK),
            ctypes.c_void_p, _ULONG, _ULONG, _ULONG, _ULONG, ctypes.c_void_p, _ULONG])
        self._configure(ntdll.RtlNtStatusToDosError, _ULONG, [_LONG])
        self._configure(kernel32.GetFileType, _ULONG, [_HANDLE])
        self._configure(kernel32.GetFileInformationByHandleEx, _LONG,
                        [_HANDLE, ctypes.c_int, ctypes.c_void_p, _ULONG])
        self._configure(kernel32.ReadFile, _LONG,
                        [_HANDLE, ctypes.c_void_p, _ULONG, ctypes.POINTER(_ULONG), ctypes.c_void_p])
        self._configure(kernel32.CloseHandle, _LONG, [_HANDLE])

    @staticmethod
    def _configure(function, result, arguments):
        function.restype, function.argtypes = result, arguments

    @staticmethod
    def _error(code=None):
        # Do not include source paths or filenames in errors crossing the service.
        if code is None:
            code = ctypes.get_last_error()
        return OSError(code, "Native Windows file access failed")

    def _open(self, name: str, parent: int | None, *, directory: bool) -> int:
        try:
            encoded = name.encode("utf-16-le", errors="strict")
        except UnicodeEncodeError as exc:
            raise OSError("Unsafe Windows filename") from exc
        if not encoded or len(encoded) > 65532 or "\x00" in name:
            raise OSError("Unsafe Windows filename")
        # UTF-16 bytes, not host wchar_t, keep the ABI independently testable.
        buffer = ctypes.create_string_buffer(encoded + b"\x00\x00")
        unicode_name = _UNICODE_STRING(len(encoded), len(encoded) + 2, ctypes.addressof(buffer))
        # Absolute DOS/UNC roots traverse an object-manager device alias;
        # OBJ_DONT_REPARSE can reject that alias on Windows 10. Once the root
        # identity is verified, all child opens also refuse path reparsing.
        flags = OBJ_CASE_INSENSITIVE | (OBJ_DONT_REPARSE if parent is not None else 0)
        attributes = _OBJECT_ATTRIBUTES(ctypes.sizeof(_OBJECT_ATTRIBUTES), parent,
            ctypes.pointer(unicode_name), flags, None, None)
        handle, status_block = _HANDLE(), _IO_STATUS_BLOCK()
        access = FILE_READ_ATTRIBUTES | SYNCHRONIZE
        access |= (FILE_LIST_DIRECTORY | FILE_TRAVERSE) if directory else FILE_READ_DATA
        options = FILE_SYNCHRONOUS_IO_NONALERT | FILE_OPEN_REPARSE_POINT | FILE_OPEN_NO_RECALL
        options |= FILE_DIRECTORY_FILE if directory else FILE_NON_DIRECTORY_FILE
        status = self._ntdll.NtCreateFile(ctypes.byref(handle), access, ctypes.byref(attributes),
            ctypes.byref(status_block), None, 0, FILE_SHARE_READ, FILE_OPEN, options, None, 0)
        # NTSTATUS is a signed 32-bit value, including when using a test double.
        if ctypes.c_int32(status).value < 0:
            raise self._error(self._ntdll.RtlNtStatusToDosError(status))
        if status != 0 or not handle.value or handle.value == ctypes.c_void_p(-1).value:
            # We requested synchronous I/O, with no option allowing pending or
            # alternate-success opens. Never use an incompletely opened handle.
            if handle.value and handle.value != ctypes.c_void_p(-1).value:
                self.close(handle.value)
            raise OSError("Native Windows file open did not complete")
        return handle.value

    def open_root(self, path: str) -> int:
        return self._open(_nt_root_name(path), None, directory=True)

    def open_child(self, parent: int, name: str, *, directory: bool) -> int:
        _validate_component(name)
        return self._open(name, parent, directory=directory)

    def info(self, handle: int) -> _FileInfo:
        if self._kernel32.GetFileType(handle) != FILE_TYPE_DISK:
            raise OSError("Dataset handles must refer to disk files")
        identity, basic, standard = _FILE_ID_INFO(), _FILE_BASIC_INFO(), _FILE_STANDARD_INFO()
        for kind, value in ((18, identity), (0, basic), (1, standard)):
            if not self._kernel32.GetFileInformationByHandleEx(handle, kind, ctypes.byref(value),
                                                              ctypes.sizeof(value)):
                raise self._error()
        return _FileInfo((identity.VolumeSerialNumber, bytes(identity.FileId)), standard.EndOfFile,
            basic.LastWriteTime, basic.ChangeTime, basic.FileAttributes, bool(standard.Directory),
            bool(standard.DeletePending))

    def read(self, handle: int, size: int) -> bytes:
        if not 0 < size <= 65536:
            raise ValueError("Windows dataset reads must be positive bounded chunks")
        buffer, count = ctypes.create_string_buffer(size), _ULONG()
        if not self._kernel32.ReadFile(handle, buffer, size, ctypes.byref(count), None):
            raise self._error()
        if count.value > size:
            raise OSError("Invalid native read length")
        return buffer.raw[:count.value]

    def close(self, handle: int) -> None:
        if not self._kernel32.CloseHandle(handle):
            raise self._error()


def _nt_root_name(path: str) -> str:
    """Convert only absolute drive/UNC filesystem paths to native NT syntax."""
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    value = PureWindowsPath(path)
    drive = value.drive
    local_drive = bool(re.fullmatch(r"[A-Za-z]:", drive))
    unc = drive.startswith("\\\\") and len(drive[2:].split("\\")) == 2
    if (not value.is_absolute() or not (local_drive or unc) or "\x00" in path
            or any(part == ".." for part in value.parts)
            or path.startswith("\\\\.\\") or (unc and any(c in drive for c in '?*:'))):
        raise OSError("Dataset root must be an absolute Windows filesystem path")
    normalized = str(value)
    return "\\??\\" + normalized if local_drive else "\\??\\UNC\\" + normalized[2:]


def _validate_component(part: str) -> None:
    # No Win32 aliases, ADS, namespaces, wildcard resolution, separators, or
    # traversal. Native relative opens see exactly one canonical component.
    stem = part.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³")
    if (not part or part in {".", ".."} or part.endswith((".", " ")) or stem in reserved
            or any(character in part for character in '/\\:*?"<>|')
            or any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF for character in part)):
        raise OSError("Unsafe Windows dataset path component")


def _parts(relative_path: str) -> tuple[str, ...]:
    value = PurePosixPath(relative_path)
    if value.is_absolute() or not value.parts or str(value) != relative_path:
        raise OSError("Dataset source must use a canonical relative path")
    for part in value.parts:
        _validate_component(part)
    return value.parts


def _check_kind(info: _FileInfo, *, directory: bool) -> None:
    if info.attributes & FILE_ATTRIBUTE_REPARSE_POINT or info.delete_pending:
        raise OSError("Reparse points and pending deletions are not dataset sources")
    if info.directory != directory or bool(info.attributes & FILE_ATTRIBUTE_DIRECTORY) != directory:
        raise ServiceError("dataset_source_invalid", "Dataset source must be a regular file")
    if info.size < 0:
        raise OSError("Invalid dataset file size")


def snapshot_windows_root(path, *, _api=None) -> WindowsRoot:
    """Snapshot the directory's native 128-bit identity, not Python stat fields."""
    api = _api if _api is not None else _WindowsAPI()
    handle = api.open_root(str(path))
    try:
        info = api.info(handle)
        _check_kind(info, directory=True)
        return WindowsRoot(str(path), info.identity)
    finally:
        active_error = sys.exception() is not None
        try:
            api.close(handle)
        except OSError:
            if not active_error:
                raise


def read_windows_source(root: WindowsRoot, relative_path: str, max_bytes: int, *, _api=None) -> bytes:
    """Read once from the same bounded, checked handle under a pinned root.

    All opened ancestors stay alive with no data-write/delete sharing until the read
    finishes. A root path swap fails identity validation; a child junction or
    symlink is opened without following and rejected before reading/traversing.
    The caller must verify the returned bytes against the manifest content hash.
    """
    parts = _parts(relative_path)
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("Dataset byte limit must be a nonnegative integer")
    api = _api if _api is not None else _WindowsAPI()
    handles = []
    try:
        parent = api.open_root(root.path)
        handles.append(parent)
        root_info = api.info(parent)
        _check_kind(root_info, directory=True)
        if root_info.identity != root.identity:
            raise ServiceError("dataset_root_changed", "Configured dataset root changed since service startup")
        for part in parts[:-1]:
            parent = api.open_child(parent, part, directory=True)
            handles.append(parent)
            _check_kind(api.info(parent), directory=True)
        handle = api.open_child(parent, parts[-1], directory=False)
        handles.append(handle)
        before = api.info(handle)
        _check_kind(before, directory=False)
        if before.size > max_bytes:
            raise ServiceError("dataset_limit_exceeded", "Dataset source exceeds the configured byte limit")
        chunks, size = [], 0
        while chunk := api.read(handle, min(65536, max_bytes + 1 - size)):
            size += len(chunk)
            if size > max_bytes:
                raise ServiceError("dataset_limit_exceeded", "Dataset source exceeds the configured byte limit")
            chunks.append(chunk)
        after = api.info(handle)
        _check_kind(after, directory=False)
        if before != after or size != before.size:
            raise ServiceError("dataset_source_changed", "Dataset source changed while it was being read")
        return b"".join(chunks)
    finally:
        # Close every ancestor; never obscure the original validation/read error.
        active_error = sys.exception() is not None
        error = None
        for handle in reversed(handles):
            try:
                api.close(handle)
            except OSError as exc:
                error = error or exc
        if error is not None and not active_error:
            raise error


class _WindowsFileReader(io.RawIOBase):
    """Streaming view over one checked HANDLE, retaining its locked ancestors.

    The final HANDLE is adopted by the CRT/FileIO rather than reopened by path.
    This supplies Python's portable seek/fileno API without a second native path
    lookup or loading an entire multi-gigabyte export into memory.
    """

    def __init__(self, source, api, handle, ancestors, before, max_bytes):
        super().__init__()
        self._source, self._api, self._handle = source, api, handle
        self._ancestors, self._before, self._max_bytes = ancestors, before, max_bytes
        self._position = 0

    def _verify(self):
        after = self._api.info(self._handle)
        _check_kind(after, directory=False)
        if after != self._before:
            raise OSError("Windows source changed during a verified export read")

    def readable(self):
        return True

    def seekable(self):
        return True

    def fileno(self):
        self._checkClosed()
        return self._source.fileno()

    def tell(self):
        self._checkClosed()
        return self._position

    def seek(self, offset, whence=os.SEEK_SET):
        self._checkClosed()
        self._verify()
        if type(offset) is not int or whence not in {os.SEEK_SET, os.SEEK_CUR, os.SEEK_END}:
            raise ValueError("Invalid export seek")
        position = offset + (self._position if whence == os.SEEK_CUR else
                             self._before.size if whence == os.SEEK_END else 0)
        if not 0 <= position <= self._before.size:
            raise ValueError("Export seek exceeds the verified file bounds")
        actual = self._source.seek(position, os.SEEK_SET)
        if actual != position:
            raise OSError("Windows export seek did not reach the requested offset")
        self._verify()
        self._position = position
        return position

    def readinto(self, buffer):
        self._checkClosed()
        if not len(buffer):
            return 0
        self._verify()
        # Every OS read stays small even when BufferedReader assembles a 4 MiB
        # transfer chunk. Read at most one extra byte to detect unexpected growth.
        count = min(len(buffer), 65536, self._max_bytes + 1 - self._position)
        if count <= 0:
            raise OSError("Windows export exceeded its byte limit")
        read = self._source.readinto(memoryview(buffer)[:count])
        if read is None or not 0 <= read <= count:
            raise OSError("Invalid Windows export read length")
        self._position += read
        if self._position > self._before.size or (read == 0 and self._position != self._before.size):
            raise OSError("Windows export changed size during read")
        self._verify()
        return read

    def close(self):
        if self.closed:
            return
        active_error, failure = sys.exception() is not None, None
        try:
            self._verify()
        except (OSError, ServiceError) as error:
            failure = error
        finally:
            try:
                # FileIO owns and closes the adopted file HANDLE exactly once.
                self._source.close()
            except OSError as error:
                failure = failure or error
            for handle in reversed(self._ancestors):
                try:
                    self._api.close(handle)
                except OSError as error:
                    failure = failure or error
            super().close()
        if failure is not None and not active_error:
            raise failure


def _adopt_windows_file(handle):
    """Transfer ownership of the already checked native HANDLE to FileIO."""
    import msvcrt  # Built in on Windows; never imported on POSIX.
    descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    try:
        return io.FileIO(descriptor, mode="rb", closefd=True)
    except BaseException as error:
        try:
            os.close(descriptor)
        except OSError:
            pass
        # The CRT already took ownership. Tell the caller not to CloseHandle
        # this value again (a recycled HANDLE could belong to another thread).
        failure = OSError("Could not adopt the verified Windows file handle")
        failure.handle_consumed = True
        raise failure from error


def open_windows_source(root: WindowsRoot, relative_path: str, max_bytes: int, *, _api=None, _adopt=None):
    """Open a bounded, seekable stream with the dataset reader's native protections.

    The caller owns the returned BinaryIO and must close it. All ancestor handles
    are retained until close with read-only sharing. This API is intended for
    multi-gigabyte exports; read_windows_source remains the bounded bytes API.
    """
    parts = _parts(relative_path)
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("File byte limit must be a nonnegative integer")
    api, handles = _api if _api is not None else _WindowsAPI(), []
    source = None
    try:
        parent = api.open_root(root.path)
        handles.append(parent)
        root_info = api.info(parent)
        _check_kind(root_info, directory=True)
        if root_info.identity != root.identity:
            raise OSError("Windows export root changed before opening the file")
        for part in parts[:-1]:
            parent = api.open_child(parent, part, directory=True)
            handles.append(parent)
            _check_kind(api.info(parent), directory=True)
        handle = api.open_child(parent, parts[-1], directory=False)
        handles.append(handle)
        before = api.info(handle)
        _check_kind(before, directory=False)
        if before.size > max_bytes:
            raise OSError("Windows export exceeds its byte limit")
        adopt = _adopt if _adopt is not None else _adopt_windows_file
        try:
            source = adopt(handle)
        except BaseException as error:
            if getattr(error, "handle_consumed", False):
                handles.pop()
            raise
        handles.pop()  # FileIO now owns the final HANDLE; ancestors remain ours.
        raw = _WindowsFileReader(source, api, handle, handles, before, max_bytes)
        handles, source = [], None  # The raw reader now owns all resources.
        try:
            return io.BufferedReader(raw, buffer_size=65536)
        except BaseException:
            raw.close()
            raise
    except BaseException:
        if source is not None:
            source.close()
        for handle in reversed(handles):
            try:
                api.close(handle)
            except OSError:
                pass
        raise
