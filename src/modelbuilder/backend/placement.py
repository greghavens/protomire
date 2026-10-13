"""Single-device placement, never resource reservation or an isolation boundary.

Discovery/plan do not allocate tensors. Submission verifies a tiny operation in
an isolated configured interpreter. Framework execution rechecks in its worker.
CUDA requires an explicit reviewed CUDA profile and the matching visible runtime.
"""
from __future__ import annotations
import copy
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
from pathlib import Path

MPS_LOCK_SHA256 = "c6a0af3c9c73c6c0293139e581859de8df067d2738e8cd95c7397d844b93bdf7"
CUDA_PROFILE_REASON = "CUDA requires the reviewed Linux x64/ARM64 CUDA 12.8 backend profile, Python 3.12.14, Torch 2.9.0+cu128 and a visible compatible NVIDIA device. No installation or driver change is performed."
CUDA_PROFILES = {
    "linux-x64": {"path": "requirements/profiles/backend-cuda-cu128-linux-x64.lock", "sha256": "4ad97d32bad25f2cd1a68f39f243776247190a8b763d28c4edf97c3d0b516933"},
    "linux-arm64": {"path": "requirements/profiles/backend-cuda-cu128-linux-arm64.lock", "sha256": "ab2c32c254e3f5811f40bf4b0eb3ca71f635b37656d8dc957b0b9d04a5eaa21f"},
}
DEVICE_PATTERN = r"^(cpu|mps|mlx-cpu|mlx-metal|cuda:(0|[1-9][0-9]{0,2}))$"


def device_from_recipe(recipe: dict) -> str:
    device = recipe.get("environment", {}).get("device", "cpu")
    if not isinstance(device, str) or re.fullmatch(DEVICE_PATTERN, device) is None:
        raise ValueError("Select cpu, mps, or an explicit cuda:<visible ordinal>; automatic device selection is unsupported")
    return device


def target_for_device(device: str) -> str:
    if re.fullmatch(DEVICE_PATTERN, device) is None:
        raise ValueError("Invalid explicit device")
    return "local-" + device


def is_local_target(target) -> bool:
    return isinstance(target, str) and (target in {"local-cpu", "local-mps", "local-mlx-cpu", "local-mlx-metal"} or re.fullmatch(r"local-cuda:(0|[1-9][0-9]{0,2})", target) is not None)


def placement_environment(device: str, base: dict) -> dict:
    env = dict(base)
    # Retain operator CUDA visibility; recipe ordinals index this visible set.
    # Visibility is advisory for trusted code, not a security boundary.
    if device == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["HIP_VISIBLE_DEVICES"] = ""
    elif device.startswith("cuda:"):
        # Built-in training enables deterministic algorithms. CUDA matmul needs
        # this documented cuBLAS workspace policy before context initialization.
        env["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
    env["MODELBUILDER_EXECUTION_DEVICE"] = device
    env["PYTORCH_ENABLE_MPS_FALLBACK"] = "0"
    return env


PROBE = r'''
import json, platform, sys
body = json.load(sys.stdin)
result = {}
try:
 device=body['device']
 if device=='mps' and sys.platform!='darwin': raise RuntimeError('MPS requires macOS in the configured backend interpreter')
 import torch
 result={'device':device,'python':platform.python_version(),'platform':sys.platform,
         'machine':platform.machine(),'torch':str(torch.__version__),'cuda_build':torch.version.cuda,
         'hip_build':torch.version.hip,
         'mps_built':bool(torch.backends.mps.is_built()),'mps_available':bool(torch.backends.mps.is_available()),
         'allocation_verified':False}
 if device=='mps' and not result['mps_available']: raise RuntimeError('MPS is not available in the configured backend interpreter')
 if device.startswith('cuda:'):
  index=int(device.split(':')[1])
  result['cuda_visible_devices']=int(torch.cuda.device_count())
  if torch.version.hip or not torch.version.cuda: raise RuntimeError('Selected CUDA device requires an NVIDIA CUDA Torch build')
  if not torch.cuda.is_available() or index >= result['cuda_visible_devices']: raise RuntimeError('Selected CUDA device is not visible in the configured backend interpreter')
 if body['allocate']:
  x=torch.ones(2,device=device,requires_grad=True)
  y=(x*x).sum(); y.backward()
  if device=='mps': torch.mps.synchronize()
  elif device.startswith('cuda:'): torch.cuda.synchronize(device)
  if y.detach().cpu().item()!=2 or x.grad.detach().cpu().tolist()!=[2,2]: raise RuntimeError('Selected-device arithmetic verification failed')
  result['allocation_verified']=True
 result['available']=True
 print(json.dumps(result))
except Exception as error:
 print(json.dumps({**result,'available':False,'device':body['device'],'reason':str(error)[:1000]}))
'''


_PREPARED_PROBES = ContextVar("modelbuilder_placement_probes", default=None)


def _path_identity(path):
    if not path:
        return None
    value = Path(path)
    try:
        stat = value.stat()
        return {"path": str(value), "resolved": str(value.resolve()),
                "stat": [stat.st_dev, stat.st_ino, stat.st_mode, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]}
    except OSError:
        return {"path": str(value), "unavailable": True}


def _stat_identity(value):
    return [value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def _config_identity(path):
    if path is None:
        return None
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None  # A non-venv interpreter has no configuration file.
    except OSError:
        return {"path": str(path), "invalid": "Venv configuration identity is unavailable"}
    invalid = {"path": str(path), "invalid": "Venv configuration requires bounded no-follow regular-file access"}
    if not stat.S_ISREG(before.st_mode) or not all(hasattr(os, flag) for flag in ("O_NONBLOCK", "O_NOFOLLOW")):
        return invalid
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            opened = os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_size > 16384
                    or _stat_identity(before) != _stat_identity(opened)):
                return invalid
            raw = os.read(descriptor, 16385)
            if (len(raw) != opened.st_size or _stat_identity(os.fstat(descriptor)) != _stat_identity(opened)
                    or _stat_identity(path.lstat()) != _stat_identity(opened)):
                return invalid
            return {"path": str(path), "stat": _stat_identity(opened), "sha256": hashlib.sha256(raw).hexdigest()}
        finally:
            os.close(descriptor)
    except OSError:
        return invalid


def backend_identity(backend):
    """Cheap local configuration/identity fence; never a driver/runtime probe."""
    python = getattr(backend, "python", None)
    config = Path(python).parent.parent / "pyvenv.cfg" if python else None
    mlx_python = getattr(backend, 'mlx_python', None)
    mlx_config = Path(mlx_python).parent.parent / 'pyvenv.cfg' if mlx_python else None
    return {"python": _path_identity(python), "venv_config": _config_identity(config),
            "mlx_python": _path_identity(getattr(backend, 'mlx_python', None)),
            "mlx_venv_config": _config_identity(mlx_config),
            "repository": _path_identity(getattr(backend, "repository", None)),
            "root": str(getattr(backend, "root", "")),
            "trusted_code_roots": list(getattr(backend, "trusted_code_roots", [])),
            "dependency_paths": copy.deepcopy(getattr(backend, "dependency_paths", {})),
            "probe_environment": {key: os.environ.get(key) for key in
                ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "CUDA_VISIBLE_DEVICES")}}


def _require_safe_identity(backend):
    identity = backend_identity(backend)
    config = identity.get("venv_config") or {}
    if config.get("invalid"):
        from .admission import BackendError
        raise BackendError("capability_unavailable", config["invalid"])
    if (identity.get('mlx_venv_config') or {}).get('invalid'):
        from .admission import BackendError
        raise BackendError('capability_unavailable', identity['mlx_venv_config']['invalid'])
    return identity


@contextmanager
def probe_session(entries, *, compute):
    """One request's preparation only; never persisted capability/authorization."""
    token = _PREPARED_PROBES.set((entries, compute))
    try:
        yield
    finally:
        _PREPARED_PROBES.reset(token)


def probe(backend, device: str, *, allocate: bool = False) -> dict:
    identity = _require_safe_identity(backend)
    session = _PREPARED_PROBES.get()
    if session is None:
        return _probe(backend, device, allocate=allocate)
    entries, compute = session
    key = json.dumps([identity, device, allocate], sort_keys=True)
    if key in entries:
        return copy.deepcopy(entries[key])
    if not compute:
        from .admission import BackendError
        raise BackendError("placement_preparation_changed", "Device probe was not prepared for the current interpreter/settings; retry explicitly")
    result = _probe(backend, device, allocate=allocate)
    entries[key] = copy.deepcopy(result)
    return result


def _probe(backend, device: str, *, allocate: bool = False) -> dict:
    if device.startswith('mlx-'):
        from .components.mlx import probe
        return probe(getattr(backend, 'mlx_python', None), device, allocate=allocate)
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "CUDA_VISIBLE_DEVICES"}}
    env = placement_environment(device, env)
    env.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    try:
        result = subprocess.run([backend.python, "-I", "-B", "-c", PROBE],
                                input=json.dumps({"device": device, "allocate": allocate}),
                                capture_output=True, text=True, timeout=20, env=env)
        if result.returncode or len(result.stdout) > 16384:
            raise ValueError("invalid probe result")
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or type(value.get("available")) is not bool or value.get("device") != device:
            raise ValueError("invalid probe result")
        if value["available"] and allocate and value.get("allocation_verified") is not True:
            raise ValueError("allocation was not verified")
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"available": False, "device": device, "reason": "Configured backend device probe failed or exceeded its 20-second limit"}


def validate_placement(backend, payload: dict, *, allocate: bool = False) -> dict:
    from .admission import BackendError
    _require_safe_identity(backend)
    try:
        device = device_from_recipe(payload.get("recipe", {}))
    except ValueError as error:
        raise BackendError("validation_error", str(error)) from error
    target = payload.get("target", "local-cpu")
    if target != target_for_device(device):
        raise BackendError("dependency_mismatch", "Execution target must match the recipe's versioned environment.device", {"device": device, "target": target})
    if device != "cpu" and payload.get("resume_mode", payload.get("mode")) == "exact_state":
        raise BackendError("capability_unavailable", "Accelerator exact-state continuation is not hardware-verified; use explicit warm_start")
    if device.startswith('mlx-'):
        from .components.mlx import PROFILE_SHA256
        environment = payload['recipe'].get('environment', {})
        profile = environment.get('mlx_profile', {})
        if profile.get('sha256') != PROFILE_SHA256:
            raise BackendError('dependency_mismatch', 'MLX requires the reviewed separate hash-locked MLX worker profile')
        path = (backend.repository / profile.get('path', '')).resolve()
        if not path.is_relative_to(backend.repository) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != PROFILE_SHA256:
            raise BackendError('dependency_mismatch', 'Declared MLX worker profile is missing or changed')
        result = probe(backend, device, allocate=allocate)
        if not result.get('available'):
            raise BackendError('capability_unavailable', result.get('reason', 'Configured MLX worker unavailable'))
    elif device.startswith("cuda:"):
        environment = payload["recipe"].get("environment", {})
        digest = environment.get("lock_file", {}).get("sha256")
        if digest not in {profile["sha256"] for profile in CUDA_PROFILES.values()}:
            raise BackendError("dependency_mismatch", "CUDA requires a reviewed hash-locked backend-cuda-cu128 profile as a recipe revision",
                               {"device": device, "profile_status": "required"})
        result = probe(backend, device, allocate=allocate)
        profile = cuda_profile(result)
        if profile is None or not result.get("available"):
            raise BackendError("capability_unavailable", result.get("reason", CUDA_PROFILE_REASON))
        if profile["sha256"] != digest:
            raise BackendError("dependency_mismatch", "CUDA profile architecture differs from the configured backend interpreter")
        count = result.get("cuda_visible_devices")
        if type(count) is not int or not 0 < count <= 1000 or int(device.split(":")[1]) >= count:
            raise BackendError("capability_unavailable", "Selected CUDA visible ordinal is unavailable")
    elif device == "mps":
        environment = payload["recipe"].get("environment", {})
        if environment.get("lock_file", {}).get("sha256") != MPS_LOCK_SHA256:
            raise BackendError("dependency_mismatch", "MPS requires the reviewed hash-locked backend-darwin-arm64 profile as a recipe revision")
        result = probe(backend, device, allocate=allocate)
        if result.get("platform") != "darwin" or result.get("machine") != "arm64" or result.get("torch") != "2.9.0":
            raise BackendError("capability_unavailable", result.get("reason", "MPS requires the pinned macOS ARM64 Torch 2.9.0 interpreter"))
        if not result["available"]:
            raise BackendError("capability_unavailable", result.get("reason", "MPS is unavailable"))
    else:
        from ..execution import selection
        from ..models import ServiceError
        try:
            selected = set(selection(payload.get("recipe", {}), payload.get("execution"))["node_ids"]) if allocate else set()
        except ServiceError as error:
            raise BackendError(error.code, error.message, error.details) from error
        needs_probe = any(node.get("id") in selected and node.get("kind") == "model.train"
                          for node in payload.get("recipe", {}).get("nodes", []))
        result = probe(backend, device, allocate=True) if needs_probe else {"device": "cpu", "available": True, "allocation_verified": False}
        if not result["available"]:
            raise BackendError("capability_unavailable", result.get("reason", "CPU probe failed"))
    return {**result, "target": target, "requested_device": device,
            "allocation_verification_scope": "tiny_tensor_probe_only", "component_execution_observed": False,
            "reservation": False, "isolation": False,
            "live_evaluation_device": "cpu", "exact_resume": device == "cpu",
            "hardware_acceptance": "cpu_regression_tested" if device == "cpu" else "not_verified"}


def cuda_profile(runtime: dict) -> dict | None:
    """Map inspected runtime metadata to the exact reviewed binary profile."""
    if (runtime.get("platform") != "linux" or runtime.get("python") != "3.12.14"
            or runtime.get("torch") != "2.9.0+cu128" or runtime.get("cuda_build") != "12.8"
            or runtime.get("hip_build") is not None):
        return None
    target = {"x86_64": "linux-x64", "aarch64": "linux-arm64"}.get(runtime.get("machine"))
    return CUDA_PROFILES.get(target)


def targets(backend) -> list[dict]:
    reason = backend._capability_reason()
    base = {"backend": "zenml", "version": "0.97.0", "cancel": "supervised_process_termination",
            "untrusted_isolation": False, "reservation": False, "live_evaluation_device": "cpu"}
    mps = probe(backend, "mps") if reason is None else {"available": False, "reason": reason}
    mps_available = bool(mps.get("available") and mps.get("platform") == "darwin" and mps.get("machine") == "arm64" and mps.get("torch") == "2.9.0")
    result = [{**base, "id": "local-cpu", "device": "cpu", "available": reason is None, "unavailable_reason": reason, "exact_resume": True},
              {**base, "id": "local-mps", "device": "mps", "available": mps_available,
               "unavailable_reason": None if mps_available else mps.get("reason", "Requires pinned macOS ARM64 Torch 2.9.0"),
               "required_profile_sha256": MPS_LOCK_SHA256, "exact_resume": False, "hardware_acceptance": "not_verified"}]
    cuda = probe(backend, "cuda:0") if reason is None else {"available": False, "reason": reason}
    profile = cuda_profile(cuda)
    count = cuda.get("cuda_visible_devices")
    if profile and cuda.get("available") and type(count) is int and 0 < count <= 1000:
        # Enumerate interpreter-visible ordinals, never physical nvidia-smi IDs.
        # Availability is metadata only; admission verifies the complete lock and
        # submission probes the selected ordinal's actual forward/backward work.
        for index in range(count):
            result.append({**base, "id": f"local-cuda:{index}", "device": f"cuda:{index}", "available": True,
                           "unavailable_reason": None, "required_profile_sha256": profile["sha256"],
                           "required_profile_path": profile["path"], "exact_resume": False,
                           "allocation_verified": False, "environment_verification": "submission_required",
                           "hardware_acceptance": "not_verified"})
    else:
        result.append({**base, "id": "local-cuda", "device": None, "available": False,
                       "unavailable_reason": cuda.get("reason", CUDA_PROFILE_REASON),
                       "requires_explicit_device": "cuda:<visible ordinal>", "exact_resume": False,
                       "hardware_acceptance": "not_verified"})
    if getattr(backend, 'mlx_python', None):
        from .components.mlx import PROFILE_SHA256
        for device in ('mlx-cpu', 'mlx-metal'):
            metadata = probe(backend, device)
            result.append({**base, **metadata, 'id': target_for_device(device), 'exact_resume': False,
                           'required_mlx_profile_sha256': PROFILE_SHA256, 'hardware_acceptance': 'submission_required'})
    return result


def synchronize(device: str) -> None:
    if device == "cpu":
        return
    import torch
    if device == "mps":
        torch.mps.synchronize()
    elif device.startswith("cuda:"):
        torch.cuda.synchronize(device)


def checkpoint_cpu(value):
    """Detach/copy complete framework state for portable artifact publication."""
    import torch
    if isinstance(value, torch.Tensor):
        return value.detach().cpu().clone()
    if isinstance(value, dict):
        return {key: checkpoint_cpu(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(checkpoint_cpu(item) for item in value)
    if isinstance(value, list):
        return [checkpoint_cpu(item) for item in value]
    return value


def runtime_device_identity(device: str) -> dict:
    if device.startswith('mlx-'):
        raise ValueError('MLX device identity must be inspected in its isolated configured worker, not Torch')
    import torch
    if device.startswith("cuda:") and cuda_profile({"python": platform.python_version(), "platform": sys.platform,
            "machine": platform.machine(), "torch": str(torch.__version__), "cuda_build": torch.version.cuda,
            "hip_build": torch.version.hip}) is None:
        raise ValueError("Selected CUDA device requires the pinned Linux CUDA 12.8 runtime; CPU fallback is disabled")
    if device == "mps" and not torch.backends.mps.is_available():
        raise ValueError("Selected MPS device is unavailable; CPU fallback is disabled")
    if device.startswith("cuda:") and (torch.version.hip or not torch.version.cuda or not torch.cuda.is_available()
                                      or int(device.split(":")[1]) >= torch.cuda.device_count()):
        raise ValueError("Selected CUDA device is unavailable; CPU fallback is disabled")
    return {"device": device, "requested_device": device, "component_execution_observed": False,
            "torch": str(torch.__version__), "cuda_build": torch.version.cuda,
            "device_name": torch.cuda.get_device_name(device) if device.startswith("cuda:") else device,
            "deterministic_algorithms": bool(torch.are_deterministic_algorithms_enabled())}


def effective_resume_modes(device: str, declared: list[str]) -> list[str]:
    """A component's CPU declaration is not hardware evidence for accelerators."""
    return [mode for mode in declared if device == "cpu" or mode != "exact_state"]
