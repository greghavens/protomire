"""Host-independent CUDA dependency selection, never a native GPU acceptance claim."""
import json
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import bootstrap  # noqa: E402
from scripts.dependency_profiles import CUDA_INDEX, CUDA_TARGETS, backend_reason, lock_path  # noqa: E402


@pytest.mark.parametrize("target", sorted(CUDA_TARGETS))
def test_cuda_profile_is_fully_hash_locked_and_preserves_existing_package_versions(target):
    lock = lock_path(ROOT, "backend", target, backend_variant="cuda-cu128")
    text = lock.read_text()
    pins = dict(re.findall(r"^([\w-]+)==([^\s]+)", text, flags=re.M))
    original = dict(re.findall(r"^([\w-]+)==([^\s]+)", lock_path(ROOT, "backend", target).read_text(), flags=re.M))
    assert pins["torch"] == "2.9.0+cu128" and len(pins) > len(original)
    assert all(pins[name] == version for name, version in original.items() if name != "torch")
    assert pins["nvidia-cuda-runtime-cu12"] == "12.8.90"
    assert pins["nvidia-cudnn-cu12"] == "9.10.2.21"
    assert pins["triton"] == "3.5.0"
    for entry in re.split(r"(?m)(?=^[\w-]+==)", text)[1:]:
        assert re.search(r"--hash=sha256:[0-9a-f]{64}", entry), entry
    assert "--backend-variant cuda-cu128" in text
    assert backend_reason(target, "cuda-cu128") is None


@pytest.mark.parametrize("target", ["darwin-arm64", "darwin-x64", "win32-x64", "win32-arm64", "linux-armhf"])
def test_cuda_rejects_unreviewed_native_targets(target):
    assert backend_reason(target, "cuda-cu128")
    with pytest.raises(ValueError, match="CUDA 12.8"):
        lock_path(ROOT, "backend", target, backend_variant="cuda-cu128")


def test_cuda_bootstrap_plan_requires_explicit_backend_selection(monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "detect_target", lambda: "linux-x64")
    monkeypatch.setattr(bootstrap, "run", lambda *a, **k: pytest.fail("Plan must not install"))
    with pytest.raises(ValueError, match="requires --backend"):
        bootstrap.main(["--plan", "--backend-variant", "cuda-cu128"])
    assert bootstrap.main(["--plan", "--backend-only", "--backend-variant", "cuda-cu128"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["backend_variant"] == "cuda-cu128" and plan["backend_unavailable"] is None
    assert plan["backend_lock"].endswith("backend-cuda-cu128-linux-x64.lock")


def test_cuda_bootstrap_sync_is_explicit_hash_enforced_and_wheel_only(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(bootstrap, "detect_target", lambda: "linux-x64")
    monkeypatch.setattr(bootstrap, "run", lambda command, **kwargs: calls.append([str(value) for value in command]))
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "/official/uv")
    service, backend = tmp_path / "service", tmp_path / "cuda"
    assert bootstrap.main(["--backend-only", "--backend-variant", "cuda-cu128", "--skip-model-download",
        "--service-venv", str(service), "--backend-venv", str(backend)]) == 0
    sync = next(command for command in calls if command[1:3] == ["pip", "sync"])
    assert "--require-hashes" in sync and "--only-binary" in sync and ":all:" in sync
    assert CUDA_INDEX in sync and str(backend / "bin/python") in sync
    assert any(value.endswith("backend-cuda-cu128-linux-x64.lock") for value in sync)
    assert not any(str(service / "bin/python") in command for command in calls)
    assert calls[-1][1:3] == ["pip", "check"]


def test_unsupported_cuda_bootstrap_does_not_install_or_silently_fall_back(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "detect_target", lambda: "darwin-arm64")
    monkeypatch.setattr(bootstrap, "run", lambda *a, **k: pytest.fail("Unsupported backend must not install"))
    assert bootstrap.main(["--backend-only", "--backend-variant", "cuda-cu128", "--skip-model-download",
        "--service-venv", str(tmp_path / "service"), "--backend-venv", str(tmp_path / "cuda")]) == 2
