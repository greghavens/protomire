"""Run RSS fixtures below a clean, stdlib-only parent, never the pytest heap.

On Linux, getrusage's high-water RSS survives exec. A direct subprocess of a
large pytest process can therefore report pytest's peak rather than its own.
The intermediate interpreter has a small current address space when it creates
the measured child. No peak is subtracted, reset, sampled, or capped.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys


def run_with_clean_parent(command, *, env=None, timeout=120):
    if os.name != "posix":
        raise RuntimeError("RSS fixture containment requires POSIX process groups")
    launcher = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), *command]
    with subprocess.Popen(launcher, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            # The measured worker inherits the launcher's group. Killing only
            # the launcher on timeout would leave that worker running.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=5)
            raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


if __name__ == "__main__":
    # Keep this interpreter free of pytest/product imports and their heaps.
    raise SystemExit(subprocess.run(sys.argv[1:]).returncode)
