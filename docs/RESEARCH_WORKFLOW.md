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

This guard detects exact normalized intent and source/content novelty; it does not understand semantic equivalence or judge whether a new paper is persuasive. Prior rejection reasons remain visible for the reviewer. Manual text imports still carry user-attested source and rights status; supported provider retrieval records its distinct source/license checks.

## Example lifecycle

1. Create or refresh a configured watch. Review findings and select saved evidence.
2. Optionally import authorized text or explicitly retrieve a supported licensed paper. Inspect methods, search the local snapshot and verify an exact passage.
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

## Bounded paper content and methodology inspection

The Research screen now exposes **Retrieve licensed paper** on saved evidence and findings, **Local paper snapshots**, content retrieval outcomes, a bounded paper reader with a section index, and **Search within paper**. The existing evidence/claim/hypothesis/proposal/measurement workflow remains unchanged. A method section is source material for human inspection, not an LLM summary, scientific verification, or evidence that the method works in this project.

### Supported content and rights

- OpenAlex discovery requests `abstract_inverted_index`. It reconstructs at most 10,000 contiguous tokens and 50,000 UTF-8 bytes only when that work's `primary_location.license` is `cc-by`, `cc-by-sa`, or `cc0`. The returned abstract records the provider's license label and attribution identity. This is a provider assertion, not independent legal verification; OpenAlex metadata openness alone does not license article text. Missing abstracts, malformed indices and unknown/unsupported rights remain explicit statuses without reconstructed text. No arbitrary source link is followed.
- Full-text retrieval supports only **Europe PMC open-access JATS XML** from the fixed `https://www.ebi.ac.uk/europepmc/webservices/rest/{PMCID}/fullTextXML` API. The caller supplies a bounded `PMC` identifier, never a URL. The returned XML must contain that exact PMC identity and must match either the saved evidence's PMC identifier/official PMC article URL or its DOI. Matching a title alone is insufficient. For OpenAlex evidence with a DOI, the DOI establishes the content-to-citation binding; callers still supply the PMC identifier.
- Local retention requires an article-level license element containing one supported canonical Creative Commons URI: CC BY or CC BY-SA versions 1.0, 2.0, 2.5, 3.0 or 4.0, or CC0 1.0. Scope-limited `specific-use` licenses, unknown/plain-text-only rights, noncommercial/no-derivatives variants and inconsistent alternatives fail closed. The article's title, authors, copyright notice and license statement are retained. The provider's article license is checked structurally; ownership, license compliance for a later reuse and third-party material exclusions are not independently established by this application.
- The extractor retains the article title, abstracts and body text, including supplied table/caption text with block boundaries. It retains section titles and stable offsets, including methodology when supplied by the publisher. It does not provide a complete publisher rendering: references, supplementary files, images, layout/formula layout, PDF/OCR and arbitrary publisher/DOI/arXiv URLs are outside this adapter's coverage. The original XML is not persisted; its exact SHA-256 and byte length are retained separately from the normalized text hash. The extractor version is `modelbuilder-jats-text-v1`.

These limits deliberately leave unknown rights and unsupported sources unavailable instead of claiming universal full-text access. The existing explicit user-attested text import supports authorized material from other sources without fetching it.

Provider documentation: [Europe PMC REST API](https://europepmc.org/RestfulWebService), [supported download protocols](https://europepmc.org/developers), [copyright and full-text access](https://europepmc.org/help), and [OpenAlex work attributes and abstract constraints](https://help.openalex.org/data/works/attributes/). These are public interface references, not live-provider acceptance evidence.

### Shared service / HTTP / native MCP operations

- `research.text.fetch`: `project_id`, `evidence_id`, `expected_evidence_revision`, `provider:"europe_pmc"`, `source_identifier:"PMC123"`, and an `idempotency_key`. This is a `modify` operation because it appends local text and attempt records; the remote interaction is a read-only GET. It sends only the public PMC identifier to `www.ebi.ac.uk`. It returns a durable retrieval outcome and `text_id` on success. A failure returns `status:"failed"`, a fixed safe error code/message and no text. It never falls back to another route or follows redirects.
- `research.text.list`: optional `evidence_id`; returns local snapshot summaries without whole paper bodies, plus successful/failed retrieval history. No fetch occurs.
- `research.text.get`: the existing complete local snapshot read now includes either imported or provider-retrieved text and its source/rights status.
- `research.text.inspect`: `text_id`, optional `section_id`, `start` and `length` (1–16,000 characters; default 8,000). Returns exact source text, Unicode start/end offsets, stable section index, attribution, provenance and `next_start` where another passage exists. Imported plain text has a single `document` section. It does not infer nonexistent methods sections.
- `research.text.search`: `text_id`, literal `query` (1–300 nonblank characters), optional `case_sensitive` (default false), `start` and `limit` (1–50, default 20). Returns exact matching quotes, offsets, bounded context and section identities. `next_start` supports pagination without dropping a match. This is local literal search, not semantic ranking, regex execution or provider inference. No source content or search query is uploaded.

Offsets are zero-based Unicode character positions into the immutable normalized local text; the end is exclusive. Both imported and fetched passages use the existing proposal citation shape. Fetched citations report `passage_matched_retrieved_text` together with their text hash, rights validation and source validation; user-imported citations keep `passage_matched_user_supplied_text`. Both retain `claim_verified:false`. No scientific claim, proposal approval/application, recipe revision, training, promotion or release follows from retrieving or searching a paper.

### Resource, network and concurrency guards

The fixed-origin GET runs in an isolated stdlib child process with a sanitized environment, no credential-bearing ambient proxy, no redirects and a 20-second wall timeout including DNS/slow response reads. Timeout kills and reaps the child. There is an additional child watchdog. Only uncompressed UTF-8 XML is accepted, capped at 4 MB wire bytes and 200,000 extracted UTF-8 text bytes. Oversize results fail rather than truncate silently. XML internal DTDs and entities are rejected; external DTD declarations may be present but are never loaded. Tree depth, element count, section count and attribution sizes are bounded. XML tags, links, instructions and scripts are never executed; the UI uses text nodes only.

Short SQLite claim and commit transactions surround the network operation. Provider I/O does not hold a store lock or write transaction. At most two content requests per database may be in flight across workers. Forty-second token leases fence stale responses; the exact initiating request grant, current actor/project authority and the evidence revision are rechecked before retaining content. Removing, replacing or narrowing that grant fences its in-flight request even if another modifying token for the same actor remains. The other grant can initiate its own fresh explicit request; actor-owned watch scheduling retains its separate authorization semantics. Concurrent identical request keys report `request_in_progress`; conflicting use reports `idempotency_conflict`. Committed identical requests replay without fetching. A process crash can cause an unfinished read-only GET to repeat after lease expiry, but cannot duplicate a committed immutable snapshot. A new successful fetch of identical XML/license/citation reuses its text snapshot while retaining the separate retrieval attempt.

No content schedule is added. Opening the Research screen, a retrieval form, a reader, or a source list is inert until an explicit operation is submitted. There are no LLM/provider-inference calls, model charges, paywall/robot/access-control bypasses, arbitrary URL inputs, source execution, raw evidence uploads, publication or changes to the one-project/one-recipe lifecycle.

### Verification scope

`tests/research/test_research_content.py` uses synthetic local article fixtures. It covers licensed XML/abstract/method extraction, attribution, DOI/PMC identity, exact Unicode search offsets, pagination, source/proposal provenance, durable failed rights/access outcomes, input/authorization/project guards, stale evidence and authority, unlocked I/O, concurrent idempotency, cross-worker admission, byte/encoding/XML/redirect protections, and an actually killed/reaped timeout child. HTTP tests and an official MCP SDK memory-stream session traverse the same operation service. No live Europe PMC/OpenAlex paper request or private-query/content upload is used by the tests.

`packages/ide_feature/test/researchView.test.cjs` covers inert source rendering and reader/actions. `researchContentDom.cjs` is the controlled Chromium acceptance script for retrieval cancellation/disclosure, section inspection, search pagination, dismiss/reopen and stale project navigation; running it requires a Chromium-capable environment. Browser-only checks do not establish native Code - OSS or live-provider acceptance.

## Explicit local PDF and OCR ingestion

Saved evidence and findings now expose **Import authorized PDF** in the existing
Research screen. This opens the native file picker, asks for the rights basis and
license/permission, and offers embedded text, local OCR for empty-text pages, or
local OCR for every page. A final review names the exact citation, filename,
byte count, raw SHA-256, OCR mode and configured service destination before any
PDF bytes are sent. Cancel at any review step sends no document. Opening the
reader or Research screen is inert. This workflow does not add a recipe import
menu, embedded agent, scientific summary, or new model framework.

The selected file is read with a bounded, same-handle local reader; symlinks,
nonregular files, unexpected changes and remote filesystem-provider URIs are
rejected. Local filesystem paths and raw PDF bytes never enter the webview.
The webview passes only the current project/evidence IDs and evidence revision
to the native host. Project/service changes fence the pending upload; an
uncertain network result is never automatically retried. For a remote configured
ModelBuilder service, the explicitly reviewed upload transfers the document to
that service. “Local extraction” means on the configured service machine, not
necessarily on the IDE user's computer. No document is forwarded to an OCR or
model-inference provider.

### Operations and immutable evidence

- `research.document.capabilities` is a project-scoped read. It reports platform,
  installed fixed-location tool availability, maximum limits and configuration
  errors without parsing a document or making a network request. Tool presence
  does not prove installed English OCR data or extraction success.
- `research.document.import` requires `project_id`, `evidence_id`,
  `expected_evidence_revision`, a basename `filename` ending in `.pdf`,
  `media_type:"application/pdf"`, canonical `content_base64`, the mandatory
  `expected_sha256` of decoded bytes, `license`, `rights_basis`,
  `rights_confirmed:true`, and `idempotency_key`. Optional `ocr_mode` is
  `disabled` (default), `auto` (only pages with no embedded text), or `always`.
  Optional `max_pages` (1–100) and `max_ocr_pages` (1–20) can reduce page limits.
  No URL, password, arbitrary filesystem path, shell option or cloud OCR
  destination is accepted.
- Success returns the existing durable retrieval shape and `text_id`. Parser,
  capability, timeout, resource and access failures return `status:"failed"`
  with a safe code and no partial source text. Invalid payloads, rights/hash
  mismatch, changed citations, stale grants and lost leases remain service
  errors. Reusing a committed idempotency key replays its recorded outcome;
  another attempt requires a new explicit key.

The extracted snapshot is the existing `research_text` resource. It includes the
exact citation revision/snapshot; user-attested rights; normalized text hash;
original PDF byte hash and size; tool versions; extraction options; page count;
and per-page Unicode offsets, UTF-8 size, text hash and extraction method.
Physical pages are sections (`page-1`, etc.), including empty pages. Text is
normalized by converting CRLF/CR to LF, stripping boundary whitespace on each
page and joining pages with two LFs. Original PDF bytes and rendered OCR images
are temporary and are removed after the worker has stopped on normal and timed-out
requests; they are not saved in the application store. A forcibly killed service
or host crash can leave private temporary files for operator cleanup. This is
not guaranteed secure erasure from operating system storage, swap or external backups.

Existing list/read/search/inspect operations work on the PDF snapshot without
network I/O. Search and inspect return overlapping physical-page evidence;
proposal citations retain the original PDF hash, exact text hash and quote
Unicode offsets, matching page hashes/ranges, and extractor provenance.
Duplicate extractions with the same evidence revision, source bytes, options,
tool versions and text are reused. Conflicting rights declarations for that
same extraction are rejected. None of these hashes independently establishes
publisher identity or scientific validity: the source remains
`user_supplied_not_independently_verified`, rights remain `user_attested`, and
`claim_verified` remains `false`.

### Runtime and safety limits

This optional extraction path currently fails closed outside Linux. It uses
operator-installed `/usr/bin/pdfinfo` and `/usr/bin/pdftotext` from Poppler, plus
`/usr/bin/pdftoppm` and `/usr/bin/tesseract` with English data when OCR is explicitly
selected. No packages are downloaded/installed by an import, and no PDF password
or access-restriction override is attempted. Tool versions are recorded per
snapshot rather than silently claiming cross-version reproducibility.

Defaults and hard upper bounds are 8 MiB raw PDF, 100 physical pages, 20 OCR pages,
200,000 normalized UTF-8 text bytes and a 90-second extraction wall deadline.
Operators may lower the raw-document cap using
`MODELBUILDER_RESEARCH_MAX_DOCUMENT_BYTES` (integer 1,024–8,388,608); invalid
configuration disables the capability. The native picker honors that reported
cap. HTTP retains its existing 16 MiB actual-body cap. IDE and MCP document-import
requests allow at least 120 seconds to receive the bounded extraction outcome.

The standalone decoder is started in a private temporary directory with a
sanitized environment and a private process group. Before parsing, it establishes
Linux hard limits: 768 MiB address space, 60/61 CPU seconds, 16 MiB per output
file, 64 descriptors and no core dumps. Tool output is file-bounded before any
parent allocation, and the parent independently validates page ranges, Unicode
text, text sizes, page hashes and tool-output structure. OCR rasterization is
limited to 2,048 pixels on the longest side, grayscale, one page at a time.
Timeout terminates the process group and waits for the worker; the normal
termination path lets the worker kill/reap its active tool before temporary
content is removed. Linux `waitid(WNOWAIT)` keeps the exact worker PID reserved
until group cleanup, preventing a post-reap signal from targeting a reused PID. At most two imports per database and two decoders per
service process are active. Short 120-second exact-grant/evidence leases keep
extraction outside SQLite writer locks and fence revoked or superseded work. The
IDE keeps its read-only run/checkpoint notification polling active during a PDF
import while still fencing duplicate workbench actions.

These are process/resource bounds, not an operating-system hostile-code sandbox.
PDFs remain untrusted parser inputs. Poppler/Tesseract are external mature tools;
this path does not establish containment of a compromised native parser. Their
platform packaging, patch level, font and language data remain operator
responsibilities. OCR can misread numbers, equations, words and reading order.
The snapshot is not a publisher rendering: images, layout, formula layout,
attachments and annotations are omitted. Digital signatures are not verified.
Arbitrary publisher/DOI URL fetching, paywall bypass, non-English OCR, automatic
scientific synthesis and semantic claim verification are still outside this
implementation.

### Verification

`tests/research/test_research_documents.py` includes actual Poppler extraction
from self-authored generated PDFs and actual Tesseract extraction from generated
raster-only PDFs. It verifies page/Unicode/hash boundaries, empty-page inspection,
explicit OCR, page limits, HTTP and official MCP SDK-to-ASGI proposal provenance,
rights and hash guards, durable failures, idempotency, project/actor/revision
fencing, cross-connection concurrency, effective child hard limits and actual
worker/tool timeout cleanup. Other race/error cases deliberately use controlled
extraction doubles. Set `MODELBUILDER_REQUIRE_DOCUMENTS=1` to fail rather than skip
native extraction when the optional tools are absent.

`packages/ide_feature/test/researchDocuments.test.cjs` verifies native host
consent/cancel paths, changed project/service guards, local bounded reads and
byte-free webview messages using explicit dialog and service doubles.
`researchDocumentHttp.cjs` invokes the compiled existing Research host against a
real isolated loopback service with actual Poppler/Tesseract; only native dialogs
are doubled. It also starts an independent official SDK client and standalone
MCP stdio server against the same real HTTP service, confirming the same immutable
PDF text ID, page hash and text hash across the native host and MCP. `researchDocumentDom.cjs` is a separate controlled Chromium DOM
acceptance script. The latest Chromium attempt stopped before page creation at
an OS `socket() ... Operation not permitted` restriction. No security fallback
was used, and no current native Code - OSS GUI acceptance is claimed.
