# Installed streaming sampling service acceptance driver

`installed_streaming_driver.py` is a standalone optional driver for the existing
`reproduce_installed_acceptance.py` additional-driver hook. It uses real public
service operations, the registered streaming dataset imports, and the existing
Trainer v1 train/evaluate/export adapters. It is not another runner or optimizer.

## Prepared scope; execution remains unrun

The planned acceptance is Python 3.12 Linux x86-64 CPU float32 with the existing
hash-locked Trainer overlay. Both CSV and JSONL have one 120,000-row training
source, above the actual 100,000-row eager ceiling, plus two held-out rows. All
rows are generated locally; the ten-token tokenizer and tiny one-layer BERT are
authored locally. No public pretrained weights or upstream data are downloaded.

Each format tests proportional and equal sampling with an explicit fraction
0.0001, seed, two required strata, minimum 1, and zero share-error tolerance.
The source mix is 75/25: independent known quotas are 9/3 and 6/6. A bounded
standalone rank oracle retains only the top candidates, never the full source.
The driver compares all twelve selected IDs, row bodies, complete six-field
ordered lineage, and exact canonical selection/policy digests. It checks the
held-out explicit order, service restart, original source immutability, a stricter
restarted cache cap, owned source/index tamper copies, and preexisting portable
compilation cancellation. It does not claim in-flight ingestion/ranking cancellation.

The existing five-node DAG runs JSONL/proportional and CSV/equal, sequentially.
Each executes two real Trainer optimizer steps with batch size 2. It compares
actual checkpoint consumption against the independent first four selected rows;
twelve selected rows must never be reported as twelve consumed. This specifically
excludes DataLoader lookahead/prefetch from consumption. Native finite weights
must change, and held-out item-addressed evaluation plus fresh-process export
verification must refer to the selected checkpoint. A separate run pauses after
native step1 checkpoint publication, then public cancellation must retain exactly
two consumed rows and confirm owned native termination.

This driver does not claim Trainer streaming exact-state continuation, execute
warm start, exercise Parquet, establish cross-role checkpoint byte equality,
or replace installed/GUI acceptance. Its source/contract review and lightweight
tests are not installed execution evidence. The generic harness must retain
`REPORTED_UNVERIFIED` until a workload-specific reviewer checks both roles' actual
receipts. Existing strict replay comparison remains unchanged.

## Isolation and public rights

The harness copies and hashes this single script. The controller uses the
supplied installed service interpreter with `-I -B`; backend asset creation and
tensor inspection invoke the same script under the distinct supplied backend
interpreter with `-I -B`. It never imports checkout tests or inserts checkout paths.
A fresh HOME and TMPDIR live inside each `driver_work`; credentials/provider
configuration are dropped and CPU threads bounded. This is trusted offline test
isolation, not an OS sandbox for arbitrary code.

Source adapter, model assets, and each data encoding receive actual public local
license inspection and review of their exact fixture-only terms. Dataset/model/
source bindings use public operations. The unchanged adapter reuses its genuine
first review across both cases, preserving source provenance. Per-case exports
must retain the actual model/data/source policy IDs. No policies are fabricated,
written directly into SQLite, inherited from the synthetic replay, or erased.

The tiny adapter only delegates the production functions and adds a test-owned
checkpoint callback. It forwards native publication before marking readiness and
raises InterruptedError when public cancellation arrives, preventing the v1
callback's pre-snapshot cancellation check from admitting an additional step.
Production methods and classes are not reassigned.

Every native service lives at `driver_work/state/application.sqlite3` with its
normal `backend/jobs` directory. Operations are appended durably before dispatch
and after results/errors. A driver-wide finally discovers all owned jobs,
including a submit that failed before returning, uses public status/cancel,
requires explicit worker/supervisor/process-group absence and confirmed native
termination, and continues after independent cleanup failures. The coordinator's
existing second cleanup pass remains active. No raw PID kill or original artifact
deletion is used by this driver.

## Execution prerequisites and invocation

Do not run while source or bytecode can change, during another native workload,
or before explicit execution authorization and capacity verification. Four fresh
copy installations previously required about 2.5 GiB allocated; source/index and
native job copies add materially to that. Reserve at least 5 GiB on disk-backed
storage, then separately check current available RAM and logs. More may be needed
for concurrent processes. A tmpfs work area consumes RAM as well as filesystem
quota. Do not delete any previous run to make room without separate permission.

The full existing service/backend/Trainer dependency locks must be cached offline.
Git and uv must be available. No Parquet overlay is needed for this scoped driver.
The existing harness validates current shipping pins rather than hardcoding a
historical runtime. No source, locks, shipping recipes, runner, or production code
are changed by these three test-tool files.

```bash
ROOT=/absolute/path/to/modelbuilder
WORK=/absolute/new/path/outside/the/checkout
"$ROOT/.venv/bin/python" -I -B \
  "$ROOT/tests/portability/reproduce_installed_acceptance.py" "$WORK" \
  --root "$ROOT" --python "$ROOT/.venv/bin/python" \
  --cache-dir "$ROOT/.cache/uv" --command-timeout 2400 \
  --driver "$ROOT/tests/portability/installed_streaming_driver.py" \
  --driver-id streaming-sampling-service-v1 > "$WORK.launch.log" 2>&1
```

WORK must not exist. Its parent must exist. Execute through the harness only,
never against an unbaselined or reused environment. The driver reserves 120
seconds for cleanup; the coordinator has its own cleanup budget. A failure,
timeout, missing native evidence or unconfirmed termination cannot become PASS.

## Lightweight preparation checks

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q \
  tests/portability/test_installed_streaming_driver.py
.venv/bin/ruff check tests/portability/installed_streaming_driver.py \
  tests/portability/test_installed_streaming_driver.py
```

These checks use 24-row local datasets, inert helper execution, and mocked tensor/
native behavior. They do not install dependencies, create fixture commits, submit
native jobs, generate the large inputs, or establish installed acceptance.
