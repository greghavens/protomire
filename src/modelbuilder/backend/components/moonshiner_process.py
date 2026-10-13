"""Bounded installed-CLI boundary; no installer, credentials, detached service or provider path."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time

import psutil

from . import identity
from .moonshiner import UPSTREAM_COMMIT, UPSTREAM_VERSION
from ..process_execution import _stop_descendants
from ...export_packages import safe_file

CONFIGURATION = {"pipeline": {"queues": {"seed_authoring": False, "tracing": False}},
    "publish": {"hf_dataset": None}, "synthetic_corrections": {"enabled": False, "hf_dataset": None},
    "source": {}, "teacher": {"model": ""}, "judge": {"model": ""}}


def installation_identity():
    """Read installed bytes and metadata without importing/running Moonshiner.

    The upstream commit is the reviewed compatibility contract, not an assertion
    that arbitrary package metadata proves Git provenance. The source digest
    identifies the exact operator-reviewed installed package tree instead.
    """
    from ..runner import environment_snapshot
    distribution = importlib.metadata.distribution("moonshiner")
    if distribution.version != UPSTREAM_VERSION:
        raise ValueError("Only reviewed Moonshiner 0.8.23 is supported")
    root = Path(distribution.locate_file("moonshiner_app")).absolute()
    if any(part.is_symlink() for part in (root, *root.parents)) or not root.is_dir():
        raise ValueError("Moonshiner requires a regular installed package tree")
    files, total = {}, 0
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise ValueError("Moonshiner installed source contains a symlink")
        if path.is_dir():
            continue
        with safe_file(path, max_bytes=32 * 1024 * 1024) as stream:
            total += os.fstat(stream.fileno()).st_size
            if total > 512 * 1024 * 1024 or len(files) >= 100000:
                raise ValueError("Moonshiner installed source exceeds inspection bounds")
            files[str(path.relative_to(root))] = hashlib.file_digest(stream, "sha256").hexdigest()
    if not {"cli.py", "bundle/moonshiner.py", "bundle/src/dataset_prep.py", "bundle/config.json"} <= files.keys():
        raise ValueError("Moonshiner installed CLI closure is incomplete")
    return {"version": distribution.version, "reviewed_upstream_commit": UPSTREAM_COMMIT,
            "source_sha256": identity(files), "environment_sha256": identity(environment_snapshot())}


def _loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key in Moonshiner output")
            result[key] = value
        return result
    def reject(value):
        raise ValueError("Nonfinite JSON in Moonshiner output")
    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)
    json.dumps(result, allow_nan=False)
    return result


def read_jsonl(path, limits):
    with safe_file(path, max_bytes=limits["max_bytes"]) as stream:
        data = stream.read(limits["max_bytes"] + 1)
    if len(data) > limits["max_bytes"]:
        raise ValueError("Moonshiner output exceeds byte bound")
    rows = []
    for line in data.splitlines():
        if line.strip():
            rows.append(_loads(line))
            if len(rows) > limits["max_records"]:
                raise ValueError("Moonshiner output exceeds record bound")
    return rows


def command(source, output, seed):
    # Never accept argv, executable, module, tokenizer, HF source or config
    # overrides from a recipe. -I removes PYTHONPATH and cwd imports.
    return [sys.executable, "-I", "-B", str(Path(__file__).with_name("moonshiner_worker.py")),
            "dataset", "compose", "--source", str(source), "--out", str(output), "--seed", str(seed)]


def _execute(argv, *, work, env, limits, cancel_file, output):
    """Keep the native worker's process group and reap observed descendants."""
    if cancel_file and Path(cancel_file).exists():
        raise RuntimeError("Moonshiner composition cancelled before process startup")
    observed = {}
    child = None
    log_size = 0
    try:
        with (work / "moonshiner.log").open("wb") as log:
            child = subprocess.Popen(argv, cwd=work, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=False)
            os.set_blocking(child.stdout.fileno(), False)
            deadline = time.monotonic() + limits["timeout_seconds"]
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    try:
                        for process in psutil.Process(child.pid).children(recursive=True):
                            observed[(process.pid, process.create_time())] = process
                    except psutil.NoSuchProcess:
                        pass
                    if cancel_file and Path(cancel_file).exists():
                        raise RuntimeError("Moonshiner composition cancelled")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Moonshiner composition deadline exceeded")
                    if output.exists() and (output.is_symlink() or output.stat().st_size > limits["max_bytes"]):
                        raise ValueError("Moonshiner output exceeds safe file/byte bound")
                    events = selector.select(.025)
                    for _, _ in events:
                        chunk = os.read(child.stdout.fileno(), 65536)
                        log_size += len(chunk)
                        if log_size > 1024 * 1024:
                            raise ValueError("Moonshiner log exceeds byte bound")
                        log.write(chunk)
                    if child.poll() is not None:
                        break
            if child.wait() != 0:
                raise ValueError("Moonshiner CLI failed; inspect the bounded native attempt log")
    finally:
        if child is not None:
            try:
                for process in psutil.Process(child.pid).children(recursive=True):
                    observed[(process.pid, process.create_time())] = process
            except psutil.NoSuchProcess:
                pass
            # Ask descendants to exit while their parent can still reap them.
            remaining = _stop_descendants(list(observed.values()))
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            if child.stdout:
                child.stdout.close()
            if remaining:
                raise RuntimeError("Moonshiner descendant cleanup could not be confirmed")


def compose(source, work, parameters, context):
    if sys.platform != "linux":
        raise ValueError("Moonshiner v1 foreground lifecycle is accepted only on Linux")
    source, work = Path(source).resolve(), Path(work).resolve()
    output = work / "composed.jsonl"
    # Upstream requires model workspace storage outside its CLI project cwd.
    home = work.parent / "moonshiner-private-home"
    state = work / ".moonshiner"
    home.mkdir(mode=0o700)
    (home / "runtime").mkdir(mode=0o700)
    (home / "tmp").mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    config = {**CONFIGURATION, "workspace": {"confirmed_root": str(work)}, "storage": {"root": str(state)}}
    (state / "config.json").write_text(json.dumps(config, sort_keys=True))
    env = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
        "HOME": str(home), "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"), "XDG_DATA_HOME": str(home / "data"),
        "XDG_RUNTIME_DIR": str(home / "runtime"), "TMPDIR": str(home / "tmp"),
        "MOONSHINER_HOME": str(state),
        "HF_HOME": str(home / "hf"), "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    _execute(command(source, output, parameters["seed"]), work=work, env=env,
             limits=parameters["limits"], cancel_file=context.get("cancel_file"), output=output)
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_digest:
        raise ValueError("Moonshiner changed the reviewed source file")
    sidecar = output.with_suffix(".jsonl.manifest.json")
    with safe_file(sidecar, max_bytes=1024 * 1024) as stream:
        manifest = _loads(stream.read(1024 * 1024 + 1))
    rows = read_jsonl(output, parameters["limits"])
    with safe_file(output, max_bytes=parameters["limits"]["max_bytes"]) as stream:
        output_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if (not isinstance(manifest, dict) or manifest.get("manifest_version") != 2
            or manifest.get("dry_run") is not False or type(manifest.get("rows")) is not int
            or manifest.get("rows") != len(rows)
            or manifest.get("output") != str(output) or manifest.get("sha256") != output_digest
            or manifest.get("seed") != parameters["seed"]
            or manifest.get("sources") != [{"spec": str(source), "kind": "local", "path": str(source),
                "sha256": source_digest}]):
        raise ValueError("Moonshiner composition manifest identity mismatch")
    return output, {"configuration": CONFIGURATION}
