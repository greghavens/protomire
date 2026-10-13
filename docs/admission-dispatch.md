# Local run admission and dispatch

Run planning, submission and continuation use the existing ZenML/PyTorch backend.
The service never holds its SQLite writer transaction while inspecting Git/source
bytes, decoding datasets, probing an environment/device, resolving native bindings,
materializing a parent checkpoint, waiting for backend initialization, calling a
native RPC or spawning a supervisor.

## Protocol

1. Validate the caller, expected recipe revision, policy, target and trust consent.
   A submit/resume atomically reserves the ordinary global `(actor_id, key)`
   idempotency namespace, a pending operation and a dispatch intent. The native
   run ID remains null. Research reservations share that namespace.
2. Prepare a request-local source/dataset/placement context and an opaque,
   immutable backend handle outside the writer. At most four preparations run
   concurrently per service process; duplicate-key reads bypass that limit.
3. Recheck the exact live initiating grant and the captured project, recipe,
   policy, source settings, dataset roots/versions and interpreter identities.
   The ready intent remains owned by the original request only.
4. Under the per-job launch lock, persist the immutable native request and
   perform a short final authority/cancellation
   check. A resume's new immutable recipe revision and the dispatching intent
   commit together. This is the dispatch authorization fence. Revocation or
   cancellation before this fence prevents launch. Subsequent cancellation is a
   durable request to stop the authorized process; it cannot atomically undo an
   OS process creation.
5. Persist a durable spawn-attempt marker before Popen.
   The supervisor acknowledges its birth identity before spawning a worker.
   The inherited launch lock spans both processes. Reconcile the native result
   into application correlation with terminal-state and native-ID checks.

Backend initialization uses a five-second lock-acquisition deadline and the
existing bounded native validation RPC. The validation child inherits the lock,
so service death cannot let a second initializer overlap an unfinished child.
No packages are installed and no environment/profile locks are relaxed.

## Retries, interruption and cancellation

- A matching key returns the existing intent/correlation; a conflicting operation
  or payload returns an idempotency conflict. A changed correlation revision drops
  obsolete native liveness/metric fields from the original receipt.
- Persisted actor IDs, leases and intents are never reconstructed into grants.
  Restart never adopts a reserved request or launches it. A retry under another
  token for the same actor may read an authorized receipt, but cannot authorize
  the previous request's launch.
- An interrupted preparation remains pending/readable/cancellable. Starting a
  distinct attempt requires an explicit new request/key and current authorization.
- An attempted spawn with no reliable native/process evidence is unknown and
  uncertain. Missing PID files, native metadata, elapsed time or service restart
  do not prove that no training launched. Such an intent is reconciled only;
  automatically repeating training is disabled.
- Pre-fence cancellation reports cancelled with `launch_prevented: true`, not an
  invented native termination proof. Post-fence cancellation stays requested
  until existing supervisor/worker ownership and termination checks confirm it.
- Late launch/status/cancel replies and late launch errors cannot overwrite a
  terminal observation. Native identity changes are rejected.
- A resume rejected or cancelled before the final fence does not save a recipe
  revision. After that fence, its admitted revision remains immutable history even
  if launch becomes uncertain.

## Boundaries

This is local placement and process ownership, not resource reservation or
isolation. The database, filesystem and OS process table are not one transaction;
there is no cross-store exactly-once claim. The protocol favors an inspectable
uncertain state over duplicate training. Native metadata reads remain bounded
external calls and can be slow; the HTTP submit/resume call still waits for its
own preparation. Existing unrelated operations may have their own writer scopes.

The independent-stage bound is the saved recipe's
`execution.max_parallel_stages` (1–8, default 1), not a run-time override or a
second configuration. Plan and submit validate the same field; selection keeps
the exact canonical value. Read [the stage-settings contract](NATIVE_PARALLEL_STAGES.md)
for default compatibility, editor behavior and supported execution boundaries.

Run status, logs, artifacts, evaluations, events and cancellation use short
ownership checks with backend I/O outside the application writer. Pending event
pages preserve the requested cursor and existing response shapes. All statuses
remain in the existing HTTP/MCP run-status schema; native IDs are never fabricated.
