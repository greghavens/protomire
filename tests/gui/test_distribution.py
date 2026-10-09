"""Small offline distribution-policy tests, explicitly not GUI acceptance."""

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_source_and_toolchain_are_immutable():
    pin = json.loads((ROOT / "distribution/code_oss_pin.json").read_text())
    assert len(pin["commit"]) == 40
    assert len(pin["node_linux_x64_sha256"]) == 64
    assert pin["core_patches"] == 0
    assert pin["node_version"] == "22.19.0"


def test_product_has_no_marketplace_or_bundled_agent():
    p = json.loads((ROOT / "distribution/overlay/product.json").read_text())
    assert p["nameShort"] == "Modelbuilder"
    assert p["builtInExtensions"] == []
    assert not p.get("extensionsGallery")
    assert not p.get("defaultChatAgent")
    assert p["enableTelemetry"] is False


def test_extension_lock_has_individual_pins_and_licenses():
    lock = json.loads((ROOT / "distribution/extension_manifest/extensions.lock.json").read_text())
    assert {x["id"] for x in lock["extensions"]} == {
        "ms-python.python",
        "ms-python.debugpy",
        "ms-toolsai.jupyter",
        "detachhead.basedpyright",
    }
    for extension in lock["extensions"]:
        assert extension["license"] == "MIT"
        assert len(extension["sha256"]) == 64
        assert extension["version"] in extension["url"]
        assert extension["url"].startswith("https://open-vsx.org/")
        assert "darwin" not in extension["url"]


def test_smoke_does_not_disable_sandbox_or_silently_skip():
    script = (ROOT / "scripts/smoke_ide.sh").read_text()
    assert "--no-sandbox" not in "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    assert "'status':'BLOCKED'" in script
    assert "extensionDevelopmentPath" in script
    assert "status']=='PASS'" in script
