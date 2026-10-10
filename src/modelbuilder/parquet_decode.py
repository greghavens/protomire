"""Standalone Linux resource-contained Parquet decoder; no caller paths or code.

The parent sends verified bounded bytes only. This helper is deliberately usable
from an installed backend wheel without importing the service or model libraries.
"""
from __future__ import annotations
import io
import json
import sys

PYARROW_REQUIREMENT = "pyarrow==25.0.0"
PARQUET_METADATA_BYTES = 1024 * 1024
PARQUET_METADATA_ITEMS = 100_000
MEMORY_LIMIT = 512 * 1024 * 1024

class DecodeError(Exception):
    def __init__(self, code, message="", details=None):
        self.code = code
        super().__init__(message)

def _fail(code, message):
    raise DecodeError(code, message)

def decode(data, max_file_bytes, max_rows, decoded_budget):
    try:
        import pyarrow as arrow
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise DecodeError("capability_unavailable",
                           "Parquet requires the optional pinned PyArrow profile; bootstrap with --parquet") from exc
    if arrow.__version__ != PYARROW_REQUIREMENT.split("==")[1]:
        _fail("dependency_mismatch", "Parquet adapter differs from the pinned optional dependency profile")
    try:
        # Only verified bytes enter Arrow. Never give it a filesystem path,
        # URI, directory dataset, partition resolver, or decryption callback.
        with parquet.ParquetFile(
                io.BytesIO(data), pre_buffer=False, memory_map=False,
                thrift_string_size_limit=min(max_file_bytes, PARQUET_METADATA_BYTES),
                thrift_container_size_limit=PARQUET_METADATA_ITEMS,
                page_checksum_verification=True, arrow_extensions_enabled=False) as table:
            metadata = table.metadata
            if (metadata.num_rows > max_rows or metadata.num_row_groups > 4096
                    or metadata.num_columns > 256):
                _fail("dataset_limit_exceeded", "Parquet metadata exceeds configured structural limits")
            encoded_size, row_count = 0, 0
            for index in range(metadata.num_row_groups):
                group = metadata.row_group(index)
                row_count += group.num_rows
                for index in range(group.num_columns):
                    column = group.column(index)
                    if column.file_path:
                        _fail("dataset_source_invalid", "Parquet column references must remain in the pinned file")
                    if column.total_uncompressed_size < 0 or column.num_values < 0:
                        _fail("dataset_source_invalid", "Parquet metadata contains invalid sizes")
                    encoded_size += column.total_uncompressed_size
                    if encoded_size > decoded_budget or column.num_values > decoded_budget:
                        _fail("dataset_limit_exceeded", "Uncompressed Parquet data exceeds the configured byte limit")
            if row_count != metadata.num_rows:
                _fail("dataset_source_invalid", "Parquet row counts are inconsistent")

            def check_fields(fields, depth=0):
                if depth > 8 or len(fields) > 256:
                    _fail("dataset_limit_exceeded", "Parquet schema exceeds configured structural limits")
                if len({field.name for field in fields}) != len(fields):
                    _fail("dataset_source_invalid", "Parquet fields must have unique names")
                for field in fields:
                    kind = field.type
                    if arrow.types.is_struct(kind):
                        check_fields(list(kind), depth + 1)
                    elif (arrow.types.is_list(kind) or arrow.types.is_large_list(kind)
                          or arrow.types.is_fixed_size_list(kind)):
                        check_fields([kind.value_field], depth + 1)
                    elif not (arrow.types.is_string(kind) or arrow.types.is_large_string(kind)
                              or arrow.types.is_boolean(kind) or arrow.types.is_integer(kind)
                              or arrow.types.is_floating(kind) or arrow.types.is_null(kind)):
                        _fail("dataset_source_invalid", "Parquet columns must use portable JSON-compatible types")
            check_fields(list(table.schema_arrow))
            rows, decoded = [], 0
            # These logical limits run inside an OS-memory-contained process.
            # Metadata cannot bound dictionary or nested decoded allocation.
            for batch in table.iter_batches(batch_size=64, use_threads=False, use_pandas_metadata=False):
                decoded += batch.nbytes
                if decoded > decoded_budget or len(rows) + batch.num_rows > max_rows:
                    _fail("dataset_limit_exceeded", "Decoded Parquet data exceeds configured limits")
                rows.extend(batch.to_pylist())
            if len(rows) != metadata.num_rows:
                _fail("dataset_source_invalid", "Decoded Parquet row count differs from its metadata")
            size = 0
            for row in rows:
                size += len(json.dumps(row, allow_nan=False).encode("utf-8"))
                if size > decoded_budget:
                    _fail("dataset_limit_exceeded", "Decoded JSON exceeds the configured byte limit")
            return rows
    except (MemoryError, arrow.ArrowMemoryError) as exc:
        raise DecodeError("dataset_limit_exceeded", "Decoder memory limit reached") from exc
    except (arrow.ArrowException, OSError) as exc:
        raise DecodeError("dataset_source_invalid", "Parquet source is invalid or exceeds metadata limits",
                           {}) from exc


def main():
    if sys.platform != "linux":
        raise DecodeError("capability_unavailable")
    # A failure to establish any hard limit disables this optional capability;
    # never continue parsing with an advisory or weaker fallback.
    try:
        import resource
        limits = [(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT)),
                  (resource.RLIMIT_CPU, (15, 16)),
                  (resource.RLIMIT_FSIZE, (34 * 1024 * 1024, 34 * 1024 * 1024)),
                  (resource.RLIMIT_CORE, (0, 0))]
        for kind, bound in limits:
            resource.setrlimit(kind, bound)
            if resource.getrlimit(kind) != bound:
                raise DecodeError("capability_unavailable")
    except (ImportError, OSError, ValueError) as exc:
        raise DecodeError("capability_unavailable") from exc
    options = json.loads(sys.stdin.buffer.readline(1025))
    if (not isinstance(options, dict) or set(options) != {"max_file_bytes", "max_rows", "decoded_budget"}
            or any(type(value) is not int or value <= 0 for value in options.values())
            or options["max_file_bytes"] > 16 * 1024 * 1024 or options["max_rows"] > 100_000
            or options["decoded_budget"] > 32 * 1024 * 1024):
        raise DecodeError("dataset_limit_exceeded")
    data = sys.stdin.buffer.read(options["max_file_bytes"] + 1)
    if len(data) > options["max_file_bytes"]:
        raise DecodeError("dataset_limit_exceeded")
    rows = decode(data, **options)
    # Escaped Unicode may occupy up to six bytes per original UTF-8 byte;
    # use UTF-8 JSON, with a strict bound checked before writing to the parent.
    result = json.dumps({"rows": rows}, allow_nan=False, ensure_ascii=False).encode("utf-8")
    if len(result) > options["decoded_budget"] + 2 * options["max_rows"] + 1024:
        raise DecodeError("dataset_limit_exceeded")
    sys.stdout.buffer.write(result)
    return 0

if __name__ == "__main__":
    try:
        code = main()
    except DecodeError as exc:
        sys.stdout.buffer.write(json.dumps({"error": exc.code}).encode())
        code = 1
    except MemoryError:
        sys.stdout.buffer.write(b'{"error":"dataset_limit_exceeded"}')
        code = 1
    except Exception:
        sys.stdout.buffer.write(b'{"error":"dataset_source_invalid"}')
        code = 1
    raise SystemExit(code)
