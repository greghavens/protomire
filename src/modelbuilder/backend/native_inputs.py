"""Native, project-owned artifact boundary inputs; no project-code imports.

Discovery hashes the persisted native artifact bytes only when explicitly
requested. Plan/submission/worker recheck the same bytes and ownership. Loading
is left to ZenML's public ArtifactVersionResponse pipeline input mechanism.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import stat
from uuid import UUID

from .admission import BackendError
from .common import read_json, recipe_provenance
from .components import identity


# Native materializations can hold checkpoints, but admission must do finite work.
# Count directories as well as files so empty directory trees are bounded too.
MAX_NATIVE_ENTRIES = 100_000
MAX_NATIVE_FILE_BYTES = 16 * 1024**3
MAX_NATIVE_TOTAL_BYTES = 16 * 1024**3
MAX_NATIVE_DEPTH = 128
NATIVE_READ_BYTES = 1024 * 1024
MAX_NATIVE_JSON_BYTES = 64 * 1024 * 1024


def _denied(message):
    raise BackendError("artifact_binding_denied", message)


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _hash_regular_file(fd, before, remaining):
    if before.st_size < 0 or before.st_size > min(MAX_NATIVE_FILE_BYTES, remaining):
        _denied("Native artifact exceeds its byte limit")
    digest, size = hashlib.sha256(), 0
    # Probe at most one byte past the initial size. Growing files cannot make
    # admission run indefinitely, and the digest is never built from a pathname.
    while chunk := os.read(fd, min(NATIVE_READ_BYTES, before.st_size + 1 - size)):
        size += len(chunk)
        if size > before.st_size:
            _denied("Native artifact changed while being read")
        digest.update(chunk)
    if size != before.st_size or _signature(os.fstat(fd)) != _signature(before):
        _denied("Native artifact changed while being read")
    return digest.hexdigest(), size


def storage_identity(artifact, root: Path) -> str:
    return _storage_identity(artifact, root, metadata_only=False)


def storage_fingerprint(artifact, root: Path) -> str:
    """Secure bounded inode/ctime witness for already content-verified evidence.

    Not a content digest and never a substitute for initial byte verification.
    Used only to reject changes between prepared verification and native commit.
    """
    return _storage_identity(artifact, root, metadata_only=True)


def _storage_identity(artifact, root: Path, *, metadata_only: bool) -> str:
    """Hash a bounded snapshot through nofollow, descriptor-relative POSIX reads.

    The lexical store boundary is checked before opening any artifact content.
    Every ancestor and child is opened without following links; enumeration and
    reads use those same descriptors. Files and directory entries are checked
    for replacement or mutation during the read. This is admission-time identity,
    not a guarantee that another process cannot change storage afterward.
    """
    if (os.name != "posix" or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd or os.stat not in os.supports_follow_symlinks
            or os.scandir not in os.supports_fd
            or any(not hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))):
        raise BackendError("capability_unavailable", "Secure native artifact directory reads are unavailable")
    handles, ancestors, entries = [], [], []
    count, total = 0, 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

    def walk(parent, parts=()):
        nonlocal count, total
        before = os.fstat(parent)
        if metadata_only:
            entries.append({"path": str(Path(*parts)), "stat": _signature(before)})
        names = []
        # Do not materialize an unbounded rglob/listdir result before enforcing
        # the count limit. scandir(fd) does not reopen a raced directory path.
        with os.scandir(parent) as scan:
            for entry in scan:
                count += 1
                if count > MAX_NATIVE_ENTRIES:
                    _denied("Native artifact exceeds its entry-count limit")
                names.append(entry.name)
        for name in sorted(names):
            relative = (*parts, name)
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            is_directory = stat.S_ISDIR(info.st_mode)
            if not is_directory and not stat.S_ISREG(info.st_mode):
                _denied("Native artifact contains a symlink or nonregular file")
            flags = (directory_flags if is_directory else
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
            child = os.open(name, flags, dir_fd=parent)
            try:
                if _signature(os.fstat(child)) != _signature(info):
                    _denied("Native artifact entry changed while being opened")
                if is_directory:
                    if len(relative) > MAX_NATIVE_DEPTH:
                        _denied("Native artifact exceeds its directory-depth limit")
                    walk(child, relative)
                else:
                    if metadata_only:
                        size = info.st_size
                        if size < 0 or size > min(MAX_NATIVE_FILE_BYTES, MAX_NATIVE_TOTAL_BYTES - total):
                            _denied("Native artifact exceeds its byte limit")
                        entries.append({"path": str(Path(*relative)), "stat": _signature(info)})
                    else:
                        digest, size = _hash_regular_file(child, info, MAX_NATIVE_TOTAL_BYTES - total)
                        entries.append({"path": str(Path(*relative)), "sha256": digest})
                    total += size
                if _signature(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _signature(info):
                    _denied("Native artifact entry changed while being read")
            finally:
                os.close(child)
        if _signature(os.fstat(parent)) != _signature(before):
            _denied("Native artifact directory changed while being read")

    try:
        raw, allowed = Path(artifact.uri), Path(root).absolute() / "zenml-stores"
        if (not raw.is_absolute() or ".." in raw.parts or ".." in allowed.parts
                or not raw.is_relative_to(allowed)):
            _denied("Artifact is outside the owned local native artifact store")
        parent = os.open(raw.anchor, directory_flags)
        handles.append(parent)
        for name in raw.parts[1:]:
            child = os.open(name, directory_flags, dir_fd=parent)
            handles.append(child)
            info = os.fstat(child)
            ancestors.append((parent, name, (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))))
            parent = child
        walk(parent)
        # Detect ancestor renames/swaps without treating unrelated sibling writes
        # in /tmp or the project root as changes to this artifact.
        for parent, name, expected in ancestors:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)) != expected:
                _denied("Native artifact storage ancestor changed while being read")
    except (OSError, TypeError, ValueError) as error:
        raise BackendError("artifact_binding_denied", "Native artifact storage is missing, unsafe, or unreadable") from error
    finally:
        for handle in reversed(handles):
            os.close(handle)
    if not entries:
        _denied("Native artifact content is absent")
    # Preserve the original Path-based ordering, including directory/file prefix
    # cases such as a/weights and a.json; joined-string ordering is different.
    entries.sort(key=lambda entry: Path(entry["path"]).parts)
    if metadata_only:
        return identity({"entries": entries, "ancestors": [(name, expected) for _, name, expected in ancestors]})
    return identity(entries)


def validate_native_type(artifact, declared_type):
    expected = {"dict": {"builtins.dict"}, "DatasetArtifact": {"builtins.dict"},
                "CheckpointArtifact": {"builtins.dict"}, "EvaluationArtifact": {"builtins.dict"},
                "ExportArtifact": {"builtins.dict"}, "str": {"builtins.str"}, "int": {"builtins.int"},
                "float": {"builtins.float"}, "bool": {"builtins.bool"},
                "Path": {"pathlib.Path", "pathlib.PosixPath", "pathlib.WindowsPath"}}
    expected.update(dataset=expected["dict"], metrics=expected["dict"], checkpoint=expected["Path"], artifact=expected["Path"], comparison=expected["dict"])
    materializers = {"zenml.materializers.path_materializer.PathMaterializer",
                    "zenml.materializers.built_in_materializer.BuiltInMaterializer",
                    "zenml.materializers.built_in_materializer.BuiltInContainerMaterializer"}
    if artifact.data_type.import_path not in expected.get(declared_type, set()) or artifact.materializer.import_path not in materializers:
        raise BackendError("artifact_type_mismatch", "Native artifact datatype or materializer does not match its declared typed port")


def _native_dict(artifact):
    """Read only ZenML's inert JSON container form, never nested materializers."""
    from ..export_packages import safe_file

    def finite(value):
        raise ValueError("Native JSON must contain finite values")

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Native JSON number overflows finite float range")
        return result

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Native JSON has duplicate keys")
            result[key] = value
        return result

    try:
        with safe_file(Path(artifact.uri) / "data.json", max_bytes=MAX_NATIVE_JSON_BYTES) as stream:
            raw = stream.read(MAX_NATIVE_JSON_BYTES + 1)
        if len(raw) > MAX_NATIVE_JSON_BYTES:
            raise ValueError("Native JSON exceeds its inspection bound")
        body = json.loads(raw, parse_constant=finite, parse_float=number, object_pairs_hook=unique)
        if not isinstance(body, dict):
            raise ValueError("Native dictionary artifact must contain a JSON object")
        return body
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise BackendError("artifact_binding_denied", "Native dictionary requires bounded inert data.json; nested materializers are not inspected") from error


def artifact_rights_descriptor(artifact, root: Path, declared_type: str):
    """Inert rights projection from exact native bytes, never producer imports."""
    validate_native_type(artifact, declared_type)
    before = storage_identity(artifact, root)
    value, protected = None, False
    from ..generated_artifacts import is_protected_dict, dict_rights, prepared_rights
    if artifact.data_type.import_path in {"pathlib.Path", "pathlib.PosixPath", "pathlib.WindowsPath"}:
        from .native_exports import native_directory
        from .components import read_checkpoint, read_export
        with native_directory(artifact, root) as directory:
            if (directory / "checkpoint.manifest.json").is_file():
                value, protected = read_checkpoint(directory).get("rights"), True
            elif (directory / "export.manifest.json").is_file():
                value, protected = read_export(directory).get("rights"), True
            elif (directory / "dataset.manifest.json").is_file():
                value, protected = prepared_rights(directory), True
            elif declared_type == "checkpoint":
                protected = True
    elif artifact.data_type.import_path == "builtins.dict":
        # A whitelisted container materializer can still dispatch nested custom
        # materializers. Do not call artifact.load() during rights preflight.
        body = _native_dict(artifact)
        protected = declared_type in {"CheckpointArtifact", "DatasetArtifact", "ExportArtifact"} or (
            isinstance(body, dict) and is_protected_dict(body))
        if protected:
            try:
                value = dict_rights(body) if isinstance(body, dict) else None
            except Exception as error:
                raise BackendError("license_rights_unreviewed", "Generated native input has invalid parent rights lineage") from error
    if storage_identity(artifact, root) != before:
        raise BackendError("artifact_binding_denied", "Native rights evidence changed during inspection")
    return {"rights_required": True, "rights": value} if protected else {}


def output_descriptor(job: Path, run, node_id: str, output_port: str, artifact_id: str) -> tuple[dict, object]:
    request = read_json(job / "request.json")
    recipe = request["recipe"]
    native_identity = getattr(getattr(run, "config", None), "extra", {})
    for key, value in recipe_provenance(recipe).items():
        if native_identity.get(key) != value:
            raise BackendError("artifact_binding_denied", "Producer recipe provenance disagrees with the immutable native run")
    if native_identity.get("application_project_id") != request["project_id"]:
        raise BackendError("artifact_binding_denied", "Producer native project provenance disagrees with the owning job")
    node = next((node for node in recipe["nodes"] if node["id"] == node_id), None)
    port = next((port for port in (node or {}).get("outputs", []) if port["name"] == output_port), None)
    from .native_invocations import output_step
    try:
        info, step = output_step(run, recipe, node_id, output_port, artifact_id)
    except ValueError as error:
        raise BackendError("artifact_binding_denied", str(error)) from error
    if port is None or step is None:
        raise BackendError("artifact_binding_denied", "Native binding producer is not a declared executed output")
    if request.get("execution") and node_id not in request["execution"]["node_ids"]:
        raise BackendError("artifact_binding_denied", "Native binding producer was outside the executed selection")
    artifact = next((a for a in step.outputs.get(output_port, []) if str(a.id) == artifact_id), None)
    if artifact is None or str(artifact.project.id) != str(run.project.id):
        raise BackendError("artifact_binding_denied", "Artifact does not belong to the requested native output and project")
    validate_native_type(artifact, port["type"])
    descriptor = {**info, "backend_run_id": str(run.id), "source_node_id": node_id, "source_port": output_port,
                  "artifact_id": str(artifact.id), "declared_type": port["type"],
                  "content_sha256": storage_identity(artifact, Path(request["root"])),
                  "producer_recipe_id": recipe["recipe_id"], "producer_recipe_revision": recipe["revision"],
                  "producer_recipe_digest": recipe_provenance(recipe)["recipe_digest"],
                  "producer_model_version_ref": str(run.model_version.id) if run.model_version else None,
                  "producer_production_eligible": recipe.get("execution", {}).get("purpose") != "smoke",
                  "producer_role": node.get("component", {}).get("role") or ("training" if node["kind"] == "model.train" else node["kind"])}
    if node.get("component", {}).get("role") == "derivation" and port["type"] == "Path":
        from .checkpoint_derivation import verify_native_output
        descriptor["producer_production_eligible"] = verify_native_output(run, request, node, info, step, artifact)["production_eligible"]
    descriptor.update(artifact_rights_descriptor(artifact, Path(request["root"]), port["type"]))
    from .native_cache import output_provenance
    descriptor["cache"] = output_provenance(step, run)
    return descriptor, artifact


def resolve_bindings(payload: dict, jobs: list[str], *, native_objects=False):
    from .sdk import project_client
    from zenml.enums import ExecutionStatus
    from ..execution import selection
    execution = selection(payload["recipe"], payload.get("execution"))
    client, project = project_client(payload["project_id"])
    resolved, objects = [], {}
    by_run = {}
    for value in jobs:
        job = Path(value)
        request, native = read_json(job / "request.json"), read_json(job / "native.json")
        if request.get("project_id") != payload["project_id"] or job.parent != Path(request.get("root", "")) / "jobs":
            raise BackendError("artifact_binding_denied", "Native input producer is not owned by this project")
        run_id = native.get("backend_run_id")
        if run_id:
            by_run[run_id] = job
    nodes = {node["id"]: node for node in payload["recipe"]["nodes"]}
    for binding in execution["input_bindings"]:
        run_id = binding["backend_run_id"]
        job = by_run.get(run_id)
        if job is None:
            raise BackendError("artifact_binding_denied", "Native artifact producer is unknown in this project")
        try:
            run = client.get_pipeline_run(UUID(run_id), project=project.id, include_full_metadata=True)
        except (KeyError, ValueError) as error:
            raise BackendError("artifact_binding_denied", "Native artifact producer is unavailable") from error
        if str(run.project.id) != str(project.id) or run.status != ExecutionStatus.COMPLETED:
            raise BackendError("artifact_binding_denied", "Native input requires a completed producer in this project")
        descriptor, artifact = output_descriptor(job, run, binding["source_node_id"], binding["source_port"], binding["artifact_id"])
        port = next(port for port in nodes[binding["node_id"]]["inputs"] if port["name"] == binding["input_port"])
        if port["type"] != descriptor["declared_type"]:
            raise BackendError("artifact_type_mismatch", "Bound native output and selected input have different declared types")
        if binding["content_sha256"] != descriptor["content_sha256"]:
            raise BackendError("artifact_content_mismatch", "Native artifact bytes differ from the explicitly pinned binding")
        resolved.append({**binding, **descriptor})
        objects[(binding["node_id"], binding["input_port"])] = artifact
    if native_objects:
        return resolved, objects
    return {"input_bindings": resolved}
