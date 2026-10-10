#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
mkdir -p evidence/tests
PYTHON="${MODELBUILDER_TEST_PYTHON:-$ROOT/.venv/bin/python}"
if [[ -x "$ROOT/.venv-tracking/bin/python" ]]; then
  export MODELBUILDER_TRACKING_TEST_PYTHON="$ROOT/.venv-tracking/bin/python"
fi
"$PYTHON" -m pip check
"$PYTHON" -m compileall -q src
"$PYTHON" -m ruff check src tests scripts distribution
"$PYTHON" scripts/export_contracts.py --check
"$PYTHON" scripts/validate_artifacts.py
"$PYTHON" -m pytest -m 'not integration and not portability and not gui' -v --junitxml=evidence/tests/unit-service-mcp.xml "$@" | tee evidence/tests/unit-service-mcp.log
if [[ -f packages/ide_feature/package-lock.json ]]; then
  (cd packages/ide_feature && npm run check && npm run compile && npm test && npm run test:service && npm run test:workspace-http && npm run test:source-pin-http && npm run test:history-http) | tee evidence/tests/workbench.log
fi
if [[ "${MODELBUILDER_RUN_INTEGRATION:-0}" == 1 ]]; then
  test -x "$ROOT/.venv-backend/bin/python"
  (cd packages/ide_feature && MODELBUILDER_COMPONENT_NATIVE=1 MODELBUILDER_GUI_PYTHON="$PYTHON" MODELBUILDER_BACKEND_PYTHON="$ROOT/.venv-backend/bin/python" npm run test:component-http) | tee evidence/tests/component-authoring-native.log
  MODELBUILDER_BACKEND_PYTHON="$ROOT/.venv-backend/bin/python" "$PYTHON" -m pytest tests/service/test_real_backend.py tests/integration/test_mcp_training.py tests/security/test_adversarial_review.py tests/integration/test_source_bound_execution.py tests/integration/test_native_multi_output.py tests/integration/test_project_source_execution.py tests/integration/test_bound_dataset_checkpoint_release.py tests/integration/test_source_deadline_hooks.py tests/integration/test_live_mcp_tracking.py tests/datasets/test_mcp_dataset.py tests/datasets/test_native_dataset.py tests/native_cache/test_native_reuse.py tests/conditions/test_native_conditions.py -m integration -v --junitxml=evidence/tests/integration.xml | tee evidence/tests/integration.log
  MODELBUILDER_REQUIRE_LOCAL_INFERENCE=1 PYTHONPATH="$ROOT/src" "$ROOT/.venv-backend/bin/python" -m pytest tests/integration/test_cpu_fixtures.py tests/integration/test_native_lifecycle.py tests/integration/test_early_cancellation.py tests/integration/test_builtin_warm_start.py tests/integration/test_model_release_workflow.py tests/integration/test_checkpoint_subgraph.py tests/integration/test_conditional_checkpoint.py tests/integration/test_calibrated_reference_lifecycle.py tests/integration/test_live_reads_during_discovery.py tests/components tests/evaluation tests/live_evaluation tests/datasets/test_reference_atomic_read.py tests/tracking/test_cpu_live_tracking.py -v --junitxml=evidence/tests/cpu-fixtures.xml | tee evidence/tests/cpu-fixtures.log
fi
