# Current verification and remaining gates

## 2026-10-13 local aggregate passed; product gates remain

The full product remains incomplete. This frozen runtime checkpoint implements
Transformers Trainer/inference components, streaming datasets, conditional live
execution, bounded native iteration, PDF/OCR research ingestion, a locked CUDA
profile and an offline-tested native Modal bridge. The **complete local aggregate
passed with exit 0** for its declared Linux CPU selections. That result does not
establish the remaining product, platform, GUI or provider acceptance gates.

- The current combined light regression has **1,952 passed, 136 skipped and 63
  deselected** in **82.68 seconds**, including the repinned shipping source
  closures. The preceding 1,916-pass attempt had two stale-pin failures; those
  were corrected by
  committing the runtime and regenerating both manifests, not weakening tests.
- The final native queue completed in **1,561.526 seconds**: lifecycle
  **24 passed** in **1,219.55 seconds**; service/native integration **50 passed,
  43 deselected** in **1,484.27 seconds**; components **373 passed, 2 skipped** in
  **338.99 seconds**. All groups exited 0; `cleanup.complete` is true with
  **zero signaled processes**. The two component skips are the explicit
  accelerator-Trainer and actual-CUDA hardware gates, not CPU execution passes.
  See the [preserved final aggregate evidence](../evidence/product-completion-20261012/final-local-ci/summary.json)
  for the runner and per-group terminal receipts.
- Frontend checks have **349 passing tests**. The separate offline Modal SDK
  construction suite has **3 passes**; its socket-blocked tests make no live
  provider or billing claim. The preserved final aggregate evidence above
  records these terminal results alongside the native queue.
- Focused real CPU Trainer/native pipelines, streaming JSONL/Parquet consumption,
  general local inference and research HTTP/MCP extraction tests pass. Reports
  remain separately recorded; overlapping counts are not summed.
- The complete `tests/iterations/test_native_iterations.py` file passed:
  **12 passed, zero skipped, 339.737 seconds**. Its evidence verifies actual
  iterations, early exit, checkpoint ancestry, explicit warm-start and HTTP/MCP
  parity, including expected failure/cancellation paths. All **14 native job
  workers were reaped and their process groups terminated**, as recorded in
  `evidence/tests/native-iterations/combined-d51a90d.json`. Runtime and test hashes
  were unchanged during that run. General parallel scheduling within
  user-authored pipelines remains outstanding.
- The earlier complete CPU component preflight passed: **373 passed, 2 skipped**,
  exit **0**, with `cleanup.complete: true` in
  `evidence/product-completion-20261012/component-preflight/runner.json` and counts
  in the adjacent `results.xml`. This is one focused group, not a complete
  aggregate pass. These results overlap other selections and are not summed.
- The CUDA profile installs and passes CPU-side admission checks. Actual CUDA
  execution is unverified because this Linux environment exposes no GPU. The
  separate Mac task's mandatory MPS pass is scoped below; it does not establish
  general accelerator or other-platform acceptance.
- Fresh Linux Code - OSS source/toolchain and native-header preparation
  succeeded. The build stopped at an npm network policy denial. An offline-only
  retry then failed with `ENOTCACHED` for `vscode-uri-3.0.2.tgz`. Neither attempt
  establishes a current native build or GUI pass.
- The Modal SDK bridge is offline-tested; production dispatch remains blocked
  by source-image provenance and authoritative all-in billing/runtime limits.
  An account or spending approval alone does not resolve those design gates.
- The host cannot establish the required isolated worker namespace. No hostile
  code sandbox is claimed. Full native GUI and cross-platform runtime
  acceptance, live vendor assistant calls and online tracking remain separate
  unverified gates; the partial Mac results below do not close them.
- Hosted CI remains disabled by user instruction. All checks here run locally.

Runtime source is now frozen at `d51a90d93df4a150c088c2d9800c340168b911b4`;
shipping manifests are committed at `bdafebeb736a3839294755c2f2ba223a42cdd5b4`.
Both manifests pin the complete **106-file** runtime/lock closure. The aggregate
used test checkpoint `4154a2fdd6e5fed67f840ba6c9316cf06ccb3e46`, containing the test-only
held-discovery correction and documentation updates; runtime bytes and shipping
pins remain unchanged.

Independent installed-wheel replay passed for both fixtures: **35/35 checks
each**, exact hashes for eight custom and twelve pretrained output files, four
fresh environments and native stores, before/after source and installed-byte
verification, and verified worker termination. **182 portability tests** passed.
The complete local aggregate rerun has now **passed with exit 0**, including the
combined native queue above. Publication verification remains pending. An npm
registry policy denial interrupted the first aggregate before native tests; an explicit offline-only
retry passed that stage. Its native queue then failed an existing held-discovery
fixture after 23 lifecycle passes: the test asserted that every accelerator
probe was MPS, although discovery now also checks CUDA. Exact tracing reproduced
the stale assertion. The test-only fix retains lock, no-allocation, latency and
live-read/write assertions; all three real CPU cases and 180 related checks pass,
with one actual-CUDA hardware skip. Runtime bytes and shipping pins are unchanged.
The successful final rerun does not relabel the earlier canceled or not-started
groups as passes. Standalone iteration, preflight, light and native selections
intentionally overlap; their counts are not added as unique coverage. No hosted
CI ran.

A separate authorized Mac task on **Apple M3 Max, arm64, macOS 26.6.2** has
reported **1 mandatory MPS test passed**, **45 backend passes**, and **64 service
passes after a Mac-local snapshot-path fix**. The source-built Code - OSS app
passed **8 native software smoke checks**. An earlier GUI attempt timed out at
workspace trust; the later smoke passes do not establish all native GUI gates.
Mac-local fixes and standalone MLX work are **not yet integrated into the frozen
runtime source**. These separate results are not promoted to this Linux
aggregate, and no integrated MLX or full Mac GUI acceptance is claimed.

The preceding report below is preserved as historical evidence, not applied
to this source.

---

# Historical 2026-10-10 verification

Runtime source: `decf680d96ed92e28170580c09bf388c17a1ff22`, with an **86-file verified source closure**. This is an implementation checkpoint, not full-product completion. The [preceding 84-file report and passing d14 local CI](https://github.com/greghavens/modelbuilder/blob/5475d94145ae68bca06fd3bca05bd52e3f43143c/docs/CURRENT_VERIFICATION.md) remain historical evidence.

## Verified at this checkpoint

- **Four clean installed-wheel runs, four independent environments/stores, 20 uncached native steps and 70/70 comparison checks.** All eight custom and twelve pretrained output hashes match. Numerical metrics and complete semantic evaluation/binding evidence match; independent native artifact/run/model-version ownership is checked separately. Export reload difference is **0.0**. All **86 source files, 65 service modules and 49 backend modules** match Git and installed wheel bytes before/after execution.
- **167 portability tests** and **10 native ABI tests** pass. ABI tests use the fresh backend wheel outside the checkout. Baseline dependency locks are unchanged and do not install optional PyArrow.
- Separate fresh profiles install the same wheels plus hash-locked PyArrow 25.0.0: **72 service and 104 backend distributions** pass compatibility checks. Registered Parquet feeds an actual native pipeline producing **four local GPT-2 candidates and four separate BERT predictions**; **25 installed boundary tests** pass. Module bytes, immutable assets and worker termination are verified. This uses ASGI routing and an independent worker, not live TCP/MCP or GUI. Tiny synthetic models establish execution/provenance, not quality or ground truth.
- **Full local CI passed with exit 0** against this runtime source and the repinned shipping examples: **1,781 light tests** (62 skipped, 33 deselected; 62.36 seconds), including both shipping source-closure checks. Contracts and tracked-artifact validation pass.
- **319 frontend unit/host tests**, both TypeScript checks, four real HTTP harnesses and native component authoring pass. Nine regression cases cover cleanup ownership, late writes, evidence retention and the non-native path. Dialogs use explicit doubles. The combined source-built Code - OSS compilation/assembly and launcher **1.105.0** pass; native window interaction is a separate gate.
- All **309 distinct native cases** passed: **25 service tests** (941.34 seconds), **24 lifecycle tests** (1,223.16 seconds) and **260 component tests** (255.13 seconds). Serial/queue collection matches without missing or duplicate cases. The bounded two-process queue completed in **1,224.728 seconds**; all groups exited 0, cleanup completed and no descendants required a signal. Light/native selections intentionally overlap and must not be added as unique coverage.

The first full-CI attempt passed its light/frontend checks but exited 1 after the component-authoring test's functional native pass: deletion raced the detached supervisor's final configuration write and raised `ENOTEMPTY`. The native queue did not start. Test-only cleanup now waits for inherited launch leases, birth-identified owner exit and confirmed worker/group termination before deletion or PASS; uncertain cleanup retains evidence. Deterministic regressions, both real harness modes and the complete second CI attempt passed. Runtime source remained unchanged; the failed attempt remains recorded.

The first optional-profile setup attempt stopped before environment creation because uv selected a read-only default cache. The explicit workspace-cache retry passed without runtime changes. Its failed log remains preserved. A held-admission native test also initially resolved its venv interpreter to base Python without ZenML. A test-only path correction preserved the venv; controlled red-to-green verification and the actual native rerun passed, with runtime source unchanged. Earlier failed attempts and corrections remain in historical reports; retries do not change their original status.

## Admission and placement boundaries

Run planning/submission/resume now prepare source, datasets, environments, device checks, native bindings, parent checkpoints and backend initialization outside the service writer transaction. Short reservations and final dispatch checks retain the exact initiating grant, immutable revisions, cancellation and idempotency authority. Run reads/cancellation also release the writer for backend I/O and recheck response authority. Other operations may retain writer scopes; this is not an all-API latency guarantee. An uncertain launch is reconciled without automatically repeating training. There is no cross-store exactly-once claim. See [admission and dispatch](admission-dispatch.md).

CPU placement is demonstrated. MPS requires the reviewed macOS ARM64 profile and remains **hardware-unverified**. CUDA execution is **unavailable** without a reviewed hash-locked CUDA profile. A tiny allocation probe does not prove full-model fit or a trusted component's actual device use. Live evaluation remains CPU-only. Accelerator exact-state continuation is rejected; supported warm-start remains explicit. Placement reserves no hardware and supplies no hostile-code isolation. See [local device placement](LOCAL_DEVICE_PLACEMENT.md).

## Remaining platform, provider and product gates

- Linux Parquet children enforce a **512 MiB address-space cap before Arrow parsing**, CPU/output/slot limits and a parent kill/reap deadline. These are resource limits, not a sandbox or parent RSS guarantee. Optional contained Parquet **fails closed on macOS/Windows**; JSONL/CSV remain usable. Linux arm64 runtime is unverified. Windows HANDLE readers exist but are host-unverified; native process supervision/checkpoint-subgraph binding remain unavailable on Windows.
- Native GUI/notification acceptance remains blocked by display/local-socket restrictions. macOS GUI requires a connected user environment; none was connected at the latest check. Build, dialog doubles and HTTP tests do not establish native interaction or other-platform acceptance.
- Controlled research tests pass. The latest actual Europe PMC request failed with `source_unavailable`; the same helper identified temporary DNS failure (`errno -3`). No content was retained or security fallback attempted. Online W&B, authenticated Claude/Codex inference and production remote-provider execution/billing are not established. Production remote execution remains disabled.
- Broader gaps remain: a comprehensive reusable catalog, arbitrary publisher/PDF/OCR retrieval and scientific synthesis, general branching/iteration/concurrency beyond supported conditions, and hostile-code isolation.
- Hosted CI is intentionally disabled by the owner; workflows are manual-only and no hosted run is authorized. Historical [run 38013134412](https://github.com/greghavens/modelbuilder/actions/runs/38013134412) was canceled at its 40-minute cap and is not a pass. Earlier hosted runs are not asserted to have been free. This checkpoint's complete CI was run locally and passed as recorded above.
- Raw reports, hash/comparison attestations and environment metadata remain local. Completed generated environments were archived with full comparison/integrity checks and restoration instructions; source trees, native stores and proof remain preserved. No raw evidence publication is implied.

## Reproduce from source

On the supported Linux CPU profile:

```sh
bash scripts/bootstrap.sh --backend --tracking --parquet
MODELBUILDER_RUN_INTEGRATION=1 MODELBUILDER_REQUIRE_PARQUET=1 \
  MODELBUILDER_NATIVE_TEST_PROCESSES=2 bash scripts/test_all.sh
.venv/bin/python -m pytest tests/portability -q
.venv/bin/python scripts/replay_bundle.py --help
```

Complete replay requires independent service/backend environments and stores. Baseline fixture and optional-profile proofs are separate. See [dataset lifecycle](DATASET_LIFECYCLE.md) and [dependency profiles](../requirements/README.md).
