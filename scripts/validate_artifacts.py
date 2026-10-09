"""Check publishable tracked source without inspecting private runtime state."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
files = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
errors = []
for name in filter(None, files):
    path = root / name
    if not path.is_file():
        continue
    if path.stat().st_size > 5_000_000:
        errors.append(f"oversized tracked artifact: {name}")
    if any(part in {".venv", ".venv-backend", ".runtime", "node_modules", ".upstream"} for part in path.parts):
        errors.append(f"runtime/dependency directory tracked: {name}")
    if path.suffix in {".pt", ".safetensors", ".sqlite", ".db"}:
        errors.append(f"runtime data tracked: {name}")
    try:
        content = path.read_text()
    except UnicodeDecodeError:
        continue
    for pattern in [r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", r"\bgh[pousr]_[A-Za-z0-9]{30,}",
                    r"\bhf_[A-Za-z0-9]{25,}", r"\bsk-[A-Za-z0-9_-]{30,}"]:
        if re.search(pattern, content):
            errors.append(f"credential-shaped content: {name}")
    if path.suffix == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError:
            errors.append(f"invalid JSON: {name}")
print(json.dumps({"tracked_files": len(files) - 1, "errors": errors}, indent=2))
raise SystemExit(1 if errors else 0)
