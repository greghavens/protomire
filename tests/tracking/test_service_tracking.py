from uuid import uuid4

import pytest

from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService

ACTOR = Actor("owner", frozenset({"read", "modify", "execute"}))
ONLINE = {"mode": "online", "entity": "explicit-team", "project": "explicit-project", "metric_names": ["loss"]}


class Backend:
    def __init__(self):
        self.payloads = []

    def prepare_execution(self, operation, payload):
        return operation, payload

    def launch_prepared(self, prepared, authorize=None):
        if authorize:
            authorize()
        return self.invoke(*prepared, None)

    def invoke(self, op, payload, actor):
        self.payloads.append(payload)
        if op == "run.plan":
            return {"valid": True}
        return {"operation_id": payload["operation_id"], "backend_run_id": str(uuid4()), "status": "submitted"}


def setup(tmp_path, tracking=None):
    backend = Backend()
    service = OperationService(tmp_path / "state.sqlite3", backend=backend)
    p = service.invoke("project.create", {"slug": "test", "model_name": "Private model", "idempotency_key": "create",
                                          **({"tracking": tracking} if tracking else {})}, ACTOR)
    return service, backend, p, {"project_id": p["project_id"], "recipe_id": p["recipe_id"]}


def test_project_default_disabled_and_run_override(tmp_path):
    service, backend, project, base = setup(tmp_path)
    assert project["tracking"]["mode"] == "disabled"
    result = service.invoke("run.submit", {**base, "trusted": True, "expected_revision": 1,
                                           "idempotency_key": "submit"}, ACTOR)
    assert result["tracking_config"]["mode"] == "disabled"
    assert backend.payloads[-1]["tracking"]["mode"] == "disabled"


def test_plan_discloses_online_without_sending_and_submit_requires_consent(tmp_path):
    service, backend, project, base = setup(tmp_path, ONLINE)
    plan = service.invoke("run.plan", base, ACTOR)
    assert plan["tracking"]["requires_online_consent"] is True
    assert plan["tracking"]["entity"] == "explicit-team"
    assert plan["tracking"]["metric_names"] == ["loss"]
    count = len(backend.payloads)
    request = {**base, "trusted": True, "expected_revision": 1, "idempotency_key": "submit"}
    with pytest.raises(ServiceError, match="consent"):
        service.invoke("run.submit", request, ACTOR)
    assert len(backend.payloads) == count
    service.invoke("run.submit", {**request, "tracking_online_consent": True}, ACTOR)
    assert backend.payloads[-1]["tracking"]["entity"] == "explicit-team"
    service.invoke("run.submit", {**request, "idempotency_key": "disable", "tracking": {"mode": "disabled"}}, ACTOR)
    assert backend.payloads[-1]["tracking"]["mode"] == "disabled"


def test_no_credential_value_or_wrong_secret_alias_persisted_or_returned(tmp_path):
    service, backend, project, base = setup(tmp_path)
    for bad in ({"api_key": "SENTINEL-SECRET"}, {"credential_env": "SENTINEL-SECRET"}):
        with pytest.raises(ServiceError) as error:
            service.invoke("run.plan", {**base, "tracking": {**ONLINE, **bad}}, ACTOR)
        assert "SENTINEL-SECRET" not in str(error.value.as_dict())
    assert not backend.payloads
    assert b"SENTINEL-SECRET" not in (tmp_path / "state.sqlite3").read_bytes()
