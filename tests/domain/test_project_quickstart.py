import pytest
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService


@pytest.mark.parametrize("starting_point", ["blank", "research", "base_model", "scratch"])
@pytest.mark.parametrize("assistant", ["none", "codex", "claude", "triggerfish"])
def test_every_quickstart_path_creates_exactly_one_project_recipe(tmp_path, starting_point, assistant):
    service = OperationService(tmp_path / "state.sqlite3")
    owner = Actor("owner", frozenset({"read", "modify"}))
    payload = {"model_name": "New architecture", "slug": "new-architecture", "intent": {
        "schema_version": "1", "goal": "Study evidence, implement and evaluate a custom architecture",
        "starting_point": starting_point, "assistant": assistant}, "idempotency_key": "same-click"}
    first = service.invoke("project.create", payload, owner)
    assert service.invoke("project.create", payload, owner) == first
    assert first["intent"] == payload["intent"]
    assert first["recipe_id"] == first["active_recipe_id"]
    assert len(service.store.list("project")) == len(service.store.list("recipe")) == 1
    assert service.store.list("operation") == []
    assert service.store.get("recipe", first["recipe_id"])["revision"] == 1
    changed = {"project_id": first["project_id"], "expected_revision": 1, "changes": {"intent": {
        **payload["intent"], "goal": "Reviewed next goal"}}, "idempotency_key": "update"}
    second = service.invoke("project.update", changed, owner)
    assert second["revision"] == 2
    assert service.store.get("project", first["project_id"], 1)["intent"]["goal"] == payload["intent"]["goal"]
    assert len(service.invoke("project.history", {"project_id": first["project_id"]}, owner)["events"]) == 2
    with pytest.raises(ServiceError) as error:
        service.invoke("project.update", {**changed, "idempotency_key": "stale"}, owner)
    assert error.value.code == "revision_conflict"
    service.store.close()


@pytest.mark.parametrize("intent", [{"schema_version": "2"}, {"goal": "x" * 8001}, {"starting_point": "download_and_train"},
                                    {"assistant": "unknown"}, {"authorization": True}, {"goal": 123}])
def test_invalid_or_permission_like_intent_is_rejected_atomically(intent):
    service = OperationService(":memory:")
    with pytest.raises(ServiceError):
        service.invoke("project.create", {"model_name": "Model", "slug": "model", "intent": intent,
                                         "idempotency_key": "create"}, Actor("owner", frozenset({"modify"})))
    assert service.store.list("project") == service.store.list("recipe") == []
    service.store.close()
