#!/usr/bin/env python3
"""Native Code OSS build orchestration, with reviewed per-target toolchain pins."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from platform_config import detect_target, validate_target

ROOT = Path(__file__).resolve().parents[1]


def build_plan(root: Path, target: str, environ: dict | None = None) -> dict:
    env = os.environ if environ is None else environ
    target = validate_target(target)
    pin = json.loads((root / "distribution/code_oss_pin.json").read_text())
    artifact = pin["node_artifacts"][target]
    cache = Path(env.get("MODELBUILDER_BUILD_CACHE", str(root / ".cache"))).expanduser().resolve()
    source = (
        Path(env.get("MODELBUILDER_CODE_OSS_SOURCE", str(cache / "code-oss" / target))).expanduser().resolve()
    )
    runtime = Path(env.get("MODELBUILDER_RUNTIME_DIR", str(root / ".runtime"))).expanduser().resolve()
    toolchain = cache / "toolchains" / artifact["archive"].removesuffix(".tar.gz").removesuffix(".zip")
    return {
        "target": target,
        "cache": str(cache),
        "source": str(source),
        "runtime": str(runtime),
        "evidence": str(
            Path(env.get("MODELBUILDER_BUILD_EVIDENCE", str(root / "evidence/gui/distribution" / target)))
            .expanduser()
            .resolve()
        ),
        "toolchain": str(toolchain),
        "archive": artifact["archive"],
        "sha256": artifact["sha256"],
        "node_url": f"https://nodejs.org/dist/v{pin['node_version']}/{artifact['archive']}",
        "package_task": f"vscode-{target}-min",
        "pin": pin,
    }


def extract_archive(archive: Path, destination: Path) -> None:
    """Reject archive traversal before extraction; hashes are verified separately."""
    destination = destination.resolve()

    def contained(name: str) -> bool:
        return (destination / name).resolve().is_relative_to(destination)

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            if any(not contained(name) for name in bundle.namelist()):
                raise ValueError("Unsafe archive path")
            bundle.extractall(destination)
    else:
        with tarfile.open(archive) as bundle:
            if any(not contained(member.name) for member in bundle.getmembers()):
                raise ValueError("Unsafe archive path")
            bundle.extractall(destination, filter="data")


def create_launcher(root: Path, plan: dict) -> Path:
    directory = Path(plan["runtime"]) / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    # Relative references keep a checkout and its default cache relocatable.
    config = {key: os.path.relpath(plan[key], directory) for key in ("source", "toolchain")}
    config["target"] = plan["target"]
    (directory / "modelbuilder-ide.json").write_text(json.dumps(config, indent=2) + "\n")
    shutil.copyfile(root / "distribution/launch_ide.py", directory / "modelbuilder-ide.py")
    if plan["target"].startswith("win32-"):
        path = directory / "modelbuilder-ide.cmd"
        path.write_text(
            '@echo off\r\nif defined MODELBUILDER_PYTHON (\r\n  "%MODELBUILDER_PYTHON%" "%~dp0modelbuilder-ide.py" %*\r\n) else (\r\n  py -3 "%~dp0modelbuilder-ide.py" %*\r\n)\r\n'
        )
    else:
        path = directory / "modelbuilder-ide"
        path.write_text(
            '#!/usr/bin/env bash\nset -euo pipefail\nHERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\nexec "${MODELBUILDER_PYTHON:-python3}" "$HERE/modelbuilder-ide.py" "$@"\n'
        )
        path.chmod(0o755)
    return path


def build(root: Path, plan: dict, mode: str) -> None:
    if sys.version_info < (3, 12):
        raise ValueError("Python 3.12+ is required for safe toolchain extraction")
    if plan["target"] != detect_target():
        raise ValueError("Cross-compilation is not configured; run the build on the selected native host")
    source, cache, toolchain = (Path(plan[key]) for key in ("source", "cache", "toolchain"))
    # Upstream native-module builds do not support paths containing spaces.
    if any(char.isspace() for char in str(source)):
        raise ValueError(
            "Code OSS native build source path must not contain whitespace; set MODELBUILDER_BUILD_CACHE to a suitable directory"
        )
    if plan["target"].startswith("linux-") and (
        platform.libc_ver()[0].lower() == "musl" or any(Path("/lib").glob("ld-musl-*.so*"))
    ):
        raise ValueError(
            "Native Code OSS desktop requires glibc Linux; Alpine/musl is not a supported desktop build host"
        )
    for name in ("git",):
        if not shutil.which(name):
            raise ValueError(f"Required build tool is missing: {name}")
    evidence = Path(plan["evidence"])
    evidence.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    pin = plan["pin"]
    with (evidence / "build-ide.log").open("w") as log:

        def run(args: list[str], cwd: Path = root) -> None:
            print("+ " + " ".join(map(str, args)), flush=True)
            process = subprocess.Popen(
                [str(arg) for arg in args],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
            if process.wait():
                raise subprocess.CalledProcessError(process.returncode, args)

        if not (source / ".git").exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--single-branch",
                    "--branch",
                    pin["tag"],
                    pin["repository"],
                    str(source),
                ]
            )
        actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        if actual != pin["commit"]:
            raise ValueError("Upstream pin mismatch; use a clean build cache")
        windows = plan["target"].startswith("win32-")
        node_bin = toolchain if windows else toolchain / "bin"
        node = node_bin / ("node.exe" if windows else "node")
        if not node.is_file():
            toolchain.parent.mkdir(parents=True, exist_ok=True)
            archive = toolchain.parent / plan["archive"]
            with (
                urllib.request.urlopen(plan["node_url"], timeout=120) as response,
                archive.open("wb") as output,
            ):
                shutil.copyfileobj(response, output)
            with archive.open("rb") as downloaded:
                actual_digest = hashlib.file_digest(downloaded, "sha256").hexdigest()
            if actual_digest != plan["sha256"]:
                raise ValueError("Node archive checksum mismatch")
            extract_archive(archive, toolchain.parent)
        if subprocess.check_output([str(node), "--version"], text=True).strip() != "v" + pin["node_version"]:
            raise ValueError("Node toolchain version mismatch")
        env["PATH"] = str(node_bin) + os.pathsep + env.get("PATH", "")
        env.setdefault("npm_config_cache", str(cache / "npm"))
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(cache / "playwright"))
        env.setdefault("electron_config_cache", str(cache / "electron"))
        env.setdefault("ELECTRON_CACHE", str(cache / "electron"))
        env.setdefault("XDG_CACHE_HOME", str(cache / "xdg"))
        env.setdefault("npm_config_devdir", str(cache / "node-gyp" / plan["target"]))
        env.setdefault("npm_config_jobs", "2")
        env.setdefault("NODE_OPTIONS", "--max-old-space-size=6144")
        # Optional Linux sysroot remains an explicit, host-matching accommodation.
        sysroot = os.environ.get("MODELBUILDER_SYSROOT")
        if sysroot:
            if not plan["target"].startswith("linux-"):
                raise ValueError("MODELBUILDER_SYSROOT is only supported for Linux builds")
            triplet = {
                "linux-x64": "x86_64-linux-gnu",
                "linux-arm64": "aarch64-linux-gnu",
                "linux-armhf": "arm-linux-gnueabihf",
            }[plan["target"]]
            env["PKG_CONFIG_SYSROOT_DIR"] = str(Path(sysroot).resolve())
            env["PKG_CONFIG_PATH"] = os.pathsep.join(
                str(Path(sysroot) / p) for p in (f"usr/lib/{triplet}/pkgconfig", "usr/share/pkgconfig")
            )
        # Calling npm's JS entrypoint avoids Windows .cmd/shell quoting problems.
        npm_cli = toolchain / (
            "node_modules/npm/bin/npm-cli.js" if windows else "lib/node_modules/npm/bin/npm-cli.js"
        )
        npm = [str(node), str(npm_cli)]
        run([sys.executable, str(root / "distribution/apply_overlay.py"), str(source)])
        if mode == "prepare":
            return
        if mode in ("build", "package"):
            run(npm + ["ci", "--no-audit", "--no-fund"], source)
        feature = root / "packages/ide_feature"
        run(npm + ["ci", "--no-audit", "--no-fund"], feature)
        run(npm + ["run", "compile"], feature)
        generated = source / "extensions/modelbuilder-workbench"
        if generated.exists():
            manifest = json.loads((generated / "package.json").read_text())
            if (
                manifest.get("publisher") != "modelbuilder"
                or manifest.get("name") != "modelbuilder-workbench"
            ):
                raise ValueError("Refusing to remove unexpected generated extension")
            shutil.rmtree(generated)
        if env.get("MODELBUILDER_SERIAL_COMPILE") == "1":
            # The exact constituent upstream tasks, serialized for memory-limited hosts.
            run([str(node), str(root / "distribution/compile_serial.cjs"), str(source)], source)
        else:
            run(npm + ["run", "compile"], source)
        if env.get("HTTPS_PROXY") or env.get("HTTP_PROXY"):
            env.update(
                ELECTRON_GET_USE_PROXY="1",
                GLOBAL_AGENT_HTTP_PROXY=env.get("HTTP_PROXY", ""),
                GLOBAL_AGENT_HTTPS_PROXY=env.get("HTTPS_PROXY", ""),
            )
            run([str(node), "-r", "global-agent/bootstrap", "build/lib/electron.js"], source)
        else:
            run(npm + ["run", "electron"], source)
        run(
            [
                sys.executable,
                str(root / "distribution/apply_overlay.py"),
                str(source),
                "--feature",
                str(feature),
            ]
        )
        launcher = create_launcher(root, plan)
        if mode == "package":
            run(npm + ["run", "gulp", plan["package_task"]], source)
        from verify_build import verify

        manifest = verify(source, target=plan["target"])
        (evidence / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"Built development distribution: {launcher}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", nargs="?", choices=("prepare", "build", "compile", "package"), default="build"
    )
    parser.add_argument(
        "--target", help="Native target; overrides MODELBUILDER_TARGET (cross builds are not supported)"
    )
    parser.add_argument(
        "--plan", action="store_true", help="Print resolved configuration without downloads or changes"
    )
    args = parser.parse_args()
    try:
        plan = build_plan(ROOT, args.target or os.environ.get("MODELBUILDER_TARGET") or detect_target())
        if args.plan:
            print(json.dumps(plan, indent=2))
            return
        build(ROOT, plan, args.mode)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(2, f"Build blocked: {error}\n")


if __name__ == "__main__":
    main()
