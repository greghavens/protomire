"""Bounded, process-fenced scratch-space accounting for Hub file copies.

Reservations coordinate Modelbuilder processes sharing one Hub root. They are
conservative accounting, not filesystem quotas against other OS applications.
Already materialized files consume actual free space; dead-process reservations
are reclaimed when their OS locks can be acquired.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

OVERHEAD = 16 * 1024 * 1024
MAX_RESERVATIONS = 1024


def check_free_space(root, required):
    from .hub import MAX_BYTES, fail
    if type(required) is not int or not 0 <= required <= MAX_BYTES * 2:
        fail("hub_disk_budget", "Invalid bounded scratch-space requirement")
    if shutil.disk_usage(root).free < required + OVERHEAD:
        fail("hub_disk_budget", "Insufficient free space for approved Hub staging and verified copies")


@contextmanager
def reserve_space(root, required):
    from .export_packages import safe_file
    from .hub import MAX_BYTES, TransferLock, directory, fail, safe_output
    if type(required) is not int or not 0 <= required <= MAX_BYTES * 2:
        fail("hub_disk_budget", "Invalid bounded scratch-space reservation")
    directory = directory(Path(root) / "space-reservations")
    try:
        guard = TransferLock(directory / "admission.lock")
    except BlockingIOError:
        fail("hub_busy", "Hub disk admission is busy; retry explicitly", 503)
    lease, path, lease_path = None, None, None
    try:
        reserved, count = 0, 0
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.endswith(".reserve.lock") and not Path(entry.path[:-5]).exists():
                    try:
                        orphan = TransferLock(Path(entry.path))
                    except BlockingIOError:
                        continue
                    orphan.close()
                    Path(entry.path).unlink(missing_ok=True)
                if not entry.name.endswith(".reserve"):
                    continue
                count += 1
                if count > MAX_RESERVATIONS:
                    fail("hub_disk_budget", "Too many Hub disk reservations")
                file = Path(entry.path)
                lock_file = Path(entry.path + ".lock")
                try:
                    stale = TransferLock(lock_file)
                except BlockingIOError:
                    try:
                        with safe_file(file, max_bytes=256) as handle:
                            record = json.load(handle)
                    except FileNotFoundError:
                        continue  # The owner completed and removed its lease.
                    if (not isinstance(record, dict) or set(record) != {"bytes"}
                            or type(record["bytes"]) is not int or not OVERHEAD <= record["bytes"] <= MAX_BYTES * 2 + OVERHEAD):
                        fail("hub_disk_budget", "Hub scratch-space reservation is invalid")
                    reserved += record["bytes"]
                else:
                    stale.close()
                    file.unlink(missing_ok=True)
                    lock_file.unlink(missing_ok=True)
        if shutil.disk_usage(root).free < reserved + required + OVERHEAD:
            fail("hub_disk_budget", "Free space is already reserved or insufficient for this exact Hub transfer")
        path = directory / (uuid4().hex + ".reserve")
        lease_path = Path(str(path) + ".lock")
        lease = TransferLock(lease_path)
        with safe_output(path) as handle:
            handle.write(json.dumps({"bytes": required + OVERHEAD}).encode())
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if lease:
            lease.close()
        if path:
            path.unlink(missing_ok=True)
        if lease_path:
            lease_path.unlink(missing_ok=True)
        raise
    finally:
        guard.close()
    try:
        yield
    finally:
        lease.close()
        path.unlink(missing_ok=True)
        lease_path.unlink(missing_ok=True)
