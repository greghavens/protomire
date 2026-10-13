"""Real offline Git restore probes, including shallow-bundle regression."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "distribution"))
import build_runtime as builder  # noqa: E402
import managed_core as core  # noqa: E402


def git(*args, cwd=None, check=True):
    return subprocess.run([shutil.which("git"), "-c", "user.name=Restore test", "-c",
                           "user.email=restore-test@example.invalid", *args], cwd=cwd, check=check,
                          capture_output=True, text=True,
                          env={**core.service_environment(), "GIT_CONFIG_GLOBAL": os.devnull,
                               "GIT_CONFIG_NOSYSTEM": "1"})


@pytest.fixture(params=[False, True], ids=["complete", "shallow"])
def source_bundle(tmp_path, request):
    original = tmp_path / "original"
    git("init", "--template=", str(original))
    for value in ["first", "second", "third"]:
        (original / "source.txt").write_text(value)
        git("add", ".", cwd=original)
        git("commit", "--no-gpg-sign", "-m", value, cwd=original)
    source = original
    if request.param:
        source = tmp_path / "shallow"
        git("clone", "--depth=1", original.as_uri(), str(source))
    commit = git("rev-parse", "HEAD", cwd=source).stdout.strip()
    git("update-ref", "refs/checkpoints/restore-test", commit, cwd=source)
    git("tag", "-a", "--no-sign", "-m", "Reviewed fixture", "reviewed", cwd=source)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    shutil.copy2(shutil.which("git"), runtime / "git")
    archive = runtime / "source.bundle"
    git("bundle", "create", str(archive), "--all", cwd=source)
    manifest = {"source_bundle": archive.name, "source_commit": commit, "git_executable": "git"}
    if request.param:
        shutil.copyfile(source / ".git/shallow", runtime / "source.bundle.shallow")
        manifest["source_bundle_shallow"] = "source.bundle.shallow"
    return runtime, manifest, source, request.param


def test_bundle_restores_exact_commit_all_refs_and_fsck_without_changing_input(source_bundle, tmp_path):
    runtime, manifest, source, shallow = source_bundle
    original_bundle = (runtime / "source.bundle").read_bytes()
    if shallow:
        # Reproduce the actual packaging defect with native Git, not a mock.
        failed = git("clone", "--no-checkout", str(runtime / "source.bundle"), str(tmp_path / "old-clone"), check=False)
        assert failed.returncode != 0
    state = tmp_path / "state"
    state.mkdir()
    workspace = core.provision_source(runtime, state, manifest)
    assert git("rev-parse", "HEAD", cwd=workspace).stdout.strip() == manifest["source_commit"]
    assert git("show-ref", cwd=workspace).stdout == git("show-ref", cwd=source).stdout
    assert git("fsck", "--full", cwd=workspace).returncode == 0
    assert git("rev-parse", "--is-shallow-repository", cwd=workspace).stdout.strip() == str(shallow).lower()
    assert (workspace / "source.txt").read_text() == "third"
    assert (runtime / "source.bundle").read_bytes() == original_bundle
    assert not list(state.glob(".source-*"))
    # A restart/update must preserve edits, untracked files, refs and HEAD.
    (workspace / "source.txt").write_text("user edits")
    (workspace / "user-only.txt").write_text("keep me")
    before = git("show-ref", cwd=workspace).stdout
    assert core.provision_source(runtime, state, {**manifest, "source_commit": "b" * 40}) == workspace
    assert (workspace / "source.txt").read_text() == "user edits"
    assert (workspace / "user-only.txt").read_text() == "keep me"
    assert git("show-ref", cwd=workspace).stdout == before


def test_bad_source_identity_fails_without_leaving_partial_workspace(source_bundle, tmp_path):
    runtime, manifest, _, _ = source_bundle
    state = tmp_path / "state"
    state.mkdir()
    with pytest.raises(core.CoreError, match="initialize"):
        core.provision_source(runtime, state, {**manifest, "source_commit": "f" * 40})
    assert not list(state.iterdir())


def test_missing_shallow_sidecar_fails_closed(source_bundle, tmp_path):
    runtime, manifest, _, shallow = source_bundle
    if not shallow:
        return
    state = tmp_path / "state"
    state.mkdir()
    manifest.pop("source_bundle_shallow")
    with pytest.raises(core.CoreError, match="initialize"):
        core.provision_source(runtime, state, manifest)
    assert not list(state.iterdir())


@pytest.mark.parametrize("boundaries", ["", "not-a-commit\n", "a" * 40 + "\n" + "a" * 40 + "\n", "f" * 40 + "\n"])
def test_invalid_shallow_boundaries_are_rejected_and_cleaned(source_bundle, tmp_path, boundaries):
    runtime, manifest, _, _ = source_bundle
    (runtime / "source.bundle.shallow").write_text(boundaries)
    state = tmp_path / "state"
    state.mkdir()
    with pytest.raises(core.CoreError, match="initialize"):
        core.provision_source(runtime, state, {**manifest, "source_bundle_shallow": "source.bundle.shallow"})
    assert not list(state.iterdir())


def test_restore_refuses_existing_destination_without_touching_it(tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "user.txt").write_text("preserve")
    with pytest.raises(core.CoreError, match="new destination"):
        core.restore_source(Path(shutil.which("git")), tmp_path / "absent.bundle", destination, "a" * 40)
    assert (destination / "user.txt").read_text() == "preserve"


def test_builder_checks_shallow_sidecar_hash_before_creating_output(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "detect_target", lambda: "linux-x64")
    file = tmp_path / "input"
    file.write_text("inert input")
    item = {"path": str(file), "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    inputs = {"target": "linux-x64", "service": {"python": item, "wheel": item},
              "backend": {"python": item, "wheel": item}, "git": item, "source_bundle": item,
              "source_bundle_shallow": {**item, "sha256": "0" * 64}}
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(inputs))
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="SHA256"):
        builder.build_runtime(path, output)
    assert not output.exists()
