"""Adapter continuation keeps immutable selected-node cuts, rechecking bindings.

Contract doubles isolate admission policy; native three-class acceptance executes
same-reference exact state, changed-input refusal, and new-stage/export paths.
"""
from copy import deepcopy
from uuid import uuid4

import pytest
from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.admission import BackendError
from modelbuilder.models import ServiceError


def parent_request():
    pin = {"node_id": "policy", "input_port": "reference", "backend_run_id": "reference-run",
        "source_node_id": "reference", "source_port": "checkpoint", "artifact_id": "reference-artifact", "content_sha256": "a" * 64}
    return {"project_id": "project", "recipe": {"recipe_id": "recipe", "revision": 1,
        "nodes": [{"id": "reference", "kind": "component.run", "inputs": [], "outputs": [{"name": "checkpoint", "type": "Path"}]},
                  {"id": "policy", "kind": "component.run", "inputs": [{"name": "reference", "type": "Path"}], "outputs": [], "parameters": {"rate": .1}}],
        "edges": [{"source_node": "reference", "source_port": "checkpoint", "target_node": "policy", "target_port": "reference"}]},
        "execution": {"node_ids": ["policy"], "input_bindings": [pin]},
        "resolved_input_bindings": [{"path": "/stale/local/path"}], "input_binding_jobs": ["/stale/job"]}


def backend_with_parent(tmp_path, monkeypatch):
    backend = ZenMLBackend(tmp_path)
    parent = parent_request()
    monkeypatch.setattr(backend, "_job", lambda payload: (tmp_path, parent))
    def rpc(operation, payload):
        if operation == "run.status":
            return {"status": "cancelled", "worker_alive": False}
        assert operation == "model.checkpoint.get"
        return {"backend_run_id": "policy-parent", "artifact_id": "policy-checkpoint", "path": str(tmp_path)}
    monkeypatch.setattr(backend, "_rpc", rpc)
    submitted = []
    monkeypatch.setattr(backend, "submit", lambda payload: submitted.append(deepcopy(payload)) or {"status": "submitted"})
    return backend, parent, submitted


@pytest.mark.parametrize("mode", ["exact_state", "warm_start"])
def test_cut_resume_retains_unresolved_pins_and_forgets_resolved_host_state(tmp_path, monkeypatch, mode):
    backend, parent, submitted = backend_with_parent(tmp_path, monkeypatch)
    original = deepcopy(parent)
    changed = deepcopy(parent["recipe"])
    changed["revision"] = 2
    if mode == "warm_start":
        changed["nodes"][1]["parameters"]["rate"] = .2
    result = backend.resume({"project_id": "project", "operation_id": str(uuid4()), "recipe": changed, "trusted": True, "mode": mode})
    assert result["status"] == "submitted"
    assert submitted[0]["execution"] == original["execution"]
    assert submitted[0]["recipe"] == changed
    assert "resolved_input_bindings" not in submitted[0] and "input_binding_jobs" not in submitted[0]
    assert parent == original


@pytest.mark.parametrize("change", ["reference", "calibration", "node-cut"])
def test_exact_cut_resume_refuses_changed_input_or_expanded_work(tmp_path, monkeypatch, change):
    backend, parent, submitted = backend_with_parent(tmp_path, monkeypatch)
    selection = deepcopy(parent["execution"])
    if change == "node-cut":
        selection = {"node_ids": ["reference", "policy"], "input_bindings": []}
    elif change == "reference":
        selection["input_bindings"][0]["artifact_id"] = "other-reference"
    else:
        selection["input_bindings"][0]["content_sha256"] = "b" * 64
    with pytest.raises(BackendError, match="Exact continuation must retain") as error:
        backend.resume({"project_id": "project", "operation_id": str(uuid4()), "mode": "exact_state", "execution": selection, "trusted": True})
    assert error.value.code == "dependency_mismatch" and not submitted


@pytest.mark.parametrize("change", ["remove-input", "rename-node", "new-required-input"])
def test_current_warm_start_recipe_must_still_bind_preserved_cut(tmp_path, monkeypatch, change):
    backend, parent, submitted = backend_with_parent(tmp_path, monkeypatch)
    changed = deepcopy(parent["recipe"])
    changed["revision"] = 2
    if change == "remove-input":
        changed["nodes"][1]["inputs"] = []
    elif change == "rename-node":
        changed["nodes"][1]["id"] = "new-policy"
    else:
        changed["nodes"][1]["inputs"].append({"name": "calibration", "type": "Path"})
    with pytest.raises(ServiceError, match="declared input|unknown recipe node|bind this input explicitly"):
        backend.resume({"project_id": "project", "operation_id": str(uuid4()), "mode": "warm_start", "recipe": changed, "trusted": True})
    assert not submitted


@pytest.mark.parametrize("change", ["add-node", "remove-node"])
def test_full_unbound_parent_warm_start_selects_full_current_graph(tmp_path, monkeypatch, change):
    backend, parent, submitted = backend_with_parent(tmp_path, monkeypatch)
    parent["execution"] = {"node_ids": ["reference", "policy"], "input_bindings": []}
    changed = deepcopy(parent["recipe"])
    changed["revision"] = 2
    if change == "add-node":
        changed["nodes"].append({"id": "new-evaluator", "kind": "component.run", "inputs": [], "outputs": []})
    else:
        changed["nodes"] = [changed["nodes"][1]]
        changed["nodes"][0]["inputs"] = []
        changed["edges"] = []
    backend.resume({"project_id": "project", "operation_id": str(uuid4()), "mode": "warm_start", "recipe": changed, "trusted": True})
    assert submitted[0]["execution"] == {"node_ids": [node["id"] for node in changed["nodes"]], "input_bindings": []}
    assert submitted[0]["recipe"] == changed


@pytest.mark.parametrize("mode", ["exact_state", "warm_start"])
def test_public_cut_resume_selects_one_trainer_before_dataset_resolution(tmp_path, mode):
    from modelbuilder.models import Actor
    from modelbuilder.recipe import default_recipe
    from modelbuilder.service import OperationService

    class NativeContract:
        # Admission double; actual native continuation is independently exercised.
        def __init__(self):
            self.calls = []
        def prepare_execution(self, operation, payload):
            return operation, payload
        def launch_prepared(self, prepared, authorize=None):
            if authorize:
                authorize()
            return self.invoke(*prepared, None)
        def invoke(self, operation, payload, actor):
            self.calls.append((operation, deepcopy(payload)))
            result = {"operation_id": payload.get("operation_id"), "status": "submitted",
                      "backend_run_id": "native-" + payload.get("operation_id", "plan")}
            if payload.get("execution"):
                result["execution"] = deepcopy(payload["execution"])
                for binding in result["execution"]["input_bindings"]:
                    binding.update(path="/cached/path/must/not/be/reused", producer_role="training", producer_recipe_revision=1)
            return result
    backend = NativeContract()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    owner = Actor("cut-owner", {"read", "modify", "execute"})
    def call(operation, **payload):
        return service.invoke(operation, payload, owner)
    body = default_recipe("template", "template", "two-stage")
    trainer = {"id": "reference", "kind": "component.run", "implementation_ref": {
        "module": "harness", "entrypoint": "train", "path": "harness.py", "sha256": "a" * 64, "commit": "1" * 40},
        "inputs": [], "outputs": [{"name": "checkpoint", "type": "Path"}], "parameters": {"rate": .1},
        "environment_ref": "default", "resource_request": {"gpu": 0}, "timeout": 30,
        "retry": {"max_attempts": 1, "backoff_seconds": 0}, "cache_policy": "disabled", "provenance": {},
        "component": {"id": "harness", "version": "1", "protocol": "modelbuilder.component/v1", "role": "training",
            "parameter_schema": {}, "capabilities": {"resume_modes": ["exact_state", "warm_start"], "export_formats": [], "frameworks": ["pytorch"]}}}
    policy = {**deepcopy(trainer), "id": "policy", "inputs": [{"name": "reference", "type": "Path"}]}
    unavailable_dataset = {**deepcopy(trainer), "id": "unavailable_data", "kind": "dataset.version.import",
        "implementation_ref": "builtin:dataset.version.import", "inputs": [], "outputs": [{"name": "data", "type": "dict"}],
        "parameters": {"reference_id": "missing", "split_id": "train", "purpose": "invalid-missing-data-policy"}}
    unavailable_dataset.pop("component")
    body.update(nodes=[trainer, policy, unavailable_dataset], edges=[{"source_node": "reference", "source_port": "checkpoint",
        "target_node": "policy", "target_port": "reference"}], source={"repository": "urn:sha256:" + "a" * 64, "commit": "1" * 40,
        "files": [{"path": "harness.py", "sha256": "a" * 64}]})
    try:
        project = call("project.create", slug="two-stage", model_name="One model", recipe=body, idempotency_key="project")
        common = {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}
        reference = call("run.submit", **common, expected_revision=1, trusted=True, idempotency_key="reference",
            execution={"node_ids": ["reference"], "input_bindings": []})
        pin = {"node_id": "policy", "input_port": "reference", "backend_run_id": reference["backend_run_id"],
            "source_node_id": "reference", "source_port": "checkpoint", "artifact_id": "reference-artifact", "content_sha256": "a" * 64}
        execution = {"node_ids": ["policy"], "input_bindings": [pin]}
        parent = call("run.submit", **common, expected_revision=1, trusted=True, idempotency_key="policy", execution=execution)
        assert "path" in parent["execution"]["input_bindings"][0]  # Older enriched correlation representation.
        # Resolving the whole recipe would fail on the unavailable dataset.
        assert not call("run.plan", **common)["executable"]
        edited = call("recipe.get", **common)
        edited["nodes"][1]["parameters"]["rate"] = .2
        call("recipe.update", **common, expected_revision=1, recipe=edited, idempotency_key="edit")
        call("run.resume", project_id=project["project_id"], operation_id=parent["operation_id"], expected_recipe_revision=2,
            mode=mode, trusted=True, idempotency_key="resume")
        operation, submitted = backend.calls[-1]
        assert operation == "run.resume" and submitted["execution"] == execution
        assert len(submitted["recipe"]["nodes"]) == 3  # Canonical recipe is never pruned.
        assert submitted["recipe"]["nodes"][1]["parameters"]["rate"] == (.1 if mode == "exact_state" else .2)
        assert call("recipe.get", **common, revision=1)["nodes"][1]["parameters"]["rate"] == .1
    finally:
        service.store.close()


@pytest.mark.parametrize("mode", ["exact_state", "warm_start"])
def test_unselected_builtin_trainer_does_not_impose_its_step_budget(tmp_path, monkeypatch, mode):
    backend, parent, submitted = backend_with_parent(tmp_path, monkeypatch)
    parent["recipe"]["nodes"][0].update(kind="model.train", parameters={"steps": 99})
    backend.resume({"project_id": "project", "operation_id": str(uuid4()), "mode": mode, "trusted": True})
    assert submitted[0]["execution"]["node_ids"] == ["policy"]
    assert submitted[0]["recipe"]["nodes"][0]["parameters"]["steps"] == 99
