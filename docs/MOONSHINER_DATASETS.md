# Moonshiner dataset integration

## Implemented boundary

`modelbuilder.moonshiner.prepare-dataset` is a source-bound dataset component in
the existing recipe and native ZenML lifecycle. It runs the installed Moonshiner
local composition CLI in the foreground, validates the output and returns a
`Path` with a sealed `dataset.manifest.json`. It introduces no scheduler, trainer,
provider client, workspace flow, installation or automatic dataset registration.

The reviewed upstream contract is Moonshiner **0.8.23**, commit
[`7abb66ac5328db6a5d5f3990cf85ddc40310f0b8`](https://github.com/greghavens/moonshiner/commit/7abb66ac5328db6a5d5f3990cf85ddc40310f0b8):

- [`src/moonshiner_app/cli.py`](https://github.com/greghavens/moonshiner/blob/7abb66ac5328db6a5d5f3990cf85ddc40310f0b8/src/moonshiner_app/cli.py) is the installed console entry point. It copies its bundled corpus into project-local state; it is not invoked during discovery/planning.
- [`src/dataset_prep.py`](https://github.com/greghavens/moonshiner/blob/7abb66ac5328db6a5d5f3990cf85ddc40310f0b8/src/dataset_prep.py) implements `dataset compose --source LOCAL --out LOCAL --seed N`. With one local source, no weighting/target-token/tokenizer options, it deduplicates and deterministically shuffles rows, normalizes their messages, and emits a version-2 sidecar with output SHA256, row count and source identities.
- [`src/canonical_dataset.py`](https://github.com/greghavens/moonshiner/blob/7abb66ac5328db6a5d5f3990cf85ddc40310f0b8/src/canonical_dataset.py) describes canonical messages and `target_message_index`. Compose does **not** preserve this index, so Modelbuilder restores it only after checking the exact source row and full message sequence.

The fixed invocation has no executable/argv/config-path overrides. No `run`,
`setup`, `publish`, `--detach`, `--resume`, remote source, tokenizer, or arbitrary
flags are exposed. The worker uses isolated Python imports, a private HOME/HF
cache and configuration, no inherited credentials, disabled trace/seed queues,
no publication target, and Python audit guards denying socket creation, network
lookups/connections and child-command events. Only local `socket.gethostname`
is allowed because the audited privacy scanner uses it to detect identifying text. These are defense-in-depth controls for reviewed local code,
not a hostile-code sandbox or OS-level network isolation.

## Explicit review and identity

1. Install/review Moonshiner separately in the operator-selected runtime. The
   adapter never installs, signs in, downloads a tokenizer or obtains credentials.
   The existing `modelbuilder.runner/v1` environment route can select an isolated
   installed Python environment; a default runtime is also supported.
2. Inspect `moonshiner_process.installation_identity()` in that runtime. This
   reads package bytes and distribution metadata without importing Moonshiner.
   Pin its exact version, installed source SHA256 and existing runner environment
   digest in `parameters.installation`. The reviewed upstream commit identifies
   compatibility evidence; it is not proof of a wheel's Git provenance. The
   operator must verify the installed package's origin when reviewing its digest.
3. Pin `examples/moonshiner/adapter.py` in the project's existing trusted source
   closure. The native runtime still verifies source/dependency/environment and
   current project-owned rights policies before dispatch.
4. Supply one reviewed native `DatasetArtifact` `traces` input from a train split,
   and its exact canonical `traces_digest`. Rows must explicitly have `split:
   "train"`, teacher model/runtime, source trajectory ID/SHA256, canonical messages,
   and a final `target_message_index`. Declare the teacher/model as a rights-bearing
   admitted dependency and name its exact `rights_parent_id`. A model-name string
   is never permission to use teacher output.
5. Supply explicit record/byte/time limits, `max_cost_usd: 0`, a seed, a separate
   benchmark identity, review note, output root ID, `train-only` split policy and
   `final-assistant-content-only` label policy. Positive-cost/provider modes are
   unavailable. The CLI remains within the native worker process group; timeout,
   cancel-file, process exit, bounded logs/output and descendant cleanup are checked.

The v1 process boundary is enabled on Linux only. A maximum of 1,024 input/output records, 16 MiB serialized records, and 300 seconds
is supported. These are record/process bounds, not a bound on copying the package's
bundled corpus into its private working directory. The adapter checks the installed
source/environment again before accepting completed output.

## Target, split and rights semantics

Every input row must end at an explicitly indexed, nonempty assistant **text**
target. Tool-call targets, absent indices, earlier assistant targets, mismatched
teachers, and validation/test rows fail closed. Intermediate assistant, tool,
user, system and reasoning content remains context. Output records carry:

- the unchanged canonical `messages` and verified `target_message_index`;
- `target_text`, taken only from the final message's `content`, and its SHA256;
- `message_loss_mask`, a **message-level intent** with only that index set to 1;
- original trajectory, teacher and full source-row identities.

No tokenizer runs and no token-level labels are produced. A compatible
source-owned conversation consumer must validate this metadata, format the
conversation, and mask every non-target token (including final-message reasoning
and framing tokens as applicable). The built-in plain-text Transformers Trainer
rejects these structured target rows rather than silently training on all tokens.
This integration does not certify an arbitrary consumer's loss masking.

Compose may reorder or deduplicate exact conversations. It must not replace,
rewrite or sanitize the reviewed message sequence during this step: changed
messages require a separate review. The prepared dataset has one train split,
no fabricated labels, validation/test split, quality score or evaluation evidence.

Teacher/model, original data and all declared parent rights are inherited through
the existing generated-artifact seal. Missing obligations remain unresolved;
`license` is an output label, never a replacement for parent restrictions. The
existing rights admission checks remain authoritative for subsequent use.

The native artifact is a prepared registration proposal. Once the operator maps
its directory to the declared dataset root, use the existing `dataset.validate`,
`dataset.register`, reviewed output policy and `dataset.bind` operations. Those
operations preserve inherited rights. Nothing enters training automatically.

## Provider distillation is an open acceptance gate

Upstream [`trace_pipeline.py`](https://github.com/greghavens/moonshiner/blob/7abb66ac5328db6a5d5f3990cf85ddc40310f0b8/src/trace_pipeline.py)
provides `run --limit N --max-attempts N --workers N --yes`, but explicitly has
**no run-wide model-call ceiling**. Real execution can bootstrap configured HF
state and launch an independent publisher; runtime/toolchain setup has additional
side effects. A seed limit is not a hard spend limit. The catalog therefore does
not expose a runnable trace/provider adapter. Local composition of existing
accepted traces is not new teacher generation or real-provider distillation.

The official 0.8.23 wheel was hash-verified, installed in an isolated environment
and exercised on owned local fixtures. Two real CLI compositions produced
identical dataset and sealed-manifest hashes across independent private
directories. An additional opt-in test ran genuine composition through the
existing native process ABI, verified artifact handoff and confirmed the direct
child was reaped with no remaining descendants. These are local CLI/native-body
checks, not a complete service-to-ZenML/GUI end-to-end acceptance.

The default regression suite uses an owned deterministic CLI double for malformed
output, timeout/cancellation, descendant cleanup and rights checks. It does not
stand in for provider acceptance. The separately gated installed test is
`tests/components/test_moonshiner.py::test_official_installed_compose_through_native_process_abi`;
set `MODELBUILDER_MOONSHINER_TEST_PYTHON` only to an explicitly reviewed isolated
interpreter containing the supported official release. It performs no install.

The initial real run exposed our overly broad audit guard blocking the local
privacy scanner's `socket.gethostname`. That failure was retained; the corrected
guard permits only that non-network event. Regression tests confirm local
hostname inspection works while socket creation, host/address lookup and child
commands remain blocked. Separate real installed-CLI checks enforced a 0.1-second deadline and cancelled
during bundled-corpus copying. Both published no prepared artifact and left no
child process. This covers foreground startup/copy cancellation, not long-running
provider tracing. Complete service-to-ZenML/GUI lifecycle integration remains
additional acceptance coverage; native-body checks must not be described as that pass.

Before provider tracing can be enabled, review and implement a supported upstream
API/CLI contract with enforceable whole-run call/token/spend caps and no automatic
bootstrap, installation, auth, publication or detached workers. Bind explicit
provider/model/data permissions and approvals to the exact config, endpoint,
limits and current rights policies; then test real bounded generation, cost
reconciliation and native descendant cleanup with explicit authorization. No
provider, paid, network, GPU, macOS/Windows or GUI acceptance is claimed here.

### Released-version selection, 2026-10-13

The first inspected HEAD was unreleased 0.8.24 at
`fb4d4ed13a206b0676f6c41881ad3831a51326c5`. Its exact PyPI and GitHub-release
endpoints returned 404. No existing Moonshiner installation was found.
The live [PyPI metadata](https://pypi.org/pypi/moonshiner/json) identified the
official project as `greghavens/moonshiner` and reported 0.8.23. Its wheel SHA256
`ac1f2708e90e71addeb3e27cf17117c55efb8515f3d49c2edb13e373c88bb5c6` matched the
[official v0.8.23 GitHub release](https://github.com/greghavens/moonshiner/releases/tag/v0.8.23).

The integration intentionally targets that released version. The tag resolves to
`7abb66ac5328db6a5d5f3990cf85ddc40310f0b8`. The compose implementation, canonical
row helpers, installed CLI entry and configuration module have identical Git
blobs in the released tag and initially inspected HEAD. Shared `common.py` changes
affect seed verification, outside this compose path. The downloaded wheel's
local-compose source files were checked against the released GitHub source.
Unreleased 0.8.24 remains unsupported and is rejected by the version gate.
