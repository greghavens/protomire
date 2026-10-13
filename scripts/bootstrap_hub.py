#!/usr/bin/env python3
"""Install a separate reviewed Hub worker/test environment, never configure accounts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "distribution"))
from platform_config import detect_target, validate_target  # noqa: E402


def plan(*, target=None, tests=False, directory=None):
    target = validate_target(target or detect_target())
    component = "hub-test" if tests else "hub"
    lock = ROOT / "requirements" / "profiles" / f"{component}-{target}.lock"
    if not lock.is_file():
        raise ValueError(f"No reviewed binary Hub profile for {target}")
    directory = Path(directory or ROOT / (".venv-hub-test" if tests else ".venv-hub")).absolute()
    if directory.resolve() in {(ROOT / name).resolve() for name in (".venv", ".venv-backend", ".venv-trainer", ".venv-mlx")}:
        raise ValueError("The Hub worker must use a separate environment")
    python = directory / ("Scripts/python.exe" if target.startswith("win32-") else "bin/python")
    return {"target": target, "lock": str(lock), "directory": str(directory), "python": str(python), "test_profile": tests,
            "credentials_configured": False, "provider_network_acceptance": "pending"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="Install test-only pytest alongside the exact Hub worker closure")
    parser.add_argument("--venv", type=Path)
    parser.add_argument("--offline-wheelhouse", type=Path)
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    try:
        result = plan(tests=args.test, directory=args.venv)
        if not args.plan:
            expected = tuple(map(int, (ROOT / ".python-version").read_text().strip().split(".")))
            if sys.version_info[:3] != expected:
                raise ValueError("Use the exact repository CPython pin to install this Hub environment")
            if args.offline_wheelhouse and not args.offline_wheelhouse.is_dir():
                raise ValueError("Offline wheelhouse is missing")
            if Path(result["directory"]).exists():
                raise ValueError("Choose a fresh Hub environment; existing environments are never overwritten")
            subprocess.run([sys.executable, "-I", "-m", "venv", result["directory"]], check=True)
            command = [result["python"], "-I", "-m", "pip", "--isolated", "install", "--disable-pip-version-check", "--keyring-provider", "disabled", "--require-hashes", "--no-cache-dir", "--only-binary=:all:", "-r", result["lock"]]
            if args.offline_wheelhouse:
                command += ["--no-index", "--find-links", str(args.offline_wheelhouse.absolute())]
            else:
                command += ["--index-url", "https://pypi.org/simple"]
            subprocess.run(command, check=True)
            subprocess.run([result["python"], "-I", "-m", "pip", "--isolated", "check"], check=True)
            subprocess.run([result["python"], "-I", "-c", "import importlib.metadata as m; import huggingface_hub, keyring; assert m.version('huggingface-hub')=='0.36.2'; assert m.version('keyring')=='25.6.0'"], check=True)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
