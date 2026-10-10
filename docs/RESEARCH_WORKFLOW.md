# Evidence-linked research review

This extends the original one-model lifecycle: research → hypothesis → staged recipe revision → explicit review → explicit apply → separately authorized experiment → decision. It uses the existing evidence, claim, hypothesis and decision resources and the project's single stable recipe. There is no embedded assistant or automatic training path.

## Shared operations

HTTP uses `POST /v1/operations/{operation}`. Native MCP exposes the same operation with dots replaced by underscores. Every operation requires `project_id`; every mutation also requires an `idempotency_key` and `modify` scope. Read operations require `read` scope. Project-scoped actors cannot access another project's watch, finding, source text or proposal. Mutations retain the service's authorization, idempotency, audit and append-only history. Refresh uses separate short claim and commit transactions around provider I/O.

### Configured watches

- `research.watch.create`: `name`, `query`; optional `provider` (only `openalex`), `limit` (1–10, default 5), `enabled` (default true), `interval_seconds` (900–2,592,000, default 86,400).
- `research.watch.list`: list saved watches without network activity.
- `research.watch.get`: `watch_id`; includes the last 20 durable refresh attempts.
- `research.watch.update`: `watch_id`, `expected_revision`, `changes`. Changes are limited to the configuration fields above. Disable a watch without deleting its history. Changing its query, provider, limit or enabled flag resets its due time; changing only the interval recalculates the next due time after the last attempt.
- `research.watch.refresh`: `watch_id`, `expected_revision`; perform one explicit refresh. A disabled watch must be enabled first. Returns `{watch, refresh, watch_id, project_id, revision}`.
- `research.watch.refresh_due`: optional `max_watches` (1–5, default 1). Explicitly refresh the oldest enabled due watches. Returns `{refreshes, refreshed, remaining_due, scheduler_running, skipped_busy}`.

Creation and reads never fetch. New and legacy watches remain manual unless explicitly opted in with `research.watch.schedule_enable`. The existing Research screen displays the recipient (`api.openalex.org`), exact query, result limit and interval; its enable action opens a form with an unchecked required confirmation. No new navigation, model selector, agent product or recipe identity is introduced.

- `research.watch.schedule_enable`: `watch_id`, `expected_revision`, the exact current `provider`, `query`, `limit`, `interval_seconds`, and `confirm_schedule:true`. The disclosure must match the watch revision. Due requests may start immediately; subsequent attempts use the disclosed frequency.
- `research.watch.schedule_disable`: `watch_id`, `expected_revision`. Stops future automatic requests and invalidates any in-flight result. It leaves manual refresh available. A request already sent cannot be recalled.
- Changing query, provider, limit, interval or enabled state disables scheduling and requires new opt-in. Renaming a watch invalidates an old in-flight response conservatively without changing the approved request configuration.

The HTTP service starts one bounded polling loop per worker with its normal application lifespan. It checks up to two due, opted-in watches every 15 seconds, performing no catch-up burst for missed intervals. Due times, current owner, consent disclosure, results and leases survive restart. The watch owner's currently configured modifying grant and project access are checked before fetching and before committing. When a due watch is inspected, missing modifying grants, lost project access or an archived project disable its schedule before any fetch. Watches that are not yet due are not scanned for revocation; if authority is removed and restored before the next inspection, the existing opt-in remains active. Use `research.watch.schedule_disable` for immediate, persistent cancellation. Grants come from the running `TokenAuth` registry; environment-based changes take effect when the service workers are restarted with the new configuration. Operators must update/restart every worker using that registry. Restoring authority does not silently re-enable a watch whose schedule was disabled during inspection.

Forty-second SQLite leases coordinate manual and automatic requests across workers, with at most two admitted watch-provider fetches per database (direct read-only evidence searches do not use watch leases). Claims and commits use short transactions. Neither a service/store lock nor a SQLite write transaction is held during provider I/O, leaving live checkpoint/status traffic responsive. The existing OpenAlex adapter runs its fixed public endpoint retrieval in an isolated subprocess with a hard 20-second wall deadline including DNS and slow response reads, a 2 MB response cap, no redirects, no ambient proxy credentials, and at most ten results. Timeout kills and reaps that child. Shutdown stops new claims, waits at most 25 seconds for the scheduler thread, and discards a response that arrives after stop. Database contention can leave the daemon loop finishing its short cleanup after that wait; it cannot start another fetch.

A manual due tick performs at most five requests sequentially outside transactions. Use `max_watches:1` for interactive callers and budget the transport timeout accordingly for a larger batch. In-flight duplicate keys return `request_in_progress`; busy watches return `watch_busy` for a single refresh or are skipped by a due tick. Retrying the same request key resumes durable completed progress after its request lease expires. A process crash may repeat an unfinished read-only provider GET after lease expiry; committed progress and citation findings are never duplicated. Late responses cannot commit after another lease owner, disable/configuration change, authorization loss or shutdown.

Successful and failed attempts advance the due time by the configured interval (15 minutes to 30 days); manual refresh can retry sooner. Failed fetches never fabricate evidence or retain upstream diagnostics. Automatic work only appends citation snapshots and evidence-inbox findings. It never calls an LLM, changes a proposal, applies a recipe, trains, promotes, or incurs model-provider/cloud execution charges. OpenAlex availability, policies and quotas still apply. This is a bounded ranked metadata query, not full-corpus/incremental coverage: `coverage.kind:bounded_ranked_query`, `complete:false`, `deletions_inferred:false`. A missing result does not mean a paper was withdrawn.

Each novel `(watch, canonical source identity, metadata hash)` becomes a finding. Identical repeated results are deduplicated. Overlapping watches share an identical project evidence snapshot. Changed metadata creates a new evidence record and finding linked to the previous finding and evidence; previous cited metadata is never overwritten. DOI spelling is normalized when supplied by the adapter, otherwise the source identifier is used. Source identity and exact metadata hash determine deduplication; this is not semantic paper matching.

A retrieval or metadata-validation failure returns a durable refresh with `status:failed` and a safe error code. It is a recorded outcome, not a successful fetch and not a transport exception that rolls back the history. All normalized metadata records are validated before persistence; an invalid normalized record prevents the whole batch from being saved. The reused OpenAlex parser filters raw entries without a valid OpenAlex work identity, so a mixed raw page can still yield a valid filtered batch. Raw upstream exception messages are not retained. The retry of an identical idempotency key returns its original outcome without a second network call; a fresh attempt requires a fresh key and current watch revision.

### Finding inbox

- `research.finding.list`: optional `watch_id` and `status` (`unreviewed`, `reviewed`, `dismissed`). Returns `findings`.
- `research.finding.review`: `finding_id`, `expected_revision`, `status` (`reviewed` or `dismissed`), nonblank `rationale`.

Review records the actor, time and rationale as a new immutable resource revision. Triage never creates or changes a scientific claim. Watch results, saved evidence metadata, findings and their reviews explicitly set `claim_verified:false`.

### Local source-text import

- `research.text.import`: `evidence_id`, `expected_evidence_revision`, `text`, `license`, `rights_basis`, `rights_confirmed:true`; optional `expected_sha256`.
- `research.text.get`: `text_id`.

This imports user-supplied text only. It does not read arbitrary file paths, open a URL, scrape a publisher, parse PDFs, execute content or bypass a paywall. Callers may provide text they are authorized to retain. Accepted rights bases are `self_authored`, `public_domain`, `licensed` and `permission`; a nonblank license/permission label and explicit attestation are required. Rights are recorded as `user_attested`, not independently verified. The linked evidence retains the source URL/identifier and bibliographic attribution.

Text is bounded to 200,000 UTF-8 bytes (also at most 200,000 characters), must be nonblank and cannot contain NUL. It is stored locally with its exact SHA-256, source citation snapshot, actor, time and rights declaration. An optional expected hash is checked before storage. Identical text for the same evidence is deduplicated. Conflicting rights declarations for the same content are rejected. No document redistribution/publication operation is added.

A proposal citation is either:

```json
{"evidence_id":"saved-evidence-id"}
```

or an exact passage reference:

```json
{
  "evidence_id":"saved-evidence-id",
  "text_id":"imported-text-id",
  "quote":"Exact source passage",
  "start":42,
  "end":62
}
```

Offsets are zero-based Python/Unicode character positions, with `end` exclusive; they are not UTF-8 byte offsets. All four passage fields are required together. The text must belong to the same evidence record and the character slice must exactly equal `quote`. The result is `passage_matched_user_supplied_text`, with the text hash and rights metadata. This proves only a match in the imported snapshot. It does not establish publisher authenticity, methodological validity or a scientifically verified claim. A citation without text stays `citation_only`.

### Staged proposals

- `research.proposal.create`: `hypothesis_id`, `title`, `rationale`, `citations` (1–50), `recipe`, `expected_recipe_revision`; optional `reconsideration_reason`.
- `research.proposal.list`: all proposals, retaining rejected and applied entries.
- `research.proposal.get`: `proposal_id`.
- `research.proposal.review`: `proposal_id`, `expected_revision`, `outcome` (`approved` or `rejected`), nonblank `rationale`.
- `research.proposal.apply`: `proposal_id`, `expected_revision`, `expected_recipe_revision`, `confirm_apply:true`.

Create validates the full proposed canonical recipe, preserves project and recipe identity, rejects a no-op, and stages the next immutable recipe revision without saving it as current. It snapshots the hypothesis, linked claims and citations; records the base recipe revision/digest; and provides an exact unified diff, staged recipe digest and proposal digest. At least one citation must overlap the hypothesis/claim evidence when those links exist. Duplicate equivalent citations are rejected.

The staged recipe includes `evidence_links` and `extensions.modelbuilder_research` referencing the proposal, hypothesis revision and citation IDs. These additions appear in the reviewable diff before approval. The recipe saved by apply has the exact staged digest, and the proposal retains its applied recipe revision/digest. A proposal points to a recipe revision; it is not a backend run or model version.

Approval records the actor, time, rationale and exact proposal digest. It does not alter the current recipe, mark the hypothesis scientifically accepted, submit a run or promote a model. Rejected proposals are retained and cannot be silently reapproved. Applying requires a separate explicit call. Both approval and apply check that the base recipe, hypothesis, claims and evidence have not changed. Apply checks the current recipe revision and the proposal revision; a concurrent editor wins or loses explicitly, never through last-writer-wins. Recipe and proposal updates are atomic. A stale proposal can still be rejected; to merge or change it, create a fresh proposal against the current recipe and review its new diff.

A proposal matching a previously rejected proposal, hypothesis or scientific decision requires a novel source/content evidence key plus a nonblank `reconsideration_reason`. The matching intent is a normalized proposed change, scope and acceptance-metric object. Recreating an identical citation under a new UUID does not qualify. Negative scientific decisions are matched using the hypothesis/claim revisions at the recorded decision time, so later edits do not erase the old rejection. The same guard runs again before approval/apply to catch rejection recorded after staging.

This guard detects exact normalized intent and source/content novelty; it does not understand semantic equivalence or judge whether a new paper is persuasive. Prior rejection reasons remain visible for the reviewer. Full-text imports still carry user-attested source and rights status.

## Example lifecycle

1. Create or refresh a configured watch. Review findings and select saved evidence.
2. Optionally import authorized text and verify an exact passage.
3. Create a claim and measurable hypothesis using the existing operations. Record uncertainty and contradictions honestly.
4. Submit the proposed recipe to `research.proposal.create`, using its current revision and explicit evidence citations.
5. Inspect `recipe_diff`, citation validation status and prior rejections. Call `research.proposal.review` with `outcome:approved` only after review.
6. Separately call `research.proposal.apply` with both expected revisions and `confirm_apply:true`.
7. Use the existing plan, submit, evaluation, decision and version workflows. Training remains a separate `execute`-scoped action. A decision must reference real run IDs and a recipe revision actually executed by a linked run.

## Validation and remaining gates

`tests/research/test_research_workflow.py` exercises the shared service, SQLite persistence, HTTP transport, generated native MCP schemas and an actual official SDK client/server session over memory streams using deterministic source responses. The SDK session traverses the HTTP/ASGI gateway and proves the stage-review-apply result matches direct service state and preserves stale-edit errors. It covers source changes/deduplication, invalid response rollback, failed refresh durability/recovery, due-time boundaries/restarts, project isolation, local text rights/hash/passage checks, stale review/application, atomic rollback, historical negative decisions, concurrent applies and the absence of implicit training.

These contract tests do not claim a live OpenAlex feed, deployed periodic scheduler, authenticated vendor-assistant session, independent publisher text verification, semantic method comparison, scientific validity or a completed model-training experiment. Those require their own acceptance evidence. The frontend must keep review and apply visibly separate and expose validation/source limits rather than presenting metadata as proof.

### Automatic schedule verification

`tests/research/test_research_scheduler.py` uses controlled metadata fixtures with no real network or private queries. It covers opt-in migration, matching disclosure, authorization/project revocation, schedule disable and configuration races, durable restart/due boundaries, repeated and concurrent keys, crash-after-commit batch recovery, shared manual/multiworker leases and global admission, expired-lease fencing, redacted failures, bounded ticks/shutdown, an actually killed/reaped slow child, and an official MCP SDK session through HTTP/ASGI. A slow-provider fixture verifies concurrent reads on the same store plus writes from another SQLite connection complete before the provider is released. The `packages/ide_feature/test/researchScheduleDom.cjs` browser script covers the existing Research surface and confirmation/cancel flows. Run the browser check against the current generated operation catalog after building the frontend. Controlled browser checks do not establish native Code - OSS GUI or live OpenAlex acceptance.
