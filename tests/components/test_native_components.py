"""Actual independent image/forecast training, evaluation, export under ZenML."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
pytest.importorskip("torch")
pytest.importorskip("zenml")
from modelbuilder.backend.components import read_checkpoint, read_export
from modelbuilder.backend.common import write_json


def make_node(name, role, inputs, outputs, parameters, reference):
    return {"id": name, "kind": "component.run", "implementation_ref": {**reference, "entrypoint": name},
            "inputs": inputs, "outputs": outputs, "parameters": parameters, "timeout": 120.0,
            "retry": {"max_attempts": 1}, "component": {"id": "independent." + name, "version": "1",
            "protocol": "modelbuilder.component/v1", "role": role, "parameter_schema": {"type": "object"},
            "capabilities": {"checkpoint_events": role == "training", "resume_modes": ["exact_state", "warm_start"] if role == "training" else [], "frameworks": ["pytorch"], "export_formats": ["pytorch-state-dict"] if role == "export" else []}}}


@pytest.mark.integration
@pytest.mark.parametrize("task,width,live", [("image", 3, False), ("image", 5, False), ("forecast", 4, False), ("image", 3, True)])
def test_native_independent_harness(tmp_path, task, width, live):
    source = tmp_path / "independent-repository"
    source.mkdir()
    shutil.copy2(Path(__file__).with_name("independent_harness.py"), source / "harness.py")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Acceptance", "-c", "user.email=acceptance@example.invalid", "commit", "-qm", "Independent harness"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    digest = hashlib.sha256((source / "harness.py").read_bytes()).hexdigest()
    reference = {"path": "harness.py", "sha256": digest, "module": "independent_harness", "commit": commit}
    trained = {"name": "trained", "type": "Path"}
    nodes = [make_node("train", "training", [], [trained], {"architecture": {"task": task, "width": width, "loss_weight": 0.6}, "learning_rate": 0.01, "steps": 4, "step_delay": 3 if live else 0}, reference),
             make_node("evaluate", "evaluation", [trained], [{"name": "metrics", "type": "dict"}], {}, reference),
             make_node("export", "export", [trained], [{"name": "exported", "type": "Path"}], {}, reference)]
    recipe = {"recipe_id": "independent", "revision": 1, "nodes": nodes,
              "evaluation": {"live": {"enabled": live, "max_concurrency": 1, "timeout_seconds": 30}},
              "source": {"commit": commit, "files": [{"path": "harness.py", "sha256": digest}]},
              "edges": [{"source_node": "train", "source_port": "trained", "target_node": name, "target_port": "trained"} for name in ("evaluate", "export")]}
    from tests.integration.native_fixture_support import synthetic_review
    from tests.components.rights_test_support import authorize_test_recipe
    project_id = "independent-" + task
    review = synthetic_review(recipe, project_id, source)
    authorization = authorize_test_recipe(tmp_path, recipe, {review["policy_id"]: review}, project_id=project_id)
    job = tmp_path / "job"
    write_json(job / "request.json", {**authorization, "recipe": recipe, "project_id": "independent-" + task, "operation_id": "test-" + str(width),
               "run_name": "independent-" + task + "-" + str(width), "source_root": str(source), "trusted_code_roots": [str(source)]})
    env = {**os.environ, "ZENML_CONFIG_PATH": str(tmp_path / "zenml-config"), "ZENML_LOCAL_STORES_PATH": str(tmp_path / "zenml-stores"),
           "ZENML_ANALYTICS_OPT_IN": "false", "ZENML_LOGGING_VERBOSITY": "ERROR", "OMP_NUM_THREADS": "1"}
    result = subprocess.run([sys.executable, "-m", "modelbuilder.backend.worker", str(job)], env=env, capture_output=True, text=True, timeout=180)
    (tmp_path / "worker.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, (result.stdout + result.stderr)[-12000:]
    checkpoint = read_checkpoint(job / "outputs" / "train")
    assert checkpoint["metrics"]["parameter_change"] > 0
    assert len(checkpoint["history"]) == 4
    exported = read_export(job / "outputs" / "export")
    assert exported["verification"]["fresh_process"]
    assert exported["verification"]["max_abs_difference"] == 0
    assert (job / "native.json").exists()
    if live:
        from modelbuilder.backend.live_evaluation import list_evaluations, list_events
        evaluations = list_evaluations(job)["evaluations"]
        assert len(evaluations) == 4 and all(item["status"] == "completed" for item in evaluations), evaluations
        events = list_events(job)["events"]
        completed = [event for event in events if event["kind"] == "evaluation.completed"]
        last_checkpoint = next(event for event in events if event["kind"] == "checkpoint.ready" and event["step"] == 4)
        assert completed[0]["created_at"] < last_checkpoint["created_at"]



def test_independent_exact_continuation_and_custom_loss_effect(tmp_path):
    import importlib.util
    import torch
    from modelbuilder.backend.components import validate_resume
    module_spec = importlib.util.spec_from_file_location("independent_model", Path(__file__).with_name("independent_harness.py"))
    harness = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(harness)
    parameters = {"architecture": {"task": "image", "width": 3, "loss_weight": 0.6}, "learning_rate": 0.01, "steps": 4}
    identities = {key: key for key in ("architecture", "source", "dependencies", "training", "environment")}
    def context(name, **extra):
        return {"output_dir": str(tmp_path / name), "identities": identities, **extra}
    def stop(**kwargs):
        if kwargs["step"] == 2:
            raise InterruptedError("Simulated process stop after completed update")
    harness.train(inputs={}, parameters=parameters, context=context("full"))
    with pytest.raises(InterruptedError):
        harness.train(inputs={}, parameters=parameters, context=context("partial", checkpoint_ready=stop))
    descriptor = validate_resume(tmp_path / "partial", "exact_state", identities, {"resume_modes": ["exact_state"]})
    harness.train(inputs={}, parameters=parameters, context=context("resumed", resume={"path": str(tmp_path / "partial"), "descriptor": descriptor}, resume_mode="exact_state"))
    def equal(a, b):
        if isinstance(a, torch.Tensor):
            return torch.equal(a, b)
        if isinstance(a, dict):
            return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
        if isinstance(a, list):
            return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
        return a == b
    full = torch.load(tmp_path / "full/complete-state.bin", weights_only=True)
    resumed = torch.load(tmp_path / "resumed/complete-state.bin", weights_only=True)
    assert equal(full, resumed)
    harness.train(inputs={}, parameters={**parameters, "architecture": {**parameters["architecture"], "loss_weight": 0.0}}, context=context("other_loss"))
    other = torch.load(tmp_path / "other_loss/complete-state.bin", weights_only=True)
    assert not equal(full["model"], other["model"])
    assert full["history"] != other["history"]


@pytest.mark.integration
@pytest.mark.parametrize("interrupt_mid_checkpoint", [False, True])
def test_service_component_cancel_recover_resume_export(tmp_path, interrupt_mid_checkpoint):
    import time
    import uuid
    from modelbuilder.backend import ZenMLBackend
    from modelbuilder.models import Actor
    from modelbuilder.service import OperationService
    source = tmp_path / "user-source"
    source.mkdir()
    shutil.copy2(Path(__file__).with_name("independent_harness.py"), source / "harness.py")
    if interrupt_mid_checkpoint:
        # Pause after corrupting the mutable next-step weights, before its new
        # manifest. A valid prior immutable step must survive actual SIGTERM.
        harness_file = source / "harness.py"
        needle = '        torch.save(model.state_dict(), output / "inference-tensors.bin")'
        injected = needle + ('\n        if i == 1 and context.get("job_dir") and not context.get("resume"):\n'
            '            (Path(context["job_dir"]) / "mid-checkpoint-write").write_text("ready")\n'
            '            while True:\n'
            '                time.sleep(0.1)')
        assert needle in harness_file.read_text()
        harness_file.write_text(harness_file.read_text().replace(needle, injected))
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "User owned image harness"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    digest = hashlib.sha256((source / "harness.py").read_bytes()).hexdigest()
    reference = {"path": "harness.py", "sha256": digest, "module": "independent_harness", "commit": commit}
    trained = {"name": "trained", "type": "Path"}
    nodes = [make_node("train", "training", [], [trained], {"architecture": {"task": "image", "width": 3, "loss_weight": 0.6}, "learning_rate": 0.01, "steps": 8, "step_delay": 1.0}, reference),
             make_node("evaluate", "evaluation", [trained], [{"name": "metrics", "type": "dict"}], {}, reference),
             make_node("export", "export", [trained], [{"name": "exported", "type": "Path"}], {}, reference)]
    for node in nodes:
        node.update(environment_ref="default", resource_request={"gpu": 0}, cache_policy="disabled", provenance={})
        node["retry"]["backoff_seconds"] = 0
    recipe = {"schema_version": "0", "recipe_id": str(uuid.uuid4()), "revision": 1, "project_id": "pending", "name": "User segmentation",
              "source": {"repository": "urn:sha256:" + digest, "commit": commit, "files": [{"path": "harness.py", "sha256": digest}]},
              "nodes": nodes, "edges": [{"source_node": "train", "source_port": "trained", "target_node": name, "target_port": "trained"} for name in ("evaluate", "export")],
              "resources": [], "external_dependencies": [], "environment": {"python": "3.12", "requirements": ["zenml==0.97.0"]}, "evaluation": {}, "outputs": []}
    from tests.integration.native_fixture_support import synthetic_review, register_project_review
    review = synthetic_review(recipe, "pending", source)
    backend = ZenMLBackend(tmp_path / "state", python_executable=sys.executable, source_root=source, trusted_code_roots=[str(source)])
    service = OperationService(tmp_path / "service.sqlite3", backend=backend, source_root=source)
    actor = Actor("owner", {"read", "modify", "execute"})
    project = service.invoke("project.create", {"slug": "segmentation", "model_name": "User model", "recipe": recipe, "idempotency_key": "create"}, actor)
    register_project_review(service, review, project["project_id"])
    request = {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}
    plan = service.invoke("run.plan", {**request, "trusted": True}, actor)
    assert plan["executable"], plan
    submitted = service.invoke("run.submit", {**request, "expected_revision": 1, "trusted": True, "idempotency_key": "submit"}, actor)
    operation = {"project_id": project["project_id"], "operation_id": submitted["operation_id"]}
    job = backend.root / "jobs" / submitted["operation_id"]
    checkpoint_path = (job / "mid-checkpoint-write" if interrupt_mid_checkpoint else
                       job / "outputs/train/checkpoints/00000001/checkpoint.manifest.json")
    deadline = time.monotonic() + 90
    while not checkpoint_path.exists() and time.monotonic() < deadline:
        time.sleep(.1)
    assert checkpoint_path.exists(), (job / "worker.log").read_text()
    cancelled = service.invoke("run.cancel", {**operation, "idempotency_key": "cancel"}, actor)
    assert cancelled["status"] == "cancelled", cancelled
    if interrupt_mid_checkpoint:
        with pytest.raises(ValueError, match="integrity mismatch"):
            read_checkpoint(job / "outputs/train")
    recovered = service.invoke("model.checkpoint.get", operation, actor)
    if interrupt_mid_checkpoint:
        assert recovered["descriptor"]["step"] == 1
    assert recovered["descriptor"]["step"] < 8
    resumed = service.invoke("run.resume", {**operation, "mode": "exact_state", "trusted": True, "expected_recipe_revision": 1, "idempotency_key": "resume"}, actor)
    resumed_operation = {**operation, "operation_id": resumed["operation_id"]}
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status = service.invoke("run.status", resumed_operation, actor)
        if status["status"] in {"failed", "cancelled", "succeeded"}:
            break
        time.sleep(.25)
    assert status["status"] == "succeeded", status
    final = service.invoke("model.checkpoint.get", resumed_operation, actor)
    assert final["descriptor"]["step"] == 8
    # Compare actual process-kill/resume result against uninterrupted execution
    # of the same independent harness, including all framework-owned state.
    import importlib.util
    import torch
    module_spec = importlib.util.spec_from_file_location("uninterrupted_user_harness", source / "harness.py")
    harness = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(harness)
    baseline = tmp_path / "uninterrupted"
    harness.train(inputs={}, parameters=nodes[0]["parameters"],
                  context={"output_dir": str(baseline), "identities": final["descriptor"]["identities"]})
    uninterrupted = torch.load(baseline / "complete-state.bin", weights_only=True)
    continued = torch.load(Path(final["path"]) / "complete-state.bin", weights_only=True)
    def equal(left, right):
        if isinstance(left, torch.Tensor):
            return torch.equal(left, right)
        if isinstance(left, dict):
            return left.keys() == right.keys() and all(equal(left[key], right[key]) for key in left)
        if isinstance(left, list):
            return len(left) == len(right) and all(equal(x, y) for x, y in zip(left, right))
        return left == right
    assert equal(uninterrupted, continued)

    current = service.invoke("recipe.get", request, actor)
    assert current["recipe_id"] == project["recipe_id"] and current["revision"] == 2
    assert read_export(backend.root / "jobs" / resumed["operation_id"] / "outputs/export")["verification"]["passed"]
    service.store.close()
