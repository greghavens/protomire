"""Bounded process boundary for optional Parquet decoding, never in-process Arrow."""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

from .models import ServiceError

PYARROW_VERSION = "25.0.0"
WALL_SECONDS = 20
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_DECODED_BYTES = 32 * 1024 * 1024
MAX_ROWS = 100_000
_SLOTS = threading.BoundedSemaphore(2)
_ERRORS = {
    "capability_unavailable": "A memory-contained Parquet decoder is unavailable. Use JSONL/CSV or a supported Linux service/backend with the optional --parquet profile.",
    "dependency_mismatch": "Parquet adapter differs from the pinned optional dependency profile.",
    "dataset_source_invalid": "Parquet source is invalid or exceeds structural limits.",
    "dataset_limit_exceeded": "Parquet exceeds its decoded-data or isolated-process resource limit.",
    "dataset_decode_timeout": "Parquet decoding exceeded its bounded 20-second wall deadline.",
    "dataset_decode_busy": "Two Parquet decoders are already active in this service process; retry later.",
}


def _fail(code):
    raise ServiceError(code, _ERRORS[code])


def require_platform():
    # Linux RLIMIT_AS is a hard bound for native allocations, not an advisory
    # RSS observation. Equivalent macOS/Windows containment is not yet verified.
    if sys.platform != "linux":
        _fail("capability_unavailable")


def read_parquet(data, *, max_file_bytes, max_rows, decoded_budget):
    require_platform()
    try:
        version = importlib.metadata.version("pyarrow")
    except importlib.metadata.PackageNotFoundError:
        _fail("capability_unavailable")
    if version != PYARROW_VERSION:
        _fail("dependency_mismatch")
    limits = {"max_file_bytes": min(max_file_bytes, MAX_FILE_BYTES),
              "max_rows": min(max_rows, MAX_ROWS), "decoded_budget": min(decoded_budget, MAX_DECODED_BYTES)}
    if (not isinstance(data, bytes) or any(type(value) is not int or value <= 0 for value in limits.values())
            or len(data) > limits["max_file_bytes"]):
        _fail("dataset_limit_exceeded")
    if not _SLOTS.acquire(blocking=False):
        _fail("dataset_decode_busy")
    try:
        environment = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
                       "ARROW_NUM_THREADS": "1"}
        # The child receives bytes, never a path, URI, source manifest, or code.
        wire = json.dumps(limits, separators=(",", ":")).encode() + b"\n" + data
        maximum_output = limits["decoded_budget"] + 2 * limits["max_rows"] + 1024
        # An anonymous output file plus child RLIMIT_FSIZE bounds output before
        # any parent allocation. No raw dataset output is retained on disk.
        with tempfile.TemporaryFile() as output:
            try:
                result = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).with_name("parquet_decode.py"))],
                    input=wire, stdout=output, stderr=subprocess.DEVNULL, env=environment,
                    timeout=WALL_SECONDS, check=False)
            except subprocess.TimeoutExpired:
                _fail("dataset_decode_timeout")
            except OSError:
                _fail("capability_unavailable")
            if os.fstat(output.fileno()).st_size > maximum_output:
                _fail("dataset_limit_exceeded")
            output.seek(0)
            raw = output.read(maximum_output + 1)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            _fail("dataset_limit_exceeded" if result.returncode else "dataset_source_invalid")
        if isinstance(value, dict) and value.get("error") in _ERRORS:
            _fail(value["error"])
        if (result.returncode or not isinstance(value, dict) or set(value) != {"rows"}
                or not isinstance(value["rows"], list) or len(value["rows"]) > limits["max_rows"]):
            _fail("dataset_source_invalid")
        return value["rows"]
    finally:
        _SLOTS.release()
