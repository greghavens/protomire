"""Probe installed vendor transports without authentication or model inference.

Uses disposable, synthetic local service credentials and isolated vendor homes.
These checks never substitute for authenticated agent CRUD/run/error parity.
"""

import json
import os
from pathlib import Path
import secrets
import selectors
import socket
import subprocess
import tempfile
import time

import httpx

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "evidence/gui/distribution/assistant-mcp-discovery.json"
PYTHON = str(ROOT / ".venv/bin/python")
CODEX = ROOT / ".runtime/extensions/openai.chatgpt-26.5908.31748/bin/linux-x86_64/codex"
CLAUDE = ROOT / ".runtime/extensions/anthropic.claude-code-2.1.295/resources/native-binary/claude"


def main():
    report = {"status": "PARTIAL", "authenticated_agent_parity": "BLOCKED_AUTH", "probes": {}}
    with tempfile.TemporaryDirectory(prefix="assistant-mcp-", dir=ROOT / ".runtime") as tmp:
        home = Path(tmp)
        token = secrets.token_hex(32)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "CLAUDE_CODE_OAUTH_TOKEN",
                "MODELBUILDER_TOKENS_JSON",
            }
        }
        env.update(
            HOME=tmp,
            CODEX_HOME=str(home / "codex"),
            CLAUDE_CONFIG_DIR=str(home / "claude"),
            PYTHONPATH=str(ROOT / "src"),
            MODELBUILDER_API_URL=f"http://127.0.0.1:{port}",
            MODELBUILDER_HOST="127.0.0.1",
            MODELBUILDER_PORT=str(port),
            MODELBUILDER_BACKEND="none",
            MODELBUILDER_TOKEN=token,
            MODELBUILDER_DB_PATH=str(home / "service.sqlite"),
        )
        (home / "codex").mkdir()
        (home / "claude").mkdir()
        api = subprocess.Popen(
            [PYTHON, "-m", "modelbuilder.api"],
            env=env,
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        codex = None
        try:
            for _ in range(100):
                try:
                    if httpx.get(env["MODELBUILDER_API_URL"] + "/health", trust_env=False).is_success:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            config = {"type": "stdio", "command": PYTHON, "args": ["-m", "modelbuilder.mcp_server"]}

            def run(args):
                result = subprocess.run(
                    args,
                    cwd=ROOT / ".runtime/gui-workspace",
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                return {
                    "exit_code": result.returncode,
                    "stdout": result.stdout.replace(token, "[synthetic token redacted]"),
                    "stderr": result.stderr.replace(token, "[synthetic token redacted]"),
                }

            report["probes"]["claude_configuration"] = run(
                [str(CLAUDE), "mcp", "add-json", "modelbuilder", json.dumps(config), "--scope", "user"]
            )
            report["probes"]["claude_health"] = run([str(CLAUDE), "mcp", "list"])
            (home / "codex/config.toml").write_text(
                "[analytics]\nenabled = false\n[mcp_servers.modelbuilder]\ncommand = "
                + json.dumps(PYTHON)
                + '\nargs = ["-m", "modelbuilder.mcp_server"]\nenv_vars = ["PYTHONPATH", "MODELBUILDER_API_URL", "MODELBUILDER_TOKEN"]\n'
            )
            report["probes"]["codex_configuration"] = run([str(CODEX), "mcp", "list", "--json"])
            codex = subprocess.Popen(
                [str(CODEX), "app-server", "--stdio"],
                env=env,
                cwd=ROOT / ".runtime/gui-workspace",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            selector = selectors.DefaultSelector()
            selector.register(codex.stdout, selectors.EVENT_READ)

            def rpc(request, timeout=60):
                codex.stdin.write(json.dumps(request) + "\n")
                codex.stdin.flush()
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if selector.select(timeout=1):
                        line = codex.stdout.readline()
                        if not line:
                            raise RuntimeError("Vendor app-server closed output")
                        data = json.loads(line)
                        if data.get("id") == request["id"]:
                            return data
                raise TimeoutError("Vendor app-server response not received")

            report["probes"]["codex_initialize"] = rpc(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "modelbuilder-acceptance", "version": "0.1.0"},
                        "capabilities": {"experimentalApi": True},
                    },
                }
            )
            codex.stdin.write('{"method":"initialized"}\n')
            codex.stdin.flush()
            report["probes"]["codex_mcp_inventory"] = rpc(
                {"id": 2, "method": "mcpServerStatus/list", "params": {}}
            )
            # Creating an ephemeral local thread does not submit a model turn.
            thread = rpc(
                {
                    "id": 3,
                    "method": "thread/start",
                    "params": {"cwd": str(ROOT / ".runtime/gui-workspace"), "ephemeral": True},
                }
            )
            report["probes"]["codex_local_thread"] = thread
            if "result" in thread:
                thread_id = thread["result"]["thread"]["id"]
                result = rpc(
                    {
                        "id": 4,
                        "method": "mcpServer/tool/call",
                        "params": {
                            "threadId": thread_id,
                            "server": "modelbuilder",
                            "tool": "project_create",
                            "arguments": {
                                "slug": "vendor-transport-probe",
                                "model_name": "Synthetic MCP acceptance",
                                "idempotency_key": "vendor-transport-project",
                            },
                        },
                    }
                )
                report["probes"]["codex_direct_mcp_project_create"] = result
                report["direct_transport_only"] = (
                    "No turn/start or model prompt was submitted; direct MCP calls are not authenticated agent parity."
                )
        except Exception as error:
            report["probe_error"] = str(error).replace(token, "[synthetic token redacted]")
        finally:
            for process in [codex, api]:
                if process:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print(OUT)


if __name__ == "__main__":
    main()
