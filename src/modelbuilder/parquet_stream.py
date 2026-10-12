"""Bounded streaming bridge to a memory-contained, descriptor-only Arrow worker."""
from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import threading

from .models import ServiceError
from .parquet_reader import _SLOTS, PYARROW_VERSION, require_platform


def iter_parquet(stream, limits, *, group=None, start=0):
    require_platform()
    try:
        if importlib.metadata.version("pyarrow") != PYARROW_VERSION:
            raise ServiceError("dependency_mismatch", "Parquet adapter differs from the pinned dependency profile")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ServiceError("capability_unavailable", "Streaming Parquet requires the pinned optional PyArrow profile") from exc
    if not _SLOTS.acquire(blocking=False):
        raise ServiceError("dataset_decode_busy", "Two Parquet decoders are already active; retry later")
    process, timer = None, None
    timed_out = threading.Event()
    try:
        options = {"limits": limits, "group": group, "start": start}
        stream.seek(0)
        process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).with_name("parquet_stream_decode.py")),
                                    str(stream.fileno())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, pass_fds=(stream.fileno(),),
                                   env={"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
                                        "ARROW_NUM_THREADS": "1"})
        # Streaming may legitimately process many shards/rows; hard memory limits
        # still apply throughout. A stalled or adversarial single source is bounded.
        def timeout():
            timed_out.set()
            process.kill()
        timer = threading.Timer(600, timeout)
        timer.daemon = True
        timer.start()
        process.stdin.write(json.dumps(options).encode() + b"\n")
        process.stdin.close()
        while raw := process.stdout.readline(limits["max_row_bytes"] + 2049):
            if len(raw) > limits["max_row_bytes"] + 2048 or not raw.endswith(b"\n"):
                raise ServiceError("dataset_limit_exceeded", "Decoded Parquet row exceeds its output budget")
            try:
                item = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise ServiceError("dataset_source_invalid", "Parquet decoder returned invalid output") from exc
            if "error" in item:
                code = item["error"] if item["error"] in {"dataset_limit_exceeded", "dataset_source_invalid", "capability_unavailable", "dependency_mismatch"} else "dataset_source_invalid"
                raise ServiceError(code, "Streaming Parquet decoder rejected the source or its resource limits")
            yield item["number"], item["locator"], item["row"]
        code = process.wait()
        if timed_out.is_set():
            raise ServiceError("dataset_decode_timeout", "Parquet source exceeded the bounded ten-minute decode deadline")
        if code:
            raise ServiceError("dataset_limit_exceeded", "Parquet decoder exceeded its isolated resource limit")
    except OSError as exc:
        raise ServiceError("capability_unavailable", "A memory-contained streaming Parquet decoder is unavailable") from exc
    finally:
        if timer is not None:
            timer.cancel()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout:
                process.stdout.close()
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        _SLOTS.release()


def read_parquet_row(stream, locator, limits):
    # Compatibility for isolated random rows; page/epoch reads keep one decoder
    # alive per row group in dataset_streaming.indexed_rows.
    iterator = iter_parquet(stream, limits, group=locator["group"], start=locator["row"])
    try:
        return next(iterator)[2]
    finally:
        iterator.close()
