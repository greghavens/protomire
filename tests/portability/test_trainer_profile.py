"""Reproducible optional Trainer dependency installation stays backend-only."""
import json
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import bootstrap  # noqa: E402


def test_optional_trainer_overlay_is_exact_and_wheel_hash_locked():
    lock = (ROOT / "requirements/profiles/trainer.lock").read_text()
    assert re.findall(r"^[^#\s].*", lock, re.M) == ["accelerate==1.11.0 \\"]
    assert "--hash=sha256:" in lock
    for target in ["linux-x64", "linux-arm64", "darwin-arm64"]:
        assert "accelerate==" not in (ROOT / f"requirements/profiles/backend-{target}.lock").read_text()
        assert "accelerate==" not in (ROOT / f"requirements/profiles/service-{target}.lock").read_text()


def test_trainer_flag_needs_backend_and_plan_does_not_install(monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "run", lambda *a, **k: pytest.fail("Plan must not install"))
    monkeypatch.setattr(bootstrap, "detect_target", lambda: "linux-x64")
    with pytest.raises(ValueError, match="requires --backend"):
        bootstrap.main(["--trainer"])
    assert bootstrap.main(["--trainer", "--backend", "--plan"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["trainer_available"] and plan["trainer_lock"].endswith("requirements/profiles/trainer.lock")


@pytest.mark.parametrize("argument", ["--backend", "--backend-only"])
def test_trainer_installs_only_in_backend_with_hashes_no_dependency_upgrade(argument, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(bootstrap, "detect_target", lambda: "linux-x64")
    monkeypatch.setattr(bootstrap, "run", lambda command, **kwargs: calls.append(list(map(str, command))))
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "uv" if name == "uv" else None)
    service, backend = tmp_path / "service", tmp_path / "backend"
    assert bootstrap.main([argument, "--trainer", "--skip-frontend", "--skip-model-download",
        "--service-venv", str(service), "--backend-venv", str(backend)]) == 0
    installs = [call for call in calls if any(arg.endswith("trainer.lock") for arg in call)]
    assert len(installs) == 1
    install = installs[0]
    assert "--require-hashes" in install and "--no-deps" in install and ":all:" in install
    assert str(backend / "bin/python") in install and str(service / "bin/python") not in install
    assert any("check" in call and str(backend / "bin/python") in call for call in calls)
