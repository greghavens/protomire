"""Transport fixtures select verified native locks without changing their model."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tests.integration.native_fixture_support import native_backend_profile

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("target,torch", [
    ("linux-x64", "torch==2.9.0+cpu"),
    ("linux-arm64", "torch==2.9.0+cpu"),
    ("darwin-arm64", "torch==2.9.0"),
])
def test_native_fixture_uses_reviewed_profile_and_preserves_workload(target, torch):
    recipe = json.loads((ROOT / "examples/custom-pytorch/recipe.json").read_text())
    original = deepcopy(recipe)
    selected = native_backend_profile(recipe, ROOT, target=target)
    assert recipe == original
    assert torch in selected["environment"]["requirements"]
    path = f"requirements/profiles/backend-{target}.lock"
    entry = next(item for item in recipe["source"]["files"] if item["path"] == path)
    assert selected["environment"]["lock_file"] == entry
    assert [pin.split("==")[0] for pin in selected["environment"]["requirements"]] == [
        pin.split("==")[0] for pin in recipe["environment"]["requirements"]]
    selected["environment"] = original["environment"]
    assert selected == original


def test_native_fixture_detects_host_through_existing_platform_selector(monkeypatch):
    from distribution import platform_config
    monkeypatch.setattr(platform_config, "detect_target", lambda: "darwin-arm64")
    recipe = json.loads((ROOT / "examples/custom-pytorch/recipe.json").read_text())
    assert "torch==2.9.0" in native_backend_profile(recipe, ROOT)["environment"]["requirements"]


@pytest.mark.parametrize("failure", ["missing_inventory", "changed_bytes"])
def test_native_fixture_does_not_accept_unverified_profile(tmp_path, failure):
    recipe = json.loads((ROOT / "examples/custom-pytorch/recipe.json").read_text())
    path = "requirements/profiles/backend-darwin-arm64.lock"
    if failure == "missing_inventory":
        recipe["source"]["files"] = [item for item in recipe["source"]["files"] if item["path"] != path]
    else:
        lock = tmp_path / path
        lock.parent.mkdir(parents=True)
        lock.write_bytes((ROOT / path).read_bytes() + b"\n# changed\n")
    with pytest.raises(ValueError, match="verified source inventory|changed after source verification"):
        native_backend_profile(recipe, tmp_path, target="darwin-arm64")


def test_native_fixture_rejects_unsupported_backend_without_fallback():
    recipe = json.loads((ROOT / "examples/custom-pytorch/recipe.json").read_text())
    with pytest.raises(ValueError, match="does not support native Windows"):
        native_backend_profile(recipe, ROOT, target="win32-x64")
