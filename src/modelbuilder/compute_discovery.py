"""Read-only service-host observations, never scheduler capacity or execution admission.

psutil supplies a real (blocking 100 ms) CPU sample. Native GPU queries have fixed
argument vectors, bounded output and timeouts. Torch is only imported in the
configured, isolated backend interpreter, never in the operation service. No
packages are installed, tensors allocated, kernels launched, or settings changed.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import math
import os
import platform
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

MAX_OUTPUT_BYTES = 128 * 1024
MAX_DEVICES = 64
CACHE_SECONDS = 10
PROBE_SECONDS = 2
TORCH_SECONDS = 3


def _number(value, *, maximum=None, integer=False):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(result) or result < 0 or result > (maximum if maximum is not None else 2**63-1):
        return None
    if integer:
        return int(result) if result.is_integer() else None
    return result


def _label(value, default="Unknown GPU"):
    if not isinstance(value, str):
        return default
    return "".join(char for char in value[:200] if char.isprintable()).strip() or default


def _probe_env():
    # No application credentials, tokens, arbitrary PYTHONPATH or shell startup.
    allowed = {"PATH", "SystemRoot", "WINDIR", "SYSTEMROOT", "CUDA_VISIBLE_DEVICES",
               "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update(LANG="C", LC_ALL="C", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
               PYTORCH_NVML_BASED_CUDA_CHECK="1", OMP_NUM_THREADS="1")
    return env


def _run_probe(arguments, timeout=PROBE_SECONDS):
    """Bound both retained bytes and elapsed time; no shell and no stderr disclosure."""
    try:
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=_probe_env(), cwd=os.path.abspath(os.sep),
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, ValueError):
        return None, "unavailable"
    chunks = []
    oversized = threading.Event()

    def read():
        total = 0
        try:
            while chunk := process.stdout.read(4096):
                total += len(chunk)
                if total > MAX_OUTPUT_BYTES:
                    oversized.set()
                    process.kill()
                    break
                chunks.append(chunk)
        except (OSError, ValueError):
            pass
        finally:
            process.stdout.close()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    status = "ok"
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        status = "timeout"
        process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass  # Kernel/driver teardown must not extend the discovery deadline.
    reader.join(timeout=1)
    if reader.is_alive():
        # A descendant unexpectedly holding stdout must not keep the service waiting.
        status = "invalid_output"
    else:
        process.stdout.close()
    if oversized.is_set():
        status = "output_limit"
    if status == "ok" and process.returncode:
        status = "error"
    return (b"".join(chunks).decode("utf-8-sig", errors="replace") if status == "ok" else None), status


def _read_small(path):
    with Path(path).open("r", encoding="utf-8", errors="replace") as stream:
        return stream.read(4096).strip()


def _optional_read(path):
    try:
        return _read_small(path)
    except (OSError, ValueError):
        return None


def _gpu(source, index, name, vendor, *, total=None, used=None, free=None, utilization=None, memory_kind="unknown"):
    total, used, free = [_number(value, integer=True) for value in (total, used, free)]
    if total is not None and ((used is not None and used > total) or (free is not None and free > total)):
        used = free = None
    return {"id": f"{source}:{index}", "name": _label(name), "vendor": vendor, "source": source,
            "memory_kind": memory_kind, "vram_total_bytes": total, "vram_used_bytes": used,
            "vram_free_bytes": free, "utilization_percent": _number(utilization, maximum=100),
            "execution_target_id": None, "reservable": False}


def _parse_nvidia(text):
    devices = []
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        if len(row) != 6 or len(devices) >= MAX_DEVICES or not row[0].strip().isdigit():
            raise ValueError("Invalid NVIDIA query output")
        index, name, total, used, free, utilization = [value.strip() for value in row]
        memory = [_number(value) for value in (total, used, free)]
        memory = [int(value * 1024 * 1024) if value is not None else None for value in memory]
        devices.append(_gpu("nvidia-smi", index, name, "NVIDIA", total=memory[0], used=memory[1],
                            free=memory[2], utilization=utilization, memory_kind="dedicated"))
    return devices


def _linux_gpus(root=Path("/sys/class/drm")):
    devices = []
    # Enumerate only bounded, conventional device names; never follow arbitrary metadata paths.
    with os.scandir(root) as entries:
        for entry in entries:
            if not re.fullmatch(r"card\d+", entry.name):
                continue
            if len(devices) >= MAX_DEVICES:
                break
            device = Path(entry.path) / "device"
            vendor_id = _optional_read(device / "vendor")
            if vendor_id is None:
                continue
            vendor = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel", "0x106b": "Apple"}.get(vendor_id, "Unknown")
            device_id = _optional_read(device / "device")
            name = _optional_read(device / "product_name") or f"{vendor} GPU (PCI device {_label(device_id, 'unknown')})"
            total = _number(_optional_read(device / "mem_info_vram_total"), integer=True)
            used = _number(_optional_read(device / "mem_info_vram_used"), integer=True)
            devices.append(_gpu("linux-drm", entry.name, name, vendor, total=total, used=used,
                                free=total-used if total is not None and used is not None and used <= total else None,
                                utilization=_optional_read(device / "gpu_busy_percent"),
                                memory_kind="dedicated" if total else "unknown"))
    return devices


def _parse_macos(text):
    body = json.loads(text)
    rows = body.get("SPDisplaysDataType")
    if not isinstance(rows, list) or len(rows) > MAX_DEVICES:
        raise ValueError("Invalid display inventory")
    devices = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("Invalid display record")
        name = row.get("sppci_model", row.get("_name"))
        unified = isinstance(name, str) and name.startswith("Apple ")
        # Unified RAM is never presented as independently reservable VRAM.
        value = row.get("spdisplays_vram")
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(MB|GB)", value or "") if isinstance(value, str) else None
        size = _number(match[1]) if match and not unified else None
        total = int(size * (1024**2 if match[2] == "MB" else 1024**3)) if size is not None else None
        devices.append(_gpu("system-profiler", index, name, "Apple" if unified else _label(row.get("spdisplays_vendor"), "Unknown"),
                            total=total, memory_kind="unified" if unified else "dedicated" if total else "unknown"))
    return devices


def _parse_windows(text):
    rows = json.loads(text)
    rows = [rows] if isinstance(rows, dict) else rows
    if rows is None:
        rows = []
    if not isinstance(rows, list) or len(rows) > MAX_DEVICES or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Invalid display inventory")
    # Win32_VideoController.AdapterRAM is uint32 and cannot establish actual VRAM.
    return [_gpu("windows-cim", index, row.get("Name"), _label(row.get("AdapterCompatibility"), "Unknown"))
            for index, row in enumerate(rows)]


_TORCH_QUERY = """
import importlib.util, json
if importlib.util.find_spec('torch') is None:
    print(json.dumps({'status': 'unavailable', 'reason': 'torch_not_installed'}))
else:
    import torch
    # device_count uses NVML when possible. No tensor, device-properties, memory
    # allocation, CUDA context request, or user model import is made here.
    count = int(torch.cuda.device_count())
    mps = getattr(torch.backends, 'mps', None)
    print(json.dumps({'status': 'ok', 'cuda_visible_devices': count,
                     'cuda_api': 'rocm' if torch.version.hip else 'cuda' if torch.version.cuda else 'none',
                     'mps_available': bool(mps and mps.is_available())}))
"""


def _torch_probe(python):
    if not python or not Path(python).is_file():
        return {"source": "torch", "status": "unavailable", "reason": "backend_interpreter_unavailable"}
    raw, status = _run_probe([str(python), "-I", "-B", "-c", _TORCH_QUERY], timeout=TORCH_SECONDS)
    if status != "ok":
        return {"source": "torch", "status": status}
    try:
        value = json.loads(raw)
        if value.get("status") == "unavailable":
            return {"source": "torch", "status": "unavailable", "reason": "torch_not_installed"}
        count = _number(value.get("cuda_visible_devices"), maximum=MAX_DEVICES, integer=True)
        if value.get("status") != "ok" or count is None or type(value.get("mps_available")) is not bool or value.get("cuda_api") not in {"cuda", "rocm", "none"}:
            raise ValueError("Invalid torch result")
        return {"source": "torch", "status": "ok", "cuda_visible_devices": count,
                "cuda_api": value["cuda_api"], "mps_available": value["mps_available"],
                "scope": "configured_backend_runtime_visibility_only", "gpu_execution_enabled": False}
    except (ValueError, TypeError, AttributeError):
        return {"source": "torch", "status": "invalid_output"}


def _nvidia_executable(system):
    # Do not let a project cwd or a relative/user-controlled PATH entry select a
    # driver program. Windows executable lookup otherwise searches the cwd.
    if system == "Linux":
        candidates = [Path(path) for path in ("/usr/bin/nvidia-smi", "/usr/local/nvidia/bin/nvidia-smi", "/bin/nvidia-smi")]
    elif system == "Windows":
        root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
        programs = os.environ.get("ProgramFiles") or r"C:\Program Files"
        candidates = [Path(root) / "System32/nvidia-smi.exe", Path(programs) / "NVIDIA Corporation/NVSMI/nvidia-smi.exe"]
    else:
        return None
    for candidate in candidates:
        try:
            if candidate.is_absolute() and candidate.is_file():
                return str(candidate)
        except OSError:
            pass
    return None


def _accelerators(system, python):
    devices, probes = [], []
    nvidia = _nvidia_executable(system)
    if nvidia:
        raw, status = _run_probe([nvidia, "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu", "--format=csv,noheader,nounits"])
        if status == "ok":
            try:
                devices = _parse_nvidia(raw)
            except (ValueError, TypeError, csv.Error):
                status = "invalid_output"
        probes.append({"source": "nvidia-smi", "status": status})
    else:
        probes.append({"source": "nvidia-smi", "status": "unavailable"})
    native, source, status = [], None, "ok"
    try:
        if system == "Linux":
            source = "linux-drm"
            native = _linux_gpus()
        elif system == "Darwin":
            source = "system-profiler"
            executable = "/usr/sbin/system_profiler"
            raw, status = _run_probe([executable, "SPDisplaysDataType", "-json", "-detailLevel", "mini"])
            if status == "ok":
                native = _parse_macos(raw)
        elif system == "Windows":
            source = "windows-cim"
            root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
            executable = str(Path(root) / "System32/WindowsPowerShell/v1.0/powershell.exe")
            command = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-CimInstance -ClassName Win32_VideoController | Select-Object Name,AdapterCompatibility | ConvertTo-Json -Compress"
            raw, status = _run_probe([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command])
            if status == "ok":
                native = _parse_windows(raw)
    except OSError:
        status = "unavailable"
    except (ValueError, TypeError, AttributeError):
        status = "invalid_output"
    if source:
        probes.append({"source": source, "status": status})
    if status == "ok":
        # Native display adapters are a fallback for vendors not covered by SMI.
        # Do not add runtime device counts to hardware counts: views can overlap.
        vendors = {device["vendor"] for device in devices}
        devices.extend(device for device in native if device["vendor"] not in vendors and not
                       ("NVIDIA" in vendors and "nvidia" in device["vendor"].lower()))
    probes.append(_torch_probe(python))
    return {"devices": devices[:MAX_DEVICES], "observed_count": min(len(devices), MAX_DEVICES),
            "status": "detected" if devices else "not_detected" if any(probe["status"] == "ok" and probe["source"] != "torch" for probe in probes) else "unknown", "coverage": "partial",
            "note": "No detection is not proof of absence. Driver visibility, virtual devices and permissions may limit discovery. Runtime visibility is separate and cannot enable execution."}, probes


def _host_snapshot():
    cpu = {"logical_cores": None, "physical_cores": None, "process_affinity_cores": None,
           "utilization_percent": None, "sample_window_seconds": 0.1, "source": "psutil"}
    memory = {"total_bytes": None, "available_bytes": None, "used_bytes": None,
              "utilization_percent": None, "source": "psutil"}
    probes = []
    for field, probe in (("logical_cores", lambda: psutil.cpu_count(logical=True)),
                         ("physical_cores", lambda: psutil.cpu_count(logical=False)),
                         ("process_affinity_cores", lambda: len(psutil.Process().cpu_affinity())),
                         ("utilization_percent", lambda: psutil.cpu_percent(interval=0.1))):
        try:
            cpu[field] = _number(probe(), maximum=100 if field == "utilization_percent" else None,
                                 integer=field != "utilization_percent")
        except (OSError, ValueError, AttributeError, NotImplementedError, psutil.Error):
            pass
    try:
        values = psutil.virtual_memory()
        for field, attr in (("total_bytes", "total"), ("available_bytes", "available"), ("used_bytes", "used")):
            memory[field] = _number(getattr(values, attr), integer=True)
        memory["utilization_percent"] = _number(values.percent, maximum=100)
    except (OSError, ValueError, AttributeError, NotImplementedError, psutil.Error):
        pass
    for source, values in (("psutil-cpu", cpu), ("psutil-memory", memory)):
        unknown = [key for key, value in values.items() if value is None]
        probes.append({"source": source, "status": "partial" if unknown else "ok", "unknown_fields": unknown})
    return cpu, memory, probes


def collect_inventory(backend_python=None):
    start = time.monotonic()
    sampled_at = datetime.now(timezone.utc).isoformat()
    system = platform.system()
    cpu, memory, probes = _host_snapshot()
    gpu, gpu_probes = _accelerators(system, backend_python)
    return {"schema_version": "1", "scope": "operation_service_host", "platform": system,
            "architecture": platform.machine(), "sampled_at": sampled_at,
            "collection_seconds": round(time.monotonic()-start, 3), "cpu": cpu, "memory": memory,
            "gpu": gpu, "probes": probes + gpu_probes,
            "limits": {"cpu_quota_cores": None, "memory_limit_bytes": None,
                       "note": "Host-visible counts and available memory are observations, not guaranteed capacity. Container quotas and other workloads may reduce usable resources."},
            "resource_control": {"cpu_reservation": False, "ram_reservation": False,
                                 "gpu_reservation": False, "gpu_execution": False,
                                 "note": "Inventory does not enable execution or reserve CPU/RAM/GPU resources. Consult executable targets for separately validated device placement; discovered GPUs do not become targets automatically."}}


class HardwareDiscovery:
    """Short-lived process-local cache shared by request-scoped backend copies."""
    def __init__(self, backend_python):
        self.python = backend_python
        self.lock = threading.Lock()
        self.value = None
        self.completed_at = 0.0

    def snapshot(self):
        with self.lock:
            age = time.monotonic() - self.completed_at
            cached = self.value is not None and age < CACHE_SECONDS
            if not cached:
                self.value = collect_inventory(self.python)
                self.completed_at = time.monotonic()
                age = 0.0
            result = copy.deepcopy(self.value)
        result["freshness"] = {"cached": cached, "age_seconds": round(age, 3), "cache_ttl_seconds": CACHE_SECONDS}
        return result
