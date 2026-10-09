# Current verification and remaining gates

Runtime source checkpoint: `dee51909ee7f0e98c798492e0a865e967d60e400` (63-file closure). This is an implementation checkpoint, not full-product completion. Work is directly on `main`; no pull request is required.

## Previously demonstrated baseline

- Source-built Linux x64 Code - OSS distribution compiles successfully using the pinned upstream tasks; the assembled launcher reports the expected version.
- Frontend TypeScript checks, 125 unit/host tests, and seven real service/external-MCP parity tests pass.
- The current service test selection passed 793 tests, with nine skips and 15 deselected integration tests, while the optional backend path was explicitly unavailable.
- Independent source-bound image and forecasting models train, evaluate, export and reload; exact continuation preserves model, optimizer, scheduler and random state.
- An official MCP client starts real native ZenML training. Twelve checkpoint evaluations complete, with early results and actual offline W&B acknowledgement occurring before later checkpoints are saved. HTTP and MCP expose matching results and provenance.
- At the final da9b67c runtime checkpoint, each of the two shipping fixtures passed 31 independent installed-wheel replay comparisons, including exact output hashes and separate environments/stores.
- The preceding da9b67c checkpoint passed nine service/native integration tests and 89 CPU/component/evaluation/tracking tests. The current service-only safety follow-up additionally passed a real three-pipeline warm/exact-state regression and 24 packaging/source-closure tests.
- Three cancellation/trust fixture regressions found by the aggregate run were repaired. The da9b67c two-file rerun passed all eight tests, including no leftover node deadline and no native hook warning. The original failing aggregate is not presented as a pass.

The safety follow-up fixes inherited behavior: warm starts now use edited current recipe settings while exact continuation retains parent settings; authored exports prove packaged bytes match their declared Git commit. Git-free inert bundle transport remains supported.

## Current source-bound authoring and model release verification

This batch is based on previously published main `732dd5014d84d737a97cb2e4664cc24c8d3b771d` and keeps the inherited warm-start and export-provenance fixes. It adds approved per-project source bindings, explicit reviewed source pinning, private recipe registry actions, durable cost diagnostics with production remote execution disabled, portable dataset reads and immutable dataset binding, typed component authoring, and native model comparison, decision, promotion and Save workflows.

- Combined service, contract, security and portability selection: **1,107 passed, 16 skipped, 18 deselected**. Shipping examples are repinned to the actual source commit above.
- The final source checkpoint passed both the official MCP→HTTP→ZenML→live checkpoint evaluation→offline W&B integration and the multi-output native pipeline regression. Evaluation results and real offline tracking acknowledgements arrived before later checkpoints. The final source also passed four clean installed-wheel runs, 62/62 replay comparisons and 124/124 portability checks. All eight custom and twelve pretrained output hashes matched exactly, with zero export reload difference; all 63 source files and installed wheel bytes were reverified. Ten multi-output ABI checks also passed against a fresh installed backend wheel outside the checkout.
- Multi-output source components and Python nodes now use the pinned ZenML ABI correctly, reject wrong output shapes, and retain all authoritative artifacts. Ten pinned-SDK regression tests and a real two-node native pipeline pass.
- Combined frontend TypeScript checks and **239 unit/host tests** pass. Real loopback HTTP plus external MCP parity, typed workspace/private registry actions, and source-pin native-dialog helpers pass; these use dialog doubles and do not establish GUI acceptance.
- Configured native dataset execution through external MCP and HTTP passes, including preserving the configured virtual-environment interpreter path. Source pinning to a real committed checkout and independent native model execution pass.
- Final combined native release and bound-source workflows passed: real independent runs, comparison, decision, promotion, saved-package fresh reload with zero prediction difference, immutable source pinning and exact resume after rebinding. The newly created component also passed real HTTP and native execution.
- The backend runtime wheel is tested in a fresh process without access to the source checkout, including the new dataset, cost, source and export module closure.

This remains an implementation checkpoint. Later explicit checkpoint-subgraph execution and recurring research scheduling are outside this batch. Raw machine reports and environment metadata are omitted.

## Current limits

- Fresh Linux native compilation passed all pinned upstream tasks, Electron assembly and launcher version verification after a prior SIGKILL retry. The cached File-menu upgrade guard passed 22 tests without permitting arbitrary upstream edits. This is a compile/launcher pass, not GUI acceptance.

- Fresh native GUI and notification acceptance is blocked: the execution environment has no usable display and rejects local Unix-domain sockets. Compilation and frontend tests do not substitute for actual window interaction.
- macOS, Windows and ARM native runtime acceptance has not been performed here. Current model-package reading requires POSIX handle-relative file access; a Windows implementation is still outstanding. Dataset Windows support has a separate implementation but remains unverified on a Windows host.
- Online W&B, authenticated Claude/Codex inference, remote-provider execution/billing enforcement, and cloud spending have not been authorized or claimed as passed.
- New raw diagnostic evidence is retained locally but omitted from this publication pending approval to share its environment metadata. Paths cited in other documents may therefore refer to locally retained evidence rather than committed files. Historical committed evidence is explicitly historical.
- The first remote contracts job exposed unit tests that accidentally required the optional backend environment. Those pure admission tests now use their own service interpreter and pass with the backend path explicitly unavailable. Replacement GitHub Actions run `38003492895` for published main `732dd5014d84d737a97cb2e4664cc24c8d3b771d` succeeded in both contracts and CPU-lifecycle jobs. The current batch requires its own CI after publication.

The checkpoint evaluation design and API are documented in `design/LIVE_CHECKPOINT_EVALUATION.md`; all current tests are provided as reproducible source.
