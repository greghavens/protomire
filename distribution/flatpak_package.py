"""Package an already sealed Linux app; never download runtimes or install it.

Build hosts supply an explicit installed-runtime commit lock. The resulting
unsigned bundle is not a claim of Bazzite, GPU, or clean-machine acceptance.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess

from branding import ASSETS, apply_linux_branding


PERMISSIONS = ["--share=network", "--share=ipc", "--socket=wayland", "--socket=fallback-x11",
               "--socket=pulseaudio", "--device=dri", "--filesystem=home",
               "--talk-name=org.freedesktop.Notifications", "--talk-name=org.freedesktop.secrets"]


def validate_profile(profile):
    required = {"app_id", "branch", "runtime", "sdk", "base", "runtime_version", "commits"}
    if not isinstance(profile, dict) or set(profile) != required:
        raise ValueError("Flatpak profile requires explicit app, runtime, SDK, base and commit locks")
    for key in ("app_id", "runtime", "sdk", "base"):
        if not isinstance(profile[key], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){2,}", profile[key]):
            raise ValueError("Invalid Flatpak identifier: " + key)
    for key in ("branch", "runtime_version"):
        if not isinstance(profile[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", profile[key]):
            raise ValueError("Invalid Flatpak branch/version")
    locks = profile["commits"]
    if not isinstance(locks, dict) or set(locks) != {"runtime", "sdk", "base"} or any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in locks.values()
    ):
        raise ValueError("Flatpak runtimes require exact OSTree commit locks")
    return profile


def package_flatpak(app: Path, output: Path, target: str, artifact: Path, profile: dict, *, tool=None):
    validate_profile(profile)
    arch = {"linux-x64": "x86_64", "linux-arm64": "aarch64"}.get(target)
    if arch is None:
        raise ValueError("Flatpak delivery currently supports native Linux x64/arm64 only")
    executable = tool or shutil.which("flatpak")
    if not executable:
        raise ValueError("Flatpak is not installed on this build host; no installer was built")
    build = output / "flatpak-build"
    repo = output / "flatpak-repo"
    artifact = artifact.with_name(artifact.name + ".flatpak")
    if any(path.exists() for path in (build, repo, artifact)):
        raise ValueError("Flatpak build/repository/artifact exists; select a fresh output directory")

    def run(*args, capture=False):
        return subprocess.run([str(executable), *map(str, args)], check=True, text=True,
                              capture_output=capture, timeout=1800)

    def verify_dependencies():
        for key in ("runtime", "sdk", "base"):
            ref = profile[key] + "//" + profile["runtime_version"]
            actual = run("info", "--arch=" + arch, "--show-commit", ref, capture=True).stdout.strip()
            if actual != profile["commits"][key]:
                raise ValueError("Installed Flatpak " + key + " differs from the reviewed commit lock")

    verify_dependencies()
    run("build-init", "--arch=" + arch, "--base=" + profile["base"],
        "--base-version=" + profile["runtime_version"], build, profile["app_id"],
        profile["sdk"], profile["runtime"], profile["runtime_version"])
    files = build / "files"
    # The reviewed base uses an absolute /app symlink, which is valid inside
    # Flatpak but cannot be dereferenced against the build host's root.
    try:
        run("build", build, "/usr/bin/test", "-x", "/app/bin/zypak-wrapper")
    except subprocess.CalledProcessError as error:
        raise ValueError("Reviewed Electron base lacks zypak-wrapper; no sandbox-disable fallback") from error
    shutil.copytree(app, files / "lib/modelbuilder", symlinks=True)
    apply_linux_branding(files / "lib/modelbuilder/resources/app", profile["app_id"])
    launcher = files / "bin/modelbuilder"
    shutil.copyfile(Path(__file__).resolve().parent / "assets/flatpak-launcher.sh", launcher)
    launcher.chmod(0o755)
    core_root = "/app/lib/modelbuilder/resources/app/extensions/modelbuilder-workbench/resources/core"
    manifest = json.loads((app / "resources/app/extensions/modelbuilder-workbench/resources/core/runtime.json").read_text())
    service_python = manifest["service_python"]
    if not isinstance(service_python, str) or Path(service_python).is_absolute() or ".." in Path(service_python).parts:
        raise ValueError("Flatpak MCP requires a safe bundled service interpreter")
    mcp = files / "bin/modelbuilder-mcp"
    mcp.write_text('#!/bin/sh\nset -eu\n'
                   'if [ "$#" -ne 4 ] || [ "$1" != "--state-dir" ] || [ "$3" != "--project-id" ]; then exit 2; fi\n'
                   + "exec " + shlex.quote(core_root + "/" + service_python) + " -I -B "
                   + shlex.quote(core_root + "/managed_core.py") + " mcp --runtime-root " + shlex.quote(core_root)
                   + ' --state-dir "$2" --project-id "$4"\n')
    mcp.chmod(0o755)
    profile_path = files / "share/modelbuilder/flatpak-profile.json"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(json.dumps(profile, indent=2) + "\n")
    app_id = profile["app_id"]
    desktop = files / "share/applications" / (app_id + ".desktop")
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text("[Desktop Entry]\nType=Application\nName=Protomire\nExec=modelbuilder %F\nIcon=" + app_id +
                       "\nCategories=Development;Science;\nTerminal=false\nStartupWMClass=" + app_id + "\n")
    # Flatpak validates exported raster icons with a 512px maximum. Preserve
    # the approved 1024px/source assets in the repository, outside the export.
    for size in (16, 24, 32, 48, 64, 96, 128, 192, 256, 512):
        icon = files / f"share/icons/hicolor/{size}x{size}/apps" / (app_id + ".png")
        icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ASSETS / f"icons/light-tile/protomire-{size}.png", icon)
    run("build-finish", "--command=modelbuilder", *PERMISSIONS, build)
    verify_dependencies()
    run("build-export", "--arch=" + arch, repo, build, profile["branch"])
    run("build-bundle", "--arch=" + arch, repo, artifact, app_id, profile["branch"])
    if not artifact.is_file() or not artifact.stat().st_size:
        raise ValueError("Flatpak packager produced no nonempty bundle")
    with artifact.open("rb") as stream:
        artifact_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    receipt = {"artifact": str(artifact), "sha256": artifact_digest,
               "target": target, "format": "flatpak", "profile": profile, "permissions": PERMISSIONS,
               "signed": False, "published": False, "native_acceptance": "not_run"}
    (output / "flatpak-build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt
