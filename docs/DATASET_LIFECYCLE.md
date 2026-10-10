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
that are parsed. Limits are 16 MiB per source, 32 MiB total source bytes, 100,000
rows, 64 sources, and 32 declared transformations.

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

Sources can be CSV or JSONL. CSV uses Python's standard `csv` parser; declared
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

PyArrow-based Parquet decoding is implemented as an optional adapter, with
metadata row and decoded-batch byte limits. PyArrow is **not installed in the
pinned tested environments**: requesting Parquet there returns
`capability_unavailable`. It has not passed a real Parquet acceptance gate and
is not claimed as supported by this delivery.

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
