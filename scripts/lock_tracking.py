"""Generate optional W&B locks without changing service/backend environments."""
from pathlib import Path
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {"linux-x64": "x86_64-unknown-linux-gnu", "linux-arm64": "aarch64-unknown-linux-gnu",
           "darwin-arm64": "aarch64-apple-darwin"}


def main():
    if subprocess.check_output(["uv", "--version"], text=True).split()[1] != "0.12.23":
        raise SystemExit("Lock generation requires uv 0.12.23")
    env = {**os.environ, "UV_CACHE_DIR": str(ROOT / ".cache/uv")}
    for target, platform in TARGETS.items():
        subprocess.run(["uv", "pip", "compile", "requirements/tracking-wandb.in", "-c", "requirements/backend.constraints",
                        "--python-version", "3.12.14", "--python-platform", platform, "--generate-hashes", "--no-annotate",
                        "--custom-compile-command", "python scripts/lock_tracking.py",
                        "-o", f"requirements/profiles/tracking-wandb-{target}.lock"],
                       cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
