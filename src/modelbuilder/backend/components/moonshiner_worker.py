"""Narrow offline installed Moonshiner CLI entry, run only by the native adapter.

Python audit guards are defense in depth for reviewed code, not a hostile-code
sandbox. No Moonshiner code is imported by catalog inspection or planning.
"""
from __future__ import annotations

import sys


def offline_guard(event, args):
    # Upstream privacy checks compare against the local host name; this does
    # not create a socket or perform DNS/network I/O. Everything else remains blocked.
    if ((event.startswith("socket.") and event != "socket.gethostname") or event in {"subprocess.Popen", "os.system", "os.posix_spawn",
            "os.posix_spawnp", "os.fork", "os.forkpty", "os.exec", "pty.spawn"}):
        raise PermissionError("Moonshiner offline compose denies network and child commands: " + event)


def main():
    argv = sys.argv[1:]
    if (len(argv) != 8 or argv[:3] != ["dataset", "compose", "--source"]
            or argv[4] != "--out" or argv[6] != "--seed"):
        raise ValueError("Only the fixed Moonshiner local compose command is admitted")
    from pathlib import Path
    root = Path.cwd().resolve()
    if (Path(argv[3]).resolve() != root / "accepted-traces.jsonl"
            or Path(argv[5]).resolve() != root / "composed.jsonl"
            or not argv[7].isdigit()):
        raise ValueError("Moonshiner inputs and outputs must remain in the native private attempt")
    sys.addaudithook(offline_guard)
    from moonshiner_app.cli import main as cli
    return cli()


if __name__ == "__main__":
    raise SystemExit(main())
