"""Regenerate exact Python 3.12 dependency profiles using pinned uv and existing constraints."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.dependency_profiles import (  # noqa: E402
    BACKEND_TARGETS, BACKEND_VARIANTS, CPU_INDEX, CUDA_INDEX, CUDA_TARGETS,
    PARQUET_TARGETS, UV_PLATFORMS,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=sorted(UV_PLATFORMS), action='append')
    parser.add_argument('--component', choices=['service', 'backend', 'parquet'], action='append')
    parser.add_argument('--backend-variant', choices=BACKEND_VARIANTS, default='default')
    args = parser.parse_args()
    if args.backend_variant != 'default' and args.component != ['backend']:
        parser.error('--backend-variant requires --component backend')
    if args.backend_variant == 'cuda-cu128' and args.target and any(target not in CUDA_TARGETS for target in args.target):
        parser.error('CUDA 12.8 profiles support Linux x64/ARM64 only')
    env = dict(os.environ)
    env.setdefault('UV_CACHE_DIR', str(ROOT / '.cache/uv'))
    version = subprocess.check_output(['uv', '--version'], text=True, env=env).split()[1]
    if version != '0.12.23':
        raise SystemExit('Lock generation requires uv 0.12.23 from requirements/service.lock.')
    destination = ROOT / 'requirements/profiles'
    destination.mkdir(parents=True, exist_ok=True)
    for target in args.target or sorted(UV_PLATFORMS):
        for component in args.component or ['service', 'backend', 'parquet']:
            if component == 'backend' and target not in BACKEND_TARGETS:
                continue
            if component == 'parquet' and target not in PARQUET_TARGETS:
                continue
            if component == 'backend' and args.backend_variant == 'cuda-cu128' and target not in CUDA_TARGETS:
                continue
            cuda = component == 'backend' and args.backend_variant == 'cuda-cu128'
            source = 'requirements/backend-cuda-cu128.in' if cuda else f'requirements/{component}.in'
            lock_component = 'backend-cuda-cu128' if cuda else component
            variant_command = ' --backend-variant cuda-cu128' if cuda else ''
            if component == 'backend' and target == 'darwin-arm64':
                source = 'requirements/backend-darwin-arm64.in'
            command = ['uv', 'pip', 'compile', source, '--python-version',
                       (ROOT / '.python-version').read_text().strip(), '--python-platform', UV_PLATFORMS[target],
                       '--generate-hashes', '--no-annotate', '--custom-compile-command',
                       f'python scripts/lock_dependencies.py --target {target} --component {component}{variant_command}',
                       '-o', f'requirements/profiles/{lock_component}-{target}.lock']
            if component == 'parquet' or cuda:
                command += ['--only-binary', ':all:']
            if component == 'backend':
                command += ['-c', 'requirements/backend.constraints']
                if target.startswith('linux-'):
                    command += ['--extra-index-url', CUDA_INDEX if cuda else CPU_INDEX, '--index-strategy', 'unsafe-best-match']
            subprocess.run(command, check=True, cwd=ROOT, env=env, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
