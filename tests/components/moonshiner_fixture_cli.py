"""Owned deterministic CLI double. No Moonshiner/provider code is executed."""
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import time

mode, *argv = sys.argv[1:]
assert len(argv) == 8 and argv[:3] == ["dataset", "compose", "--source"]
assert argv[4] == "--out" and argv[6] == "--seed"
source, output, seed = Path(argv[3]), Path(argv[5]), int(argv[7])
if mode in {"sleep", "spawn", "spawn-return"}:
    if mode in {"spawn", "spawn-return"}:
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
        (output.parent / "descendant.pid").write_text(str(child.pid))
    time.sleep(.15 if mode == "spawn-return" else 60)
if mode == "failure":
    raise SystemExit(17)
if mode == "log-flood":
    print("x" * (2 * 1024 * 1024))
    time.sleep(60)
rows, seen = [], set()
for i, row in enumerate(json.loads(line) for line in source.read_text().splitlines()):
    key = json.dumps(row["messages"], sort_keys=True)
    if key in seen:
        continue
    seen.add(key)
    # Actual compose normalizes messages/meta and drops target_message_index.
    rows.append({"messages": row["messages"], "meta": {"source": str(source), "source_row": i}})
random.Random(seed).shuffle(rows)
if mode == "mutated":
    rows[0]["messages"][-1]["content"] = "Unreviewed replacement"
if mode == "bad-index":
    rows[0]["meta"]["source_row"] = True
if mode == "duplicate":
    rows.append(rows[0])
if mode == "empty":
    rows = []
raw = "".join(json.dumps(row) + "\n" for row in rows)
if mode == "malformed":
    raw = '{"messages":'
if mode == "nonfinite":
    raw = '{"value": NaN}\n'
if mode == "duplicate-key":
    raw = '{"messages": [], "messages": []}\n'
if mode == "oversized":
    raw = "x" * (20 * 1024 * 1024)
output.write_text(raw)
manifest = {"manifest_version": 2, "dry_run": False, "rows": len(rows), "seed": seed,
    "sources": [{"spec": str(source), "kind": "local", "path": str(source),
                 "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}],
    "output": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
if mode == "bad-manifest":
    manifest["sha256"] = "0" * 64
if mode == "remote-source":
    manifest["sources"][0]["kind"] = "huggingface"
output.with_suffix(".jsonl.manifest.json").write_text(json.dumps(manifest))
if mode == "symlink":
    regular = output.with_name("elsewhere.jsonl")
    output.rename(regular)
    output.symlink_to(regular)
