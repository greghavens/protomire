#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "${MODELBUILDER_PYTHON:-${PYTHON:-python3.12}}" "$ROOT/scripts/bootstrap.py" --backend-only "$@"
