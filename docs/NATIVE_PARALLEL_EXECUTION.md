# Bounded native parallel execution

The canonical recipe can opt into `execution.max_parallel_stages` (integer 1–8).
Omitting it, or setting 1, preserves the existing sequential execution paths.
See [canonical settings](NATIVE_PARALLEL_STAGES.md) for admission and editor/API
semantics. The bound is part of the immutable recipe identity, not the separate
run request's subgraph selection.

## Native ownership

An opted-in recipe uses the existing public ZenML dynamic pipeline. Typed
stages are declared with `BaseStep.submit()`, and public artifact futures form
actual native input dependencies. `ZENML_DYNAMIC_PIPELINE_WORKER_COUNT` is set
from the validated bound before creating the native runner. Modelbuilder does
not create a second step scheduler, artifact registry, cache, or run identity.
The native worker pool bounds active native stages including attempts and
materialization. Retries remain ZenML retries within those slots.

Conditions resolve their real native boolean gate artifacts before deciding
whether to declare the step. Skipped functions create no fake native step or
placeholder artifact. Optional joins omit skipped inputs and retain exact native
IDs for included inputs. Explicit bounded-iteration blocks are barriers: prior
futures complete before entering a block; all body futures complete before
feedback, early-exit evaluation, or the next iteration. Body siblings may run
concurrently. Live evaluation retains its existing independent sidecar bound;
those sidecars are not additional canonical DAG stages. Iteration/live-evaluation
combinations remain subject to the existing admission prohibition.

## Process-scoped trusted bodies

The pinned local ZenML orchestrator implements dynamic native stages inline in
threads and does not honor a requested isolated native runtime. Therefore each
actual opted-in step body runs in a fresh interpreter while ZenML's thread
continues to own metadata, caching and materialization.

- Python imports, source snapshots, module names, Torch/Python RNG state and
  framework thread settings belong to that body process.
- Startup uses isolated Python (`-I`) and the explicitly selected platform package
  root. Ambient `PYTHONPATH` and `PYTHONHOME` are removed.
- Native inputs are materialized by ZenML first. Inert tagged JSON carries typed
  primitive values, containers and local Paths; executable pickle is not used.
  The protocol allows at most 256 MiB per message. Native JSON-compatible scalar
  subclasses are normalized to primitives. Paths are normalized against the child
  working directory. Every returned Path is copied into durable attempt storage
  before child exit, preserving source snapshots and module-owned temporary files
  through later native materialization. Original/input paths are never moved or
  modified; this can add local I/O/storage proportional to Path output size.
- Source SHA256/trust and dependency validation still occur in the body.
  Output contract validation precedes returning values to native materializers.
- Per-attempt ownership journals record PID/birth identity and direct-child
  reaping separately from observed descendant cleanup. Large IPC input/output
  files are removed after the owner reaps the child.

This is process-state separation for explicitly trusted code, **not a hostile-code
sandbox**, CPU/memory reservation, per-stage environment, or authorization for
remote spend. Trusted code that deliberately evades ownership through daemonizing,
new sessions, arbitrary OS mutation, or security changes is outside this contract.

## Deadlines, cancellation and recovery

Every invocation attempt owns a unique token-named deadline journal. Native
start/end hooks retain the canonical node, invocation/iteration identity, native
step identity, PID/thread owner and monotonic deadline. An end hook clears only
its own unexpired attempt. Expired records remain for independent supervision;
concurrent attempts cannot overwrite or erase each other's deadlines. The
supervisor selects the earliest expiry deterministically. Legacy singleton
journals remain readable during recovery.

The independently launched supervisor survives service restart. Cancellation
first stops/reaps owned body processes, then retains existing process-group
SIGTERM/SIGKILL escalation. Linux execution bodies and parallel-job supervisors
act as subreapers so ordinary orphaned descendants can be adopted and reaped;
reaping is restricted to the owned job session, excluding tracking processes.
Body cleanup rescans after termination to catch newly adopted grandchildren.
A hard body exit, missing response, or unconfirmed cleanup cancels the entire job
rather than retrying alongside potentially orphaned work. Cancellation publication
is atomic and preserves the first reason. Native terminal reconciliation still
follows confirmed worker/group termination.

The verified initial concurrent execution scope is Linux CPU. Default sequential
platform support remains unchanged. Accelerator and other-OS concurrency require
separate actual overlap, lifecycle and termination acceptance.

## Acceptance

`tests/concurrency/` contains actual ZenML acceptance, not an SDK-only probe:

- independent pinned source functions with a mutual filesystem barrier proving
  overlap; four roots with active count at most two;
- shared import module names with different source files, exact independent
  Python/Torch seeded values, distinct process IDs;
- typed Path/dictionary multi-output and exact native producer/join artifact IDs;
- absent/default-one sequential compatibility;
- native retries, cache reuse and original native producer lineage;
- forced sibling overlap within bounded iterations and exact feedback barriers;
- true/false conditions, optional joins and skipped source imports;
- concurrent deadline ownership and job cancellation;
- service reopen, cancellation, descendant termination and reaping;
- independent framework trainers with a mutual barrier inside their first
  backward passes, changed weights and exact distinct native checkpoint ancestry;
- actual fixture and independent component training/evaluation/export, including
  completed live checkpoint evaluations before training finishes;
- inert IPC, excluded ambient Python paths, child cleanup on successful return,
  failed ownership-journal creation, and atomic cancellation publication;
- hard process exit with adopted orphan reaping, and default-one/parallel parity
  for snapshot file/directory outputs and relative Paths after a child cwd change.

The existing failure suite verifies deadline ownership, stale-attempt cleanup,
iteration identity, fail-closed hooks and legacy journal compatibility. Run the
native suites using the pinned backend Python with `PYTHONPATH=src`; no hosted CI,
provider dispatch or paid cloud compute is required.
