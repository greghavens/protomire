# Versioned datasets and consumed-row lineage

The dataset registry references externally owned files. It stores immutable
manifests, checksums, sample identities, split membership, rights, and
transformation provenance in the existing application resource store. It does
not copy dataset contents into a hosting service or create another run registry.
ZenML owns artifacts produced when a recipe imports a registered version.

## Shared operations

HTTP, native MCP, and the workbench use the same service methods:

- `dataset.register(project_id, manifest, idempotency_key)` creates revision 1.
- `dataset.version(project_id, dataset_id, expected_revision, manifest,
  idempotency_key)` creates a new immutable revision. Stale writes conflict.
- `dataset.get(project_id, dataset_id, revision?)` reads current or historical
  metadata. Historical manifests are never replaced by new source bytes.
- `dataset.list(project_id)` lists current versions without full sample indexes.
- `dataset.validate(project_id, manifest)` performs a read-only import check.
  Alternatively specify `dataset_id` and optionally `revision` to verify an
  existing version against its external files.
- `dataset.preview(project_id, dataset_id, revision, split_id, purpose,
  offset?, limit?)` verifies the pinned files and displays a page. `limit` is
  1–100, the default is 20, and displayed row content is additionally bounded to
  1 MiB. Preview is not evidence that a model trained on these rows.

Registration/versioning require `modify`; other operations require `read`.
Every call checks project membership. All mutations use the shared transaction,
idempotency, revision, and audit mechanisms. There is no operation that accepts
an absolute directory from a remote client or changes the operator's file roots.

## Operator configuration

Create the project first to obtain its immutable project ID. The operator then
configures roots in the file selected by `MODELBUILDER_OPERATOR_CONFIG` and
restarts the service. Relative configuration paths resolve beside that file:

```json
{
  "dataset_roots": {
    "PROJECT_ID": {
      "data": "./research-data"
    }
  }
}
```

Root aliases are scoped to an individual project. Another project cannot read
that alias unless the operator separately grants it a root. Stored manifests
contain only the alias and a relative filename. Responses do not contain the
operator's absolute directories. `get` and `list` remain usable without any
configured local roots; materialization reports the unavailable capability.

The POSIX reader opens directories and files using descriptor-relative
`O_NOFOLLOW` handles, verifies the startup root inode, rejects symlinks and
nonregular files (including FIFOs), and checks the opened file before/after
reading for concurrent changes. SHA256 is checked on the exact bounded bytes
that are parsed. Existing eager manifests retain limits of 16 MiB per source,
32 MiB total source bytes and 100,000 rows. The manifest supports up to 4,096
explicit source shards and 32 transformations. Select `access_mode: "streaming"`
for larger data; its separate resource caps and index semantics are below.

The Windows reader is implemented in `platform_file_access.py`: it traverses
with native no-reparse handles, checks file identity and metadata, and retains
ancestor handles while reading bounded bytes. Unsupported secure handle or
filesystem capabilities fail closed. Linux-hosted contract tests cover Windows
dispatch and failure paths, but real native Windows execution remains unverified.
This reader does not add native Windows ZenML training support.

## Manifest contract

The complete input contract is
[`schemas/dataset.v1.schema.json`](../schemas/dataset.v1.schema.json), generated
from `modelbuilder.dataset.MANIFEST_SCHEMA` and checked by a regression test.

```json
{
  "name": "Weather descriptions",
  "benchmark": "weather-benchmark-v1",
  "license": "CC0-1.0",
  "provenance": {
    "source": "Locally authored examples",
    "source_revision": "1"
  },
  "sources": [
    {
      "id": "examples",
      "root_id": "data",
      "path": "weather.jsonl",
      "format": "jsonl",
      "sha256": "REPLACE_WITH_SHA256_OF_FILE_BYTES"
    }
  ],
  "id_column": "id",
  "row_schema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "id": {"type": "string"},
      "text": {"type": "string"},
      "label": {"type": "integer"}
    },
    "required": ["id", "text", "label"]
  },
  "splits": [
    {"id": "train", "role": "train", "sample_ids": ["train-1", "train-2"]},
    {"id": "dev", "role": "validation", "sample_ids": ["dev-1"]},
    {"id": "heldout", "role": "test", "sample_ids": ["heldout-1"]}
  ],
  "transformations": [
    {"kind": "text.normalize", "fields": ["text"], "strip": true, "casefold": true}
  ]
}
```

Example file contents:

```jsonl
{"id":"train-1","text":"  sunny day  ","label":1}
{"id":"train-2","text":"cold rain","label":0}
{"id":"dev-1","text":"warm afternoon","label":1}
{"id":"heldout-1","text":"winter snow","label":0}
```

Sources can be CSV, JSONL, or optional hash-pinned Parquet. CSV uses Python's standard `csv` parser; declared
numeric, boolean, object, array, and null fields use JSON decoding. JSONL rows
must be finite JSON objects. The data schema is a bounded, explicit JSON Schema
subset, validated with the existing `jsonschema` library. It rejects remote
references, custom code, and unsupported schema keywords. IDs must be unique,
nonempty strings. IDs cannot be rewritten by transformations.

A split can select explicit `sample_ids`, preserving that declared order, or
whole `source_ids`. Every source sample belongs to exactly one split before
transformation. Roles are `train`, `validation`, `test`, and `calibration`.
The reader refuses training on validation/test/calibration data, calibration on
other roles, and evaluation on training/calibration data. It also refuses exact
duplicate content crossing splits, even when sample IDs differ.

### Optional real Parquet adapter

`python3.12 scripts/bootstrap.py --parquet --skip-frontend` adds the exact
`pyarrow==25.0.0` profile to the selected service environment. Use
`--backend --parquet` to install the adapter in both separate environments, or
`--backend-only --parquet` for an existing service. Choose fresh environment
directories with `--service-venv` / `--backend-venv` when preserving a frozen
installation. Original service/backend lockfiles are unchanged; the optional
SHA256-locked overlay is under `requirements/profiles/parquet-*.lock`.

The reader accepts a single local Parquet file per source, including bounded
variable-length lists and structs of JSON-compatible scalar types. It refuses
binary, timestamp, decimal and extension values instead of silently coercing
them, as well as duplicate field names and external column-file references.
No partition discovery, remote URI, Hugging Face resolver, pandas conversion,
or arbitrary framework dataset support is implied.

Only already-hashed bounded bytes reach a standalone decoder subprocess. On
Linux it establishes hard limits **before importing PyArrow**: 512 MiB total
virtual address space (including Python, Arrow and runtime baseline), 15 seconds
CPU time with a 16-second hard kill, and 34 MiB output-file size. The parent adds
a 20-second wall deadline, kills/reaps timed-out children, and bounds the anonymous
output before loading JSON. There is no weaker in-process fallback. Two decoder
slots per service/worker process bound concurrent parser work; overload refuses
with a retryable diagnostic. This is resource containment, not a general sandbox
for arbitrary code. Inputs cannot select paths, URLs, plugins, or executable code;
the child receives verified bytes under a sanitized environment.

Thrift metadata limits remain 1 MiB of strings and 100,000 container items.
Preflight bounds cover 256 leaf columns, 4,096 row groups and encoded sizes.
The 64-row decode batch and 32 MiB cumulative decoded/serialized-JSON budget are
logical data limits, not proofs of allocation size: dictionary and nested-list
expansion occurs only inside the hard-capped process. The parent still allocates
bounded JSON rows and application lineage structures; its RSS is not capped at
32 MiB. Page checksums are verified when present; pinned whole-file SHA256
validation remains mandatory.

Dataset registration, versioning, validation, preview, binding and run admission
prepare decoding and row transformations outside the SQLite writer transaction.
Atomic commit rechecks the exact initiating token grant, project/source binding,
recipe and selected dataset revisions, approved root identity, and pinned source
bytes. A second token for the same actor cannot revive revoked work. Concurrent
idempotent calls retain one receipt. Bounded secure source reads/hashes remain in
the commit phase; native parsing and row transformation do not.

The contained decoder is currently enabled only on Linux; Linux x64 has actual
runtime acceptance. macOS and Windows Parquet use fails closed with
`capability_unavailable` until an equivalent hard boundary is verified, even when
the optional PyArrow wheel installs successfully. This does not block the IDE,
project/recipe workflow, JSONL or CSV. Admission diagnoses the optional platform
limit before execution. Linux arm64 is not claimed as native-tested here.

`dataset.bind` records the exact PyArrow requirement in the revisioned recipe.
Admission separately verifies the worker interpreter's installed adapter, so a
service-only installation cannot produce an executable Parquet plan. A missing
adapter or mismatched version fails closed. The source hash, row schema, ordered
split manifest, generator/source declaration and per-row lineage retain the
existing immutable contract. External bytes are never copied into the registry.

Linux x64 tests generate 48 real variable-length sequences with a fixed seed,
write real Parquet and separate schema/split JSON manifests, re-generate matching
bytes, apply a versioned deterministic normalization, and verify original bytes
and metadata remain unchanged. HTTP/MCP gateway binding, actual backend admission,
portable split loading, schema/split drift, duplicate handling, source tampering,
symlinks, metadata bounds and compression/dictionary expansion have dedicated
coverage in `tests/datasets/test_parquet.py`. The parameterized native acceptance
in `tests/datasets/test_native_dataset.py` also passes with Parquet: a separate
service and official MCP client bind three registered splits; the independent
ZenML/Torch harness records 1,800 actual training IDs, checks split separation,
reduces loss and saves weights with real native run/artifact identities. Both
service and worker use separately bootstrapped optional profiles. This does not
complete the broader G07 generator lifecycle, required external benchmark-
contamination check, or native GUI gate.

Run the mandatory optional-profile gate with:

```bash
MODELBUILDER_REQUIRE_PARQUET=1 PYTHONPATH=src .venv/bin/python -m pytest tests/datasets/test_parquet.py tests/datasets/test_parquet_boundary.py tests/portability/test_parquet_profiles.py
```

Without that flag, a base installation lacking PyArrow skips the optional tests;
CI installs `--parquet` and sets the flag so a missing/broken adapter fails.

## Streaming and sharded datasets

Choose **Streaming: large local files and shards** in the workbench's Data →
Register dataset → access mode selector, then apply the manifest fields. The
same `access_mode: "streaming"` declaration works through HTTP and native MCP.
Existing manifests without the field preserve their eager behavior and identity.
Split cards, version inspection, binding, validation and paged previews use the
same operations. Sources remain explicit hash-pinned CSV, JSONL or Parquet files;
there is no remote download, upload, hosted copy or arbitrary directory discovery.

For large splits, declare `source_ids`, for example:

```json
{
  "access_mode": "streaming",
  "splits": [
    {"id": "train", "role": "train", "source_ids": ["train-000", "train-001"]},
    {"id": "validation", "role": "validation", "source_ids": ["validation-000"]}
  ]
}
```

This is a fragment to merge into a complete manifest. Each named shard must have
its own approved root alias, relative path, format and full-file SHA256 entry.
Explicit ordered `sample_ids` remain available for smaller curated splits.

The service streams ingestion and builds a private SQLite lineage index beside
its application database, in `<database-name>.dataset-lineage/`. This contains
sample identities, source row positions/byte spans, row hashes, split membership
and transformation fingerprints. Sampling keeps declared stratum values in a
temporary distinct-value table, erased before publishing the lineage index. Standard
SQLite indexes implement global identity uniqueness, stable deduplication and
training-only decontamination without in-memory ID sets. All transformations
retain their declared ordering, including filtering before schema validation.
The persisted, hash-checked index is reused after service restart. Missing indexes
are reconstructed from pinned sources; changed indexes or source bytes fail closed.

Every row's original content digest and transformed content digest contribute to
an ordered length-prefixed canonical-JSON SHA256 stream. Each split pins its
ordered lineage hash and sample count. The compact version identity includes
source hashes/counts, schema, transformations, split lineages and original manifest.
Records therefore do not embed millions of sample IDs. Preview uses a primary-key
ordinal seek, then decodes only selected rows; it does not materialize or skip
decode the preceding split. Full pinned source and metadata-index hashing still
occurs outside the writer transaction. Commit checks descriptor/inode, size,
mtime and ctime against that verified snapshot, and each returned row is checked
against its pinned original/transformed hashes. Concurrent changes refuse the
operation rather than silently advancing a registered version.

Default streaming caps are operator-controlled with `dataset_limits` in the
operator configuration (all values are positive integers):

```json
{
  "dataset_limits": {
    "max_file_bytes": 68719476736,
    "max_total_bytes": 274877906944,
    "max_rows": 100000000,
    "max_row_bytes": 1048576,
    "max_decoded_bytes": 549755813888,
    "max_index_bytes": 34359738368,
    "max_sampling_strata": 10000,
    "max_report_bytes": 16777216
  }
}
```

These mean 64 GiB per source, 256 GiB source total, 100 million rows, 1 MiB per
row, 512 GiB decoded JSON total and 32 GiB metadata index. Lower deployment caps
are supported, including after restart. Operators may raise caps deliberately;
individual row size retains a hard 16 MiB ceiling. SQLite uses a 2 MiB page cache
and disk-backed sorts, so sufficient local temporary disk is required in addition
to index storage. Preview remains at most 100 rows/1 MiB regardless of these caps.
This is a bounded resource policy, not a claim that every configured maximum has
been benchmarked or is a good fit for a particular computer.

Streaming `rows.sample` uses the same policy validation, constructive quota
planner and SHA256 ranking as eager sampling. It samples each selected split
across all its shards after preceding transformations. `proportional` preserves
the input stratum shares; `equal` requests equal shares. Required strata and
minimum coverage are allocated before checking the declared share tolerance.
An absent split/category, missing selected evidence, infeasible coverage or
infeasible tolerance blocks preparation. Unselected splits are preserved and
need no sampling fields. Source counts are evidence; there is no implicit source
balancing or source cap. Source caps would require a separate explicit policy.

SQLite holds temporary distinct stratum values, ranks and winner membership under the same
index budget, using indexed quota seeks rather than Python collections of IDs
or row bodies. The default sampling cap allows 10,000 split/stratum pairs per
stage, and the transformation report is capped at 16 MiB. Both caps are operator
configurable and rechecked when reusing an index, including after restart.
Report budgets use canonical UTF-8 JSON consistently during construction and
cache reuse, irrespective of SQLite's internal JSON escaping. Physical index
high-water bytes are recorded in `lineage_index` and enforced with actual file
size on reuse; they do not contribute to semantic lineage/content/version hashes.
Temporary values are erased and ranking/winner tables removed after membership is chosen; source bytes and
all original row lineage remain pinned. Output follows original source order;
consumption follows any explicit split order.

Sampling reports retain representation, source counts, selected count and the
same policy and canonical selection digests as eager access. They omit the
unbounded selected-ID list. Unicode selection digests use the eager canonical
JSON escaping. Overall streaming dataset/version/consumption identities retain
their existing schemas. Preparation records examples available for use; only
the consumption tracker records examples actually consumed.

Compilation cooperatively observes the existing worker `cancel.json` signal
during hashing, ingestion, SQLite ranking/filtering and publication preparation.
Cancelled compilation removes its incomplete index and publishes no version.
Local callers can scope that same signal with `compilation_context(cancel_file)`;
portable loading accepts `cancel_file`. Parquet continues to require the pinned
optional decoder and verified Linux containment; Mac/Windows reject at that
boundary rather than decoding in-process.
The decoder's bounded pipe wait also observes that signal while output is stalled
or only a partial record is available. Its existing ten-minute stall deadline,
output bounds, decoder slots and owned-child cleanup remain enforced.

Streaming Parquet retains the optional exact PyArrow pin and Linux-only hard
memory boundary. It passes an already-open, checked descriptor to an isolated
worker, streams 64-row batches through a bounded pipe, rejects remote/external
column references and extension values, verifies available page checksums, and
limits each row group's advertised uncompressed data to 256 MiB. The process is
limited to 512 MiB address space, 590/591 seconds CPU and a 600-second wall deadline
per source/group read; two decoder slots bound concurrency. Random previews read
only the relevant bounded row groups. These streaming limits are separate from
the smaller eager Parquet decoder described above.

Runtime dataset imports return a compact `modelbuilder.dataset-stream/v1`
descriptor. Components consume `modelbuilder.dataset_streaming.iter_stream_rows`
in bounded memory, with full source/version/split verification and per-row lineage.
`StreamingConsumption` records only rows delivered to the actual batch consumer,
checks their indexed membership, and exposes ordered SHA256/count checkpoints as
`modelbuilder.dataset-consumption/v2`. Repeated epochs preserve repeated ordered
row identities. A preview or import descriptor never claims model consumption.
The descriptor's runtime-approved paths are worker-only; saved recipes, portable
references, consumption snapshots and service dataset metadata contain no local
absolute paths. Components that require eager row lists must explicitly support
this streaming interface before receiving a streaming dataset.

For bounded exact-ID evaluation, `materialize_stream_split(data, max_rows=...,
max_bytes=...)` materializes only the selected held-out split. Its immutable row
and decoded-byte counts are checked before row reads, and it retains the same
dataset/version with full sample IDs and per-row hashes. Training and evaluation
share one runtime metadata index. This lets a large training split and a small
held-out split stay in one registered manifest; no second registration is needed.
Consumers supply their explicit budgets. Oversized evaluation splits fail closed
instead of silently truncating evidence or loading the full training dataset.

Tests cover a real JSONL source over 32 MiB with 110,001 rows, incremental peak RSS
below 48 MiB, restart and historical pages that forbid full-source decoding,
indexed deep-page seeks, CSV multiline records, real multi-row-group Parquet,
source/index tampering, concurrent source changes, ordered filters and split
lineage, operator limits, and independent native-MCP lifecycle/restart. Runtime
training acceptance is covered separately by the standard Trainer tests.

## Declared transformations

The following deterministic operations use Python's mature standard libraries;
they do not evaluate expressions or import functions supplied in a manifest:

- `text.normalize`: `unicodedata.normalize`, optional trim, and optional
  casefold on specified fields. Unicode data version is recorded.
- `rows.drop_missing`: remove rows with null, missing, or empty selected fields.
- `columns.select`: retain declared columns, always preserving the ID column.
- `rows.deduplicate`: stable exact-match deduplication within each split.
- `rows.decontaminate`: remove matching **training** rows when their selected
  fields occur in validation/test/calibration. Evaluation rows stay unchanged.

Deduplication/decontamination default to all non-ID fields, or accept explicit
fields. These are exact-match operations, not semantic/fuzzy contamination
detectors. Transform order is part of the immutable manifest. Each operation
records its declaration, implementation version, input/output counts, and
removed sample IDs. Original row and final content hashes remain in lineage.
Empty resulting splits, overlapping identities, and unresolved fields fail.

## Runtime artifacts and portability

`DatasetRegistry.portable_reference(project_id, dataset_id, revision)` returns
an inert JSON reference containing the immutable identity, manifest, and
content-addressed file dependencies. It never includes operator directories.
Persist that JSON as an external artifact and pin its hash in the recipe;
resolve its dependency and every `dataset-{sha256}` source through the
operator's dependency map. No import downloads or publishes files.

The `dataset.version.import` recipe node takes:

```json
{
  "reference_id": "registered-version",
  "split_id": "train",
  "purpose": "training"
}
```

Its native ZenML output is a JSON dataset artifact containing arbitrary-schema
`rows`, the evaluation-compatible `dataset` and `split` identities, and a
`consumption` selection manifest for the yielded rows. It can connect to a
trusted `component.run` harness using an ordinary `dict` input. The worker
checks the reference hash, project identity, declared dependency closure,
every source hash, version identity, split roles, and transformation output.
It does not need a service database or the originating root aliases.

`load_portable_split(reference, dependency_paths, split_id, purpose=...,
expected_project_id=...)` is the same helper used by the native step. The
dependency mapping must come from trusted operator admission, never from the
reference JSON itself.

Harnesses must record IDs they actually consume, not call registration proof
of training. `consumption(record, split_id, actual_ordered_ids, purpose=...)`
validates subsets and repeated IDs (multiple epochs), preserves order, binds
every content digest, and reports total versus unique counts. The import
artifact's selection is available to the harness; model consumption must be
reported by that harness's execution. No training loop is implemented here.

`evaluation_identity(record, split_id)` returns the existing `evaluation.py`
identity contract. It does not invent a competing evaluator or comparison
schema. Dataset revisions, content changes, and split changes make evaluations
incompatible; rankings require reevaluation on a common pinned version.

Old source bytes remain the owner's responsibility. Creating a new version
does not snapshot or silently restore deleted external files. Checksums and
historical metadata remain available, and unavailable/tampered old bytes fail
verification rather than being relabeled.

## Verified scope

Tests in `tests/datasets/` cover real CSV/JSONL registration, versions, ordering,
transformations, consumed IDs, split protection, tamper refusal, file bounds,
symlinks, root replacement, read-time mutation, FIFO refusal, project isolation,
service authorization/idempotency/audit, and evaluation incompatibility.

The real native test uses service-created records and three dataset imports
feeding an unrelated source-pinned PyTorch MLP. It records 1,800 actual training
IDs, checks dev/held-out separation, measures loss reduction, saves weights,
and verifies real ZenML run/artifact IDs. The independent official SDK MCP test
uses a separate stdio server and HTTP service process, including service restart,
historical reads, stale writes, preview, checksum failure, and read-only denial.
These are separate gates; they are not authenticated Claude/Codex acceptance.

HF retrieval, remote/paid generation, synthetic-generator lifecycle, fuzzy
decontamination and large streaming datasets remain additional implementation
work. Native Windows file access is implemented but still needs real Windows
acceptance. No remote costs, uploads, Biff mutation, or private
code copies are involved in this implementation.
