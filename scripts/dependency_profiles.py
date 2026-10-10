"""Pinned dependency capabilities; desktop availability is independent of backend support."""
from __future__ import annotations

from pathlib import Path

UV_PLATFORMS = {
    'linux-x64': 'x86_64-manylinux_2_28',
    'linux-arm64': 'aarch64-manylinux_2_28',
    'darwin-x64': 'x86_64-apple-darwin',
    'darwin-arm64': 'aarch64-apple-darwin',
    'win32-x64': 'x86_64-pc-windows-msvc',
    'win32-arm64': 'aarch64-pc-windows-msvc',
}
BACKEND_TARGETS = frozenset({'linux-x64', 'linux-arm64', 'darwin-arm64'})
PARQUET_TARGETS = frozenset({'linux-x64', 'linux-arm64', 'darwin-x64', 'darwin-arm64', 'win32-x64'})
CPU_INDEX = 'https://download.pytorch.org/whl/cpu'


def backend_reason(target: str) -> str | None:
    if target in BACKEND_TARGETS:
        return None
    if target.startswith('win32-'):
        return ('ZenML 0.97.0 does not support native Windows. Install the service/MCP and IDE '
                'natively; run the complete service and backend inside WSL2 or on a supported '
                'Linux/macOS host and connect the IDE to that service. WSL is not a native backend.')
    if target == 'darwin-x64':
        return 'PyTorch 2.9.0 has no official macOS Intel wheel; use a supported backend host.'
    if target == 'linux-armhf':
        return 'PyTorch 2.9.0 has no official Linux armhf wheel; use a supported backend host.'
    return f'No reviewed backend dependency profile for {target}.'


def lock_path(root: Path, component: str, target: str) -> Path:
    if component not in {'service', 'backend', 'parquet'}:
        raise ValueError(f'Unknown dependency component: {component}')
    if component == 'parquet' and target not in PARQUET_TARGETS:
        raise ValueError(f'No official pinned PyArrow CPython 3.12 wheel profile for {target}.')
    if component == 'backend' and (reason := backend_reason(target)):
        raise ValueError(reason)
    if target not in UV_PLATFORMS and target != 'linux-armhf':
        raise ValueError(f'No dependency profile for {target}')
    # The Linux service closure has no architecture markers. armhf uses the same
    # exact packages and source hashes; native build prerequisites may be needed.
    lock_target = 'linux-x64' if target == 'linux-armhf' else target
    return root / 'requirements' / 'profiles' / f'{component}-{lock_target}.lock'


def venv_python(directory: Path, target: str) -> Path:
    return directory / ('Scripts/python.exe' if target.startswith('win32-') else 'bin/python')
