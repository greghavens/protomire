# Remote cost admission: local accounting contract, production blocked

## Acceptance boundary

This implementation adds durable cost policy, quote, consent, reservation, lifecycle,
and reconciliation contracts. It does not provision a provider account, install
credentials, create cloud resources, submit a real ZenML remote run, or establish
remote cost enforcement. No provider rate or invoice has been invented.

Every production-facing result reports `spending_enforced: false`. Diagnostics
report `remote_execution_enabled: false` and `remote_acceptance: BLOCKED`.
`cost.quote.create` and `cost.reserve` fail closed even for authenticated actors
with execute and spend scopes. No runtime configuration flag or capability
assertion enables production dispatch. The local simulator is not exposed by the
HTTP/MCP operation facade and accepts only explicit synthetic fixture amounts.

The provider interface reuses existing ZenML stack/orchestrator and run identities.
It is not a training framework, orchestrator implementation, or GPU marketplace.

An [optional real Modal SDK/native ZenML bridge](REMOTE_MODAL_PROVIDER.md) is now
implemented. It includes native identity recovery, status/termination, and
provider rate/usage/summary reads, with a separate hash-locked environment.
Its missing all-in total-charge, global-deadline, provider-idempotency and
invoice-finality guarantees keep production admission blocked. Offline SDK
construction tests are explicitly not remote acceptance.

## Implemented invariants

- Money uses integer minor units, a currency code, and an explicit decimal exponent.
  Individual amounts are bounded to the exact JavaScript integer range. Aggregate
  balances are decimal strings because accumulated late invoices can exceed that
  range. Consumers must preserve strings or use BigInt/decimal arithmetic.
- Every quote is immutable and records its rate source, observation time, creation
  and expiry, opaque provider-account alias, target, ZenML stack/orchestrator,
  immutable recipe digest, runtime, estimate, all-in upper bound, and evidence type.
- Bounded quotes include compute, storage, egress, tax, fees, and cleanup, including
  explicit zero-valued components. Missing or unbounded categories block admission.
- Spend consent binds the authenticated actor, immutable quote digest, exact total,
  and maximum runtime. Read, execute, and spend scopes are required for simulated
  reservation. Production reservation remains disabled regardless of consent.
- Both a project and an account policy are mandatory. All account policies share
  one currency/exponent. Budgets are lifetime totals, not windows that can be reset
  to erase old charges. Policy identity/scope is immutable; revisions are checked.
- SQLite `BEGIN IMMEDIATE` serializes balance checks and reservation inserts across
  threads, separate connections, and processes. Duplicate request IDs return the
  original receipt; reusing a key for changed content fails.
- A reservation holds the maximum of reserved total and observed cumulative charge.
  It becomes actual-charge-only exposure after BOTH cleanup and final billing.
  Cancellation requests, worker loss, unknown submission outcomes, and provisional
  billing never release a hold. A never-submitted reservation can be cancelled or
  expired with proof supplied by its durable pre-submission state.
- Submission is claimed durably before invoking the adapter. The reservation ID is
  its stable provider idempotency key. Lost responses recover that key instead of
  resubmitting. Provider and ZenML run IDs are immutable once recorded.
- Runtime deadline and worker lease are distinct. Heartbeats cannot extend runtime
  or revive an expired lease. Recovery records stop intent before cleanup attempts.
  Observed spend reaching the reservation maximum also requests stop immediately.
- Cleanup requires separate evidence for stopped execution, idle compute shutdown,
  and deleted persistent storage. A stopped job with unresolved cleanup remains a
  retry candidate. Adapter failure for one binding does not strand other bindings.
- Billing reconciliation proceeds independently after cleanup and also inspects
  settled simulator jobs for delayed charges. Late invoices can create budget debt
  and authorization overruns. Either freezes further admission. Reopened cleanup,
  billing, or execution creates an enforcement alert and conservatively restores
  unresolved exposure. Credits cannot silently refund budget: declining cumulative
  charges require a future separately reviewed adjustment workflow.
- Provider observations carry account/job identities, monotonic sequences, time,
  evidence type and source. Duplicate identical observations are idempotent;
  conflicting/stale/future/foreign/mixed synthetic-provider evidence is rejected.
- Events preserve actor, request, policy revision, quote digest, exact consent,
  transitions, charges and source evidence. No credentials belong in these records.
- Account policy access is re-authorized before the application's idempotency cache
  is read. Narrowing the same actor ID to project-scoped access cannot replay an
  earlier account-wide receipt.

## Public operation contract

All operations use the existing shared `/v1/operations/{operation}` HTTP path and
its dynamically generated native MCP tool. Input schemas are exported in
`schemas/remote-cost-contracts.json`.

| Operation | Scopes | Behavior |
|---|---|---|
| cost.providers | read | Unconfigured/unverified diagnostics; remote acceptance BLOCKED |
| cost.policy.get | read | Visible project policies and exact balances; account totals only for unrestricted actors |
| cost.policy.put | modify, spend | Versioned policy update; never enables execution; account scope requires unrestricted actor |
| cost.quote.create | read | Fail closed: no verified provider quote source |
| cost.quote.get | read | Read stored immutable quote and provenance |
| cost.reserve | read, execute, spend | Fail closed: no production admission or dispatch |
| cost.ledger.get | read | Project reservation list or one reservation |
| cost.cancel | execute | Cancellation intent, with no claim of terminated billing |
| cost.history | read | Append-only project events with sequence pagination |

No public operation accepts rates, raw quote snapshots, invoice amounts, provider
observations, cleanup flags, adapter implementations, or simulator fixture settings.
The ordinary existing `cost.estimate` local-CPU response and disabled remote target
behavior are unchanged.

## Root integration

The shared service integrates the accounting contract through these hooks:

1. Add dependency-light remote operation specifications to `models.OPERATIONS`.
2. Call `authorize_cost_operation` after shared schema validation and BEFORE the
   outer application idempotency lookup.
3. Lazily create `CostOperations(CostLedger(sidecar_path))` and route the new cost
   operation names to it. Keep `cost.estimate` on its existing path.

The ledger must use a distinct database path, conventionally
`<application_database_path>.cost.sqlite3`, or its own `:memory:` connection in
in-memory tests. Do not pass the application database path while its write
transaction is open. The sidecar owns its transactional idempotency, so a retry
after an application-side rollback does not duplicate a budget mutation.
Close its connection with `service._remote_cost_operations.ledger.close()` when
tearing down an initialized service. Application shutdown infrastructure can
centralize this later; this release does not create a background controller.

## Tests and limits

Local regression coverage includes exact-money rejection, schema validation,
12 concurrent connections, 12 separate-process requests, duplicate concurrent
requests, stale quotes, changed consent, narrowed scopes, both policy scopes,
cap exhaustion, cancellation/cleanup divergence, partial cleanup failure,
provisional and delayed final billing, debt, oversized aggregate totals, worker
loss/restart, ambiguous submission recovery, runtime/heartbeat races, unknown
adapter recovery, obsolete bindings, and shared-service idempotency authorization.

These tests are accounting and state-machine evidence only. They do not satisfy
T22/P5, test a real provider invoice, prove GPU idle shutdown or storage cleanup,
verify provider-side hard runtime/total limits, or establish reliable remote
scheduler operation. A future production adapter still requires explicitly
authorized execute+spend on a configured real account, real bounded paid tests,
actual usage/invoice reconciliation, concurrency and delayed-billing tests,
independent compute/storage cleanup checks, restart recovery, and a separately
reviewed production-dispatch enablement change. No such authorization is assumed.
