# Live checkpoint evaluation and IDE notifications

Live evaluation is part of the single project/model/recipe lifecycle. It wraps an
existing framework or source-pinned component evaluation callable; it does not
create another training framework or treat an external tracker as authoritative.

## Ready means complete

The training harness calls `context['checkpoint_ready'](path, step, purpose)` only
after committing every checkpoint asset and its descriptor. Generic checkpoints
use `modelbuilder.checkpoint/v1` manifests. The bundled CPU fixture invokes the
same callback after atomic checkpoint writes. `checkpoint_interval_steps` controls
fixture publication cadence (default 1); generic cadence remains a harness or
framework setting.

Publication copies the declared files into a private staging directory, verifies
their hashes before and after copying, then atomically renames the complete
snapshot and ready marker. Symlinks, escaping paths, incomplete manifests and
changed files fail publication. The original trainer can keep replacing its own
checkpoint files without changing published snapshots. A live snapshot is limited
to 4 GiB on the local target; an oversized snapshot produces an explicit error.
Snapshot files are read-only, and the evaluator rechecks the journal-bound marker
and asset hashes both before and after invoking the harness.

Checkpoint identity is SHA256 of the selected fixture checkpoint bytes, or the
canonical identity of the generic committed checkpoint descriptor. Publication ID
also binds the operation, training node, step and purpose. This is a real local
immutable publication ID, not a fabricated ZenML artifact ID. ZenML continues to
own the parent run and its final materialized artifacts.

## Concurrent evaluation

`backend.live_evaluation.LiveEvaluationCoordinator` persists the queue in a
job-local SQLite journal. Child processes reuse either the bundled evaluator or
the recipe's existing source-pinned evaluation component. Generic input bindings
must be resolvable from upstream training inputs; admission rejects an enabled
live configuration that lacks a supported checkpoint bridge or evaluator binding.

Recipe `evaluation.live` selects `enabled`, `max_concurrency` (1–4, default 1),
and `timeout_seconds` (>0 and at most 3600, default 300). The concurrency check is
transactional across coordinators sharing the job. Running child processes have
bounded lifetimes and remain in the training worker's supervised process group.
Normal close drains pending evaluations. Training cancellation or failure cancels
the queue and terminates/reaps the evaluator processes and observed descendants.
The supervisor also reconciles abandoned records after verified process-group
termination.

Results become readable as soon as each evaluator finishes, without waiting for
the training function or parent run to complete. A short run may naturally finish
its optimization loop before its first evaluator returns. The training step drains
its queue before returning, but the live read API is never gated on parent status.

A checkpoint evaluation failure is recorded independently and does not silently
fail training or stop evaluation of later checkpoints. Publication contract or
coordinator infrastructure errors are explicit training errors, rather than a
false claim that live evaluation is functioning. Queued work survives a coordinator
restart. Running work whose owner and child are positively gone becomes a durable
interruption failure, never a fabricated completed result or an automatic duplicate
execution.

## Evidence and trends

A completed evaluator must return finite scalar metrics and valid immutable
`modelbuilder.evaluation/v1` records. Each record's metric name and value must
match its reported scalar, and its treatment checkpoint must equal the published
checkpoint digest. Generic harnesses receive that digest in
`context['checkpoint_digest']`. Source closure and declared dependency hashes are
verified again immediately before evaluation.

For each measured metric, the backend compares the result with the latest completed
lower-step result from the same training node using the existing fail-closed
scientific comparison function. `comparisons[metric]` includes `compatible`,
`direction`, `previous_evaluation_id`, `delta_from_previous`, `improved` and
`diagnostics`. Incompatible data, split, protocol, metric or treatment identities
produce no delta and no improvement/regression claim. No previous completed
checkpoint produces no comparison. With concurrency greater than one, comparison
uses the eligible results actually available when that evaluation completed; no
future observation is invented or silently backfilled into an old event.

## Canonical reads and durable event replay

The operation service and native MCP expose the same two read operations using
existing project-scoped run selectors:

- `run.evaluations.list`: `{schema, checkpoints, evaluations, summary}`. Each
  evaluation has `evaluation_id`, `checkpoint_id`, `checkpoint_digest`, `node_id`,
  `step`, `status`, timestamps, finite `metrics`, `evaluation_records`,
  `comparisons`, and nullable structured `error`. Status is `queued`, `running`,
  `completed`, `failed`, or `canceled`. Absolute checkpoint paths are omitted.
- `run.events.list`: accepts `after` (nonnegative integer cursor, default 0) and
  `limit` (1–200, default 200; the internal journal helper also supports larger bounded pages). Returns `{events, next_cursor, has_more}`. Events
  have a monotonic integer `cursor`, stable `event_id`, `kind`, operation/native
  run identity when available, checkpoint identity, node, step, and timestamp.

Event kinds are `checkpoint.ready`, `evaluation.queued`, `evaluation.running`,
`evaluation.completed`, `evaluation.failed`, and `evaluation.canceled`. Completion
events include safe finite numeric metrics and authoritative comparisons. Failed
and canceled events include a structured error. Queue/status updates and their
events commit in the same SQLite transaction. Unique event identities bind the
operation, checkpoint/evaluation and transition. Duplicate callbacks, reconnects,
and repeated reads cannot generate another event for the same transition.

Clients retain `next_cursor` and deduplicate `event_id`. A historical initial fetch
should establish a baseline without issuing old notifications. After reconnect,
process every page until `has_more` is false. An explicit newly submitted run can
start at cursor 0. Improvement/regression notifications require an authoritative
compatible comparison; metric-name equality alone is insufficient. Preferences
belong to the IDE, while the event history remains independent of whether a client
was connected. External W&B mirroring, when explicitly enabled, uses only its
configured allowlist of numeric metrics and the actual checkpoint step.

Reads never start execution, reconcile status, create a journal, or change user
state. SQLite read lock waiting is bounded to 50 ms so polling cannot hold up
process cancellation. A busy reader may retry on its next poll.

## Verification

Run from the repository:

```sh
.venv/bin/python -m pytest tests/live_evaluation -q
PYTHONPATH=src .venv-backend/bin/python -m pytest tests/live_evaluation -q
```

The backend test performs actual Torch optimization and evaluation: eight CPU
training steps, four committed checkpoints, and at least two independent checkpoint
evaluation reports observed while the training loop is still running. Additional
subprocess tests cover deduplication, cursor pagination/reconnect, immutable
snapshots, partial-write and symlink rejection, post-publication tampering, compatible
loss deltas, isolated evaluator failure, concurrency, timeout, descendant
cancellation, persistent queue restart, and birth-identity-aware crash recovery.

### Parent terminal notifications

The supervisor also records `run.succeeded`, `run.failed`, or `run.cancelled` after
canonical native reconciliation. These events carry the operation/run identity and
status, without checkpoint fields. One terminal event identity is retained across
repeated reconciliation. A successful local exit code is not evidence of a
successful model run. Cancellation needs native termination metadata plus positive
process-group termination, and a cancellation request alone emits no terminal
event. A failure before a native run exists remains visible in `run.status`; no
native terminal event is fabricated for it.

## Product acceptance boundaries

Requested explicitly on 2026-10-09: checkpoint results must be available during
training, not merely after parent completion. The real CPU backend test establishes
that timing. It does not establish native GUI delivery: native IDE rendering,
notification preferences, deduplication after reconnect, and interrupted/stale
project navigation require separate frontend/native acceptance evidence. A
browser-only result must be labeled separately. Optional W&B offline tests do
not establish authenticated online upload or vendor billing acceptance.
