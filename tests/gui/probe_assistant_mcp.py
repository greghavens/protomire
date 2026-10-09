"""Probe installed vendor transports without authentication or model inference.

Uses disposable, synthetic local service credentials and isolated vendor homes.
These checks never substitute for authenticated agent CRUD/run/error parity.
"""

import argparse
import json
import os
from pathlib import Path
import secrets
import queue
import sys
import threading
import socket
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
def discover_binary(extensions: Path, extension_id: str, name: str, *, windows: bool | None = None) -> Path:
    """Find one installed native vendor binary; never download or guess versions.

    Multiple versions/architectures are deliberately ambiguous. Use an explicit
    --codex/--claude override rather than choosing an arbitrary installation.
    """
    windows = os.name == "nt" if windows is None else windows
    basename = name + (".exe" if windows else "")
    candidates = []
    if extensions.is_dir():
        for directory in extensions.iterdir():
            package_path = directory / "package.json"
            if not package_path.is_file():
                continue
            try:
                package = json.loads(package_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if f"{package.get('publisher', '')}.{package.get('name', '')}".lower() != extension_id.lower():
                continue
            subtree = directory / ("bin" if name == "codex" else "resources/native-binary")
            candidates.extend(path.resolve() for path in subtree.rglob(basename) if path.is_file())
    candidates = sorted(set(candidates))
    if len(candidates) != 1:
        raise ValueError(f"Expected one installed {extension_id} native {basename}; found {len(candidates)}. "
                         f"Supply --{name} with the reviewed executable path.")
    return candidates[0]


def executable(value: str | None, extensions: Path, extension_id: str, name: str) -> Path:
    path = Path(value).expanduser().resolve() if value else discover_binary(extensions, extension_id, name)
    if not path.is_file():
        raise ValueError(f"Missing {name} executable: {path}")
    return path


def isolated_environment(home: Path, token: str, port: int) -> dict[str, str]:
    # Both POSIX and Windows vendor paths point into the disposable directory.
    env = {key: value for key, value in os.environ.items() if key not in {
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_AUTH_TOKEN",
        "MODELBUILDER_TOKENS_JSON", "ANTHROPIC_AUTH_TOKEN", "CODEX_API_KEY",
    }}
    env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "appdata"),
               LOCALAPPDATA=str(home / "localappdata"), XDG_CONFIG_HOME=str(home / "config"),
               CODEX_HOME=str(home / "codex"), CLAUDE_CONFIG_DIR=str(home / "claude"),
               PYTHONPATH=str(ROOT / "src"), MODELBUILDER_API_URL=f"http://127.0.0.1:{port}",
               MODELBUILDER_HOST="127.0.0.1", MODELBUILDER_PORT=str(port), MODELBUILDER_BACKEND="none",
               MODELBUILDER_TOKEN=token, MODELBUILDER_DB_PATH=str(home / "service.sqlite"))
    for key in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    return env


def stdout_queue(stream):
    """Threaded pipe reader also works on Windows, unlike select() on pipes."""
    messages = queue.Queue()
    def read():
        try:
            for line in stream:
                messages.put(line)
        finally:
            messages.put(None)
    threading.Thread(target=read, daemon=True).start()
    return messages


def default_python(root: Path = ROOT, *, windows: bool | None = None) -> str:
    windows = os.name == "nt" if windows is None else windows
    configured = os.environ.get("MODELBUILDER_GUI_PYTHON")
    candidate = root / ".venv" / ("Scripts/python.exe" if windows else "bin/python")
    return configured or str(candidate if candidate.is_file() else Path(sys.executable))


def main(argv=None):
    runtime = Path(os.environ.get("MODELBUILDER_RUNTIME_DIR", ROOT / ".runtime")).resolve()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extensions-dir", type=Path, default=runtime / "extensions")
    parser.add_argument("--codex", help="Reviewed, installed Codex native executable")
    parser.add_argument("--claude", help="Reviewed, installed Claude native executable")
    parser.add_argument("--python", default=default_python(), help="Python with service dependencies; host .venv or current interpreter")
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/gui/distribution/assistant-mcp-discovery.json")
    args = parser.parse_args(argv)
    codex_binary = executable(args.codex, args.extensions_dir, "openai.chatgpt", "codex")
    claude_binary = executable(args.claude, args.extensions_dir, "anthropic.claude-code", "claude")
    python = str(Path(args.python).expanduser().resolve())
    if not Path(python).is_file():
        parser.error("--python must name an existing interpreter")
    import httpx

    report = {"status": "PARTIAL", "authenticated_agent_parity": "BLOCKED_AUTH", "probes": {}}
    with tempfile.TemporaryDirectory(prefix="modelbuilder-assistant-mcp-") as tmp:
        home = Path(tmp)
        token = secrets.token_hex(32)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = isolated_environment(home, token, port)
        workspace = home / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        api = subprocess.Popen(
            [python, "-m", "modelbuilder.api"],
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
            else:
                raise TimeoutError("Disposable operation service did not become healthy")
            config = {"type": "stdio", "command": python, "args": ["-m", "modelbuilder.mcp_server"]}

            def run(args):
                result = subprocess.run(
                    args,
                    cwd=workspace,
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
                [str(claude_binary), "mcp", "add-json", "modelbuilder", json.dumps(config), "--scope", "user"]
            )
            report["probes"]["claude_health"] = run([str(claude_binary), "mcp", "list"])
            (home / "codex/config.toml").write_text(
                "[analytics]\nenabled = false\n[mcp_servers.modelbuilder]\ncommand = "
                + json.dumps(python)
                + '\nargs = ["-m", "modelbuilder.mcp_server"]\nenv_vars = ["PYTHONPATH", "MODELBUILDER_API_URL", "MODELBUILDER_TOKEN"]\n'
            )
            report["probes"]["codex_configuration"] = run([str(codex_binary), "mcp", "list", "--json"])
            codex = subprocess.Popen(
                [str(codex_binary), "app-server", "--stdio"],
                env=env,
                cwd=workspace,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            messages = stdout_queue(codex.stdout)

            def rpc(request, timeout=60):
                codex.stdin.write(json.dumps(request) + "\n")
                codex.stdin.flush()
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        line = messages.get(timeout=max(0.01, deadline - time.monotonic()))
                    except queue.Empty:
                        break
                    if line is None:
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
                    "params": {"cwd": str(workspace), "ephemeral": True},
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
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
