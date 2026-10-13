"""Inert native-target wheel metadata and profile-integrity regression probes."""
from __future__ import annotations

from email.parser import BytesParser
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import zipfile

import pytest

from modelbuilder.recipe import RecipeValidationError, default_recipe

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("profile_replay", ROOT / "scripts/replay_bundle.py")
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


@pytest.fixture
def snapshot(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    shutil.copytree(ROOT / "src/modelbuilder", source / "src/modelbuilder", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "requirements/profiles", source / "requirements/profiles")
    shutil.copyfile(ROOT / "pyproject.toml", source / "pyproject.toml")
    recipe = default_recipe("wheel-test", "wheel-test")
    # Verified inert source transport only; this fixture makes no ancestry claim.
    recipe["source"]["commit"] = "a" * 40
    recipe["environment"]["requirements"] = ["numpy==2.2.6", "torch==2.9.0+cpu",
                                             "transformers==4.57.1", "zenml[server]==0.97.0"]
    recipe["source"]["files"] = [{"path": str(path.relative_to(source)),
                                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                                 for path in sorted(source.rglob("*")) if path.is_file()]
    (source / "recipe.json").write_text(json.dumps(recipe))
    return source


def metadata(result):
    with zipfile.ZipFile(result["wheel"]) as wheel:
        path = next(name for name in wheel.namelist() if name.endswith(".dist-info/METADATA"))
        return BytesParser().parsebytes(wheel.read(path))


@pytest.mark.parametrize("target,variant,torch", [("linux-x64", "default", "2.9.0+cpu"),
    ("linux-arm64", "default", "2.9.0+cpu"), ("darwin-arm64", "default", "2.9.0"),
    ("linux-x64", "cuda-cu128", "2.9.0+cu128"), ("linux-arm64", "cuda-cu128", "2.9.0+cu128")])
def test_backend_metadata_matches_reviewed_native_lock_and_preserves_closure(snapshot, tmp_path, target, variant, torch):
    result = replay.build_wheel(snapshot, tmp_path / "native", "backend", target=target, backend_variant=variant)
    assert metadata(result).get_all("Requires-Dist") == ["numpy==2.2.6", "torch==" + torch,
                                                       "transformers==4.57.1", "zenml[server]==0.97.0"]
    profile = result["dependency_profile"]
    assert profile["target"] == target and profile["backend_variant"] == variant
    lock = snapshot / profile["lock_file"]["path"]
    assert profile["lock_file"]["sha256"] == hashlib.sha256(lock.read_bytes()).hexdigest()
    for requirement in profile["requirements"]:
        plain = requirement.replace("[server]", "")
        assert plain + " \\" in lock.read_text().splitlines()
    legacy = replay.build_wheel(snapshot, tmp_path / "legacy", "backend")
    with zipfile.ZipFile(result["wheel"]) as native, zipfile.ZipFile(legacy["wheel"]) as old:
        modules = {name for name in native.namelist() if name.startswith("modelbuilder/")}
        assert modules == {name for name in old.namelist() if name.startswith("modelbuilder/")}
        assert all(native.read(name) == old.read(name) for name in modules)
        recorded = json.loads(native.read("modelbuilder_runtime-0.1.0.dist-info/modelbuilder-profile.json"))
        assert recorded == profile
    repeated = replay.build_wheel(snapshot, tmp_path / "repeat", "backend", target=target, backend_variant=variant)
    assert repeated["sha256"] == result["sha256"]


def test_default_replay_keeps_exact_recipe_environment(snapshot, tmp_path):
    result = replay.build_wheel(snapshot, tmp_path / "legacy", "backend")
    assert "dependency_profile" not in result
    assert "torch==2.9.0+cpu" in metadata(result).get_all("Requires-Dist")


@pytest.mark.parametrize("target", ["linux-x64", "darwin-arm64", "win32-x64"])
def test_service_native_metadata_matches_its_independent_lock(snapshot, tmp_path, target):
    result = replay.build_wheel(snapshot, tmp_path / target, "service", target=target)
    assert "torch==2.9.0+cpu" not in metadata(result).get_all("Requires-Dist")
    assert "pydantic==2.11.4" in metadata(result).get_all("Requires-Dist")
    assert result["dependency_profile"]["lock_file"]["path"] == f"requirements/profiles/service-{target}.lock"


@pytest.mark.parametrize("target,variant", [("darwin-x64", "default"), ("darwin-arm64", "cuda-cu128"),
    ("linux-x64", "unknown"), ("../escape", "default"), ("linux-x64", "../escape"), (None, "cuda-cu128")])
def test_missing_unsupported_or_escaping_profile_is_rejected_before_wheel_write(snapshot, tmp_path, target, variant):
    destination = tmp_path / "wheel"
    with pytest.raises(ValueError):
        replay.build_wheel(snapshot, destination, "backend", target=target, backend_variant=variant)
    assert not destination.exists()


def test_uninventoried_profile_is_rejected_even_when_file_exists(snapshot, tmp_path):
    recipe = json.loads((snapshot / "recipe.json").read_text())
    recipe["source"]["files"] = [item for item in recipe["source"]["files"]
                                  if item["path"] != "requirements/profiles/backend-darwin-arm64.lock"]
    (snapshot / "recipe.json").write_text(json.dumps(recipe))
    with pytest.raises(ValueError, match="verified source inventory"):
        replay.build_wheel(snapshot, tmp_path / "wheel", "backend", target="darwin-arm64")


def test_modified_profile_fails_source_verification(snapshot, tmp_path):
    path = snapshot / "requirements/profiles/backend-darwin-arm64.lock"
    path.write_text(path.read_text().replace("torch==2.9.0", "torch==99.0.0"))
    with pytest.raises(RecipeValidationError, match="Source bytes"):
        replay.build_wheel(snapshot, tmp_path / "wheel", "backend", target="darwin-arm64")


def test_missing_direct_requirement_does_not_silently_drop_dependency(snapshot, tmp_path):
    recipe = json.loads((snapshot / "recipe.json").read_text())
    recipe["environment"]["requirements"].append("unreviewed-dependency==1.0.0")
    (snapshot / "recipe.json").write_text(json.dumps(recipe))
    with pytest.raises(ValueError, match="does not pin"):
        replay.build_wheel(snapshot, tmp_path / "wheel", "backend", target="darwin-arm64")
