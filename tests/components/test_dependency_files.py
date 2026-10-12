"""Large source shards stay bounded at admission and component boundaries."""
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from modelbuilder.backend import admission, dependency_files, placement
from modelbuilder.backend.components import runtime

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Native dependency reads require POSIX descriptors")


@pytest.fixture
def shard(tmp_path, monkeypatch):
    path = tmp_path / "source.jsonl"
    block = b'{"id":"sample","text":"streamed data"}\n' * 30000
    digest = hashlib.sha256()
    with path.open("wb") as stream:
        for _ in range(3):
            stream.write(block)
            digest.update(block)
    original = Path.read_bytes
    def guarded(value):
        if value == path:
            pytest.fail("Native source dependencies must never use read_bytes")
        return original(value)
    monkeypatch.setattr(Path, "read_bytes", guarded)
    return path, digest.hexdigest()


def test_hash_reads_fixed_chunks_and_never_materializes_shard(shard, monkeypatch):
    path, expected = shard
    original, requests = os.read, []
    def bounded(descriptor, count):
        requests.append(count)
        assert 0 < count <= dependency_files.READ_BYTES
        return original(descriptor, count)
    monkeypatch.setattr(os, "read", bounded)
    assert dependency_files.dependency_sha256(path) == expected
    assert len(requests) > 3


@pytest.mark.parametrize("kind", ["file-link", "directory-link", "fifo", "directory", "missing"])
def test_unsafe_dependencies_fail_before_read(tmp_path, kind):
    original = tmp_path / "real"
    original.mkdir()
    (original / "file").write_bytes(b"data")
    path = tmp_path / "dependency"
    if kind == "file-link":
        path.symlink_to(original / "file")
    elif kind == "directory-link":
        path.symlink_to(original, target_is_directory=True)
        path /= "file"
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    with pytest.raises(ValueError):
        dependency_files.dependency_sha256(path)


@pytest.mark.parametrize("change", ["overwrite", "append", "replace", "replace-parent"])
def test_file_and_path_mutation_are_fenced(tmp_path, monkeypatch, change):
    root = tmp_path / "root"
    root.mkdir()
    path = root / "dependency"
    path.write_bytes(b"initial contents")
    original, changed = os.read, False
    def raced(descriptor, count):
        nonlocal changed
        if not changed:
            changed = True
            if change == "overwrite":
                path.write_bytes(b"changed contents")
            elif change == "append":
                with path.open("ab") as stream:
                    stream.write(b"extra")
            elif change == "replace":
                path.rename(root / "old")
                path.write_bytes(b"initial contents")
            else:
                root.rename(tmp_path / "old-root")
                root.mkdir()
                path.write_bytes(b"initial contents")
        return original(descriptor, count)
    monkeypatch.setattr(os, "read", raced)
    with pytest.raises(ValueError, match="changed"):
        dependency_files.dependency_sha256(path)


def test_component_checks_large_dependency_without_eager_allocation(shard, tmp_path, monkeypatch):
    path, expected = shard
    component = {"id": "test.reader", "version": "1", "protocol": "modelbuilder.component/v1",
                 "role": "utility", "parameter_schema": {"type": "object"},
                 "capabilities": {"resume_modes": [], "export_formats": [], "frameworks": []}}
    node = {"component": component, "parameters": {}, "implementation_ref": {},
            "outputs": [{"name": "result", "type": "dict"}]}
    spec = {"component_node": node, "dependencies": [{"id": "shard", "sha256": expected}],
            "dependency_paths": {"shard": str(path)}}
    monkeypatch.setitem(sys.modules, "modelbuilder.backend.worker", SimpleNamespace(context=lambda spec: ({}, tmp_path / "out")))
    class Verified(Exception):
        pass
    def finished():
        raise Verified("dependency verification completed")
    monkeypatch.setattr(runtime, "runtime_environment", finished)
    with pytest.raises(Verified):
        runtime.dispatch({}, spec)
    spec["dependencies"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="changed after admission"):
        runtime.dispatch({}, spec)


def test_admission_checks_large_dependency_without_eager_allocation(shard, tmp_path, monkeypatch):
    path, expected = shard
    source = tmp_path / "adapter.py"
    source.write_text("def run(**kwargs): return {}\n")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    commit = "a" * 40
    ref = {"path": source.name, "sha256": source_hash, "commit": commit, "module": "adapter", "entrypoint": "run"}
    node = {"id": "reader", "kind": "code.python", "implementation_ref": ref, "inputs": [],
            "outputs": [{"name": "result", "type": "dict"}], "parameters": {}, "timeout": 10,
            "retry": {"max_attempts": 1}}
    recipe = {"nodes": [node], "edges": [], "source": {"commit": commit,
              "files": [{"path": source.name, "sha256": source_hash}]},
              "external_dependencies": [{"id": "shard", "sha256": expected}]}
    backend = SimpleNamespace(_require_capability=lambda: None, repository=tmp_path,
                              trusted_code_roots=[str(tmp_path)], dependency_paths={"shard": str(path)})
    monkeypatch.setattr(placement, "validate_placement", lambda *args: {"device": "cpu"})
    monkeypatch.setattr(admission, "verify_environment", lambda *args: {})
    payload = {"recipe": recipe, "trusted": True}
    assert admission.validate_execution(backend, payload) == recipe
    recipe["external_dependencies"][0]["sha256"] = "0" * 64
    with pytest.raises(admission.BackendError, match="bytes do not match"):
        admission.validate_execution(backend, payload)
