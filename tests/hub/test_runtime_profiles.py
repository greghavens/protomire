"""Hub packages remain a separately locked worker and test-only environment."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = spec_from_file_location("bootstrap_hub", ROOT / "scripts" / "bootstrap_hub.py")
bootstrap = module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)
TARGETS = ("linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64", "win32-x64", "win32-arm64")


def pins(path):
    return dict(re.findall(r"^([a-z0-9-]+)==([^\s\\]+)", path.read_text(), re.M))


@pytest.mark.parametrize("target", TARGETS)
def test_every_binary_target_has_exact_worker_and_test_closure(target):
    worker = bootstrap.plan(target=target)
    test = bootstrap.plan(target=target, tests=True)
    prod = pins(Path(worker["lock"]))
    tests = pins(Path(test["lock"]))
    assert prod["huggingface-hub"] == "0.36.2" and prod["keyring"] == "25.6.0"
    assert "pytest" not in prod and tests["pytest"] == "8.3.5"
    assert prod.items() <= tests.items()
    assert worker["directory"] != test["directory"]
    assert not worker["credentials_configured"] and worker["provider_network_acceptance"] == "pending"
    for file in (worker["lock"], test["lock"]):
        records = re.split(r"(?m)^(?=[a-z0-9-]+==)", Path(file).read_text())[1:]
        assert len(records) == len(pins(Path(file)))
        assert all(re.search(r"--hash=sha256:[0-9a-f]{64}", item) for item in records)


def test_no_invented_armhf_or_unknown_profile():
    for target in ("linux-armhf", "../linux-x64", "unknown"):
        with pytest.raises(ValueError):
            bootstrap.plan(target=target)


@pytest.mark.parametrize("environment", [".venv", ".venv-backend", ".venv-trainer", ".venv-mlx"])
def test_worker_install_cannot_replace_an_execution_environment(environment):
    with pytest.raises(ValueError, match="separate environment"):
        bootstrap.plan(directory=ROOT / environment)
