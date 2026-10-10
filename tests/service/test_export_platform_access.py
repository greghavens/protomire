"""Portable export streaming contracts; mocks do not certify a native OS."""
from dataclasses import replace
import hashlib
import io
import os
from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from modelbuilder import export_packages as exports
from modelbuilder import platform_file_access as access


DIRECTORY = access._FileInfo((7, b"root".ljust(16, b"0")), 0, 10, 20,
                             access.FILE_ATTRIBUTE_DIRECTORY, True)
ROOT = access.WindowsRoot("C:\\", DIRECTORY.identity)


class WindowsContractAPI:
    def __init__(self, content):
        self.content, self.opened, self.closed = content, [], []
        self.before = access._FileInfo((7, b"file".ljust(16, b"0")), len(content), 30, 40, 0, False)
        self.infos = {10: DIRECTORY, 11: replace(DIRECTORY, identity=(7, b"nested".ljust(16, b"0"))), 12: self.before}
        self.fail = None
        self.source = None

    def open_root(self, path):
        self.opened.append(("root", path))
        return 10

    def open_child(self, parent, name, *, directory):
        self.opened.append((parent, name, directory))
        if self.fail == name:
            raise OSError("Unsafe or unavailable child")
        return 11 if directory else 12

    def info(self, handle):
        return self.infos[handle]

    def close(self, handle):
        self.closed.append(handle)

    def adopt(self, handle):
        assert handle == 12
        api = self
        class SameHandleFile(io.BytesIO):
            def __init__(self):
                super().__init__(api.content)
                self.read_sizes = []

            def readinto(self, buffer):
                self.read_sizes.append(len(buffer))
                return super().readinto(buffer)

            def close(self):
                if not self.closed:
                    api.closed.append(handle)
                super().close()
        self.source = SameHandleFile()
        return self.source


def opened(content=b"verified export bytes", *, maximum=1000000, api=None):
    api = api or WindowsContractAPI(content)
    return access.open_windows_source(ROOT, "exports/weights.bin", maximum, _api=api, _adopt=api.adopt), api


def test_windows_stream_keeps_ancestors_and_hashes_same_bounded_handle():
    content = b"0123456789" * 30000
    stream, api = opened(content)
    with stream:
        assert api.closed == []
        assert hashlib.file_digest(stream, "sha256").hexdigest() == hashlib.sha256(content).hexdigest()
        assert api.closed == []
        assert api.opened == [("root", "C:\\"), (10, "exports", True), (11, "weights.bin", False)]
        assert max(api.source.read_sizes) <= 65536
    assert api.closed == [12, 11, 10]
    stream.close()
    assert api.closed == [12, 11, 10]


def test_windows_stream_seek_supports_bounded_package_chunks_without_prefix_reads():
    content = b"a" * 100000 + b"TAIL"
    stream, api = opened(content)
    with stream:
        assert stream.seek(100000) == 100000
        assert stream.read(4) == b"TAIL"
        assert sum(api.source.read_sizes) <= 65536
        assert stream.seek(-4, os.SEEK_END) == 100000
        assert stream.read() == b"TAIL"
        assert stream.seek(0) == 0
        assert stream.read(3) == b"aaa"
        for offset in (-1, len(content) + 1):
            with pytest.raises(ValueError, match="bounds"):
                stream.seek(offset)


@pytest.mark.parametrize("handle", [10, 11, 12])
def test_windows_stream_rejects_each_reparse_ancestor_before_adoption(handle):
    api = WindowsContractAPI(b"bytes")
    api.infos[handle] = replace(api.infos[handle], attributes=api.infos[handle].attributes | access.FILE_ATTRIBUTE_REPARSE_POINT)
    with pytest.raises(OSError, match="Reparse"):
        opened(api=api)
    assert api.source is None
    assert api.closed == list(reversed([10, 11, 12][:handle - 9]))


def test_windows_stream_rejects_root_replacement_before_opening_child():
    api = WindowsContractAPI(b"bytes")
    api.infos[10] = replace(DIRECTORY, identity=(8, DIRECTORY.identity[1]))
    with pytest.raises(OSError, match="root changed"):
        opened(api=api)
    assert api.opened == [("root", "C:\\")]
    assert api.closed == [10]


@pytest.mark.parametrize("field,value", [("identity", (8, b"changed-id-123456")), ("size", 99),
    ("modified", 999), ("changed", 888), ("attributes", 0x20)])
def test_windows_stream_fails_changed_identity_or_metadata_before_returning_bytes(field, value):
    stream, api = opened()
    try:
        api.infos[12] = replace(api.before, **{field: value})
        with pytest.raises(OSError, match="changed"):
            stream.read(1)
    finally:
        with pytest.raises(OSError, match="changed"):
            stream.close()
    assert api.closed == [12, 11, 10]


def test_windows_stream_checks_final_metadata_even_without_reading_to_eof():
    stream, api = opened()
    assert stream.read(1)
    api.infos[12] = replace(api.before, changed=999)
    with pytest.raises(OSError, match="changed"):
        stream.close()
    assert api.closed == [12, 11, 10]


def test_windows_stream_preserves_original_error_and_closes_every_handle():
    with pytest.raises(RuntimeError, match="original"):
        stream, api = opened()
        with stream:
            api.infos[12] = replace(api.before, changed=999)
            raise RuntimeError("original")
    assert api.closed == [12, 11, 10]


@pytest.mark.parametrize("actual", [b"short", b"longer than expected"])
def test_windows_stream_rejects_short_or_growing_bytes_even_when_metadata_lies(actual):
    api = WindowsContractAPI(actual)
    api.infos[12] = replace(api.before, size=10)
    with opened(api=api)[0] as stream:
        with pytest.raises(OSError, match="size"):
            stream.read(100)


def test_windows_stream_bounds_checked_before_adoption_and_closes_on_failed_open():
    api = WindowsContractAPI(b"too big")
    with pytest.raises(OSError, match="byte limit"):
        opened(api=api, maximum=3)
    assert api.source is None and api.closed == [12, 11, 10]
    api = WindowsContractAPI(b"bytes")
    api.fail = "weights.bin"
    with pytest.raises(OSError):
        opened(api=api)
    assert api.closed == [11, 10]


@pytest.mark.parametrize("consumed", [False, True])
def test_adoption_failure_never_double_closes_a_consumed_handle(consumed):
    api = WindowsContractAPI(b"bytes")
    def fail(handle):
        error = OSError("Adoption failure")
        error.handle_consumed = consumed
        if consumed:
            api.closed.append(handle)
        raise error
    with pytest.raises(OSError):
        access.open_windows_source(ROOT, "exports/weights.bin", 100, _api=api, _adopt=fail)
    assert api.closed == [12, 11, 10]


def test_export_dispatch_uses_windows_anchor_and_relative_components_without_posix_open(monkeypatch):
    recorded = []
    monkeypatch.setattr(exports, "_WINDOWS", True)
    monkeypatch.setattr(access, "snapshot_windows_root", lambda anchor: recorded.append(("root", anchor)) or ROOT)
    result = io.BytesIO(b"safe")
    monkeypatch.setattr(access, "open_windows_source", lambda root, relative, maximum: recorded.append((root, relative, maximum)) or result)
    path = SimpleNamespace(absolute=lambda: PureWindowsPath(r"C:\exports\nested\weights.bin"))
    assert exports.safe_file(path, max_bytes=123) is result
    assert recorded == [("root", "C:\\"), (ROOT, "exports/nested/weights.bin", 123)]


def test_windows_native_reader_failure_never_falls_back_to_ordinary_path_open(monkeypatch):
    monkeypatch.setattr(exports, "_WINDOWS", True)
    def denied(*args):
        raise OSError("No native identity available")
    monkeypatch.setattr(access, "snapshot_windows_root", denied)
    path = SimpleNamespace(absolute=lambda: PureWindowsPath(r"C:\exports\weights.bin"))
    with pytest.raises(OSError, match="native identity"):
        exports.safe_file(path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-follow implementation is exercised on POSIX hosts")
def test_posix_reader_remains_no_follow_and_applies_pre_read_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(exports, "_WINDOWS", False)
    path = tmp_path / "weights.bin"
    path.write_bytes(b"abc")
    with exports.safe_file(path, max_bytes=3) as stream:
        assert stream.read() == b"abc"
    with pytest.raises(ValueError, match="bounded"):
        exports.safe_file(path, max_bytes=2)
    symlink = tmp_path / "redirect"
    symlink.symlink_to(path)
    with pytest.raises(OSError):
        exports.safe_file(symlink)


@pytest.mark.skipif(os.name != "nt", reason="Requires actual native Windows HANDLE and CRT ownership semantics")
def test_native_windows_export_read_seek_hash_and_sharing(tmp_path):
    path = tmp_path / "weights.bin"
    content = b"abcdef" * 30000
    path.write_bytes(content)
    with exports.safe_file(path, max_bytes=len(content)) as stream:
        assert os.fstat(stream.fileno()).st_size == len(content)
        assert stream.seek(100000) == 100000
        assert stream.read(6) == content[100000:100006]
        stream.seek(0)
        assert hashlib.file_digest(stream, "sha256").hexdigest() == hashlib.sha256(content).hexdigest()
        with pytest.raises(OSError):
            path.write_bytes(b"changed")
        with pytest.raises(OSError):
            path.rename(tmp_path / "moved.bin")
    path.rename(tmp_path / "moved.bin")


@pytest.mark.skipif(os.name != "nt", reason="Requires actual native Windows export/package filesystem reads")
def test_native_windows_inspect_package_and_authenticated_chunk_bytes(tmp_path):
    import base64
    import json
    import zipfile
    root = tmp_path / "export"
    root.mkdir()
    assets = {"loader.py": b"# Loader is inspected, never executed\n", "weights.bin": b"weights" * 100000}
    for name, content in assets.items():
        (root / name).write_bytes(content)
    manifest = {"schema_version": "modelbuilder.export/v1", "selected_checkpoint": "selected",
                "loader": {"path": "loader.py"}, "signature": {"inputs": "tensor"},
                "dependencies": {"framework": "pinned"}, "source": {"commit": "immutable"},
                "verification": {"passed": True, "atol": 0},
                "assets": [{"path": name, "sha256": hashlib.sha256(content).hexdigest()}
                           for name, content in assets.items()]}
    (root / "export.manifest.json").write_text(json.dumps(manifest))
    provenance = {"project_id": "project", "backend_run_id": "run", "model_version_id": "version", "artifact_id": "artifact"}
    descriptor = exports.inspect_export(root, provenance)
    transfer = exports.package_export(root, tmp_path / "cache", descriptor, descriptor["export_digest"])
    downloaded = b"".join(base64.b64decode(exports.read_package(tmp_path / "cache", {
        **provenance, "package_id": transfer["package_id"], "chunk_index": index})["data_base64"])
        for index in range(len(transfer["chunk_sha256"])))
    assert hashlib.sha256(downloaded).hexdigest() == transfer["sha256"]
    with zipfile.ZipFile(io.BytesIO(downloaded)) as archive:
        for name, content in assets.items():
            assert archive.read(name) == content


def test_crt_adoption_reuses_the_native_handle_and_transfers_descriptor_ownership(tmp_path, monkeypatch):
    import sys
    path = tmp_path / "binary.bin"
    path.write_bytes(b"a\r\nb\x00c")
    descriptor = os.open(path, os.O_RDONLY)
    calls = []
    def adopt(handle, flags):
        calls.append((handle, flags))
        return descriptor
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=adopt))
    monkeypatch.setattr(os, "O_BINARY", 0x8000, raising=False)
    native_handle = 2**40 + 123
    with access._adopt_windows_file(native_handle) as source:
        assert source.fileno() == descriptor
        assert source.read() == b"a\r\nb\x00c"
    assert calls == [(native_handle, os.O_RDONLY | 0x8000)]
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_crt_constructor_failure_marks_native_handle_as_consumed(tmp_path, monkeypatch):
    import sys
    path = tmp_path / "binary.bin"
    path.write_bytes(b"bytes")
    descriptor = os.open(path, os.O_RDONLY)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=lambda handle, flags: descriptor))
    monkeypatch.setattr(os, "O_BINARY", 0x8000, raising=False)
    def fail(*args, **kwargs):
        raise OSError("FileIO initialization failed")
    monkeypatch.setattr(io, "FileIO", fail)
    with pytest.raises(OSError) as error:
        access._adopt_windows_file(123)
    assert error.value.handle_consumed is True
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_export_reader_import_does_not_require_posix_execution_module(tmp_path):
    """A Windows service import must not eagerly load the POSIX-only native SDK."""
    import subprocess
    import sys
    from pathlib import Path
    source = Path(__file__).resolve().parents[2] / 'src'
    code = '''import sys
sys.path.insert(0, sys.argv[1])
sys.modules['fcntl'] = None
from modelbuilder import export_packages, platform_file_access
from modelbuilder.backend import adapter
assert adapter.fcntl is None
assert callable(export_packages.safe_file)
assert callable(platform_file_access.open_windows_source)
assert 'modelbuilder.backend.sdk' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-I', '-c', code, str(source)], cwd=tmp_path,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
