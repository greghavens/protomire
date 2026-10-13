"""Immutable, project-scoped dataset references and reproducible local adapters.

This registry stores manifests and row lineage, never a second dataset hosting
service. Every use rereads and verifies pinned external bytes. It neither imports
Python from a manifest nor downloads data. The service owns authorization,
transactions, idempotency and audit; the registry independently checks ownership.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .evaluation import digest
from .rules import SAMPLE_SCHEMA, sample_rows
from .generated_artifacts import MANIFEST_FIELDS, manifest_rights
from .models import ServiceError, new_id, utcnow
from .platform_file_access import WindowsRoot, read_windows_source, snapshot_windows_root

SCHEMA = "modelbuilder.dataset/v1"
CONSUMPTION_SCHEMA = "modelbuilder.dataset-consumption/v1"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ROWS = 100_000
PYARROW_REQUIREMENT = "pyarrow==25.0.0"
PARQUET_METADATA_BYTES = 1024 * 1024
PARQUET_METADATA_ITEMS = 100_000
_MATERIALIZATIONS = ContextVar("modelbuilder_dataset_materializations", default=None)


@contextmanager
def materialization_session(entries, *, compute):
    """Request-local verified preparation, never a cross-request content cache."""
    token = _MATERIALIZATIONS.set((entries, compute))
    try:
        yield
    finally:
        _MATERIALIZATIONS.reset(token)


def recheck_materialized_sources(entries):
    # Reread pinned bytes after the expensive decode and before the commit phase.
    for entry in entries.values():
        if "value" in entry:
            for source in entry["value"][0]["sources"]:
                entry["reader"]._verify_source(entry["project_id"], source, entry["value"][0])


_ID = {"type": "string", "minLength": 1, "maxLength": 200}
_FIELDS = {"type": "array", "items": _ID, "minItems": 1, "maxItems": 256, "uniqueItems": True}
_SOURCE = {"type": "object", "additionalProperties": False,
           "required": ["id", "root_id", "path", "format", "sha256"],
           "properties": {"id": _ID, "root_id": _ID,
                          "path": {"type": "string", "minLength": 1, "maxLength": 2048},
                          "format": {"enum": ["csv", "jsonl", "parquet"]},
                          "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}}
_SPLIT = {"type": "object", "additionalProperties": False, "required": ["id", "role"],
          "properties": {"id": _ID, "role": {"enum": ["train", "validation", "test", "calibration"]},
                         "source_ids": {"type": "array", "items": _ID, "minItems": 1, "maxItems": 4096, "uniqueItems": True},
                         "sample_ids": {"type": "array", "items": _ID, "minItems": 1,
                                        "maxItems": MAX_ROWS, "uniqueItems": True}},
          "oneOf": [{"required": ["source_ids"]}, {"required": ["sample_ids"]}]}


def _transform_schema(kind, properties=None, required=()):
    return {"type": "object", "additionalProperties": False,
            "required": ["kind", *required], "properties": {"kind": {"const": kind}, **(properties or {})}}


MANIFEST_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "additionalProperties": False,
    "required": ["name", "benchmark", "license", "provenance", "sources", "id_column", "row_schema", "splits"],
    "properties": {
        **MANIFEST_FIELDS,
        "name": _ID, "description": {"type": "string", "maxLength": 10000}, "benchmark": _ID,
        "license": {"type": "string", "minLength": 1, "maxLength": 2048},
        "provenance": {"type": "object", "additionalProperties": False, "required": ["source"],
                       "properties": {"source": {"type": "string", "minLength": 1, "maxLength": 2048},
                                      "source_revision": _ID, "retrieved_at": _ID,
                                      "description": {"type": "string", "maxLength": 10000}}},
        "sources": {"type": "array", "items": _SOURCE, "minItems": 1, "maxItems": 4096},
        "access_mode": {"enum": ["eager", "streaming"]},
        "id_column": _ID, "row_schema": {"type": "object"},
        "splits": {"type": "array", "items": _SPLIT, "minItems": 1, "maxItems": 32},
        "transformations": {"type": "array", "maxItems": 32, "items": {"oneOf": [
            SAMPLE_SCHEMA,
            _transform_schema("text.normalize", {"fields": _FIELDS,
                "unicode_form": {"enum": ["NFC", "NFD", "NFKC", "NFKD"]},
                "strip": {"type": "boolean"}, "casefold": {"type": "boolean"}}, ("fields",)),
            _transform_schema("rows.drop_missing", {"fields": _FIELDS}, ("fields",)),
            _transform_schema("rows.deduplicate", {"fields": _FIELDS}),
            _transform_schema("rows.decontaminate", {"fields": _FIELDS}),
            _transform_schema("columns.select", {"fields": _FIELDS}, ("fields",)),
        ]}},
    },
}


def _fail(code, message, **details):
    raise ServiceError(code, message, details)


def validate_manifest(manifest: dict) -> dict:
    """Return a detached normalized declaration, rejecting executable schemas."""
    errors = sorted(Draft202012Validator(MANIFEST_SCHEMA).iter_errors(manifest), key=lambda e: str(e.path))
    if errors:
        _fail("dataset_manifest_invalid", "Dataset manifest does not match its schema",
              fields=[".".join(map(str, e.path)) for e in errors[:20]])
    try:
        manifest_rights(manifest)
    except (ValueError, TypeError, KeyError, ValidationError) as error:
        _fail("dataset_manifest_invalid", "Generated dataset parent rights lineage is invalid", reason=str(error))
    result = copy.deepcopy(manifest)
    result.setdefault("description", "")
    result.setdefault("transformations", [])
    schema = result["row_schema"]
    # A deliberately bounded JSON Schema subset. No resolvers, remote refs,
    # plugins, custom formats, backtracking regexes or executable predicates.
    supported = {"type", "properties", "required", "additionalProperties", "items", "enum", "minimum", "maximum",
                 "minLength", "maxLength", "minItems", "maxItems", "description"}

    def check_schema(value, depth=0):
        if not isinstance(value, dict) or set(value) - supported or depth > 8:
            _fail("dataset_schema_unsupported", "Row schema uses unsupported keywords or nesting")
        if not isinstance(value.get("type"), str) or value["type"] not in {
                "object", "array", "string", "number", "integer", "boolean", "null"}:
            _fail("dataset_schema_unsupported", "Row schema needs an explicit supported type")
        properties = value.get("properties", {})
        if not isinstance(properties, dict) or len(properties) > 256:
            _fail("dataset_schema_unsupported", "Row schema has too many or invalid properties")
        for child in properties.values():
            check_schema(child, depth + 1)
        if "items" in value:
            check_schema(value["items"], depth + 1)
        if "additionalProperties" in value and not isinstance(value["additionalProperties"], bool):
            _fail("dataset_schema_unsupported", "Additional properties must be explicitly allowed or forbidden")
    check_schema(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ServiceError("dataset_schema_invalid", "Dataset row schema is invalid") from exc
    if schema.get("type") != "object" or result["id_column"] not in schema.get("properties", {}):
        _fail("dataset_schema_invalid", "Row schema must declare the sample identity column")
    if schema["properties"][result["id_column"]].get("type") != "string":
        _fail("dataset_schema_invalid", "Sample identities must be strings")
    for group in ("sources", "splits"):
        values = [item["id"] for item in result[group]]
        if len(values) != len(set(values)):
            _fail("dataset_manifest_invalid", "Source and split identities must be unique", field=group)
    for source in result["sources"]:
        relative = PurePosixPath(source["path"])
        if (relative.is_absolute() or not relative.parts or str(relative) != source["path"] or ".." in relative.parts
                or "\\" in source["path"] or ":" in source["path"] or "\x00" in source["path"]):
            _fail("dataset_path_invalid", "Dataset paths must be canonical relative paths")
    for transform in result["transformations"]:
        if result["id_column"] in transform.get("fields", []) and transform["kind"] != "columns.select":
            _fail("dataset_transform_invalid", "Transforms cannot change or use the identity column as content")
        if transform["kind"] == "columns.select" and result["id_column"] not in transform["fields"]:
            _fail("dataset_transform_invalid", "Column selection must preserve sample identities")
    return result


class DatasetRegistry:
    def __init__(self, store, *, project_roots=None, max_file_bytes=MAX_FILE_BYTES,
                 max_total_bytes=MAX_TOTAL_BYTES, max_rows=MAX_ROWS, streaming_limits=None):
        self.store = store
        import tempfile
        from .dataset_streaming import limits
        self.streaming_limits = limits(streaming_limits)
        self._stream_temp = tempfile.TemporaryDirectory(prefix="modelbuilder-lineage-")
        self._stream_directory = self._stream_temp.name
        if store is not None and getattr(store, "path", ":memory:") != ":memory:":
            directory = Path(store.path).absolute().with_suffix(".dataset-lineage")
            if directory.is_symlink():
                raise ValueError("Dataset lineage cache cannot be a symlink")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._stream_directory = str(directory)
        self._stream_indexes = {}
        self._stream_records = {}
        self._stream_signatures = {}
        self.max_file_bytes, self.max_total_bytes, self.max_rows = max_file_bytes, max_total_bytes, max_rows
        self._roots = {}
        for project_id, roots in (project_roots or {}).items():
            if not isinstance(roots, dict):
                raise ValueError("Dataset roots must map projects to root aliases")
            self._roots[project_id] = {}
            for alias, value in roots.items():
                if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", alias):
                    raise ValueError("Dataset root aliases must be simple identifiers")
                path = Path(value).resolve(strict=True)
                info = path.stat()
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError("Configured dataset root is not a directory")
                self._roots[project_id][alias] = (snapshot_windows_root(path) if os.name == "nt"
                                                  else (path, info.st_dev, info.st_ino))

    def _read_source(self, project_id, source):
        root = self._roots.get(project_id, {}).get(source["root_id"])
        if root is None:
            _fail("dataset_root_unavailable", "Dataset root alias is not configured for this project")
        if isinstance(root, WindowsRoot):
            try:
                data = read_windows_source(root, source["path"], self.max_file_bytes)
            except OSError as exc:
                raise ServiceError("dataset_source_unavailable", "Dataset source is missing, unsafe, or unreadable",
                                   {"source_id": source["id"]}) from exc
            if hashlib.sha256(data).hexdigest() != source["sha256"]:
                _fail("dataset_checksum_mismatch", "Dataset source checksum differs from the immutable manifest",
                      source_id=source["id"])
            return data
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            _fail("capability_unavailable", "Secure descriptor-relative dataset reads are unavailable on this platform")
        handles = []
        try:
            fd = os.open(root[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            handles.append(fd)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != root[1:]:
                _fail("dataset_root_changed", "Configured dataset root changed since service startup")
            parts = PurePosixPath(source["path"]).parts
            for part in parts[:-1]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                handles.append(fd)
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            handles.append(fd)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                _fail("dataset_source_invalid", "Dataset source must be a regular file")
            if before.st_size > self.max_file_bytes:
                _fail("dataset_limit_exceeded", "Dataset source exceeds the configured byte limit")
            chunks, size = [], 0
            while chunk := os.read(fd, min(65536, self.max_file_bytes + 1 - size)):
                chunks.append(chunk)
                size += len(chunk)
                if size > self.max_file_bytes:
                    _fail("dataset_limit_exceeded", "Dataset source exceeds the configured byte limit")
            after = os.fstat(fd)
            def signature(info):
                return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if signature(before) != signature(after):
                _fail("dataset_source_changed", "Dataset source changed while it was being read")
            data = b"".join(chunks)
            if hashlib.sha256(data).hexdigest() != source["sha256"]:
                _fail("dataset_checksum_mismatch", "Dataset source checksum differs from the immutable manifest",
                      source_id=source["id"])
            return data
        except (OSError, IndexError) as exc:
            raise ServiceError("dataset_source_unavailable", "Dataset source is missing, unsafe, or unreadable",
                               {"source_id": source["id"]}) from exc
        finally:
            for handle in reversed(handles):
                os.close(handle)

    def _verify_source(self, project_id, source, manifest, stream=None):
        if manifest.get("access_mode") == "streaming":
            from .dataset_streaming import verify_source, verify_prepared_source
            session = _MATERIALIZATIONS.get()
            if session is not None and not session[1]:
                entry = next((entry for entry in session[0].values() if "value" in entry
                              and entry.get("project_id") == project_id and entry["value"][0] == manifest), None)
                if entry is None:
                    _fail("dataset_preparation_required", "Streaming source was not verified before the writer transaction")
                expected = entry["reader"]._stream_signatures.get((project_id, source["id"], source["sha256"]))
                return verify_prepared_source(self, project_id, source, expected, stream)
            return verify_source(self, project_id, source, stream)
        return self._read_source(project_id, source)

    def _parse(self, data, source, schema, *, decoded_budget=None):
        decoded_budget = self.max_total_bytes if decoded_budget is None else decoded_budget
        fmt = source["format"]
        try:
            if fmt == "jsonl":
                rows = []
                for line in io.StringIO(data.decode("utf-8-sig")):
                    if not line.strip():
                        continue
                    if len(rows) >= self.max_rows:
                        _fail("dataset_limit_exceeded", "Dataset exceeds the configured row limit")
                    rows.append(json.loads(line))
            elif fmt == "csv":
                reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
                if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                    _fail("dataset_source_invalid", "CSV header must contain unique column names")
                rows = []
                for row in reader:
                    if len(rows) >= self.max_rows:
                        _fail("dataset_limit_exceeded", "Dataset exceeds the configured row limit")
                    if None in row or None in row.values():
                        _fail("dataset_source_invalid", "CSV row does not match its header")
                    for field, value in row.items():
                        kind = schema.get("properties", {}).get(field, {}).get("type")
                        if kind in {"number", "integer", "boolean", "null", "array", "object"}:
                            row[field] = json.loads(value)
                    rows.append(row)
            else:
                rows = self._parse_parquet(data, source, decoded_budget)
            if len(rows) > self.max_rows:
                _fail("dataset_limit_exceeded", "Dataset exceeds the configured row limit")
            if any(not isinstance(row, dict) for row in rows):
                _fail("dataset_source_invalid", "Dataset rows must be JSON objects")
            # Reject NaN/Infinity and nonportable Parquet values (timestamps,
            # bytes, extension objects) rather than silently changing semantics.
            size = 0
            for row in rows:
                size += len(json.dumps(row, allow_nan=False).encode("utf-8"))
                if size > decoded_budget:
                    _fail("dataset_limit_exceeded", "Decoded dataset exceeds the configured byte limit")
            return rows
        except (UnicodeError, ValueError, TypeError, RecursionError, csv.Error) as exc:
            raise ServiceError("dataset_source_invalid", "Dataset rows cannot be decoded as finite JSON objects",
                               {"source_id": source["id"]}) from exc

    def _parse_parquet(self, data, source, decoded_budget):
        from .parquet_reader import read_parquet
        return read_parquet(data, max_file_bytes=self.max_file_bytes,
                            max_rows=self.max_rows, decoded_budget=decoded_budget)

    def _materialize(self, project_id, manifest):
        session = _MATERIALIZATIONS.get()
        if session is None:
            return self._materialize_unprepared(project_id, manifest)
        entries, compute = session
        key = (project_id, digest(manifest), self.max_file_bytes, self.max_total_bytes, self.max_rows, digest(self.streaming_limits))
        if key in entries:
            entry = entries[key]
            if "error" in entry:
                raise entry["error"]
            # A different portable reader must prove its own approved path/root
            # still supplies the exact same pinned bytes before sharing values.
            for source in entry["value"][0]["sources"]:
                self._verify_source(project_id, source, entry["value"][0])
            return entry["value"]
        if not compute:
            _fail("dataset_preparation_required", "Dataset decoding was not prepared outside the writer transaction")
        try:
            value = self._materialize_unprepared(project_id, manifest)
        except ServiceError as exc:
            entries[key] = {"error": exc}
            raise
        entries[key] = {"value": value, "reader": self, "project_id": project_id}
        return value

    def _materialize_unprepared(self, project_id, manifest):
        manifest = validate_manifest(manifest)
        if manifest.get("access_mode") == "streaming":
            from .dataset_streaming import compile_index
            return compile_index(self, project_id, manifest)
        indexed, total, decoded_total, seen = [], 0, 0, set()
        id_column = manifest["id_column"]
        for source in manifest["sources"]:
            data = self._read_source(project_id, source)
            total += len(data)
            if total > self.max_total_bytes:
                _fail("dataset_limit_exceeded", "Combined dataset sources exceed the configured byte limit")
            rows = self._parse(data, source, manifest["row_schema"],
                               decoded_budget=self.max_total_bytes - decoded_total)
            for number, row in enumerate(rows, 1):
                decoded_total += len(json.dumps(row, allow_nan=False).encode("utf-8"))
                sample_id = row.get(id_column)
                if not isinstance(sample_id, str) or not sample_id or len(sample_id) > 200:
                    _fail("dataset_sample_invalid", "Every sample needs a nonempty string identity")
                if sample_id in seen:
                    _fail("dataset_sample_overlap", "Sample identities must be unique across all sources", sample_id=sample_id)
                seen.add(sample_id)
                indexed.append({"sample_id": sample_id, "source_id": source["id"], "source_row": number,
                                "source_content_digest": digest(row),
                                "row": row, "split_id": None})
                if len(indexed) > self.max_rows:
                    _fail("dataset_limit_exceeded", "Dataset exceeds the configured row limit")
        source_ids = {s["id"] for s in manifest["sources"]}
        by_id = {item["sample_id"]: item for item in indexed}
        for split in manifest["splits"]:
            if "source_ids" in split:
                if set(split["source_ids"]) - source_ids:
                    _fail("dataset_split_invalid", "Split refers to an unknown source")
                selected = [item for item in indexed if item["source_id"] in split["source_ids"]]
            else:
                if set(split["sample_ids"]) - set(by_id):
                    _fail("dataset_split_invalid", "Split refers to an unknown sample")
                selected = [by_id[sample_id] for sample_id in split["sample_ids"]]
            for item in selected:
                if item["split_id"] is not None:
                    _fail("dataset_split_overlap", "A sample cannot belong to multiple splits", sample_id=item["sample_id"])
                item["split_id"] = split["id"]
        if any(item["split_id"] is None for item in indexed):
            _fail("dataset_split_invalid", "Splits must assign every source sample exactly once")
        indexed, report = _transform(indexed, manifest)
        validator = Draft202012Validator(manifest["row_schema"])
        for item in indexed:
            errors = list(validator.iter_errors(item["row"]))
            if errors:
                _fail("dataset_row_invalid", "A transformed row differs from the declared schema",
                      sample_id=item["sample_id"], fields=[".".join(map(str, e.path)) for e in errors[:10]])
            item["content_digest"] = digest({k: v for k, v in item["row"].items() if k != id_column})
        content_splits = {}
        for item in indexed:
            previous = content_splits.setdefault(item["content_digest"], item["split_id"])
            if previous != item["split_id"]:
                _fail("dataset_content_overlap", "Identical sample content crosses split boundaries; explicitly decontaminate training data")
        splits = []
        for spec in manifest["splits"]:
            # Preserve declared explicit item order for order-sensitive protocols.
            values = [item["sample_id"] for item in indexed if item["split_id"] == spec["id"]]
            if "sample_ids" in spec:
                present = set(values)
                values = [sample_id for sample_id in spec["sample_ids"] if sample_id in present]
            if not values:
                _fail("dataset_split_empty", "Every declared split must retain at least one sample", split_id=spec["id"])
            split = {"id": spec["id"], "role": spec["role"], "sample_ids": values}
            split["digest"] = digest(split)
            splits.append(split)
        return manifest, indexed, splits, report

    def register(self, project_id, manifest):
        self._project(project_id)
        record = self._build(project_id, new_id(), 1, manifest)
        self.store.put("dataset", record["dataset_id"], project_id, record)
        return record

    def version(self, project_id, dataset_id, expected_revision, manifest):
        previous = self.get(project_id, dataset_id)
        if previous["revision"] != expected_revision:
            raise ServiceError("revision_conflict", "Dataset changed since it was read",
                               {"expected_revision": expected_revision, "current_revision": previous["revision"]}, 409)
        record = self._build(project_id, dataset_id, expected_revision + 1, manifest, previous["version_id"])
        self.store.put("dataset", dataset_id, project_id, record, expected_revision)
        return record

    def _project(self, project_id):
        project = self.store.get("project", project_id)
        if project.get("project_id") != project_id:
            raise ServiceError("not_found", "Project was not found", status_code=404)

    def get(self, project_id, dataset_id, revision=None):
        self._project(project_id)
        # Check current ownership before looking up a requested historical version.
        current = self.store.get("dataset", dataset_id)
        if current.get("project_id") != project_id:
            raise ServiceError("not_found", "Dataset was not found in this project", status_code=404)
        record = self.store.get("dataset", dataset_id, revision) if revision is not None else current
        if record.get("project_id") != project_id:
            raise ServiceError("not_found", "Dataset was not found in this project", status_code=404)
        if record.get("access_mode") == "streaming":
            self._stream_records[(project_id, record["manifest_digest"])] = record
        return record

    def list(self, project_id):
        self._project(project_id)
        return {"datasets": [{k: v for k, v in record.items() if k not in {"manifest", "sample_index"}}
                             for record in self.store.list("dataset", project_id)]}

    def _build(self, project_id, dataset_id, revision, declaration, parent=None):
        manifest, indexed, splits, report = self._materialize(project_id, declaration)
        return _version_record(project_id, dataset_id, revision, manifest, indexed, splits, report, parent)

    def validate(self, project_id, manifest=None, *, dataset_id=None, revision=None):
        self._project(project_id)
        if (manifest is None) == (dataset_id is None):
            _fail("dataset_request_invalid", "Validate either a manifest or an existing dataset identity")
        if manifest is not None and revision is not None:
            _fail("dataset_request_invalid", "A revision applies only to a registered dataset")
        record = self.get(project_id, dataset_id, revision) if dataset_id is not None else None
        try:
            result = self._build(project_id, dataset_id or "validation", record["revision"] if record else 1,
                                 record["manifest"] if record else manifest)
            if record and result["version_id"] != record["version_id"]:
                _fail("dataset_identity_mismatch", "Materialized dataset differs from the registered immutable version")
            return {"valid": True, "diagnostics": [], "summary": {
                "row_count": result["row_count"], "content_digest": result["content_digest"],
                "version_id": result["version_id"], "splits": result["splits"], "transformations": result["transformations"]}}
        except ServiceError as exc:
            return {"valid": False, "diagnostics": [{"code": exc.code, "message": exc.message, **exc.details}]}

    def read_split(self, project_id, dataset_id, revision, split_id, *, purpose):
        record = self.get(project_id, dataset_id, revision)
        materialized = self._materialize(project_id, record["manifest"])
        if record.get("access_mode") == "streaming":
            from .dataset_streaming import check_record, stream_data
            check_record(record, materialized)
            paths, guards = self.execution_sources(project_id, dataset_id, revision)
            return stream_data(record, self.portable_reference(project_id, dataset_id, revision), paths, guards,
                               split_id, purpose, self.streaming_limits)
        return _split_data(record, materialized, split_id, purpose)

    def portable_reference(self, project_id, dataset_id, revision):
        """Portable manifest and dependency closure; never return operator paths."""
        record = self.get(project_id, dataset_id, revision)
        return {"schema": SCHEMA, "project_id": project_id, "dataset_id": dataset_id, "revision": record["revision"],
                "version_id": record["version_id"], "content_digest": record["content_digest"], "manifest": record["manifest"],
                **({"streaming_limits": self.streaming_limits} if record.get("access_mode") == "streaming" else {}),
                "dependencies": [{"id": "dataset-" + source["sha256"], "sha256": source["sha256"],
                                  "kind": "dataset", "license": record["license"],
                                  "source_id": source["id"], "required": True}
                                 for source in record["manifest"]["sources"]]}

    def execution_sources(self, project_id, dataset_id, revision):
        """Server-only approved locations and startup root identities, never public metadata."""
        record = self.get(project_id, dataset_id, revision)
        paths, guards = {}, {}
        for source in record["manifest"]["sources"]:
            # Independently reread via the captured approved root, not a resolved client path.
            self._verify_source(project_id, source, record["manifest"])
            root = self._roots[project_id][source["root_id"]]
            if isinstance(root, WindowsRoot):
                _fail("capability_unavailable", "Native Windows dataset execution requires a supported backend host")
            key = "dataset-" + source["sha256"]
            paths[key] = str(root[0] / source["path"])
            guards[key] = {"root": str(root[0]), "device": root[1], "inode": root[2], "path": source["path"]}
        return paths, guards

    def preview(self, project_id, dataset_id, revision, split_id, *, purpose, offset=0, limit=20):
        """Bound displayed rows/IDs; a preview is never evidence of model consumption."""
        if (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
                or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
            _fail("dataset_request_invalid", "Preview requires a positive limit at most 100 and a nonnegative offset")
        record = self.get(project_id, dataset_id, revision)
        if record.get("access_mode") == "streaming":
            from .dataset_streaming import preview
            return preview(record, self._materialize(project_id, record["manifest"]), split_id, purpose, offset, limit)
        data = self.read_split(project_id, dataset_id, revision, split_id, purpose=purpose)
        rows, size = [], 0
        for row in data["rows"][offset:offset + limit]:
            size += len(json.dumps(row, allow_nan=False).encode())
            if size > 1024 * 1024:
                if not rows:
                    _fail("dataset_limit_exceeded", "One dataset row exceeds the preview byte limit")
                break
            rows.append(row)
        selected = data["split"]["sample_ids"][offset:offset + len(rows)]
        return {"dataset": data["dataset"], "version_id": data["consumption"]["version_id"],
                "split": {"id": split_id, "role": data["split"]["role"],
                          "sample_count": len(data["rows"]), "digest": digest(data["split"])},
                "rows": rows, "sample_ids": selected,
                "offset": offset, "limit": limit, "total": len(data["rows"]),
                "has_more": offset + len(rows) < len(data["rows"])}


def _version_record(project_id, dataset_id, revision, manifest, indexed, splits, report, parent=None):
    if manifest.get("access_mode") == "streaming":
        from .dataset_streaming import version_record
        return version_record(project_id, dataset_id, revision, manifest, indexed, splits, report, parent)
    sample_index = [{k: v for k, v in item.items() if k != "row"} for item in indexed]
    content_digest = digest({"sample_index": sample_index, "splits": splits, "row_schema": manifest["row_schema"]})
    version_id = digest({"manifest": manifest, "content_digest": content_digest})
    return {"schema": SCHEMA, "project_id": project_id, "dataset_id": dataset_id, "revision": revision,
            "version_id": version_id, "parent_version_id": parent, "created_at": utcnow(),
            "name": manifest["name"], "benchmark": manifest["benchmark"], "license": manifest["license"],
            "manifest": manifest, "manifest_digest": digest(manifest), "content_digest": content_digest,
            "row_count": len(indexed), "sample_index": sample_index, "splits": splits,
            "transformations": report}


def _split_data(record, materialized, split_id, purpose):
    manifest, indexed, splits, _ = materialized
    sample_index = [{k: v for k, v in item.items() if k != "row"} for item in indexed]
    content = digest({"sample_index": sample_index, "splits": splits, "row_schema": manifest["row_schema"]})
    if content != record["content_digest"] or digest({"manifest": manifest, "content_digest": content}) != record["version_id"]:
        _fail("dataset_identity_mismatch", "Dataset materialization changed since registration")
    identity = evaluation_identity(record, split_id)
    lineage = consumption(record, split_id, identity["split"]["sample_ids"], purpose=purpose)
    lookup = {item["sample_id"]: item["row"] for item in indexed}
    return {**identity, "rows": [lookup[sample_id] for sample_id in identity["split"]["sample_ids"]],
            "consumption": lineage,
            **({"rights": copy.deepcopy(manifest_rights(manifest))} if manifest_rights(manifest) is not None else {})}


def load_portable_split(reference, dependency_paths, split_id, *, purpose, expected_project_id=None, source_guards=None,
                        streaming_limits=None, _return_index=False, cancel_file=None):
    """Read a portable registered version inside a worker without a service DB.

    The caller obtains dependency_paths from operator admission, never from
    manifest payloads. A recipe pins the JSON reference itself as a dependency.
    Every source is reopened through a no-symlink descriptor and checked again.
    """
    required = {"schema", "project_id", "dataset_id", "revision", "version_id", "content_digest", "manifest", "dependencies"}
    if not isinstance(reference, dict) or (set(reference) - {"streaming_limits"}) != required or reference.get("schema") != SCHEMA:
        _fail("dataset_reference_invalid", "Portable dataset reference has an unsupported shape")
    for key in ("project_id", "dataset_id"):
        if not isinstance(reference[key], str) or not reference[key]:
            _fail("dataset_reference_invalid", "Portable dataset reference lacks immutable identities")
    if (not isinstance(reference["revision"], int) or isinstance(reference["revision"], bool)
            or reference["revision"] < 1 or any(not isinstance(reference[key], str)
               or not re.fullmatch(r"[a-f0-9]{64}", reference[key]) for key in ("version_id", "content_digest"))):
        _fail("dataset_reference_invalid", "Portable dataset reference has invalid version identities")
    project_id = reference["project_id"]
    if expected_project_id is not None and project_id != expected_project_id:
        _fail("dataset_reference_invalid", "Portable dataset reference does not belong to this project")
    manifest = validate_manifest(reference["manifest"])
    expected = [{"id": "dataset-" + source["sha256"], "sha256": source["sha256"], "kind": "dataset",
                 "license": manifest["license"], "source_id": source["id"], "required": True}
                for source in manifest["sources"]]
    if reference["dependencies"] != expected or not isinstance(dependency_paths, dict):
        _fail("dataset_reference_invalid", "Portable dataset dependency closure differs from its manifest")
    roots, sources, captured = {}, {}, {}
    for source in manifest["sources"]:
        key = "dataset-" + source["sha256"]
        value = dependency_paths.get(key)
        if not isinstance(value, (str, Path)) or not value:
            _fail("dataset_dependency_unavailable", "A pinned dataset dependency has not been resolved", dependency_id=key)
        path = Path(value).absolute()
        roots[key] = path.parent
        sources[source["id"]] = {**source, "root_id": key, "path": path.name}
        if source_guards is not None:
            guard = source_guards.get(key)
            if (not isinstance(guard, dict) or set(guard) != {"root", "device", "inode", "path"}
                    or str(Path(guard["root"]) / guard["path"]) != str(path)):
                _fail("dataset_reference_invalid", "Dataset runtime root identity is missing or inconsistent")
            captured[key] = (Path(guard["root"]), guard["device"], guard["inode"])
            sources[source["id"]] = {**source, "root_id": key, "path": guard["path"]}

    class PortableReader(DatasetRegistry):
        def _read_source(self, project_id, source):
            return super()._read_source(project_id, sources[source["id"]])

    try:
        reader = PortableReader(None, project_roots={} if source_guards is not None else {project_id: roots},
                                streaming_limits=streaming_limits or reference.get("streaming_limits"))
        reader._stream_sources = sources
        if source_guards is not None:
            reader._roots = {project_id: captured}
    except (OSError, ValueError) as exc:
        raise ServiceError("dataset_dependency_unavailable", "A dataset dependency directory is unavailable") from exc
    from .dataset_streaming import compilation_context
    with compilation_context(cancel_file):
        materialized = reader._materialize(project_id, manifest)
    record = _version_record(project_id, reference["dataset_id"], reference["revision"], *materialized)
    if record["version_id"] != reference["version_id"] or record["content_digest"] != reference["content_digest"]:
        _fail("dataset_identity_mismatch", "Portable dataset contents differ from the pinned version")
    if manifest.get("access_mode") == "streaming":
        from .dataset_streaming import stream_data
        data = stream_data(record, reference, dependency_paths, source_guards, split_id, purpose, reader.streaming_limits)
        return (data, materialized[1]) if _return_index else data
    return _split_data(record, materialized, split_id, purpose)


def _transform(indexed, manifest):
    report = []
    id_column = manifest["id_column"]
    roles = {split["id"]: split["role"] for split in manifest["splits"]}
    for spec in manifest["transformations"]:
        before = len(indexed)
        removed = []
        fields = spec.get("fields")

        def fingerprint(item):
            row = item["row"]
            selected = fields or [k for k in row if k != id_column]
            if any(field not in row for field in selected):
                _fail("dataset_transform_invalid", "Transform refers to a missing field")
            return digest({key: row[key] for key in selected})

        sampling_report = {}
        if spec['kind'] == 'rows.sample':
            try:
                prior = indexed
                indexed, sampling_report = sample_rows(indexed, spec)
                kept = {item['sample_id'] for item in indexed}
                removed = [item['sample_id'] for item in prior if item['sample_id'] not in kept]
            except ValueError as error:
                _fail('dataset_sampling_blocked', str(error))
            implementation = 'modelbuilder.stratified-sha256-largest-remainder'
        elif spec["kind"] == "text.normalize":
            for item in indexed:
                for field in fields:
                    value = item["row"].get(field)
                    if not isinstance(value, str):
                        _fail("dataset_transform_invalid", "Text normalization requires string fields")
                    value = unicodedata.normalize(spec.get("unicode_form", "NFC"), value)
                    if spec.get("strip", True):
                        value = value.strip()
                    if spec.get("casefold", False):
                        value = value.casefold()
                    item["row"][field] = value
            implementation = "python.unicodedata.normalize+str"
        elif spec["kind"] == "columns.select":
            for item in indexed:
                if any(field not in item["row"] for field in fields):
                    _fail("dataset_transform_invalid", "Column selection refers to a missing field")
                item["row"] = {field: item["row"][field] for field in fields}
            implementation = "python.mapping.projection"
        else:
            keep, seen = [], set()
            protected = ({fingerprint(item) for item in indexed if roles[item["split_id"]] != "train"}
                         if spec["kind"] == "rows.decontaminate" else set())
            for item in indexed:
                if spec["kind"] == "rows.drop_missing":
                    drop = any(item["row"].get(field) in (None, "") for field in fields)
                elif spec["kind"] == "rows.deduplicate":
                    key = (item["split_id"], fingerprint(item))
                    drop = key in seen
                    seen.add(key)
                else:
                    drop = roles[item["split_id"]] == "train" and fingerprint(item) in protected
                if drop:
                    removed.append(item["sample_id"])
                else:
                    keep.append(item)
            indexed = keep
            implementation = "python.set+sha256" if spec["kind"] != "rows.drop_missing" else "python.mapping.filter"
        report.append({"declaration": copy.deepcopy(spec), "implementation": implementation,
                       "implementation_version": "1", "unicode_version": unicodedata.unidata_version,
                       "input_rows": before, "output_rows": len(indexed), "removed_sample_ids": removed,
                       **sampling_report})
    return indexed, report


def evaluation_identity(record, split_id):
    """Bridge to evaluation.py's single canonical identity contract."""
    split = next((item for item in record["splits"] if item["id"] == split_id), None)
    if split is None:
        _fail("dataset_split_unknown", "Dataset split was not found")
    return {"dataset": {"id": record["dataset_id"], "revision": str(record["revision"]),
                        "content_digest": record["content_digest"], "benchmark": record["benchmark"]},
            "split": {key: copy.deepcopy(split[key]) for key in ("id", "role", "sample_ids")}}


def consumption(record, split_id, sample_ids, *, purpose):
    """Describe exact ordered consumed IDs, allowing repeated epochs/batches.

    Call this with IDs actually yielded to a harness. The registry does not claim
    that registration or reading a manifest proves model consumption.
    """
    identity = evaluation_identity(record, split_id)
    allowed = {"training": {"train"}, "evaluation": {"validation", "test"}, "calibration": {"calibration"}}
    if purpose not in allowed or identity["split"]["role"] not in allowed[purpose]:
        _fail("dataset_split_purpose_invalid", "Dataset split role cannot be used for this purpose")
    if (not isinstance(sample_ids, list) or not sample_ids or len(sample_ids) > MAX_ROWS * 10
            or any(not isinstance(item, str) for item in sample_ids)):
        _fail("dataset_consumption_invalid", "Consumed sample IDs must be a bounded nonempty ordered array")
    lookup = {item["sample_id"]: item for item in record["sample_index"] if item["split_id"] == split_id}
    if set(sample_ids) - set(lookup):
        _fail("dataset_consumption_invalid", "Consumed sample IDs must belong to the pinned split")
    result = {"schema": CONSUMPTION_SCHEMA, "project_id": record["project_id"], **identity,
              "version_id": record["version_id"], "purpose": purpose,
              "sample_ids": list(sample_ids), "sample_digests": [lookup[item]["content_digest"] for item in sample_ids],
              "count": len(sample_ids), "unique_count": len(set(sample_ids))}
    result["digest"] = digest(result)
    return result
