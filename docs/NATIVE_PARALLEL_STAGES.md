# Bounded native stage execution

This page covers the canonical setting and editor. See
[native implementation, cleanup and verification](NATIVE_PARALLEL_EXECUTION.md)
for the execution contract and evidence boundaries.

The stage bound belongs to the existing canonical recipe:

```json
"execution": {"max_parallel_stages": 2}
```

`max_parallel_stages` is an integer from 1 through 8. An omitted `execution`
object, an empty object, or an omitted field means 1. The service does not insert
defaults into historical manifests, so existing recipe digests are unchanged.
Changing the field requires the ordinary recipe save and creates an immutable
revision. The bound travels with canonical JSON, history and inert recipe bundles.
It does not create a second recipe, model, run configuration or registry.

Run-request `execution.node_ids` and `execution.input_bindings` still select an
exact subgraph and pinned native input artifacts. They cannot override this
recipe setting. The same read-only setting validation is used by plan and submit;
invalid numbers, booleans, strings, unknown keys and malformed objects fail
before dispatch. A plan is revalidated at submission as usual.

## Editor

Use **Recipe → Stage execution → Maximum parallel stages**, apply the setting to
the existing draft, then save its revision and preview the execution plan. Native
JSON/Monaco editing uses the same checked-in schema. Unapplied settings block
save and survive draft recovery. A stale form cannot overwrite newer source
settings; **Discard stage settings edits** reloads the current canonical source.
Import and export remain the existing File-menu actions.

The panel reports backend support only when discovery explicitly advertises both
`control_flow.independent_stage_concurrency` and the supported maximum in
`recipe_settings.execution.max_parallel_stages`. Authoring a value by itself
does not establish backend or hardware acceptance.

## Execution boundaries

- Bounds greater than 1 are initially admitted only on Linux CPU. macOS parallel
  execution awaits actual native lifecycle and descendant-cleanup acceptance;
  this is a verification gate, not a permanent platform exclusion. Accelerator
  parallelism is also unverified and rejected. The omitted/default bound of 1
  retains existing supported macOS/MPS and other sequential routes.
- One retains the existing synchronous routes. Larger bounds request concurrent
  independent native stages within the selected graph. Data and condition
  dependencies must finish before a dependent stage can start.
- Bounded iteration blocks retain sequential iteration barriers. The bound
  applies to independent stages inside the current body, not simultaneous loop
  iterations. Existing typed state, early-exit, whole-block selection and resume
  constraints continue to apply.
- Checkpoint sidecars remain governed by `evaluation.live.max_concurrency`.
  The stage bound is not a combined process, thread, CPU or memory quota.
  Iteration/live-evaluation combinations remain rejected by their existing
  contract.
- Local CPU/memory/GPU reservations, per-stage environments, remote execution,
  hostile-code isolation and hardware acceptance on untested accelerators are
  not implied. All source code still requires pinned bytes and explicit trust.
- Actual native runs, artifact ancestry, retry behavior, cancellation and
  deadlines must be verified independently of configuration and editor tests.

## Configuration verification

`tests/recipe/test_execution_settings.py` verifies strict bounds, omitted-default
compatibility, digest changes, shared schema copies and bundle roundtrips.
`tests/recipe/test_execution_settings_transport.py` uses the real service through
ASGI for revision checks, inert export/import, one recipe identity and rejection
without dispatch. `tests/failure/test_execution_admission.py` checks shared
plan/submit failures, fail-closed platform/device gates, matching discovery and
read-only validation. The existing `npm run test:service` harness uses real HTTP
and an external official MCP client to verify that typed-editor settings survive
save, MCP inspection/editing and TypeScript reload unchanged.

The frontend's `executionSettings.test.cjs` covers transforms and a deliberately
simulated DOM: apply/discard, stale source, rejected application, exact incomplete
draft recovery and conservative capability display. These unit tests do not
establish real-browser or native GUI acceptance. `npm run test:execution-browser`
provides a separate actual Chromium DOM test with simulated host messages. Native
Code - OSS and actual backend execution remain separate acceptance gates.
