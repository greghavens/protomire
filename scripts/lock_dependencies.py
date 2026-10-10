"""Regenerate exact Python 3.12 dependency profiles using pinned uv and existing constraints."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.dependency_profiles import BACKEND_TARGETS, CPU_INDEX, PARQUET_TARGETS, UV_PLATFORMS  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=sorted(UV_PLATFORMS), action='append')
    parser.add_argument('--component', choices=['service', 'backend', 'parquet'], action='append')
    args = parser.parse_args()
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
            source = f'requirements/{component}.in'
            if component == 'backend' and target == 'darwin-arm64':
                source = 'requirements/backend-darwin-arm64.in'
            command = ['uv', 'pip', 'compile', source, '--python-version',
                       (ROOT / '.python-version').read_text().strip(), '--python-platform', UV_PLATFORMS[target],
                       '--generate-hashes', '--no-annotate', '--custom-compile-command',
                       f'python scripts/lock_dependencies.py --target {target} --component {component}',
                       '-o', f'requirements/profiles/{component}-{target}.lock']
            if component == 'parquet':
                command += ['--only-binary', ':all:']
            if component == 'backend':
                command += ['-c', 'requirements/backend.constraints']
                if target.startswith('linux-'):
                    command += ['--extra-index-url', CPU_INDEX, '--index-strategy', 'unsafe-best-match']
            subprocess.run(command, check=True, cwd=ROOT, env=env, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
