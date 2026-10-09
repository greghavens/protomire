#!/usr/bin/env python3
"""Reproduce local no-inference assistant readiness using a disposable read-only service.

Does not install clients, sign in, read vendor auth stores, invoke model turns,
or call Modelbuilder tools. Run with the installed service's Python interpreter.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("modelbuilder_assistant_setup", ROOT / "distribution/assistant_setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def run(client: str, binary: str | None = None) -> dict:
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "host_target": setup.host_target(),
              "scope": "Isolated native MCP and existing unauthenticated vendor CLI discovery. "
                       "Not exact IDE extension or authenticated agent acceptance.",
              "credential_kind": "disposable synthetic read-only token", "tool_calls": 0,
              "model_prompts": 0, "writes_to_real_vendor_config": 0}
    found = setup.discover_client(client, None, binary)
    if found["status"] == "found":
        version = subprocess.run([found["path"], "--version"], env=setup.safe_environment(),
                                 capture_output=True, text=True, timeout=15, check=False)
        # Retain only simple version lines, never arbitrary vendor diagnostics.
        lines = version.stdout.strip().splitlines()
        report["vendor_version"] = lines[0] if version.returncode == 0 and len(lines) == 1 and len(lines[0]) < 120 else "UNVERIFIED"
    with tempfile.TemporaryDirectory(prefix="modelbuilder-setup-acceptance-") as directory:
        home = Path(directory)
        token = secrets.token_hex(32)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = setup.safe_environment()
        env.update(MODELBUILDER_HOST="127.0.0.1", MODELBUILDER_PORT=str(port), MODELBUILDER_BACKEND="none",
                   MODELBUILDER_DB_PATH=str(home / "service.sqlite"),
                   MODELBUILDER_TOKENS_JSON=json.dumps({token: {"id": "setup-readiness-reader", "scopes": ["read"]}}))
        service = subprocess.Popen([sys.executable, "-I", "-m", "modelbuilder.api"], cwd=home, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        previous = os.environ.get("MODELBUILDER_TOKEN")
        try:
            url = f"http://127.0.0.1:{port}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(100):
                try:
                    with opener.open(url + "/health", timeout=0.2) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Disposable local service did not start.")
            os.environ["MODELBUILDER_TOKEN"] = token
            report["readiness"] = setup.doctor(client, sys.executable, url, None, binary, probe_client=True)
            if token in json.dumps(report):
                raise RuntimeError("Credential redaction check failed; no report written.")
            report["token_not_serialized"] = True
        finally:
            if previous is None:
                os.environ.pop("MODELBUILDER_TOKEN", None)
            else:
                os.environ["MODELBUILDER_TOKEN"] = previous
            service.terminate()
            try:
                service.wait(timeout=10)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=["codex", "claude"], required=True)
    parser.add_argument("--client-binary")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.client, args.client_binary)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
