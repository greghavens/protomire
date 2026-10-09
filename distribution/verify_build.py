#!/usr/bin/env python3
"""Fail-closed verification of source pin, overlay boundary and built artifacts."""

import hashlib
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def verify(source: pathlib.Path) -> dict:
    pin = json.loads((ROOT / "distribution/code_oss_pin.json").read_text())
    sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    assert sha == pin["commit"], "Upstream commit mismatch"
    changed = subprocess.check_output(
        ["git", "-C", str(source), "diff", "HEAD", "--name-only"], text=True
    ).splitlines()
    assert set(changed) <= {"product.json"}, f"Unexpected core changes: {changed}"
    product = json.loads((source / "product.json").read_text())
    assert product["nameShort"] == "Modelbuilder"
    assert not product.get("extensionsGallery"), "No Microsoft Marketplace integration is permitted"
    assert not product.get("defaultChatAgent"), "No bundled assistant runtime"
    assert product["builtInExtensions"] == [], "Unaudited downloaded built-ins"
    feature = source / "extensions/modelbuilder-workbench/package.json"
    manifest = json.loads(feature.read_text())
    required = [
        source / "out/main.js",
        source / ".build/electron/modelbuilder-ide",
        feature.parent / manifest["main"],
        source / "LICENSE.txt",
        source / "ThirdPartyNotices.txt",
    ]
    for item in required:
        assert item.is_file(), f"Missing build artifact: {item.relative_to(source)}"
    return {
        "source_commit": sha,
        "source_tag": pin["tag"],
        "node_version": pin["node_version"],
        "core_patches": 0,
        "tracked_overlay_files": changed,
        "artifacts": {
            str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in required
        },
        "gui_status": "UNVERIFIED",
        "note": "Artifact existence and hashes do not establish GUI functionality.",
    }


if __name__ == "__main__":
    print(json.dumps(verify(pathlib.Path(sys.argv[1]).resolve()), indent=2))
