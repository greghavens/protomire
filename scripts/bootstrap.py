"""Portable bootstrap. Python is already installed; no credentials or remote compute are provisioned."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from distribution.platform_config import detect_target  # noqa: E402
from scripts.dependency_profiles import (  # noqa: E402
    BACKEND_VARIANTS, CPU_INDEX, CUDA_INDEX, PARQUET_TARGETS,
    backend_reason, lock_path, venv_python,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--backend', action='store_true', help='Also install the supported local backend')
    result.add_argument('--backend-only', action='store_true')
    result.add_argument('--backend-variant', choices=BACKEND_VARIANTS, default='default',
                        help='Explicit backend wheel profile; CUDA installs runtime libraries, never a driver')
    result.add_argument('--parquet', action='store_true',
                        help='Add the hash-locked PyArrow adapter to the selected service/backend environments')
    result.add_argument('--trainer', action='store_true',
                        help='Add the optional hash-locked Transformers Trainer adapter to the backend only')
    result.add_argument('--tracking', action='store_true', help='Install isolated optional W&B SDK; never enable uploads')
    result.add_argument('--tracking-venv', type=Path, default=Path(os.environ.get('MODELBUILDER_TRACKING_VENV', ROOT / '.venv-tracking')))
    result.add_argument('--skip-frontend', action='store_true', help='Install Python components only')
    result.add_argument('--skip-model-download', action='store_true', help='Install backend; defer pinned fixture download')
    result.add_argument('--plan', action='store_true', help='Print detected paths/capabilities without installation')
    result.add_argument('--service-venv', type=Path, default=Path(os.environ.get('MODELBUILDER_SERVICE_VENV', ROOT / '.venv')))
    result.add_argument('--backend-venv', type=Path, default=Path(os.environ.get('MODELBUILDER_BACKEND_VENV', ROOT / '.venv-backend')))
    return result


def run(command: list[str | Path], *, env: dict[str, str], cwd: Path = ROOT) -> None:
    subprocess.run([str(arg) for arg in command], check=True, cwd=cwd, env=env)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    target = detect_target()
    pin = (ROOT / '.python-version').read_text().strip()
    service = args.service_venv.expanduser().resolve()
    backend = args.backend_venv.expanduser().resolve()
    if service == backend:
        raise ValueError('Service and backend environments must remain separate.')
    tracking = args.tracking_venv.expanduser().resolve()
    if tracking in {service, backend}:
        raise ValueError('Optional tracking must use a separate environment.')
    wants_backend = args.backend or args.backend_only
    if args.backend_variant != "default" and not wants_backend:
        raise ValueError("--backend-variant requires --backend or --backend-only.")
    if args.trainer and not wants_backend:
        raise ValueError("--trainer requires --backend or --backend-only.")
    reason = backend_reason(target, args.backend_variant)
    trainer_lock = ROOT / "requirements/profiles/trainer.lock"
    backend_lock = None if reason else lock_path(ROOT, "backend", target, backend_variant=args.backend_variant)
    parquet_wheel = target in PARQUET_TARGETS
    parquet_runtime = parquet_wheel and target.startswith('linux-')
    parquet_unavailable = (None if parquet_runtime else
        'Contained Parquet decoding is currently Linux-only; use JSONL/CSV or a supported Linux service/backend.'
        if parquet_wheel else 'No reviewed optional PyArrow wheel profile is available for this target; use JSONL/CSV.')
    plan = {'target': target, 'python': pin, 'service_python': str(venv_python(service, target)),
            'backend_python': str(venv_python(backend, target)),
            'service_lock': str(lock_path(ROOT, 'service', target)), 'backend_unavailable': reason,
            'backend_variant': args.backend_variant, 'backend_lock': str(backend_lock) if backend_lock else None,
            'trainer_available': reason is None and trainer_lock.is_file(), 'trainer_lock': str(trainer_lock),
            'tracking_python': str(venv_python(tracking, target)),
            'parquet_available': parquet_runtime,
            'parquet_wheel_available': parquet_wheel,
            'parquet_unavailable': parquet_unavailable,
            'parquet_lock': str(lock_path(ROOT, 'parquet', target)) if parquet_wheel else None,
            'tracking_available': (ROOT / 'requirements/profiles' / f'tracking-wandb-{target}.lock').is_file()}
    if args.plan:
        print(json.dumps(plan, indent=2))
        return 0
    if '.'.join(map(str, sys.version_info[:3])) != pin:
        raise ValueError(f'Pinned Python {pin} required; run this script with that interpreter.')
    parquet_lock = lock_path(ROOT, 'parquet', target) if args.parquet else None
    env = dict(os.environ)
    env.setdefault('PIP_CACHE_DIR', str(ROOT / '.cache' / 'pip'))
    env.setdefault('UV_CACHE_DIR', str(ROOT / '.cache' / 'uv'))
    if not args.backend_only:
        run([sys.executable, '-m', 'venv', service], env=env)
        python = venv_python(service, target)
        run([python, '-m', 'pip', 'install', '--require-hashes', '-r', lock_path(ROOT, 'service', target)], env=env)
        run([python, '-m', 'pip', 'install', '--no-deps', '--no-build-isolation', '-e', ROOT], env=env)
        if parquet_lock:
            run([python, '-m', 'pip', 'install', '--require-hashes', '--only-binary=:all:', '-r', parquet_lock], env=env)
        run([python, '-m', 'pip', 'check'], env=env)
        if not args.skip_frontend and (ROOT / 'packages/ide_feature/package-lock.json').is_file():
            npm = shutil.which('npm.cmd' if target.startswith('win32-') else 'npm')
            if not npm:
                raise ValueError('Node/npm is required for the workbench. Install Node 20+ or use --skip-frontend.')
            for arguments in (['ci'], ['run', 'check'], ['run', 'compile']):
                run([npm, *arguments], env=env, cwd=ROOT / 'packages/ide_feature')
    if wants_backend:
        if reason:
            print(f'Local backend unavailable: {reason}', file=sys.stderr)
            return 2
        service_uv = venv_python(service, target).parent / ('uv.exe' if target.startswith('win32-') else 'uv')
        uv = str(service_uv) if service_uv.is_file() else shutil.which('uv')
        if not uv:
            raise ValueError('Install the service environment or official uv 0.12.23 first.')
        run([uv, 'venv', '--allow-existing', '--python', sys.executable, backend], env=env)
        command = [uv, 'pip', 'sync', '--python', venv_python(backend, target), '--require-hashes', backend_lock]
        if target.startswith('linux-'):
            index = CUDA_INDEX if args.backend_variant == 'cuda-cu128' else CPU_INDEX
            command += ['--extra-index-url', index, '--index-strategy', 'unsafe-best-match']
        if args.backend_variant == 'cuda-cu128':
            command += ['--only-binary', ':all:']
        run(command, env=env)
        if parquet_lock:
            run([uv, 'pip', 'install', '--python', venv_python(backend, target), '--require-hashes',
                 '--only-binary', ':all:', '-r', parquet_lock], env=env)
        if args.trainer:
            run([uv, 'pip', 'install', '--python', venv_python(backend, target), '--require-hashes',
                 '--only-binary', ':all:', '--no-deps', '-r', trainer_lock], env=env)
        run([uv, 'pip', 'check', '--python', venv_python(backend, target)], env=env)
        if not args.skip_model_download:
            env['PYTHONPATH'] = str(ROOT / 'src')
            run([venv_python(backend, target), ROOT / 'scripts/download_model.py'], env=env)
    if args.tracking:
        tracking_lock = ROOT / 'requirements/profiles' / f'tracking-wandb-{target}.lock'
        if not tracking_lock.is_file():
            raise ValueError('Optional W&B SDK profile is not available for this native target.')
        service_uv = venv_python(service, target).parent / ('uv.exe' if target.startswith('win32-') else 'uv')
        uv = str(service_uv) if service_uv.is_file() else shutil.which('uv')
        if not uv:
            raise ValueError('Install the service environment or official uv 0.12.23 first.')
        run([uv, 'venv', '--allow-existing', '--python', sys.executable, tracking], env=env)
        run([uv, 'pip', 'sync', '--python', venv_python(tracking, target), '--require-hashes', tracking_lock], env=env)
        run([uv, 'pip', 'check', '--python', venv_python(tracking, target)], env=env)
    print('Bootstrap complete. No credentials or remote resources were created.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f'Bootstrap failed: {error}', file=sys.stderr)
        raise SystemExit(1) from error
