# Current verification and remaining gates

Runtime source checkpoint: `da9b67c361088aa49fefb7dcea1ab53efa805ce8`. This is an implementation checkpoint, not full-product completion. Work is directly on `main`; no pull request is required.

## Demonstrated locally

- Source-built Linux x64 Code - OSS distribution compiles successfully using the pinned upstream tasks; the assembled launcher reports the expected version.
- Frontend TypeScript checks, 125 unit/host tests, and seven real service/external-MCP parity tests pass.
- The final source service test selection passed 771 tests, with eight skips and 15 deselected integration tests.
- Independent source-bound image and forecasting models train, evaluate, export and reload; exact continuation preserves model, optimizer, scheduler and random state.
- An official MCP client starts real native ZenML training. Twelve checkpoint evaluations complete, with early results and actual offline W&B acknowledgement occurring before later checkpoints are saved. HTTP and MCP expose matching results and provenance.
- At the preceding runtime checkpoint, each of the two shipping fixtures passed 31 independent installed-wheel replay comparisons, including exact output hashes and separate environments/stores.
- Final-source service/native integration passed nine tests; the separate CPU/component/evaluation/tracking selection passed 89 tests.
- Three cancellation/trust fixture regressions found by the aggregate run were repaired. A focused two-file rerun passed eight tests; final-source integration verification is in progress. The original failing aggregate is not presented as a pass.

## Current limits

- Fresh native GUI and notification acceptance is blocked: the execution environment has no usable display and rejects local Unix-domain sockets. Compilation and frontend tests do not substitute for actual window interaction.
- macOS, Windows and ARM native runtime acceptance has not been performed here. Integration-specific platform restrictions are documented separately.
- Online W&B, authenticated Claude/Codex inference, remote-provider execution/billing enforcement, and cloud spending have not been authorized or claimed as passed.
- New raw diagnostic evidence is retained locally but omitted from this publication pending approval to share its environment metadata. Paths cited in other documents may therefore refer to locally retained evidence rather than committed files. Historical committed evidence is explicitly historical.
- CI for the final published commit must be checked separately. Local tests do not establish remote CI success.

The checkpoint evaluation design and API are documented in `design/LIVE_CHECKPOINT_EVALUATION.md`; all current tests are provided as reproducible source.
