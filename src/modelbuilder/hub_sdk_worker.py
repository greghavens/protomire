"""Isolated HF SDK worker. Invoked by absolute path with Python -I, never as user code.

Reviewed API: huggingface_hub 0.36.2. No login, code loading, inference, auto
account creation, dataset scripts, or vendor compute APIs are called here.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import quote

SDK_VERSION = "0.36.2"
MAX_FILES = 10000
MAX_METADATA = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 2**50
MAX_STREAM_CHUNK = 1024 * 1024


class WorkerError(Exception):
    def __init__(self, code):
        self.code = code


def file_record(item):
    if type(item.size) is not int or not 0 <= item.size <= MAX_ARTIFACT_BYTES:
        raise WorkerError("hub_budget_exceeded")
    lfs = getattr(item, "lfs", None)
    sha = getattr(lfs, "sha256", None) if lfs else None
    return {"path": item.path, "size_bytes": item.size, "sha256": sha,
            "git_blob_sha1": item.blob_id if not sha else None}


def inspect(api, p):
    info = api.repo_info(p["repo_id"], repo_type=p["repo_type"], revision=p.get("remote_revision"), timeout=20)
    if not re.fullmatch(r"[a-f0-9]{40}", info.sha or ""):
        raise WorkerError("hub_verification_failed")
    files = []
    for item in api.list_repo_tree(p["repo_id"], repo_type=p["repo_type"], revision=info.sha, recursive=True):
        if not hasattr(item, "size"):
            continue
        if len(files) >= MAX_FILES:
            raise WorkerError("hub_budget_exceeded")
        files.append(file_record(item))
    card = getattr(info, "card_data", None)
    license_name = card.get("license") if card else None
    private, gated = bool(info.private), bool(getattr(info, "gated", False))
    access = "available"
    if gated:
        from huggingface_hub.errors import GatedRepoError
        try:
            api.auth_check(p["repo_id"], repo_type=p["repo_type"])
            access = "gated_authorized"
        except GatedRepoError:
            access = "gated"
    return {"repo_id": p["repo_id"], "repo_type": p["repo_type"], "commit": info.sha,
            "private": private, "gated": gated, "access_state": access,
            "license": license_name if isinstance(license_name, str) else "unspecified",
            "card_metadata": card.to_dict() if card and hasattr(card, "to_dict") else dict(card or {}), "files": files}


def safe_open(path):
    path = Path(path).absolute()
    # Symlinks are checked at each ancestor on every platform. POSIX also uses
    # no-follow descriptor traversal, preventing replacement of checked parents.
    if os.name == "nt":
        for ancestor in [path, *path.parents]:
            if ancestor.is_symlink() or bool(getattr(ancestor.lstat(), "st_file_attributes", 0) & 0x400):
                raise WorkerError("hub_verification_failed")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    else:
        parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
    handle = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        handle.close()
        raise WorkerError("hub_verification_failed")
    return handle



class BoundedUploadFile(io.BufferedIOBase):
    """Keep SDK file-object operations bounded, including regular-upload base64.

    Large artifacts use the SDK's streamed LFS path. If a server requests a large
    regular upload, fail rather than buffering the entire artifact into JSON.
    """
    def __init__(self, handle, size):
        self.handle, self.size = handle, size

    def read(self, size=-1):
        requested = max(0, self.size - self.tell()) if size is None or size < 0 else size
        if requested > MAX_METADATA:
            raise WorkerError("hub_upload_memory_budget")
        return self.handle.read(size)

    def seek(self, offset, whence=0):
        return self.handle.seek(offset, whence)

    def tell(self):
        return self.handle.tell()

    def fileno(self):
        return self.handle.fileno()

    def readable(self):
        return True

    def seekable(self):
        return True


def hashes(handle, size):
    sha = hashlib.sha256()
    git = hashlib.sha1(f"blob {size}\0".encode(), usedforsecurity=False)
    total = 0
    while chunk := handle.read(1024 * 1024):
        total += len(chunk)
        if total > size:
            raise WorkerError("hub_budget_exceeded")
        sha.update(chunk)
        git.update(chunk)
    if total != size:
        raise WorkerError("hub_verification_failed")
    return sha.hexdigest(), git.hexdigest()


def verify_commit(api, p, commit):
    if not re.fullmatch(r"[a-f0-9]{40}", commit or ""):
        raise WorkerError("hub_verification_failed")
    info = api.repo_info(p["repo_id"], repo_type=p["repo_type"], revision=commit, timeout=20)
    if (info.sha != commit or bool(info.private) != (p["visibility"] == "private")
            or (getattr(info, "card_data", None) or {}).get("license") != p["license"]):
        raise WorkerError("hub_verification_failed")
    found = {}
    for offset in range(0, len(p["files"]), 100):
        paths = [item["path"] for item in p["files"][offset:offset + 100]]
        for item in api.get_paths_info(p["repo_id"], paths=paths, repo_type=p["repo_type"], revision=commit):
            found[item.path] = file_record(item)
    for item in p["files"]:
        actual = found.get(item["path"], {})
        if actual.get("size_bytes") != item["size_bytes"] or not (
                actual.get("sha256") == item["sha256"] or actual.get("git_blob_sha1") == item["git_blob_sha1"]):
            raise WorkerError("hub_verification_failed")
    return {"commit": commit, "verified": True,
            "url": f"https://huggingface.co/{'datasets/' if p['repo_type'] == 'dataset' else ''}{p['repo_id']}/tree/{commit}"}


def validate_staging(root, byte_limit):
    """Reject reused SDK staging with links, special files or unbounded leftovers."""
    root = Path(root).absolute()
    for ancestor in [root, *root.parents]:
        info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise WorkerError("hub_verification_failed")
    total, count = 0, 0
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(parent) / name
            info = path.lstat()
            count += 1
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise WorkerError("hub_verification_failed")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise WorkerError("hub_verification_failed")
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
            if total > byte_limit or count > MAX_FILES * 6:
                raise WorkerError("hub_budget_exceeded")


def execute(action, p, api, *, token=False, sdk=None, session_budget=None):
    from huggingface_hub.errors import RepositoryNotFoundError
    if action == "license.inspect":
        repo = inspect(api, p)
        if repo["access_state"] not in {"available", "gated_authorized"}:
            raise WorkerError("hub_gated")
        candidates = [item for item in repo["files"] if re.fullmatch(r"(?i)(readme|license|licence|notice|copying|authors)(?:[._-][a-z0-9-]+)?(?:\.(?:md|txt|rst))?", Path(item["path"]).name)]
        if len(candidates) > 32 or sum(item["size_bytes"] for item in candidates) > 4 * 1024 * 1024 or any(item["size_bytes"] > 1024 * 1024 for item in candidates):
            raise WorkerError("hub_budget_exceeded")
        documents = []
        for item in candidates:
            with tempfile.TemporaryDirectory(prefix="modelbuilder-license-") as temporary:
                result = execute("download", {**p, "commit": repo["commit"], "files": [item], "staging_dir": temporary}, api, sdk=sdk, token=token, session_budget=session_budget)
                path = result["files"][0]["local_path"]
                with safe_open(path) as handle:
                    sha, git = hashes(handle, item["size_bytes"])
                    if not (item.get("sha256") == sha or item.get("git_blob_sha1") == git):
                        raise WorkerError("hub_verification_failed")
                    handle.seek(0)
                    text = handle.read(1024 * 1024).decode("utf-8")
                prefix = "datasets/" if p["repo_type"] == "dataset" else ""
                documents.append({"path": item["path"], "size_bytes": item["size_bytes"], "sha256": sha,
                                  "git_blob_sha1": git, "text": text,
                                  "source_url": f"https://huggingface.co/{prefix}{p['repo_id']}/blob/{repo['commit']}/{quote(item['path'])}"})
        return {**repo, "documents": documents}
    if action == "card.inspect":
        import yaml
        from huggingface_hub import ModelCard

        class UniqueKeys(yaml.SafeLoader):
            pass

        def mapping(loader, node, deep=False):
            result = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=deep)
                if not isinstance(key, str) or key in result:
                    raise WorkerError("hub_license_mismatch")
                result[key] = loader.construct_object(value_node, deep=deep)
            return result
        UniqueKeys.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", p["card"], re.S)
        if not match:
            raise WorkerError("hub_license_mismatch")
        metadata = yaml.load(match[1], Loader=UniqueKeys)
        if not isinstance(metadata, dict) or not isinstance(metadata.get("license"), str):
            raise WorkerError("hub_license_mismatch")
        official = ModelCard(p["card"]).data.get("license")
        if metadata["license"] != official:
            raise WorkerError("hub_license_mismatch")
        return {"license": official}
    if action == "search":
        listing = api.list_models if p["repo_type"] == "model" else api.list_datasets
        result = []
        for item in listing(search=p["query"], limit=p.get("limit", 20)):
            if len(result) >= p.get("limit", 20):
                break
            result.append({"repo_id": item.id, "repo_type": p["repo_type"], "private": bool(item.private),
                           "gated": bool(getattr(item, "gated", False))})
        return {"items": result}
    if action in {"inspect", "destination"}:
        try:
            return {"exists": True, **inspect(api, p)}
        except RepositoryNotFoundError:
            if action == "destination":
                return {"exists": False, "access_state": "private_or_missing"}
            raise
    if action == "download":
        destination = Path(p["staging_dir"])
        if not destination.is_dir() or any(destination.iterdir()):
            raise WorkerError("hub_verification_failed")
        validate_staging(destination, p.get("total_bytes", sum(item["size_bytes"] for item in p["files"])) * 2 + MAX_METADATA)
        result = []
        for item in p["files"]:
            if session_budget is not None:
                session_budget[0] = max(item["size_bytes"], 1)
            path = sdk.hf_hub_download(repo_id=p["repo_id"], repo_type=p["repo_type"], revision=p["commit"],
                                       filename=item["path"], local_dir=str(destination), token=token,
                                       etag_timeout=20, library_name="modelbuilder")
            validate_staging(destination, p.get("total_bytes", sum(item["size_bytes"] for item in p["files"])) * 2 + MAX_METADATA)
            result.append({"path": item["path"], "local_path": str(path)})
        return {"files": result}
    if action == "publish":
        current = execute("destination", p, api)
        if p["create_repo"]:
            if current["exists"]:
                raise WorkerError("hub_destination_changed")
            api.create_repo(p["repo_id"], repo_type=p["repo_type"], private=p["visibility"] == "private", exist_ok=False)
            current = execute("destination", p, api)
        elif (not current["exists"] or current["commit"] != p["parent_commit"]
              or current["private"] != (p["visibility"] == "private")):
            raise WorkerError("hub_destination_changed")
        with ExitStack() as stack:
            operations = []
            for item in p["files"]:
                handle = stack.enter_context(safe_open(Path(p["snapshot_dir"]) / item["path"]))
                if hashes(handle, item["size_bytes"]) != (item["sha256"], item["git_blob_sha1"]):
                    raise WorkerError("hub_verification_failed")
                handle.seek(0)
                operations.append(sdk.CommitOperationAdd(path_in_repo=item["path"], path_or_fileobj=BoundedUploadFile(handle, item["size_bytes"])))
            commit = api.create_commit(p["repo_id"], repo_type=p["repo_type"], operations=operations,
                                       commit_message=p["commit_marker"], parent_commit=current["commit"],
                                       revision="main", num_threads=1)
        return verify_commit(api, p, commit.oid)
    if action == "reconcile":
        for i, commit in enumerate(api.list_repo_commits(p["repo_id"], repo_type=p["repo_type"], revision="main")):
            if i >= 200:
                break
            if commit.title == p["commit_marker"]:
                return verify_commit(api, p, commit.commit_id)
        return {"verified": False, "state": "needs_reconciliation"}
    raise WorkerError("hub_provider_failed")



def response_allowance(prepared, streamed, status, artifact_budget):
    """Only successful streamed file GETs use the artifact budget, never JSON/errors."""
    from urllib.parse import urlsplit
    metadata = (not streamed or prepared.method != "GET" or status >= 400
                or urlsplit(prepared.url).path.startswith("/api/"))
    return MAX_METADATA if metadata else artifact_budget


def main(request):
    if importlib.metadata.version("huggingface_hub") != SDK_VERSION:
        raise WorkerError("hub_dependency_unavailable")
    import huggingface_hub as sdk
    import requests
    # Supported SDK HTTP customization: TLS stays enabled, implicit auth/proxies
    # are disabled, requests and streamed response bytes have hard upper bounds.
    budget = [MAX_METADATA]

    class Session(requests.Session):
        def __init__(self):
            super().__init__()
            self.trust_env = False

        def request(self, method, url, **kwargs):
            kwargs["timeout"] = (10, 20)
            return super().request(method, url, **kwargs)

        def send(self, prepared, **kwargs):
            streamed = kwargs.get("stream", False)
            kwargs["stream"] = True
            response = super().send(prepared, **kwargs)
            iterator = response.iter_content
            allowance = response_allowance(prepared, streamed, response.status_code, budget[0])
            requested_range = prepared.headers.get("Range", "")
            if re.fullmatch(r"bytes=[0-9]+-", requested_range):
                allowance = max(0, allowance - int(requested_range[6:-1]))

            def bounded(*args, **options):
                total = 0
                try:
                    args = list(args)
                    requested = args[0] if args else options.get("chunk_size", MAX_STREAM_CHUNK)
                    chunk_size = min(requested, MAX_STREAM_CHUNK) if type(requested) is int and requested > 0 else MAX_STREAM_CHUNK
                    if args:
                        args[0] = chunk_size
                    else:
                        options["chunk_size"] = chunk_size
                    for chunk in iterator(*args, **options):
                        total += len(chunk)
                        if total > allowance:
                            raise WorkerError("hub_budget_exceeded")
                        yield chunk
                finally:
                    response.close()
            response.iter_content = bounded
            if not streamed:
                response.content
            return response

    sdk.configure_http_backend(backend_factory=Session)
    token = False
    if request.get("credential"):
        import keyring
        provider = keyring.get_keyring()
        module = type(provider).__module__
        if not module.startswith(("keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService", "keyring.backends.kwallet")):
            raise WorkerError("hub_account_unavailable")
        credential = request["credential"]
        token = provider.get_password(credential["service"], credential["username"])
        if not token:
            raise WorkerError("hub_account_unavailable")
    api = sdk.HfApi(endpoint="https://huggingface.co", token=token, library_name="modelbuilder")
    return execute(request["action"], request["payload"], api, token=token, sdk=sdk, session_budget=budget)


if __name__ == "__main__":
    import sys
    request = json.loads(sys.stdin.buffer.read(16 * 1024 * 1024 + 1))
    try:
        result = main(request)
    except Exception as error:
        code = getattr(error, "code", None)
        if code is None:
            code = {"GatedRepoError": "hub_gated", "RepositoryNotFoundError": "hub_private_or_missing",
                    "RevisionNotFoundError": "hub_revision_missing", "HfHubHTTPError": "hub_access_denied",
                    "PackageNotFoundError": "hub_dependency_unavailable", "ModuleNotFoundError": "hub_dependency_unavailable"}.get(type(error).__name__, "hub_provider_failed")
        result = {"error": {"code": code}}
    target = Path(request["result_path"])
    target.write_text(json.dumps(result, allow_nan=False))
