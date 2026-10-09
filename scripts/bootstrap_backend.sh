#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_CACHE_DIR="$PWD/.cache/uv"
export PIP_CACHE_DIR="$PWD/.cache/pip"
mkdir -p "$UV_CACHE_DIR" "$PIP_CACHE_DIR"
command -v uv >/dev/null || { echo 'Install uv from its official distribution first.' >&2; exit 1; }
uv venv --python 3.12 .venv-backend
uv pip sync --python .venv-backend/bin/python --require-hashes requirements/backend.lock \
  --extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match
PYTHONPATH="$PWD/src" .venv-backend/bin/python scripts/download_model.py
uv pip check --python .venv-backend/bin/python
