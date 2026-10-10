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
test lists and keeps its existing two jobs and 40-minute CPU-job budget.
There is no xdist dependency.

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
that failed, were canceled or were never started. No raw test data is copied into
these reports; temporary dataset/checkpoint state stays outside the artifact
upload directory and is removed after child cleanup. If cleanup cannot be confirmed,
temporary state is retained and only its local path is recorded in the receipt.

The queue has a 35-minute wall deadline, followed by at most eight seconds of
cleanup; an explicitly authorized hosted run would also have the existing
40-minute job deadline including bootstrap/preflight. A group failure stops
scheduling and cancels running work.
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
