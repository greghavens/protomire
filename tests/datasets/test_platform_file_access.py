"""Windows ABI/algorithm contracts; mocks are not native Windows acceptance."""
import ctypes
from dataclasses import replace
import os
from types import SimpleNamespace

import pytest

from modelbuilder.models import ServiceError

from modelbuilder import platform_file_access as access


DIRECTORY = access._FileInfo((5, b'root'.ljust(16, b'0')), 0, 10, 20,
                             access.FILE_ATTRIBUTE_DIRECTORY, True)
CONTENT = b'project-specific content\n'
REGULAR = access._FileInfo((5, b'file'.ljust(16, b'0')), len(CONTENT), 30, 40, 0, False)
ROOT = access.WindowsRoot(r'C:\project\data', DIRECTORY.identity)


class FakeAPI:
    def __init__(self):
        self.opens, self.closes, self.reads, self.stats = [], [], [], []
        self.position = 0
        self.metadata = {10: DIRECTORY, 11: replace(DIRECTORY, identity=(5, b'child'.ljust(16, b'0'))),
                         12: REGULAR}
        self.content = CONTENT
        self.fail_open = None
        self.after = None

    def open_root(self, path):
        self.opens.append(('root', path))
        return 10

    def open_child(self, parent, name, *, directory):
        self.opens.append((parent, name, directory))
        if self.fail_open == name:
            raise OSError('simulated unsafe or unavailable source')
        return 11 if directory else 12

    def info(self, handle):
        self.stats.append(handle)
        if handle == 12 and self.after is not None and self.stats.count(12) > 1:
            return self.after
        return self.metadata[handle]

    def read(self, handle, size):
        self.reads.append((handle, size))
        result = self.content[self.position:self.position + size]
        self.position += len(result)
        return result

    def close(self, handle):
        self.closes.append(handle)


def test_same_handle_bounded_read_and_reverse_cleanup():
    api = FakeAPI()
    assert access.read_windows_source(ROOT, 'nested/data.jsonl', 100, _api=api) == CONTENT
    assert api.opens == [('root', ROOT.path), (10, 'nested', True), (11, 'data.jsonl', False)]
    assert api.reads == [(12, 101), (12, 101 - len(CONTENT))]
    assert api.stats == [10, 11, 12, 12]
    assert api.closes == [12, 11, 10]


def test_root_snapshot_uses_native_identity_and_releases_handle():
    api = FakeAPI()
    assert access.snapshot_windows_root(ROOT.path, _api=api) == ROOT
    assert api.closes == [10]


def test_root_replacement_refused_before_child_open():
    api = FakeAPI()
    api.metadata[10] = replace(DIRECTORY, identity=(6, DIRECTORY.identity[1]))
    with pytest.raises(ServiceError, match='changed') as exc:
        access.read_windows_source(ROOT, 'data.jsonl', 100, _api=api)
    assert exc.value.code == 'dataset_root_changed'
    assert api.opens == [('root', ROOT.path)] and not api.reads
    assert api.closes == [10]


@pytest.mark.parametrize('handle', [10, 11, 12])
def test_any_reparse_point_rejected_before_traverse_or_read(handle):
    api = FakeAPI()
    api.metadata[handle] = replace(api.metadata[handle],
        attributes=api.metadata[handle].attributes | access.FILE_ATTRIBUTE_REPARSE_POINT)
    with pytest.raises(OSError, match='Reparse'):
        access.read_windows_source(ROOT, 'nested/data.jsonl', 100, _api=api)
    assert not api.reads
    assert api.closes == list(reversed([10, 11, 12][:handle - 9]))
    if handle == 11:
        assert not any(item[1] == 'data.jsonl' for item in api.opens)


@pytest.mark.parametrize('field,value', [('identity', (7, b'changed-id-123456')),
    ('size', len(CONTENT) + 1), ('modified', 31), ('changed', 41), ('attributes', 0x20)])
def test_identity_size_write_time_change_time_and_attributes_checked_after_read(field, value):
    api = FakeAPI()
    api.after = replace(REGULAR, **{field: value})
    with pytest.raises(ServiceError) as exc:
        access.read_windows_source(ROOT, 'data.jsonl', 100, _api=api)
    assert exc.value.code == 'dataset_source_changed'
    assert api.closes == [12, 10]


def test_short_read_is_not_accepted_even_when_metadata_stays_same():
    api = FakeAPI()
    api.content = CONTENT[:-1]
    with pytest.raises(ServiceError) as exc:
        access.read_windows_source(ROOT, 'data.jsonl', 100, _api=api)
    assert exc.value.code == 'dataset_source_changed'


@pytest.mark.parametrize('initial_size', [0, len(CONTENT)])
def test_byte_limit_applies_to_metadata_and_actual_read(initial_size):
    api = FakeAPI()
    api.metadata[12] = replace(REGULAR, size=initial_size)
    with pytest.raises(ServiceError) as exc:
        access.read_windows_source(ROOT, 'data.jsonl', 4, _api=api)
    assert exc.value.code == 'dataset_limit_exceeded'
    assert all(size <= 5 for _, size in api.reads)
    assert api.closes == [12, 10]


def test_read_chunk_size_bounded_at_64_kib_and_exact_limit_allowed():
    api = FakeAPI()
    api.content = b'x' * 100000
    api.metadata[12] = replace(REGULAR, size=len(api.content))
    assert len(access.read_windows_source(ROOT, 'data.jsonl', len(api.content), _api=api)) == 100000
    assert api.reads == [(12, 65536), (12, 34465), (12, 1)]


def test_empty_file_and_zero_byte_limit():
    api = FakeAPI()
    api.content = b''
    api.metadata[12] = replace(REGULAR, size=0)
    assert access.read_windows_source(ROOT, 'empty.jsonl', 0, _api=api) == b''
    assert api.reads == [(12, 1)]


def test_failed_child_open_closes_every_preceding_directory():
    api = FakeAPI()
    api.fail_open = 'data.jsonl'
    with pytest.raises(OSError):
        access.read_windows_source(ROOT, 'nested/data.jsonl', 100, _api=api)
    assert api.closes == [11, 10]


@pytest.mark.parametrize('path', ['', '.', '..', '../a', './a', 'a//b', '/a', 'a/../b',
    r'a\b', 'C:/a', 'file:stream', 'file ', 'file.', 'NUL', 'nul.txt', 'LPT1.csv', 'COM¹',
    'CONIN$', 'a/\x00b', 'a/*', 'a/?', 'a/"', 'a/|', 'a/\ud800'])
def test_invalid_paths_never_open_any_handle(path):
    api = FakeAPI()
    with pytest.raises(OSError):
        access.read_windows_source(ROOT, path, 100, _api=api)
    assert not api.opens


@pytest.mark.parametrize('path,expected', [
    (r'C:\project\data', r'\??\C:\project\data'),
    (r'\\?\C:\project\data', r'\??\C:\project\data'),
    (r'\\server\share\data', r'\??\UNC\server\share\data'),
    (r'\\?\UNC\server\share\data', r'\??\UNC\server\share\data')])
def test_absolute_root_conversion(path, expected):
    assert access._nt_root_name(path) == expected


@pytest.mark.parametrize('path', ['data', r'C:data', r'C:\..\data', r'\\.\PhysicalDrive0',
    r'\\?\GLOBALROOT\Device\HarddiskVolume1', r'\Device\HarddiskVolume1', 'C:\\data\x00'])
def test_root_namespace_and_traversal_refused(path):
    with pytest.raises(OSError):
        access._nt_root_name(path)


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


def abi_api(**overrides):
    funcs = dict(NtCreateFile=lambda *args: 0, RtlNtStatusToDosError=lambda status: 5,
                 GetFileType=lambda handle: access.FILE_TYPE_DISK,
                 GetFileInformationByHandleEx=lambda *args: 1,
                 ReadFile=lambda *args: 1, CloseHandle=lambda handle: 1)
    funcs.update(overrides)
    dll = SimpleNamespace(**{key: Function(value) for key, value in funcs.items()})
    return access._WindowsAPI(kernel32=dll, ntdll=dll), dll


def test_native_open_abi_is_single_component_handle_relative_nonfollowing_read_only():
    observed = []
    def create(handle, desired, attributes, status, allocation, attrs, share, disposition,
               options, ea, ea_length):
        obj = ctypes.cast(attributes, ctypes.POINTER(access._OBJECT_ATTRIBUTES)).contents
        name = obj.ObjectName.contents
        observed.append((obj.RootDirectory, ctypes.string_at(name.Buffer, name.Length).decode('utf-16-le'),
            desired, share, disposition, options, obj.Attributes, obj.Length))
        assert allocation is None and attrs == 0 and ea is None and ea_length == 0
        ctypes.cast(handle, ctypes.POINTER(ctypes.c_void_p))[0] = 123
        return 0
    api, dll = abi_api(NtCreateFile=create)
    assert api.open_child(88, 'data-😀.jsonl', directory=False) == 123
    assert api.open_child(88, 'nested', directory=True) == 123
    for parent, name, desired, share, disposition, options, obj_flags, length in observed:
        assert parent == 88
        assert '\\' not in name and '/' not in name
        assert desired & access.FILE_READ_ATTRIBUTES and desired & access.SYNCHRONIZE
        assert share == access.FILE_SHARE_READ
        assert disposition == access.FILE_OPEN
        assert options & access.FILE_OPEN_REPARSE_POINT
        assert options & access.FILE_OPEN_NO_RECALL
        assert options & access.FILE_SYNCHRONOUS_IO_NONALERT
        assert obj_flags == access.OBJ_CASE_INSENSITIVE | access.OBJ_DONT_REPARSE
        assert length == ctypes.sizeof(access._OBJECT_ATTRIBUTES)
    assert observed[0][5] & access.FILE_NON_DIRECTORY_FILE
    assert observed[1][5] & access.FILE_DIRECTORY_FILE
    assert dll.NtCreateFile.restype is ctypes.c_int32
    assert len(dll.NtCreateFile.argtypes) == 11


def test_native_error_status_is_signed_32_bit():
    api, _ = abi_api(NtCreateFile=lambda *args: 0xC0000022)
    with pytest.raises(OSError) as exc:
        api.open_child(10, 'data', directory=False)
    assert exc.value.errno == 5


def test_native_incomplete_status_never_returns_handle_and_closes_it():
    closed = []
    def create(handle, *args):
        ctypes.cast(handle, ctypes.POINTER(ctypes.c_void_p))[0] = 123
        return 0x103
    api, _ = abi_api(NtCreateFile=create, CloseHandle=lambda handle: closed.append(handle) or 1)
    with pytest.raises(OSError, match='did not complete'):
        api.open_child(10, 'data', directory=False)
    assert closed == [123]


def test_native_info_queries_identity_size_and_both_timestamps_on_same_handle():
    calls = []
    def query(handle, kind, buffer, size):
        calls.append((handle, kind, size))
        if kind == 18:
            value = ctypes.cast(buffer, ctypes.POINTER(access._FILE_ID_INFO)).contents
            value.VolumeSerialNumber = 2 ** 40 + 5
            for index in range(16):
                value.FileId[index] = index
        elif kind == 0:
            value = ctypes.cast(buffer, ctypes.POINTER(access._FILE_BASIC_INFO)).contents
            value.LastWriteTime, value.ChangeTime, value.FileAttributes = 100, 200, 0
        else:
            value = ctypes.cast(buffer, ctypes.POINTER(access._FILE_STANDARD_INFO)).contents
            value.EndOfFile = 1234
        return 1
    api, _ = abi_api(GetFileInformationByHandleEx=query)
    info = api.info(987)
    assert info == access._FileInfo((2 ** 40 + 5, bytes(range(16))), 1234, 100, 200, 0, False)
    assert [call[:2] for call in calls] == [(987, 18), (987, 0), (987, 1)]
    assert ctypes.sizeof(access._FILE_ID_INFO) == 24
    assert ctypes.sizeof(access._FILE_BASIC_INFO) == 40
    assert ctypes.sizeof(access._FILE_STANDARD_INFO) == 24


def test_native_non_disk_rejected_before_metadata_or_read():
    api, _ = abi_api(GetFileType=lambda handle: 3,
        GetFileInformationByHandleEx=lambda *args: pytest.fail('Must not query a pipe'))
    with pytest.raises(OSError, match='disk'):
        api.info(777)


def test_native_read_uses_the_supplied_handle_and_exact_bound():
    calls = []
    def read(handle, buffer, size, count, overlapped):
        calls.append((handle, size, overlapped))
        ctypes.memmove(buffer, CONTENT, len(CONTENT))
        ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))[0] = len(CONTENT)
        return 1
    api, _ = abi_api(ReadFile=read)
    assert api.read(456, 100) == CONTENT
    assert calls == [(456, 100, None)]


@pytest.mark.skipif(os.name != 'nt', reason='Requires genuine native Windows filesystem handles')
def test_native_windows_regular_read_and_root_replacement(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'nested').mkdir()
    (root / 'nested' / 'data.jsonl').write_bytes(CONTENT)
    snapshot = access.snapshot_windows_root(root)
    assert access.read_windows_source(snapshot, 'nested/data.jsonl', len(CONTENT)) == CONTENT
    root.rename(tmp_path / 'old-root')
    root.mkdir()
    with pytest.raises(ServiceError) as exc:
        access.read_windows_source(snapshot, 'nested/data.jsonl', 100)
    assert exc.value.code == 'dataset_root_changed'


@pytest.mark.skipif(os.name != 'nt', reason='Requires genuine native Windows junctions')
def test_native_windows_junction_is_never_followed(tmp_path):
    import subprocess
    root, outside = tmp_path / 'root', tmp_path / 'outside'
    root.mkdir()
    outside.mkdir()
    (outside / 'data.jsonl').write_bytes(CONTENT)
    result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(root / 'junction'), str(outside)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    snapshot = access.snapshot_windows_root(root)
    with pytest.raises(OSError):
        access.read_windows_source(snapshot, 'junction/data.jsonl', 100)


@pytest.mark.skipif(os.name != 'nt', reason='Requires genuine native Windows sharing enforcement')
def test_native_windows_directory_swap_and_file_write_blocked_during_read(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    nested = root / 'nested'
    nested.mkdir(parents=True)
    target = nested / 'data.jsonl'
    target.write_bytes(CONTENT)
    snapshot = access.snapshot_windows_root(root)
    api = access._WindowsAPI()
    original_read = api.read
    attempted = []
    def read(handle, size):
        if not attempted:
            attempted.append(True)
            with pytest.raises(OSError):
                nested.rename(root / 'moved')
            with pytest.raises(OSError):
                target.write_bytes(b'replacement')
        return original_read(handle, size)
    monkeypatch.setattr(api, 'read', read)
    assert access.read_windows_source(snapshot, 'nested/data.jsonl', 100, _api=api) == CONTENT
    # Handles must all be released on return.
    nested.rename(root / 'moved')
    assert attempted == [True]


def test_cleanup_preserves_original_failure_and_attempts_every_handle():
    api = FakeAPI()
    api.after = replace(REGULAR, changed=41)
    def close(handle):
        api.closes.append(handle)
        raise OSError('simulated close error')
    api.close = close
    with pytest.raises(ServiceError) as exc:
        access.read_windows_source(ROOT, 'nested/data.jsonl', 100, _api=api)
    assert exc.value.code == 'dataset_source_changed'
    assert api.closes == [12, 11, 10]


def test_cleanup_failure_is_reported_after_otherwise_successful_read():
    api = FakeAPI()
    def close(handle):
        api.closes.append(handle)
        if handle == 12:
            raise OSError('simulated close error')
    api.close = close
    with pytest.raises(OSError, match='close'):
        access.read_windows_source(ROOT, 'nested/data.jsonl', 100, _api=api)
    assert api.closes == [12, 11, 10]


def test_native_absolute_root_does_not_reject_dos_device_namespace_alias():
    calls = []
    def create(handle, desired, attributes, *args):
        obj = ctypes.cast(attributes, ctypes.POINTER(access._OBJECT_ATTRIBUTES)).contents
        calls.append((obj.RootDirectory, obj.Attributes))
        ctypes.cast(handle, ctypes.POINTER(ctypes.c_void_p))[0] = 123
        return 0
    api, _ = abi_api(NtCreateFile=create)
    assert api.open_root(r'C:\project\data') == 123
    assert calls == [(None, access.OBJ_CASE_INSENSITIVE)]


def test_native_lone_surrogate_is_safe_oserror():
    api, _ = abi_api(NtCreateFile=lambda *args: pytest.fail('Invalid Unicode reached native API'))
    with pytest.raises(OSError, match='Unsafe'):
        api.open_root('C:\\project\\\ud800')


def test_registry_windows_branch_hashes_exact_returned_bytes_and_is_project_bound(monkeypatch):
    import hashlib
    from modelbuilder import dataset
    api = FakeAPI()
    registry = dataset.DatasetRegistry(None)
    registry._roots = {'project-a': {'data': ROOT}}
    source = {'root_id': 'data', 'id': 'source', 'path': 'data.jsonl',
              'sha256': hashlib.sha256(CONTENT).hexdigest()}
    monkeypatch.setattr(dataset, 'read_windows_source',
                        lambda root, path, limit: access.read_windows_source(root, path, limit, _api=api))
    assert registry._read_source('project-a', source) == CONTENT
    before = len(api.opens)
    with pytest.raises(ServiceError) as exc:
        registry._read_source('project-b', source)
    assert exc.value.code == 'dataset_root_unavailable' and len(api.opens) == before
    api.position = 0
    source['sha256'] = '0' * 64
    with pytest.raises(ServiceError) as exc:
        registry._read_source('project-a', source)
    assert exc.value.code == 'dataset_checksum_mismatch'


def test_registry_windows_branch_masks_source_paths_on_oserror(monkeypatch):
    from modelbuilder import dataset
    registry = dataset.DatasetRegistry(None)
    registry._roots = {'project-a': {'data': ROOT}}
    def fail(*args):
        raise OSError('private source details')
    monkeypatch.setattr(dataset, 'read_windows_source', fail)
    with pytest.raises(ServiceError) as exc:
        registry._read_source('project-a', {'root_id': 'data', 'id': 'source', 'path': 'data.jsonl'})
    assert exc.value.code == 'dataset_source_unavailable'
    assert 'private source' not in str(exc.value)
    assert ROOT.path not in str(exc.value)


@pytest.mark.skipif(os.name != 'nt', reason='Requires genuine native Windows registry reads')
def test_native_windows_registry_checks_checksum_and_project_identity(tmp_path):
    import hashlib
    from modelbuilder import dataset
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'data.jsonl').write_bytes(CONTENT)
    registry = dataset.DatasetRegistry(None, project_roots={'project-a': {'data': root}})
    assert isinstance(registry._roots['project-a']['data'], access.WindowsRoot)
    source = {'root_id': 'data', 'id': 'source', 'path': 'data.jsonl',
              'sha256': hashlib.sha256(CONTENT).hexdigest()}
    assert registry._read_source('project-a', source) == CONTENT
    (root / 'data.jsonl').write_bytes(b'changed data')
    with pytest.raises(ServiceError) as exc:
        registry._read_source('project-a', source)
    assert exc.value.code == 'dataset_checksum_mismatch'
    with pytest.raises(ServiceError) as exc:
        registry._read_source('project-b', source)
    assert exc.value.code == 'dataset_root_unavailable'


@pytest.mark.skipif(os.name != 'nt', reason='Requires genuine native Windows symlinks')
def test_native_windows_leaf_symlink_is_never_followed(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    outside = tmp_path / 'outside.jsonl'
    outside.write_bytes(CONTENT)
    try:
        (root / 'link.jsonl').symlink_to(outside)
    except OSError as exc:
        if getattr(exc, 'winerror', None) == 1314:
            pytest.skip('Host has no existing symlink-creation privilege; security settings unchanged')
        raise
    snapshot = access.snapshot_windows_root(root)
    with pytest.raises(OSError):
        access.read_windows_source(snapshot, 'link.jsonl', 100)


@pytest.mark.skipif(os.name != 'nt', reason='Requires genuine native Windows reparse mutation')
def test_native_windows_checked_ancestor_cannot_be_retargeted_to_outside(tmp_path, monkeypatch):
    import struct
    root, outside = tmp_path / 'root', tmp_path / 'outside'
    nested = root / 'nested'
    nested.mkdir(parents=True)
    outside.mkdir()
    (outside / 'data.jsonl').write_bytes(CONTENT)
    snapshot = access.snapshot_windows_root(root)
    api = access._WindowsAPI()
    original_open, original_info = api.open_child, api.info
    child_handles, attempted = set(), []

    def open_child(parent, name, *, directory):
        handle = original_open(parent, name, directory=directory)
        if name == 'nested':
            child_handles.add(handle)
        return handle

    def info(handle):
        value = original_info(handle)
        if handle in child_handles and not attempted:
            attempted.append(True)
            # Metadata-only access is exempt from ordinary write sharing. Try a
            # junction conversion after metadata was captured but before the
            # reader validates that metadata and opens the next component.
            kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True, winmode=0x800)
            kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
            kernel.CreateFileW.restype = ctypes.c_void_p
            kernel.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
            kernel.DeviceIoControl.restype = ctypes.c_int32
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle.restype = ctypes.c_int32
            metadata_handle = kernel.CreateFileW(str(nested), 0x100, 7, None, 3, 0x02200000, None)
            if metadata_handle == ctypes.c_void_p(-1).value:
                assert ctypes.get_last_error() in {5, 32}, 'Unexpected metadata-open failure'
            else:
                try:
                    substitute = access._nt_root_name(str(outside)).encode('utf-16-le')
                    display = str(outside).encode('utf-16-le')
                    names = substitute + b'\0\0' + display + b'\0\0'
                    data = struct.pack('<IHHHHHH', 0xA0000003, 8 + len(names), 0,
                                       0, len(substitute), len(substitute) + 2, len(display)) + names
                    buffer, count = ctypes.create_string_buffer(data), ctypes.c_uint32()
                    mutated = kernel.DeviceIoControl(metadata_handle, 0x900A4, buffer, len(data),
                                                      None, 0, ctypes.byref(count), None)
                    if not mutated:
                        assert ctypes.get_last_error() in {5, 32, 1314}, 'Unexpected junction-mutation failure'
                finally:
                    assert kernel.CloseHandle(metadata_handle)
        return value

    monkeypatch.setattr(api, 'open_child', open_child)
    monkeypatch.setattr(api, 'info', info)
    # Whether the OS refuses the mutation or the relative open refuses the new
    # junction, outside bytes must never be returned. No file exists in nested.
    with pytest.raises(OSError):
        access.read_windows_source(snapshot, 'nested/data.jsonl', 100, _api=api)
    assert attempted == [True]


@pytest.mark.parametrize('change', [
    {'directory': True, 'attributes': access.FILE_ATTRIBUTE_DIRECTORY},
    {'directory': False, 'attributes': access.FILE_ATTRIBUTE_DIRECTORY},
    {'delete_pending': True}, {'size': -1}])
def test_nonregular_inconsistent_pending_or_invalid_file_metadata_is_rejected(change):
    api = FakeAPI()
    api.metadata[12] = replace(REGULAR, **change)
    with pytest.raises((OSError, ServiceError)):
        access.read_windows_source(ROOT, 'data.jsonl', 100, _api=api)
    assert not api.reads and api.closes == [12, 10]


def test_root_snapshot_cleanup_preserves_validation_error():
    api = FakeAPI()
    api.metadata[10] = REGULAR
    api.close = lambda handle: (_ for _ in ()).throw(OSError('close error'))
    with pytest.raises(ServiceError) as exc:
        access.snapshot_windows_root(ROOT.path, _api=api)
    assert exc.value.code == 'dataset_source_invalid'
