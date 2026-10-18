"""Manifest-backed bounded access; SQLite holds lineage and sampling metadata.

Source bytes remain in operator-approved locations. Index compilation is a
streaming pass and uses SQLite for uniqueness, filtering and split ordering.
Every access verifies pinned source bytes; pages decode only their selected rows.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from collections import OrderedDict
import threading
import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import tempfile
import unicodedata

from jsonschema import Draft202012Validator

from .evaluation import digest
from .models import ServiceError, utcnow
from .platform_file_access import WindowsRoot, open_windows_source

DEFAULT_LIMITS = {
    "max_file_bytes": 64 * 1024**3,
    "max_total_bytes": 256 * 1024**3,
    "max_rows": 100_000_000,
    "max_row_bytes": 1024 * 1024,
    "max_decoded_bytes": 512 * 1024**3,
    "max_index_bytes": 32 * 1024**3,
    "max_sampling_strata": 10_000,
    "max_report_bytes": 16 * 1024**2,
}
CHAIN_SCHEMA = "sha256-length-prefixed-canonical-json/v1"
STREAM_SCHEMA = "modelbuilder.dataset-stream/v1"
CONSUMPTION_SCHEMA = "modelbuilder.dataset-consumption/v2"
# Individual physical/CSV logical records remain bounded before csv allocates.
csv.field_size_limit(16 * 1024**2)
_CANCEL_FILE = ContextVar("dataset_compile_cancel_file", default=None)


@contextmanager
def compilation_context(cancel_file=None):
    """Use the worker's existing cancel-file signal, scoped to this invocation."""
    if cancel_file is None:
        yield
        return
    token = _CANCEL_FILE.set(cancel_file)
    try:
        yield
    finally:
        _CANCEL_FILE.reset(token)


def check_cancelled():
    path = _CANCEL_FILE.get()
    if path and Path(path).exists():
        fail("dataset_cancelled", "Dataset compilation was cancelled before publishing its index")


def checked_file_digest(stream):
    sha = hashlib.sha256()
    while block := stream.read(1024 * 1024):
        check_cancelled()
        sha.update(block)
    check_cancelled()
    return sha.hexdigest()


def fail(code, message, **details):
    raise ServiceError(code, message, details)


def limits(value=None):
    if value is not None and (not isinstance(value, dict) or set(value) - set(DEFAULT_LIMITS)):
        raise ValueError("Dataset streaming limits must contain supported resource-cap names")
    result = {**DEFAULT_LIMITS, **(value or {})}
    if any(type(v) is not int or v <= 0 or v > 2**63 - 1 for v in result.values()):
        raise ValueError("Dataset resource caps must be positive bounded integers")
    if result["max_row_bytes"] > 16 * 1024**2:
        raise ValueError("Dataset rows may not exceed the 16 MiB process safety ceiling")
    return result


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


class Chain:
    def __init__(self):
        self.hash = hashlib.sha256()
        self.count = 0

    def add(self, value):
        raw = encoded(value)
        self.hash.update(len(raw).to_bytes(8, "big"))
        self.hash.update(raw)
        self.count += 1

    def value(self):
        return self.hash.hexdigest()


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def open_source(registry, project_id, source):
    # Portable readers translate aliases here, before the same security boundary.
    source = getattr(registry, "_stream_sources", {}).get(source["id"], source)
    root = registry._roots.get(project_id, {}).get(source["root_id"])
    if root is None:
        fail("dataset_root_unavailable", "Dataset root alias is not configured for this project")
    handles = []
    try:
        if isinstance(root, WindowsRoot):
            stream = open_windows_source(root, source["path"], registry.streaming_limits["max_file_bytes"])
        else:
            if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
                fail("capability_unavailable", "Secure descriptor-relative dataset reads are unavailable")
            fd = os.open(root[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            handles.append(fd)
            if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != root[1:]:
                fail("dataset_root_changed", "Configured dataset root changed since service startup")
            parts = PurePosixPath(source["path"]).parts
            for part in parts[:-1]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                handles.append(fd)
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            handles.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                fail("dataset_source_invalid", "Dataset source must be a regular file")
            if info.st_size > registry.streaming_limits["max_file_bytes"]:
                fail("dataset_limit_exceeded", "Dataset source exceeds the configured streaming byte limit")
            stream = os.fdopen(handles.pop(), "rb")
        with stream:
            before = signature(os.fstat(stream.fileno()))
            yield stream
            if signature(os.fstat(stream.fileno())) != before:
                fail("dataset_source_changed", "Dataset source changed while it was being read")
    except OSError as exc:
        raise ServiceError("dataset_source_unavailable", "Dataset source is missing, unsafe, or unreadable",
                           {"source_id": source["id"]}) from exc
    finally:
        for fd in reversed(handles):
            os.close(fd)


def verify_source(registry, project_id, source, stream=None):
    if stream is None:
        with open_source(registry, project_id, source) as opened:
            return verify_source(registry, project_id, source, opened)
    before = signature(os.fstat(stream.fileno()))
    stream.seek(0)
    sha, size = hashlib.sha256(), 0
    while block := stream.read(1024 * 1024):
        check_cancelled()
        sha.update(block)
        size += len(block)
        if size > registry.streaming_limits["max_file_bytes"]:
            fail("dataset_limit_exceeded", "Dataset source exceeds its configured byte limit")
    if sha.hexdigest() != source["sha256"]:
        fail("dataset_checksum_mismatch", "Dataset source checksum differs from the immutable manifest",
             source_id=source["id"])
    if signature(os.fstat(stream.fileno())) != before:
        fail("dataset_source_changed", "Dataset source changed while its bytes were verified")
    registry._stream_signatures[(project_id, source["id"], source["sha256"])] = before
    stream.seek(0)
    return size


def verify_prepared_source(registry, project_id, source, expected, stream=None):
    """Only descriptor/inode metadata checks inside the short writer transaction.

    Full hashes are prepared and rechecked outside the transaction. Changing
    content, inode, size, mtime or ctime invalidates that prepared snapshot.
    """
    if stream is None:
        with open_source(registry, project_id, source) as opened:
            return verify_prepared_source(registry, project_id, source, expected, opened)
    if expected is None or signature(os.fstat(stream.fileno())) != expected:
        fail("dataset_source_changed", "Dataset source changed after unlocked verification; retry explicitly")
    return expected[2]


class CSVLines:
    """csv.reader consumes physical lines while retaining byte-exact record spans."""
    def __init__(self, stream, maximum):
        self.stream, self.maximum = stream, maximum
        self.start = stream.tell()
        self.first = self.start == 0

    def __iter__(self):
        return self

    def __next__(self):
        budget = self.maximum + 1 - (self.stream.tell() - self.start)
        line = self.stream.readline(max(1, budget))
        if not line:
            raise StopIteration
        if self.stream.tell() - self.start > self.maximum:
            fail("dataset_limit_exceeded", "A CSV record exceeds the configured row byte limit")
        value = line.decode("utf-8-sig" if self.first else "utf-8")
        self.first = False
        return value


def parse_csv_row(values, fields, schema):
    if len(values) != len(fields):
        fail("dataset_source_invalid", "CSV row does not match its header")
    row = dict(zip(fields, values))
    for key, value in row.items():
        if schema.get("properties", {}).get(key, {}).get("type") in {
                "number", "integer", "boolean", "null", "array", "object"}:
            row[key] = json.loads(value)
    return row


def source_rows(registry, stream, source, schema):
    maximum = registry.streaming_limits["max_row_bytes"]
    try:
        if source["format"] == "parquet":
            from .parquet_stream import iter_parquet
            yield from iter_parquet(stream, registry.streaming_limits)
        elif source["format"] == "jsonl":
            number = 0
            while True:
                start = stream.tell()
                line = stream.readline(maximum + 1)
                if not line:
                    break
                if len(line) > maximum:
                    fail("dataset_limit_exceeded", "A JSONL record exceeds the configured row byte limit")
                text = line.decode("utf-8-sig" if start == 0 else "utf-8")
                if not text.strip():
                    continue
                number += 1
                yield number, {"offset": start, "length": len(line)}, json.loads(text)
        else:
            lines = CSVLines(stream, maximum)
            reader = csv.reader(lines, strict=True)
            fields = next(reader, None)
            if not fields or len(fields) > 256 or len(set(fields)) != len(fields):
                fail("dataset_source_invalid", "CSV header must contain unique column names")
            number = 0
            while True:
                lines.start = stream.tell()
                values = next(reader, None)
                if values is None:
                    break
                number += 1
                locator = {"offset": lines.start, "length": stream.tell() - lines.start, "fields": fields}
                yield number, locator, parse_csv_row(values, fields, schema)
    except (UnicodeError, ValueError, TypeError, RecursionError, csv.Error) as exc:
        raise ServiceError("dataset_source_invalid", "Dataset rows cannot be decoded as finite JSON objects",
                           {"source_id": source["id"]}) from exc


def transform_row(row, manifest, *, defer_errors=False, split_id=None):
    """Compute local output and stage fingerprints; global filters run in SQLite.

    A deferred error is checked only when that stage is reached by a surviving
    sample. Earlier filters can remove a malformed row, matching eager semantics.
    """
    row, filters, error = copy.deepcopy(row), [], None
    for stage, spec in enumerate(manifest["transformations"]):
        fields, kind = spec.get("fields"), spec["kind"]
        if error is not None:
            if kind.startswith("rows."):
                filters.append("unreached")
            continue
        try:
            if kind == "text.normalize":
                for field in fields:
                    value = row.get(field)
                    if not isinstance(value, str):
                        fail("dataset_transform_invalid", "Text normalization requires string fields")
                    value = unicodedata.normalize(spec.get("unicode_form", "NFC"), value)
                    row[field] = value.strip() if spec.get("strip", True) else value
                    if spec.get("casefold", False):
                        row[field] = row[field].casefold()
            elif kind == "columns.select":
                if any(field not in row for field in fields):
                    fail("dataset_transform_invalid", "Column selection refers to a missing field")
                row = {field: row[field] for field in fields}
            elif kind == "rows.drop_missing":
                filters.append("drop" if any(row.get(field) in (None, "") for field in fields) else "keep")
            elif kind == "rows.sample":
                # Sampling chooses membership globally. Retain only the stage's
                # declared stratum values, never the complete row in SQLite.
                if split_id is None or split_id not in spec["split_ids"]:
                    filters.append("unselected")
                elif any(field not in row or row[field] is None for field in fields):
                    fail("dataset_sampling_blocked", "Sampling stratum evidence is missing")
                else:
                    filters.append(json.dumps({key: row[key] for key in fields}, sort_keys=True,
                                              separators=(",", ":"), allow_nan=False))
            else:
                selected = fields or [key for key in row if key != manifest["id_column"]]
                if any(field not in row for field in selected):
                    fail("dataset_transform_invalid", "Transform refers to a missing field")
                filters.append(digest({key: row[key] for key in selected}))
        except ServiceError as exc:
            if not defer_errors:
                raise
            error = {"stage": stage, "code": exc.code, "message": exc.message}
            if kind.startswith("rows."):
                filters.append("unreached")
    return (row, filters, error) if defer_errors else (row, filters)


class StreamingIndex:
    def __init__(self, registry, project_id, manifest, path, summary, sha256, *, peak_index_bytes=0):
        self.registry, self.project_id, self.manifest = registry, project_id, manifest
        self.path, self.sha256 = Path(path), sha256
        self.summary = {key: value for key, value in summary.items() if key != "peak_index_bytes"}
        # Retain the provisional patch's observation when restoring its cache,
        # without treating physical allocation as logical lineage.
        self.peak_index_bytes = max(peak_index_bytes, summary.get("peak_index_bytes", 0))

    def __len__(self):
        return self.summary["row_count"]

    @contextmanager
    def connect(self):
        # The published operator-local cache stores lineage and stratum hashes.
        # Temporary declared stratum values are erased before publication.
        # Verify the pinned bytes and fence concurrent changes for the read.
        if self.path.is_symlink():
            fail("dataset_identity_mismatch", "Dataset lineage index is unsafe")
        before = signature(self.path.stat())
        from .dataset import _MATERIALIZATIONS
        session = _MATERIALIZATIONS.get()
        if session is not None and not session[1]:
            prepared = any("value" in entry and entry["value"][1] is self for entry in session[0].values())
            if not prepared or getattr(self, "_verified_signature", None) != before:
                fail("dataset_identity_mismatch", "Dataset lineage index changed after unlocked verification")
        else:
            with self.path.open("rb") as stream:
                if checked_file_digest(stream) != self.sha256:
                    fail("dataset_identity_mismatch", "Dataset lineage index checksum changed")
            if signature(self.path.stat()) != before:
                fail("dataset_identity_mismatch", "Dataset lineage index changed during verification")
            self._verified_signature = before
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA cache_size=-2048")
            yield db
            if signature(self.path.stat()) != before:
                fail("dataset_identity_mismatch", "Dataset lineage index changed during access")
        finally:
            db.close()


def enforce_index_caps(registry, index):
    summary, caps = index.summary, registry.streaming_limits
    pairs = ((summary["source_row_count"], "max_rows"), (summary["source_bytes"], "max_total_bytes"),
             (summary["decoded_bytes"], "max_decoded_bytes"), (summary["largest_row_bytes"], "max_row_bytes"),
             (max(index.path.stat().st_size, index.peak_index_bytes), "max_index_bytes"),
             (summary.get("sampling_strata", 0), "max_sampling_strata"),
             (len(encoded(summary["transformations"])), "max_report_bytes"))
    if any(value > caps[limit] for value, limit in pairs):
        fail("dataset_limit_exceeded", "Registered dataset exceeds the current operator resource caps")


def sample_stage(db, column, spec, caps, stage):
    """Plan bounded strata, rank on disk, and retain winners by indexed seeks."""
    from .rules import validate_sampling_spec, sampling_quotas
    required = validate_sampling_spec(spec)
    splits = sorted(spec["split_ids"])
    placeholders = ",".join("?" for _ in splits)
    scope = f"alive=1 AND split_id IN ({placeholders})"
    # Main-database tables/indexes share max_page_count with lineage. Temporary
    # sort files use the existing disk-backed SQLite resource policy.
    db.execute("CREATE TABLE sample_rank (seq INTEGER PRIMARY KEY, split_id TEXT NOT NULL, "
               "stratum TEXT NOT NULL, rank TEXT NOT NULL, sample_id TEXT NOT NULL)")
    db.create_function("sampling_rank", 1, lambda sample_id: digest({"seed": spec["seed"], "sample_id": sample_id}))
    db.execute(f"INSERT INTO sample_rank SELECT seq,split_id,{column},sampling_rank(sample_id),sample_id "
               f"FROM samples WHERE {scope}", splits)
    db.execute("CREATE INDEX sample_rank_order ON sample_rank(split_id,stratum,rank,sample_id)")
    db.execute("CREATE TABLE sample_winners (seq INTEGER PRIMARY KEY)")
    reports, cardinality, evidence_bytes = [], 0, len(encoded(spec))
    before_sources = dict(db.execute(f"SELECT source_id,COUNT(*) FROM samples WHERE {scope} GROUP BY source_id", splits))
    for split in splits:
        counts, values = {}, {}
        for item in db.execute("SELECT stratum,COUNT(*) AS count FROM sample_rank WHERE split_id=? GROUP BY stratum", (split,)):
            check_cancelled()
            cardinality += 1
            if cardinality > caps["max_sampling_strata"]:
                fail("dataset_limit_exceeded", "Sampling stratum evidence exceeds the configured report/cardinality limit")
            key = item["stratum"]
            raw = db.execute("SELECT value FROM sample_strata WHERE stage=? AND stratum=?", (stage, key)).fetchone()[0]
            value = json.loads(raw)
            # Match the final report and cached-report cap: canonical UTF-8
            # JSON, rather than SQLite's ASCII-escaped storage representation.
            evidence_bytes += len(encoded(value))
            if evidence_bytes > caps["max_report_bytes"]:
                fail("dataset_limit_exceeded", "Sampling stratum evidence exceeds the configured report/cardinality limit")
            counts[key], values[key] = item["count"], value
        if not counts:
            raise ValueError("Sampling split is unknown or empty")
        quotas, observed = sampling_quotas(counts, spec, required)
        for row in observed["strata"]:
            row["stratum_values"] = values[row["stratum"]]
            evidence_bytes += len(encoded({key: value for key, value in row.items() if key != "stratum_values"}))
            if evidence_bytes > caps["max_report_bytes"]:
                fail("dataset_limit_exceeded", "Sampling representation report exceeds the configured byte limit")
        for key, quota in quotas.items():
            check_cancelled()
            db.execute("INSERT INTO sample_winners SELECT seq FROM sample_rank WHERE split_id=? AND stratum=? "
                       "ORDER BY rank,sample_id LIMIT ?", (split, key, quota))
        reports.append({"split_id": split, **observed})
    # Match evaluation.digest(sorted(selected)) byte for byte, including JSON's
    # ASCII escaping for Unicode IDs. Chain remains the distinct lineage schema.
    selection = hashlib.sha256(b"[")
    selected_count = 0
    for item in db.execute("SELECT sample_id FROM sample_rank r JOIN sample_winners w USING(seq) ORDER BY sample_id"):
        check_cancelled()
        if selected_count:
            selection.update(b",")
        selection.update(json.dumps(item["sample_id"], separators=(",", ":"), allow_nan=False).encode())
        selected_count += 1
    selection.update(b"]")
    removed = Chain()
    for item in db.execute(f"SELECT sample_id FROM samples WHERE {scope} "
                           "AND seq NOT IN (SELECT seq FROM sample_winners) ORDER BY seq", splits):
        check_cancelled()
        removed.add(item["sample_id"])
    db.execute(f"UPDATE samples SET alive=0 WHERE {scope} AND seq NOT IN (SELECT seq FROM sample_winners)", splits)
    after_sources = dict(db.execute(f"SELECT source_id,COUNT(*) FROM samples WHERE {scope} GROUP BY source_id", splits))
    result = {"representation": reports, "selected_count": selected_count,
              "selection_digest": selection.hexdigest(), "policy_digest": digest(spec),
              "source_counts": [{"source_id": key, "before": before_sources[key], "after": after_sources.get(key, 0)}
                                for key in sorted(before_sources)]}
    peak_bytes = db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
    db.execute("DROP TABLE sample_winners")
    db.execute("DROP TABLE sample_rank")
    db.execute("DELETE FROM sample_strata WHERE stage=?", (stage,))
    db.create_function("sampling_rank", 1, None)
    return result, cardinality, peak_bytes, removed


def compile_index(registry, project_id, manifest):
    check_cancelled()
    cache_key = (project_id, digest(manifest))
    cached = registry._stream_indexes.get(cache_key)
    if cached is not None:
        enforce_index_caps(registry, cached)
        for source in manifest["sources"]:
            verify_source(registry, project_id, source)
        with cached.connect():
            pass
        return manifest, cached, cached.summary["splits"], cached.summary["transformations"]
    if registry.store is not None:
        known = registry._stream_records.get(cache_key)
        for record in ([known] if known else registry.store.list("dataset", project_id)):
            pin = record.get("lineage_index", {})
            if record.get("manifest_digest") == digest(manifest) and pin.get("sha256"):
                path = Path(registry._stream_directory) / (pin["sha256"] + ".sqlite3")
                if path.exists():
                    index = StreamingIndex(registry, project_id, manifest, path, record["lineage"], pin["sha256"],
                                           peak_index_bytes=pin.get("peak_index_bytes", 0))
                    for source in manifest["sources"]:
                        verify_source(registry, project_id, source)
                    enforce_index_caps(registry, index)
                    with index.connect():
                        pass
                    registry._stream_indexes[cache_key] = index
                    return manifest, index, index.summary["splits"], index.summary["transformations"]
    caps = registry.streaming_limits
    root = registry._stream_directory
    fd, name = tempfile.mkstemp(prefix="lineage-", suffix=".sqlite3", dir=root)
    os.close(fd)
    path = Path(name)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        def progress():
            path = _CANCEL_FILE.get()
            return int(bool(path and Path(path).exists()))
        db.set_progress_handler(progress, 1000)
        db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-2048; PRAGMA temp_store=FILE;")
        db.execute("PRAGMA max_page_count=" + str(max(1, caps["max_index_bytes"] // 4096)))
        family_policy = manifest.get("family_policy")
        family_ids = {family["id"] for family in family_policy["families"]} if family_policy else set()
        filter_specs = [spec for spec in manifest["transformations"] if spec["kind"].startswith("rows.")]
        filter_stages = [(stage, spec) for stage, spec in enumerate(manifest["transformations"])
                         if spec["kind"].startswith("rows.")]
        has_sampling = any(spec["kind"] == "rows.sample" for spec in filter_specs)
        if has_sampling:
            # Erase temporary category values and their freed pages. The final
            # index contains stage hashes, not N repeated projections of text.
            db.execute("PRAGMA secure_delete=ON")
            db.execute("CREATE TABLE sample_strata (stage INTEGER, stratum TEXT, value TEXT NOT NULL, "
                       "PRIMARY KEY(stage,stratum)) WITHOUT ROWID")
        columns = ",".join(f"f{i} TEXT" for i in range(len(filter_specs)))
        db.execute("CREATE TABLE samples (seq INTEGER PRIMARY KEY, sample_id TEXT UNIQUE NOT NULL, source_id TEXT NOT NULL, "
                   "source_row INTEGER NOT NULL, locator TEXT NOT NULL, source_digest TEXT NOT NULL, content_digest TEXT NOT NULL, "
                   "split_id TEXT NOT NULL, split_order INTEGER NOT NULL, role TEXT NOT NULL, alive INTEGER NOT NULL DEFAULT 1, "
                   "row_bytes INTEGER NOT NULL, error_stage INTEGER, error TEXT"
                   + (",family_value TEXT" if family_policy else "")
                   + ("," + columns if columns else "") + ")")
        db.execute("CREATE TABLE source_headers (source_id TEXT PRIMARY KEY, fields TEXT NOT NULL)")
        db.execute("CREATE TABLE requested (sample_id TEXT PRIMARY KEY, split_id TEXT, split_order INTEGER, role TEXT)")
        source_assignments = {}
        known_sources = {item["id"] for item in manifest["sources"]}
        for split in manifest["splits"]:
            if "source_ids" in split:
                for source_id in split["source_ids"]:
                    if source_id not in known_sources:
                        fail("dataset_split_invalid", "Split refers to an unknown source")
                    if source_id in source_assignments:
                        fail("dataset_split_overlap", "A source cannot belong to multiple splits")
                    source_assignments[source_id] = split
            else:
                for order, sample_id in enumerate(split["sample_ids"]):
                    try:
                        db.execute("INSERT INTO requested VALUES (?,?,?,?)", (sample_id, split["id"], order, split["role"]))
                    except sqlite3.IntegrityError:
                        fail("dataset_split_overlap", "A sample cannot belong to multiple splits", sample_id=sample_id)
        validator = Draft202012Validator(manifest["row_schema"])
        total_bytes, decoded, count, largest_row, source_stats = 0, 0, 0, 0, []
        for source in manifest["sources"]:
            source_count = 0
            with open_source(registry, project_id, source) as stream:
                size = verify_source(registry, project_id, source, stream)
                total_bytes += size
                if total_bytes > caps["max_total_bytes"]:
                    fail("dataset_limit_exceeded", "Combined sources exceed the configured streaming byte limit")
                for number, locator, raw_row in source_rows(registry, stream, source, manifest["row_schema"]):
                    check_cancelled()
                    count += 1
                    source_count += 1
                    if count > caps["max_rows"]:
                        fail("dataset_limit_exceeded", "Dataset exceeds the configured streaming row limit")
                    if not isinstance(raw_row, dict):
                        fail("dataset_source_invalid", "Dataset rows must be JSON objects")
                    raw_bytes = encoded(raw_row)
                    decoded += len(raw_bytes)
                    largest_row = max(largest_row, len(raw_bytes))
                    if len(raw_bytes) > caps["max_row_bytes"] or decoded > caps["max_decoded_bytes"]:
                        fail("dataset_limit_exceeded", "Decoded dataset exceeds the configured resource limit")
                    sample_id = raw_row.get(manifest["id_column"])
                    if not isinstance(sample_id, str) or not sample_id or len(sample_id) > 200:
                        fail("dataset_sample_invalid", "Every sample needs a nonempty string identity")
                    explicit = db.execute("SELECT * FROM requested WHERE sample_id=?", (sample_id,)).fetchone()
                    split = source_assignments.get(source["id"])
                    if split and explicit:
                        fail("dataset_split_overlap", "A sample cannot belong to multiple splits", sample_id=sample_id)
                    if not split and not explicit:
                        fail("dataset_split_invalid", "Splits must assign every source sample exactly once")
                    split_id, role, order = ((split["id"], split["role"], count) if split else
                                            (explicit["split_id"], explicit["role"], explicit["split_order"]))
                    row, fingerprints, transform_error = transform_row(raw_row, manifest, defer_errors=True, split_id=split_id)
                    for field_index, (stage, spec) in enumerate(filter_stages):
                        value = fingerprints[field_index]
                        if spec["kind"] == "rows.sample" and value not in {"unselected", "unreached"}:
                            key = digest(json.loads(value))
                            db.execute("INSERT OR IGNORE INTO sample_strata VALUES (?,?,?)", (stage, key, value))
                            fingerprints[field_index] = key
                    errors = list(validator.iter_errors(row))
                    # Filtering happens after ingestion; retain a validation marker
                    # so dropped rows preserve eager semantics without row storage.
                    content = digest({k: v for k, v in row.items() if k != manifest["id_column"]})
                    row_bytes = len(encoded(row))
                    if row_bytes > caps["max_row_bytes"]:
                        content = "oversized:"
                    elif errors:
                        content = "invalid:" + json.dumps([".".join(map(str, e.path)) for e in errors[:10]])
                    if "fields" in locator:
                        fields = locator.pop("fields")
                        if source_count == 1:
                            db.execute("INSERT INTO source_headers VALUES (?,?)", (source["id"], json.dumps(fields)))
                    family = row.get(family_policy["mapping"]["field"]) if family_policy else None
                    family_value = family if isinstance(family, str) and family in family_ids else None
                    values = (count, sample_id, source["id"], number, json.dumps(locator), digest(raw_row), content,
                              split_id, order, role, 1, row_bytes, transform_error["stage"] if transform_error else None,
                              json.dumps(transform_error) if transform_error else None,
                              *((family_value,) if family_policy else ()), *fingerprints)
                    try:
                        db.execute("INSERT INTO samples VALUES (" + ",".join("?" for _ in values) + ")", values)
                    except sqlite3.IntegrityError:
                        fail("dataset_sample_overlap", "Sample identities must be unique across all sources", sample_id=sample_id)
                # Verify bytes again after decoding, from the very same descriptor.
                verify_source(registry, project_id, source, stream)
            source_stats.append({"id": source["id"], "sha256": source["sha256"], "bytes": size, "row_count": source_count})
        if db.execute("SELECT 1 FROM requested r LEFT JOIN samples s USING(sample_id) WHERE s.sample_id IS NULL LIMIT 1").fetchone():
            fail("dataset_split_invalid", "Split refers to an unknown sample")
        report, filter_index, remaining, sampling_strata, peak_index_bytes = [], 0, count, 0, 0
        for stage, spec in enumerate(manifest["transformations"]):
            check_cancelled()
            invalid = db.execute("SELECT error FROM samples WHERE alive=1 AND error_stage=? LIMIT 1", (stage,)).fetchone()
            if invalid:
                error = json.loads(invalid["error"])
                fail(error["code"], error["message"])
            before = remaining
            removed = Chain()
            kind = spec["kind"]
            sampling_report = {}
            if kind.startswith("rows."):
                column = f"f{filter_index}"
                db.execute(f"CREATE INDEX filter_{filter_index} ON samples(alive, {column}, split_id, seq)")
                if kind == "rows.sample":
                    try:
                        sampling_report, strata, peak_bytes, removed = sample_stage(db, column, spec, caps, stage)
                    except ValueError as exc:
                        fail("dataset_sampling_blocked", str(exc))
                    sampling_strata = max(sampling_strata, strata)
                    peak_index_bytes = max(peak_index_bytes, peak_bytes)
                elif kind == "rows.drop_missing":
                    predicate = f"{column}='drop'"
                elif kind == "rows.deduplicate":
                    predicate = f"seq NOT IN (SELECT MIN(seq) FROM samples WHERE alive=1 GROUP BY split_id,{column})"
                else:
                    predicate = f"role='train' AND {column} IN (SELECT {column} FROM samples WHERE alive=1 AND role!='train')"
                if kind != "rows.sample":
                    for item in db.execute(f"SELECT sample_id FROM samples WHERE alive=1 AND ({predicate}) ORDER BY seq"):
                        check_cancelled()
                        removed.add(item["sample_id"])
                    db.execute(f"UPDATE samples SET alive=0 WHERE alive=1 AND ({predicate})")
                remaining -= removed.count
                filter_index += 1
            report.append({"declaration": copy.deepcopy(spec), "implementation": "modelbuilder.sqlite-stream",
                           "implementation_version": "1", "unicode_version": unicodedata.unidata_version,
                           "input_rows": before, "output_rows": remaining, "removed_count": removed.count,
                           "removed_ids_digest": removed.value(), "digest_algorithm": CHAIN_SCHEMA, **sampling_report})
            if len(encoded(report)) > caps["max_report_bytes"]:
                fail("dataset_limit_exceeded", "Transformation report exceeds the configured byte limit")
        if has_sampling:
            db.execute("DROP TABLE sample_strata")
        if db.execute("SELECT 1 FROM samples WHERE alive=1 AND content_digest='oversized:' LIMIT 1").fetchone():
            fail("dataset_limit_exceeded", "Transformed row exceeds the configured row byte limit")
        transformed_bytes, transformed_largest = db.execute("SELECT COALESCE(SUM(row_bytes),0),COALESCE(MAX(row_bytes),0) FROM samples WHERE alive=1").fetchone()
        if transformed_bytes > caps["max_decoded_bytes"]:
            fail("dataset_limit_exceeded", "Transformed dataset exceeds the configured decoded byte limit")
        decoded, largest_row = max(decoded, transformed_bytes), max(largest_row, transformed_largest)
        bad = db.execute("SELECT sample_id,content_digest FROM samples WHERE alive=1 AND content_digest LIKE 'invalid:%' LIMIT 1").fetchone()
        if bad:
            fail("dataset_row_invalid", "A transformed row differs from the declared schema", sample_id=bad["sample_id"],
                 fields=json.loads(bad["content_digest"][8:]))
        if db.execute("SELECT 1 FROM samples WHERE alive=1 GROUP BY content_digest HAVING COUNT(DISTINCT split_id)>1 LIMIT 1").fetchone():
            fail("dataset_content_overlap", "Identical sample content crosses split boundaries; explicitly decontaminate training data")
        db.execute("CREATE INDEX split_page ON samples(split_id,alive,split_order)")
        db.execute("CREATE TABLE pages (split_id TEXT, ordinal INTEGER, seq INTEGER NOT NULL, PRIMARY KEY(split_id,ordinal)) WITHOUT ROWID")
        row_chain, splits = Chain(), []
        for item in db.execute("SELECT * FROM samples WHERE alive=1 ORDER BY seq"):
            row_chain.add(row_identity(item))
        for spec in manifest["splits"]:
            chain, split_bytes = Chain(), 0
            for item in db.execute("SELECT * FROM samples WHERE alive=1 AND split_id=? ORDER BY split_order", (spec["id"],)):
                db.execute("INSERT INTO pages VALUES (?,?,?)", (spec["id"], chain.count, item["seq"]))
                split_bytes += item["row_bytes"]
                chain.add(row_identity(item))
            if not chain.count:
                fail("dataset_split_empty", "Every declared split must retain at least one sample", split_id=spec["id"])
            split = {"id": spec["id"], "role": spec["role"], "sample_count": chain.count,
                     "lineage_digest": chain.value(), "decoded_bytes": split_bytes, "digest_algorithm": CHAIN_SCHEMA}
            if family_policy and spec["role"] == "train":
                from .dataset import final_family_coverage
                counts = {family: 0 for family in family_ids}
                for family, family_count in db.execute("SELECT family_value,COUNT(*) FROM samples WHERE alive=1 AND split_id=? GROUP BY family_value", (spec["id"],)):
                    if family is None:
                        fail("dataset_family_coverage_invalid", "Final training row has a missing or unsupported family", split_id=spec["id"])
                    counts[family] = family_count
                split["final_dataset_family_coverage"] = final_family_coverage(family_policy, spec["id"], counts=counts)
            split["digest"] = digest(split)
            splits.append(split)
        summary = {"row_count": remaining, "source_row_count": count, "source_bytes": total_bytes,
                   "decoded_bytes": decoded, "largest_row_bytes": largest_row,
                   "sources": source_stats, "lineage_digest": row_chain.value(), "digest_algorithm": CHAIN_SCHEMA,
                   "splits": splits, "transformations": report}
        if family_policy and len(encoded(summary)) > caps["max_report_bytes"]:
            fail("dataset_limit_exceeded", "Family coverage report exceeds the configured byte limit")
        if sampling_strata:
            summary.update(sampling_strata=sampling_strata)
        # Physical allocation is cache resource evidence, outside semantic
        # lineage. Include final growth after rank/winner tables were removed.
        if sampling_strata:
            peak_index_bytes = max(peak_index_bytes, db.execute("PRAGMA page_count").fetchone()[0]
                                   * db.execute("PRAGMA page_size").fetchone()[0])
        check_cancelled()
        db.commit()
        db.close()
        with path.open("rb") as stream:
            sha = checked_file_digest(stream)
        check_cancelled()
        destination = Path(root) / (sha + ".sqlite3")
        os.replace(path, destination)
        path = destination
        index = StreamingIndex(registry, project_id, manifest, path, summary, sha,
                               peak_index_bytes=peak_index_bytes)
        index._verified_signature = signature(path.stat())
        registry._stream_indexes[cache_key] = index
        return manifest, index, splits, report
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        db.close()
        path.unlink(missing_ok=True)
        raise ServiceError("dataset_source_invalid", "Dataset rows must contain finite portable JSON values") from exc
    except sqlite3.DatabaseError as exc:
        db.close()
        path.unlink(missing_ok=True)
        check_cancelled()
        raise ServiceError("dataset_limit_exceeded", "Dataset lineage index exceeds its disk budget or cannot be built") from exc
    except BaseException:
        db.close()
        path.unlink(missing_ok=True)
        raise


def row_identity(item):
    return {"sample_id": item["sample_id"], "source_id": item["source_id"], "source_row": item["source_row"],
            "source_content_digest": item["source_digest"], "content_digest": item["content_digest"], "split_id": item["split_id"]}


def version_record(project_id, dataset_id, revision, manifest, index, splits, report, parent=None):
    content = digest({"streaming_lineage": index.summary, "row_schema": manifest["row_schema"]})
    pin = {"schema": "modelbuilder.dataset-lineage-index/v1", "sha256": index.sha256}
    if index.peak_index_bytes:
        pin["peak_index_bytes"] = index.peak_index_bytes
    return {"schema": "modelbuilder.dataset/v1", "project_id": project_id, "dataset_id": dataset_id,
            "revision": revision, "version_id": digest({"manifest": manifest, "content_digest": content}),
            "parent_version_id": parent, "created_at": utcnow(), "name": manifest["name"],
            "benchmark": manifest["benchmark"], "license": manifest["license"], "manifest": manifest,
            "manifest_digest": digest(manifest), "content_digest": content, "row_count": len(index),
            "access_mode": "streaming", "lineage": index.summary, "splits": splits, "transformations": report,
            "lineage_index": pin}


def split_identity(record, split_id, purpose):
    split = next((copy.deepcopy(item) for item in record["splits"] if item["id"] == split_id), None)
    if split is None:
        fail("dataset_split_unknown", "Dataset split was not found")
    allowed = {"training": {"train"}, "evaluation": {"validation", "test"}, "calibration": {"calibration"}}
    if purpose not in allowed or split["role"] not in allowed[purpose]:
        fail("dataset_split_purpose_invalid", "Dataset split role cannot be used for this purpose")
    return {"dataset": {"id": record["dataset_id"], "revision": str(record["revision"]),
                        "content_digest": record["content_digest"], "benchmark": record["benchmark"]}, "split": split}


def check_record(record, materialized):
    current = version_record(record["project_id"], record["dataset_id"], record["revision"], *materialized)
    if (current["version_id"] != record["version_id"] or current["content_digest"] != record["content_digest"]
            or current["splits"] != record["splits"]):
        fail("dataset_identity_mismatch", "Dataset materialization changed since registration")


def indexed_rows(index, split_id, *, offset=0, limit=None):
    registry, manifest = index.registry, index.manifest
    sources = {source["id"]: source for source in manifest["sources"]}
    # One descriptor at a time, even for thousands of shards. Source verification
    # precedes every yielded row from that descriptor, then row digests fence it.
    with index.connect() as db, ExitStack() as opened:
        active_source, stream = None, None
        parquet_iterator, parquet_group, parquet_position = None, None, -1
        def close_parquet():
            if parquet_iterator is not None:
                parquet_iterator.close()
        opened.callback(close_parquet)
        query = ("SELECT s.* FROM pages p JOIN samples s ON s.seq=p.seq "
                 "WHERE p.split_id=? AND p.ordinal>=? ORDER BY p.ordinal LIMIT ?")
        for item in db.execute(query, (split_id, offset, -1 if limit is None else limit)):
            source = sources[item["source_id"]]
            if active_source != source["id"]:
                opened.close()
                parquet_iterator, parquet_group, parquet_position = None, None, -1
                opened.callback(close_parquet)
                stream = opened.enter_context(open_source(registry, index.project_id, source))
                registry._verify_source(index.project_id, source, manifest, stream=stream)
                active_source = source["id"]
                header = db.execute("SELECT fields FROM source_headers WHERE source_id=?", (active_source,)).fetchone()
                fields = json.loads(header[0]) if header else None
            locator = json.loads(item["locator"])
            try:
                if source["format"] == "parquet":
                    from .parquet_stream import iter_parquet
                    if parquet_group != locator["group"] or parquet_position >= locator["row"]:
                        close_parquet()
                        parquet_iterator = iter_parquet(stream, registry.streaming_limits, group=locator["group"], start=locator["row"])
                        parquet_group, parquet_position = locator["group"], locator["row"] - 1
                    while parquet_position < locator["row"]:
                        _, position, raw = next(parquet_iterator)
                        parquet_position = position["row"]
                else:
                    stream.seek(locator["offset"])
                    raw_bytes = stream.read(locator["length"])
                    if source["format"] == "jsonl":
                        raw = json.loads(raw_bytes.decode("utf-8-sig" if locator["offset"] == 0 else "utf-8"))
                    else:
                        values = next(csv.reader(io.StringIO(raw_bytes.decode("utf-8"), newline=""), strict=True))
                        raw = parse_csv_row(values, fields, manifest["row_schema"])
                row, _ = transform_row(raw, manifest)
            except (ValueError, UnicodeError, TypeError, csv.Error) as exc:
                raise ServiceError("dataset_source_invalid", "Indexed row cannot be decoded") from exc
            if (raw.get(manifest["id_column"]) != item["sample_id"] or digest(raw) != item["source_digest"]
                    or digest({k: v for k, v in row.items() if k != manifest["id_column"]}) != item["content_digest"]):
                fail("dataset_identity_mismatch", "Indexed row differs from its immutable lineage")
            yield {**row_identity(item), "row": row}


def preview(record, materialized, split_id, purpose, offset, limit):
    check_record(record, materialized)
    identity = split_identity(record, split_id, purpose)
    rows, ids, size = [], [], 0
    iterator = indexed_rows(materialized[1], split_id, offset=offset, limit=limit)
    try:
        for item in iterator:
            size += len(encoded(item["row"]))
            if size > 1024 * 1024:
                if not rows:
                    fail("dataset_limit_exceeded", "One dataset row exceeds the preview byte limit")
                break
            rows.append(item["row"])
            ids.append(item["sample_id"])
    finally:
        iterator.close()
    total = identity["split"]["sample_count"]
    return {**identity, "version_id": record["version_id"], "rows": rows, "sample_ids": ids,
            "offset": offset, "limit": limit, "total": total, "has_more": offset + len(rows) < total}


def stream_identity(reference, split_id, purpose):
    return digest({"version_id": reference["version_id"], "content_digest": reference["content_digest"],
                   "project_id": reference["project_id"], "dataset_id": reference["dataset_id"],
                   "revision": reference["revision"], "split_id": split_id, "purpose": purpose})


def stream_data(record, reference, dependency_paths, source_guards, split_id, purpose, resource_limits):
    from .generated_artifacts import manifest_rights
    from .dataset import family_metadata
    rights = manifest_rights(record["manifest"])
    identity = split_identity(record, split_id, purpose)
    return {**identity, **family_metadata(record, split_id), **({"rights": copy.deepcopy(rights)} if rights is not None else {}), "version_id": record["version_id"], "stream": {
        "schema": STREAM_SCHEMA, "identity": stream_identity(reference, split_id, purpose),
        "reference": copy.deepcopy(reference), "dependency_paths": {key: str(value) for key, value in dependency_paths.items()},
        "source_guards": copy.deepcopy(source_guards), "split_id": split_id, "purpose": purpose,
        "limits": dict(resource_limits)}}


def _validate_descriptor(data, *, purpose="training"):
    if not isinstance(data, dict) or not isinstance(data.get("stream"), dict):
        fail("dataset_reference_invalid", "Expected a registered streaming dataset")
    stream = data["stream"]
    required = {"schema", "identity", "reference", "dependency_paths", "source_guards", "split_id", "purpose", "limits"}
    if (set(stream) != required or stream["schema"] != STREAM_SCHEMA or stream["purpose"] != purpose
            or not isinstance(stream["reference"], dict)):
        fail("dataset_reference_invalid", "Streaming dataset descriptor is invalid or has the wrong purpose")
    ref = stream["reference"]
    try:
        expected = stream_identity(ref, stream["split_id"], purpose)
        split = data["split"]
        if (stream["identity"] != expected or data["version_id"] != ref["version_id"]
                or data["dataset"]["id"] != ref["dataset_id"]
                or data["dataset"]["revision"] != str(ref["revision"])
                or data["dataset"]["content_digest"] != ref["content_digest"]
                or split["id"] != stream["split_id"]
                or split["role"] not in {"training": {"train"}, "evaluation": {"validation", "test"},
                                         "calibration": {"calibration"}}[purpose]
                or type(split["sample_count"]) is not int or split["sample_count"] <= 0
                or ref["manifest"].get("access_mode") != "streaming"):
            fail("dataset_identity_mismatch", "Streaming dataset descriptor differs from its immutable identity")
        limits(stream["limits"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ServiceError("dataset_reference_invalid", "Streaming dataset descriptor has invalid identities") from exc
    return {"dataset": copy.deepcopy(data["dataset"]), "split": copy.deepcopy(split),
            "version_id": data["version_id"], "stream_identity": expected, "purpose": purpose}


_RUNTIME_INDEXES = OrderedDict()
_RUNTIME_LOCK = threading.Lock()


def _resolve_stream(data):
    stream = data.get("stream", {})
    _validate_descriptor(data, purpose=stream.get("purpose"))
    key = digest({key: stream[key] for key in ("reference", "dependency_paths", "source_guards", "limits")})
    with _RUNTIME_LOCK:
        entry = _RUNTIME_INDEXES.get(key)
        if entry is not None:
            _RUNTIME_INDEXES.move_to_end(key)
    if entry is None:
        from .dataset import load_portable_split
        checked, index = load_portable_split(stream["reference"], stream["dependency_paths"], stream["split_id"],
            purpose=stream["purpose"], expected_project_id=stream["reference"]["project_id"],
            source_guards=stream["source_guards"], streaming_limits=stream["limits"], _return_index=True)
        if (checked["dataset"] != data["dataset"] or checked["split"] != data["split"]
                or any(checked.get(key) != data.get(key) for key in ("family_policy", "final_dataset_family_coverage"))):
            fail("dataset_identity_mismatch", "Streaming artifact differs from its verified split")
        entry = checked, index
        with _RUNTIME_LOCK:
            _RUNTIME_INDEXES[key] = entry
            while len(_RUNTIME_INDEXES) > 4:
                _RUNTIME_INDEXES.popitem(last=False)
    else:
        _, index = entry
        ref = stream["reference"]
        record = version_record(ref["project_id"], ref["dataset_id"], ref["revision"], index.manifest, index,
                                index.summary["splits"], index.summary["transformations"])
        checked = stream_data(record, ref, stream["dependency_paths"], stream["source_guards"],
                              stream["split_id"], stream["purpose"], stream["limits"])
        if (checked["dataset"] != data["dataset"] or checked["split"] != data["split"]
                or any(checked.get(key) != data.get(key) for key in ("family_policy", "final_dataset_family_coverage"))):
            fail("dataset_identity_mismatch", "Streaming artifact differs from its verified split")
        for source in index.manifest["sources"]:
            verify_source(index.registry, index.project_id, source)
        with index.connect():
            pass
    return checked, index


def validate_stream_data(data, *, purpose="training"):
    """Validate structure, source hashes, row/split lineage and purpose before use."""
    identity = _validate_descriptor(data, purpose=purpose)
    _resolve_stream(data)
    return identity


def iter_stream_rows(data):
    """Reiterable epochs with bounded decoding and exact per-row source lineage."""
    checked, index = _resolve_stream(data)
    chain = Chain()
    for item in indexed_rows(index, data["stream"]["split_id"]):
        chain.add({key: value for key, value in item.items() if key != "row"})
        yield item
    if chain.count != checked["split"]["sample_count"] or chain.value() != checked["split"]["lineage_digest"]:
        fail("dataset_identity_mismatch", "Streaming iteration differs from its pinned split lineage")


class StreamingConsumption:
    """Append only IDs actually delivered to a consumer; repeats preserve order.

    No registration or preview claims consumption. Framework adapters call add
    at their actual batch boundary; snapshots are compact checkpoint lineage.
    """
    def __init__(self, data):
        self.identity = _validate_descriptor(data, purpose=data.get("stream", {}).get("purpose"))
        checked, self.index = _resolve_stream(data)
        if "final_dataset_family_coverage" in checked:
            self.identity["final_dataset_family_coverage"] = copy.deepcopy(checked["final_dataset_family_coverage"])
        self.chain = Chain()
        self._index_signature = signature(self.index.path.stat())
        self._db = sqlite3.connect(self.index.path.as_uri() + "?mode=ro", uri=True)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA cache_size=-2048")

    def close(self):
        if getattr(self, "_db", None) is not None:
            self._db.close()
            self._db = None

    def __del__(self):
        self.close()

    def add(self, item):
        if (not isinstance(item, dict) or item.get("split_id") != self.identity["split"]["id"]
                or not isinstance(item.get("sample_id"), str)
                or not isinstance(item.get("content_digest"), str)):
            fail("dataset_consumption_invalid", "Consumption needs an item yielded by the pinned stream")
        identity = {key: item.get(key) for key in ("sample_id", "source_id", "source_row",
                        "source_content_digest", "content_digest", "split_id")}
        # Indexed membership checks are bounded and do not retain an ID set.
        if signature(self.index.path.stat()) != self._index_signature:
            fail("dataset_identity_mismatch", "Dataset lineage index changed during consumption")
        expected = self._db.execute("SELECT * FROM samples WHERE sample_id=? AND alive=1", (item["sample_id"],)).fetchone()
        if expected is None or row_identity(expected) != identity:
            fail("dataset_consumption_invalid", "Consumed row does not belong to the pinned split lineage")
        self.chain.add(identity)

    def snapshot(self):
        result = {"schema": CONSUMPTION_SCHEMA, **copy.deepcopy(self.identity), "count": self.chain.count,
                  "ordered_lineage_digest": self.chain.value(), "digest_algorithm": CHAIN_SCHEMA}
        result["digest"] = digest(result)
        return result


def materialize_stream_split(data, *, max_rows, max_bytes):
    """Bridge a bounded selected split to exact-ID eager evaluation consumers.

    Never materializes other splits. Pinned row/byte counts preflight the entire
    selected split before row decoding; all IDs/digests retain the same version.
    """
    if type(max_rows) is not int or type(max_bytes) is not int or min(max_rows, max_bytes) < 1:
        fail("dataset_limit_exceeded", "Selected split materialization needs positive row and byte budgets")
    purpose = data.get("stream", {}).get("purpose")
    identity = _validate_descriptor(data, purpose=purpose)
    if identity["split"]["sample_count"] > max_rows or identity["split"].get("decoded_bytes", max_bytes + 1) > max_bytes:
        fail("dataset_limit_exceeded", "Selected split exceeds the consumer's explicit materialization budget")
    checked, index = _resolve_stream(data)
    if checked["split"]["sample_count"] > max_rows or checked["split"]["decoded_bytes"] > max_bytes:
        fail("dataset_limit_exceeded", "Verified split exceeds the consumer's explicit materialization budget")
    values, ids, hashes, size, chain = [], [], [], 0, Chain()
    for item in indexed_rows(index, data["stream"]["split_id"]):
        size += len(encoded(item["row"]))
        if len(values) >= max_rows or size > max_bytes:
            fail("dataset_limit_exceeded", "Selected split exceeds the consumer's explicit materialization budget")
        values.append(item["row"])
        ids.append(item["sample_id"])
        hashes.append(item["content_digest"])
        chain.add({key: value for key, value in item.items() if key != "row"})
    if chain.count != checked["split"]["sample_count"] or chain.value() != checked["split"]["lineage_digest"]:
        fail("dataset_identity_mismatch", "Materialized selected split differs from its pinned lineage")
    split = {"id": checked["split"]["id"], "role": checked["split"]["role"], "sample_ids": ids}
    consumption = {"schema": "modelbuilder.dataset-consumption/v1", "project_id": data["stream"]["reference"]["project_id"],
                   "dataset": checked["dataset"], "split": split, "version_id": checked["version_id"], "purpose": purpose,
                   "sample_ids": list(ids), "sample_digests": hashes, "count": len(ids), "unique_count": len(ids)}
    if "final_dataset_family_coverage" in checked:
        consumption["final_dataset_family_coverage"] = copy.deepcopy(checked["final_dataset_family_coverage"])
    consumption["digest"] = digest(consumption)
    return {"dataset": checked["dataset"], "split": split, "rows": values, "consumption": consumption,
            **{key: copy.deepcopy(checked[key]) for key in ("family_policy", "final_dataset_family_coverage") if key in checked}}
