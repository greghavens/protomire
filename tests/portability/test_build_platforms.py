"""Offline platform routing tests; these are not native build/GUI acceptance."""

from __future__ import annotations
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "distribution"))
from platform_config import TARGETS, detect_target, electron_path  # noqa: E402
from build_ide import build_plan, create_launcher, extract_archive  # noqa: E402
import launch_ide  # noqa: E402


@pytest.mark.parametrize(
    "system,machine,expected",
    [
        ("Linux", "x86_64", "linux-x64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Linux", "armv7l", "linux-armhf"),
        ("Darwin", "arm64", "darwin-arm64"),
        ("Darwin", "x86_64", "darwin-x64"),
        ("Windows", "AMD64", "win32-x64"),
        ("Windows", "ARM64", "win32-arm64"),
    ],
)
def test_host_detection(system, machine, expected):
    assert detect_target(system, machine) == expected


@pytest.mark.parametrize(
    "system,machine", [("FreeBSD", "x86_64"), ("Windows", "i686"), ("Darwin", "armv7l"), ("Linux", "riscv64")]
)
def test_unsupported_hosts_fail_closed(system, machine):
    with pytest.raises(ValueError, match="Unsupported"):
        detect_target(system, machine)


@pytest.mark.parametrize("target", sorted(TARGETS))
def test_plan_matches_pinned_artifact_and_package_target(target, tmp_path):
    plan = build_plan(
        ROOT,
        target,
        {
            "MODELBUILDER_BUILD_CACHE": str(tmp_path / "cache"),
            "MODELBUILDER_RUNTIME_DIR": str(tmp_path / "runtime"),
        },
    )
    assert plan["source"] == str(tmp_path / "cache/code-oss" / target)
    assert plan["package_task"] == f"vscode-{target}-min"
    assert plan["node_url"].endswith(plan["archive"])
    assert len(plan["sha256"]) == 64
    assert not (tmp_path / "cache").exists()  # planning has no side effects


def test_build_entrypoint_plan_runs_without_downloads(tmp_path):
    env = dict(os.environ, MODELBUILDER_BUILD_CACHE=str(tmp_path / "cache"))
    result = subprocess.run(
        [sys.executable, str(ROOT / "distribution/build_ide.py"), "--plan", "--target", "darwin-arm64"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["target"] == "darwin-arm64"
    assert not (tmp_path / "cache").exists()


def test_cross_build_rejected_before_download(tmp_path):
    target = "darwin-arm64" if detect_target() != "darwin-arm64" else "linux-x64"
    env = dict(os.environ, MODELBUILDER_BUILD_CACHE=str(tmp_path / "cache"))
    result = subprocess.run(
        [sys.executable, str(ROOT / "distribution/build_ide.py"), "--target", target],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Cross-compilation" in result.stderr
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("target", sorted(TARGETS))
@pytest.mark.parametrize(
    "args,cli",
    [(["space in project"], False), (["--install-extension=a b.vsix"], True), (["--version"], True)],
)
def test_generated_launcher_uses_native_binary_and_preserves_args(target, args, cli, tmp_path, monkeypatch):
    source = tmp_path / "source with spaces"
    source.mkdir()
    product = {
        "applicationName": "modelbuilder-ide",
        "nameShort": "Modelbuilder",
        "nameLong": "Modelbuilder IDE",
    }
    (source / "product.json").write_text(json.dumps(product))
    binary = electron_path(source, product, target)
    binary.parent.mkdir(parents=True)
    binary.touch()
    plan = {
        "runtime": str(tmp_path / "runtime with spaces"),
        "source": str(source),
        "toolchain": str(tmp_path / "toolchain"),
        "target": target,
    }
    launcher = create_launcher(ROOT, plan)
    calls = []
    monkeypatch.setattr(launch_ide.subprocess, "call", lambda command, **kw: calls.append((command, kw)) or 0)
    assert launch_ide.launch(launcher.parent, args) == 0
    command, options = calls[0]
    assert command[0] == str(binary)
    assert command[-len(args) :] == args
    assert options["cwd"] == source
    assert (options["env"].get("ELECTRON_RUN_AS_NODE") == "1") is cli
    assert launcher.suffix == (".cmd" if target.startswith("win32") else "")
    assert not Path(
        json.loads((launcher.parent / "modelbuilder-ide.json").read_text())["source"]
    ).is_absolute()


def test_extension_tests_are_not_disabled(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    product = {"applicationName": "modelbuilder-ide"}
    (source / "product.json").write_text(json.dumps(product))
    binary = source / ".build/electron/modelbuilder-ide"
    binary.parent.mkdir(parents=True)
    binary.touch()
    launcher = create_launcher(
        ROOT,
        {
            "runtime": str(tmp_path / "runtime"),
            "source": str(source),
            "toolchain": str(tmp_path / "node"),
            "target": "linux-x64",
        },
    )
    calls = []
    monkeypatch.setattr(launch_ide.subprocess, "call", lambda command, **kw: calls.append(command) or 0)
    launch_ide.launch(launcher.parent, ["--extensionTestsPath", "tests with spaces"])
    assert not any("disable-extension" in arg for arg in calls[0])


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_archive_traversal_rejected(kind, tmp_path):
    archive = tmp_path / ("archive." + kind)
    if kind == "zip":
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("../outside", "bad")
    else:
        with tarfile.open(archive, "w") as bundle:
            member = tarfile.TarInfo("../outside")
            member.size = 3
            bundle.addfile(member, io.BytesIO(b"bad"))
    with pytest.raises(ValueError, match="Unsafe archive"):
        extract_archive(archive, tmp_path / "destination")
    assert not (tmp_path / "outside").exists()


def test_wrapper_has_no_bash4_or_gnu_dependency():
    script = (ROOT / "scripts/build_ide.sh").read_text()
    for forbidden in ("readarray", "mapfile", "sha256sum", "readlink -f", "declare -A"):
        assert forbidden not in script
    subprocess.run(["bash", "-n", str(ROOT / "scripts/build_ide.sh")], check=True)


@pytest.mark.parametrize("target", sorted(TARGETS))
@pytest.mark.parametrize("mode", ["prepare", "build", "compile", "package"])
def test_build_orchestration_all_native_targets(target, mode, tmp_path, monkeypatch):
    """Mock external build commands; test routing without claiming native execution."""
    import build_ide
    import verify_build

    plan = build_plan(
        ROOT,
        target,
        {
            "MODELBUILDER_BUILD_CACHE": str(tmp_path / "cache"),
            "MODELBUILDER_RUNTIME_DIR": str(tmp_path / "runtime"),
            "MODELBUILDER_BUILD_EVIDENCE": str(tmp_path / "evidence"),
        },
    )
    source = Path(plan["source"])
    (source / ".git").mkdir(parents=True)
    toolchain = Path(plan["toolchain"])
    node = toolchain / ("node.exe" if target.startswith("win32-") else "bin/node")
    node.parent.mkdir(parents=True)
    node.touch()
    monkeypatch.setattr(build_ide, "detect_target", lambda: target)
    monkeypatch.setattr(
        build_ide.subprocess,
        "check_output",
        lambda command, **kw: plan["pin"]["commit"]
        if command[0] == "git"
        else "v" + plan["pin"]["node_version"],
    )
    monkeypatch.setattr(
        verify_build, "verify", lambda source, target: {"target": target, "gui_status": "UNVERIFIED"}
    )
    commands = []

    class Process:
        def __init__(self, command, **kw):
            commands.append((command, kw))
            self.stdout = iter(["mock output\n"])
            self.returncode = 0

        def wait(self):
            return 0

    monkeypatch.setattr(build_ide.subprocess, "Popen", Process)
    build_ide.build(ROOT, plan, mode)
    flattened = [command for command, _ in commands]
    package = [c for c in flattened if "gulp" in c]
    assert len(package) == (1 if mode == "package" else 0)
    if package:
        assert package[0][-1] == "vscode-" + target + "-min"
    manifest = Path(plan["evidence"]) / "build-manifest.json"
    assert manifest.exists() == (mode != "prepare")
    if manifest.exists():
        assert json.loads(manifest.read_text())["target"] == target
    for command, _ in commands:
        if command[0] == str(node) and "npm-cli.js" in command[1]:
            assert command[1].endswith("node_modules/npm/bin/npm-cli.js")


@pytest.mark.parametrize("target", sorted(TARGETS))
def test_gui_display_and_python_paths_are_host_specific(target):
    import smoke_ide

    assert smoke_ide.needs_display_environment(target) == target.startswith("linux-")
    executable = smoke_ide.python_in(Path("/venv"), target)
    assert executable.as_posix() == (
        "/venv/Scripts/python.exe" if target.startswith("win32-") else "/venv/bin/python"
    )


def test_gui_without_linux_display_records_blocked(tmp_path):
    if not detect_target().startswith("linux-"):
        pytest.skip("Runs actual Linux headless guard only on Linux")
    env = {key: value for key, value in os.environ.items() if key not in ("DISPLAY", "WAYLAND_DISPLAY")}
    env["MODELBUILDER_GUI_EVIDENCE"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, str(ROOT / "distribution/smoke_ide.py")], env=env, capture_output=True, text=True
    )
    assert result.returncode == 2
    assert json.loads((tmp_path / "smoke-results.json").read_text())["status"] == "BLOCKED"
