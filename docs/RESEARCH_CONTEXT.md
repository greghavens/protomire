# Project-scoped research context

This is a local, deterministic context foundation for the existing research
workflow and external MCP clients. The installed retriever is lexical term
coverage, not embeddings, semantic retrieval, scientific synthesis or graph
RAG. It reuses the same Unicode-preserving literal matcher as within-paper
search. Existing evidence search, source ingestion, PDF/OCR, claims, hypotheses,
proposal review/apply and source-owned component APIs remain authoritative.

## Shared operations and native UI

The Research screen exposes **Build project context** and **Context
capabilities**. The context reader shows exact source revisions, hashes,
review/history state, citation and PDF-page provenance, rights warnings and
budget omissions. **Inspect exact passage** reopens that precise revision;
**Rebuild same corpus** preserves the original query, selection and budgets and
rejects changed source state. Closing or reopening a reader performs no
transmission. Source strings are rendered as text nodes, never HTML or commands.

The same authenticated operations are available through
`POST /v1/operations/{operation}` and native MCP tool names with dots replaced by
underscores. They require `read` scope and explicit `project_id`. Reads recheck
the initiating grant immediately before and after retrieval; a revoked grant
cannot return the prepared context. There is no independent assistant database,
provider connection or UI-only action.

- `research.context.capabilities`: the actually installed adapter descriptors,
  readiness, hard limits and unimplemented resource/external-processing gates.
- `research.context.build`: required `project_id` and `query`; optional
  `retriever_id` (default `local-lexical-v1`), `source_kinds`, `source_ids`,
  `exclude_source_ids`, `states`, `include_history` (default false),
  `expected_corpus_sha256`, `use_scope`, `exclude_unresolved_rights`, and budgets.
- `research.context.inspect`: `project_id`, `source_kind`, `source_id`,
  `revision`, exact `record_sha256`, indexed `field`, `start`, optional `length`
  (default 1,200, maximum 4,000 Unicode code points). It rejects an unexpected
  source hash, wrong project, non-indexed field or invalid span. The historical
  content remains pinned while current revision/state is reported separately.

A minimal local read request is:

```json
{"project_id":"<project-id>","query":"calibration held out","source_kinds":["research_text","claim","hypothesis","research_proposal"],"max_items":10,"max_bytes":24000,"use_scope":"local_review"}
```

The build response has `context`, its canonical JSON `context_sha256`, `budget`,
`coverage` and `usage_receipt`. Canonical serialization is UTF-8 JSON with sorted
keys, no ASCII escaping, separators `,` and `:`, and no nonfinite numbers.
`context.request` retains the selection/budgets for a reproducible rebuild.
There is no timestamp or random result identity; identical authorized source
state and requests produce identical results. No context or index is persisted.
The original resources and immutable revision history are reused.

## Exact provenance and retained uncertainty

Every included passage records source kind/ID, project ID, exact revision,
canonical record SHA-256, indexed-field SHA-256, passage SHA-256 and `[start,end)`
Unicode code-point offsets. Strings are indexed unchanged. Structured fields
such as citation metadata or recipe extensions use the documented canonical
JSON serialization; their offsets refer to that derived field, not a publisher
PDF's byte positions. Inspection uses the same serialization.

Imported paper passages retain citation revision and metadata hash, exact text
hash, license/rights/source validation, attribution and extraction provenance.
PDF passages also retain original-document hash, intersecting physical page
numbers/hashes/ranges, extraction method and section IDs. OCR remains explicitly
unverified. Proposal passages retain their original citation snapshots,
review/digest and application linkage. Existing claims/hypotheses can contain
unversioned relationship IDs; these remain labeled
`only_explicit_revision_fields_are_pinned` rather than inventing historic linked
revisions.

States are preserved verbatim. `approved` means a proposal review, `accepted`
means an existing hypothesis/decision outcome, and `supported` means an existing
claim review. These are not equivalent and none creates scientific proof.
`proposed`, `rejected`, `applied`, negative/inconclusive states and their original
records remain queryable. With `include_history:true`, prior revisions are
labeled `superseded` and retain `original_state` and `current_revision`. A newer
snapshot of the same paper is not automatically a retraction or scientific
supersession. Current-source corpus state counts and omission flags remain
visible even when a selected passage budget omits records.

Imported instructions, links and scripts are untrusted evidence. The fixed
context authority envelope states `instructions_are_authority:false`, grants
no actions and never declares a claim verified. The assembler only returns
server-rehydrated source spans, not adapter-authored prose. It validates source
identity/hash, authorized corpus binding, offsets, scores and passage length;
index/adapter attempts to substitute a different project, revision, hash or
mutated source fail closed. A downstream harness must preserve this trust
boundary and must not place source passages in its system/developer
instructions. This is a context contract, not a guarantee that an arbitrary
external agent will obey it.

## Bounded local baseline

- Up to 16 distinct Unicode word terms from a query of at most 300 characters;
  literal case-insensitive matching with no stemming or semantic inference.
- Up to eight occurrences per term/field considered. Candidate windows are
  ranked by distinct-term coverage, then stable source identity/field/offset.
  At most `min(240,max_items*8)` candidates are retained; overlapping passages
  from the same field/revision are deduplicated. Truncation is disclosed.
- `max_sources`: default 256, maximum 1,000 source revisions; `max_scan_bytes`:
  default 2,000,000, maximum 8,000,000 stored JSON bytes. SQL checks counts/bytes
  before materializing the corpus. Exceeding either fails explicitly, rather
  than returning an arbitrary prefix. Narrow kinds/IDs or choose a larger
  allowed budget. Historical revisions count separately.
- `max_items`: default 10, maximum 30. `passage_chars`: default 1,200, range
  80–4,000. `max_bytes`: default 24,000, range 2,048–131,072. The byte budget
  covers the entire canonical `context` object, including request, authority,
  source metadata, rights and citations. Whole items are omitted if they do not
  fit; hashes/citations and Unicode characters are never clipped to fit.
- Receipt/coverage fields outside `context` are not part of the byte budget.
  Inspection has a separate 131,072-byte item cap. There is no claimed universal
  token count: consumers must serialize and budget with their own tokenizer.

No-match or budget-limited results are not evidence that a method does not
exist. Coverage is always marked incomplete; lexical retrieval is intentionally
not advertised as corpus-complete or semantically exhaustive. The bounded scan
runs within the existing service snapshot transaction; a persistent scalable
index and long-running model inference require separate unlocked preparation,
authority fencing and concurrency acceptance.

## Rights, attribution and external harnesses

`use_scope:"local_review"` is the default. The receipt describes only a local
read of already-retained records; it is not legal clearance. Local import
attestation, a public URL, an open-access label, or a train/generate/publish
permission must not be repurposed as permission to disclose to another harness,
process with an external model provider or redistribute excerpts.

Each included item marks external-processing and redistribution terms unresolved,
and source rights/attribution are preserved. `exclude_source_ids` excludes known
restricted records and transitively excludes records whose explicit stored
identity links refer to them, including quoted proposal citations. When exclusions
are present, the bounded scan includes all project source revisions to check
historical/intermediate links, even if output kinds/IDs/history are narrowed.
Output selection is then applied. The receipt distinguishes selected sources,
scanned revisions, directly excluded and indirectly excluded records. If this
broader provenance scan exceeds the budget, it fails rather than returning
possibly restricted passages. It does not
attempt to infer provenance from free-form prose. `exclude_unresolved_rights:true` excludes all unresolved
records. The current shared Hub policy has no exact research-text/audience/use
disclosure adapter. Therefore `use_scope:"external_harness"` currently includes
no source passages, reports `blocked_unresolved_rights` when the selected corpus
is nonempty, and returns the excluded-source count. This is deliberately
conservative for all source kinds, including user-authored notes, until a
reviewed source/audience/purpose contract exists. No inferred permissive
license engine or caller-supplied rights override is added.

Assembly performs no vendor call, upload, transmission, subscription, model
load or compute purchase. A client reading a local-review context is responsible
for keeping it within that scope. Actual external Claude/Codex/Triggerfish
content transfer requires an authorized destination and rights review; the
local-read API is not a data-loss-prevention boundary against an already
read-authorized client copying content. Existing MCP transport compatibility
is tested; live end-to-end harness use is not accepted by these tests.

## Extension and resource contracts

`research_context.py` defines typed `SourceReference`, `IndexDocument`,
`ResearchIndex`, `Indexer`, `RetrieverDescriptor`, `Retriever`, `RetrievalQuery`,
`RetrievalHit` and `RetrievalBatch`. A server-owned `RetrievalRegistry` maps
adapter IDs to installed indexers/retrievers; requests never import a module,
execute a path, download a model or silently substitute another retriever.
Operators integrating reviewed code may provide
`service.research_context_registry`. Indexers receive an already-authorized
snapshot, may attach a private backend handle, and must preserve its project,
manifest and source-reference sequence. Adapter handles never enter context
receipts. The assembler resolves candidates against the authorized source
snapshot rather than trusting an index as the source of truth.

Research-method names are not enumerated. Arbitrary notes, hypotheses and
existing recipe `extensions` are searchable, including proposal
`method_extensions`. Actual new architectures/losses/method execution continue
through the existing source-owned component ABI and recipe review/validation.
Retrieval cannot install, approve or execute them.

`ResourceRequest`, `AdmittedResources`, `ResourceAdmission` and
`ModelResourceLifecycle` define future immutable asset, project/corpus identity,
policy snapshot, residency/device/deadline, admission/revalidation/release and
load/unload seams. The shared Hub asset policy must govern model/corpus
resources via `service.hub.policy_snapshot(project_id)` and the existing
`hub_rights.validate_artifact_rights`, never recipe/client policy maps. These
asset-use checks do not authorize external content disclosure. A separate typed
`ResearchContentPolicy`/`ContentPolicyDecision` seam binds source revision/hash,
use, destination, exact policy revision and evidence digest to a future shared
policy decision. There is no functioning permissive policy/lifecycle adapter.
Registered model-backed retrievers are currently reported not ready and refused
before any retrieval call, even if an extension mistakenly marks one ready.

Biff remains optional and unready. No Biff/Jev/jev-graph-builder dependency,
repository edit, model download, model training or fake graph edge is added.
The same exact source/citation references can anchor a future vector or graph
adapter without changing consumers' provenance contract.

## Verified scope and remaining acceptance

Offline tests use actual SQLite operations and actual HTTP/official MCP SDK
memory sessions. They cover deterministic hashes/byte budgets, Unicode offsets,
review/history distinction, unfamiliar recipe extensions, corpus changes,
resource caps, project isolation, grant revocation, forged/mutated adapter
results, pluggable local adapters and fail-closed model/rights gates. Actual
Poppler and Tesseract extract self-authored PDF fixtures before context retrieval;
raw/page/text hashes and exact citation offsets survive inspection. No model,
GPU, external provider, hosted CI or third-party paper is used.

Native UI unit tests exercise inert rendering, exact inspection, pinned rebuild,
Close/reopen and empty blocked-rights receipts. TypeScript compilation and the
existing frontend suite are separate checks. These do not establish native
Code - OSS GUI acceptance or live Claude/Codex/Triggerfish behavior.

Remaining gates:

1. Native IDE end-to-end selection, forms, stale navigation, cancellation,
   Close/reopen and reader/clipboard behavior on supported platforms.
2. Source/audience/purpose-specific external processing and redistribution
   policy review, authoritative shared policy integration, then explicit
   end-to-end harness provenance, tokenizer budgets and trust-boundary tests.
3. Semantic retrieval: pinned licensed model/corpus inputs, admission,
   load/unload/cancel/deadline/memory/concurrency tests, index invalidation and
   recall/ranking evaluation against an independently reviewed research set.
4. Graph retrieval: a completed, validated Biff implementation and explicit
   approved jev-graph-builder adapter; real graph build/traversal and edge-to-
   source citation fidelity tests, isolation, stale-index recovery and quality
   comparison. No graph capability is claimed before these pass.
