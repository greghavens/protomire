#!/usr/bin/env python3
"""Build the bundled runtime entirely from reviewed, hash-pinned local inputs.

Builder input JSON names native redistributable CPython/Git archives, exact
service/backend wheels, an offline wheelhouse and a source Git bundle. There is
no system-Python fallback, on-device dependency install or network resolution.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
from email.parser import BytesParser

from build_ide import extract_archive
from managed_core import relative_file
from platform_config import detect_target
from single_app import ROOT, digest, seal_runtime, safe_version, macos_floor

sys.path.insert(0, str(ROOT / "scripts"))
from dependency_profiles import backend_reason, lock_path  # noqa: E402


def verified_input(base: Path, item: dict) -> Path:
    if not isinstance(item, dict) or not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64:
        raise ValueError("Every build input requires an exact SHA256")
    path = Path(item["path"])
    path = path if path.is_absolute() else base / path
    if not path.is_file() or digest(path) != item["sha256"]:
        raise ValueError("A reviewed build input is missing or its SHA256 does not match")
    return path.resolve()


def extract_input(base: Path, item: dict, destination: Path) -> Path:
    archive = verified_input(base, item)
    if not item.get("source_url", "").startswith("https://"):
        raise ValueError("Native runtime archives require recorded official HTTPS provenance")
    destination.mkdir()
    extract_archive(archive, destination)
    return relative_file(destination, item["executable"])


def build_runtime(inputs_file: Path, output: Path) -> dict:
    inputs = json.loads(inputs_file.read_text())
    base = inputs_file.resolve().parent
    target = inputs["target"]
    if target != detect_target():
        raise ValueError("Build and probe the runtime on its native target")
    if output.exists():
        raise ValueError("Runtime output already exists; choose a fresh staging directory")
    # Validate all heavyweight inputs before changing staging state.
    components = ["service"] + ([] if backend_reason(target) else ["backend"])
    if inputs.get("mlx"):
        if target != "darwin-arm64":
            raise ValueError("MLX payloads require the reviewed Apple Silicon target")
        components.append("mlx")
    for component in components:
        verified_input(base, inputs[component]["python"])
        if component != "mlx":
            verified_input(base, inputs[component]["wheel"])
    verified_input(base, inputs["git"])
    verified_input(base, inputs["source_bundle"])
    if inputs.get("source_bundle_shallow"):
        verified_input(base, inputs["source_bundle_shallow"])
    wheelhouse = Path(inputs["wheelhouse"])
    if not wheelhouse.is_absolute():
        wheelhouse = base / wheelhouse
    if not wheelhouse.is_dir():
        raise ValueError("An offline wheelhouse is required")
    notices = Path(inputs["notices"])
    if not notices.is_absolute():
        notices = base / notices
    if not notices.is_dir():
        raise ValueError("Reviewed redistribution notices and source-offer records are required")
    with zipfile.ZipFile(verified_input(base, inputs["service"]["wheel"])) as wheel:
        metadata_files = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_files) != 1:
            raise ValueError("Application wheel must have one identity")
        application_version = safe_version(BytesParser().parsebytes(wheel.read(metadata_files[0]))["Version"])
    expected_python = (ROOT / ".python-version").read_text().strip()
    for component in components:
        if inputs[component]["python"].get("version") != expected_python:
            raise ValueError("Both runtime archives must declare the exact repository Python pin")
    output.mkdir(parents=True)
    manifest = {"schema_version": 1, "target": target, "source_commit": inputs["source_commit"],
                "source_bundle": "source.bundle", "application_version": application_version, "python_version": expected_python, "native_local_backend": backend_reason(target) or "requires_native_acceptance"}
    shutil.copytree(notices, output / "notices")
    for component in components:
        item = inputs[component]
        interpreter = extract_input(base, item["python"], output / (component + "-python"))
        wheel = verified_input(base, item["wheel"]) if component != "mlx" else None
        if wheel and not wheel.name.endswith(".whl"):
            raise ValueError("Application installs require an inert verified wheel, never a source build hook")
        lock = ROOT / "requirements/profiles/mlx-darwin-arm64.lock" if component == "mlx" else lock_path(ROOT, component, target)
        # Exact target locks already contain wheel hashes. --only-binary rejects
        # build hooks; --no-index forbids an accidental network dependency fetch.
        command = [str(interpreter), "-I", "-m", "pip", "--isolated", "install", "--no-index", "--no-compile",
                   "--only-binary=:all:", "--find-links", str(wheelhouse.resolve()), "--require-hashes", "-r", str(lock)]
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "TMP", "TEMP", "TMPDIR"}}
        subprocess.run(command, env=env, check=True)
        if wheel:
            wheel_requirements = output / ("." + component + "-application.txt")
            wheel_requirements.write_text(wheel.as_uri() + " --hash=sha256:" + item["wheel"]["sha256"] + "\n")
            try:
                subprocess.run([str(interpreter), "-I", "-m", "pip", "--isolated", "install", "--no-index", "--no-deps", "--no-compile",
                                "--require-hashes", "-r", str(wheel_requirements)], env=env, check=True)
            finally:
                wheel_requirements.unlink()
        subprocess.run([str(interpreter), "-I", "-m", "pip", "--isolated", "check"], env=env, check=True)
        manifest[component + "_python"] = interpreter.relative_to(output).as_posix()
        manifest[component + "_lock_sha256"] = digest(lock)
        if wheel:
            manifest[component + "_wheel_sha256"] = item["wheel"]["sha256"]
        manifest[component + "_python_provenance"] = {"source_url": item["python"]["source_url"], "archive_sha256": item["python"]["sha256"], "notice": item["python"]["notice"], "version": item["python"]["version"]}
    git = extract_input(base, inputs["git"], output / "git")
    manifest["git_executable"] = git.relative_to(output).as_posix()
    manifest["git_provenance"] = {"source_url": inputs["git"]["source_url"], "archive_sha256": inputs["git"]["sha256"], "notice": inputs["git"]["notice"]}
    shutil.copyfile(verified_input(base, inputs["source_bundle"]), output / "source.bundle")
    if inputs.get("source_bundle_shallow"):
        shutil.copyfile(verified_input(base, inputs["source_bundle_shallow"]), output / "source.bundle.shallow")
        manifest["source_bundle_shallow"] = "source.bundle.shallow"
    if target.startswith("darwin-"):
        floors = {component: macos_floor(output / (component + "-python"), inputs[component]["python"].get("minimum_os")) for component in components}
        manifest["minimum_os"] = max((floor for component, floor in floors.items() if component != "mlx"), key=lambda value: tuple(map(int, value.split("."))))
        if "mlx" in floors:
            manifest["mlx_minimum_os"] = floors["mlx"]
    (output / "runtime.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return seal_runtime(output, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build_runtime(args.inputs.resolve(), args.output.resolve()), indent=2))
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "runtime_built": False, "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
