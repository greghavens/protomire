"""Standalone Arrow streaming decoder: inherited checked FD, no paths or plugins."""
from __future__ import annotations

import json
import os
import sys

MEMORY_LIMIT = 512 * 1024**2
MAX_GROUP_BYTES = 256 * 1024**2


class Rejected(Exception):
    pass


def main():
    import resource
    for kind, bound in ((resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT)),
                        (resource.RLIMIT_CPU, (590, 591)), (resource.RLIMIT_CORE, (0, 0))):
        resource.setrlimit(kind, bound)
        if resource.getrlimit(kind) != bound:
            raise Rejected("capability_unavailable")
    options = json.loads(sys.stdin.buffer.readline(4097))
    limits = options["limits"]
    if (any(type(value) is not int or not 0 < value <= 2**63 - 1 for value in limits.values())
            or not 0 < limits["max_row_bytes"] <= 16 * 1024**2):
        raise Rejected("dataset_limit_exceeded")
    import pyarrow as arrow
    import pyarrow.parquet as parquet
    if arrow.__version__ != "25.0.0":
        raise Rejected("dependency_mismatch")
    with os.fdopen(os.dup(int(sys.argv[1])), "rb") as stream:
        with parquet.ParquetFile(stream, pre_buffer=False, memory_map=False,
                thrift_string_size_limit=1024 * 1024, thrift_container_size_limit=100_000,
                page_checksum_verification=True, arrow_extensions_enabled=False) as table:
            metadata = table.metadata
            if metadata.num_rows > limits["max_rows"] or metadata.num_row_groups > 100_000 or metadata.num_columns > 256:
                raise Rejected("dataset_limit_exceeded")
            def check_fields(fields, depth=0):
                if depth > 8 or len(fields) > 256 or len({field.name for field in fields}) != len(fields):
                    raise Rejected("dataset_source_invalid")
                for field in fields:
                    kind = field.type
                    if arrow.types.is_struct(kind):
                        check_fields(list(kind), depth + 1)
                    elif arrow.types.is_list(kind) or arrow.types.is_large_list(kind) or arrow.types.is_fixed_size_list(kind):
                        check_fields([kind.value_field], depth + 1)
                    elif not (arrow.types.is_string(kind) or arrow.types.is_large_string(kind)
                              or arrow.types.is_boolean(kind) or arrow.types.is_integer(kind)
                              or arrow.types.is_floating(kind) or arrow.types.is_null(kind)):
                        raise Rejected("dataset_source_invalid")
            check_fields(list(table.schema_arrow))
            total, number, decoded = 0, 0, 0
            for group_id in range(metadata.num_row_groups):
                group = metadata.row_group(group_id)
                group_bytes = 0
                for column_id in range(group.num_columns):
                    column = group.column(column_id)
                    if column.file_path or column.total_uncompressed_size < 0 or column.num_values < 0:
                        raise Rejected("dataset_source_invalid")
                    group_bytes += column.total_uncompressed_size
                total += group_bytes
                if group_bytes > MAX_GROUP_BYTES or total > limits["max_decoded_bytes"]:
                    raise Rejected("dataset_limit_exceeded")
                if options["group"] is not None and options["group"] != group_id:
                    number += group.num_rows
                    continue
                row_id = 0
                for batch in table.iter_batches(batch_size=64, row_groups=[group_id], use_threads=False, use_pandas_metadata=False):
                    if batch.nbytes > MAX_GROUP_BYTES:
                        raise Rejected("dataset_limit_exceeded")
                    for row in batch.to_pylist():
                        number += 1
                        if options["group"] is None or row_id >= options["start"]:
                            raw_row = json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                            decoded += len(raw_row.encode())
                            if len(raw_row.encode()) > limits["max_row_bytes"] or decoded > limits["max_decoded_bytes"]:
                                raise Rejected("dataset_limit_exceeded")
                            line = {"number": number, "locator": {"group": group_id, "row": row_id}, "row": row}
                            sys.stdout.buffer.write(json.dumps(line, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode() + b"\n")
                        row_id += 1
                if row_id != group.num_rows:
                    raise Rejected("dataset_source_invalid")
            if options["group"] is None and number != metadata.num_rows:
                raise Rejected("dataset_source_invalid")


if __name__ == "__main__":
    try:
        main()
    except Rejected as error:
        print(json.dumps({"error": str(error)}))
        raise SystemExit(1)
    except MemoryError:
        print('{"error":"dataset_limit_exceeded"}')
        raise SystemExit(1)
    except Exception:
        print('{"error":"dataset_source_invalid"}')
        raise SystemExit(1)
