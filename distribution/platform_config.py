"""Native desktop target names shared by build and extension installation.

These are the targets provided by the pinned upstream Code OSS build. Detection
is not a claim that a target's native GUI has passed Modelbuilder acceptance.
"""

from __future__ import annotations

import platform
from pathlib import Path

TARGETS = frozenset(
    {
        "linux-x64",
        "linux-arm64",
        "linux-armhf",
        "darwin-x64",
        "darwin-arm64",
        "win32-x64",
        "win32-arm64",
    }
)


def validate_target(target: str) -> str:
    if target not in TARGETS:
        raise ValueError(f"Unsupported native desktop target {target!r}; choose {', '.join(sorted(TARGETS))}")
    return target


def detect_target(system: str | None = None, machine: str | None = None) -> str:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    operating_system = {"linux": "linux", "darwin": "darwin", "windows": "win32", "win32": "win32"}.get(
        system
    )
    architecture = {
        "x86_64": "x64",
        "amd64": "x64",
        "x64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "armhf",
        "armv7": "armhf",
        "armhf": "armhf",
    }.get(machine)
    if not operating_system or not architecture:
        raise ValueError(f"Unsupported host {system}/{machine}")
    return validate_target(f"{operating_system}-{architecture}")


def electron_path(source: Path, product: dict, target: str) -> Path:
    validate_target(target)
    root = source / ".build" / "electron"
    if target.startswith("darwin-"):
        return root / f"{product['nameLong']}.app" / "Contents" / "MacOS" / "Electron"
    if target.startswith("win32-"):
        return root / f"{product['nameShort']}.exe"
    return root / product["applicationName"]
