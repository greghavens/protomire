#!/usr/bin/env python3
"""Offline synthetic imported-snapshot CPU replay in new isolated directories.

Copies existing pinned dependency installations (hardlinks when available),
installs locally built non-editable wheels, and uses only public review APIs.
Does not alter the dependency templates, shipping manifests or any user policies.
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path)
    parser.add_argument("--service-dependencies", type=Path, required=True)
    parser.add_argument("--backend-dependencies", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    work = args.work.absolute()
    if work.exists() or work.is_symlink():
        parser.error("Use a new absolute work directory; previous receipts are never overwritten")
    work.mkdir(parents=True)
    evidence, logs = work / "evidence", work / "logs"
    evidence.mkdir()
    logs.mkdir()
    spec = importlib.util.spec_from_file_location("snapshot_replay", root / "scripts/replay_bundle.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    environment = dict(os.environ)
    for key in list(environment):
        if key in {"PYTHONPATH", "VIRTUAL_ENV"} or any(value in key for value in ("API_KEY", "ACCESS_TOKEN", "AUTH_TOKEN", "SECRET")):
            environment.pop(key)
    environment.update(PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1",
                       HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", ZENML_ANALYTICS_OPT_IN="false", WANDB_MODE="disabled")
    receipts = []

    def write(name, value):
        (evidence / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")

    def command(name, argv):
        started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with (logs / (name + ".log")).open("x") as log:
            result = subprocess.run([str(arg) for arg in argv], cwd=work, env=environment, stdout=log, stderr=subprocess.STDOUT)
        receipts.append({"step": name, "command": [str(arg) for arg in argv], "cwd": str(work), "returncode": result.returncode,
                         "started_at": started, "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                         "log_sha256": hashlib.sha256((logs / (name + ".log")).read_bytes()).hexdigest()})
        write("commands.json", receipts)
        print(name, result.returncode, flush=True)
        if result.returncode:
            raise RuntimeError(f"{name} failed; inspect its retained log")

    author = work / "author-source"
    author.mkdir()
    inventory = replay.source_inventory(root)
    for name in inventory:
        target = author / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    (author / "TERMS.txt").write_text("Synthetic fixture terms: train, evaluate and export these self-authored deterministic fixture bytes in isolated tests only. This test review is not user or third-party legal approval.\n")
    command("git-init", ["git", "init", "-q", author])
    command("git-add", ["git", "-C", author, "add", "."])
    command("git-commit", ["git", "-C", author, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid",
                           "commit", "-qm", "Isolated snapshot-rights test source"])
    commit = subprocess.check_output(["git", "-C", str(author), "rev-parse", "HEAD"], text=True).strip()
    from modelbuilder.recipe import export_bundle, import_bundle, pin_source_files
    recipe = json.loads((root / "examples/custom-pytorch/recipe.json").read_text())
    recipe = pin_source_files(recipe, author, inventory + ["TERMS.txt"], commit)
    archive = work / "synthetic.recipe.tar"
    bundle = export_bundle(recipe, archive, source_root=author)
    write("export.json", {"source_commit": commit, "source_files": len(bundle["files"]), "bundle_digest": bundle["bundle_digest"],
                          "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "synthetic_fixture_only": True})

    def link_or_copy(source, target):
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)

    runs = []
    for role in ("origin", "replay"):
        source = work / f"{role}-source"
        import_bundle(archive, source)
        for kind in ("service", "backend"):
            venv = work / f"{role}-{kind}"
            command(f"{role}-{kind}-venv", [sys.executable, "-m", "venv", "--without-pip", venv])
            template = getattr(args, kind + "_dependencies").resolve() / "lib/python3.12/site-packages"
            if not template.is_dir():
                raise ValueError("Dependency templates must be preinstalled Python 3.12 Linux environments")
            shutil.copytree(template, venv / "lib/python3.12/site-packages", dirs_exist_ok=True,
                            symlinks=True, copy_function=link_or_copy)
            wheel = replay.build_wheel(source, work / f"{role}-wheels", kind)
            write(f"{role}-{kind}-wheel.json", wheel)
            command(f"{role}-{kind}-install", ["uv", "pip", "install", "--offline", "--no-cache", "--python", venv / "bin/python",
                                              "--no-deps", "--reinstall", wheel["wheel"]])
            command(f"{role}-{kind}-check", ["uv", "pip", "check", "--no-cache", "--python", venv / "bin/python"])
        base = [work / f"{role}-service/bin/python", "-I", "-B", source / "scripts/replay_bundle.py"]
        prepared = evidence / f"{role}-prepared.json"
        command(f"{role}-prepare", base + ["prepare", source, "--data-dir", work / f"{role}-data", "--backend-python",
                                            work / f"{role}-backend/bin/python", "--trust-repository-source", "--output", prepared])
        initial = json.loads(prepared.read_text())
        assert initial["status"] == "BLOCKED" and initial["submitted"] is False
        command(f"{role}-review", [work / f"{role}-service/bin/python", "-I", "-B", root / "tests/portability/snapshot_review_fixture.py",
                                   prepared, evidence / f"{role}-review.json", "--confirm-synthetic-fixture"])
        plan_path = evidence / f"{role}-plan.json"
        command(f"{role}-plan", base + ["plan", prepared, "--trust-repository-source", "--output", plan_path])
        plan = json.loads(plan_path.read_text())
        assert plan["status"] == "PREPARED", plan["plan"]
        output = evidence / f"{role}-run.json"
        command(f"{role}-run", base + ["run-prepared", prepared, "--expected-revision", str(plan["execution_revision"]),
                                       "--expected-digest", plan["execution_digest"], "--trust-repository-source", "--output", output])
        runs.append(json.loads(output.read_text()))
    left, right = runs
    comparison = replay.compare_results(left, right)
    write("strict-comparison.json", comparison)
    matching = [name for name in left["output_sha256"] if left["output_sha256"][name] == right["output_sha256"].get(name)]
    different = [name for name in left["output_sha256"] if name not in matching]
    expected_differences = {"initial/checkpoint.manifest.json", "train/checkpoint.manifest.json", "export/export.manifest.json"}
    assert len(matching) == 7 and set(different) == expected_differences
    assert [name for name, value in comparison["checks"].items() if not value] == ["output_hashes_equal"]
    assert all(run["status"] == "PASS" and not run["run"]["worker_alive"] for run in runs)
    write("summary.json", {"status": "PASS", "scope": "Synthetic same-host CPU imported-snapshot review; not shipping/native installer acceptance",
        "source_commit": commit, "source_files": len(bundle["files"]), "actual_git_free_runs": 2,
        "scientific_outputs_matching": matching, "independent_review_provenance_differences": different,
        "strict_byte_comparison_status": comparison["status"], "strict_failed_checks": ["output_hashes_equal"],
        "single_owned_recipe_identity": all(replay.valid_recipe_identity(run["recipe_identity"]) for run in runs),
        "installed_source_verified": all(run["installed_source_verified"] for run in runs), "worker_alive": [False, False]})
    print(evidence / "summary.json")


if __name__ == "__main__":
    main()
