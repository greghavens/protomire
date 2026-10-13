#!/usr/bin/env python3
"""Seal a self-contained runtime and package existing native Code OSS output.

This release-engineering tool is not an end-user setup script. It never installs
Python on the destination machine, downloads unpinned runtimes, publishes,
signs, configures an updater, or claims cross-platform native acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import platform
import re
import secrets
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from build_ide import build, build_plan
from platform_config import detect_target, validate_target
from managed_core import CoreError, relative_file, restore_source, runtime_manifest, verify_runtime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from dependency_profiles import backend_reason, HUB_TARGETS, lock_path  # noqa: E402


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_version(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9][A-Za-z0-9.-]*)?", value):
        raise ValueError("Application version must be a safe explicit semantic version")
    return value


def macos_floor(root: Path, declared: str, exclude_prefixes: tuple[str, ...] = ()) -> str:
    if not isinstance(declared, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", declared):
        raise ValueError("macOS runtime inputs require an explicit minimum_os, including their Python build floor")
    minima = [tuple(map(int, declared.split(".")))]
    for wheel in root.rglob("*.dist-info/WHEEL"):
        if wheel.relative_to(root).parts[0] in exclude_prefixes:
            continue
        tags = re.findall(r"macosx_([0-9]+)_([0-9]+)_(?:arm64|x86_64|universal2)", wheel.read_text())
        if tags:
            minima.append(min(tuple(map(int, tag)) for tag in tags))
    return ".".join(map(str, max(minima)))


def artifact_name(product: dict, target: str) -> str:
    name = product.get("applicationName")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name):
        raise ValueError("Application artifact name contains unsafe characters")
    return name + "-" + safe_version(product.get("modelbuilderVersion")) + "-" + validate_target(target) + "-dev-unsigned"


def plan(target: str, root: Path = ROOT) -> dict:
    target = validate_target(target)
    upstream = build_plan(root, target)
    return {"target": target, "upstream_task": upstream["package_task"],
            "upstream_output": str(Path(upstream["source"]).parent / ("VSCode-" + target)),
            "format": "AppImage" if target.startswith("linux-") else "dmg" if target.startswith("darwin-") else "exe",
            "core": "mandatory bundled operation service and per-consumer project-scoped read-only MCP",
            "build_status": "not_built", "release_status": "private_unsigned_development_only",
            "native_local_backend": backend_reason(target) or "requires_native_acceptance",
            "trainer_profile": {"status": "unsupported_target" if backend_reason(target) else "required_bundled_overlay",
                                "path": "requirements/profiles/trainer.lock", "native_execution": "not_run"},
            "requirements": ["native Code OSS package", "sealed relocatable Python/runtime payload", "bundled Git and notices",
                             "source bundle", "native artifact smoke and clean-machine acceptance"]}


def inventory(root: Path) -> dict[str, str]:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.name == "runtime.json" and path.parent == root:
            continue
        if path.is_symlink() and not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Runtime contains a symlink outside its payload")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = digest(path)
        elif not path.is_dir():
            raise ValueError("Runtime contains an unsupported filesystem entry")
    return files


def probe_python(root: Path, executable: str, modules: list[str], expected_version: str) -> dict:
    python = relative_file(root, executable)
    code = ("import sys,json,importlib; names=" + repr(modules) + "; "
            "locations={name:importlib.import_module(name).__file__ for name in names}; "
            "print(json.dumps({'prefix':sys.prefix,'base_prefix':sys.base_prefix,'version':list(sys.version_info[:3]),'modules':locations}))")
    with tempfile.TemporaryDirectory(prefix="modelbuilder-runtime-probe-") as home:
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG"}}
        env.update(HOME=home, USERPROFILE=home, APPDATA=home, LOCALAPPDATA=home,
                   ZENML_CONFIG_PATH=str(Path(home) / "zenml"), ZENML_ANALYTICS_OPT_IN="false",
                   HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        result = subprocess.run([str(python), "-I", "-B", "-c", code], capture_output=True, text=True, timeout=120, check=True, env=env)
    probe = json.loads(result.stdout)
    paths = [probe["prefix"], probe["base_prefix"], *probe["modules"].values()]
    if ".".join(map(str, probe["version"])) != expected_version or any(not Path(path).resolve().is_relative_to(root.resolve()) for path in paths):
        raise ValueError("Python must match the exact declared pin and be self-contained, not a system interpreter or external venv")
    return {"version": probe["version"], "modules": {name: str(Path(path).relative_to(root)) for name, path in probe["modules"].items()}}


def validate_hub_profile(root: Path, manifest: dict, target: str) -> Path | None:
    if target not in HUB_TARGETS:
        if manifest.get("hub_python") or manifest.get("hub_status") != "unsupported_target":
            raise ValueError("No reviewed Hub/keyring runtime for this target")
        return None
    if manifest.get("hub_status") != "bundled":
        raise ValueError("Mandatory bundled Hub worker is missing")
    relative_file(root, manifest.get("hub_python"))
    expected = lock_path(ROOT, "hub", target)
    if manifest.get("hub_profile") != "profiles/" + expected.name:
        raise ValueError("Hub profile must match the exact native target")
    profile = relative_file(root, manifest["hub_profile"])
    if digest(profile) != digest(expected) or manifest.get("hub_lock_sha256") != digest(expected):
        raise ValueError("Bundled Hub profile differs from its reviewed hash lock")
    return profile


def probe_hub(root: Path, manifest: dict, worker: str) -> dict:
    """Probe the moved interpreter, complete locked closure and real offline SDK worker."""
    result = probe_python(root, manifest["hub_python"], ["huggingface_hub", "keyring"], manifest["python_version"])
    lock = relative_file(root, manifest["hub_profile"])
    pins = dict(re.findall(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9.+!-]+)(?:\s|$)", lock.read_text(), re.M))
    if pins.get("huggingface-hub") != "0.36.2" or pins.get("keyring") != "25.6.0":
        raise ValueError("Hub profile does not contain the reviewed SDK and keyring pins")
    python = relative_file(root, manifest["hub_python"])
    code = ("import json,importlib.metadata as m; names=" + repr(list(pins)) + "; "
            "print(json.dumps({n:{'version':m.version(n),'root':str(m.distribution(n).locate_file(''))} for n in names}))")
    with tempfile.TemporaryDirectory(prefix="modelbuilder-hub-offline-probe-") as home:
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG"}}
        env.update(HOME=home, USERPROFILE=home, APPDATA=home, LOCALAPPDATA=home,
                   HF_HOME=home, HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1")
        found = json.loads(subprocess.run([str(python), "-I", "-B", "-c", code], capture_output=True, text=True,
                                         timeout=120, check=True, env=env).stdout)
        if any(found[name]["version"] != version or not Path(found[name]["root"]).resolve().is_relative_to(root.resolve()) for name, version in pins.items()):
            raise ValueError("Hub dependency closure differs from its sealed profile or uses external packages")
        output = Path(home) / "result.json"
        request = {"action": "card.inspect", "payload": {"card": "---\nlicense: apache-2.0\n---\nOffline packaging fixture\n"},
                   "credential": None, "result_path": str(output)}
        subprocess.run([str(python), "-I", "-B", str(relative_file(root, worker))], input=json.dumps(request),
                       capture_output=True, text=True, timeout=120, check=True, env=env)
        if not output.is_file() or json.loads(output.read_text()) != {"license": "apache-2.0"}:
            raise ValueError("The relocated bundled Hub SDK worker failed its offline operation")
    return {**result, "locked_packages": pins, "offline_worker": "passed"}


def validate_trainer_profile(root: Path, manifest: dict, target: str) -> dict:
    profile = manifest.get("trainer_profile", {})
    if not isinstance(profile, dict):
        raise ValueError("Trainer profile metadata is missing or invalid")
    if backend_reason(target):
        if profile.get("status") != "unsupported_target":
            raise ValueError("Trainer must explicitly report the unsupported local-backend target")
        return profile
    expected = ROOT / "requirements/profiles/trainer.lock"
    if (profile.get("status") != "bundled" or profile.get("path") != "profiles/trainer.lock"
            or profile.get("sha256") != digest(expected)
            or digest(relative_file(root, profile["path"])) != digest(expected)
            or profile.get("backend_lock_sha256") != digest(lock_path(ROOT, "backend", target))):
        raise ValueError("Bundled Trainer requires the exact overlay and complete target backend lock")
    return profile


def probe_trainer(root: Path, manifest: dict) -> dict:
    result = probe_python(root, manifest["backend_python"], ["accelerate", "transformers.trainer"], manifest["python_version"])
    python = relative_file(root, manifest["backend_python"])
    pins = {}
    for profile in (lock_path(ROOT, "backend", manifest["target"]), ROOT / "requirements/profiles/trainer.lock"):
        for name, version in re.findall(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9.+!-]+)(?:\s|$)", profile.read_text(), re.M):
            if name in pins and pins[name] != version:
                raise ValueError("Trainer overlay conflicts with its complete backend profile")
            pins[name] = version
    code = ("import importlib.metadata as m,json; names=" + repr(list(pins)) + "; "
            "print(json.dumps({name:{'version':m.version(name),'root':str(m.distribution(name).locate_file(''))} for name in names}))")
    found = json.loads(subprocess.run([str(python), "-I", "-B", "-c", code], capture_output=True, text=True,
                                     check=True, timeout=30, env={key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG"}}).stdout)
    if any(found.get(name, {}).get("version") != version or not Path(found[name]["root"]).resolve().is_relative_to(root.resolve()) for name, version in pins.items()):
        raise ValueError("Installed Trainer/backend closure differs from its sealed hash locks")
    return {**result, "packages": pins, "profile_sha256": manifest["trainer_profile"]["sha256"],
            "backend_lock_sha256": manifest["trainer_profile"]["backend_lock_sha256"], "native_execution": "requires_native_acceptance"}


def seal_runtime(root: Path, target: str) -> dict:
    """Seal builder-supplied, reviewed native runtime trees; no network resolution."""
    target = validate_target(target)
    if target != detect_target():
        raise ValueError("Seal runtime on its native platform; cross-native probes are not acceptance")
    manifest = runtime_manifest(root)
    if manifest.get("target") != target:
        raise ValueError("Runtime target does not match the selected native package")
    for field in ("service_python", "git_executable", "source_bundle"):
        relative_file(root, manifest.get(field))
    if not isinstance(manifest.get("source_commit"), str) or not re.fullmatch(r"[0-9a-f]{40}", manifest["source_commit"]):
        raise ValueError("A verified full source commit is required")
    if not (root / "notices").is_dir() or not any((root / "notices").rglob("*")):
        raise ValueError("Runtime/Python/Git/dependency license notices and applicable source obligations are required")
    expected_version = (ROOT / ".python-version").read_text().strip()
    if manifest.get("python_version") != expected_version:
        raise ValueError("Runtime interpreter declaration must match the repository's exact Python pin")
    safe_version(manifest.get("application_version"))
    hub_profile = validate_hub_profile(root, manifest, target)
    validate_trainer_profile(root, manifest, target)
    provenance_fields = ["service_python_provenance", "git_provenance"]
    if hub_profile:
        provenance_fields.append("hub_python_provenance")
    if backend_reason(target) is None:
        provenance_fields.append("backend_python_provenance")
    if manifest.get("mlx_python"):
        if target != "darwin-arm64":
            raise ValueError("The reviewed MLX runtime is Apple Silicon only")
        relative_file(root, manifest["mlx_python"])
        provenance_fields.append("mlx_python_provenance")
    optional_prefixes = (Path(manifest["mlx_python"]).parts[0],) if manifest.get("mlx_python") else ()
    if target.startswith("darwin-") and macos_floor(root, manifest.get("minimum_os"), optional_prefixes) != manifest["minimum_os"]:
        raise ValueError("Declared macOS minimum is below an installed native wheel's floor")
    for field in provenance_fields:
        metadata = manifest.get(field, {})
        if not isinstance(metadata, dict) or not metadata.get("source_url", "").startswith("https://") or len(metadata.get("archive_sha256", "")) != 64 or not metadata.get("notice"):
            raise ValueError("Runtime provenance requires reviewed source URL, archive SHA256 and license notice")
        relative_file(root, metadata["notice"])
    if backend_reason(target) is None:
        relative_file(root, manifest.get("backend_python"))
    # Move (not copy) the entire prepared payload so absolute build-location
    # dependencies cannot be hidden by the original tree still being present.
    moved = root.with_name(root.name + " relocated " + secrets.token_hex(6))
    root.rename(moved)
    try:
        probes = {"service": probe_python(moved, manifest["service_python"], ["modelbuilder.api", "modelbuilder.mcp_server", "psutil"] + (["modelbuilder.hub_sdk_worker"] if hub_profile else []), expected_version)}
        if backend_reason(target) is None:
            probes["backend"] = probe_python(moved, manifest["backend_python"], ["modelbuilder.backend.sdk", "zenml", "torch"], expected_version)
            probes["trainer"] = probe_trainer(moved, manifest)
        if hub_profile:
            probes["hub"] = probe_hub(moved, manifest, probes["service"]["modules"]["modelbuilder.hub_sdk_worker"])
        if manifest.get("mlx_python"):
            required = manifest.get("mlx_minimum_os")
            if not isinstance(required, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", required):
                raise ValueError("Optional MLX requires its own declared macOS floor")
            if tuple(map(int, platform.mac_ver()[0].split(".")[:2])) >= tuple(map(int, required.split("."))):
                probes["mlx"] = probe_python(moved, manifest["mlx_python"], ["mlx.core", "mlx_lm"], expected_version)
            else:
                probes["mlx"] = {"status": "disabled_on_this_host", "minimum_os": required}
        git = relative_file(moved, manifest["git_executable"])
        git_env = {key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG"}}
        git_exec = Path(subprocess.check_output([str(git), "--exec-path"], env=git_env, text=True, timeout=30).strip())
        if not git_exec.is_dir() or not git_exec.resolve().is_relative_to(moved.resolve()):
            raise ValueError("Bundled Git helpers still depend on an external installation after relocation")
        shallow = relative_file(moved, manifest["source_bundle_shallow"]) if manifest.get("source_bundle_shallow") else None
        with tempfile.TemporaryDirectory(prefix="modelbuilder-source-probe-") as temporary:
            source_probe = restore_source(git, relative_file(moved, manifest["source_bundle"]),
                                          Path(temporary) / "source", manifest["source_commit"], shallow)
        probes["git"] = {"exec_path": git_exec.relative_to(moved).as_posix(),
                         "source_commit_present": True, "source_restore": source_probe}

    finally:
        moved.rename(root)
    shutil.copyfile(ROOT / "distribution/managed_core.py", root / "managed_core.py")
    shutil.copyfile(ROOT / "distribution/windows_storage.py", root / "windows_storage.py")
    shutil.copyfile(ROOT / "distribution/reviewed_requests.py", root / "reviewed_requests.py")
    shutil.copyfile(ROOT / "distribution/reviewed_mcp.py", root / "reviewed_mcp.py")
    shutil.copyfile(ROOT / "distribution/managed_hub.py", root / "managed_hub.py")
    shutil.copyfile(ROOT / "distribution/hub_credentials.py", root / "hub_credentials.py")
    manifest["files"] = inventory(root)
    manifest["probes"] = probes
    manifest["sealed"] = True
    manifest["relocation_probe"] = "passed"
    manifest["native_dependency_closure"] = "requires_clean_machine_acceptance"
    manifest["native_acceptance"] = "not_run"
    (root / "runtime.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    verify_runtime(root, manifest)
    return {"target": target, "files": len(manifest["files"]), "sealed": True, "native_acceptance": "not_run"}


def application_root(app: Path, target: str) -> Path:
    if target.startswith("darwin-"):
        apps = [app] if app.suffix == ".app" else list(app.glob("*.app"))
        if len(apps) != 1:
            raise ValueError("Expected exactly one native .app bundle")
        return apps[0] / "Contents/Resources/app"
    return app / "resources/app"


def validate_payload(payload: Path, target: str) -> dict:
    manifest = runtime_manifest(payload)
    if manifest.get("target") != target or manifest.get("sealed") is not True:
        raise ValueError("Package requires a sealed runtime for this exact native target")
    if manifest.get("relocation_probe") != "passed":
        raise ValueError("Runtime must pass a moved-payload interpreter and Git probe before packaging")
    safe_version(manifest.get("application_version"))
    if manifest.get("python_version") != (ROOT / ".python-version").read_text().strip():
        raise ValueError("Runtime does not declare the exact pinned interpreter version")
    validate_hub_profile(payload, manifest, target)
    validate_trainer_profile(payload, manifest, target)
    verify_runtime(payload, manifest)
    if manifest["files"] != inventory(payload):
        raise ValueError("Runtime contains unlisted or missing files; reseal after review")
    for field in ("service_python", "git_executable", "source_bundle"):
        relative_file(payload, manifest.get(field))
    if backend_reason(target) is None:
        relative_file(payload, manifest.get("backend_python"))
    for name in ("managed_core.py", "windows_storage.py", "reviewed_requests.py", "reviewed_mcp.py", "managed_hub.py", "hub_credentials.py"):
        if not (payload / name).is_file() or digest(payload / name) != digest(ROOT / "distribution" / name):
            raise ValueError("Runtime lifecycle is stale; reseal using the current distribution scripts")
    return manifest


def stage(app: Path, payload: Path, output: Path, target: str) -> dict:
    validate_target(target)
    manifest = validate_payload(payload, target)
    source_root = application_root(app, target)
    feature = source_root / "extensions/modelbuilder-workbench"
    for name in ("package.json", "out/extension.js", "out/managedCore.js", "out/nativeReview.js"):
        if not (feature / name).is_file():
            raise ValueError("Native application is missing the compiled mandatory core integration")
    product = json.loads((source_root / "product.json").read_text())
    if product.get("applicationName") != "modelbuilder-ide" or product.get("extensionsGallery"):
        raise ValueError("Expected the approved Modelbuilder Code OSS product")
    if output.exists():
        raise ValueError("Staging destination already exists; use a fresh output directory")
    shutil.copytree(app, output, symlinks=True)
    destination = application_root(output, target)
    core = destination / "extensions/modelbuilder-workbench/resources/core"
    if core.exists():
        raise ValueError("An unreviewed runtime is already embedded in this app")
    shutil.copytree(payload, core, symlinks=True)
    product["modelbuilderBundledCore"] = True
    product["modelbuilderVersion"] = safe_version(manifest.get("application_version"))
    product.pop("updateUrl", None)
    product.pop("downloadUrl", None)
    (destination / "product.json").write_text(json.dumps(product, indent=2) + "\n")
    if target.startswith("darwin-"):
        optional_prefixes = (Path(manifest["mlx_python"]).parts[0],) if manifest.get("mlx_python") else ()
        floor = macos_floor(core, manifest.get("minimum_os"), optional_prefixes)
        info_path = destination.parents[1] / "Info.plist"
        info = plistlib.loads(info_path.read_bytes())
        old = str(info.get("LSMinimumSystemVersion", "0.0")).split(".")[:2]
        if tuple(map(int, floor.split("."))) > tuple(map(int, old)):
            info["LSMinimumSystemVersion"] = floor
        info_path.write_bytes(plistlib.dumps(info))
    (destination / "MODELBUILDER-DISTRIBUTION.txt").write_text(
        "PRIVATE UNSIGNED DEVELOPMENT BUILD. Not a published or signed release.\n"
        "Product license decision, third-party redistribution review, native clean-machine acceptance, signing and updater are pending.\n"
        "Mandatory local core and MCP are bundled. Optional vendor assistants are not bundled.\n"
        "Windows native local training is not implemented; a Windows installer is not proof of POSIX backend compatibility.\n")
    return {"target": target, "application": str(output), "runtime_files": len(manifest["files"]),
            "staged": True, "installer_built": False, "native_acceptance": "not_run"}


def package(app: Path, output: Path, target: str, appimagetool: Path | None = None,
            appimagetool_sha256: str | None = None) -> dict:
    if target != detect_target():
        raise ValueError("Installers must be built and tested on the native target")
    destination = application_root(app, target)
    product = json.loads((destination / "product.json").read_text())
    if product.get("modelbuilderBundledCore") is not True:
        raise ValueError("Package only a staged single-app product")
    validate_payload(destination / "extensions/modelbuilder-workbench/resources/core", target)
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / artifact_name(product, target)
    if target.startswith("linux-"):
        if not appimagetool or not appimagetool_sha256 or digest(appimagetool) != appimagetool_sha256:
            raise ValueError("Supply a reviewed appimagetool and its exact SHA256; no automatic tool download")
        appdir = output / "Modelbuilder.AppDir"
        if appdir.exists():
            raise ValueError("AppDir already exists; use a fresh output directory")
        shutil.copytree(app, appdir / "usr/lib/modelbuilder", symlinks=True)
        (appdir / "AppRun").write_text('#!/bin/sh\nset -eu\nHERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\nexec "$HERE/usr/lib/modelbuilder/modelbuilder-ide" "$@"\n')
        (appdir / "AppRun").chmod(0o755)
        shutil.copyfile(ROOT / "distribution/assets/modelbuilder.desktop", appdir / "modelbuilder.desktop")
        shutil.copyfile(ROOT / "packages/ide_feature/media/model.svg", appdir / "modelbuilder.svg")
        artifact = artifact.with_name(artifact.name + ".AppImage")
        env = {**os.environ, "ARCH": {"x64": "x86_64", "arm64": "aarch64", "armhf": "armhf"}[target.split("-")[1]], "APPIMAGE_EXTRACT_AND_RUN": "1"}
        subprocess.run([str(appimagetool.resolve()), str(appdir.resolve()), str(artifact.resolve())], env=env, check=True)
    elif target.startswith("darwin-"):
        apps = [app] if app.suffix == ".app" else list(app.glob("*.app"))
        disk = output / "dmg-root"
        disk.mkdir()
        shutil.copytree(apps[0], disk / apps[0].name, symlinks=True)
        (disk / "Applications").symlink_to("/Applications", target_is_directory=True)
        artifact = artifact.with_name(artifact.name + ".dmg")
        subprocess.run(["hdiutil", "create", "-volname", "Modelbuilder", "-srcfolder", str(disk),
                        "-format", "UDZO", str(artifact)], check=True)
        subprocess.run(["hdiutil", "verify", str(artifact)], check=True)
    else:
        artifact = windows_installer(app, output, target)

    if not artifact.is_file():
        raise ValueError("Native packager did not produce the expected artifact")
    return {"artifact": str(artifact), "sha256": digest(artifact), "target": target,
            "signed": False, "published": False, "native_acceptance": "not_run"}


def windows_installer(app: Path, output: Path, target: str) -> Path:
    """Invoke the pinned upstream Inno Setup recipe against the staged app."""
    upstream = build_plan(ROOT, target)
    source = Path(upstream["source"])
    actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if actual != upstream["pin"]["commit"]:
        raise ValueError("Windows installer requires the exact pinned upstream source")
    compiler = source / "node_modules/innosetup/bin/ISCC.exe"
    recipe = source / "build/win32/code.iss"
    if not compiler.is_file() or not recipe.is_file():
        raise ValueError("Install the pinned upstream native build dependencies before packaging Windows")
    product_file = application_root(app, target) / "product.json"
    product = json.loads(product_file.read_text())
    product["target"] = "user"
    installer_product = output / "product.json"
    installer_product.write_text(json.dumps(product, indent=2) + "\n")
    architecture = target.split("-")[1]
    ids = {"x64": "{7F2CEB6F-6C24-4A45-9852-566193F1D433}", "arm64": "{351D596A-773D-4BCA-B21A-4E0ECA73FA22}"}
    definitions = {
        "NameLong": product["nameLong"], "NameShort": product["nameShort"], "DirName": product["win32DirName"],
        "Version": safe_version(product.get("modelbuilderVersion")), "RawVersion": safe_version(product.get("modelbuilderVersion")).split("-")[0], "NameVersion": product["nameLong"] + " (User)",
        "ExeBasename": product["nameShort"], "RegValueName": product["win32RegValueName"],
        "ShellNameShort": product["win32ShellNameShort"], "AppMutex": product["win32MutexName"],
        "TunnelMutex": "modelbuilder-unused-tunnel", "TunnelServiceMutex": "modelbuilder-unused-tunnel-service",
        "TunnelApplicationName": "modelbuilder-unused-tunnel", "ApplicationName": product["applicationName"],
        "Arch": architecture, "AppId": ids[architecture], "IncompatibleTargetAppId": ids[architecture],
        "AppUserId": product["win32AppUserModelId"], "ArchitecturesAllowed": architecture,
        "ArchitecturesInstallIn64BitMode": architecture, "SourceDir": str(app), "RepoDir": str(source),
        "OutputDir": str(output), "InstallTarget": "user", "ProductJsonPath": str(installer_product), "Quality": "dev"}
    subprocess.run([str(compiler), str(recipe), *["/d" + key + "=" + value for key, value in definitions.items()]], check=True)
    candidates = list(output.glob("*.exe"))
    if len(candidates) != 1:
        raise ValueError("Upstream Windows installer did not produce exactly one executable")
    return candidates[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "build-editor", "seal-runtime", "stage", "package"])
    parser.add_argument("--target", default=detect_target())
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--app", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--appimagetool", type=Path)
    parser.add_argument("--appimagetool-sha256")
    args = parser.parse_args()
    try:
        if args.action == "plan":
            result = plan(args.target)
        elif args.action == "build-editor":
            build(ROOT, build_plan(ROOT, args.target), "package")
            result = plan(args.target)
            result["build_status"] = "upstream_package_completed"
        elif args.action == "seal-runtime":
            if not args.runtime:
                parser.error("--runtime is required")
            result = seal_runtime(args.runtime.resolve(), args.target)
        elif args.action == "stage":
            if not all([args.app, args.runtime, args.output]):
                parser.error("--app, --runtime and --output are required")
            result = stage(args.app.resolve(), args.runtime.resolve(), args.output.resolve(), args.target)
        else:
            if not all([args.app, args.output]):
                parser.error("--app and --output are required")
            result = package(args.app.resolve(), args.output.resolve(), args.target, args.appimagetool, args.appimagetool_sha256)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, CoreError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "error": str(error), "installer_built": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
