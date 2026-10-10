"""Binding admission hashes native bytes without loading project artifacts."""
import os
from types import SimpleNamespace

import pytest
from modelbuilder.backend.admission import BackendError
from modelbuilder.backend.native_inputs import storage_identity, validate_native_type


requires_posix_reads = pytest.mark.skipif(os.name != "posix", reason="Requires POSIX descriptor traversal")


@requires_posix_reads
def test_native_byte_identity_detects_tamper_without_deserialization(tmp_path):
    artifact = tmp_path / "zenml-stores" / "native" / "artifact"
    artifact.mkdir(parents=True)
    (artifact / "data.tar.gz").write_bytes(b"opaque checkpoint materialization")
    native = SimpleNamespace(uri=str(artifact), load=lambda: (_ for _ in ()).throw(AssertionError("No artifact load allowed")))
    first = storage_identity(native, tmp_path)
    assert first == storage_identity(native, tmp_path)
    (artifact / "data.tar.gz").write_bytes(b"tampered")
    assert storage_identity(native, tmp_path) != first


@pytest.mark.parametrize("escape", ["file-link", "root-link", "outside"])
@requires_posix_reads
def test_native_binding_rejects_symlinks_and_foreign_store(tmp_path, escape):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data").write_text("foreign")
    root = tmp_path / "owner"
    artifact = root / "zenml-stores" / "artifact"
    artifact.parent.mkdir(parents=True)
    if escape == "root-link":
        artifact.symlink_to(outside, target_is_directory=True)
    else:
        artifact.mkdir()
        (artifact / "data").symlink_to(outside / "data")
    with pytest.raises(BackendError):
        storage_identity(SimpleNamespace(uri=str(outside if escape == "outside" else artifact)), root)


def test_native_type_and_materializer_must_match_without_import():
    valid = SimpleNamespace(data_type=SimpleNamespace(import_path="pathlib.PosixPath"),
        materializer=SimpleNamespace(import_path="zenml.materializers.path_materializer.PathMaterializer"))
    validate_native_type(valid, "checkpoint")
    with pytest.raises(BackendError):
        validate_native_type(valid, "dict")
    valid.materializer.import_path = "project.source.UntrustedLoader"
    with pytest.raises(BackendError):
        validate_native_type(valid, "Path")


def _native_artifact(tmp_path):
    artifact = tmp_path / "owner" / "zenml-stores" / "native" / "artifact"
    artifact.mkdir(parents=True)
    (artifact / "data").write_bytes(b"owned")
    return SimpleNamespace(uri=str(artifact)), artifact, tmp_path / "owner"


def _replace_open(monkeypatch, replacement):
    # Keep capability discovery accurate when intercepting the actual POSIX open.
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, replacement})
    monkeypatch.setattr(os, "open", replacement)


@requires_posix_reads
def test_native_identity_preserves_path_order_and_ignores_metadata(tmp_path):
    import hashlib
    from modelbuilder.backend.components import identity
    native, artifact, root = _native_artifact(tmp_path)
    (artifact / "a").mkdir()
    (artifact / "a" / "weights").write_bytes(b"weights")
    (artifact / "a.json").write_bytes(b"metadata")
    (artifact / "empty").mkdir()
    expected = identity([
        {"path": name, "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in [("a/weights", b"weights"), ("a.json", b"metadata"), ("data", b"owned")]
    ])
    assert storage_identity(native, root) == expected
    (artifact / "data").touch()
    (artifact / "empty").rmdir()
    assert storage_identity(native, root) == expected


@pytest.mark.parametrize("component", ["owner", "zenml-stores", "native", "artifact", "nested"])
@pytest.mark.parametrize("when", ["before_open", "after_open"])
@requires_posix_reads
def test_native_ancestor_symlink_swaps_never_read_foreign_bytes(tmp_path, monkeypatch, component, when):
    native, artifact, root = _native_artifact(tmp_path)
    (artifact / "nested").mkdir()
    (artifact / "nested" / "weights").write_bytes(b"owned weights")
    target = next(path for path in [artifact / "nested", artifact, *artifact.parents] if path.name == component)
    outside = tmp_path / "foreign"
    outside.mkdir()
    (outside / "data").write_bytes(b"foreign bytes")
    real_open, real_read = os.open, os.read
    reads, swapped = [], False

    def swap():
        target.rename(target.with_name(target.name + "-original"))
        target.symlink_to(outside, target_is_directory=True)

    def racing_open(name, flags, *args, **kwargs):
        nonlocal swapped
        if name == component and not swapped:
            swapped = True
            assert flags & os.O_NOFOLLOW and flags & os.O_DIRECTORY
            if when == "before_open":
                swap()
                return real_open(name, flags, *args, **kwargs)
            fd = real_open(name, flags, *args, **kwargs)
            swap()
            return fd
        return real_open(name, flags, *args, **kwargs)

    def observe_read(fd, count):
        chunk = real_read(fd, count)
        reads.append(chunk)
        return chunk

    _replace_open(monkeypatch, racing_open)
    monkeypatch.setattr(os, "read", observe_read)
    with pytest.raises(BackendError, match="unsafe|changed") as error:
        storage_identity(native, root)
    assert error.value.code == "artifact_binding_denied"
    assert swapped
    assert b"foreign" not in b"".join(reads)


@pytest.mark.parametrize("replacement", ["symlink", "fifo", "directory"])
@requires_posix_reads
def test_native_leaf_swap_rejected_before_read(tmp_path, monkeypatch, replacement):
    native, artifact, root = _native_artifact(tmp_path)
    outside = tmp_path / "foreign"
    outside.write_bytes(b"foreign bytes")
    real_open = os.open

    def racing_open(name, flags, *args, **kwargs):
        if name == "data":
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
            (artifact / "data").unlink()
            if replacement == "symlink":
                (artifact / "data").symlink_to(outside)
            elif replacement == "fifo":
                os.mkfifo(artifact / "data")
            else:
                (artifact / "data").mkdir()
        return real_open(name, flags, *args, **kwargs)

    def forbidden_read(*args):
        pytest.fail("Replaced leaf must be rejected before a read")

    _replace_open(monkeypatch, racing_open)
    monkeypatch.setattr(os, "read", forbidden_read)
    with pytest.raises(BackendError) as error:
        storage_identity(native, root)
    assert error.value.code == "artifact_binding_denied"


@requires_posix_reads
def test_native_fifo_rejected_without_opening(tmp_path, monkeypatch):
    native, artifact, root = _native_artifact(tmp_path)
    os.mkfifo(artifact / "special")
    real_open = os.open

    def checked_open(name, *args, **kwargs):
        assert name != "special", "Special files must not be opened"
        return real_open(name, *args, **kwargs)

    _replace_open(monkeypatch, checked_open)
    with pytest.raises(BackendError, match="nonregular"):
        storage_identity(native, root)


@pytest.mark.parametrize("kind", ["S_IFSOCK", "S_IFCHR", "S_IFBLK"])
@requires_posix_reads
def test_native_special_modes_rejected_without_opening(tmp_path, monkeypatch, kind):
    import stat
    native, artifact, root = _native_artifact(tmp_path)
    (artifact / "special").touch()
    real_open, real_stat = os.open, os.stat

    def special_stat(name, *args, **kwargs):
        if name == "special":
            assert kwargs.get("follow_symlinks") is False
            return SimpleNamespace(st_mode=getattr(stat, kind) | 0o600)
        return real_stat(name, *args, **kwargs)

    def checked_open(name, *args, **kwargs):
        assert name != "special", "Special files must not be opened"
        return real_open(name, *args, **kwargs)

    _replace_open(monkeypatch, checked_open)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, special_stat})
    monkeypatch.setattr(os, "supports_follow_symlinks", {*os.supports_follow_symlinks, special_stat})
    monkeypatch.setattr(os, "stat", special_stat)
    with pytest.raises(BackendError, match="nonregular"):
        storage_identity(native, root)


@pytest.mark.parametrize("kind", ["entry_count", "directory_depth", "file_bytes", "total_bytes"])
@requires_posix_reads
def test_native_storage_limits(tmp_path, monkeypatch, kind):
    from modelbuilder.backend import native_inputs
    native, artifact, root = _native_artifact(tmp_path)
    if kind == "entry_count":
        (artifact / "empty-one").mkdir()
        (artifact / "empty-two").mkdir()
        monkeypatch.setattr(native_inputs, "MAX_NATIVE_ENTRIES", 2)
    elif kind == "directory_depth":
        (artifact / "nested" / "deep").mkdir(parents=True)
        monkeypatch.setattr(native_inputs, "MAX_NATIVE_DEPTH", 1)
    elif kind == "file_bytes":
        monkeypatch.setattr(native_inputs, "MAX_NATIVE_FILE_BYTES", 4)
    else:
        (artifact / "second").write_bytes(b"owned")
        monkeypatch.setattr(native_inputs, "MAX_NATIVE_FILE_BYTES", 5)
        monkeypatch.setattr(native_inputs, "MAX_NATIVE_TOTAL_BYTES", 9)
    with pytest.raises(BackendError, match="limit") as error:
        storage_identity(native, root)
    assert error.value.code == "artifact_binding_denied"


@requires_posix_reads
def test_native_exact_byte_limits_and_bounded_reads(tmp_path, monkeypatch):
    from modelbuilder.backend import native_inputs
    native, artifact, root = _native_artifact(tmp_path)
    (artifact / "second").write_bytes(b"owned")
    monkeypatch.setattr(native_inputs, "MAX_NATIVE_FILE_BYTES", 5)
    monkeypatch.setattr(native_inputs, "MAX_NATIVE_TOTAL_BYTES", 10)
    monkeypatch.setattr(native_inputs, "NATIVE_READ_BYTES", 2)
    real_read, reads = os.read, []

    def bounded_read(fd, count):
        assert 0 < count <= 2
        reads.append(count)
        return real_read(fd, count)

    monkeypatch.setattr(os, "read", bounded_read)
    assert len(storage_identity(native, root)) == 64
    assert reads == [2, 2, 2, 1] * 2


@requires_posix_reads
def test_native_oversized_file_rejected_before_read(tmp_path, monkeypatch):
    from modelbuilder.backend import native_inputs
    native, _, root = _native_artifact(tmp_path)
    monkeypatch.setattr(native_inputs, "MAX_NATIVE_FILE_BYTES", 4)

    def forbidden_read(*args):
        pytest.fail("Oversized native file should be rejected before reading")

    monkeypatch.setattr(os, "read", forbidden_read)
    with pytest.raises(BackendError, match="byte limit"):
        storage_identity(native, root)


@pytest.mark.parametrize("mutation", ["grow", "truncate", "overwrite", "add_entry", "replace_leaf"])
@requires_posix_reads
def test_native_mutations_during_read_are_rejected(tmp_path, monkeypatch, mutation):
    from modelbuilder.backend import native_inputs
    native, artifact, root = _native_artifact(tmp_path)
    monkeypatch.setattr(native_inputs, "NATIVE_READ_BYTES", 2)
    real_read, calls = os.read, 0

    def racing_read(fd, count):
        nonlocal calls
        calls += 1
        assert calls <= 4, "Growth must not turn hashing into an unbounded read"
        chunk = real_read(fd, count)
        if mutation == "grow":
            with (artifact / "data").open("ab") as stream:
                stream.write(b"unbounded growth")
        elif calls == 1:
            if mutation == "add_entry":
                (artifact / "new").write_bytes(b"new entry")
            elif mutation == "replace_leaf":
                (artifact / "data").unlink()
                (artifact / "data").write_bytes(b"owned")
            else:
                (artifact / "data").write_bytes(b"" if mutation == "truncate" else b"other")
        return chunk

    monkeypatch.setattr(os, "read", racing_read)
    with pytest.raises(BackendError, match="changed"):
        storage_identity(native, root)


@pytest.mark.parametrize("uri", ["relative", "traversal", "store_prefix"])
@requires_posix_reads
def test_native_lexical_store_boundary_checked_before_open(tmp_path, monkeypatch, uri):
    native, artifact, root = _native_artifact(tmp_path)
    native.uri = {"relative": "zenml-stores/native/artifact",
                  "traversal": str(artifact / ".." / ".." / ".." / "foreign"),
                  "store_prefix": str(root / "zenml-stores-foreign" / "artifact")}[uri]

    def forbidden_open(*args, **kwargs):
        pytest.fail("Foreign paths should be rejected before opening")

    _replace_open(monkeypatch, forbidden_open)
    with pytest.raises(BackendError, match="outside"):
        storage_identity(native, root)


def test_native_unsupported_descriptor_access_fails_closed(tmp_path, monkeypatch):
    native, _, root = _native_artifact(tmp_path)
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(BackendError) as error:
        storage_identity(native, root)
    assert error.value.code == "capability_unavailable"


@requires_posix_reads
def test_native_empty_artifact_is_rejected(tmp_path):
    native, artifact, root = _native_artifact(tmp_path)
    (artifact / "data").unlink()
    with pytest.raises(BackendError, match="absent"):
        storage_identity(native, root)


@requires_posix_reads
def test_native_descriptors_closed_after_rejection(tmp_path, monkeypatch):
    native, artifact, root = _native_artifact(tmp_path)
    (artifact / "nested").mkdir()
    (artifact / "nested" / "link").symlink_to(artifact / "data")
    real_open, real_close = os.open, os.close
    active = set()

    def tracked_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        assert fd not in active
        active.add(fd)
        return fd

    def tracked_close(fd):
        active.remove(fd)
        return real_close(fd)

    _replace_open(monkeypatch, tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)
    with pytest.raises(BackendError):
        storage_identity(native, root)
    assert not active


@requires_posix_reads
def test_native_entry_limit_bounds_enumeration_itself(tmp_path, monkeypatch):
    from modelbuilder.backend import native_inputs
    native, _, root = _native_artifact(tmp_path)
    monkeypatch.setattr(native_inputs, "MAX_NATIVE_ENTRIES", 2)

    class Scan:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            for index in range(4):
                assert index < 3, "Enumeration must stop immediately after the count limit"
                yield SimpleNamespace(name=str(index))

    def scan_fd(fd):
        assert isinstance(fd, int), "Enumeration must use the already-open directory descriptor"
        return Scan()

    monkeypatch.setattr(os, "supports_fd", {*os.supports_fd, scan_fd})
    monkeypatch.setattr(os, "scandir", scan_fd)
    with pytest.raises(BackendError, match="entry-count limit"):
        storage_identity(native, root)


@pytest.mark.parametrize("declared", ["dict", "DatasetArtifact", "CheckpointArtifact", "EvaluationArtifact", "ExportArtifact"])
def test_native_type_dict_materializer_allowlist_remains_inert(declared):
    valid = SimpleNamespace(data_type=SimpleNamespace(import_path="builtins.dict"),
        materializer=SimpleNamespace(import_path="zenml.materializers.built_in_materializer.BuiltInContainerMaterializer"))
    validate_native_type(valid, declared)
    valid.materializer.import_path = "zenml.materializers.cloudpickle_materializer.CloudpickleMaterializer"
    with pytest.raises(BackendError) as error:
        validate_native_type(valid, declared)
    assert error.value.code == "artifact_type_mismatch"
