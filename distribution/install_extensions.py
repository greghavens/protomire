#!/usr/bin/env python3
"""Install only individually audited pinned Open VSX artifacts.

The upstream switch avoids installing extension packs such as proprietary
Pylance. No assistant binaries are bundled or downloaded by this script.
"""

import argparse
import hashlib
import json
import pathlib
import subprocess
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]


def download(entry: dict, cache: pathlib.Path) -> pathlib.Path:
    target = cache / f"{entry['id']}-{entry['version']}.vsix"
    if not target.exists():
        with urllib.request.urlopen(entry["url"], timeout=120) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError(f"VSIX hash mismatch: {entry['id']}")
        target.write_bytes(data)
    if hashlib.sha256(target.read_bytes()).hexdigest() != entry["sha256"]:
        raise ValueError(f"Cached VSIX hash mismatch: {entry['id']}")
    return target


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--download-only", action="store_true")
    p.add_argument("--cli", default=str(ROOT / ".runtime/bin/modelbuilder-ide"))
    p.add_argument("--extensions-dir", default=str(ROOT / ".runtime/extensions"))
    args = p.parse_args()
    cache = ROOT / ".cache/extensions"
    cache.mkdir(parents=True, exist_ok=True)
    entries = json.loads((ROOT / "distribution/extension_manifest/extensions.lock.json").read_text())[
        "extensions"
    ]
    for entry in entries:
        assert entry["license"] == "MIT", "A new license requires explicit audit"
        target = download(entry, cache)
        print(f"Verified {entry['id']}@{entry['version']} {entry['sha256']}", flush=True)
        if not args.download_only:
            subprocess.run(
                [
                    args.cli,
                    "--user-data-dir",
                    str(ROOT / ".runtime/gui-user-data"),
                    "--extensions-dir",
                    args.extensions_dir,
                    "--install-extension",
                    str(target),
                    "--do-not-include-pack-dependencies",
                    "--force",
                ],
                check=True,
            )
