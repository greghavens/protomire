"""Controlled pipe stalls on POSIX; real pinned containment runs on Linux."""
import hashlib
import json
import subprocess
import sys
import threading
import time

import pytest

from modelbuilder import dataset_streaming as streaming
from modelbuilder import parquet_stream as parquet
from modelbuilder.models import ServiceError


def slots_available():
    acquired = []
    try:
        for _ in range(2):
            assert parquet._SLOTS.acquire(blocking=False)
            acquired.append(True)
        assert not parquet._SLOTS.acquire(blocking=False)
    finally:
        for _ in acquired:
            parquet._SLOTS.release()


@pytest.fixture
def controlled_decoder(tmp_path, monkeypatch):
    if sys.platform not in {'darwin', 'linux'}:
        pytest.skip('Controlled anonymous-pipe readiness requires POSIX')
    processes, timers = [], []
    popen, timer = subprocess.Popen, threading.Timer
    original_version = parquet.importlib.metadata.version
    # Only this synthetic stdlib child bypasses platform/Arrow discovery. Real
    # decoding below always exercises the actual verified Linux boundary.
    monkeypatch.setattr(parquet, 'require_platform', lambda: None)
    monkeypatch.setattr(parquet.importlib.metadata, 'version',
                        lambda name: parquet.PYARROW_VERSION if name == 'pyarrow' else original_version(name))
    code = {'value': ''}
    def launch(argv, **kwargs):
        process = popen([sys.executable, '-I', '-B', '-c', code['value']], **kwargs)
        processes.append(process)
        return process
    def deadline(interval, callback):
        timers.append(interval)
        return timer(interval, callback)
    monkeypatch.setattr(parquet.subprocess, 'Popen', launch)
    monkeypatch.setattr(parquet.threading, 'Timer', deadline)
    path = tmp_path / 'source.parquet'
    path.write_bytes(b'PAR1')
    yield code, path, processes, timers
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


@pytest.mark.parametrize('prefix', [b'', b'{"number":1', None], ids=['no-output', 'partial-no-newline', 'closed-output'])
def test_stalled_decoder_cooperatively_cancels_and_releases_owned_child(controlled_decoder, tmp_path, prefix):
    code, source, processes, timers = controlled_decoder
    marker, cancel = tmp_path / 'ready', tmp_path / 'cancel.json'
    emit = 'os.close(1)' if prefix is None else f'os.write(1,{prefix!r})'
    code['value'] = ('import os,sys,time\nfrom pathlib import Path\n'
                     'sys.stdin.buffer.readline()\n'
                     f'{emit}\nPath({str(marker)!r}).touch()\ntime.sleep(60)\n')
    done, outcome = threading.Event(), {}
    def compile_in_thread():
        try:
            with streaming.compilation_context(str(cancel)), source.open('rb') as stream:
                iterator = parquet.iter_parquet(stream, streaming.limits())
                try:
                    next(iterator)
                finally:
                    iterator.close()
        except BaseException as error:
            outcome['error'] = error
        finally:
            done.set()
    worker = threading.Thread(target=compile_in_thread, daemon=True)
    worker.start()
    try:
        until = time.monotonic() + 5
        while not marker.exists() and not done.is_set() and time.monotonic() < until:
            time.sleep(0.01)
        assert marker.exists(), outcome
        started = time.monotonic()
        cancel.touch()
        assert done.wait(3), 'Compiler did not observe cancellation while the decoder pipe stalled'
        assert time.monotonic() - started < 3
        assert isinstance(outcome.get('error'), ServiceError), outcome
        assert outcome['error'].code == 'dataset_cancelled'
        assert len(processes) == 1 and processes[0].poll() is not None
        assert processes[0].stdout.closed and processes[0].stdin.closed
        assert timers == [600]
        slots_available()
    finally:
        # A failed assertion must also reap the deliberately stalled child.
        for process in processes:
            if process.poll() is None:
                process.kill()
        assert done.wait(3)
        worker.join(timeout=3)
        assert not worker.is_alive()


def test_partial_output_still_enforces_the_original_record_budget(controlled_decoder):
    code, source, processes, timers = controlled_decoder
    caps = streaming.limits({'max_row_bytes': 64})
    code['value'] = ('import os,sys,time\nsys.stdin.buffer.readline()\n'
                     f'os.write(1,b"x"*{caps["max_row_bytes"] + 2049})\ntime.sleep(60)\n')
    with source.open('rb') as stream:
        iterator = parquet.iter_parquet(stream, caps)
        try:
            with pytest.raises(ServiceError) as error:
                next(iterator)
            assert error.value.code == 'dataset_limit_exceeded'
        finally:
            iterator.close()
    assert processes[0].poll() is not None and timers == [600]
    slots_available()


def test_multiple_buffered_records_and_incomplete_eof_preserve_framing(controlled_decoder):
    code, source, processes, _ = controlled_decoder
    row = json.dumps({'number': 1, 'locator': {'group': 0, 'row': 0}, 'row': {'id': '雪'}}).encode() + b'\n'
    code['value'] = f'import os,sys\nsys.stdin.buffer.readline()\nos.write(1,{(row * 3 + b"partial")!r})\n'
    with source.open('rb') as stream:
        iterator = parquet.iter_parquet(stream, streaming.limits({'max_row_bytes': 64}))
        try:
            assert [next(iterator)[2] for _ in range(3)] == [{'id': '雪'}] * 3
            with pytest.raises(ServiceError) as error:
                next(iterator)
            assert error.value.code == 'dataset_limit_exceeded'
        finally:
            iterator.close()
    assert processes[0].poll() is not None
    slots_available()


@pytest.mark.skipif(sys.platform != 'linux', reason='Real pinned Parquet cancellation requires verified Linux RLIMIT_AS containment')
def test_real_linux_parquet_decoder_cancellation_reaps_child_and_preserves_source(tmp_path, monkeypatch):
    arrow = pytest.importorskip('pyarrow')
    pq = pytest.importorskip('pyarrow.parquet')
    assert parquet.importlib.metadata.version('pyarrow') == parquet.PYARROW_VERSION
    source, cancel = tmp_path / 'source.parquet', tmp_path / 'cancel.json'
    pq.write_table(arrow.Table.from_pylist([{'id': str(i), 'text': '雪' * 100} for i in range(20_000)]),
                   source, row_group_size=256, write_page_checksum=True)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    processes, original = [], subprocess.Popen
    def observed(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(parquet.subprocess, 'Popen', observed)
    with streaming.compilation_context(str(cancel)), source.open('rb') as stream:
        iterator = parquet.iter_parquet(stream, streaming.limits())
        try:
            assert next(iterator)[2]['id'] == '0'
            cancel.touch()
            with pytest.raises(ServiceError) as error:
                next(iterator)
            assert error.value.code == 'dataset_cancelled'
        finally:
            iterator.close()
    assert len(processes) == 1 and processes[0].poll() is not None
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    slots_available()
