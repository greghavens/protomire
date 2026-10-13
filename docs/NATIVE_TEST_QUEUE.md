# Bounded native CI queue

CI is run locally at the owner's request. Both GitHub workflows are manual-only:
pushes to `main` and pull requests do not start hosted jobs. Do not dispatch a
hosted workflow without fresh explicit authorization. This does not claim that
earlier hosted runs were free; account billing was not available for inspection.

The default `scripts/test_all.sh` remains serial. After bootstrap, run the complete
local Linux CI with:

```bash
MODELBUILDER_REQUIRE_PARQUET=1 MODELBUILDER_RUN_INTEGRATION=1 \
MODELBUILDER_NATIVE_TEST_PROCESSES=2 bash scripts/test_all.sh
```

Only `1` and `2` are accepted. The retained manual GitHub definition uses the same
test lists and keeps its existing two jobs and 40-minute CPU-job budget. It remains
dormant and unused per the owner's local-only instruction. Its hosted budget is
not validated for the expanded 45-minute local queue allowance. There is no xdist
dependency.

Preflight, service/unit/MCP, workbench and native component-authoring HTTP checks
finish first. This prevents npm compilation from changing the JavaScript read by
backend component tests. Then the Linux-only stdlib supervisor starts the complete
backend lifecycle group and the complete service/native group. The complete
backend components/evaluation/live/reference/tracking group runs when a slot
frees. No more than two pytest roots run concurrently. Native tests still create
their normal service, SDK, supervisor, worker, and evaluation descendants.

Each group receives a unique HOME, XDG cache, ZenML configuration/store, temporary
directory, pytest cache and pytest basetemp. Thread-count environment defaults
are fixed at one for BLAS/OMP/MKL/Arrow, including fresh component processes that
can no longer inherit earlier fixture calls to `torch.set_num_threads(1)`.
The three selected-live HTTP cases run in the existing service environment,
which has TestClient/httpx, while still invoking the pinned backend interpreter
for native training. The backend groups exclude exactly that file; this corrects
an existing interpreter-placement error in both serial and concurrent aggregates
without installing a package, skipping a case or weakening the native assertions.

Already-pinned model assets, virtual environments, source, and compiled JavaScript
are shared read-only. Do not install dependencies or modify source during a run.

Parallel outputs are `evidence/tests/integration.{log,xml}`,
`cpu-lifecycle.{log,xml}`, `cpu-components.{log,xml}`, and `native-runner.json`.
Each group owns its outputs; no concurrent append to another group's log occurs.
The serial backend output remains `cpu-fixtures.{log,xml}`. Stale JUnit reports
are removed before starting a group, and a successful process without a fresh
nonempty JUnit report fails the queue. The summary explicitly records groups
that failed, were canceled or were never started. `native-runner.json` is also an
atomic progress receipt: it is replaced on group transitions and the existing
30-second heartbeat using a same-directory temporary file, file fsync, rename,
and directory fsync. Completed group rows preserve exit codes and the observed
JUnit byte count/SHA256 (hashing is bounded to 16 MiB; larger or unreadable evidence
is explicitly labeled). XML contents, command arguments and environment values
are not copied into the receipt. JUnit identity is diagnostic evidence, not an
additional test-result parser or a changed pass/fail rule.

Every queue invocation has a fresh `run_id` and UTC `updated_at`. A progress
receipt has `finalized: false`, `exit_code: null`, and `cleanup: null`, even when
all observed groups passed. Its `running` or `finalizing` state describes the last
observation, not proof that the supervisor is still alive. If the supervisor is
killed, the last valid snapshot remains explicitly unfinished; its timestamp can
be stale, and it cannot establish aggregate acceptance or confirmed cleanup.
Acceptance requires both a successful supervisor process exit and its matching
current-run finalized receipt with exit code zero and confirmed cleanup. A final
replacement can become visible before a subsequent directory-fsync error, so the
receipt alone cannot prove successful final publication. Setup failures are
finalized failures and claim no cleanup.
No unfinished group is silently resumed. Progress I/O failures are reported to
stderr without changing scheduling; failure to publish the final receipt remains
an error. Completed evidence survives a finalization error, and temporary state
is retained when launched children have no confirmed cleanup. No raw test data is copied into
these reports; temporary dataset/checkpoint state stays outside the artifact
upload directory and is removed after child cleanup. If cleanup cannot be confirmed,
temporary state is retained and only its local path is recorded in the receipt.

The local queue has a 45-minute (2,700-second) wall deadline, followed by at most
eight seconds of cleanup. The unchanged dormant hosted workflow has a shorter
40-minute job ceiling, including bootstrap/preflight; it cannot provide the full
local queue allowance and no hosted acceptance or preflight margin is claimed.
Do not dispatch it. A group failure stops scheduling and cancels running work.
SIGINT/SIGTERM also stop scheduling and clean up. Linux subreaper mode adopts
orphaned children, including native workers that create separate sessions.
Cleanup repeatedly finds only this supervisor's descendants, opens Linux pidfds and verifies PID/start time after opening each handle,
sends TERM then KILL through those handles, and reaps descendants. PID reuse
after verification cannot redirect a signal to an unrelated process. Unsupported process
containment refuses this opt-in mode instead of silently weakening cleanup.
A direct SIGKILL of the supervisor cannot run Python cleanup; the CI runner's
ordinary whole-job cleanup remains the final boundary for that case.

`tests/service/test_native_test_runner.py` verifies selector coverage equals the
unchanged serial lists, expands directory selectors to guard against overlaps,
checks group environment isolation, and uses tiny actual subprocesses to prove
two-slot scheduling, detached-session cleanup on failure/deadline/cancellation,
stale-report rejection and PID-reuse protection. These checks do not prove the
full native suites pass together. That requires actual complete opt-in native
execution locally. Local success does not establish hosted-runner timing or
native GUI acceptance. Controlled subprocess proof is not a substitute for
ZenML/model lifecycle acceptance.

## Aggregate allowance revision, 2026-10-13

The previous 2,100-second allowance predated twelve added rules integration
cases and the pending epoch-Trainer coverage. The
[prior 6323bfd receipt](../evidence/rules-20261013/prior-native-6323bfd/native-runner.json)
completed all groups in 2,017.17 seconds, leaving only 82.83 seconds of allowance;
it still failed its existing lifecycle rights case. The
[expanded run](../evidence/rules-20261013/full-native-first/native-runner.json)
reached the old deadline with concurrency canceled. Its twelve new rules cases
accounted for 279.89 seconds. Combining its completed group durations with the
prior completed concurrency duration gives 4,266.763 process-seconds: even ideal
two-slot balancing would require 2,133.382 seconds. Reordering the four intact
groups would require at least 2,217.832 seconds under those measured durations.

The staged Trainer candidate added 285.431 seconds in a focused run that included
one setup timeout. Including that observed duration projects roughly 2,451 seconds
under the existing schedule. Substituting the successful quiet calibrated,
discovery and conditional rerun timings instead projects roughly 2,310 seconds.
These are planning estimates, not a passing aggregate benchmark. The revised
2,700-second local aggregate allowance provides bounded headroom for the expanded
coverage while retaining two pytest roots, the same selectors, thread limits,
individual test/RPC deadlines, failure propagation and descendant cleanup.

Historical 2,100-second receipts remain unchanged and the failed expanded run
remains failed. A fresh complete canonical run on the final source, without other
aggregate or Trainer workloads alongside it, is required to establish acceptance.
No hosted workflow was dispatched for this allowance change.
