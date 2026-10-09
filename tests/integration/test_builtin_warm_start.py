"""Real shared-service/native ZenML regression for edited built-in warm starts.

Three native CPU runs establish the service recipe selection, actual optimizer
behavior, and immutable history. Direct deterministic calculations are additional
tensor-level references, never substitutes for the native execution assertions.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import UUID

import pytest

torch = pytest.importorskip("torch", reason="Native warm-start acceptance requires .venv-backend")

from modelbuilder.backend import ZenMLBackend, fixtures  # noqa: E402
from modelbuilder.backend.common import read_json  # noqa: E402
from modelbuilder.models import Actor  # noqa: E402
from modelbuilder.service import OperationService  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BACKEND_PYTHON = Path(os.environ.get("MODELBUILDER_BACKEND_PYTHON", str(ROOT / ".venv-backend/bin/python")))
pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not BACKEND_PYTHON.is_file(), reason="Pinned backend environment is not installed")]


def parameters(recipe, kind="model.train"):
    return next(node["parameters"] for node in recipe["nodes"] if node["kind"] == kind)


def equal_state(left, right):
    if isinstance(left, torch.Tensor):
        return isinstance(right, torch.Tensor) and torch.equal(left, right)
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(equal_state(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(equal_state(a, b) for a, b in zip(left, right))
    return left == right


def assert_training_state(actual, expected):
    for field in ("model", "reference", "optimizer", "scheduler", "torch_rng", "python_rng",
                  "order", "cursor", "epoch", "history", "training_contract", "step"):
        assert equal_state(actual[field], expected[field]), field


def committed_fixture_recipe(tmp_path):
    """Pin only this workload and its dependency lock in an isolated real repo."""
    source = tmp_path / "fixture-source"
    manifest = json.loads((ROOT / "examples/custom-pytorch/recipe.json").read_text())
    files = []
    for relative in ("src/modelbuilder/backend/fixtures.py", "requirements/backend.lock"):
        content = (ROOT / relative).read_bytes()
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        files.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest()})
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Acceptance",
        "-c", "user.email=acceptance@example.invalid", "commit", "-qm", "Native CPU regression fixture"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    manifest["source"] = {"repository": "https://example.invalid/native-cpu-fixture", "commit": commit, "files": files}
    manifest["environment"]["lock_file"] = files[1]
    manifest.pop("digest", None)
    parameters(manifest)["steps"] = 4
    return source, manifest


def test_native_builtin_warm_start_uses_current_settings_exact_state_keeps_parent(tmp_path):
    actor = Actor("native-warm-start-owner", {"read", "modify", "execute"})
    database = tmp_path / "service.sqlite3"
    source, manifest = committed_fixture_recipe(tmp_path)
    backend = ZenMLBackend(tmp_path, python_executable=str(BACKEND_PYTHON), source_root=source)
    service = OperationService(database, backend)

    def invoke(operation, payload):
        return service.invoke(operation, payload, actor)

    def completed(submission):
        query = {"project_id": submission["project_id"], "operation_id": submission["operation_id"]}
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            result = invoke("run.status", query)
            if result["status"] in {"succeeded", "failed", "cancelled"}:
                assert result["status"] == "succeeded", result
                if result["worker_alive"] is not False:
                    time.sleep(0.25)
                    continue
                assert result["backend_status"] == "completed", result
                assert result["backend"] == "zenml" and result["backend_version"] == "0.97.0"
                for field in ("backend_run_id", "backend_snapshot_id", "model_version_ref"):
                    assert str(UUID(result[field])) == result[field], field
                job = backend.root / "jobs" / result["operation_id"]
                assert read_json(job / "native.json")["backend_run_id"] == result["backend_run_id"]
                return result
            time.sleep(0.25)
        invoke("run.cancel", {**query, "idempotency_key": "timeout-" + submission["operation_id"]})
        pytest.fail(f"Native CPU run exceeded the test deadline: {result}")

    def checkpoint(run):
        reference = invoke("model.checkpoint.get", {
            "project_id": run["project_id"], "operation_id": run["operation_id"], "checkpoint_kind": "last"})
        assert str(UUID(reference["artifact_id"])) == reference["artifact_id"]
        assert reference["backend_run_id"] == run["backend_run_id"]
        return reference, fixtures.load_checkpoint(Path(reference["path"]))

    try:
        project = invoke("project.create", {"slug": "native-builtin-warm-start",
            "model_name": "Native built-in warm start", "idempotency_key": "project"})
        scope = {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}
        # Admission verifies the isolated workload/lock closure and real Git pin.
        # Unrelated source or shipping-example changes cannot invalidate this test.
        parent_recipe = invoke("recipe.create", {"project_id": project["project_id"], "recipe": manifest,
            "expected_revision": 1, "idempotency_key": "parent-recipe"})
        snapshots = {parent_recipe["revision"]: deepcopy(parent_recipe)}
        plan = invoke("run.plan", {**scope, "trusted": True})
        assert plan["executable"], plan
        parent = completed(invoke("run.submit", {**scope, "expected_revision": parent_recipe["revision"],
            "trusted": True, "idempotency_key": "parent-run"}))
        parent_ref, parent_state = checkpoint(parent)
        assert parent_state["step"] == 4
        assert parent_state["start_step"] == 0 and parent_state["resume_mode"] is None
        parent_path = Path(parent_ref["path"])
        parent_checkpoint_digest = hashlib.sha256((parent_path / "checkpoint.pt").read_bytes()).hexdigest()
        requests = {parent["operation_id"]: (
            backend.root / "jobs" / parent["operation_id"] / "request.json").read_bytes()}

        edited = deepcopy(parent_recipe)
        parameters(edited).update(steps=8, learning_rate=0.004, batch_size=20, scheduler_gamma=0.8)
        parameters(edited)["kl"].update(enabled=True, coefficient=0.35, direction="model_to_reference")
        edited_recipe = invoke("recipe.update", {**scope, "recipe": edited,
            "expected_revision": parent_recipe["revision"], "idempotency_key": "edit-hyperparameters"})
        snapshots[edited_recipe["revision"]] = deepcopy(edited_recipe)
        edited_contract = {"learning_rate": 0.004, "batch_size": 20, "scheduler_gamma": 0.8,
                           "coefficient": 0.35, "direction": "model_to_reference"}
        assert parent_state["training_contract"] != edited_contract

        resume = {"project_id": project["project_id"], "operation_id": parent["operation_id"],
                  "checkpoint_ref": parent_ref["artifact_id"], "checkpoint_kind": "last", "trusted": True}
        warm = completed(invoke("run.resume", {**resume, "mode": "warm_start", "steps": 6,
            "expected_recipe_revision": edited_recipe["revision"], "idempotency_key": "warm-start"}))
        warm_recipe = invoke("recipe.get", scope)
        snapshots[warm_recipe["revision"]] = deepcopy(warm_recipe)
        assert parameters(warm_recipe) == {**parameters(edited_recipe), "steps": 6}
        warm_ref, warm_state = checkpoint(warm)
        assert warm_state["training_contract"] == edited_contract
        assert warm_state["resume_mode"] == "warm_start" and warm_state["start_step"] == 0
        assert warm_state["step"] == 6 and len(warm_state["history"]) == 6
        assert warm_state["optimizer"]["param_groups"][0]["lr"] == pytest.approx(0.004 * 0.8 ** 6)
        assert warm_state["scheduler"]["base_lrs"] == [0.004]
        assert warm_state["scheduler"]["gamma"] == 0.8
        requests[warm["operation_id"]] = (
            backend.root / "jobs" / warm["operation_id"] / "request.json").read_bytes()

        # The canonical current recipe still contains the edited settings. Exact
        # continuation of the original parent must deliberately ignore those edits.
        exact = completed(invoke("run.resume", {**resume, "mode": "exact_state", "steps": 7,
            "expected_recipe_revision": warm_recipe["revision"], "idempotency_key": "exact-state"}))
        exact_recipe = invoke("recipe.get", scope)
        snapshots[exact_recipe["revision"]] = deepcopy(exact_recipe)
        assert parameters(exact_recipe) == {**parameters(parent_recipe), "steps": 7}
        exact_ref, exact_state = checkpoint(exact)
        assert exact_state["training_contract"] == parent_state["training_contract"]
        assert exact_state["resume_mode"] == "exact_state" and exact_state["start_step"] == 4
        assert exact_state["step"] == 7 and exact_state["history"][:4] == parent_state["history"]
        old_parameters = parameters(parent_recipe)
        assert exact_state["optimizer"]["param_groups"][0]["lr"] == pytest.approx(
            old_parameters["learning_rate"] * old_parameters["scheduler_gamma"] ** 7)

        # Prove the executed tensors, not merely recipe/checkpoint metadata, used
        # the edited settings and the existing parent model. The stale-settings
        # reference reproduces the inherited bug and must produce different weights.
        data = fixtures.dataset(parameters(parent_recipe, "dataset.synthetic"))
        initial = fixtures.initialize(data, parameters(parent_recipe, "model.initialize"), tmp_path / "reference-initial")
        warm_expected = fixtures.load_checkpoint(fixtures.train(data, initial,
            {**parameters(warm_recipe), "resume_mode": "warm_start"}, tmp_path / "reference-warm", resume=parent_path))
        stale_warm = fixtures.load_checkpoint(fixtures.train(data, initial,
            {**old_parameters, "steps": 6, "resume_mode": "warm_start"}, tmp_path / "reference-stale", resume=parent_path))
        uninterrupted = fixtures.load_checkpoint(fixtures.train(data, initial,
            {**old_parameters, "steps": 7}, tmp_path / "reference-uninterrupted"))
        assert_training_state(warm_state, warm_expected)
        assert not equal_state(warm_state["model"], stale_warm["model"])
        assert_training_state(exact_state, uninterrupted)

        assert len({run["backend_run_id"] for run in (parent, warm, exact)}) == 3
        assert len({run["backend_snapshot_id"] for run in (parent, warm, exact)}) == 3
        assert len({run["recipe_digest"] for run in (parent, warm, exact)}) == 3
        assert [recipe["revision"] for recipe in snapshots.values()] == [2, 3, 4, 5]
        for run in (warm, exact):
            assert run["recipe_id"] == parent["recipe_id"]
            assert run["parent_run_id"] == parent["backend_run_id"]
            assert run["parent_checkpoint_ref"] == parent_ref["artifact_id"]
            assert run["parent_recipe_revision"] == parent_recipe["revision"]
            assert run["parent_recipe_digest"] == parent_recipe["digest"]
        for operation_id, original_bytes in requests.items():
            assert (backend.root / "jobs" / operation_id / "request.json").read_bytes() == original_bytes
        assert hashlib.sha256((parent_path / "checkpoint.pt").read_bytes()).hexdigest() == parent_checkpoint_digest
        assert_training_state(fixtures.load_checkpoint(parent_path), parent_state)

        # Historical recipe equality must survive a real application-store reopen.
        service.store.close()
        service = OperationService(database, backend)
        for revision, original_recipe in snapshots.items():
            assert invoke("recipe.get", {**scope, "revision": revision}) == original_recipe
        assert invoke("recipe.get", scope) == exact_recipe
        historical_parent = invoke("run.status", {
            "project_id": project["project_id"], "operation_id": parent["operation_id"]})
        for field in ("recipe_revision", "recipe_digest", "backend_snapshot_id", "backend_run_id"):
            assert historical_parent[field] == parent[field]

        evidence = {"status": "passed", "source_commit": manifest["source"]["commit"],
            "backend_run_ids": [run["backend_run_id"] for run in (parent, warm, exact)],
            "backend_snapshot_ids": [run["backend_snapshot_id"] for run in (parent, warm, exact)],
            "checkpoint_artifact_ids": [ref["artifact_id"] for ref in (parent_ref, warm_ref, exact_ref)],
            "recipe_id": parent["recipe_id"], "immutable_revisions": list(snapshots),
            "parent_contract": parent_state["training_contract"], "warm_contract": warm_state["training_contract"],
            "exact_contract": exact_state["training_contract"], "warm_tensor_parity": True,
            "stale_warm_tensors_differ": True, "exact_uninterrupted_state_parity": True,
            "prior_recipe_snapshots_and_parent_checkpoint_unchanged": True}
        (tmp_path / "native-builtin-warm-start-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    finally:
        service.store.close()
