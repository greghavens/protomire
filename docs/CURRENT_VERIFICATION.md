# Current verification and remaining gates

## 2026-10-12 implementation in progress

The full product remains incomplete. This working-tree checkpoint adds real
Transformers Trainer/inference components, streaming datasets, conditional live
execution, bounded native iteration, PDF/OCR research ingestion, a locked CUDA
profile and an offline-tested native Modal bridge. These are implementation and
focused-test results, not a replacement for final combined acceptance.

- The first combined light regression has **1,916 passed, 137 skipped and 59
  deselected**; two shipping source-closure tests fail because the new source
  modules are not yet repinned to a committed runtime. They remain required.
- Focused real CPU Trainer/native pipelines, streaming JSONL/Parquet consumption,
  general local inference and research HTTP/MCP extraction tests pass. Reports
  remain separately recorded; overlapping counts are not summed.
- Native bounded-iteration tests demonstrate actual iterations, early exit,
  checkpoint ancestry and explicit warm-start. Final parity/regression checks
  are in progress. General concurrent native execution is still outstanding.
- The CUDA profile installs and passes CPU-side admission checks. Actual GPU
  execution is unverified because this environment exposes no GPU. MPS and other
  platform acceptance remain hardware-specific gates.
- Fresh Code - OSS source/toolchain preparation and native-header preparation
  succeeded. The build stopped at an npm network policy denial. An offline-only
  retry then failed with `ENOTCACHED` for `vscode-uri-3.0.2.tgz`. Neither attempt
  establishes a current native build or GUI pass.
- The Modal SDK bridge is offline-tested; production dispatch remains blocked
  by source-image provenance and authoritative all-in billing/runtime limits.
  An account or spending approval alone does not resolve those design gates.
- The host cannot establish the required isolated worker namespace. No hostile
  code sandbox is claimed. Native GUI, macOS/Windows/ARM runtime, live vendor
  assistant calls and online tracking remain separate unverified gates.
- Hosted CI remains disabled by user instruction. All checks here run locally.

Source freeze, current installed-wheel replay, refreshed shipping pins, final
combined tests and publication verification are still pending. The preceding
report below is preserved as historical evidence, not applied to this source.

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
