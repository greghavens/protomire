# Actual implementation evidence

Start with [VALIDATION_SUMMARY.json](VALIDATION_SUMMARY.json) and the acceptance matrix.

- `tests/`: final contract, service/MCP and actual CPU/native lifecycle JUnit/logs.
- `gui/distribution/`: actual source-built editor, notebook, breakpoint, terminal/Git and separately installed vendor extension results.
- `gui/workbench/`: actual graph, native Monaco, independent MCP parity and conflict/draft-preservation results.
- `gui/execution/`: real GUI button/confirmation submission through the shared service to ZenML.
- `portability/`: four genuine runs in independently installed environments/stores, comparisons and complete inert source bundles.
- `backend/`: native IDs, artifacts, checkpoint state parity, early cancellation and pending-cancel resume tests.
- `mcp/`, `ide/`, `recipe/`, `demo/`, `research/`, `dependencies/`: scoped additional evidence.
- `ci/`: retained CI failures and final summaries. First bootstrap and source-pin failures were fixed, not skipped.

Historical files containing `initial`, `failure`, `before` or unsuccessful build attempts describe those attempts; they are not PASS evidence. Current acceptance uses the specifically linked final result files. Test counts from overlapping runs must not be summed. Some larger historical desktop images are kept only in the local build workspace; the committed screenshots are the selected actual acceptance captures. No screenshot is a design mockup.

The first complete local script's constituent tests passed but editing its shell file during execution caused a trailing parse error. `full-validation-final.log` records a subsequent complete exit-zero run. The later contract-only rerun and final frontend unit tests cover added regression cases.
