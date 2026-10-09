#!/usr/bin/env python3
"""Generated development-launcher payload; keeps arguments and paths portable."""

from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path


def launch_command(directory: Path, args: list[str]) -> tuple[list[str], Path, dict]:
    config = json.loads((directory / "modelbuilder-ide.json").read_text())
    source = (directory / config["source"]).resolve()
    toolchain = (directory / config["toolchain"]).resolve()
    target = config["target"]
    product = json.loads((source / "product.json").read_text())
    binary = source / ".build/electron"
    if target.startswith("darwin-"):
        binary /= f"{product['nameLong']}.app/Contents/MacOS/Electron"
    elif target.startswith("win32-"):
        binary /= product["nameShort"] + ".exe"
    else:
        binary /= product["applicationName"]
    if not binary.is_file():
        raise ValueError(f"Missing Electron executable: {binary}; rebuild this native target")
    env = os.environ.copy()
    env["PATH"] = (
        str(toolchain if target.startswith("win32-") else toolchain / "bin")
        + os.pathsep
        + env.get("PATH", "")
    )
    env["MODELBUILDER_IDE_CLI"] = str(directory / ("modelbuilder-ide.cmd" if target.startswith("win32-") else "modelbuilder-ide"))
    env["MODELBUILDER_IDE_LAUNCHER"] = str(directory / "modelbuilder-ide.py")
    env["MODELBUILDER_IDE_PYTHON"] = sys.executable
    args = list(args)
    for flag, variable in (("--extensions-dir", "MODELBUILDER_EXTENSIONS_DIR"),
                           ("--user-data-dir", "MODELBUILDER_USER_DATA_DIR")):
        explicit = None
        for index, argument in enumerate(args):
            if argument == flag and index + 1 < len(args):
                explicit = args[index + 1]
            elif argument.startswith(flag + "="):
                explicit = argument.split("=", 1)[1]
        if explicit is not None:
            path = Path(explicit).expanduser()
            env[variable] = str((source / path).resolve() if not path.is_absolute() else path.resolve())
        elif env.get(variable):
            path = Path(env[variable]).expanduser().resolve()
            env[variable] = str(path)
            args += [flag, str(path)]
    env.update(
        NODE_ENV="development",
        VSCODE_DEV="1",
        VSCODE_CLI="1",
        VSCODE_SKIP_PRELAUNCH="1",
        ELECTRON_ENABLE_LOGGING="1",
        ELECTRON_ENABLE_STACK_DUMPING="1",
    )
    cli = any(
        arg.split("=", 1)[0]
        in {
            "--install-extension",
            "--uninstall-extension",
            "--list-extensions",
            "--version",
            "--locate-extension",
        }
        for arg in args
    )
    if cli:
        env["ELECTRON_RUN_AS_NODE"] = "1"
        command = [str(binary), str(source / "out/cli.js"), "."]
    else:
        env.pop("ELECTRON_RUN_AS_NODE", None)
        command = [str(binary), "."]
    if not any(arg.split("=", 1)[0] == "--extensionTestsPath" for arg in args):
        command.append("--disable-extension=vscode.vscode-api-tests")
    return command + args, source, env


def launch(directory: Path, args: list[str]) -> int:
    command, source, env = launch_command(directory, args)
    return subprocess.call(command, cwd=source, env=env)


if __name__ == "__main__":
    try:
        raise SystemExit(launch(Path(__file__).resolve().parent, sys.argv[1:]))
    except (ValueError, OSError) as error:
        raise SystemExit(f"Launch blocked: {error}")
