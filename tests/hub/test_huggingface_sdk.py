"""Exercise the real pinned HF SDK against deterministic fake HTTP responses.

No credentials, network, login, real downloads or real uploads are used.
"""
import hashlib
import io
import json
import os
import socket
import importlib.metadata
from pathlib import Path
from urllib.parse import urlsplit

import pytest

if os.environ.get("MODELBUILDER_REQUIRE_HUB_SDK") == "1":
    import huggingface_hub as sdk
else:
    sdk = pytest.importorskip("huggingface_hub", reason="Separate locked Hub SDK test interpreter is required")
import requests  # noqa: E402

from modelbuilder.hub_sdk_worker import execute, hashes, WorkerError  # noqa: E402

COMMIT, RESULT = "a" * 40, "b" * 40
CONTENT = b"tiny pinned test file"
BLOB = hashlib.sha1(f"blob {len(CONTENT)}\0".encode() + CONTENT, usedforsecurity=False).hexdigest()


class FakeHTTP(requests.adapters.BaseAdapter):
    def __init__(self):
        self.calls, self.published = [], False

    def send(self, request, **kwargs):
        self.calls.append((request.method, request.url, request.body))
        assert "Authorization" not in request.headers
        assert urlsplit(request.url).hostname == "huggingface.co"
        path = urlsplit(request.url).path
        method = request.method
        headers = {}
        response = requests.Response()
        response.status_code = 200
        if path.startswith("/api/models/owner/tiny/tree/") or path.endswith("/paths-info/" + RESULT):
            body = [{"type": "file", "oid": BLOB, "size": len(CONTENT), "path": "file.txt"}]
        elif path.startswith("/api/models/owner/tiny/preupload/"):
            body = {"files": [{"path": "file.txt", "uploadMode": "regular", "shouldIgnore": False}]}
        elif path.startswith("/api/models/owner/tiny/commit/") and method == "POST":
            self.published = True
            body = {"commitOid": RESULT, "commitUrl": f"https://huggingface.co/owner/tiny/commit/{RESULT}", "hookOutput": ""}
        elif path.startswith("/api/models/owner/tiny/commits/"):
            body = [{"id": RESULT, "title": "ModelBuilder publication test", "message": "", "date": "2026-10-13T00:00:00.000Z", "authors": []}]
        elif path.startswith("/api/models/owner/tiny"):
            commit = RESULT if path.endswith(RESULT) else COMMIT
            body = {"id": "owner/tiny", "sha": commit, "private": True, "gated": False, "cardData": {"license": "mit"}}
        elif path == f"/owner/tiny/resolve/{COMMIT}/file.txt":
            body = b"" if method == "HEAD" else CONTENT
            headers.update({"X-Repo-Commit": COMMIT, "ETag": f'"{BLOB}"', "Content-Length": str(len(CONTENT))})
        else:
            raise AssertionError(f"Unexpected fake HTTP route: {method} {path}")
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        response.url, response.request = request.url, request
        response.headers.update(headers)
        response.raw = io.BytesIO(raw)
        response._content = raw if method != "GET" or "resolve" not in path else False
        return response

    def close(self):
        pass


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Hub SDK fake-HTTP acceptance must not open a network socket")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def test_complete_worker_profile_is_present_when_required():
    assert sdk.__version__ == "0.36.2"
    if os.environ.get("MODELBUILDER_REQUIRE_HUB_SDK") == "1":
        assert importlib.metadata.version("keyring") == "25.6.0"
        import keyring  # noqa: F401


@pytest.fixture
def boundary(monkeypatch):
    assert sdk.__version__ == "0.36.2", "Use the reviewed isolated Hugging Face SDK version"
    transport = FakeHTTP()

    def factory():
        session = requests.Session()
        session.trust_env = False
        session.mount("https://", transport)
        return session
    sdk.configure_http_backend(backend_factory=factory)
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "1")
    try:
        yield transport, sdk.HfApi(endpoint="https://huggingface.co", token=False)
    finally:
        sdk.configure_http_backend()


def test_actual_sdk_resolves_immutable_tree_and_downloads_tiny_file(boundary, tmp_path):
    transport, api = boundary
    payload = {"repo_id": "owner/tiny", "repo_type": "model", "remote_revision": "main"}
    inspected = execute("inspect", payload, api)
    assert inspected["commit"] == COMMIT
    assert inspected["files"] == [{"path": "file.txt", "size_bytes": len(CONTENT), "sha256": None, "git_blob_sha1": BLOB}]
    downloaded = execute("download", {**payload, "commit": COMMIT, "files": inspected["files"], "staging_dir": str(tmp_path)}, api, sdk=sdk, token=False)
    assert Path(downloaded["files"][0]["local_path"]).read_bytes() == CONTENT
    assert any(method == "HEAD" and COMMIT in url for method, url, _ in transport.calls)
    assert any(method == "GET" and "/resolve/" in url for method, url, _ in transport.calls)


def test_actual_sdk_publication_preupload_commit_parent_and_immutable_verification(boundary, tmp_path):
    transport, api = boundary
    (tmp_path / "file.txt").write_bytes(CONTENT)
    item = {"path": "file.txt", "size_bytes": len(CONTENT), "sha256": hashlib.sha256(CONTENT).hexdigest(), "git_blob_sha1": BLOB}
    payload = {"repo_id": "owner/tiny", "repo_type": "model", "visibility": "private", "files": [item],
               "license": "mit", "parent_commit": COMMIT, "create_repo": False, "snapshot_dir": str(tmp_path), "commit_marker": "ModelBuilder publication test"}
    result = execute("publish", payload, api, sdk=sdk)
    assert result["commit"] == RESULT and result["verified"]
    assert transport.published
    commit = next(body for method, url, body in transport.calls if "/commit/" in url)
    encoded = commit if isinstance(commit, bytes) else b"".join(commit)
    lines = [json.loads(line) for line in encoded.splitlines()]
    assert lines[0]["value"]["parentCommit"] == COMMIT
    assert any("paths-info/" + RESULT in url for _, url, _ in transport.calls)
    reconciled = execute("reconcile", payload, api, sdk=sdk)
    assert reconciled["commit"] == RESULT


def test_actual_sdk_rejects_changed_parent_before_commit(boundary, tmp_path):
    transport, api = boundary
    with pytest.raises(WorkerError) as error:
        execute("publish", {"repo_id": "owner/tiny", "repo_type": "model", "visibility": "private", "files": [], "license": "mit", "parent_commit": "c" * 40, "create_repo": False}, api, sdk=sdk)
    assert error.value.code == "hub_destination_changed"
    assert not transport.published


def test_hash_verification_is_bounded_and_checks_git_blob():
    assert hashes(io.BytesIO(CONTENT), len(CONTENT)) == (hashlib.sha256(CONTENT).hexdigest(), BLOB)
    with pytest.raises(WorkerError):
        hashes(io.BytesIO(CONTENT + b"extra"), len(CONTENT))


def test_sdk_staging_rejects_preexisting_symlink_without_network(boundary, tmp_path):
    transport, api = boundary
    root = tmp_path / "sdk"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"unrelated user bytes")
    (root / "file.txt").symlink_to(outside)
    with pytest.raises(WorkerError):
        execute("download", {"repo_id": "owner/tiny", "repo_type": "model", "commit": COMMIT,
                             "files": [{"path": "file.txt", "size_bytes": len(CONTENT)}], "staging_dir": str(root)}, api, sdk=sdk, token=False)
    assert outside.read_bytes() == b"unrelated user bytes" and transport.calls == []


@pytest.mark.parametrize("metadata", ['license: mit\n"license": apache-2.0', 'license: mit\nlicense: |\n  apache-2.0', 'license: mit\nlicense: apache-2.0'])
def test_sdk_card_parser_rejects_duplicate_ambiguous_license(boundary, metadata):
    transport, api = boundary
    with pytest.raises(WorkerError):
        execute("card.inspect", {"card": f"---\n{metadata}\n---\nModel"}, api, sdk=sdk)
    assert transport.calls == []


def test_sdk_card_parser_accepts_standard_metadata_without_network(boundary):
    transport, api = boundary
    result = execute("card.inspect", {"card": "---\nlicense: mit\ntags:\n- tiny\n- test\n---\nModel"}, api, sdk=sdk)
    assert result == {"license": "mit"} and transport.calls == []


@pytest.mark.parametrize("name", ["subdirectory", ".cache"])
def test_sdk_nested_or_cache_symlink_rejected_before_any_request(boundary, tmp_path, name):
    transport, api = boundary
    root = tmp_path / "attempt"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / name).symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkerError):
        execute("download", {"repo_id": "owner/tiny", "repo_type": "model", "commit": COMMIT,
                             "files": [{"path": "file.txt", "size_bytes": len(CONTENT)}], "staging_dir": str(root)}, api, sdk=sdk)
    assert not list(outside.iterdir()) and not transport.calls


def test_immutable_commit_verification_rejects_remote_license_mismatch(boundary):
    from modelbuilder.hub_sdk_worker import verify_commit
    _, api = boundary
    with pytest.raises(WorkerError) as error:
        verify_commit(api, {"repo_id": "owner/tiny", "repo_type": "model", "visibility": "private", "license": "apache-2.0", "files": []}, RESULT)
    assert error.value.code == "hub_verification_failed"


def test_fresh_attempt_does_not_reuse_poisoned_sdk_cache(boundary, tmp_path):
    transport, api = boundary
    original = transport.send
    corrupt = [True]

    def send(request, **kwargs):
        response = original(request, **kwargs)
        if corrupt[0] and request.method == "GET" and "/resolve/" in request.url:
            response.raw = io.BytesIO(b"x" * len(CONTENT))
            response._content = False
        return response
    transport.send = send
    first, second = tmp_path / "first-exclusive", tmp_path / "second-exclusive"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    payload = {"repo_id": "owner/tiny", "repo_type": "model", "commit": COMMIT, "files": [{"path": "file.txt", "size_bytes": len(CONTENT)}]}
    one = execute("download", {**payload, "staging_dir": str(first)}, api, sdk=sdk, token=False)
    assert hashlib.sha256(Path(one["files"][0]["local_path"]).read_bytes()).hexdigest() != hashlib.sha256(CONTENT).hexdigest()
    before = len(transport.calls)
    corrupt[0] = False
    two = execute("download", {**payload, "staging_dir": str(second)}, api, sdk=sdk, token=False)
    assert Path(two["files"][0]["local_path"]).read_bytes() == CONTENT and len(transport.calls) > before


def test_actual_sdk_captures_full_license_notice_text_at_immutable_revision(boundary):
    transport, api = boundary
    original = transport.send
    documents = {"LICENSE": b"Synthetic authored test bytes: permitted only for this deterministic fixture.\n", "NOTICE.txt": b"Attribution: test fixture author.\n"}
    hashes_by_path = {name: hashlib.sha1(f"blob {len(raw)}\0".encode() + raw, usedforsecurity=False).hexdigest() for name, raw in documents.items()}

    def send(request, **kwargs):
        path = urlsplit(request.url).path
        if "/tree/" in path:
            response = original(request, **kwargs)
            body = [{"type": "file", "oid": hashes_by_path[name], "size": len(raw), "path": name} for name, raw in documents.items()]
            response._content = json.dumps(body).encode()
            response.raw = io.BytesIO(response._content)
            return response
        if "/resolve/" in path:
            name = path.rsplit("/", 1)[1]
            assert name in documents and COMMIT in path
            transport.calls.append((request.method, request.url, request.body))
            raw = documents[name]
            response = requests.Response()
            response.status_code, response.url, response.request = 200, request.url, request
            response.headers.update({"X-Repo-Commit": COMMIT, "ETag": '"' + hashes_by_path[name] + '"', "Content-Length": str(len(raw))})
            response.raw = io.BytesIO(b"" if request.method == "HEAD" else raw)
            response._content = b"" if request.method == "HEAD" else False
            return response
        return original(request, **kwargs)
    transport.send = send
    result = execute("license.inspect", {"repo_id": "owner/tiny", "repo_type": "model", "remote_revision": "main"}, api, sdk=sdk)
    assert result["commit"] == COMMIT and result["card_metadata"]["license"] == "mit"
    assert {item["path"]: item["text"].encode() for item in result["documents"]} == documents
    for item in result["documents"]:
        assert item["sha256"] == hashlib.sha256(documents[item["path"]]).hexdigest()
        assert f"/blob/{COMMIT}/" in item["source_url"]
    assert all("Authorization" not in str(body) for _, _, body in transport.calls)


def test_actual_sdk_regular_upload_guard_cannot_allocate_large_base64_payload():
    from modelbuilder.hub_sdk_worker import BoundedUploadFile
    handle = io.BytesIO(b"tiny local fixture")
    operation = sdk.CommitOperationAdd(path_in_repo="weights.bin", path_or_fileobj=BoundedUploadFile(handle, 75 * 1024**3))
    # SDK sampling/hash setup streams the tiny fixture; its regular-upload encoder
    # must fail before an unbounded read, regardless of a server's upload-mode choice.
    with pytest.raises(WorkerError) as error:
        operation.b64content()
    assert error.value.code == "hub_upload_memory_budget"
    assert not handle.closed
