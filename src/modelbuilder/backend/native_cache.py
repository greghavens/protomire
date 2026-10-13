"""Conservative value-stage reuse through the public ZenML cache policy.

ZenML remains the only step/artifact registry and scheduler. Application digests
bind immutable execution intent; native input IDs bind individual upstream
artifacts. Cache output checks use inert JSON, never a project materializer.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys

from .admission import BackendError
from .common import read_json, recipe_provenance
from .components import identity

CACHE_PROTOCOL = "modelbuilder.native-value-cache/v1"
VALUE_TYPES = {"dict", "str", "int", "float", "bool", "DatasetArtifact"}
MAX_VALUE_BYTES = 16 * 1024 * 1024
MAX_CACHE_CANDIDATES = 1000


def supports_cache(node: dict) -> bool:
    """Only explicitly opted-in value functions; lifecycle outputs stay fresh."""
    if node.get("kind") not in {"code.python", "component.run"}:
        return False
    if node["kind"] == "component.run" and node.get("component", {}).get("role") not in {"dataset", "utility"}:
        return False
    return bool(node.get("outputs")) and all(
        port.get("type") in VALUE_TYPES for port in node.get("inputs", []) + node["outputs"])


def cache_identity(request: dict, node: dict, execution: dict, environment: dict) -> str:
    """Exclude only operational paths/IDs, never recipe or scientific identity."""
    return identity({"protocol": CACHE_PROTOCOL, "project_id": request["project_id"],
        "recipe": recipe_provenance(request["recipe"]), "node": node,
        "execution": execution, "environment": environment,
        "resume": {key: request.get(key) for key in (
            "parent_run_id", "parent_checkpoint_ref", "resume_mode", "checkpoint_kind",
            "parent_recipe_id", "parent_recipe_revision", "parent_recipe_digest")}})


def value_digest(value) -> str:
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 32 or count > 100000:
            raise ValueError("Cache values exceed bounded JSON complexity")
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError("Cache dictionaries require string keys")
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
        elif type(item) not in {str, int, float, bool, type(None)}:
            raise ValueError("Cache values must be plain finite JSON, without custom objects")
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_VALUE_BYTES:
        raise ValueError("Cache value exceeds its byte limit")
    return hashlib.sha256(raw).hexdigest()


def record_outputs(spec: dict, result):
    """Record output digests before ZenML serializes a successful value step."""
    if not spec.get("cache_identity"):
        return result
    from zenml import log_metadata
    ports = spec["component_node"]["outputs"]
    values = (result,) if len(ports) == 1 else result
    if not isinstance(values, (tuple, list)) or len(values) != len(ports):
        raise ValueError("Cached value outputs disagree with declared ports")
    expected = {"dict": dict, "DatasetArtifact": dict, "str": str, "int": int, "float": float, "bool": bool}
    if any(type(value) is not expected[port["type"]] for port, value in zip(ports, values)):
        raise ValueError("Cached value output differs from its exact declared type")
    log_metadata({"modelbuilder_cache_identity": spec["cache_identity"],
                  "modelbuilder_cache_outputs": {port["name"]: value_digest(value) for port, value in zip(ports, values)}})
    return result


def _read_native_json(uri: str, root: Path):
    """Read only native data.json using bounded nofollow descriptor-relative IO."""
    from .native_inputs import _signature
    raw, allowed = Path(uri), root.absolute() / "zenml-stores"
    if (os.name != "posix" or not raw.is_absolute() or ".." in raw.parts or
            not raw.is_relative_to(allowed) or os.open not in os.supports_dir_fd or
            not hasattr(os, "O_NOFOLLOW")):
        raise ValueError("Cached value is outside supported owned native storage")
    handles, ancestors = [], []
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        parent = os.open(raw.anchor, directory_flags)
        handles.append(parent)
        for name in raw.parts[1:]:
            child = os.open(name, directory_flags, dir_fd=parent)
            handles.append(child)
            info = os.fstat(child)
            ancestors.append((parent, name, (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))))
            parent = child
        # Never take the container materializer's alternate dynamic-import path.
        fd = os.open("data.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        handles.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_VALUE_BYTES:
            raise ValueError("Cached JSON is not a bounded regular file")
        data = bytearray()
        while chunk := os.read(fd, min(1024 * 1024, before.st_size + 1 - len(data))):
            data.extend(chunk)
            if len(data) > before.st_size:
                raise ValueError("Cached JSON changed while being read")
        if (len(data) != before.st_size or _signature(os.fstat(fd)) != _signature(before) or
                _signature(os.stat("data.json", dir_fd=parent, follow_symlinks=False)) != _signature(before)):
            raise ValueError("Cached JSON changed while being read")
        for parent, name, expected in ancestors:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)) != expected:
                raise ValueError("Cached storage changed while being read")
        return json.loads(data)
    finally:
        for handle in reversed(handles):
            os.close(handle)


def _validate_cache_request(backend, request: dict):
    """Revalidate a private persisted job without admitting public authority fields.

    Reviewed assets remain uncacheable. Only the two worker-owned envelope fields
    are removed after their integrity and exact asset-free intent are verified;
    ordinary public admission still checks source/dependency bytes and bindings.
    """
    from .execution_rights import prepare_authorization, recheck_authorization
    from .admission import validate_execution
    from ..execution import selection, selected_recipe

    authorization = request.get("rights_authorization")
    recheck_authorization(authorization, project_id=request.get("project_id"))
    receipt = authorization["receipt"]
    if (receipt.get("scope") != "no_declared_external_asset_inputs"
            or authorization["policies"] or receipt.get("native_input_rights")):
        raise BackendError("license_cache_unavailable", "Reviewed asset execution cannot reuse native value caches")
    public = {key: value for key, value in request.items()
              if key not in {"rights_authorization", "rights_extensions"}}
    recipe = selected_recipe(request["recipe"], selection(request["recipe"], request.get("execution")))
    regenerated = prepare_authorization(backend, public, recipe)["receipt"]
    fields = ("project_id", "scope", "declarations", "nodes", "native_input_rights")
    if (any(receipt.get(key) != regenerated.get(key) for key in fields)
            or request.get("rights_extensions") != regenerated["declarations"]["extensions"]):
        raise BackendError("license_policy_changed", "Cached source, nodes or rights declarations differ from the admitted receipt")
    validate_execution(backend, public)


def verify_cached_outputs(expected_identity: str, node_id: str) -> str:
    """ZenML public cache_func: fail closed on corrupt existing candidates.

    The native engine still selects and reuses the candidate. This check neither
    creates cached step runs nor searches an application-owned artifact registry.
    """
    from zenml.client import Client
    from .native_inputs import validate_native_type
    from .adapter import ZenMLBackend
    from .components.runtime import runtime_environment
    from ..execution import selection
    client = Client()
    request = read_json(Path(os.environ["MODELBUILDER_JOB_DIR"]) / "request.json")
    # Cached execution skips the normal source/dependency loader. Reuse the same
    # read-only admission immediately before native lookup rather than bypass it.
    backend = ZenMLBackend(Path(request["root"]).parent, python_executable=sys.executable,
        source_root=request["source_root"], trusted_code_roots=request["trusted_code_roots"],
        dependency_paths=request.get("dependency_paths", {}))
    backend.dataset_source_guards = request.get("dataset_source_guards", {})
    _validate_cache_request(backend, request)
    node = next(node for node in request["recipe"]["nodes"] if node["id"] == node_id)
    execution = {**selection(request["recipe"], request.get("execution")),
                 "input_bindings": request.get("resolved_input_bindings", [])}
    if cache_identity(request, node, execution, runtime_environment()) != expected_identity:
        raise ValueError("Native cache execution identity changed after admission")
    candidates = client.list_run_steps(project=client.active_project.id, status="completed",
        run_metadata="modelbuilder_cache_identity:" + expected_identity, size=MAX_CACHE_CANDIDATES,
        hydrate=True)
    if candidates.total > MAX_CACHE_CANDIDATES:
        raise ValueError("Native cache validation exceeds its candidate bound; disable caching for this revision")
    for candidate in candidates.items:
        metadata = {key: getattr(value, "value", value) for key, value in candidate.run_metadata.items()}
        if str(candidate.project.id) != str(client.active_project.id) or metadata.get("modelbuilder_cache_identity") != expected_identity:
            raise ValueError("Native cached step identity or project differs")
        run = client.get_pipeline_run(candidate.pipeline_run_id, project=client.active_project.id)
        if run.config.extra.get("application_project_id") != request["project_id"]:
            raise ValueError("Native cached producer belongs to a different application project")
        digests = metadata.get("modelbuilder_cache_outputs", {})
        node = next((node for node in request["recipe"]["nodes"] if node["id"] == candidate.name), None)
        if node is None or not supports_cache(node) or set(digests) != set(candidate.outputs):
            raise ValueError("Native cached outputs do not match the immutable declared stage")
        declared = {port["name"]: port["type"] for port in node["outputs"]}
        if set(declared) != set(digests):
            raise ValueError("Native cached output ports differ")
        for port, versions in candidate.outputs.items():
            if len(versions) != 1:
                raise ValueError("Cached value ports require exactly one native artifact")
            artifact = versions[0]
            validate_native_type(artifact, declared[port])
            if str(artifact.project.id) != str(candidate.project.id) or artifact.producer_step_run_id != candidate.id:
                raise ValueError("Cached artifact ownership differs from its original native producer")
            if value_digest(_read_native_json(artifact.uri, Path(request["root"]))) != digests[port]:
                raise ValueError("Cached native value differs from its recorded SHA256")
    return expected_identity


def configure_cache(function, node: dict, job: Path, digest: str):
    """A tiny generated no-argument callback adapts ZenML's public CachePolicy."""
    from zenml.config import CachePolicy
    if not supports_cache(node):
        raise BackendError("capability_unavailable", "Native caching requires a supported value-producing stage")
    name = "modelbuilder_cache_" + hashlib.sha256(node["id"].encode()).hexdigest()[:20]
    directory = job / "adapters"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + ".py")
    path.write_text("from modelbuilder.backend.native_cache import verify_cached_outputs\n"
                    f"def cache_identity():\n    return verify_cached_outputs({digest!r}, {node['id']!r})\n")
    sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return function.with_options(enable_cache=True, cache_policy=CachePolicy(
        include_step_code=True, include_step_parameters=False,
        include_artifact_values=False, include_artifact_ids=True,
        # Dynamic ZenML materializes ordinary parameters as fresh artifacts.
        # Their values and the ABI spec are already covered by our SHA256 key;
        # actual declared input port IDs must remain in the native key.
        ignored_inputs=["spec", *[name for name in node.get("parameters", {})
                                 if name not in {port["name"] for port in node.get("inputs", [])}]],
        cache_func=module.cache_identity))


def output_provenance(step_run, run) -> dict:
    """Expose original native ownership rather than relabeling a cache hit."""
    cached = step_run.status.value == "cached"
    original_id = getattr(step_run, "original_step_run_id", None)
    result = {"reused": cached, "original_step_run_id": str(original_id) if original_id else None,
              "producer_backend_run_id": str(run.id), "producer_step_run_id": str(step_run.id)}
    if cached:
        from zenml.client import Client
        if not original_id:
            raise BackendError("artifact_binding_denied", "Cached native step has no original producer")
        original = Client().get_run_step(original_id)
        if str(original.project.id) != str(run.project.id) or original.status.value != "completed":
            raise BackendError("artifact_binding_denied", "Cached original step is not a completed producer in this project")
        if {key: [str(a.id) for a in values] for key, values in original.outputs.items()} != {
                key: [str(a.id) for a in values] for key, values in step_run.outputs.items()}:
            raise BackendError("artifact_binding_denied", "Cached outputs differ from the original native producer")
        result.update(producer_backend_run_id=str(original.pipeline_run_id), producer_step_run_id=str(original.id))
    return result
