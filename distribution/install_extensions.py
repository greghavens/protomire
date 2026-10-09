#!/usr/bin/env python3
"""Install audited, checksum-pinned Open VSX artifacts for the selected host.

No assistant binaries are bundled or downloaded. No credentials or MCP client
configuration are written. --download-only can prepare another platform's cache.
"""

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

# Also supports importlib-based offline tests and execution from another cwd.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from platform_config import detect_target, validate_target  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME = pathlib.Path(os.environ.get("MODELBUILDER_RUNTIME_DIR", ROOT / ".runtime")).resolve()
CACHE = pathlib.Path(os.environ.get("MODELBUILDER_BUILD_CACHE", ROOT / ".cache")).resolve()


def select_artifact(entry: dict, target: str) -> dict:
    """Never substitute another native platform or a registry's default build."""
    artifacts = entry.get("artifacts", {})
    selected = artifacts.get(target) or artifacts.get("universal")
    if selected is None:
        raise ValueError(f"No audited {target} artifact for {entry['id']}@{entry['version']}")
    artifact = {**entry, **selected}
    artifact.pop("artifacts", None)
    actual = artifact.get("target_platform", "universal")
    if actual not in {target, "universal"}:
        raise ValueError(f"Artifact target mismatch for {entry['id']}: {actual} != {target}")
    if artifact.get("license") != "MIT":
        raise ValueError("A new license requires explicit audit; assistants are not included")
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", artifact["id"]):
        raise ValueError("Invalid extension ID")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", artifact["version"]):
        raise ValueError("Invalid extension version")
    if not re.fullmatch(r"[a-f0-9]{64}", artifact.get("sha256", "")):
        raise ValueError("A pinned SHA256 is required")
    url = urllib.parse.urlsplit(artifact["url"])
    if url.scheme != "https" or url.hostname != "open-vsx.org" or url.username or url.password:
        raise ValueError("Only audited HTTPS Open VSX artifacts are supported")
    return artifact


def verify_archive(path: pathlib.Path, entry: dict) -> None:
    """Verify bytes and embedded identity/target without extracting or executing."""
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
        raise ValueError(f"VSIX hash mismatch: {entry['id']}")
    with zipfile.ZipFile(path) as archive:
        package = json.loads(archive.read("extension/package.json"))
        identity = f"{package['publisher']}.{package['name']}"
        if identity.lower() != entry["id"].lower() or package["version"] != entry["version"]:
            raise ValueError(f"VSIX identity mismatch: {entry['id']}")
        manifest = ET.fromstring(archive.read("extension.vsixmanifest"))
        identities = [element for element in manifest.iter() if element.tag.split("}")[-1] == "Identity"]
        if len(identities) != 1:
            raise ValueError("VSIX must contain one identity")
        actual = identities[0].get("TargetPlatform", "universal")
        if actual != entry.get("target_platform", "universal"):
            raise ValueError(f"VSIX platform mismatch: {actual}")


def download(entry: dict, cache: pathlib.Path) -> pathlib.Path:
    target = cache / f"{entry['id']}-{entry['version']}@{entry.get('target_platform', 'universal')}.vsix"
    cache.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with urllib.request.urlopen(entry["url"], timeout=120) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError(f"VSIX hash mismatch: {entry['id']}")
        target.write_bytes(data)
    verify_archive(target, entry)
    return target


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--target", default=os.environ.get("MODELBUILDER_TARGET"))
    parser.add_argument("--manifest", type=pathlib.Path, default=ROOT / "distribution/extension_manifest/extensions.lock.json")
    parser.add_argument("--cli", help="IDE CLI executable; default is the platform-aware distribution launcher")
    parser.add_argument("--cache-dir", type=pathlib.Path, default=CACHE / "extensions")
    parser.add_argument("--user-data-dir", type=pathlib.Path, default=RUNTIME / "gui-user-data")
    parser.add_argument("--extensions-dir", type=pathlib.Path, default=RUNTIME / "extensions")
    args = parser.parse_args(argv)
    target = validate_target(args.target) if args.target else detect_target()
    if not args.download_only and target != detect_target():
        parser.error("Cross-target installation is not supported; use --download-only on this host")
    lock = json.loads(args.manifest.read_text(encoding="utf-8"))
    if lock.get("schema_version") != 2:
        parser.error("Expected extension manifest schema_version 2")
    # Validate the whole manifest before downloading or installing anything.
    entries = [select_artifact(entry, target) for entry in lock["extensions"]]
    cli = [args.cli] if args.cli else [sys.executable, str(RUNTIME / "bin/modelbuilder-ide.py")]
    downloaded = [(entry, download(entry, args.cache_dir)) for entry in entries]
    for entry, artifact in downloaded:
        print(f"Verified {entry['id']}@{entry['version']} ({entry['target_platform']}) {entry['sha256']}", flush=True)
        if not args.download_only:
            subprocess.run(
                [*cli, "--user-data-dir", str(args.user_data_dir), "--extensions-dir", str(args.extensions_dir),
                 "--install-extension", str(artifact), "--do-not-include-pack-dependencies", "--force"],
                check=True,
            )


if __name__ == "__main__":
    main()
