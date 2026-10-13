"""Real common-operation CPU/eager Trainer v2 continuation and native artifacts."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import shutil
import tarfile
import time

import pytest
from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.components import read_checkpoint, read_export
from modelbuilder.backend.components import transformers_epochs, transformers_training
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService
from tests.components.rights_test_support import review_local_dataset, write_reviewed_policies
from tests.integration.native_fixture_support import synthetic_review, register_project_review
from test_transformers_epochs import epoch_local as registered_epoch_fixture, assert_state_equal

from test_transformers_training import local as registered_local_fixture

local = registered_local_fixture
epoch_local = registered_epoch_fixture
pytestmark = pytest.mark.integration

WRAPPER = """from pathlib import Path
import time
from modelbuilder.backend.components.transformers_epochs import train as epoch_train
from modelbuilder.backend.components.transformers_training import evaluate, export

def train(*, inputs, parameters, context):
    original=context.get('checkpoint_ready')
    def ready(**event):
        if original: original(**event)
        job=Path(context['job_dir'])
        # Test-owned synchronization only. Scientific parameters and scheduler
        # horizon are identical in the uninterrupted, cancelled and resumed runs.
        if event['step']==4 and not context.get('resume') and (job/'pause-at-boundary').is_file():
            (job/'boundary-waiting').write_text('completed epoch 2')
            until=time.monotonic()+30
            while time.monotonic()<until and not Path(context['cancel_file']).exists():
                time.sleep(.05)
            if not Path(context['cancel_file']).exists():
                raise RuntimeError('Test boundary synchronization expired')
    from transformers import Trainer
    import torch
    original_train=Trainer.train
    original_step=torch.optim.AdamW.step
    def entered_train(self,*args,**kwargs):
        (Path(context['job_dir'])/'trainer-entered').write_text('actual Trainer.train entered')
        return original_train(self,*args,**kwargs)
    def entered_step(self,*args,**kwargs):
        with (Path(context['job_dir'])/'optimizer-entered').open('a') as stream: stream.write('AdamW.step entered\\n')
        return original_step(self,*args,**kwargs)
    Trainer.train=entered_train
    torch.optim.AdamW.step=entered_step
    try:
        return epoch_train(inputs=inputs,parameters=parameters,context={**context,'checkpoint_ready':ready})
    finally:
        Trainer.train=original_train
        torch.optim.AdamW.step=original_step
"""


def setup_service(local):
    source = local["source"]
    wrapper = source / "adapter.py"
    wrapper.write_text(WRAPPER)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "Source-owned service epoch synchronization",
        ],
        check=True,
    )
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    source_identity = {
        "repository": "urn:sha256:" + hashlib.sha256(wrapper.read_bytes()).hexdigest(),
        "commit": commit,
        "files": [{"path": "adapter.py", "sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest()}],
    }
    pid = local["project"]["project_id"]
    actor = Actor("owner", {"read", "modify", "execute"})
    registry = local["registry"]
    dependency_paths, _ = registry.execution_sources(pid, local["record"]["dataset_id"], 1)
    dependency_paths.update(local["context"]["dependencies"])
    backend = ZenMLBackend(
        local["tmp"] / "native",
        python_executable=sys.executable,
        source_root=source,
        trusted_code_roots=[str(source)],
        dependency_paths=dependency_paths,
    )
    service = OperationService(
        local["service"].store,
        backend,
        source_root=source,
        dataset_roots={pid: {"data": str(local["tmp"] / "data")}},
    )
    reviewed = review_local_dataset(service, local["record"], local["tmp"] / "data", actor)
    reference = service._dataset_registry.portable_reference(
        pid, local["record"]["dataset_id"], reviewed["revision"]
    )
    from modelbuilder.dataset_binding import reference_dependency, source_dependencies

    dependencies = [
        {
            "id": key,
            "kind": "model",
            "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            "metadata": {"rights": local["rights"]},
        }
        for key, path in local["context"]["dependencies"].items()
    ]
    dataset_rights = service.hub.dataset_rights(reference)
    dependencies.extend(source_dependencies(reference, dataset_rights))
    ref = reference_dependency(reference, dataset_rights)
    dependencies.append(ref)
    entries = [transformers_epochs.catalog_entries()[0], *transformers_training.catalog_entries()[1:]]
    parameters = [
        local["parameters"],
        {"batch_size": 2, "metrics": ["accuracy", "cross_entropy"]},
        {"parity_texts": ["sun sky"], "atol": 1e-7, "verification_timeout_seconds": 60},
    ]
    nodes = []
    for name, entry, params in zip(["train", "evaluate", "export"], entries, parameters):
        nodes.append(
            {
                "id": name,
                "kind": "component.run",
                "component": {
                    key: entry[key]
                    for key in (
                        "id",
                        "version",
                        "protocol",
                        "role",
                        "description",
                        "parameter_schema",
                        "capabilities",
                    )
                },
                "inputs": entry["inputs"],
                "outputs": entry["outputs"],
                "parameters": deepcopy(params),
                "cache_policy": "disabled",
                "timeout": 120,
                "retry": {"max_attempts": 1},
                "resource_request": {"gpu": 0},
                "environment_ref": "default",
                "implementation_ref": {
                    **source_identity["files"][0],
                    "module": "service_epochs",
                    "entrypoint": name,
                    "commit": commit,
                },
            }
        )
    for name, split, purpose in [
        ("training_data", "train", "training"),
        ("validation_data", "dev", "evaluation"),
    ]:
        nodes.append(
            {
                "id": name,
                "kind": "dataset.version.import",
                "inputs": [],
                "outputs": [{"name": "data", "type": "dict"}],
                "parameters": {"reference_id": ref["id"], "split_id": split, "purpose": purpose},
                "cache_policy": "disabled",
                "timeout": 120,
                "retry": {"max_attempts": 1},
            }
        )
    edges = [
        {
            "source_node": "training_data",
            "source_port": "data",
            "target_node": "train",
            "target_port": "train",
        },
        {
            "source_node": "validation_data",
            "source_port": "data",
            "target_node": "train",
            "target_port": "validation",
        },
        {
            "source_node": "validation_data",
            "source_port": "data",
            "target_node": "evaluate",
            "target_port": "data",
        },
        *[
            {"source_node": "train", "source_port": "trained", "target_node": name, "target_port": "trained"}
            for name in ("evaluate", "export")
        ],
    ]
    recipe = {
        "schema_version": "0",
        "project_id": pid,
        "recipe_id": local["project"]["recipe_id"],
        "revision": 1,
        "name": "Service exact epoch fixture",
        "source": source_identity,
        "nodes": nodes,
        "edges": edges,
        "resources": [],
        "external_dependencies": dependencies,
        "environment": {"python": "3.12", "requirements": ["zenml==0.97.0"]},
        "outputs": [],
        "evaluation": {"live": {"enabled": True, "max_concurrency": 1, "timeout_seconds": 60}},
    }
    for dependency in dependencies:
        dependency.setdefault("url", "urn:sha256:" + dependency["sha256"])
        dependency.setdefault("license", "NOASSERTION")
    for node in nodes:
        node.setdefault("implementation_ref", "builtin:" + node["kind"])
        node.setdefault("environment_ref", "default")
        node.setdefault("resource_request", {"gpu": 0})
        node["retry"].setdefault("backoff_seconds", 0)
        node.setdefault("provenance", {})
    policy = synthetic_review(recipe, pid, source)
    register_project_review(service, policy, pid)
    write_reviewed_policies(service.store.path, {local["policy"]["policy_id"]: local["policy"]})
    recipe = service.invoke(
        "recipe.create",
        {"project_id": pid, "recipe": recipe, "expected_revision": 1, "idempotency_key": "service-recipe"},
        actor,
    )
    return service, backend, actor, recipe


def verify_owned_processes_gone(backend, operation_id):
    from modelbuilder.backend.common import alive, process_group_state, read_json

    state = read_json(backend.root / "jobs" / operation_id / "state.json")
    for prefix in ("worker", "supervisor"):
        assert not alive(state.get(prefix + "_pid"), state.get(prefix + "_identity")), state
    if state.get("worker_pid"):
        assert process_group_state(state["worker_pid"]) is False, state
        assert state.get("worker_reaped") is True, state


def stop_and_wait(service, backend, actor, pid, operation_id, *, key):
    service.invoke(
        "run.cancel", {"project_id": pid, "operation_id": operation_id, "idempotency_key": key}, actor
    )
    until = time.monotonic() + 30
    while time.monotonic() < until:
        row = service.invoke("run.status", {"project_id": pid, "operation_id": operation_id}, actor)
        if row["worker_alive"] is False and row["supervisor_alive"] is False:
            verify_owned_processes_gone(backend, operation_id)
            return row
        time.sleep(0.2)
    raise AssertionError("Cancellation did not prove owned process termination: " + str(row))


@pytest.fixture
def service_case(epoch_local):
    service, backend, actor, recipe = setup_service(epoch_local)
    pid = epoch_local["project"]["project_id"]
    try:
        yield service, backend, actor, recipe
    finally:
        failures = []
        # Includes reserved/failed operations, not just IDs returned by submit.
        for operation in service.store.list("operation", pid):
            operation_id = operation["operation_id"]
            if not (backend.root / "jobs" / operation_id).exists():
                continue  # No native ownership record or job was created.
            try:
                row = service.invoke("run.status", {"project_id": pid, "operation_id": operation_id}, actor)
                if row["worker_alive"] is not False or row["supervisor_alive"] is not False:
                    stop_and_wait(service, backend, actor, pid, operation_id, key="cleanup-" + operation_id)
                else:
                    verify_owned_processes_gone(backend, operation_id)
            except Exception as error:
                failures.append((operation_id, str(error)))
        assert not failures, failures


def wait_run(service, actor, pid, operation_id, *, expected="succeeded"):
    until = time.monotonic() + 180
    while time.monotonic() < until:
        row = service.invoke("run.status", {"project_id": pid, "operation_id": operation_id}, actor)
        if (
            row["status"] in {"succeeded", "failed", "cancelled"}
            and row["worker_alive"] is False
            and row["supervisor_alive"] is False
        ):
            assert row["status"] == expected, row
            return row
        time.sleep(0.2)
    stop_and_wait(service, service.backend, actor, pid, operation_id, key="timeout-" + operation_id)
    pytest.fail("Native service operation exceeded existing fixture deadline: " + str(row))


def test_service_submit_discover_cancel_resume_matches_full_horizon(epoch_local, service_case):
    import torch
    from safetensors.torch import load_file

    service, backend, actor, recipe = service_case
    pid = epoch_local["project"]["project_id"]
    scope = {"project_id": pid, "recipe_id": recipe["recipe_id"]}

    def submit(key):
        return service.invoke(
            "run.submit",
            {**scope, "expected_revision": recipe["revision"], "trusted": True, "idempotency_key": key},
            actor,
        )

    full = wait_run(service, actor, pid, submit("uninterrupted")["operation_id"])
    full_ref = service.invoke(
        "model.checkpoint.get",
        {"project_id": pid, "operation_id": full["operation_id"], "checkpoint_kind": "last"},
        actor,
    )
    interrupted = submit("interrupt")
    job = backend.root / "jobs" / interrupted["operation_id"]
    (job / "pause-at-boundary").write_text("fixture synchronization")
    until = time.monotonic() + 90
    while time.monotonic() < until and not (job / "boundary-waiting").is_file():
        time.sleep(0.05)
    assert (job / "boundary-waiting").is_file(), service.invoke(
        "run.logs", {"project_id": pid, "operation_id": interrupted["operation_id"]}, actor
    )
    service.invoke(
        "run.cancel",
        {
            "project_id": pid,
            "operation_id": interrupted["operation_id"],
            "idempotency_key": "cancel-boundary",
        },
        actor,
    )
    parent = wait_run(service, actor, pid, interrupted["operation_id"], expected="cancelled")
    recovered = service.invoke(
        "model.checkpoint.get",
        {"project_id": pid, "operation_id": parent["operation_id"], "checkpoint_kind": "last"},
        actor,
    )
    assert read_checkpoint(Path(recovered["path"]))["step"] == 4
    assert "exact_state" in recovered["resume_modes"]
    resume = {
        "project_id": pid,
        "operation_id": parent["operation_id"],
        "checkpoint_kind": "last",
        "checkpoint_ref": recovered["artifact_id"],
        "mode": "exact_state",
        "trusted": True,
        "expected_recipe_revision": recipe["revision"],
        "idempotency_key": "exact-resume",
    }
    parent_before = (job / "request.json").read_bytes()
    with pytest.raises(ServiceError) as denied:
        service.invoke(
            "run.resume", {**resume, "idempotency_key": "read-only-resume"}, Actor("owner", {"read"})
        )
    assert denied.value.code == "forbidden"
    with pytest.raises(ServiceError) as denied:
        service.invoke(
            "run.resume", {**resume, "trusted": False, "idempotency_key": "untrusted-resume"}, actor
        )
    assert denied.value.code == "untrusted_execution_blocked"
    # Exact continuation deliberately keeps the parent's contract even when
    # the editable recipe has newer optimizer settings. This is not warm start.
    edited = deepcopy(recipe)
    next(node for node in edited["nodes"] if node["id"] == "train")["parameters"]["learning_rate"] = 0.009
    current = service.invoke(
        "recipe.update",
        {
            **scope,
            "recipe": edited,
            "expected_revision": recipe["revision"],
            "idempotency_key": "edited-current-contract",
        },
        actor,
    )
    resume["expected_recipe_revision"] = current["revision"]
    submitted = service.invoke("run.resume", resume, actor)
    resumed = wait_run(service, actor, pid, submitted["operation_id"])
    final = service.invoke(
        "model.checkpoint.get",
        {"project_id": pid, "operation_id": resumed["operation_id"], "checkpoint_kind": "last"},
        actor,
    )
    a, b = Path(full_ref["path"]), Path(final["path"])
    assert read_checkpoint(a)["step"] == read_checkpoint(b)["step"] == 8
    assert_state_equal(load_file(a / "model.safetensors"), load_file(b / "model.safetensors"))
    for name in ("optimizer.pt", "scheduler.pt"):
        assert_state_equal(torch.load(a / name, weights_only=True), torch.load(b / name, weights_only=True))
    assert_state_equal(
        torch.load(a / "rng_state.pth", weights_only=False),
        torch.load(b / "rng_state.pth", weights_only=False),
    )
    sa, sb = (
        json.loads((a / "epoch-state.json").read_text()),
        json.loads((b / "epoch-state.json").read_text()),
    )
    assert sa["lineage"] == sb["lineage"] and [row["metrics"] for row in sa["history"]] == [
        row["metrics"] for row in sb["history"]
    ]
    assert sa["contract"] == sb["contract"] and sb["contract"]["scheduler_horizon_steps"] == 8
    assert resumed["resume_mode"] == "exact_state" and resumed["parent_run_id"] == parent["backend_run_id"]
    assert full["metrics"]["cross_entropy"] == resumed["metrics"]["cross_entropy"]
    full_export = read_export(backend.root / "jobs" / full["operation_id"] / "outputs/export")
    resumed_export = read_export(backend.root / "jobs" / resumed["operation_id"] / "outputs/export")
    assert full_export["verification"]["fresh_process"] and resumed_export["verification"]["fresh_process"]
    assert (
        hashlib.sha256(
            (backend.root / "jobs" / full["operation_id"] / "outputs/export/model.safetensors").read_bytes()
        ).hexdigest()
        == hashlib.sha256(
            (
                backend.root / "jobs" / resumed["operation_id"] / "outputs/export/model.safetensors"
            ).read_bytes()
        ).hexdigest()
    )

    assert (job / "request.json").read_bytes() == parent_before
    executed = json.loads((backend.root / "jobs" / resumed["operation_id"] / "request.json").read_text())
    actual_parameters = next(
        node["parameters"] for node in executed["recipe"]["nodes"] if node["id"] == "train"
    )
    assert actual_parameters["learning_rate"] == 0.005 and actual_parameters["horizon_epochs"] == 4
    assert actual_parameters == epoch_local["parameters"]
    assert sb["completed_epochs"] == 4 and sb["evidence"]["subject"]["run_id"] == parent["backend_run_id"]
    assert read_checkpoint(b)["execution"]["current_subject"]["run_id"] == resumed["backend_run_id"]
    for row, descriptor, expected_checkpoint in (
        (full, full_export, full_ref),
        (resumed, resumed_export, final),
    ):
        artifacts = service.invoke(
            "run.artifacts", {"project_id": pid, "backend_run_id": row["backend_run_id"]}, actor
        )["artifacts"]
        artifact = next(
            item for item in artifacts if item["node_id"] == "export" and item["port"] == "exported"
        )
        identity = {
            "project_id": pid,
            "backend_run_id": row["backend_run_id"],
            "artifact_id": artifact["artifact_id"],
        }
        inspected = service.invoke("model.export.inspect", identity, actor)
        package = service.invoke(
            "model.export.package", {**identity, "expected_export_digest": inspected["export_digest"]}, actor
        )
        assert package["sha256"]
        assert descriptor["selected_checkpoint"] == expected_checkpoint["sha256"]
    # Corrupt only test-owned native archive bytes and restore them afterward.
    # Every call still crosses real checkpoint discovery and run.resume gates.
    archive = Path(recovered["uri"]) / "data.tar.gz"
    assert archive.resolve().is_relative_to(backend.root)
    original = archive.read_bytes()
    negative_receipts = []
    try:
        for case in ("partial-trainer-state", "changed-contract", "missing-optimizer"):
            invalid = epoch_local["tmp"] / case
            shutil.copytree(Path(recovered["path"]), invalid)
            if case == "partial-trainer-state":
                saved = json.loads((invalid / "trainer_state.json").read_text())
                saved["epoch"] = 1.5
                (invalid / "trainer_state.json").write_text(json.dumps(saved))
            elif case == "changed-contract":
                saved = json.loads((invalid / "epoch-state.json").read_text())
                saved["contract"]["parameters"]["horizon_epochs"] = 5
                (invalid / "epoch-state.json").write_text(json.dumps(saved))
            else:
                (invalid / "optimizer.pt").unlink()
            manifest = json.loads((invalid / "checkpoint.manifest.json").read_text())
            manifest["assets"] = transformers_training._assets(invalid)
            (invalid / "checkpoint.manifest.json").write_text(json.dumps(manifest))
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(invalid, arcname=".")
            revision = service.invoke("recipe.get", scope, actor)["revision"]
            candidate = {**resume, "expected_recipe_revision": revision, "idempotency_key": "reject-" + case}
            expected_rejection = (
                "Exact epoch checkpoint adapter, assets or original horizon differ"
                if case == "missing-optimizer"
                else "Exact resume requires unchanged complete native epoch/horizon and options"
            )
            unsafe = service.invoke("run.resume", candidate, actor)
            failed = wait_run(service, actor, pid, unsafe["operation_id"], expected="failed")
            assert failed["resume_mode"] == "exact_state"
            failed_job = backend.root / "jobs" / failed["operation_id"]
            logs = service.invoke(
                "run.logs", {"project_id": pid, "operation_id": failed["operation_id"]}, actor
            )
            assert "ValueError: " + expected_rejection in json.dumps(logs), logs
            assert not (failed_job / "trainer-entered").exists(), case
            assert not (failed_job / "optimizer-entered").exists(), case
            assert not list((failed_job / "outputs/train/checkpoints").glob("*/checkpoint.manifest.json"))
            verify_owned_processes_gone(backend, failed["operation_id"])
            negative_receipts.append(
                {
                    "case": case,
                    "rejection": "specific_checkpoint_validation_before_Trainer.train",
                    "expected_rejection": expected_rejection,
                    "trainer_entered": False,
                    "optimizer_entered": False,
                    "status": failed["status"],
                    "worker_alive": failed["worker_alive"],
                    "supervisor_alive": failed["supervisor_alive"],
                }
            )
            archive.write_bytes(original)
    finally:
        archive.write_bytes(original)
    assert archive.read_bytes() == original
    for row in (full, parent, resumed):
        positive_job = backend.root / "jobs" / row["operation_id"]
        assert (positive_job / "trainer-entered").is_file()
        assert (positive_job / "optimizer-entered").is_file()
        verify_owned_processes_gone(backend, row["operation_id"])

    receipt = {
        "schema": "modelbuilder.service-epoch-resume-test/v1",
        "status": "passed",
        "scope": "static five-node CPU/eager DAG: two registered dataset imports, one Trainer v2, evaluation and export",
        "checks": {
            "model_tensors_equal": True,
            "optimizer_equal": True,
            "scheduler_equal": True,
            "python_numpy_torch_rng_equal": True,
            "row_order_equal": True,
            "heldout_metrics_equal": True,
            "selected_export_weights_equal": True,
            "fresh_process_export_verification": True,
            "parent_recipe_immutable": True,
            "parent_contract_retained_over_current_edits": True,
        },
        "scheduler_horizon_steps": 8,
        "recovered_epoch": 2,
        "final_epoch": 4,
        "runs": {
            key: {
                field: row.get(field)
                for field in (
                    "backend_run_id",
                    "operation_id",
                    "status",
                    "worker_alive",
                    "supervisor_alive",
                    "recipe_revision",
                    "resume_mode",
                    "parent_run_id",
                )
            }
            for key, row in [("uninterrupted", full), ("cancelled_parent", parent), ("exact_resume", resumed)]
        },
        "negative_receipts": negative_receipts,
        "unsafe_archive_restored": True,
    }
    (epoch_local["tmp"] / "service-resume-receipt.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    )
