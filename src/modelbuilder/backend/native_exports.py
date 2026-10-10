"""Resolve existing exports through native ownership, never a user-supplied path."""
from __future__ import annotations

import hashlib
from contextlib import ExitStack, contextmanager
import os
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from uuid import UUID

from .admission import BackendError
from .common import read_json, recipe_provenance
from .components import identity, read_checkpoint
from ..export_packages import CHUNK_BYTES, MAX_EXPORT_BYTES, inspect_export, package_export, safe_file


@contextmanager
def native_directory(artifact, root: Path):
    """Read the pinned native Path format without importing a custom materializer.

    Extraction is bounded and manual: archives cannot create links, devices,
    absolute paths or traversal, even before export manifest verification.
    """
    materializer = artifact.materializer
    if (materializer.module, materializer.attribute) != ("zenml.materializers.path_materializer", "PathMaterializer"):
        raise ValueError("Only the native Path artifact format can be exported")
    uri = Path(artifact.uri)
    if not uri.is_absolute() or not uri.is_relative_to(root / "zenml-stores") or ".." in uri.parts:
        raise ValueError("Export artifact is not in the owned local native artifact store")
    with tempfile.TemporaryDirectory(prefix="modelbuilder-export-read-") as temporary:
        directory, names, size, count = Path(temporary), set(), 0, 0
        with safe_file(uri / "data.tar.gz") as source:
            if os.fstat(source.fileno()).st_size > MAX_EXPORT_BYTES + 16 * 1024**2:
                raise ValueError("Native export archive is oversized")
            with tarfile.open(fileobj=source, mode="r|gz") as archive:
                for member in archive:
                    count += 1
                    relative = PurePosixPath(member.name)
                    if relative == PurePosixPath(".") and member.isdir():
                        continue
                    if (count > 20000 or len(member.name.encode()) > 1024 or relative.is_absolute()
                            or ".." in relative.parts or "\\" in member.name or ":" in member.name
                            or not (member.isdir() or member.isfile()) or member.size < 0):
                        raise ValueError("Unsafe native export archive entry")
                    name = str(relative)
                    if name.casefold() in names:
                        raise ValueError("Duplicate native artifact archive entry")
                    names.add(name.casefold())
                    size += member.size
                    if size > MAX_EXPORT_BYTES + 16 * 1024**2:
                        raise ValueError("Native export archive exceeds its size bound")
                    destination = directory / relative
                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    incoming = archive.extractfile(member)
                    if incoming is None:
                        raise ValueError("Missing native artifact bytes")
                    with incoming, destination.open("xb") as outgoing:
                        remaining = member.size
                        while remaining:
                            chunk = incoming.read(min(CHUNK_BYTES, remaining))
                            if not chunk:
                                raise ValueError("Truncated native artifact")
                            outgoing.write(chunk)
                            remaining -= len(chunk)
        yield directory


def invoke(operation: str, payload: dict, root: Path):
    with ExitStack() as stack:
        return _invoke(operation, payload, root, stack)


def _invoke(operation: str, payload: dict, root: Path, stack: ExitStack):
    from .sdk import artifacts_for, find_version, metadata_values, native_run, project_client
    client, project = project_client(payload["project_id"])
    requested_version = find_version(client, project, payload) if payload.get("model_version_id") else None
    matches = []
    for job in (root / "jobs").iterdir():
        request, native = read_json(job / "request.json"), read_json(job / "native.json")
        if request.get("project_id") != payload["project_id"]:
            continue
        if payload.get("backend_run_id") and native.get("backend_run_id") != payload["backend_run_id"]:
            continue
        if requested_version and native.get("model_version_ref") != str(requested_version.id):
            continue
        matches.append((job, request))
    if len(matches) != 1:
        raise BackendError("not_found", "Select one native run or its model version in this project")
    job, request = matches[0]
    _, run = native_run(job)
    if not run or not run.model_version or run.status.value not in {"completed", "cached"}:
        raise BackendError("validation_error", "Model export requires a completed native run and version")
    version = find_version(client, project, {**payload, "model_version_id": str(run.model_version.id)})
    if requested_version and version.id != requested_version.id:
        raise BackendError("not_found", "Run does not own the selected model version")
    artifacts = artifacts_for(job, run)
    nodes = {node["id"]: node for node in request["recipe"]["nodes"]
             if node["kind"] == "model.export" or node.get("component", {}).get("role") == "export"}
    exports = [artifact for artifact in artifacts if artifact.get("node_id") in nodes]
    if operation != "model.export.list":
        exports = [artifact for artifact in exports if artifact["artifact_id"] == payload["artifact_id"]]
        if len(exports) != 1:
            raise BackendError("not_found", "Artifact is not a declared export of this run and version")
    results, diagnostics = [], []
    for record in exports:
        try:
            artifact = client.get_artifact_version(UUID(record["artifact_id"]))
            if str(artifact.project.id) != str(project.id):
                raise ValueError("Native artifact project differs")
            value = stack.enter_context(native_directory(artifact, root))
            if not isinstance(value, (Path, str)):
                raise ValueError("Native export must be a file artifact")
            path = Path(value)
            metadata = metadata_values(version)
            provenance = {"project_id": payload["project_id"], "backend_run_id": str(run.id),
                          "model_version_id": str(version.id), "artifact_id": record["artifact_id"],
                          "node_id": record["node_id"], "port": record["port"],
                          **recipe_provenance(request["recipe"]), "source": request["recipe"]["source"],
                          "environment": request["recipe"]["environment"],
                          "selection": {key: val for key, val in metadata.items() if key.startswith("selection_")}}
            native_execution = getattr(getattr(run, "config", None), "extra", {}).get("execution")
            if native_execution is not None:
                provenance["execution"] = native_execution
            descriptor = inspect_export(path, provenance)
            # Reverify the exact checkpoint declared by the export against connected
            # native inputs. Never silently substitute 'last' for selected/best.
            step = run.steps[record["node_id"]]
            path_ports = {port["name"] for port in nodes[record["node_id"]]["inputs"] if port["type"] in {"Path", "checkpoint"}}
            connected = [(port, artifact) for port, inputs in step.inputs.items() if port in path_ports for artifact in inputs]
            identities = []
            for input_port, checkpoint_artifact in connected:
                if str(checkpoint_artifact.project.id) != str(project.id):
                    raise ValueError("Checkpoint artifact project differs")
                checkpoint_path = stack.enter_context(native_directory(checkpoint_artifact, root))
                if not isinstance(checkpoint_path, (Path, str)):
                    continue
                checkpoint_path = Path(checkpoint_path)
                if (checkpoint_path / "checkpoint.manifest.json").is_file():
                    selected = identity(read_checkpoint(checkpoint_path))
                elif nodes[record["node_id"]]["kind"] == "model.export":
                    kind = nodes[record["node_id"]].get("parameters", {}).get("checkpoint_kind", "best")
                    filename = "best.pt" if kind == "best" else "checkpoint.pt"
                    with safe_file(checkpoint_path / filename) as source:
                        selected = hashlib.file_digest(source, "sha256").hexdigest()
                else:
                    continue
                if selected == descriptor["manifest"]["selected_checkpoint"]:
                    identities.append({"artifact_id": str(checkpoint_artifact.id), "input_port": input_port, "identity": selected})
            if not identities:
                raise ValueError("Export selected checkpoint does not match a connected native checkpoint")
            provenance["selected_checkpoints"] = identities
            descriptor = inspect_export(path, provenance)
            if operation == "model.export.package":
                return package_export(path, root / "export-packages", descriptor, payload["expected_export_digest"])
            results.append(descriptor)
        except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
            if operation != "model.export.list":
                raise BackendError("export_integrity_error", "Native export is missing, unsafe or changed", {"reason": str(error)}) from error
            diagnostics.append({"artifact_id": record["artifact_id"], "message": str(error)})
    return {"exports": results, "diagnostics": diagnostics} if operation == "model.export.list" else results[0]
