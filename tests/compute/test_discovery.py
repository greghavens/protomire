"""Real host CPU/RAM tests; GPU and non-Linux paths below are synthetic fixtures."""
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psutil
import pytest

from modelbuilder import compute_discovery as discovery
from modelbuilder.backend import ZenMLBackend


def test_actual_host_cpu_memory_sample():
    cpu, memory, probes = discovery._host_snapshot()
    assert cpu["logical_cores"] == psutil.cpu_count(logical=True)
    assert cpu["logical_cores"] > 0
    assert 0 <= cpu["utilization_percent"] <= 100
    assert cpu["sample_window_seconds"] == 0.1
    assert memory["total_bytes"] == psutil.virtual_memory().total > 0
    assert 0 <= memory["available_bytes"] <= memory["total_bytes"]
    assert 0 <= memory["utilization_percent"] <= 100
    assert probes[1]["status"] == "ok"


def test_host_probe_errors_are_unknown_never_zero(monkeypatch):
    def denied(*args, **kwargs):
        raise psutil.AccessDenied()
    for name in ("cpu_count", "cpu_percent", "virtual_memory", "Process"):
        monkeypatch.setattr(discovery.psutil, name, denied)
    cpu, memory, probes = discovery._host_snapshot()
    assert cpu["logical_cores"] is None and cpu["utilization_percent"] is None
    assert memory["total_bytes"] is None and memory["utilization_percent"] is None
    assert all(probe["status"] == "partial" for probe in probes)


def test_unsupported_affinity_does_not_hide_cpu_or_memory(monkeypatch):
    monkeypatch.setattr(discovery.psutil, "Process", lambda: object())
    cpu, memory, probes = discovery._host_snapshot()
    assert cpu["process_affinity_cores"] is None
    assert cpu["logical_cores"] > 0 and memory["total_bytes"] > 0
    assert probes[0]["unknown_fields"] == ["process_affinity_cores"]


def test_nvidia_csv_units_multiple_devices_and_unknown_metrics():
    devices = discovery._parse_nvidia('0, "GPU, one", 8192, 1024, 7168, 33\n1, MIG parent, [N/A], [Not Supported], 0, N/A\n'.replace(', "', ',"'))
    assert len(devices) == 2
    assert devices[0]["name"] == "GPU, one"
    assert devices[0]["vram_total_bytes"] == 8 * 1024**3
    assert devices[0]["vram_used_bytes"] == 1024**3
    assert devices[0]["utilization_percent"] == 33
    assert devices[1]["vram_total_bytes"] is None
    assert devices[1]["utilization_percent"] is None
    assert devices[1]["vram_free_bytes"] == 0
    assert all(device["execution_target_id"] is None and not device["reservable"] for device in devices)


@pytest.mark.parametrize("text", ["bad output", "0, Name, 4, 1, 3", "x, Name, 4, 1, 3, 0", "0, Name, 4, 1, 3, 0\n"*65])
def test_nvidia_malformed_output_is_rejected(text):
    with pytest.raises(ValueError):
        discovery._parse_nvidia(text)


def test_invalid_and_inconsistent_numbers_stay_unknown():
    gpu = discovery._parse_nvidia('0, GPU, 5, 10, 1, -1\n')[0]
    assert gpu["vram_used_bytes"] is None and gpu["vram_free_bytes"] is None
    assert gpu["utilization_percent"] is None
    gpu = discovery._parse_nvidia('0, GPU, 1e999, nan, inf, 101\n')[0]
    assert all(gpu[key] is None for key in ("vram_total_bytes", "vram_used_bytes", "vram_free_bytes", "utilization_percent"))
    assert discovery._number(True) is None


def test_synthetic_linux_amd_sysfs(tmp_path):
    device = tmp_path / "card0/device"
    device.mkdir(parents=True)
    for name, value in {"vendor": "0x1002", "device": "0x744c", "product_name": "Fixture AMD GPU",
                        "mem_info_vram_total": str(16*1024**3), "mem_info_vram_used": str(2*1024**3),
                        "gpu_busy_percent": "29"}.items():
        (device / name).write_text(value)
    (tmp_path / "card0-DP-1").mkdir()
    result = discovery._linux_gpus(tmp_path)
    assert len(result) == 1 and result[0]["vendor"] == "AMD"
    assert result[0]["vram_free_bytes"] == 14*1024**3
    assert result[0]["utilization_percent"] == 29
    (device / "mem_info_vram_total").unlink()
    assert discovery._linux_gpus(tmp_path)[0]["vram_total_bytes"] is None


def test_synthetic_macos_unified_ram_is_not_vram():
    result = discovery._parse_macos(json.dumps({"SPDisplaysDataType": [
        {"sppci_model": "Apple M3", "spdisplays_vram": "32 GB", "spdisplays_vram_shared": "32 GB"},
        {"sppci_model": "AMD Radeon", "spdisplays_vendor": "AMD", "spdisplays_vram": "8 GB"}]}))
    assert result[0]["memory_kind"] == "unified" and result[0]["vram_total_bytes"] is None
    assert result[1]["vram_total_bytes"] == 8*1024**3
    assert all(gpu["utilization_percent"] is None for gpu in result)


def test_synthetic_windows_adapterram_is_not_trusted_as_vram():
    result = discovery._parse_windows(json.dumps({"Name": "Fixture NVIDIA GPU", "AdapterCompatibility": "NVIDIA", "AdapterRAM": 4293918720}))
    assert len(result) == 1 and result[0]["name"] == "Fixture NVIDIA GPU"
    assert result[0]["vram_total_bytes"] is None and result[0]["utilization_percent"] is None
    assert discovery._parse_windows("null") == []


@pytest.mark.parametrize("system,source,output", [
    ("Darwin", "system-profiler", '{"SPDisplaysDataType":[{"sppci_model":"Apple M3"}]}'),
    ("Windows", "windows-cim", '{"Name":"Intel display","AdapterCompatibility":"Intel"}'),
])
def test_synthetic_native_routes_read_only_commands(monkeypatch, system, source, output):
    calls = []
    monkeypatch.setattr(discovery, "_nvidia_executable", lambda _: None)
    monkeypatch.setattr(discovery, "_run_probe", lambda args, timeout=2: (calls.append(args) or output, "ok"))
    gpu, probes = discovery._accelerators(system, None)
    assert gpu["observed_count"] == 1
    assert next(probe for probe in probes if probe["source"] == source)["status"] == "ok"
    if system == "Windows":
        assert "-NoProfile" in calls[0] and "-NonInteractive" in calls[0]
        assert "Get-CimInstance" in calls[0][-1] and "AdapterRAM" not in calls[0][-1]
    else:
        assert calls[0][1:3] == ["SPDisplaysDataType", "-json"]


@pytest.mark.parametrize("status", ["timeout", "unavailable", "error", "output_limit", "invalid_output"])
def test_probe_failures_do_not_erase_cpu_or_create_gpu(monkeypatch, status):
    monkeypatch.setattr(discovery.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(discovery, "_run_probe", lambda *args, **kwargs: (None, status))
    result = discovery.collect_inventory(None)
    assert result["cpu"]["logical_cores"] > 0
    assert result["gpu"]["devices"] == []
    assert next(p for p in result["probes"] if p["source"] == "system-profiler")["status"] == status
    assert result["gpu"]["coverage"] == "partial"


def test_nvidia_and_display_inventory_not_double_counted(monkeypatch):
    monkeypatch.setattr(discovery, "_nvidia_executable", lambda _: "/fixture/nvidia-smi")
    monkeypatch.setattr(discovery, "_run_probe", lambda *args, **kwargs: ('0, Fixture, 8, 1, 7, 0', "ok"))
    monkeypatch.setattr(discovery, "_linux_gpus", lambda: [discovery._gpu("linux-drm", 0, "NVIDIA duplicate", "NVIDIA"), discovery._gpu("linux-drm", 1, "Intel GPU", "Intel")])
    gpu, _ = discovery._accelerators("Linux", None)
    assert gpu["observed_count"] == 2
    assert {row["vendor"] for row in gpu["devices"]} == {"NVIDIA", "Intel"}


def test_torch_is_separate_visibility_not_hardware_capacity(monkeypatch):
    calls = []
    raw = '{"status":"ok","cuda_visible_devices":2,"cuda_api":"rocm","mps_available":false}'
    monkeypatch.setattr(discovery, "_run_probe", lambda args, timeout: (calls.append(args) or raw, "ok"))
    probe = discovery._torch_probe(sys.executable)
    assert probe["cuda_visible_devices"] == 2 and not probe["gpu_execution_enabled"]
    assert calls[0][1:4] == ["-I", "-B", "-c"]
    assert "get_device_properties" not in discovery._TORCH_QUERY
    assert "get_device_name" not in discovery._TORCH_QUERY


@pytest.mark.parametrize("raw,status", [('nonsense', 'invalid_output'), ('{"status":"ok"}', 'invalid_output'), ('{"status":"unavailable"}', 'unavailable')])
def test_torch_missing_or_invalid(monkeypatch, raw, status):
    monkeypatch.setattr(discovery, "_run_probe", lambda *args, **kwargs: (raw, "ok"))
    assert discovery._torch_probe(sys.executable)["status"] == status


def test_real_bounded_subprocess_timeout_output_and_environment(monkeypatch):
    monkeypatch.setenv("MODELBUILDER_TOKEN", "must-not-be-forwarded")
    monkeypatch.setenv("PYTHONPATH", "/must/not/import")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    raw, status = discovery._run_probe([sys.executable, "-I", "-c", 'import os,json;print(json.dumps({"secret":os.environ.get("MODELBUILDER_TOKEN"),"path":os.environ.get("PYTHONPATH"),"mask":os.environ.get("CUDA_VISIBLE_DEVICES"),"cwd":os.getcwd()}))'])
    assert status == "ok" and json.loads(raw) == {"secret": None, "path": None, "mask": "0", "cwd": discovery.os.path.abspath(discovery.os.sep)}
    start = time.monotonic()
    assert discovery._run_probe([sys.executable, "-I", "-c", "import time;time.sleep(30)"], timeout=0.1) == (None, "timeout")
    assert time.monotonic()-start < 2
    assert discovery._run_probe([sys.executable, "-I", "-c", "print('x'*200000)"]) == (None, "output_limit")
    assert discovery._run_probe([sys.executable, "-I", "-c", "raise RuntimeError('private details')"]) == (None, "error")
    assert discovery._run_probe(["/nonexistent/read-only-probe"]) == (None, "unavailable")


def test_cache_singleflight_copy_and_expiry(monkeypatch):
    calls = []
    lock = threading.Lock()
    def collect(python):
        with lock:
            calls.append(python)
        time.sleep(0.03)
        return {"cpu": {"logical_cores": 3}}
    monkeypatch.setattr(discovery, "collect_inventory", collect)
    collector = discovery.HardwareDiscovery(None)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: collector.snapshot(), range(4)))
    assert calls == [None]
    assert sum(not value["freshness"]["cached"] for value in results) == 1
    results[0]["cpu"]["logical_cores"] = 999
    assert collector.snapshot()["cpu"]["logical_cores"] == 3
    collector.completed_at -= discovery.CACHE_SECONDS+1
    assert not collector.snapshot()["freshness"]["cached"]
    assert calls == [None, None]


def test_backend_hardware_never_adds_execution_targets_or_enforcement(tmp_path, monkeypatch):
    backend = ZenMLBackend(tmp_path, python_executable=str(tmp_path / "absent-python"))
    monkeypatch.setattr(backend.hardware_discovery, "snapshot", lambda: {"gpu": {"devices": [{"name": "Fixture GPU"}]}})
    response = backend.invoke("execution.targets", {})
    assert [target["id"] for target in response["targets"]] == ["local-cpu", "local-mps", "local-cuda"]
    assert not response["targets"][0]["available"]
    assert response["capabilities"]["node_settings"]["resource_request"] == {"gpu": [0], "cpu": "unsupported", "memory_mb": "unsupported"}
    assert not response["remote_enabled"]
    assert backend.with_source_workspace(tmp_path, trusted=False, dependency_paths={}).hardware_discovery is backend.hardware_discovery


def test_slow_probe_holds_no_sqlite_transaction_and_does_not_block_status_checkpoint_or_writes(tmp_path, monkeypatch):
    """Actual SQLite concurrency with held synthetic probe/backend responses, not training acceptance."""
    from modelbuilder.models import Actor
    from modelbuilder.service import OperationService
    entered, release = threading.Event(), threading.Event()
    actor = Actor("owner", {"read", "modify", "execute"})

    class StatusBackend(ZenMLBackend):
        def invoke(self, operation, payload, actor=None):
            if operation == "run.status":
                return {"operation_id": payload["operation_id"], "backend_run_id": "fixture-native-id", "status": "running"}
            if operation == "model.checkpoint.get":
                return {"artifact_id": "synthetic-checkpoint", "kind": "concurrency-fixture"}
            return super().invoke(operation, payload, actor)

    service = OperationService(tmp_path / "state.sqlite3", StatusBackend(tmp_path, python_executable=sys.executable))
    second = OperationService(tmp_path / "state.sqlite3")
    project = service.invoke("project.create", {"slug": "concurrent", "model_name": "One model", "idempotency_key": "p"}, actor)
    project_id = project["project_id"]
    operation_id = "00000000-0000-4000-8000-000000000001"
    with service.store.transaction():
        service.store.put("operation", operation_id, project_id, {"operation_id": operation_id,
            "project_id": project_id, "backend_run_id": "fixture-native-id", "status": "running", "revision": 1,
            "recipe_id": project["recipe_id"], "recipe_revision": 1, "recipe_digest": "fixture"})
    monkeypatch.setattr(discovery, "_nvidia_executable", lambda _: None)
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5), "The concurrency test failed to release its synthetic probe"
        return None, "timeout"
    monkeypatch.setattr(discovery, "_run_probe", slow)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            probe = pool.submit(service.invoke, "execution.targets", {"project_id": project_id}, actor)
            assert entered.wait(2)
            try:
                assert not service.store.connection.in_transaction
                status = pool.submit(service.invoke, "run.status", {"project_id": project_id, "operation_id": operation_id}, actor)
                assert status.result(timeout=1)["status"] == "running"
                checkpoint = pool.submit(service.invoke, "model.checkpoint.get", {"project_id": project_id, "operation_id": operation_id}, actor)
                assert checkpoint.result(timeout=1)["artifact_id"] == "synthetic-checkpoint"
                writer = pool.submit(second.invoke, "project.create", {"slug": "during-probe", "model_name": "Other model", "idempotency_key": "other"}, actor)
                assert writer.result(timeout=1)["slug"] == "during-probe"
                assert not probe.done()
            finally:
                release.set()
            result = probe.result(timeout=2)
            assert result["hardware"]["probes"][-1]["status"] == "timeout"
    finally:
        release.set()
        service.store.close()
        second.store.close()


def test_readonly_fast_path_preserves_authorization_project_scope_and_payload_checks(tmp_path, monkeypatch):
    from modelbuilder.models import Actor, ServiceError
    from modelbuilder.service import OperationService
    backend = ZenMLBackend(tmp_path, python_executable=sys.executable)
    service = OperationService(tmp_path / "state.sqlite3", backend)
    owner = Actor("owner", {"read", "modify"})
    project = service.invoke("project.create", {"slug": "authorized", "model_name": "One model", "idempotency_key": "p"}, owner)
    calls = []
    monkeypatch.setattr(backend.hardware_discovery, "snapshot", lambda: calls.append(True) or {})
    try:
        for actor, payload, code in (
            (Actor("no-read", {"modify"}), {"project_id": project["project_id"]}, "forbidden"),
            (Actor("other-project", {"read"}, {"other"}), {"project_id": project["project_id"]}, "forbidden"),
            (owner, {"project_id": "missing"}, "not_found"),
            (owner, {"project_id": project["project_id"], "python": "/untrusted/program"}, "validation_error"),
        ):
            with pytest.raises(ServiceError) as error:
                service.invoke("execution.targets", payload, actor)
            assert error.value.code == code
        assert not calls
        service.invoke("execution.targets", {"project_id": project["project_id"]}, Actor("reader", {"read"}))
        assert calls == [True]
    finally:
        service.store.close()


def test_nvidia_lookup_never_selects_project_executable_or_path(tmp_path, monkeypatch):
    executable = tmp_path / "nvidia-smi"
    executable.write_text("raise RuntimeError('untrusted project program must never run')")
    executable.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert discovery._nvidia_executable("Linux") != str(executable)
    assert discovery._nvidia_executable("Darwin") is None
