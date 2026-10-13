"""Typed JSON-lines IPC worker; inherits the native supervisor process group."""

from __future__ import annotations
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

component_path = Path(__file__).with_name("mlx_components.py")
component_spec = importlib.util.spec_from_file_location("_modelbuilder_mlx_components", component_path)
components = importlib.util.module_from_spec(component_spec)
component_spec.loader.exec_module(components)


def emit(record):
    print(json.dumps(record, allow_nan=False), file=sys.__stdout__, flush=True)


def check_profile(profile):
    # Every resolved dependency, not just the four primary frameworks, is pinned.
    expected = {}
    for line in profile.read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^ \\]+)", line)
        if match:
            expected[match[1]] = match[2]
    if not expected or any(importlib.metadata.version(name) != version for name, version in expected.items()):
        raise ValueError("Installed MLX worker profile mismatch")
    return expected


def main():
    request = json.loads(sys.stdin.readline(4 * 1024 * 1024))
    if request.get("protocol") != "modelbuilder.mlx-ipc/v1" or request["operation"] not in {
        "train",
        "evaluate",
        "generate",
        "export",
    }:
        raise ValueError("Unsupported MLX IPC request")
    profile = Path(__file__).parent / "mlx-darwin-arm64.lock"
    if components.digest(profile) != request["profile_sha256"]:
        raise ValueError("MLX profile hash mismatch")
    check_profile(profile)
    context = request["context"]
    output = Path(context["output_dir"]).resolve()

    def checkpoint_ready(**event):
        path = Path(event["path"]).resolve(strict=True)
        if not path.is_relative_to(output):
            raise ValueError("Checkpoint outside worker artifact root")
        components.checkpoint(path)
        emit({"type": "checkpoint", "path": str(path), "step": event["step"], "purpose": event["purpose"]})

    context["checkpoint_ready"] = checkpoint_ready
    inputs = request["inputs"]
    for name in ["trained"]:
        if name in inputs:
            inputs[name] = Path(inputs[name])
    # Native framework progress output belongs to logs, never the IPC channel.
    sys.stdout = sys.stderr
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        result = getattr(components, request["operation"])(
            inputs=inputs, parameters=request["parameters"], context=context
        )
        emit(
            {
                "type": "result",
                "outputs": {
                    name: str(value) if isinstance(value, Path) else value for name, value in result.items()
                },
            }
        )
    except components.Cancelled as error:
        emit({"type": "cancelled", "message": str(error)})
        return 130
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        emit({"type": "error", "message": str(error)})
        raise SystemExit(1)
