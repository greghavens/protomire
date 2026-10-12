"""CUDA profile/admission contracts; mocked metadata is never hardware acceptance."""
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from modelbuilder.backend import placement
from modelbuilder.backend.admission import BackendError, environment_profiles, verify_environment

ROOT = Path(__file__).resolve().parents[2]


def payload(device="cuda:1", target="linux-x64"):
    return {"target": placement.target_for_device(device), "recipe": {"nodes": [], "environment": {
        "device": device, "python": "3.12.14", "lock_file": dict(placement.CUDA_PROFILES[target])}}}


def metadata(device="cuda:1", *, allocate=False, machine="x86_64"):
    return {"available": True, "device": device, "python": "3.12.14", "platform": "linux", "machine": machine,
            "torch": "2.9.0+cu128", "cuda_build": "12.8", "hip_build": None,
            "cuda_visible_devices": 2, "allocation_verified": allocate}


def test_mocked_cuda_requires_matching_reviewed_profile_and_preserves_warm_start(monkeypatch):
    calls = []
    def fake_probe(backend, device, *, allocate=False):
        calls.append((device, allocate))
        return metadata(device, allocate=allocate)
    monkeypatch.setattr(placement, "probe", fake_probe)
    body = payload()
    assert not placement.validate_placement(None, body)["allocation_verified"]
    result = placement.validate_placement(None, body, allocate=True)
    assert result["device"] == result["requested_device"] == "cuda:1"
    assert result["allocation_verified"] and not result["component_execution_observed"]
    assert result["hardware_acceptance"] == "not_verified"
    assert not result["reservation"] and not result["isolation"] and not result["exact_resume"]
    assert calls == [("cuda:1", False), ("cuda:1", True)]
    body["resume_mode"] = "exact_state"
    with pytest.raises(BackendError, match="not hardware-verified"):
        placement.validate_placement(None, body)
    assert len(calls) == 2
    body["resume_mode"] = "warm_start"
    assert placement.validate_placement(None, body)["device"] == "cuda:1"


@pytest.mark.parametrize("field,value", [
    ("platform", "win32"), ("machine", "arm64"), ("python", "3.12.13"), ("torch", "2.9.0+cpu"),
    ("torch", "2.9.1+cu128"), ("cuda_build", "12.6"), ("hip_build", "6.4"), ("available", False),
    ("cuda_visible_devices", 1), ("cuda_visible_devices", True), ("cuda_visible_devices", 1001),
])
def test_mocked_cuda_rejects_wrong_runtime_or_invisible_ordinal(monkeypatch, field, value):
    result = {**metadata(), field: value}
    monkeypatch.setattr(placement, "probe", lambda *a, **k: result)
    with pytest.raises(BackendError):
        placement.validate_placement(None, payload())


def test_mocked_cuda_rejects_other_architecture_lock(monkeypatch):
    monkeypatch.setattr(placement, "probe", lambda *a, **k: metadata(machine="aarch64"))
    with pytest.raises(BackendError, match="architecture"):
        placement.validate_placement(None, payload())
    assert placement.validate_placement(None, payload(target="linux-arm64"))["device"] == "cuda:1"


def test_cuda_visibility_is_preserved_without_reindexing_and_evaluator_is_cpu_only():
    original = {"CUDA_VISIBLE_DEVICES": "GPU-first,GPU-second", "CUBLAS_WORKSPACE_CONFIG": ":16:8"}
    selected = placement.placement_environment("cuda:1", original)
    assert selected["CUDA_VISIBLE_DEVICES"] == original["CUDA_VISIBLE_DEVICES"]
    assert selected["MODELBUILDER_EXECUTION_DEVICE"] == "cuda:1"
    assert selected["CUBLAS_WORKSPACE_CONFIG"] == ":4096:8"
    assert original["CUBLAS_WORKSPACE_CONFIG"] == ":16:8"
    evaluator = placement.placement_environment("cpu", selected)
    assert evaluator["CUDA_VISIBLE_DEVICES"] == evaluator["HIP_VISIBLE_DEVICES"] == ""
    assert evaluator["MODELBUILDER_EXECUTION_DEVICE"] == "cpu"


def test_mocked_cuda_target_discovery_uses_only_runtime_visible_ordinals_without_allocation(monkeypatch):
    backend = SimpleNamespace(_capability_reason=lambda: None)
    calls = []
    def fake_probe(backend, device, *, allocate=False):
        calls.append((device, allocate))
        return metadata(device) if device.startswith("cuda:") else {"available": False}
    monkeypatch.setattr(placement, "probe", fake_probe)
    result = placement.targets(backend)
    assert [row["id"] for row in result] == ["local-cpu", "local-mps", "local-cuda:0", "local-cuda:1"]
    assert calls == [("mps", False), ("cuda:0", False)]
    for row in result[2:]:
        assert row["available"] and not row["allocation_verified"] and not row["exact_resume"]
        assert row["required_profile_sha256"] == placement.CUDA_PROFILES["linux-x64"]["sha256"]
        assert row["hardware_acceptance"] == "not_verified"


@pytest.mark.parametrize("change", [{"torch": "2.9.0+cpu"}, {"cuda_visible_devices": 0}, {"available": False}])
def test_mocked_wrong_cuda_runtime_never_creates_available_targets(monkeypatch, change):
    backend = SimpleNamespace(_capability_reason=lambda: None)
    monkeypatch.setattr(placement, "probe", lambda *a, **k: {**metadata(), **change})
    result = placement.targets(backend)
    assert result[-1]["id"] == "local-cuda" and not result[-1]["available"]


def test_cuda_profiles_are_reviewed_immutable_discoverable_and_changed_bytes_are_hidden(tmp_path):
    backend = SimpleNamespace(repository=ROOT)
    profiles = [p for p in environment_profiles(backend) if p.get("variant") == "cuda-cu128"]
    assert {p["target_platform"] for p in profiles} == {"linux-x64", "linux-arm64"}
    for profile in profiles:
        lock = profile["environment"]["lock_file"]
        assert hashlib.sha256((ROOT / lock["path"]).read_bytes()).hexdigest() == lock["sha256"]
        assert profile["devices"] == ["cpu", "cuda"] and profile["requires_recipe_revision"]
        changed = tmp_path / lock["path"]
        changed.parent.mkdir(parents=True, exist_ok=True)
        changed.write_bytes((ROOT / lock["path"]).read_bytes() + b"# changed\n")
    assert not environment_profiles(SimpleNamespace(repository=tmp_path))


def test_cuda_lock_metadata_check_fails_on_cpu_environment():
    torch = pytest.importorskip("torch")
    if str(torch.__version__) != "2.9.0+cpu":
        pytest.skip("Negative profile check requires the pinned CPU interpreter")
    with pytest.raises(BackendError) as error:
        verify_environment(SimpleNamespace(repository=ROOT, python=sys.executable), payload()["recipe"])
    assert error.value.code in {"dependency_mismatch", "dependency_unavailable"}


def test_real_unavailable_cuda_does_not_fall_back_to_cpu():
    torch = pytest.importorskip("torch")
    if torch.cuda.is_available():
        pytest.skip("Negative-path test requires a CUDA-unavailable host")
    result = placement.probe(SimpleNamespace(python=sys.executable), "cuda:0", allocate=True)
    assert not result["available"] and not result.get("allocation_verified")
    with pytest.raises(ValueError, match="CUDA.*(unavailable|pinned)"):
        placement.runtime_device_identity("cuda:0")


def test_real_cuda_training_checkpoint_evaluation_warm_start_and_cancel(tmp_path, monkeypatch):
    """Opt-in hardware gate; no monkeypatched CUDA or simulated success is used."""
    required = os.environ.get("MODELBUILDER_REQUIRE_CUDA") == "1"
    try:
        import torch
    except ImportError:
        if required:
            pytest.fail("CUDA acceptance requires the installed CUDA backend profile")
        pytest.skip("Real CUDA acceptance requires the CUDA backend interpreter")
    if not torch.cuda.is_available():
        if required:
            pytest.fail("CUDA acceptance was required but no CUDA device is available")
        pytest.skip("Real CUDA hardware acceptance is unverified on this host")
    device = os.environ.get("MODELBUILDER_CUDA_TEST_DEVICE", "cuda:0")
    placement.device_from_recipe({"environment": {"device": device}})
    if not device.startswith("cuda:"):
        pytest.fail("MODELBUILDER_CUDA_TEST_DEVICE must name an explicit CUDA visible ordinal")
    # The external process normally receives this environment from the adapter.
    monkeypatch.setenv("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    from modelbuilder.backend import fixtures
    runtime = placement.probe(SimpleNamespace(python=sys.executable), device, allocate=True)
    profile = placement.cuda_profile(runtime)
    assert profile is not None and runtime["allocation_verified"], runtime
    recipe = {"environment": {"python": "3.12.14", "lock_file": profile}}
    assert verify_environment(SimpleNamespace(repository=ROOT, python=sys.executable), recipe)["verified"]
    data = fixtures.dataset({"fixture": "custom"})
    initial = fixtures.initialize(data, {"fixture": "custom"}, tmp_path / "initial")
    events = []
    parameters = {"steps": 3, "_execution_device": device, "_checkpoint_ready": lambda **e: events.append(e["step"])}
    trained = fixtures.train(data, initial, parameters, tmp_path / "trained")
    saved = fixtures.load_checkpoint(trained)
    assert events == [1, 2, 3] and saved["placement"]["device"] == device
    assert saved["accelerator_rng"] is not None and saved["accelerator_rng"].device.type == "cpu"
    assert all(tensor.device.type == "cpu" for tensor in saved["model"].values())
    assert fixtures.evaluate(data, trained, {}, tmp_path / "evaluated")["device"] == "cpu"
    warm = fixtures.train(data, initial, {**parameters, "resume_mode": "warm_start"}, tmp_path / "warm", resume=trained)
    assert fixtures.load_checkpoint(warm)["start_step"] == 0
    for destination in (device, "cpu"):
        with pytest.raises(ValueError, match="exact-state"):
            fixtures.train(data, initial, {**parameters, "_execution_device": destination,
                "resume_mode": "exact_state"}, tmp_path / "rejected", resume=trained)
    cancel = tmp_path / "cancel.json"
    cancel.write_text("{}")
    with pytest.raises(InterruptedError):
        fixtures.train(data, initial, {**parameters, "_cancel_file": str(cancel)}, tmp_path / "cancelled")
    assert fixtures.load_checkpoint(tmp_path / "cancelled")["step"] == 1
    assert not list((tmp_path / "cancelled").glob("*.partial"))
    assert placement.effective_resume_modes(device, ["exact_state", "warm_start"]) == ["warm_start"]
