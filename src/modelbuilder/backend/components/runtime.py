"""Thin trusted-source adapter to the existing ZenML artifact/step runtime."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import sys
import tempfile
import importlib
import importlib.metadata
import platform
import os
from pathlib import Path

from . import PORT_TYPES, identity, read_checkpoint, read_export, validate_component, validate_resume
from ..dependency_files import dependency_sha256

_SNAPSHOT_ROOTS: set[Path] = set()


def load_callable(reference: dict, source_root: str, trusted_roots: list[str], source_files: list[dict]):
    if reference.get("module") in {"__main__", "__mp_main__"}:
        raise ValueError("Source implementations cannot replace reserved process module identities")
    root = Path(source_root).resolve()
    if not any(root.is_relative_to(Path(allowed).resolve()) for allowed in trusted_roots):
        raise ValueError("Component source root is not explicitly trusted")
    declared = {item["path"]: item["sha256"] for item in source_files}
    if declared.get(reference["path"]) != reference["sha256"]:
        raise ValueError("Component entrypoint is outside the pinned source closure")
    # Check the entire closure immediately before importing any user module.
    for relative, digest in declared.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Component source closure changed after admission: " + relative)
    # Execute a private source closure, not the mutable checkout. This provides
    # reproducibility for ordinary Python imports, not isolation from trusted code.
    owner = tempfile.TemporaryDirectory(prefix="modelbuilder-source-")
    snapshot = Path(owner.name).resolve()
    prior_snapshots = set(_SNAPSHOT_ROOTS)
    _SNAPSHOT_ROOTS.add(snapshot)
    for relative, digest in declared.items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Source closure paths must be relative")
        original = root / relative
        if original.is_symlink():
            raise ValueError("Source closure cannot contain symlinked files")
        data = original.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Source changed while staging its execution snapshot")
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    # Remove ordinary checkout import paths, including cwd's implicit entry.
    # Installed environment/site-packages and the platform runtime are retained.
    sys.path[:] = [entry for entry in sys.path if entry and Path(entry).resolve() not in {root, root / "src"}
                   and not any(Path(entry).resolve().is_relative_to(old) for old in prior_snapshots)]
    for module_name, existing in list(sys.modules.items()):
        filename = getattr(existing, "__file__", None)
        # The worker may live inside the configured source checkout. Its main
        # module is platform process identity, which ZenML needs to resolve the
        # next start/end hook; evicting it silently drops deadline hooks.
        platform_main = (filename and module_name in {"__main__", "__mp_main__"}
                         and Path(filename).resolve() == Path(__file__).resolve().parents[1] / "worker.py")
        if (filename and not platform_main and not (module_name == "modelbuilder" or module_name.startswith("modelbuilder."))
                and (Path(filename).resolve().is_relative_to(root) or any(Path(filename).resolve().is_relative_to(old) for old in prior_snapshots))
                and "site-packages" not in Path(filename).parts):
            sys.modules.pop(module_name, None)
    sys.path.insert(0, str(snapshot))
    if (snapshot / "src").is_dir():
        sys.path.insert(0, str(snapshot / "src"))
    name = reference["module"]
    if "." in name:
        importlib.import_module(name.rsplit(".", 1)[0])
    path = snapshot / reference["path"]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    module.__source_snapshot_owner__ = owner
    sys.modules[name] = module
    spec.loader.exec_module(module)
    function = getattr(module, reference["entrypoint"])
    if not callable(function):
        raise ValueError("Component entrypoint is not callable")
    function.__source_snapshot_root__ = str(snapshot)
    return function


def runtime_source_identity(package_root: Path | None = None) -> str:
    """Hash installed execution code, independent of checkout/installation paths."""
    root = package_root or Path(__file__).resolve().parents[2]
    files = list((root / "backend").rglob("*.py"))
    profile = root / "backend/components/mlx-darwin-arm64.lock"
    if profile.is_file():
        files.append(profile)
    files += [root / name for name in ("__init__.py", "recipe.py", "execution.py", "conditions.py", "iterations.py", "evaluation.py", "tracking.py", "dataset.py", "dataset_streaming.py", "parquet_stream.py", "parquet_stream_decode.py", "platform_file_access.py", "models.py", "store.py", "research_contract.py") if (root / name).is_file()]
    return identity({str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted(files)})


def runtime_environment() -> dict:
    """Capture the actual installed interpreter/dependency versions, not guesses."""
    packages = {distribution.metadata["Name"].lower().replace("_", "-"): distribution.version
                for distribution in importlib.metadata.distributions() if distribution.metadata["Name"]}
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "platform": platform.system(), "machine": platform.machine(), "packages": packages,
            "modelbuilder_runtime_source": runtime_source_identity()}


def input_identity(value):
    """Content identity for external artifacts; never use ephemeral host paths."""
    if isinstance(value, Path):
        if value.is_symlink() or not value.exists():
            raise ValueError("Component input artifact is missing or symlinked")
        files = [value] if value.is_file() else sorted(value.rglob("*"))
        if len(files) > 100000:
            raise ValueError("Component input artifact exceeds file-count bound")
        entries = []
        for path in files:
            if path.is_symlink():
                raise ValueError("Component input artifact contains a symlink")
            if path.is_file():
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                entries.append({"path": path.name if value.is_file() else str(path.relative_to(value)), "sha256": digest})
        return {"files": entries}
    return value


def dispatch(inputs: dict, spec: dict):
    from ..process_execution import context
    from ..process_execution import enabled, execute
    if enabled(spec):
        context(spec)
        from ..native_cache import record_outputs
        return record_outputs(spec, execute("component", inputs, spec))
    node = spec["component_node"]
    component = validate_component(node)
    for value in inputs.values():
        if isinstance(value, Path) and (value / "checkpoint.manifest.json").is_file():
            read_checkpoint(value)
        elif isinstance(value, Path) and (value / "export.manifest.json").is_file():
            read_export(value)
    parameters, output = context(spec)
    output.mkdir(parents=True, exist_ok=True)
    for dependency in spec.get("dependencies", []):
        path = spec.get("dependency_paths", {}).get(dependency["id"])
        if not path or dependency_sha256(path) != dependency["sha256"]:
            raise ValueError("Component dependency changed after admission: " + dependency["id"])
    actual_environment = runtime_environment()
    from ..placement import runtime_device_identity, effective_resume_modes
    from .mlx import selected_device_for_component, architecture_identity, probe
    selected_device = selected_device_for_component(component, spec.get("placement", {}).get("device", "cpu"))
    is_mlx = component.get('id', '').startswith('modelbuilder.mlx.') and 'mlx' in component.get('capabilities', {}).get('frameworks', [])
    if is_mlx:
        selected_placement = probe(os.environ.get('MODELBUILDER_MLX_PYTHON'), selected_device)
        if not selected_placement.get('available'):
            raise ValueError(selected_placement.get('reason', 'MLX worker unavailable'))
        actual_environment['framework_worker'] = selected_placement
    else:
        selected_placement = runtime_device_identity(selected_device)
    resume_modes = effective_resume_modes(selected_device, component["capabilities"]["resume_modes"])
    actual_environment["placement"] = selected_placement
    identities = {
        "architecture": architecture_identity(parameters) if is_mlx and component['role']=='training' else identity(parameters.get("architecture", parameters)),
        "source": identity(spec["source"]), "dependencies": identity(spec.get("dependencies", [])),
        "environment": identity({"declared": spec.get("environment", {}), "actual": actual_environment}),
        "training": identity({"component": component, "parameters": parameters}),
        "data": identity({k: input_identity(v) for k, v in inputs.items()})}
    run_context = {"protocol": component["protocol"], "output_dir": str(output),
                   "job_dir": spec["job_dir"], "node_id": spec["node_id"], "invocation": spec.get("invocation", {}), "identities": identities, "environment": actual_environment,
                   "device": selected_device, "placement": selected_placement, "resume_modes": resume_modes,
                   "source_root": spec["source_root"], "source": spec["source"],
                   "dependencies": spec.get("dependency_paths", {}),
                   "cancel_file": str(Path(spec["job_dir"]) / "cancel.json"),
                   "resume": None, "resume_mode": None}
    if component["role"] == "training" and spec.get("resume_path"):
        mode = spec.get("resume_mode", "exact_state")
        descriptor = validate_resume(Path(spec["resume_path"]), mode, identities,
                                     {**component["capabilities"], "resume_modes": resume_modes})
        run_context.update(resume={"path": spec["resume_path"], "descriptor": descriptor}, resume_mode=mode)
    if component["role"] == "training" and spec.get("iteration_resume_input"):
        previous = inputs[spec["iteration_resume_input"]]
        descriptor = validate_resume(previous, "warm_start", identities, {**component["capabilities"], "resume_modes": resume_modes})
        run_context.update(resume={"path": str(previous), "descriptor": descriptor, "artifact_id": spec["iteration_checkpoint_ref"]}, resume_mode="warm_start")
    function = load_callable(node["implementation_ref"], spec["source_root"], spec["trusted_code_roots"], spec["source"]["files"])
    run_context["source_root"] = function.__source_snapshot_root__
    coordinator = None
    if spec.get("live_evaluation"):
        from ..live_evaluation import LiveEvaluationCoordinator
        live = spec["live_evaluation"]
        evaluation_node = live["node"]
        evaluation_spec = {**spec, "node_id": evaluation_node["id"], "component_node": evaluation_node,
                           "parameters": evaluation_node.get("parameters", {}), "live_evaluation": None}
        evaluator = {"kind": "component", "spec": evaluation_spec,
                     "inputs": {target: inputs[source] for target, source in live["input_bindings"].items()},
                     "checkpoint_port": live["checkpoint_port"]}
        coordinator = LiveEvaluationCoordinator(Path(spec["job_dir"]), node["id"], evaluator,
            max_concurrency=live["settings"].get("max_concurrency", 1), timeout_seconds=live["settings"].get("timeout_seconds", 300))
        run_context["checkpoint_ready"] = coordinator.checkpoint_ready
    try:
        results = function(inputs=inputs, parameters=parameters, context=run_context)
    except BaseException:
        if coordinator:
            coordinator.close(cancel=True)
        raise
    else:
        if coordinator:
            coordinator.close()

    if not isinstance(results, dict) or set(results) != {p["name"] for p in node["outputs"]}:
        raise ValueError("Component results must exactly match declared output ports")
    values = []
    for port in node["outputs"]:
        value = results[port["name"]]
        expected = PORT_TYPES[port["type"]]
        if not isinstance(value, expected):
            raise ValueError("Component output type mismatch: " + port["name"])
        if isinstance(value, dict):
            json.dumps(value, allow_nan=False)
        if isinstance(value, Path):
            if not value.resolve().is_relative_to(output.resolve()) or not value.exists():
                raise ValueError("Component file artifact must exist within its output directory")
            if component["role"] == "training":
                ck = read_checkpoint(value)
                if ck["identities"] != identities:
                    raise ValueError("Checkpoint identities differ from actual component execution")
                if not set(ck["resume_modes"]).issubset(resume_modes):
                    raise ValueError("Checkpoint claims unsupported device/component resume capability; accelerator exact_state is unverified")
            if component["role"] == "export":
                manifest = read_export(value)
                selected = [identity(read_checkpoint(path)) for path in inputs.values()
                            if isinstance(path, Path) and (path / "checkpoint.manifest.json").is_file()]
                if not selected or manifest["selected_checkpoint"] not in selected:
                    raise ValueError("Export is not bound to its connected selected checkpoint")
        values.append(value)
    from ..native_cache import record_outputs
    from ..process_execution import in_child
    result = values[0] if len(values) == 1 else tuple(values)
    return result if in_child() else record_outputs(spec, result)


def _shim_inputs(node: dict, input_names: set[str] | None) -> list[dict]:
    inputs = node["inputs"]
    if input_names is None:
        return inputs
    if any(port.get("required", True) and port["name"] not in input_names for port in inputs):
        raise ValueError("Native shim cannot omit a required typed input")
    return [port for port in inputs if port["name"] in input_names]


def compile_component(node: dict, job: Path, *, input_names: set[str] | None = None):
    """Generate only a typed ABI shim, not model/training code, for native ZenML."""
    from zenml import step
    validate_component(node)
    # Index-generated identifiers keep user-controlled names out of executable code.
    inputs = _shim_inputs(node, input_names)
    tag = hashlib.sha256((node["id"] + repr(inputs)).encode()).hexdigest()[:20]
    name = "modelbuilder_adapter_" + tag
    arguments = [f"{p['name']}: {PORT_TYPES[p['type']].__name__}" for p in inputs]
    arguments.append("spec: dict")
    returns = [f"Annotated[{PORT_TYPES[p['type']].__name__}, {p['name']!r}]" for p in node["outputs"]]
    annotation = returns[0] if len(returns) == 1 else "tuple[" + ", ".join(returns) + "]"
    mapping = ", ".join(f"{p['name']!r}: {p['name']}" for p in inputs)
    call = f"dispatch({{{mapping}}}, spec)"
    # ZenML 0.97 recognizes multiple outputs only from a literal tuple return.
    body = (f"return {call}" if len(returns) == 1 else
            f"values = {call}\n    if not isinstance(values, (tuple, list)) or len(values) != {len(returns)}:\n"
            "        raise ValueError('Source result does not match declared output ports')\n    return "
            + ", ".join(f"values[{index}]" for index in range(len(returns))))
    source = ("from pathlib import Path\nfrom typing import Annotated\nfrom modelbuilder.backend.components.runtime import dispatch\n"
              f"def invoke({', '.join(arguments)}) -> {annotation}:\n    {body}\n")
    directory = job / "adapters"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + ".py")
    path.write_text(source)
    sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return step(module.invoke, enable_cache=False)


def dispatch_python(arguments: dict, spec: dict):
    from ..process_execution import context
    from ..process_execution import enabled, execute
    if enabled(spec):
        context(spec)
        from ..native_cache import record_outputs
        return record_outputs(spec, execute("python", arguments, spec))
    node = spec["component_node"]
    context(spec)
    function = load_callable(node["implementation_ref"], spec["source_root"], spec["trusted_code_roots"], spec["source"]["files"])
    from ..native_cache import record_outputs
    from ..process_execution import in_child
    result = function(**arguments)
    return result if in_child() else record_outputs(spec, result)


def compile_python(node: dict, job: Path, *, input_names: set[str] | None = None):
    """Persist the deadline/identity wrapper so ZenML reconstruction cannot drop it."""
    from zenml import step
    import keyword
    inputs = _shim_inputs(node, input_names)
    tag = hashlib.sha256((node["id"] + repr(inputs)).encode()).hexdigest()[:20]
    name = "modelbuilder_python_" + tag
    if any(port["type"] not in PORT_TYPES for port in node["inputs"] + node["outputs"]):
        raise ValueError("Python shim requires a declared supported native artifact type")
    input_types = {p["name"]: PORT_TYPES[p["type"]].__name__ for p in inputs}
    input_types.update({key: type(value).__name__ if type(value) in (str, int, float, bool, dict, list) else "dict"
                        for key, value in node.get("parameters", {}).items()})
    if any(not key.isidentifier() or keyword.iskeyword(key) or key == "spec" for key in input_types):
        raise ValueError("Python argument name is not a supported identifier")
    arguments = [f"{key}: {value}" for key, value in input_types.items()] + ["spec: dict"]
    returns = [f"Annotated[{PORT_TYPES[p['type']].__name__}, {p['name']!r}]" for p in node["outputs"]]
    annotation = returns[0] if len(returns) == 1 else "tuple[" + ", ".join(returns) + "]"
    mapping = ", ".join(f"{key!r}: {key}" for key in input_types)
    call = f"dispatch_python({{{mapping}}}, spec)"
    # ZenML 0.97 recognizes multiple outputs only from a literal tuple return.
    body = (f"return {call}" if len(returns) == 1 else
            f"values = {call}\n    if not isinstance(values, (tuple, list)) or len(values) != {len(returns)}:\n"
            "        raise ValueError('Source result does not match declared output ports')\n    return "
            + ", ".join(f"values[{index}]" for index in range(len(returns))))
    source = ("from pathlib import Path\nfrom typing import Annotated\nfrom modelbuilder.backend.components.runtime import dispatch_python\n"
              f"def invoke({', '.join(arguments)}) -> {annotation}:\n    {body}\n")
    directory = job / "adapters"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + ".py")
    path.write_text(source)
    sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return step(module.invoke, enable_cache=False)
