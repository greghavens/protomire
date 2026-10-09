#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PIP_CACHE_DIR="$ROOT/.cache/pip"
PYTHON="${PYTHON:-python3.12}"
"$PYTHON" -c 'import sys; assert sys.version_info[:3] == (3,12,14), "Pinned Python 3.12.14 required"'
"$PYTHON" -m venv .venv
.venv/bin/python -m pip install -r requirements/service.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/python -m pip check
if [[ "${1:-}" == "--backend" ]]; then PATH="$ROOT/.venv/bin:$PATH" bash scripts/bootstrap_backend.sh; fi
if [[ -f packages/ide_feature/package-lock.json ]]; then (cd packages/ide_feature && npm ci && npm run check && npm run compile); fi
printf '\nBootstrap complete. No credentials or remote resources were created.\n'
