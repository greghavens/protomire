"""Service-boundary guards for checkpoint-consuming generic resume targets."""
from copy import deepcopy

import pytest

from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService


class RecordingBackend:
    """Contract stub only; this test does not assert native training execution."""
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
        return {"operation_id": payload["operation_id"], "backend_run_id": "native-test-run", "status": "submitted"}


@pytest.mark.parametrize("current_trainers", [0, 1, 2])
def test_warm_start_requires_exactly_one_current_training_component(tmp_path, current_trainers):
    backend = RecordingBackend()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    actor = Actor("owner", {"read", "modify", "execute"})

    def invoke(operation, payload):
        return service.invoke(operation, payload, actor)

    project = invoke("project.create", {"slug": "probe", "model_name": "Probe", "idempotency_key": "project"})
    base = {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}
    recipe = invoke("recipe.get", base)
    node = {"id": "trainer", "kind": "component.run", "implementation_ref": {
        "module": "harness", "entrypoint": "train", "path": "harness.py", "sha256": "a" * 64, "commit": "1" * 40},
        "inputs": [], "outputs": [{"name": "checkpoint", "type": "Path"}], "parameters": {},
        "environment_ref": "default", "resource_request": {"gpu": 0},
        "retry": {"max_attempts": 1, "backoff_seconds": 0}, "timeout": 30, "cache_policy": "disabled", "provenance": {},
        "component": {"id": "harness", "version": "1", "protocol": "modelbuilder.component/v1", "role": "training",
                      "parameter_schema": {}, "capabilities": {"resume_modes": ["warm_start"], "export_formats": [], "frameworks": []}}}
    recipe.update(nodes=[node], source={"repository": "https://example.invalid/source", "commit": "1" * 40,
                                        "files": [{"path": "harness.py", "sha256": "a" * 64}]})
    recipe = invoke("recipe.update", {**base, "expected_revision": 1, "recipe": recipe, "idempotency_key": "training-recipe"})
    parent = invoke("run.submit", {**base, "expected_revision": recipe["revision"], "trusted": True, "idempotency_key": "submit"})
    if current_trainers == 0:
        recipe["nodes"][0]["component"]["role"] = "utility"
    elif current_trainers == 2:
        recipe["nodes"].append({**deepcopy(node), "id": "second-trainer"})
    recipe = invoke("recipe.update", {**base, "expected_revision": recipe["revision"], "recipe": recipe,
                                       "idempotency_key": "edit-current"})
    request = {"project_id": project["project_id"], "operation_id": parent["operation_id"],
               "expected_recipe_revision": recipe["revision"], "mode": "warm_start", "trusted": True,
               "idempotency_key": "resume"}
    if current_trainers == 1:
        result = invoke("run.resume", request)
        assert result["status"] == "submitted"
        assert result["recipe_id"] == project["recipe_id"]
        assert backend.calls[-1][0] == "run.resume"
    else:
        with pytest.raises(ServiceError, match="exactly one checkpoint-consuming training component"):
            invoke("run.resume", request)
        assert not any(operation == "run.resume" for operation, _ in backend.calls)
        assert invoke("recipe.get", base)["revision"] == recipe["revision"]
    service.store.close()
