#!/usr/bin/env bash
# Never disable Chromium/Electron sandboxing. A missing GUI is BLOCKED, not PASS.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "${MODELBUILDER_PYTHON:-python3}" "$ROOT/distribution/smoke_ide.py" "$@"
