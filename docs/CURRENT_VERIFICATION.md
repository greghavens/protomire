# Current verification and remaining gates

Runtime source: `735d0f3e82b5a01ef41f322313c415415b9c7432`, with an **84-file verified source closure**. This is an implementation checkpoint, not full-product completion. Earlier results remain in the [69-file checkpoint report](https://github.com/greghavens/modelbuilder/blob/fc44661bece9c25d9ecdd366987300220afbb5f1/docs/CURRENT_VERIFICATION.md) and [previous authoring/release report](https://github.com/greghavens/modelbuilder/blob/9a34bb28c334f1ccaa719570c100a0f30435e08e/docs/CURRENT_VERIFICATION.md); their counts are historical.

## Verified at this checkpoint

- **Four clean installed-wheel runs, four independent environments/stores, 20 uncached native steps and 70/70 comparison checks.** All eight custom and twelve pretrained output hashes match. Numerical metrics and complete semantic evaluation/binding evidence match, while native artifact/run/model-version identities are validated separately. Export reload difference is **0.0**. All 84 source files, **63 service modules and 48 backend modules** match the actual Git source and installed wheel bytes.
- **167 portability tests** and **10 native ABI tests** pass. The ABI tests use the fresh backend wheel outside the checkout. These baseline environments retain the original dependency locks without optional PyArrow.
- Separate fresh optional-profile environments install those same wheels plus hash-locked PyArrow 25.0.0. Actual registered Parquet feeds a native pipeline producing **four local GPT-2 candidates and four separate BERT predictions**; **25 installed boundary tests** pass. Module bytes are reverified after execution. This proof uses ASGI HTTP routing and an independent native worker, not live TCP/MCP or GUI. Tiny local models establish execution/provenance, not useful model quality; predictions are not ground truth.
- **Full local CI at `d14ec068cae4b67bedaee15211c34f0a29231b0d` passed with exit 0**, with runtime/source unchanged: **1,531 light tests** (53 skipped, 32 deselected; 57.30 seconds). The bounded two-process native queue passed **24 service tests** (858.83 seconds), **23 lifecycle tests** (1,162.94 seconds) and **186 component tests** (253.33 seconds). All three groups exited 0; the queue completed in **1,164.433 seconds**, with cleanup complete and no descendants requiring a signal. The earlier aggregate stopped at backend collection because three HTTP/TestClient cases used the wrong interpreter. That failed attempt remains recorded; all three cases now run in the service group without changed dependencies or dropped coverage.
- Generated contracts, tracked-artifact validation and both shipping source-closure checks pass.
- **306 frontend unit/host tests**, both TypeScript checks and four real service/HTTP harnesses pass, including recipe history. Dialogs use explicit doubles. The combined source-built Code - OSS distribution and launcher **1.105.0** pass; no native window interaction is inferred.
- The frozen source includes exact initiating-grant revocation checks across unlocked work, chart refresh focus preservation, and immutable checkpoint-harness cancellation coverage. Pinned source is not edited to make cancellation/resume pass. Final-source aggregate results are distinguished above.

The batch includes bounded research content retrieval/search, same-recipe history/restore, measured checkpoint curves, compute diagnostics, optional real Parquet, local generation/judging, revision-scoped native JSON-value caching, explicit terminal conditions and checkpoint-bound training live evaluation. Existing source/dataset authority, immutable run provenance and verified checkpoint ownership remain required.

## Resource and platform boundaries

- Parquet parsing runs in a Linux child with a fixed **512 MiB address-space cap before Arrow parsing**, CPU/output limits, bounded slots and a parent wall deadline with kill/reap. Decode and row transformations occur outside the writer transaction; authority, source/state revisions and pinned bytes are rechecked before commit. These are resource limits, not an arbitrary-code sandbox or a parent-process RSS guarantee.
- Unlocked research/dataset work and held-I/O live tests do not establish nonblocking admission throughout the product. Environment verification, native artifact resolution, backend initialization and resume materialization can still hold the writer transaction, so concurrent admission may delay checkpoint ingestion. Admission/dispatch separation remains an open gate.
- Optional contained Parquet **fails closed on macOS/Windows**; JSONL/CSV and ordinary IDE paths remain available. Linux arm64 runtime is unverified. Dataset/model-export Windows HANDLE readers are implemented but host-unverified; the native backend still requires POSIX process supervision, and checkpoint-subgraph native binding is unavailable on Windows.
- Native GUI/notification acceptance remains blocked by the current environment's display/local-socket restrictions. macOS GUI requires a connected user environment; none was connected at the latest check. Linux compilation, dialog doubles and HTTP tests do not substitute for native interaction. Other native platform/architecture acceptance is outstanding.

## Provider, CI and product gates

- Controlled research tests pass. A current Europe PMC retrieval was actually attempted and failed with `source_unavailable`; the same helper diagnosed temporary DNS failure (`errno -3`). No content was retained and no proxy/security bypass was attempted. This environmental provider gate remains open.
- Online W&B, authenticated Claude/Codex inference, production remote-provider execution/billing and cloud spending are not established. Production remote execution remains disabled. Further gaps include a comprehensive reusable catalog, arbitrary publisher/PDF/OCR retrieval and scientific synthesis, general branching/iteration/concurrency beyond supported terminal conditions, and hostile-code isolation. Separately staged accelerator work is excluded.
- Earlier main `9a34bb28` passed both jobs in [CI run 38007002099](https://github.com/greghavens/modelbuilder/actions/runs/38007002099). The later [run 38013134412](https://github.com/greghavens/modelbuilder/actions/runs/38013134412) was canceled at its **40-minute cap** and is **not a pass**. Hosted CI is intentionally disabled at the owner's request; the workflow is manual-only, and no hosted run is authorized. The full local bounded-concurrency CI run passed as recorded above. Earlier hosted runs are not asserted to have been free.
- Raw machine reports, hash/comparison attestations and environment metadata remain local pending approval to publish. Only concise verification summaries and reproducible test source are included.

## Reproduce from source

On the supported Linux CPU profile:

```sh
bash scripts/bootstrap.sh --backend --tracking --parquet
MODELBUILDER_RUN_INTEGRATION=1 MODELBUILDER_REQUIRE_PARQUET=1 \
  MODELBUILDER_NATIVE_TEST_PROCESSES=2 bash scripts/test_all.sh
.venv/bin/python -m pytest tests/portability -q
.venv/bin/python scripts/replay_bundle.py --help
```

The replay helper exposes inert export/import/wheel and explicitly trusted run/compare operations. A complete replay needs independent service/backend environments and stores, not two runs in one environment. Baseline fixture replay and optional-profile evidence are separate. See [checkpoint evaluation design](design/LIVE_CHECKPOINT_EVALUATION.md), [dataset lifecycle](DATASET_LIFECYCLE.md) and [dependency profiles](../requirements/README.md) for precise contracts and limits.
