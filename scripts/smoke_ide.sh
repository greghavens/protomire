#!/usr/bin/env bash
# Never disable Chromium/Electron sandboxing. A missing GUI is BLOCKED, not PASS.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLI=${MODELBUILDER_IDE:-"$ROOT/.runtime/bin/modelbuilder-ide"}
SUITE=${MODELBUILDER_GUI_SUITE:-smoke}
[[ ${1:-} == --workbench ]] && SUITE=workbench
if [[ "$SUITE" == workbench ]]; then DEFAULT_EVIDENCE="$ROOT/evidence/gui/workbench"; REPORT=workbench-host-results.json; else DEFAULT_EVIDENCE="$ROOT/evidence/gui/distribution"; REPORT=smoke-results.json; fi
EVIDENCE=${MODELBUILDER_GUI_EVIDENCE:-"$DEFAULT_EVIDENCE"}
mkdir -p "$EVIDENCE" "$ROOT/.runtime/gui-workspace" "$ROOT/.runtime/gui-user-data/User"
rm -f "$EVIDENCE/$REPORT" "$EVIDENCE/runner-error.json" "$EVIDENCE/stage.txt"
blocked() {
  python3 - "$EVIDENCE" "$1" <<'PY'
import json,pathlib,sys
(pathlib.Path(sys.argv[1])/'smoke-results.json').write_text(json.dumps({'status':'BLOCKED','reason':sys.argv[2],'tests':[]},indent=2)+'\n')
PY
  echo "BLOCKED: $1"; exit 2
}
[[ -x "$CLI" ]] || blocked 'Branded editor executable is absent; run scripts/build_ide.sh first.'
[[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]] || blocked 'No GUI display in this executor. Run this script from the supported desktop terminal or under Xvfb on a sandbox-capable host.'
python3 "$ROOT/distribution/install_extensions.py" --cli "$CLI" --extensions-dir "$ROOT/.runtime/extensions" > "$EVIDENCE/extension-install.log" 2>&1
export MODELBUILDER_GUI_EVIDENCE="$EVIDENCE"
export MODELBUILDER_GUI_SMOKE_RUN=1
export MODELBUILDER_GUI_SUITE="$SUITE"
export MODELBUILDER_GUI_NODE=${MODELBUILDER_GUI_NODE:-"$ROOT/.cache/toolchains/node-v22.19.0-linux-x64/bin/node"}
export MODELBUILDER_GUI_PYTHON=${MODELBUILDER_GUI_PYTHON:-"$ROOT/.venv/bin/python"}
[[ -x "$MODELBUILDER_GUI_PYTHON" ]] || blocked 'Set MODELBUILDER_GUI_PYTHON to a Python environment with ipykernel installed.'
"$MODELBUILDER_GUI_PYTHON" -c 'import ipykernel' || blocked 'The selected Python environment needs the pinned ipykernel dependency.'
if [[ ! -d "$ROOT/.runtime/gui-workspace/.git" ]]; then git -C "$ROOT/.runtime/gui-workspace" init; fi
printf '{"telemetry.telemetryLevel":"off","workbench.startupEditor":"none","chat.disableAIFeatures":true,"python.experiments.enabled":false,"extensions.autoUpdate":false,"security.workspace.trust.enabled":true}\n' > "$ROOT/.runtime/gui-user-data/User/settings.json"
if [[ ${1:-} == --launch ]]; then
  exec "$CLI" --user-data-dir "$ROOT/.runtime/gui-user-data" --extensions-dir "$ROOT/.runtime/extensions" "$ROOT/.runtime/gui-workspace"
fi
# The fixture is repository-owned. Trust is recorded by the normal IDE trust UI;
# this script does not disable that protection or add --no-sandbox.
timeout 600 "$CLI" --user-data-dir "$ROOT/.runtime/gui-user-data" --extensions-dir "$ROOT/.runtime/extensions" --extensionDevelopmentPath="$ROOT/tests/gui" --remote-debugging-port=9222 "$ROOT/.runtime/gui-workspace" > "$EVIDENCE/launch.log" 2>&1 &
PID=$!
if [[ ${MODELBUILDER_GUI_APPROVE_FIXTURE_TRUST:-0} == 1 ]]; then
  "$MODELBUILDER_GUI_NODE" "$ROOT/tests/gui/trust_fixture.cjs" "$ROOT/.runtime/gui-workspace" > "$EVIDENCE/fixture-trust.log" 2>&1 &
fi
LAST=''
while kill -0 "$PID" 2>/dev/null; do
  STAGE=$(cat "$EVIDENCE/stage.txt" 2>/dev/null || true)
  if [[ -n "$STAGE" && "$STAGE" != "$LAST" ]]; then
    if command -v import >/dev/null; then import -window root "$EVIDENCE/$STAGE.png" 2>> "$EVIDENCE/screenshot.log" || true; fi
    LAST=$STAGE
  fi
  sleep 1
done
wait "$PID" || { echo 'Real GUI process failed; inspect launch.log'; exit 1; }
python3 - "$EVIDENCE/$REPORT" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]));print(json.dumps(d,indent=2));assert d['status']=='PASS'
PY
