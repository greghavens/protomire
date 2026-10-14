#!/usr/bin/env bash
set -euo pipefail
# Local CI uses bootstrapped dependencies; suppress npm maintenance requests.
# Clear uppercase aliases without changing bootstrap or npm configuration files.
unset NPM_CONFIG_OFFLINE NPM_CONFIG_UPDATE_NOTIFIER NPM_CONFIG_AUDIT NPM_CONFIG_FUND
export npm_config_offline=true npm_config_update_notifier=false
export npm_config_audit=false npm_config_fund=false
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
mkdir -p evidence/tests
case "${MODELBUILDER_NATIVE_TEST_PROCESSES:-1}" in
  1|2) ;;
  *) echo "MODELBUILDER_NATIVE_TEST_PROCESSES must be 1 (serial) or 2 (bounded Linux native queue)" >&2; exit 2 ;;
esac
NATIVE_DIAGNOSTIC_ARGS=()
case "${MODELBUILDER_NATIVE_COLLECT_ALL:-0}" in
  0) ;;
  1) NATIVE_DIAGNOSTIC_ARGS=(--continue-on-failure) ;;
  *) echo "MODELBUILDER_NATIVE_COLLECT_ALL must be 0 or 1" >&2; exit 2 ;;
esac
PYTHON="${MODELBUILDER_TEST_PYTHON:-$ROOT/.venv/bin/python}"
if [[ -x "$ROOT/.venv-tracking/bin/python" ]]; then
  export MODELBUILDER_TRACKING_TEST_PYTHON="$ROOT/.venv-tracking/bin/python"
fi
"$PYTHON" -m pip check
"$PYTHON" -m compileall -q src
"$PYTHON" -m ruff check src tests scripts distribution
"$PYTHON" scripts/export_contracts.py --check
"$PYTHON" scripts/validate_artifacts.py
"$PYTHON" -m pytest --ignore=tests/test_remote_modal_sdk.py --ignore=tests/hub/test_huggingface_sdk.py -m 'not integration and not portability and not gui' -v --junitxml=evidence/tests/unit-service-mcp.xml "$@" | tee evidence/tests/unit-service-mcp.log
# Hub SDK acceptance is local fake HTTP with all sockets blocked, never a provider transfer.
HUB_PYTHON="${MODELBUILDER_HUB_TEST_PYTHON:-$ROOT/.venv-hub-test/bin/python}"
rm -f evidence/tests/hub-sdk.log evidence/tests/hub-sdk.xml
if [[ -x "$HUB_PYTHON" ]]; then
  MODELBUILDER_REQUIRE_HUB_SDK=1 PYTHONPATH="$ROOT/src" "$HUB_PYTHON" -m pytest -c /dev/null tests/hub/test_huggingface_sdk.py -v --junitxml=evidence/tests/hub-sdk.xml | tee evidence/tests/hub-sdk.log
elif [[ "${MODELBUILDER_REQUIRE_HUB_SDK:-0}" == 1 ]]; then
  echo "BLOCKED: required offline Hub SDK acceptance needs the fully locked .venv-hub-test interpreter; no provider request was made." | tee evidence/tests/hub-sdk.log >&2
  exit 2
else
  echo "NOT RUN: optional offline Hub SDK acceptance (.venv-hub-test absent); real provider acceptance remains pending." | tee evidence/tests/hub-sdk.log
fi
if [[ -f packages/ide_feature/package-lock.json ]]; then
  (cd packages/ide_feature && npm run check && npm run compile && npm test && npm run test:service && npm run test:workspace-http && npm run test:source-pin-http && npm run test:history-http) | tee evidence/tests/workbench.log
fi
if [[ "${MODELBUILDER_RUN_INTEGRATION:-0}" == 1 ]]; then
  # A blocked earlier stage must not leave older successful native receipts.
  for stage in integration cpu-fixtures cpu-lifecycle cpu-components cpu-concurrency component-authoring-native native-dependencies research-document-http research-document-dependencies remote-modal-sdk native-runner; do
    rm -f "evidence/tests/$stage.log" "evidence/tests/$stage.xml" "evidence/tests/$stage.json"
  done
  if [[ ! -x "$ROOT/.venv-backend/bin/python" ]]; then
    echo "BLOCKED: native integration requires the separate .venv-backend interpreter (bootstrap --backend --trainer)." >&2
    exit 2
  fi
  if [[ ! -f packages/ide_feature/package-lock.json ]]; then
    echo "BLOCKED: native integration requires the locked workbench package for Research HTTP/MCP acceptance." >&2
    exit 2
  fi
  if ! "$ROOT/.venv-backend/bin/python" -c 'import torch, transformers, accelerate' > evidence/tests/native-dependencies.log 2>&1; then
    cat evidence/tests/native-dependencies.log >&2
    echo "BLOCKED: native integration requires pinned Torch/Transformers and the Trainer/Accelerate overlay (bootstrap --backend --trainer)." >&2
    exit 2
  fi
  # Real local extraction is required in integration mode. Never turn missing
  # native tools or English OCR data into a passing/skipped acceptance receipt.
  if ! "$PYTHON" - <<'PYDOC' | tee evidence/tests/research-document-dependencies.log
import json
from pathlib import Path
import subprocess
from modelbuilder.research_documents import document_capabilities
capabilities = document_capabilities()
ready = bool(capabilities["available"] and capabilities["ocr_available"])
if ready:
    try:
        result = subprocess.run(["/usr/bin/tesseract", "--list-langs"], capture_output=True, text=True, timeout=10)
        ready = result.returncode == 0 and "eng" in result.stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        ready = False
receipt = {"status": "READY" if ready else "BLOCKED", "scope": "Research HTTP/MCP local PDF/OCR dependencies only",
           "capabilities": capabilities, "ocr_english_data_verified": ready}
Path("evidence/tests/research-document-dependencies.json").write_text(json.dumps(receipt, indent=2) + "\n")
if not ready:
    Path("evidence/tests/research-document-http.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(json.dumps(receipt, indent=2))
raise SystemExit(0 if ready else 2)
PYDOC
  then
    echo "BLOCKED: Research document HTTP/MCP acceptance requires Linux, /usr/bin Poppler tools, and Tesseract with English data." >&2
    exit 2
  fi
  (cd packages/ide_feature && MODELBUILDER_GUI_PYTHON="$PYTHON" MODELBUILDER_RESEARCH_DOCUMENT_REPORT="$ROOT/evidence/tests/research-document-http.json" npm run test:research-document-http) | tee evidence/tests/research-document-http.log
  # This optional interpreter tests only SDK request construction. The suite
  # intercepts provider calls and forbids network sockets; it never runs Modal.
  if [[ -x "$ROOT/.venv-remote-modal/bin/python" ]]; then
    MODELBUILDER_REQUIRE_MODAL_SDK=1 PYTHONPATH="$ROOT/src" "$ROOT/.venv-remote-modal/bin/python" -m pytest tests/test_remote_modal_sdk.py -v --junitxml=evidence/tests/remote-modal-sdk.xml | tee evidence/tests/remote-modal-sdk.log
  elif [[ "${MODELBUILDER_REQUIRE_MODAL_SDK:-0}" == 1 ]]; then
    echo "BLOCKED: required offline Modal SDK acceptance needs .venv-remote-modal; no cloud request was made." | tee evidence/tests/remote-modal-sdk.log >&2
    exit 2
  else
    echo "NOT RUN: optional offline Modal SDK acceptance (.venv-remote-modal absent); live remote execution is not tested." | tee evidence/tests/remote-modal-sdk.log
  fi
  (cd packages/ide_feature && MODELBUILDER_COMPONENT_NATIVE=1 MODELBUILDER_GUI_PYTHON="$PYTHON" MODELBUILDER_BACKEND_PYTHON="$ROOT/.venv-backend/bin/python" npm run test:component-http) | tee evidence/tests/component-authoring-native.log
  if [[ "${MODELBUILDER_NATIVE_TEST_PROCESSES:-1}" == 2 ]]; then
    "$PYTHON" scripts/native_test_runner.py --root "$ROOT" --python "$PYTHON" ${NATIVE_DIAGNOSTIC_ARGS[@]+"${NATIVE_DIAGNOSTIC_ARGS[@]}"}
  else
    MODELBUILDER_BACKEND_PYTHON="$ROOT/.venv-backend/bin/python" "$PYTHON" -m pytest tests/service/test_real_backend.py tests/integration/test_mcp_training.py tests/security/test_adversarial_review.py tests/integration/test_source_bound_execution.py tests/integration/test_native_multi_output.py tests/integration/test_project_source_execution.py tests/integration/test_bound_dataset_checkpoint_release.py tests/integration/test_source_deadline_hooks.py tests/integration/test_live_mcp_tracking.py tests/datasets/test_mcp_dataset.py tests/datasets/test_native_dataset.py tests/native_cache/test_native_reuse.py tests/conditions/test_native_conditions.py tests/integration/test_admission_live_native.py tests/live_evaluation/test_selected_live_native.py tests/iterations/test_native_iterations.py tests/components/test_runner_native.py tests/rules/test_native_rules.py tests/rules/test_rule_stdio.py tests/rules/test_custom_native.py tests/rules/test_standalone_native.py -m integration -v --junitxml=evidence/tests/integration.xml | tee evidence/tests/integration.log
    MODELBUILDER_REQUIRE_TRAINER=1 MODELBUILDER_REQUIRE_LOCAL_INFERENCE=1 PYTHONPATH="$ROOT/src" "$ROOT/.venv-backend/bin/python" -m pytest tests/integration/test_cpu_fixtures.py tests/integration/test_native_lifecycle.py tests/integration/test_early_cancellation.py tests/integration/test_builtin_warm_start.py tests/integration/test_model_release_workflow.py tests/integration/test_checkpoint_subgraph.py tests/integration/test_conditional_checkpoint.py tests/integration/test_calibrated_reference_lifecycle.py tests/integration/test_live_reads_during_discovery.py tests/components tests/evaluation tests/live_evaluation tests/scientific/test_derivation.py tests/scientific/test_native_derivation.py tests/datasets/test_reference_atomic_read.py tests/tracking/test_cpu_live_tracking.py tests/placement/test_placement.py tests/placement/test_checkpoint_capabilities.py tests/placement/test_cuda_execution.py tests/integration/test_mlx_service.py --ignore=tests/live_evaluation/test_selected_live_native.py --ignore=tests/iterations/test_native_iterations.py --ignore=tests/components/test_runner_native.py --ignore=tests/rules/test_native_rules.py --ignore=tests/rules/test_rule_stdio.py --ignore=tests/rules/test_custom_native.py --ignore=tests/rules/test_standalone_native.py -v --junitxml=evidence/tests/cpu-fixtures.xml | tee evidence/tests/cpu-fixtures.log
    MODELBUILDER_REQUIRE_TRAINER=1 MODELBUILDER_REQUIRE_LOCAL_INFERENCE=1 PYTHONPATH="$ROOT/src" "$ROOT/.venv-backend/bin/python" -m pytest tests/concurrency -m integration -v --junitxml=evidence/tests/cpu-concurrency.xml | tee evidence/tests/cpu-concurrency.log
  fi
fi
