"""Real service/ASGI recipe configuration and inert File-menu transport contract."""
from fastapi.testclient import TestClient

from modelbuilder.api import create_app
from modelbuilder.models import Actor
from modelbuilder.service import OperationService


def test_stage_bound_uses_single_recipe_history_and_lossless_bundle(tmp_path):
    service = OperationService(tmp_path / "settings.sqlite3")
    actor = Actor("settings-owner", {"read", "modify"})
    client = TestClient(create_app(service, {"settings-test-token": actor}))
    client.headers["Authorization"] = "Bearer settings-test-token"

    def invoke(operation, payload, status=200):
        response = client.post("/v1/operations/" + operation, json=payload)
        assert response.status_code == status, response.text
        return response.json()

    try:
        project = invoke("project.create", {"slug": "stage-settings", "model_name": "One model",
            "idempotency_key": "project"})
        identity = {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}
        original = invoke("recipe.get", identity)
        assert "execution" not in original
        changed = invoke("recipe.update", {**identity, "expected_revision": 1, "idempotency_key": "settings",
            "recipe": {**original, "execution": {"max_parallel_stages": 3}}})
        assert changed["recipe_id"] == original["recipe_id"]
        assert changed["revision"] == 2 and changed["digest"] != original["digest"]
        assert changed["execution"] == {"max_parallel_stages": 3}
        invoke("recipe.update", {**identity, "expected_revision": 1, "idempotency_key": "stale",
            "recipe": {**original, "execution": {"max_parallel_stages": 4}}}, 409)
        assert invoke("recipe.get", identity) == changed
        exported = invoke("recipe.export", {**identity, "format": "json"})
        imported = invoke("recipe.import", {"project_id": project["project_id"], "expected_revision": 2,
            "bundle_json": exported["bundle_json"], "idempotency_key": "import"})
        assert imported["recipe_id"] == original["recipe_id"] and imported["revision"] == 3
        assert imported["execution"] == changed["execution"]
        assert imported["import_report"]["execution_allowed"] is False
        current = invoke("recipe.get", identity)
        invalid = invoke("recipe.update", {**identity, "expected_revision": 3, "idempotency_key": "invalid",
            "recipe": {**current, "execution": {"max_parallel_stages": 9}}}, 400)
        assert invalid["error"]["code"] == "invalid_recipe"
        current = invoke("recipe.get", identity)
        assert current["revision"] == 3 and current["execution"] == {"max_parallel_stages": 3}
        assert len(service.store.list("recipe", project["project_id"])) == 1
        assert service.store.list("operation", project["project_id"]) == []
    finally:
        service.store.close()
