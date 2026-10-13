# Installed static Trainer service acceptance driver

`installed_trainer_driver.py` is an optional standalone driver for the reviewed
`reproduce_installed_acceptance.py` hook. It calls the existing public service
operations, native backend, and Trainer adapters. It does not implement an
optimizer, training loop, launcher, or alternate execution runner.

## Scope and status

The driver is for the existing Python 3.12 Linux x86-64 installed service/backend
profiles with the hash-locked Trainer overlay, CPU float32 eager registered
splits, batch size 2, gradient accumulation 1, and one native worker at a time.
It authors a tiny random BERT classifier (8 hidden units, one layer, two heads)
with nonzero dropout, a ten-token tokenizer, and seven invented weather rows.
There are four training rows and two held-out rows. Nothing is downloaded.

This tooling has lightweight contract coverage and independent source review.
It has **not run installed/native acceptance**. Contract tests mock model
creation and tensor libraries; their public rights test uses actual local
service APIs and inert bytes without submitting any job.

The planned static DAG has exactly five nodes: two registered dataset imports,
Trainer v2, evaluation, and export. It tests restart continuation from a native
completed-epoch checkpoint. It does not test streaming, whole-loop resume, GPUs,
other training adapters, cross-host equality, pretrained rights, or GUI behavior.
Trainer v2 streaming exact-state continuation remains unsupported.

## Integration and invocation

Integrate the three added files without modifying runtime code, requirements,
shipping pins, or existing receipts. Keep the existing harness's strict
comparison semantics. Freeze the checkout and stop competing native workloads.
Recheck actual disk and available RAM immediately before execution. Four fresh
copy installations previously required about 2.50 GB allocated, before workload
artifacts. Reserve at least 3 GiB on the selected work filesystem, with additional
headroom appropriate to concurrent processes. A tmpfs work directory consumes
RAM as well as its filesystem quota; do not infer safe native memory headroom
from disk space. The four tmpfs environments alone retain about 2.5 GB of RAM; native
process memory needs separate headroom. Prefer adequate disk-backed capacity; do
not remove any previous run without separate authorization.

```bash
set -euo pipefail
ROOT=/absolute/path/to/modelbuilder
WORK=/absolute/new/work/path/outside/the/checkout
"$ROOT/.venv/bin/python" -I -B \
  "$ROOT/tests/portability/reproduce_installed_acceptance.py" "$WORK" \
  --root "$ROOT" --python "$ROOT/.venv/bin/python" \
  --cache-dir "$ROOT/.cache/uv" --command-timeout 2400 \
  --driver "$ROOT/tests/portability/installed_trainer_driver.py" \
  --driver-id trainer-static-service-v1 > "$WORK.launch.log" 2>&1
```

`WORK` must not exist; its parent must exist for log redirection. Git and uv must
be available. The complete service/backend/Trainer hash-lock dependencies must
already be in the offline cache. The harness derives the **current declared
runtime**, verifies it against committed bytes, builds fresh wheels, and creates
four independent copy-installed environments. Do not substitute a historical
runtime pin, reuse a previous acceptance's mutable data, or invoke the driver
against an unbaselined environment. No additional dependency installation beyond
the existing harness's approved locked environments is required.

The default 1,800-second per-command budget is also supported; the command above
allows 2,400 seconds per command and reserves driver and coordinator cleanup
budgets. Monitor disk, RAM, logs, and completion while it runs. Any timeout,
missing native observation, wrong identity, unexpected gate, or unconfirmed
termination fails closed. Never treat a killed or incomplete command as PASS.

## Evidence and assertions

The controller runs under the supplied installed service Python with `-I -B`.
Two modes of the same copied, hash-pinned script use the supplied backend Python
with `-I -B` to create assets and compare native state. The service never imports
Torch/Transformers. Each helper checks its exact executable, own environment and
module origin; the controller rejects aliased service/backend prefixes.

Model assets, dataset bytes, and committed adapter source each receive a genuine
public `hub.license.local.inspect` and `hub.license.review`. Model registration,
asset binding, dataset policy binding, and source policy binding use public
operations. Explicit fixture-only terms and usage context authorize these
self-authored bytes only. No reviewed policy is fabricated, inserted directly
into SQLite, copied from another run, or inferred from a license label. All
three actual policy identities must reach the export rights envelope.

The immutable Git-pinned adapter delegates the production training/evaluation/
export functions unchanged. A test-owned callback waits after completed epoch 2
until public cancellation arrives. A prewritten control file removes the
submit-to-marker race. The callback has a bounded timeout. Python profiling
observes actual `Trainer.train` and `AdamW.step` entry; no production method is
reassigned. Profiling is restored in `finally`, including exceptions. Each
positive run must calibrate the observer: exactly one Trainer entry and 8/4/4
optimizer steps for uninterrupted/cancelled/resumed runs.

For each independent origin/replay role, successful acceptance requires:

- Public `run.submit`, completed-epoch synchronization, `run.cancel`, checkpoint
  discovery, and `run.resume(mode=exact_state)` using the retained parent contract
- Bit-exact model tensors, optimizer, scheduler and Python/NumPy/Torch RNG state
  between uninterrupted and fresh-process resumed runs; final weights must differ
  from authored initialization
- Eight optimizer steps over the original four-epoch scheduler horizon; complete
  per-epoch held-out metric history and exact actually consumed row/content order,
  including sampler reshuffling and an immutable parent history prefix
- Native Trainer step/epoch/horizon and scientific evaluation history equality;
  wall-clock throughput and process-local timing fields are not equality claims
- Correct parent and current execution subjects, exact continuation label, original
  parent request bytes, and original optimizer options despite a newer editable
  recipe revision with a different learning rate
- Held-out item-addressed evaluation records for the selected final checkpoint;
  export of that exact checkpoint; equal exported weights, fresh-process export
  prediction parity, and successful public inspect/package operations
- Read-only/untrusted requests rejected before any job is created; partial native
  Trainer state, changed original horizon, and missing optimizer rejected with the
  specific expected validation before Trainer/optimizer entry or new checkpoint
  publication. Only owned negative copies/archives are changed; the original
  parent archive is retained separately and restored byte-for-byte in `finally`
- Final source/assets/data immutability and confirmed owned native termination

The operation transcript is durably appended before dispatch and after each
result/error. Raw native requests, checkpoints, archives, logs and stores remain
under each fresh `driver_work`. Final receipts include helper identities and
native state inventories, public review provenance, scientific assertions,
negative evidence, cleanup, and a complete final file inventory.

Driver-wide `finally` scans every owned job directory, including a submit that
failed before returning its identity. Cleanup validates request/project ownership,
uses existing `run.status`/`run.cancel`, and requires terminal state plus explicit
worker, supervisor and process-group absence and confirmed native termination.
It attempts remaining jobs after an independent failure. The coordinator has a
second bounded installed-service cleanup pass. No raw PID kill or deletion is
used. Failure preserves original diagnostics and cleanup evidence and cannot
produce a PASS result.

The generic harness intentionally records valid driver results as
`REPORTED_UNVERIFIED`; this driver does not change that contract. An actual run
needs workload-specific review of both roles' scientific, rights, identity and
cleanup receipts before installed Trainer acceptance is claimed. The custom
synthetic replay's strict comparator must still report its known rights-only
**FAIL**, with the separate structural rights audit. No normalization or silent
relabeling of strict equality is permitted.

## Lightweight tests only

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q \
  tests/portability/test_installed_trainer_driver.py
.venv/bin/ruff check tests/portability/installed_trainer_driver.py \
  tests/portability/test_installed_trainer_driver.py
```

These tests do not run native workloads, import real training libraries, install
packages, or provide installed-runtime acceptance evidence.
