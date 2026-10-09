#!/usr/bin/env python3
"""Apply metadata, a permanent feature, and the documented one-import exception.

Refuse all source modifications outside the exact native File-menu composition,
wrong pins, proprietary Marketplace integration, or missing feature artifacts.
"""

import argparse
import json
import pathlib
import shutil
import subprocess

from overlay_policy import inspect_boundary, install_menu_composition

ROOT = pathlib.Path(__file__).resolve().parents[1]


def apply(source: pathlib.Path, feature: pathlib.Path | None = None) -> dict:
    pin = json.loads((ROOT / "distribution/code_oss_pin.json").read_text())
    actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if actual != pin["commit"]:
        raise ValueError(f"Wrong Code OSS commit: {actual}")
    inspect_boundary(source, allow_unpatched=True)
    install_menu_composition(source)
    product = json.loads(
        subprocess.check_output(["git", "-C", str(source), "show", "HEAD:product.json"], text=True)
    )
    for key in (
        "extensionsGallery",
        "defaultChatAgent",
        "chatAgentExtensionId",
        "configurationSync",
        "reportIssueUrl",
        "webviewContentExternalBaseUrlTemplate",
    ):
        product.pop(key, None)
    product.update(json.loads((ROOT / "distribution/overlay/product.json").read_text()))
    (source / "product.json").write_text(json.dumps(product, indent=2) + "\n")
    if feature is not None:
        manifest = json.loads((feature / "package.json").read_text())
        if not (feature / manifest["main"]).is_file():
            raise ValueError("Compile packages/ide_feature before installing the built-in module")
        target = source / "extensions/modelbuilder-workbench"
        # Only a generated copy under our owned cache is replaced.
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(
            feature, target, ignore=shutil.ignore_patterns("node_modules", "*.vsix", "test", "tests", ".git")
        )
    return {
        "commit": actual,
        "core_patches": 1,
        "composition_imports": 1,
        "added_product_workbench_modules": 1,
        "tracked_overlay_files": inspect_boundary(source),
        "builtin_feature": feature is not None,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=pathlib.Path)
    parser.add_argument("--feature", type=pathlib.Path)
    args = parser.parse_args()
    print(
        json.dumps(apply(args.source.resolve(), args.feature.resolve() if args.feature else None), indent=2)
    )
