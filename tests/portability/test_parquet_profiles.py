"""Optional reader profile selection is separate from backend availability."""
import json
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import bootstrap  # noqa: E402
from scripts.dependency_profiles import PARQUET_TARGETS, lock_path  # noqa: E402
from modelbuilder.dataset import PYARROW_REQUIREMENT  # noqa: E402


@pytest.mark.parametrize("target", sorted(PARQUET_TARGETS))
def test_parquet_profile_is_exact_hash_locked_and_optional(target):
    lock = lock_path(ROOT, "parquet", target).read_text()
    assert re.findall(r"^[^#\s].*", lock, re.M) == [PYARROW_REQUIREMENT + " \\"]
    assert "--hash=sha256:" in lock
    assert "pyarrow" not in lock_path(ROOT, "service", target).read_text()
    assert 'parquet = ["' + PYARROW_REQUIREMENT + '"]' in (ROOT / "pyproject.toml").read_text()


@pytest.mark.parametrize("target", ["win32-arm64", "linux-armhf"])
def test_no_native_wheel_means_no_parquet_profile_or_mutation(target, monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "detect_target", lambda: target)
    monkeypatch.setattr(bootstrap, "run", lambda *a, **k: pytest.fail("unsupported profile must not install"))
    with pytest.raises(ValueError, match="official pinned PyArrow"):
        bootstrap.main(["--parquet", "--skip-frontend", "--service-venv", str(tmp_path / "service")])


def test_plan_reports_optional_profile_without_installing(monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "run", lambda *a, **k: pytest.fail("plan must not install"))
    monkeypatch.setattr(bootstrap, "detect_target", lambda: "linux-x64")
    assert bootstrap.main(["--plan", "--parquet"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["parquet_available"] and plan["parquet_lock"].endswith("parquet-linux-x64.lock")


@pytest.mark.parametrize("target,wheel,runtime", [
    ("linux-x64", True, True), ("linux-arm64", True, True),
    ("darwin-x64", True, False), ("darwin-arm64", True, False),
    ("win32-x64", True, False), ("win32-arm64", False, False),
    ("linux-armhf", False, False),
])
def test_plan_separates_wheel_profile_from_contained_runtime(target, wheel, runtime, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "detect_target", lambda: target)
    monkeypatch.setattr(bootstrap, "run", lambda *a, **k: pytest.fail("plan must not install or probe a decoder"))
    assert bootstrap.main(["--plan", "--parquet"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["parquet_wheel_available"] is wheel
    assert plan["parquet_available"] is runtime
    assert (plan["parquet_lock"] is not None) is wheel
    if wheel:
        assert plan["parquet_lock"].endswith(f"parquet-{target}.lock")
    if runtime:
        assert plan["parquet_unavailable"] is None
    else:
        assert "JSONL/CSV" in plan["parquet_unavailable"]
        assert ("Linux-only" in plan["parquet_unavailable"]) is wheel


@pytest.mark.parametrize("arguments,expected", [([], {"service"}), (["--backend"], {"service", "backend"}),
                                              (["--backend-only"], {"backend"})])
def test_optional_overlay_installs_only_into_selected_environments(arguments, expected, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(bootstrap, "detect_target", lambda: "linux-x64")
    monkeypatch.setattr(bootstrap, "run", lambda command, **kwargs: calls.append(list(map(str, command))))
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "uv" if name == "uv" else None)
    service, backend = tmp_path / "service", tmp_path / "backend"
    assert bootstrap.main(["--parquet", "--skip-frontend", "--skip-model-download", "--service-venv", str(service),
                           "--backend-venv", str(backend), *arguments]) == 0
    installs = [call for call in calls if any(arg.endswith("parquet-linux-x64.lock") for arg in call)]
    actual = set()
    for call in installs:
        assert "--require-hashes" in call and any(arg.startswith("--only-binary") for arg in call)
        actual.add("service" if str(service / "bin/python") in call else "backend")
    assert actual == expected and len(installs) == len(expected)


def test_existing_cpu_ci_requires_parquet_and_local_inference_in_separate_envs():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    cpu = workflow.split("  cpu-lifecycle:", 1)[1]
    assert "bash scripts/bootstrap.sh --backend --tracking --parquet" in cpu
    assert "MODELBUILDER_REQUIRE_PARQUET=1 MODELBUILDER_RUN_INTEGRATION=1 bash scripts/test_all.sh" in cpu
    aggregate = (ROOT / "scripts/test_all.sh").read_text()
    backend_line = next(line for line in aggregate.splitlines() if "-m pytest tests/integration/test_cpu_fixtures.py" in line)
    assert 'MODELBUILDER_REQUIRE_LOCAL_INFERENCE=1 PYTHONPATH="$ROOT/src" "$ROOT/.venv-backend/bin/python"' in backend_line
    assert "tests/components " in backend_line
    assert "export MODELBUILDER_REQUIRE_LOCAL_INFERENCE" not in aggregate
    assert "tests/datasets/test_native_dataset.py" in aggregate
    backend = lock_path(ROOT, "backend", "linux-x64").read_text()
    for requirement in ("torch==2.9.0+cpu", "transformers==4.57.1", "zenml==0.97.0", "safetensors==0.8.0", "tokenizers==0.22.2"):
        assert requirement in backend
    service = lock_path(ROOT, "service", "linux-x64").read_text()
    assert "torch==" not in service and "transformers==" not in service
