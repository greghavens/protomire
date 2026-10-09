#!/usr/bin/env bash
# Portable wrapper; orchestration deliberately needs no Bash 4/GNU utilities.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "${MODELBUILDER_PYTHON:-python3}" "$ROOT/distribution/build_ide.py" "$@"
