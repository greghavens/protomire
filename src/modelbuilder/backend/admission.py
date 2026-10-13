"""Read-only execution admission shared by planning and submission.

No ZenML client, journal writes, environment installation, or user-module imports
are permitted here. A successful plan is a snapshot, revalidated at submission.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from jsonschema.exceptions import ValidationError, SchemaError
from .common import recipe_provenance
from .dependency_files import dependency_sha256

BUILTINS = {"dataset.import", "dataset.synthetic", "dataset.validate", "model.initialize", "model.train", "model.evaluate", "model.checkpoint", "model.export", "model.compare"}

PLATFORM_BUILTINS = {"dataset.version.import"}

class BackendError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}

def validate_execution(backend, payload: dict) -> dict:
    backend._require_capability()
    from .placement import is_local_target, validate_placement
    if not is_local_target(payload.get("target", "local-cpu")):
        raise BackendError("capability_unavailable", "Remote execution and spend are disabled")
    if payload.get("trusted") is not True:
        raise BackendError("untrusted_execution_blocked", "Explicit trust consent is required for local source execution; untrusted code has no verified isolation boundary")
    placement = validate_placement(backend, payload)
    from ..tracking import resolve_tracking, TrackingPolicyError
    try:
        resolve_tracking(run_override=payload.get("tracking"), online_consent=payload.get("tracking_online_consent") is True)
    except TrackingPolicyError as error:
        raise BackendError("tracking_consent_required", str(error)) from error
    recipe = payload.get("recipe", {})
    validate_recipe_settings(recipe)
    if payload.get("project_recipe_id") and recipe.get("recipe_id") != payload["project_recipe_id"]:
        raise BackendError("conflict", "Execution must use the project's single canonical recipe identity")
    if payload.get("project_id") and recipe.get("project_id") and recipe["project_id"] != payload["project_id"]:
        raise BackendError("conflict", "Recipe and execution project identities differ")
    if recipe.get("digest") and recipe["digest"] != recipe_provenance(recipe)["recipe_digest"]:
        raise BackendError("dependency_mismatch", "Declared recipe digest differs from executed immutable revision")
    from ..execution import selection, selected_recipe
    from ..models import ServiceError
    try:
        execution = selection(recipe, payload.get("execution"))
        recipe = selected_recipe(recipe, execution)
    except ServiceError as error:
        raise BackendError(error.code, error.message, error.details) from error
    except ValueError as error:
        raise BackendError("capability_unavailable", str(error), {"field": "evaluation.live"}) from error
    from .execution_rights import prepare_authorization
    prepare_authorization(backend, payload, recipe)
    selected_payload = {**payload, "recipe": recipe}
    if any(node.get("kind") in {"code.python", "component.run"} for node in recipe.get("nodes", [])) and not backend.trusted_code_roots:
        raise BackendError("untrusted_execution_blocked", "Arbitrary Python requires an operator-configured trusted source root")
    from .components import validate_live_evaluation
    device_kind = placement["device"].split(":")[0]
    for node in recipe.get("nodes", []):
        if device_kind.startswith('mlx-') and node.get('kind', '').startswith('model.'):
            raise BackendError('capability_unavailable', 'MLX placement requires reviewed MLX components; Torch fixture model nodes cannot execute in the MLX worker')
        if device_kind != "cpu" and node.get("kind") == "code.python":
            raise BackendError("capability_unavailable", "Accelerator placement requires a device-aware component contract; plain code.python has no device context")
        if node.get("kind") == "component.run":
            component = node.get("component", {})
            from .components.mlx import selected_device_for_component
            if (component.get('id', '').startswith('modelbuilder.mlx.')
                    and 'mlx' in component.get('capabilities', {}).get('frameworks', [])
                    and not device_kind.startswith('mlx-')):
                raise BackendError('capability_unavailable',
                    'MLX components require explicit environment.device mlx-cpu or mlx-metal and the reviewed worker profile',
                    {'node_id': node['id'], 'device': device_kind})
            selected_device = selected_device_for_component(component, placement['device'])
            selected_device = selected_device.split(':')[0]
            if selected_device not in component.get("capabilities", {}).get("devices", ["cpu"]):
                raise BackendError("capability_unavailable", "Component does not declare support for the selected execution device", {"node_id": node["id"], "device": selected_device})
    try:
        validate_live_evaluation(recipe, input_bindings=execution["input_bindings"])
    except ValueError as error:
        raise BackendError("capability_unavailable", str(error), {"field": "evaluation.live"}) from error
    if any(node.get("kind") in {"code.python", "component.run"} for node in recipe.get("nodes", [])) and not any(
            backend.repository.is_relative_to(Path(root).resolve()) for root in backend.trusted_code_roots):
        raise BackendError("untrusted_execution_blocked", "The source repository root must lie within an operator-trusted root")
    source = recipe.get("source", {})
    commit = source.get("commit", "")
    if not commit or set(commit) == {"0"}:
        raise BackendError("dependency_mismatch", "Execution requires a real immutable source commit")
    declared = source.get("files", [])
    if not declared:
        raise BackendError("dependency_mismatch", "Execution requires SHA256-pinned source files")
    for reference in declared:
        path = (backend.repository / reference["path"]).resolve()
        if (backend.repository / reference["path"]).is_symlink() or not path.is_relative_to(backend.repository) or not path.is_file():
            raise BackendError("dependency_mismatch", "Declared source file is unavailable")
        if hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
            raise BackendError("dependency_mismatch", "Declared source file has changed", {"path": reference["path"]})
    fixture_ref = next((r for r in declared if r["path"] == "src/modelbuilder/backend/fixtures.py"), None)
    if any(n.get("kind") in BUILTINS for n in recipe.get("nodes", [])):
        actual_fixture = Path(__file__).with_name("fixtures.py")
        if not fixture_ref or hashlib.sha256(actual_fixture.read_bytes()).hexdigest() != fixture_ref["sha256"]:
            raise BackendError("dependency_mismatch", "Installed builtin fixture differs from declared pinned workload source")
    if (backend.repository / ".git").exists():
        probe = subprocess.run(["git", "cat-file", "-e", commit + "^{commit}"], cwd=backend.repository, capture_output=True)
        if probe.returncode:
            raise BackendError("dependency_mismatch", "Declared source commit is unavailable locally")
        for reference in declared:
            blob = subprocess.run(["git", "show", f"{commit}:{reference['path']}"], cwd=backend.repository, capture_output=True)
            if blob.returncode or hashlib.sha256(blob.stdout).hexdigest() != reference["sha256"]:
                raise BackendError("dependency_mismatch", "Source bytes do not match the declared Git commit", {"path": reference["path"]})
    for node in recipe.get("nodes", []):
        if node.get("kind") == "dataset.version.import":
            validate_dataset_import(backend, payload, node)
    for dependency in recipe.get("resources", []) + recipe.get("external_dependencies", []):
        identifier = dependency["id"]
        path = backend.dependency_paths.get(identifier)
        if path and identifier in getattr(backend, "dataset_source_guards", {}):
            continue  # Reopened through captured no-symlink roots by dataset validation above.
        if path:
            try:
                actual = dependency_sha256(path)
            except ValueError as error:
                raise BackendError("dependency_mismatch", "Configured dependency is missing, unsafe, or changed", {"id": identifier}) from error
            if actual != dependency.get("sha256"):
                raise BackendError("dependency_mismatch", "Configured dependency bytes do not match manifest", {"id": identifier})
        elif dependency.get("revision") == "6f75de8b60a9f8a2fdf7b69cbd86d9e64bcb3837":
            pinned = {"config.json": "cf3179684f7c360dd6185962d480e7d426ceb8fe49f5db7b64bdc15dba0b8798",
                      "vocab.txt": "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3",
                      "pytorch_model.bin": "dab2c2bddcfb48ea430ef63fd76d46d67d704487844d967256a50dd7d7fd0a66"}
            name = dependency.get("url", "").rsplit("/", 1)[-1]
            expected_url = f"https://huggingface.co/prajjwal1/bert-tiny/resolve/{dependency['revision']}/{name}"
            if name not in pinned or dependency["url"] != expected_url or dependency.get("sha256") != pinned[name]:
                raise BackendError("dependency_mismatch", "Unrecognized pretrained dependency identity")
            path = backend.pretrained_path / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != dependency["sha256"]:
                raise BackendError("dependency_mismatch", "Pinned pretrained dependency is absent or changed")
        else:
            raise BackendError("dependency_unavailable", "External dependency must be explicitly resolved and verified before execution", {"id": identifier})
    nodes = recipe.get("nodes", [])
    if not nodes:
        raise BackendError("validation_error", "Execution requires a nonempty recipe DAG")
    for node in nodes:
        validate_node_settings(node)
        if node.get("kind") == "component.run":
            from .components import validate_component
            try:
                validate_component(node)
            except (ValueError, ValidationError, SchemaError) as error:
                raise BackendError("validation_error", str(error), {"node_id": node.get("id")}) from error
        kind, reference = node["kind"], node.get("implementation_ref")
        if node.get("resource_request", {}).get("gpu", 0):
            raise BackendError("capability_unavailable", "Local placement does not reserve GPU resources; omit GPU reservations")
        parameters = node.get("parameters", {})
        if kind == "model.train" and "steps" in parameters and (type(parameters["steps"]) is not int or not 1 <= parameters["steps"] <= 100000):
            raise BackendError("validation_error", "Fixture steps must be an integer between 1 and 100000")
        cadence = parameters.get("checkpoint_interval_steps", 1)
        if kind == "model.train" and (type(cadence) is not int or not 1 <= cadence <= 100000):
            raise BackendError("validation_error", "Checkpoint interval must be an integer from 1 through 100000", {"node_id": node["id"]})
        kl = parameters.get("kl")
        if kind == "model.train" and kl is not None:
            initial_edges = [edge for edge in recipe.get("edges", []) if edge["target_node"] == node["id"] and edge["target_port"] == "initial"]
            if (len(initial_edges) != 1 or kl.get("reference_model_ref") != initial_edges[0]["source_node"]
                    or kl.get("applies_to") != "logits"):
                raise BackendError("capability_unavailable", "Fixture KL supports logits anchored to the actual connected initial model only")
        if kind == "dataset.import" and parameters.get("resource_id") not in backend.dependency_paths:
            raise BackendError("dependency_unavailable", "Dataset import requires an explicitly resolved content-addressed JSON dependency")
        if kind == "dataset.version.import":
            validate_dataset_import(backend, selected_payload, node)
        if kind in {"code.python", "component.run"}:
            if isinstance(reference, dict) and reference.get("module") in {"__main__", "__mp_main__"}:
                raise BackendError("validation_error", "Source implementations cannot replace reserved process module identities", {"node_id": node.get("id")})
            if isinstance(reference, dict) and (reference.get("commit") != commit or
                    not any(item.get("path") == reference.get("path") and item.get("sha256") == reference.get("sha256") for item in declared)):
                raise BackendError("dependency_mismatch", "Python implementation must belong to the pinned source closure", {"node_id": node["id"]})
            if not isinstance(reference, dict) or not backend.trusted_code_roots:
                raise BackendError("untrusted_execution_blocked", "Arbitrary Python requires an operator-configured trusted source root; untrusted isolation is unavailable")
            path = (backend.repository / reference.get("path", "")).resolve()
            if not any(path.is_relative_to(Path(root)) for root in backend.trusted_code_roots):
                raise BackendError("untrusted_execution_blocked", "Python source lies outside explicitly trusted roots")
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != reference.get("sha256"):
                raise BackendError("dependency_mismatch", "Trusted Python source SHA256 does not match")
        elif kind not in BUILTINS | PLATFORM_BUILTINS or reference != f"builtin:{kind}":
            raise BackendError("capability_unavailable", f"Executable node implementation is unsupported: {kind}")
    verify_environment(backend, recipe)
    from .runner import resolve_environments
    try:
        resolve_environments(backend, recipe)
    except (ValueError, OSError, ValidationError) as error:
        raise BackendError("capability_unavailable", str(error), {"field": "environment_ref"}) from error
    return payload["recipe"]


def validate_dataset_import(backend, payload: dict, node: dict) -> None:
    """Validate the same portable dataset used by the worker, without execution."""
    from ..dataset import load_portable_split, MAX_FILE_BYTES, PYARROW_REQUIREMENT
    from ..models import ServiceError
    parameters = node.get("parameters", {})
    if (not isinstance(parameters, dict) or set(parameters) - {"reference_id", "split_id", "purpose"}
            or any(not isinstance(parameters.get(key), str) or not parameters[key] for key in ("reference_id", "split_id"))
            or not isinstance(parameters.get("purpose", "training"), str)
            or parameters.get("purpose", "training") not in {"training", "evaluation", "calibration"}):
        raise BackendError("validation_error", "Dataset version import requires reference_id, split_id and a supported purpose",
                           {"node_id": node["id"]})
    recipe = payload["recipe"]
    declared = {item["id"]: item for item in recipe.get("resources", []) + recipe.get("external_dependencies", [])}
    identifier = parameters["reference_id"]
    path = backend.dependency_paths.get(identifier)
    if identifier not in declared or not path:
        raise BackendError("dependency_unavailable", "Dataset version reference must be declared and operator-resolved",
                           {"node_id": node["id"]})
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES or hashlib.sha256(raw).hexdigest() != declared[identifier].get("sha256"):
            raise BackendError("dependency_mismatch", "Dataset version reference is oversized or differs from its pinned hash",
                               {"node_id": node["id"]})
        reference = json.loads(raw)
        manifest = reference.get("manifest") if isinstance(reference, dict) else None
        if (isinstance(manifest, dict) and isinstance(manifest.get("sources"), list)
                and any(isinstance(source, dict) and source.get("format") == "parquet"
                        for source in manifest["sources"])):
            from ..parquet_reader import require_platform
            require_platform()
            # Admission runs in the service interpreter; workers use a separate
            # environment. Verify the worker's optional adapter before planning.
            verify_environment(backend, {"environment": {"python": "3.12", "requirements": [PYARROW_REQUIREMENT]}})
        load_portable_split(reference, {key: value for key, value in backend.dependency_paths.items() if key in declared},
                            parameters["split_id"], purpose=parameters.get("purpose", "training"),
                            expected_project_id=payload["project_id"],
                            source_guards=(getattr(backend, "dataset_source_guards", {}).copy() or None)
                            if declared[identifier].get("metadata", {}).get("dataset_reference") else None)
    except ServiceError as exc:
        raise BackendError(exc.code, exc.message, {"node_id": node["id"], **exc.details}) from exc
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise BackendError("dependency_mismatch", "Dataset version reference is unreadable or invalid",
                           {"node_id": node["id"]}) from exc


def validate_node_settings(node: dict) -> None:
    """Fail closed on settings the local sequential executor cannot honor."""
    details = {"node_id": node.get("id")}
    if node.get("environment_ref", "default") != "default" and not (
            node.get("kind") == "component.run" and node.get("component", {}).get("runner")):
        raise BackendError("capability_unavailable", "Per-node environments require an explicit pinned runner component", details)
    policy = node.get("cache_policy", "disabled")
    if policy not in {"disabled", "enabled"}:
        raise BackendError("validation_error", "Unknown native cache policy", details)
    if policy == "enabled":
        from .native_cache import supports_cache
        if not supports_cache(node):
            raise BackendError("capability_unavailable", "Caching supports JSON-value code.python and dataset/utility components only; lifecycle stages and Path ports require cache_policy=disabled", details)
    resource = node.get("resource_request", {})
    if resource.get("gpu", 0) or "cpu" in resource or "memory_mb" in resource:
        raise BackendError("capability_unavailable", "Local execution cannot enforce CPU, memory, or GPU reservations; omit CPU/memory requests and use gpu=0", details)
    retry = node.get("retry", {})
    delay = retry.get("backoff_seconds", 0)
    if not isinstance(delay, (float, int)) or delay < 0 or int(delay) != delay:
        raise BackendError("capability_unavailable", "Retry backoff must be a nonnegative whole number of seconds", details)
    if retry.get("max_attempts", 1) == 1 and delay != 0:
        raise BackendError("capability_unavailable", "Retry backoff requires max_attempts greater than one", details)
    timeout = node.get("timeout", 300)
    if not isinstance(timeout, (float, int)) or isinstance(timeout, bool) or not 0 < timeout <= 86400:
        raise BackendError("validation_error", "Node timeout must be positive and at most 86400 seconds", details)


# Execute only platform-owned metadata inspection in the selected interpreter.
# -I -B prevents current directory/PYTHONPATH imports and bytecode writes.
ENVIRONMENT_PROBE = r'''
import importlib.metadata, json, platform, sys
from packaging.requirements import Requirement
body=json.load(sys.stdin)
def fail(code,message,**details):
    print(json.dumps({"error":{"code":code,"message":message,"details":details}})); sys.exit(0)
env=body["environment"]
declared=env.get("python", "").split(".")
if platform.python_version().split(".")[:len(declared)] != declared:
    fail("dependency_mismatch", "Backend Python version differs from declared environment", actual=platform.python_version())
resolved={}
for text in body["requirements"]:
    try: requirement=Requirement(text)
    except Exception: fail("dependency_mismatch", "Malformed environment requirement", requirement=text)
    if requirement.marker and not requirement.marker.evaluate(): continue
    pins=list(requirement.specifier)
    if requirement.url or len(pins)!=1 or pins[0].operator!="==" or "*" in pins[0].version:
        fail("dependency_mismatch", "Execution requirements must be exactly pinned", requirement=text)
    try: version=importlib.metadata.version(requirement.name)
    except importlib.metadata.PackageNotFoundError: fail("dependency_unavailable", "Required backend distribution is absent", package=requirement.name)
    if version not in requirement.specifier:
        fail("dependency_mismatch", "Backend distribution differs from declared recipe", package=requirement.name,actual=version)
    resolved[requirement.name]=version
print(json.dumps({"verified":True,"python":platform.python_version(),"platform":sys.platform,"machine":platform.machine(),"packages":resolved}))
'''


def verify_environment(backend, recipe: dict) -> dict:
    environment = recipe.get("environment", {})
    if environment.get("oci_digest"):
        raise BackendError("capability_unavailable", "OCI environments are unsupported by the local interpreter executor")
    requirements = list(environment.get("requirements", []))
    locks = {}
    for field in ("lock_file", "trainer_profile"):
        lock = environment.get(field)
        if not lock:
            continue
        if field == "trainer_profile" and not environment.get("lock_file"):
            raise BackendError("dependency_mismatch", "Trainer overlay requires the complete backend lock")
        if field == "trainer_profile":
            from .trainer_profile import TRAINER_PROFILE_SHA256
            if lock["sha256"] != TRAINER_PROFILE_SHA256:
                raise BackendError("dependency_mismatch", "Trainer overlay must match the reviewed pinned profile")
        locks[field] = dict(lock)
        path = (backend.repository / lock["path"]).resolve()
        if not path.is_relative_to(backend.repository) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != lock["sha256"]:
            raise BackendError("dependency_mismatch", "Environment lock is missing or changed", {"path": lock["path"]})
        # Support the exact requirements and SHA256 continuation lines emitted
        # by our locks. Includes, constraints, editable sources, and other pip
        # directives change dependency resolution and must never be skipped.
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            entry = line.removesuffix("\\").strip()
            if entry.startswith("-"):
                if not re.fullmatch(r"--hash=sha256:[0-9a-fA-F]{64}", entry):
                    raise BackendError("dependency_mismatch", "Environment lock contains an unsupported dependency directive", {"path": lock["path"]})
                continue
            requirements.append(entry)
    safe_env = {key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL"}}
    try:
        result = subprocess.run([backend.python, "-I", "-B", "-c", ENVIRONMENT_PROBE],
                                input=json.dumps({"environment": environment, "requirements": requirements}),
                                text=True, capture_output=True, env=safe_env, timeout=30)
        response = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise BackendError("capability_unavailable", "Backend environment metadata inspection failed") from error
    if result.returncode:
        raise BackendError("capability_unavailable", "Backend environment metadata inspection failed")
    if response.get("error"):
        error = response["error"]
        raise BackendError(error["code"], error["message"], error.get("details"))
    return {**response, "lock_profiles": locks}


def validate_recipe_settings(recipe: dict) -> int:
    """Validate canonical settings on both plan and submission, without writes."""
    from ..execution import max_parallel_stages
    try:
        limit = max_parallel_stages(recipe)
    except ValueError as error:
        raise BackendError("validation_error", str(error), {"field": "execution.max_parallel_stages"}) from error
    if limit > 1 and (sys.platform != "linux" or recipe.get("environment", {}).get("device", "cpu") != "cpu"):
        raise BackendError("capability_unavailable",
            "Parallel stage execution currently requires Linux CPU; other platforms and accelerators await native lifecycle and cleanup verification. Use max_parallel_stages=1 for the existing supported sequential route.",
            {"field": "execution.max_parallel_stages", "supported_platforms": ["linux"], "supported_devices": ["cpu"]})
    return limit


def capabilities(backend) -> dict:
    from ..execution import MAX_PARALLEL_STAGES
    parallel_available = sys.platform == "linux"
    from .runner_compatibility import compatibility_catalog
    return {"schema_version": "1", "executable_kinds": sorted(BUILTINS | PLATFORM_BUILTINS | {"code.python", "component.run"}),
            "unsupported_schema_kinds": {"training.kl_anchor": "Use model.train.parameters.kl anchored to the connected initial model"},
            "node_settings": {"environment_ref": ["default", *sorted(getattr(backend, "runner_environments", {}))], "cache_policy": ["disabled", "enabled"],
                              "cache_scope": "explicitly opted-in JSON-value code.python and dataset/utility components; exact recipe revision, project, native input IDs and actual runtime identity",
                              "resource_request": {"gpu": [0], "cpu": "unsupported", "memory_mb": "unsupported"},
                              "timeout": {"minimum_exclusive": 0, "maximum": 86400, "behavior": "node deadline aborts the entire local run through process supervision"},
                              "retry": {"max_attempts": [1, 100], "backoff_seconds": "nonnegative integer"}},
            "arbitrary_python": {"configured": bool(backend.trusted_code_roots), "isolation": False,
                                 "requires": ["per-run trust consent", "operator trusted source root", "pinned implementation in source closure"]},
            "recipe_settings": {"execution": {"max_parallel_stages": {"minimum": 1,
                "maximum": MAX_PARALLEL_STAGES, "default": 1,
                "supported_maximum": MAX_PARALLEL_STAGES if parallel_available else 1,
                "platforms": ["linux"], "devices": ["cpu"],
                "scope": "independent native stages within one selected recipe; sequential bounded-iteration barriers",
                "process_boundary": "fresh trusted process bodies; not a hostile-code sandbox",
                "other_platforms": "pending native lifecycle and cleanup verification; existing sequential routes unchanged",
                "live_evaluation": True, "iteration_live_evaluation": False,
                "resource_reservations": False}}},
            "control_flow": {"conditions": "nested bool branches, required-input skip propagation, optional-input joins; sequential by default, bounded native concurrency on Linux CPU when explicitly configured",
                             "loops": "explicit bounded_iterations; typed prior-artifact feedback; post-body bool early exit; maximum 10000 invocations",
                             "iteration_resume": "explicit warm_start only; restarts at iteration 1; exact loop cursor resume unavailable",
                             "iteration_live_evaluation": False, "independent_stage_concurrency": parallel_available, "conditional_live_evaluation": True,
                             "conditional_live_gate_policy": "evaluator gate must already be a canonical ancestor of its trainer; false/skipped gates launch no sidecar"},
            "runner_compatibility": compatibility_catalog(),
            "environment": {"requirements": "exact installed versions with target markers", "oci": False,
                "runner_protocol": "modelbuilder.runner/v1", "runner_devices": ["cpu"],
                "runner_platforms": ["linux"], "runner_launchers": ["python"],
                "runner_environment_verification": "complete exact distribution metadata; not framework smoke acceptance",
                "runner_installation": False, "runner_live_evaluation": False, "runner_exact_resume": False},
            "placement": {"recipe_field": "environment.device", "reservations": False, "live_evaluation_device": "cpu",
                          "non_training_builtins_device": "cpu", "component_enforcement": "trusted component must honor declared device context",
                          "accelerator_exact_resume": False, "cuda_profile": "cuda-cu128",
                          "cuda_platforms": ["linux-x64", "linux-arm64"], "cuda_hardware_acceptance": "not_verified"},
            "dependency_resolution": "operator configured SHA256 verified local files; no automatic download"}


def environment_profiles(backend) -> list[dict]:
    """Discover immutable profile references; applying one requires a revision.

    No runtime platform substitution mutates the submitted recipe. In
    particular the Linux +cpu wheel pin is never loosened for macOS.
    """
    profiles = []
    for target, torch_pin in (("linux-x64", "2.9.0+cpu"), ("linux-arm64", "2.9.0+cpu"), ("darwin-arm64", "2.9.0")):
        relative = f"requirements/profiles/backend-{target}.lock"
        path = backend.repository / relative
        if path.is_file():
            profiles.append({"target_platform": target, "requires_recipe_revision": True,
                             "devices": ["cpu", "mps"] if target == "darwin-arm64" else ["cpu"],
                             "environment": {"python": "3.12.14", "requirements": ["numpy==2.2.6", f"torch=={torch_pin}", "transformers==4.57.1", "zenml[server]==0.97.0"],
                                             "lock_file": {"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}})
    from .trainer_profile import TRAINER_PROFILE_PATH, TRAINER_PROFILE_SHA256, TRAINER_REQUIREMENT
    trainer_path = backend.repository / TRAINER_PROFILE_PATH
    if trainer_path.is_file() and hashlib.sha256(trainer_path.read_bytes()).hexdigest() == TRAINER_PROFILE_SHA256:
        trainer = {"path": TRAINER_PROFILE_PATH, "sha256": TRAINER_PROFILE_SHA256}
        for base in list(profiles):
            profiles.append({**base, "variant": "trainer", "framework": "transformers",
                             "overlay_status": "requires_environment_verification", "native_execution": "submission_required",
                             "environment": {**base["environment"], "requirements": [*base["environment"]["requirements"], TRAINER_REQUIREMENT],
                                             "trainer_profile": trainer}})
    from .placement import CUDA_PROFILES
    for target, lock in CUDA_PROFILES.items():
        path = backend.repository / lock["path"]
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == lock["sha256"]:
            profiles.append({"target_platform": target, "variant": "cuda-cu128", "requires_recipe_revision": True,
                             "devices": ["cpu", "cuda"], "hardware_acceptance": "not_verified",
                             "environment": {"python": "3.12.14", "requirements": ["numpy==2.2.6", "torch==2.9.0+cu128", "transformers==4.57.1", "zenml[server]==0.97.0"],
                                             "lock_file": dict(lock)}})
    if getattr(backend, 'mlx_python', None):
        from .components.mlx import PROFILE_SHA256
        relative = 'requirements/profiles/mlx-darwin-arm64.lock'
        path = backend.repository / relative
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == PROFILE_SHA256:
            mac_profile = next((profile for profile in profiles if profile['target_platform']=='darwin-arm64'), None)
            if mac_profile:
                profiles.append({'target_platform':'darwin-arm64','requires_recipe_revision':True,'framework':'mlx',
                    'devices':['mlx-cpu','mlx-metal'], 'environment':{**mac_profile['environment'],
                        'mlx_profile':{'path':relative,'sha256':PROFILE_SHA256}},
                    'worker_environment':{'python':'3.12.14','lock_file':{'path':relative,'sha256':PROFILE_SHA256},
                        'isolation':'separate operator-configured interpreter','exact_state':False}})
    return profiles
