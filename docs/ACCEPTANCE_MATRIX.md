# Acceptance matrix: live implementation evidence

> Owner-requested corrections are verified below: one fixed model project and one recipe identity with immutable revisions; recipe transfer belongs only in the native File menu. Original design inputs are unchanged. Earlier selector and workspace-transfer screenshots are historical, not accepted UI. This is technical verification, not a claim of owner acceptance or full-product completion.

Current status updated 2026-10-13; see [current verification](CURRENT_VERIFICATION.md) for the in-progress full implementation and its unpassed gates. The 2026-10-10 and earlier checkpoints below remain historical.

## Current 2026-10-13 local aggregate and focused verification

Runtime remains `d51a90d93df4a150c088c2d9800c340168b911b4`; shipping manifests at
`bdafebeb736a3839294755c2f2ba223a42cdd5b4` retain the complete 106-file runtime/lock
closure. Aggregate test checkpoint `4154a2fdd6e5fed67f840ba6c9316cf06ccb3e46`
adds the test-only
held-discovery correction and documentation updates, without changing runtime
bytes or shipping pins.

| Scope | Current status | Evidence and limits |
|---|---|---|
| Complete native bounded-iteration file | PASS, focused suite | `tests/iterations/test_native_iterations.py`: 12 passed, zero skipped, 339.737 seconds. All 14 native job workers reaped and process groups terminated; runtime/test hashes unchanged. `evidence/tests/native-iterations/combined-d51a90d.json`. |
| Complete CPU component preflight | PASS, focused group with skips | 373 passed, 2 skipped; runner exit 0 and `cleanup.complete: true`. `evidence/product-completion-20261012/component-preflight/runner.json` and adjacent `results.xml`. |
| Complete local aggregate rerun | PASS, declared Linux CPU selections; exit 0 | Light: 1,952 passed, 136 skipped, 63 deselected in 82.68 seconds. Frontend: 349 passed. Separate offline Modal SDK construction: 3 passed, without live provider calls. [Preserved final aggregate evidence](../evidence/product-completion-20261012/final-local-ci/summary.json). |
| Final combined native queue | PASS, all groups exit 0 | Lifecycle: 24 passed in 1,219.55 seconds. Service/native integration: 50 passed, 43 deselected in 1,484.27 seconds. Components: 373 passed, 2 skipped in 338.99 seconds. Queue completed in 1,561.526 seconds; cleanup complete, zero signaled processes. Runner and per-group receipts are recorded in the [preserved final aggregate evidence](../evidence/product-completion-20261012/final-local-ci/summary.json). The two skips are accelerator-Trainer and actual-CUDA hardware gates. |
| Mac backend/service checks | PASS, separate Mac-local selections | Apple M3 Max, arm64, macOS 26.6.2: 45 backend passes and 64 service passes after a local snapshot-path fix. Mac fixes are not yet integrated into frozen `d51a90d`; this is not complete Mac runtime acceptance. |
| Mac native software smoke | PASS, 8 smoke checks; full GUI gates remain | Source-built Code - OSS passed 8 native software smoke checks. The earlier workspace-trust timeout remains an unsuccessful attempt; later smoke passes do not establish all native GUI gates. |
| Accelerator execution and MLX | PARTIAL: mandatory Mac MPS test passed | The separate Mac task has 1 mandatory MPS test pass. Actual CUDA remains unverified. Mac-local fixes and standalone MLX work are not integrated into the frozen source; no integrated MLX acceptance is claimed. |

Standalone, preflight, light and native selections overlap; their counts are
not added as unique coverage. The successful final aggregate does not relabel
earlier canceled or not-started groups as passes. No full-product completion,
general accelerator acceptance or hosted CI pass is claimed. Separate Mac
results and historical results below are not promoted to this frozen Linux
runtime's aggregate acceptance.

Historical checkpoint: Direct-main development, Linux x64 CPU execution evidence only. Status names are
literal: PASS is demonstrated, FAIL is a tested unmet requirement, BLOCKED needs
an external prerequisite/authorization, UNVERIFIED has not finished testing.
No aggregate below is a full-product completion claim.

## Historical 2026-10-10 integration checkpoint

Runtime source: `decf680d96ed92e28170580c09bf388c17a1ff22` (86-file source/lock closure), including bounded local placement and unlocked admission/durable dispatch. Complete local CI passed: 1,781 light tests, 319 frontend tests and all 309 native cases, with verified process cleanup. This does not establish native accelerator or GUI acceptance. See [current verification and remaining gates](CURRENT_VERIFICATION.md) for this checkpoint's installed-wheel replay, optional-profile/native tests, build, platform, provider and CI status.

The acceptance rows and narratives below are **historical evidence at their named checkpoints**. Their words "current" and "final" refer to those earlier tests; they do not establish acceptance of this source. In particular, earlier GUI passes are not current native GUI acceptance. This pointer does not relabel any historical test result.

## Historical 63-file integration checkpoint

Runtime source: `dee51909ee7f0e98c798492e0a865e967d60e400` (63-file source/lock closure). The preceding broad replay gate used da9b67c; current source-bound authoring, dataset binding and model release verification is listed in `CURRENT_VERIFICATION.md`. Work is directly on `main`; no new pull request is required.

Implemented since the earlier fixture snapshot: generic source-bound framework/harness components, immutable dataset versions and consumption lineage, scientifically compatible evaluation/promotion, live checkpoint evaluation and durable events, configurable IDE notifications, opt-in isolated W&B tracking, optional assistant setup, research watches and reviewed proposals, and native-target build/bootstrap selection.

Focused real tests establish independent image/forecast training, exact continuation state equality, export reload parity, checkpoint evaluation before training completion, offline W&B delivery, native MCP dataset/research operations and Codex CLI tool discovery. These do not establish native GUI acceptance, Claude authentication, online W&B transmission or remote billing enforcement. Final da9b67c independent installed-wheel replay passed for both fixtures: four real runs, four independent environments/stores, 31/31 comparison checks per fixture and exact hashes for six custom/ten pretrained outputs. See locally retained `evidence/portability/final-runtime/summary.json`. New raw evidence is retained locally pending publication approval; `CURRENT_VERIFICATION.md` records the concise status. The aggregate suite is recorded locally under `evidence/tests/`; historical evidence below is not automatically inherited by this checkpoint.

Fresh native IDE compilation initially failed with signal 9 while build caches occupied a RAM-backed temporary filesystem. The cache was moved to disk; the serial upstream client and extension compilation then completed with zero errors. Final Electron assembly subsequently passed, and the generated launcher reports the pinned version/commit/architecture. See `evidence/gui/current-build/`. Fresh native GUI acceptance remains a separate gate. Browser automation was separately blocked before page creation by Chromium socket restrictions. Neither is recorded as a GUI pass. Native macOS/Windows/ARM acceptance remains unverified.

## Historical tested snapshot (086908e)

Code - OSS1.105.0 /03c265b1adee71ac88f833e065f7bb956b60550a; Electron37.6.0;
Python3.12.14; native MCP SDK1.30.0; ZenML0.97.0; Torch2.9.0+cpu;
Transformers4.57.1. Extension versions and checksums are in the distribution lock.
Published runtime source checkpoint `086908e3eb20ff025bbd50108e6b6563855b7512`
pinned the prior 20-file source closure in the earlier examples. Corrected
clean replay and native File-menu GUI validation are tracked separately below.

| ID | Status | Test and evidence |
|---|---|---|
| T01 | PASS development build; zero-patch exception remains | Current source-built branded IDE launches with one documented File-menu composition import and one static menu module. Rebuild reported zero errors; exact-boundary/symlink tests pass. `evidence/gui/file-menu/build-manifest.json`, source integrity and compile log. Initial zero-patch proof remains historical. Pristine unattended bootstrap is still unverified because upstream dependency retrieval needed a documented workaround. |
| T02 | PASS | Actual IDE Python/basedpyright diagnostics, debugpy program, Jupyter output42, terminal and Git. `tests/gui/smoke.cjs`, `scripts/smoke_ide.sh`; `evidence/gui/file-menu-smoke/smoke-results.json` and fresh screenshots on the current rebuilt binary. The debugger stopped at a real breakpoint and evaluated answer=42 before continuing. |
| T03 | PASS | One model project owns one canonical recipe ID with immutable revisions; create/import/restore update the same identity. Legacy histories remain readable; cross-project access denied. `tests/domain/test_single_recipe.py`, final service contracts and actual backend integration. Six simultaneous migration opens repeated 100 times passed; startup BUSY retry tests include real processes. |
| T04 | PASS | Canonical recipe roundtrip/schema-version rejection/typed DAG/source and dependency validation. `tests/recipe/`; schema generated from same runtime contract. |
| T05 | PASS | Actual fixed-model graph authoring, typed arbitrary-Python node, complete five-node training/evaluation/model-export graph, and no model/recipe selectors or recipe-transfer workspace controls. 23 real DOM checks plus native Monaco pass. `evidence/gui/file-menu-verified/`. |
| T06 | PASS | Actual native Monaco save and GUI/external-MCP UUID/revision/digest equality, visible remote edits, native File-menu import/export with unchanged raw bytes. Concurrent MCP changes during import cannot be overwritten; export keeps its captured revision. `evidence/gui/file-menu-verified/workbench-host-results.json` and DOM report. |
| T07 | PASS | Stale revision conflict preserves local graph/JSON drafts and server data. Raw bundle tamper/duplicate/invalid-number input rejected without creating a revision. Native File commands also guard dirty source and project-context races; final real-menu proof remains separate. |
| T08 | PASS | Independent official SDK stdio client exposes 49 generated tools; seven immutable revisions retain one project recipe ID across authoring/import/restore and service restart. `evidence/mcp/single-model/`; tests cover read-only denial, stale writes and HTTP/MCP parity. |
| T09 | PASS | Scoped actor/read-only denial across mutation tools, audit records, project isolation and denied hidden paths. MCP/service/adversarial suites. |
| T10 | PASS | Actual GUI Run recipe with normal confirmation executed the startup-fixed shipping source. Native ZenML UUID 0b98377e-8d79-48f1-88f0-3b00aafae57d succeeded; GUI metrics, logs, model version and artifact IDs matched service. Three execution DOM checks plus host passed. `evidence/gui/file-menu-execution/`; service/MCP integration separately passed. |
| T11 | PASS | Consumed dataset artifact hash checked against upstream stage, tamper rejected. CPU/native suite plus clean-replay dataset equality. |
| T12 | PASS | Actual gated PyTorch width variants through native DAG; `test_native_custom_dag_variants_negative_and_models`; `native-custom-variants.json`. |
| T13 | PASS | Executed explicit KL coefficient/direction/frozen reference, manifest and training-contract checks, real loss/parameter effect; custom and pretrained suites. |
| T14 | PASS | Negative comparison plus deliberately failed native run and actual workerSIGKILL retained/queryable; native lifecycle JSON/logs. |
| T15 | PASS | Actual validation-based selection, promoted native model version and selected-parent checkpoint lineage; `native-pretrained-selection.json`, service integration decision records. |
| T16 | PASS | Deterministic model/optimizer/scheduler/Python+TorchRNG/dataloader parity after exact continuation; separate warm-start semantics. CPU and real cancellation/native-resume tests. |
| T17 | PASS | Running native worker and live descendants terminate before acknowledgement; real native metadata carries proof. ZenML static pipeline remains nativefailed, separately exposed effectivecancelled. `native-cancel-exact-resume.json`, adversarial mapping tests. Early pre-native cancellation and pending-cancel resume rejection also pass real-process tests; no native UUID is invented. |
| T18 | PASS | Independent MCP submits actual training; application service isSIGKILLed and restarted while run active; authoritative UUID/status/artifacts recovered. `tests/integration/test_mcp_training.py`, `evidence/backend/mcp-training.json`. |
| T19 | PASS | Both startup-fixed 086908e3 fixtures replayed through independently installed service/backend wheels, source roots, metadata/artifact stores and native runs. Each passed all 31 metric/hash/isolation/provenance checks; raw bundle import retained one recipe ID, preserved r1 and executed r2. `evidence/portability/*comparison.json`. Earlier pin results remain explicitly historical. |
| T20 | PASS | Actual OpenAlex paper metadata retrieval, explicit evidence/claim/hypothesis linked to real native run and decision; `tests/service/test_real_backend.py`, literature tests, `evidence/research/openalex-live.json`, service integration evidence. Retrieval is not scientific claim verification. |
| T21 | BLOCKED | Both exact Linux VSIXs installed and activated in the fork: Codex26.5908.31748 and Claude Code2.1.295. Both reach actual vendor sign-in screens. Earlier bundled-client probes connected to native MCP and Codex discovered the then-current 48 tools; current independent SDK validation covers 49 tools. This is not a new authenticated vendor-call pass. No authorized vendor session or paid inference is available, and the Codex thread/tool-call probe also hits a vendor sandbox mount-lock failure. Authenticated assistant MCP calls remain unverified. Missing API-proposal warnings are also recorded; engine-range compatibility alone is not a pass. `distribution/ASSISTANT_COMPATIBILITY.md` and `evidence/gui/distribution/` assistant report and actual sign-in screenshots. |
| T22 | BLOCKED | P5 expressly requires separate paid-provider authorization. Remote targets/spend fail closed; no provider invoice/cap enforcement claimed. |
| T23 | PASS | Inert import; tar/path/link/duplicate/collision/hash/schema/credentialURL/dependency rejection; untrusted execution denied without verified isolation. Recipe/security tests and portability helper tests. |
| T24 | PASS | Existing project was an independent read-only reference; no original-repository writes/training. Implementation uses separately authored fixtures; attached private evaluation archive is excluded. |

## Evidence interpretation

`evidence/` contains actual command output, native UUIDs, hashes and screenshots.
Historical unsuccessful build/test attempts are retained with their status;
a retry does not retroactively make their logs successful. Tests are not summed
across overlapping runs. The final aggregate transcript, clean replay and GUI
transcript are reported separately. Screenshots depict the actual running fork,
not a design mockup. Test credentials and private model material are excluded.

P0–P3 and P4 clean replay have working demonstrations. P4 exact authenticated
vendor-assistant calls remain blocked, so P0–P4 is not declared fully complete. Full production scope, signed multi-OS
releases, verified untrusted sandboxes, remote budget/accounting, richer research
monitoring, external evaluation judges and marketplace remain explicit follow-on
requirements, not implemented features.

## Aggregate validation record

Corrected runtime service/contract aggregate: **309 passed**, two Torch collection
modules skipped in the service interpreter, five integration cases deselected.
Those Torch cases run separately in the locked backend environment. Evidence:
`evidence/tests/single-model/full-script.log` and `evidence/tests/unit-service-mcp.xml`.
Three actual service/MCP/backend-mapping integration tests passed in the separate
integration run. Current-domain native lifecycle and CPU suites are recorded in
`evidence/backend/current-domain-native.*` and `cpu-fixtures-current-domain.*`.
These overlapping profiles are not summed into an invented aggregate count.

The first full correction run exposed genuine SQLite concurrent WAL startup
contention. The failure remains recorded, followed by a bounded startup fix,
19 focused tests, 100 six-opener migration races and the clean aggregate.
No test was weakened or skipped to hide that failure.

CI runs 37947375390 and 37949973281 passed the earlier implementation. They do
not establish the owner-corrected UI or final source. Runtime-pin preparation
commits intentionally skip CI while shipping manifests are being repinned;
the coherent correction publication must run both CI jobs normally.

Corrected source CI [37954882683](https://github.com/greghavens/modelbuilder/actions/runs/37954882683) passed both contracts and CPU lifecycle at `8ac10dfba4da0681566515c92c66e7e3924d01a4`. Later GUI-driver timing/evidence publication is checked separately.
