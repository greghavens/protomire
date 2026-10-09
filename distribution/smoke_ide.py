#!/usr/bin/env python3
"""Run actual desktop GUI acceptance without Unix-only toolchain assumptions."""

from __future__ import annotations
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from build_ide import build_plan
from launch_ide import launch_command
from platform_config import detect_target

ROOT = Path(__file__).resolve().parents[1]


def python_in(venv: Path, target: str) -> Path:
    return venv / ("Scripts/python.exe" if target.startswith("win32-") else "bin/python")


def needs_display_environment(target: str) -> bool:
    return target.startswith("linux-")


def stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    suites = parser.add_mutually_exclusive_group()
    suites.add_argument("--workbench", action="store_true")
    suites.add_argument("--execution", action="store_true")
    suites.add_argument("--launch", action="store_true")
    parser.add_argument(
        "--timeout", type=float, default=float(os.environ.get("MODELBUILDER_GUI_TIMEOUT", "600"))
    )
    args = parser.parse_args()
    try:
        debug_port = int(os.environ.get("MODELBUILDER_GUI_DEBUG_PORT", "9222"))
        if not 1 <= debug_port <= 65535:
            raise ValueError
    except ValueError:
        parser.error("MODELBUILDER_GUI_DEBUG_PORT must be an integer from 1 to 65535")
    target = detect_target()
    suite = (
        "workbench"
        if args.workbench
        else "execution"
        if args.execution
        else os.environ.get("MODELBUILDER_GUI_SUITE", "smoke")
    )
    reports = {
        "smoke": "smoke-results.json",
        "workbench": "workbench-host-results.json",
        "execution": "gui-execution-host-results.json",
    }
    if suite not in reports or args.timeout <= 0:
        parser.error("Unsupported GUI suite or non-positive timeout")
    plan = build_plan(ROOT, target)
    runtime = Path(plan["runtime"])
    evidence = Path(
        os.environ.get("MODELBUILDER_GUI_EVIDENCE", str(Path(plan["evidence"]) / suite))
    ).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    report = evidence / reports[suite]
    for name in (reports[suite], "runner-error.json", "stage.txt"):
        (evidence / name).unlink(missing_ok=True)

    def blocked(reason: str) -> int:
        report.write_text(json.dumps({"status": "BLOCKED", "reason": reason, "tests": []}, indent=2) + "\n")
        print(f"BLOCKED: {reason}")
        return 2

    if needs_display_environment(target) and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return blocked(
            "No Linux GUI display. Use a desktop terminal or Xvfb with normal sandbox prerequisites."
        )
    python = Path(os.environ.get("MODELBUILDER_GUI_PYTHON", str(python_in(ROOT / ".venv", target))))
    if not python.is_file():
        return blocked(
            "Set MODELBUILDER_GUI_PYTHON to the service Python environment with ipykernel installed."
        )
    if subprocess.run([str(python), "-c", "import ipykernel"], capture_output=True).returncode:
        return blocked("The selected Python environment needs the pinned ipykernel dependency.")
    backend = Path(
        os.environ.get("MODELBUILDER_BACKEND_PYTHON", str(python_in(ROOT / ".venv-backend", target)))
    )
    if suite == "execution" and not backend.is_file():
        return blocked(
            "The native backend profile must be supported and bootstrapped before the execution suite."
        )
    node = Path(
        os.environ.get(
            "MODELBUILDER_GUI_NODE",
            str(Path(plan["toolchain"]) / ("node.exe" if target.startswith("win32-") else "bin/node")),
        )
    )
    if not node.is_file():
        return blocked("Pinned GUI Node toolchain is absent; build this native IDE target first.")
    workspace = Path(os.environ.get("MODELBUILDER_GUI_WORKSPACE", str(runtime / "gui-workspace"))).resolve()
    user_data = Path(os.environ.get("MODELBUILDER_GUI_USER_DATA", str(runtime / "gui-user-data"))).resolve()
    extensions = Path(os.environ.get("MODELBUILDER_EXTENSIONS_DIR", str(runtime / "extensions"))).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    (user_data / "User").mkdir(parents=True, exist_ok=True)
    if not (workspace / ".git").exists():
        subprocess.run(["git", "-C", str(workspace), "init"], check=True)
    settings = {
        "telemetry.telemetryLevel": "off",
        "workbench.startupEditor": "none",
        "chat.disableAIFeatures": True,
        "terminal.integrated.enablePersistentSessions": False,
        "python.experiments.enabled": False,
        "extensions.autoUpdate": False,
        "security.workspace.trust.enabled": True,
    }
    (user_data / "User/settings.json").write_text(json.dumps(settings) + "\n")
    launch_args = ["--user-data-dir", str(user_data), "--extensions-dir", str(extensions)]
    if not args.launch:
        launch_args += [
            "--disable-extension",
            "openai.chatgpt",
            "--disable-extension",
            "Anthropic.claude-code",
            f"--extensionDevelopmentPath={ROOT / 'tests/gui'}",
            f"--remote-debugging-port={debug_port}",
        ]
    launch_args += [str(workspace)]
    try:
        custom_cli = os.environ.get("MODELBUILDER_IDE")
        if custom_cli:
            cli = Path(custom_cli).resolve()
            command = ([sys.executable, str(cli)] if cli.suffix == ".py" else [str(cli)]) + launch_args
            cwd, env = ROOT, os.environ.copy()
        else:
            command, cwd, env = launch_command(runtime / "bin", launch_args)
    except (OSError, ValueError) as error:
        return blocked(f"Branded editor is absent or invalid: {error}; run the native build first.")
    env.update(
        MODELBUILDER_GUI_EVIDENCE=str(evidence),
        MODELBUILDER_GUI_SMOKE_RUN="1",
        MODELBUILDER_GUI_SUITE=suite,
        MODELBUILDER_GUI_NODE=str(node),
        MODELBUILDER_GUI_PYTHON=str(python),
        MODELBUILDER_BACKEND_PYTHON=str(backend),
    )
    with (evidence / "extension-install.log").open("w") as log:
        installed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "distribution/install_extensions.py"),
                "--extensions-dir",
                str(extensions),
                "--user-data-dir",
                str(user_data),
            ]
            + (["--cli", custom_cli] if custom_cli else []),
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
        )
    if installed.returncode:
        return blocked("Extension installation failed; inspect extension-install.log.")
    if args.launch:
        return subprocess.call(command, cwd=cwd, env=env)
    helper = None
    with (
        (evidence / "launch.log").open("w") as log,
        (evidence / "fixture-trust.log").open("w") as trust_log,
        (evidence / "screenshot.log").open("w") as capture_log,
    ):
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=os.name != "nt"
        )
        try:
            if os.environ.get("MODELBUILDER_GUI_APPROVE_FIXTURE_TRUST") == "1":
                helper = subprocess.Popen(
                    [str(node), str(ROOT / "tests/gui/trust_fixture.cjs"), str(workspace)],
                    env=env,
                    stdout=trust_log,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                )
            started, last = time.monotonic(), ""
            while process.poll() is None:
                if time.monotonic() - started > args.timeout:
                    stop(process)
                    return blocked(
                        "Actual GUI suite timed out; inspect launch.log and native host prerequisites."
                    )
                stage_path = evidence / "stage.txt"
                stage = stage_path.read_text().strip() if stage_path.exists() else ""
                if stage and stage != last:
                    # Stage names are test output, never an arbitrary path.
                    name = Path(stage).name
                    subprocess.run(
                        [
                            str(node),
                            str(ROOT / "tests/gui/capture_stage.cjs"),
                            str(evidence / (name + ".png")),
                        ],
                        env=env,
                        stdout=capture_log,
                        stderr=subprocess.STDOUT,
                        timeout=30,
                        check=False,
                    )
                    last = stage
                time.sleep(1)
        finally:
            stop(process)
            if helper is not None:
                stop(helper)
    if process.returncode:
        print("Real GUI process failed; inspect launch.log")
        return 1
    if not report.is_file():
        return blocked("GUI exited without an acceptance report; no pass is inferred.")
    result = json.loads(report.read_text())
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
