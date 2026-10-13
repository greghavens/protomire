"""Isolated MLX worker metadata/allocation probe; no user source is imported."""

import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path
import re
import sys

PROFILE_SHA256 = "f408bef35d4c867e2b634ac066cc73a4197bc60214dfd1f22cb57683fb2b68bb"


def inspect(device, allocate=False):
    profile = Path(__file__).with_name("mlx-darwin-arm64.lock")
    if hashlib.sha256(profile.read_bytes()).hexdigest() != PROFILE_SHA256:
        raise ValueError("MLX profile bytes changed")
    versions = {}
    for line in profile.read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^ \\]+)", line)
        if match:
            name, expected = match.groups()
            actual = importlib.metadata.version(name)
            if actual != expected:
                raise ValueError("MLX dependency differs from reviewed profile: " + name)
            versions[name] = actual
    if platform.python_version() != "3.12.14" or sys.platform != "darwin" or platform.machine() != "arm64":
        raise ValueError("Reviewed MLX profile requires Python 3.12.14 on macOS ARM64")
    import mlx.core as mx

    if device not in {"mlx-cpu", "mlx-metal"} or device == "mlx-metal" and not mx.metal.is_available():
        raise ValueError("Explicit selected MLX device is unavailable")
    selected = mx.gpu if device == "mlx-metal" else mx.cpu
    mx.set_default_device(selected)
    if allocate:
        x = mx.array([2.0, 3.0])
        value, gradient = mx.value_and_grad(lambda data: mx.sum(data * data))(x)
        mx.eval(value, gradient)
        if value.item() != 13 or gradient.tolist() != [4, 6]:
            raise ValueError("MLX forward/backward verification failed")
    return {
        "available": True,
        "device": device,
        "framework": "mlx",
        "platform": sys.platform,
        "machine": platform.machine(),
        "python": platform.python_version(),
        "packages": versions,
        "profile_sha256": PROFILE_SHA256,
        "allocation_verified": allocate,
        "metal_available": mx.metal.is_available(),
        "device_info": mx.metal.device_info() if device == "mlx-metal" else {"device": "cpu"},
    }


if __name__ == "__main__":
    request = json.load(sys.stdin)
    try:
        print(json.dumps(inspect(request["device"], request.get("allocate", False))))
    except Exception as error:
        print(
            json.dumps(
                {
                    "available": False,
                    "device": request["device"],
                    "framework": "mlx",
                    "reason": str(error)[:1000],
                }
            )
        )
