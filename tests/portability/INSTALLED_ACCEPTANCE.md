# Frozen installed-runtime acceptance

`reproduce_installed_acceptance.py` is test tooling around the existing
`scripts/replay_bundle.py` public-operation replay. It does not implement another
training runner. It supports Python 3.12 on Linux x86-64 CPU, a local offline wheel
cache, four fresh noneditable environments, and the self-authored custom-PyTorch
fixture. Do not run it concurrently with source changes or tests that write
checkout bytecode: the frozen checkout and bytecode checks intentionally fail.

## Run after integration and source repinning

```bash
set -euo pipefail
ROOT=/absolute/path/to/modelbuilder
WORK=/absolute/new/path/outside/the/checkout
PY="$ROOT/.venv/bin/python"
"$PY" -I -B "$ROOT/tests/portability/reproduce_installed_acceptance.py" \
  "$WORK" --root "$ROOT" --python "$PY" --cache-dir "$ROOT/.cache/uv" \
  > "$WORK.launch.log" 2>&1
cat "$WORK/evidence/summary.json"
```

`WORK` must not exist. Its parent must exist for the redirected launch log.
The helper derives the runtime commit from both current shipping recipes, checks
exact inventory equality, and verifies every byte against both its hash and
`git show <declared-commit>:<path>`. It records HEAD separately. Never substitute
the old 0e82548/6323bfd reproduction scripts or a historical hardcoded pin.
No production source, shipping recipe, existing evidence or repository commit is
changed. The temporary synthetic author commit adds only test-owned fixture terms;
the published runtime commit and the synthetic transport commit remain distinct.

The coordinator must use `-B`; all installed Python invocations use `-I -B`.
`-I` ignores `PYTHONDONTWRITEBYTECODE`, so the explicit flag matters. Child
environments drop inherited credentials, PYTHONPATH, VIRTUAL_ENV, tracking and
provider configuration, use a new HOME and offline flags, and bound CPU threads.
These are local trusted test fixtures, not an OS/network sandbox for untrusted
code. No provider/cloud workload or asset download is authorized by this command.

Each environment is synchronized separately from its frozen hash lock. Dependency
files use copy installation rather than shared hardlinks. The Trainer overlay is
included. Additional pinned backend overlays may be selected explicitly:

```bash
# Add to the invocation only for a workload requiring Parquet:
--backend-overlay requirements/profiles/parquet-linux-x64.lock
```

An offline cache miss, unsupported lock entry, conflicting overlay or unexpected
installed distribution is a failure. Do not bypass it with unrecorded dependencies.

## Evidence and strict comparison

The ordinary installation baseline is captured after all four installations and
before any native replay or model import. There is no warmup or precompilation.
The full environments, including dependencies, application files, metadata,
bytecode, executable wrappers and symlink identities, are inventoried before and
after. Contained directory symlinks such as `lib64 -> lib` are recorded without
double traversal. External directory symlinks fail closed.

On both origin and replay, service and backend wheels must have valid complete
RECORD rows. Every payload member must exist with its exact bytes; the application
member set must match exactly. Installed RECORD is separately required and
validated for unique complete membership, every file's SHA256/size, and its own
empty self-digest. Generated metadata is limited to INSTALLER, REQUESTED,
direct_url.json and uv_cache.json; generated bin wrappers must be declared in the
wheel's entry_points.txt. Extra or unrecorded distribution metadata is rejected.
Actual module identities, executable, prefix, sys.path,
PYTHONPATH and .pth entries are checked against each own site-packages root.
Missing wheel members, extra application files, editable hooks and source or
opposite-environment imports cannot satisfy the audit. Distribution versions must
exactly match the selected dependency locks plus the application wheel.

The custom fixture follows prepare -> public synthetic review -> plan -> submit
and a same-identity retry, through the existing replay CLI. Each role retains its
original public review sequence, distinct policy ID/time, service records, native
request/state, one job identity and independently owned native store. Both workers
must be reaped with confirmed process-group termination.

`strict-comparison.json` must remain **FAIL**, with only `output_hashes_equal`
false. Seven scientific files match byte-for-byte. The three original rights-
bearing manifests are audited by the existing `audit_review_provenance` helper;
only structural differences under `/rights` are permitted. Originals, original
hashes, exact JSON text, policy IDs and timestamps are preserved. Nothing is
normalized or rewritten to manufacture strict equality.

`summary.json` may report `PASS_SYNTHETIC_INSTALLED_REPLAY`; that scoped result is
not strict all-artifact equality, native installer/GUI acceptance, public
pretrained rights approval, or Trainer/streaming acceptance. The final
`receipt-sha256.json` seals all JSON receipts, command logs and copied test tools.

Once the ordinary-installation baseline exists, execution/verification failure
handling independently captures all available after-images and errors, preserves
the original failure, and never emits a success summary. Earlier setup/installation
failures retain command/failure receipts without claiming an integrity baseline.
Owned native jobs are discovered under the role's fixture/driver work roots and
reconciled with existing installed `run.status`/`run.cancel` service operations.
Cleanup is bounded and uses the backend's own birth-identity-safe termination;
the harness never kills a raw PID. Unconfirmed cleanup is a failure with retained
job/state evidence and needs operator attention, not a claimed clean stop.

## Supported additional-driver hook

An explicitly supplied standalone test driver can use the same installed
interpreters and immutability boundary:

```bash
# Append to the main invocation; this does not select or implement a workload:
--driver /absolute/path/to/reviewed_driver.py --driver-id trainer-service-v1
```

The selected script is copied and hashed before execution. For each role it is
invoked as:

```text
<role-service>/bin/python -I -B <copied-driver.py>
  --acceptance-context <role-driver-context.json>
  --output <role-driver>/result.json
```

Context schema: `modelbuilder.installed-acceptance-context/v1`. It provides role,
driver ID, published runtime commit, driver hash, Git-free source root, exact
service/backend interpreter paths, fresh `driver_work`, and a timeout budget.
The driver must place each service data directory beneath `driver_work`, using the
normal `application.sqlite3` plus `backend/jobs` layout so independent cleanup can
discover every submitted job, including a submission interrupted before a result.

Driver requirements:

- Use the supplied installed service/backend interpreters with `-I -B`. Do not
  import checkout tests, add source paths, invoke an editable environment, install
  dependencies, warm up/precompile packages, monkeypatch production behavior, or
  modify an imported source/installed runtime.
- Create only test-owned tiny local models/data in `driver_work`. Any additional
  component source requires explicit immutable provenance before execution. The
  synthetic custom-fixture license policy is not inherited by another workload.
- Use actual OperationService submit/cancel/checkpoint-discovery/resume gates;
  retain operation transcripts, parent/child requests, checkpoint/export bytes,
  identities and termination states. Persist service/job identities as they are
  created. Cancel and await owned jobs in a driver-wide `finally`, within the
  supplied timeout; the coordinator also attempts bounded service cleanup.
- A static Trainer exact-state driver should prove the original full horizon,
  native model/optimizer/scheduler/RNG state, metrics/history/row order, immutable
  parent request, continuation label, evaluator/export and negative gates.
  Streaming evidence needs its own scope and actual selected/consumed rows,
  restart/tamper/cancellation cases. Do not imply unsupported Trainer streaming
  exact-state continuation.

Result schema: `modelbuilder.installed-acceptance-driver-result/v1`, with `status`
`PASS`, matching `driver_id`, `role`, `runtime_commit`, the SHA256 of the original
context file, a nonempty `assertions` object of literal true values,
`worker_termination_confirmed: true`, and a nonempty `evidence` list containing
absolute file paths within `driver_work` and their SHA256 values. Missing,
out-of-scope, changed or incomplete receipts fail the harness.

The generic harness deliberately labels a syntactically valid driver's claims
`REPORTED_UNVERIFIED`, not scientific acceptance. Its whole-environment integrity
and independent native cleanup checks still apply. A workload-specific reviewer
must validate the driver's assertions and evidence before claiming installed
Trainer or streaming acceptance. Without `--driver`, the additional workload is
explicitly `NOT_RUN`. No such driver execution is evidence from this tooling PR.

## Lightweight tooling tests

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest \
  tests/portability/test_installed_acceptance.py \
  tests/portability/test_snapshot_review_audit.py -q
.venv/bin/ruff check tests/portability/reproduce_installed_acceptance.py \
  tests/portability/test_installed_acceptance.py
```

These tests use inert wheels, tiny temporary files and mocked cleanup services.
They neither install dependencies nor run models/native acceptance.
