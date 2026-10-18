#!/usr/bin/env python3
"""Read-only bounded collateral inventory; filename roles are review hints only."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import subprocess

PRUNED = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.cache', '.aws', '.ssh', '.modelbuilder'}
SECRET_SUFFIXES = {'.key', '.pem', '.p12', '.pfx'}


def roles(path):
    suffix = path.suffix.lower()
    result = []
    if suffix in {'.py', '.rs', '.c', '.cpp', '.h', '.ipynb'}:
        result.append('source')
    if suffix in {'.md', '.rst', '.pdf', '.bib'}:
        result.append('research_or_documentation')
    if suffix in {'.jsonl', '.parquet', '.csv', '.tsv', '.arrow'}:
        result.append('dataset_candidate')
    if suffix in {'.pt', '.pth', '.ckpt', '.safetensors', '.bin'}:
        result.append('model_or_checkpoint_candidate')
    if suffix in {'.toml', '.yaml', '.yml', '.json', '.lock'} or path.name.startswith('requirements'):
        result.append('configuration_or_manifest')
    if any(part.lower() in {'eval', 'evaluation', 'evaluations', 'benchmarks', 'tests'} for part in path.parts):
        result.append('evaluation_or_test_candidate')
    return result or ['unclassified']


def inventory(repo, max_files=10000, max_hash_bytes=64*1024*1024, max_total_hash_bytes=256*1024*1024):
    root = Path(repo).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Choose an existing repository directory, not a symlink')
    if min(max_files, max_hash_bytes, max_total_hash_bytes) < 1:
        raise ValueError('Inventory limits must be positive')
    entries, skipped, hashed, scanned, truncated = [], {'sensitive': 0, 'symlink_or_special': 0, 'pruned_directories': 0}, 0, 0, False
    for directory, folders, files in os.walk(root, followlinks=False):
        retained = []
        for name in sorted(folders):
            path = Path(directory)/name
            if name in PRUNED or path.is_symlink():
                skipped['pruned_directories'] += 1
            else:
                retained.append(name)
        folders[:] = retained
        for name in sorted(files):
            scanned += 1
            if scanned > max_files:
                truncated = True
                break
            path = Path(directory)/name
            relative = path.relative_to(root)
            if name.startswith('.env') or path.suffix.lower() in SECRET_SUFFIXES or re.search(r'(^|[._-])(credentials?|secrets?|tokens?|passwords?)([._-]|$)', name.lower()):
                skipped['sensitive'] += 1
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                skipped['symlink_or_special'] += 1
                continue
            item = {'path': relative.as_posix(), 'size_bytes': info.st_size, 'role_hints': roles(relative), 'sha256': None}
            if info.st_size <= max_hash_bytes and hashed + info.st_size <= max_total_hash_bytes:
                flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
                with os.fdopen(os.open(path, flags), 'rb') as stream:
                    opened = os.fstat(stream.fileno())
                    if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                        raise ValueError('File changed during inventory')
                    h = hashlib.sha256()
                    remaining = info.st_size
                    while remaining:
                        chunk = stream.read(min(1048576, remaining))
                        if not chunk:
                            raise ValueError('File changed during inventory')
                        h.update(chunk)
                        remaining -= len(chunk)
                    if stream.read(1) or os.fstat(stream.fileno()).st_mtime_ns != info.st_mtime_ns:
                        raise ValueError('File changed during inventory')
                    item['sha256'] = h.hexdigest()
                hashed += info.st_size
            else:
                item['hash_status'] = 'not_read_size_budget'
            entries.append(item)
        if truncated:
            break
    git = {'head': None, 'working_tree': 'unverified'}
    # Built-in read commands only: no hooks, filters, fetches, builds or imported code.
    command = ['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-C', str(root)]
    try:
        result = subprocess.run(command+['rev-parse', '--verify', 'HEAD'], text=True, capture_output=True, timeout=10)
        if result.returncode == 0:
            git['head'] = result.stdout.strip()
            status = subprocess.run(command+['status', '--porcelain', '--untracked-files=no'], capture_output=True, timeout=10)
            if status.returncode == 0:
                git['working_tree'] = 'tracked_changes' if status.stdout else 'tracked_clean_untracked_not_assessed'
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {'schema_version': 'modelbuilder.repository-inventory/v1', 'git': git, 'files': entries,
            'skipped_counts': skipped, 'truncated': truncated, 'hashed_bytes': hashed,
            'execution_allowed': False, 'roles_verified': False,
            'limits': {'max_files': max_files, 'max_hash_bytes': max_hash_bytes, 'max_total_hash_bytes': max_total_hash_bytes}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository', type=Path)
    parser.add_argument('--max-files', type=int, default=10000)
    parser.add_argument('--max-hash-bytes', type=int, default=64*1024*1024)
    args = parser.parse_args()
    print(json.dumps(inventory(args.repository, args.max_files, args.max_hash_bytes), indent=2, sort_keys=True))
