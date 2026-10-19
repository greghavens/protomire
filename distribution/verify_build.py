#!/usr/bin/env python3
"""Fail-closed verification of source pin, overlay boundary and built artifacts."""

import hashlib
import json
import pathlib
import subprocess
import sys

from platform_config import detect_target, electron_path, validate_target

from overlay_policy import COMPOSITION, IMPORT_LINE, MODULE, inspect_boundary

ROOT = pathlib.Path(__file__).resolve().parents[1]


def verify(source: pathlib.Path, target: str | None = None) -> dict:
    target = validate_target(target or detect_target())
    pin = json.loads((ROOT / "distribution/code_oss_pin.json").read_text())
    sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    assert sha == pin["commit"], "Upstream commit mismatch"
    changed = inspect_boundary(source)
    product = json.loads((source / "product.json").read_text())
    assert product["nameShort"] == "Protomire"
    assert not product.get("extensionsGallery"), "No Microsoft Marketplace integration is permitted"
    assert not product.get("defaultChatAgent"), "No bundled assistant runtime"
    assert product["builtInExtensions"] == [], "Unaudited downloaded built-ins"
    feature = source / "extensions/modelbuilder-workbench/package.json"
    manifest = json.loads(feature.read_text())
    defaults = manifest["contributes"]["configurationDefaults"]
    assert defaults["chat.disableAIFeatures"] is True
    assert defaults["workbench.secondarySideBar.defaultVisibility"] == "hidden"
    required = [
        source / "out/main.js",
        electron_path(source, product, target),
        feature.parent / manifest["main"],
        source / "LICENSE.txt",
        source / "ThirdPartyNotices.txt",
        source / COMPOSITION,
        source / MODULE,
        source / "out/vs/workbench/contrib/modelbuilder/browser/modelbuilderMenus.contribution.js",
        source / "out/vs/workbench/workbench.common.main.js",
    ]
    for item in required:
        assert item.is_file(), f"Missing build artifact: {item.relative_to(source)}"
    assert IMPORT_LINE.strip() in (source / "out/vs/workbench/workbench.common.main.js").read_text(), (
        "Compiled workbench does not load the permanent native File-menu contribution"
    )
    return {
        "source_commit": sha,
        "target": target,
        "source_tag": pin["tag"],
        "node_version": pin["node_version"],
        "core_patches": 1,
        "composition_imports": 1,
        "added_product_workbench_modules": 1,
        "tracked_overlay_files": changed,
        "artifacts": {
            str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in required
        },
        "gui_status": "UNVERIFIED",
        "note": "Artifact existence and hashes do not establish GUI functionality.",
    }


if __name__ == "__main__":
    print(json.dumps(verify(pathlib.Path(sys.argv[1]).resolve()), indent=2))
