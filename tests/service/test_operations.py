"""Shared service tests. Fake backend below tests contracts, never execution acceptance."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from modelbuilder.api import create_app
from modelbuilder.auth import TokenAuth
from modelbuilder.models import Actor, OPERATIONS, ServiceError
from modelbuilder.service import OperationService

OWNER = Actor("owner", {"read", "modify", "execute"})
READER = Actor("reader", {"read"})


@pytest.fixture
def service(tmp_path):
    service = OperationService(db_path=tmp_path / "service.sqlite3")
    yield service
    service.store.close()


def project(service, slug="test"):
    return service.invoke("project.create", {"slug": slug, "model_name": "One model", "idempotency_key": slug}, OWNER)


def recipe(service, p, key="recipe"):
    return service.invoke("recipe.get", {"project_id": p["project_id"], "recipe_id": p["recipe_id"]}, OWNER)


def test_restart_preserves_projects_recipes_history(tmp_path):
    path = tmp_path / "durable.sqlite3"
    first = OperationService(path)
    p, r = project(first), None
    r = recipe(first, p)
    first.store.close()
    second = OperationService(path)
    assert second.invoke("project.get", {"project_id": p["project_id"]}, OWNER) == p
    assert second.invoke("recipe.get", {"project_id": p["project_id"], "recipe_id": r["recipe_id"]}, OWNER) == r
    assert len(second.invoke("project.history", {"project_id": p["project_id"]}, OWNER)["events"]) == 1
    second.store.close()


def test_idempotent_mutation_and_payload_conflict(service):
    p = project(service)
    assert project(service) == p
    with pytest.raises(ServiceError, match="different request") as error:
        service.invoke("project.create", {"slug": "different", "model_name": "One model", "idempotency_key": "test"}, OWNER)
    assert error.value.status_code == 409
    assert len(service.invoke("project.list", {}, OWNER)["projects"]) == 1


def test_idempotency_ignores_request_id_but_not_actor(service):
    payload = {"slug": "one", "model_name": "Model", "idempotency_key": "same", "request_id": "one"}
    p = service.invoke("project.create", payload, OWNER)
    assert service.invoke("project.create", {**payload, "request_id": "two"}, OWNER) == p
    other = Actor("other", {"read", "modify"})
    q = service.invoke("project.create", {**payload, "slug": "two"}, other)
    assert p["project_id"] != q["project_id"]


@pytest.mark.parametrize("operation", [name for name, spec in OPERATIONS.items() if spec.scope != "read"])
def test_every_nonread_operation_denies_readonly_and_audits(service, operation):
    p = project(service)
    with pytest.raises(ServiceError) as error:
        service.invoke(operation, {"project_id": p["project_id"]}, READER)
    assert error.value.code == "forbidden"
    assert error.value.status_code == 403
    events = service.invoke("project.history", {"project_id": p["project_id"]}, OWNER)["events"]
    assert events[0]["actor_id"] == "reader"
    assert events[0]["outcome"] == "denied"
    assert events[0]["operation"] == operation


def test_project_scope_and_cross_resource_isolation(service):
    a, b = project(service, "a"), project(service, "b")
    r = recipe(service, b)
    scoped = Actor("scoped", {"read", "modify"}, {a["project_id"]})
    assert service.invoke("project.list", {}, scoped)["projects"] == [a]
    with pytest.raises(ServiceError) as error:
        service.invoke("project.get", {"project_id": b["project_id"]}, scoped)
    assert error.value.status_code == 403
    with pytest.raises(ServiceError) as error:
        service.invoke("recipe.get", {"project_id": a["project_id"], "recipe_id": r["recipe_id"]}, scoped)
    assert error.value.status_code == 404
    with pytest.raises(ServiceError):
        service.invoke("project.create", {"slug": "x", "model_name": "x", "idempotency_key": "x"}, scoped)


def test_recipe_revision_conflict_diff_and_history(service):
    p = project(service)
    r = recipe(service, p)
    payload = {"project_id": p["project_id"], "recipe_id": r["recipe_id"], "expected_revision": 1,
               "recipe": {**r, "name": "GUI change"}, "idempotency_key": "gui"}
    updated = service.invoke("recipe.update", payload, OWNER)
    assert updated["revision"] == 2
    assert updated["digest"] != r["digest"]
    with pytest.raises(ServiceError) as error:
        service.invoke("recipe.update", {**payload, "recipe": {**r, "name": "MCP change"}, "idempotency_key": "mcp"}, OWNER)
    assert error.value.status_code == 409
    assert "GUI change" in error.value.details["diff"]
    old = service.invoke("recipe.get", {"project_id": p["project_id"], "recipe_id": r["recipe_id"], "revision": 1}, OWNER)
    assert old == r
    assert service.invoke("recipe.update", payload, OWNER) == updated  # stale retry stays idempotent


def test_multiple_connections_cannot_lose_updates(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    initial = OperationService(path)
    p = project(initial)
    r = recipe(initial, p)
    def update(index):
        service = OperationService(path)
        try:
            return service.invoke("recipe.update", {"project_id": p["project_id"], "recipe_id": r["recipe_id"],
                                  "expected_revision": 1, "recipe": {**r, "name": f"edit {index}"},
                                  "idempotency_key": f"edit-{index}"}, OWNER)
        except ServiceError as error:
            return error.code
        finally:
            service.store.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(update, range(4)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("revision_conflict") == 3
    initial.store.close()


def test_clone_preserves_identity_and_adds_historical_revision(service):
    p = project(service)
    r = recipe(service, p)
    clone = service.invoke("recipe.clone", {"project_id": p["project_id"], "recipe_id": r["recipe_id"],
                            "expected_revision": 1, "idempotency_key": "clone"}, OWNER)
    assert clone["recipe_id"] == r["recipe_id"]
    assert clone["project_id"] == r["project_id"]
    assert clone["revision"] == 2
    assert service.store.get("recipe", r["recipe_id"], 1) == r


def test_recipe_identity_immutable_and_unknown_schema_rejected(service):
    p = project(service)
    r = recipe(service, p)
    for body in ({**r, "project_id": "other"}, {**r, "schema_version": "1000"}):
        with pytest.raises(ServiceError):
            service.invoke("recipe.update", {"project_id": p["project_id"], "recipe_id": r["recipe_id"],
                              "expected_revision": 1, "recipe": body, "idempotency_key": "bad"}, OWNER)
    assert service.invoke("recipe.get", {"project_id": p["project_id"], "recipe_id": r["recipe_id"]}, OWNER) == r


def test_research_claims_hypotheses_negative_updates_and_links(service):
    p = project(service)
    pid = p["project_id"]
    evidence = service.invoke("evidence.add", {"project_id": pid, "title": "Distillation",
                              "source_identifier": "arXiv:1503.02531", "idempotency_key": "e"}, OWNER)
    claim = service.invoke("claim.create", {"project_id": pid, "proposition": "KL may improve retention",
                           "evidence_ids": [evidence["evidence_id"]], "idempotency_key": "c"}, OWNER)
    h = service.invoke("hypothesis.create", {"project_id": pid, "claim_ids": [claim["claim_id"]],
                       "proposed_change": "Add KL", "expected_outcome": "Lower validation loss",
                       "acceptance_metric": {"name": "validation_loss", "direction": "minimize"}, "idempotency_key": "h"}, OWNER)
    h2 = service.invoke("hypothesis.update", {"project_id": pid, "hypothesis_id": h["hypothesis_id"],
                        "expected_revision": 1, "changes": {"status": "rejected"}, "idempotency_key": "h2"}, OWNER)
    assert h2["status"] == "rejected"
    assert service.store.get("hypothesis", h["hypothesis_id"], 1)["status"] == "proposed"
    assert service.invoke("evidence.search", {"project_id": pid, "query": "distillation"}, OWNER)["evidence"] == [evidence]
    with pytest.raises(ServiceError):
        service.invoke("claim.create", {"project_id": pid, "proposition": "Unknown evidence",
                       "evidence_ids": ["missing"], "idempotency_key": "invalid"}, OWNER)


def test_no_arbitrary_changes_or_credentials(service):
    p = project(service)
    for changes in ({"revision": 999}, {"project_id": "other"}, {"git_remote": "https://user:secret@example.com/repo"}):
        with pytest.raises(ServiceError):
            service.invoke("project.update", {"project_id": p["project_id"], "expected_revision": 1,
                           "changes": changes, "idempotency_key": "bad"}, OWNER)
    with pytest.raises(ServiceError):
        service.invoke("evidence.add", {"project_id": p["project_id"], "title": "a", "source_identifier": "x",
                       "citation_metadata": {"api_key": "secret"}, "idempotency_key": "secret"}, OWNER)


@pytest.mark.parametrize("changes", [{"remote_execution_enabled": True}, {"publication_enabled": True},
                                    {"untrusted_execution_enabled": True}, {"allowed_targets": ["cloud-gpu"]},
                                    {"remote_spend_cap": 1}, {"max_local_runtime_seconds": 0},
                                    {"max_local_runtime_seconds": True}])
def test_policy_fails_closed(service, changes):
    p = project(service)
    with pytest.raises(ServiceError):
        service.invoke("policy.update", {"project_id": p["project_id"], "expected_revision": 1,
                       "changes": changes, "idempotency_key": "policy"}, OWNER)
    assert service.invoke("policy.get", {"project_id": p["project_id"]}, OWNER)["remote_execution_enabled"] is False


def test_policy_revisions_and_truthful_cost(service):
    p = project(service)
    updated = service.invoke("policy.update", {"project_id": p["project_id"], "expected_revision": 1,
                             "changes": {"max_local_runtime_seconds": 120}, "idempotency_key": "policy"}, OWNER)
    assert updated["revision"] == 2
    cost = service.invoke("cost.estimate", {"project_id": p["project_id"]}, OWNER)
    assert cost["total_cost"] is None
    assert not cost["spending_enforced"]
    with pytest.raises(ServiceError):
        service.invoke("cost.estimate", {"project_id": p["project_id"], "target": "aws"}, OWNER)


def test_public_registry_is_unavailable_and_private_publication_requires_scope(service):
    with pytest.raises(ServiceError) as error:
        service.invoke("recipe.discover", {"registry_id": "public-marketplace"}, OWNER)
    assert error.value.code == "capability_unavailable"
    with pytest.raises(ServiceError) as error:
        service.invoke("recipe.publish", {}, OWNER)
    assert error.value.code == "forbidden"


class ContractBackend:
    """Mock only for service boundary tests. No real execution is asserted here."""
    def __init__(self):
        self.calls = []
    def prepare_execution(self, operation, payload):
        return operation, payload

    def launch_prepared(self, prepared, authorize=None):
        if authorize:
            authorize()
        return self.invoke(*prepared, None)

    def invoke(self, operation, payload, actor):
        self.calls.append((operation, deepcopy(payload), actor))
        if operation in {"run.submit", "run.resume"}:
            return {"operation_id": payload["operation_id"], "backend_run_id": "zenml-native-run", "status": "submitted"}
        if operation == "run.status":
            return {"backend_run_id": "zenml-native-run", "status": "running", "metrics": {"validation_loss": 0.2}}
        if operation == "run.cancel":
            return {"backend_run_id": "zenml-native-run", "status": "cancel_requested", "confirmed": False}
        return {"versions": [], "artifacts": []}


def test_backend_correlation_authority_idempotency_and_cancellation(service):
    backend = ContractBackend()
    service.backend = backend
    p = project(service)
    r = recipe(service, p)
    payload = {"project_id": p["project_id"], "recipe_id": r["recipe_id"], "expected_revision": 1,
               "trusted": True, "idempotency_key": "submit"}
    run = service.invoke("run.submit", payload, OWNER)
    assert service.invoke("run.submit", payload, OWNER) == run
    assert len(backend.calls) == 1
    assert run["backend_run_id"] == "zenml-native-run"
    status = service.invoke("run.status", {"project_id": p["project_id"], "operation_id": run["operation_id"]}, OWNER)
    assert status["status"] == "running"
    assert status["metrics"]["validation_loss"] == 0.2
    cancelled = service.invoke("run.cancel", {"project_id": p["project_id"], "operation_id": run["operation_id"], "idempotency_key": "cancel"}, OWNER)
    assert cancelled["status"] == "cancel_requested"
    assert cancelled["confirmed"] is False
    stored = service.store.get("operation", run["operation_id"])
    assert "metrics" not in stored  # application stores correlation, not a competing run registry
    assert stored["last_reconciled_at"]


def test_remote_untrusted_stale_submit_blocked_before_backend(service):
    backend = ContractBackend()
    service.backend = backend
    p = project(service)
    r = recipe(service, p)
    base = {"project_id": p["project_id"], "recipe_id": r["recipe_id"], "expected_revision": 1, "trusted": True, "idempotency_key": "s"}
    for change in ({"target": "cloud"}, {"trusted": False}, {"expected_revision": 2}):
        with pytest.raises(ServiceError):
            service.invoke("run.submit", {**base, **change}, OWNER)
    assert not backend.calls


def test_http_routes_auth_conflict_and_catalog_same_service(service):
    client = TestClient(create_app(service, {"owner-token": OWNER, "reader-token": READER}))
    assert client.get("/health").status_code == 200
    assert client.get("/v1/projects").status_code == 401
    assert client.get("/v1/catalog").status_code == 401
    headers = {"Authorization": "Bearer owner-token"}
    catalog = client.get("/v1/catalog", headers=headers).json()["operations"]
    assert {entry["name"] for entry in catalog} == set(OPERATIONS)
    p = client.post("/v1/projects", json={"slug": "http", "model_name": "HTTP model", "idempotency_key": "http"}, headers=headers).json()
    assert client.get(f"/v1/projects/{p['project_id']}", headers=headers).json() == p
    payload = {"project_id": p["project_id"], "changes": {"description": "change"}, "expected_revision": 999, "idempotency_key": "bad"}
    conflict = client.post("/v1/operations/project.update", json=payload, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "revision_conflict"
    readonly = client.post("/v1/projects", json={"slug": "other", "model_name": "Other", "idempotency_key": "other"},
                           headers={"Authorization": "Bearer reader-token"})
    assert readonly.status_code == 403


def test_no_auth_configuration_fails_closed(service):
    client = TestClient(create_app(service, {}))
    assert client.get("/health").json()["authentication_configured"] is False
    assert client.get("/v1/projects", headers={"Authorization": "Bearer guessed"}).status_code == 401


def test_token_scopes_cannot_be_supplied_in_request(service):
    p = project(service)
    with pytest.raises(ServiceError):
        service.invoke("project.get", {"project_id": p["project_id"], "actor": {"scopes": ["modify"]}}, READER)
    with pytest.raises(ValueError):
        TokenAuth({"test": {"id": "evil", "scopes": ["admin"]}})


def test_empty_or_invalid_request_not_saved(service):
    for payload in ({"slug": "x", "model_name": "x"}, {"slug": "X space", "model_name": "x", "idempotency_key": "x"}):
        with pytest.raises(ServiceError):
            service.invoke("project.create", payload, OWNER)
    assert not service.invoke("project.list", {}, OWNER)["projects"]


@pytest.mark.asyncio
async def test_chunked_oversized_body_rejected_without_reading_entire_stream():
    from modelbuilder.api import BoundedRequestBody
    calls, consumed, output = [], [], []
    async def app(scope, receive, send):
        calls.append(True)
    chunks = iter([b"1234", b"5678", b"never-read"])
    async def receive():
        chunk = next(chunks)
        consumed.append(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}
    async def send(message):
        output.append(message)
    await BoundedRequestBody(app, limit=6)({"type": "http", "method": "POST", "headers": []}, receive, send)
    assert consumed == [b"1234", b"5678"]
    assert calls == []
    assert output[0]["status"] == 413


@pytest.mark.asyncio
async def test_chunked_bounded_body_preserves_json_for_transport():
    from modelbuilder.api import BoundedRequestBody
    seen = []
    async def app(scope, receive, send):
        seen.append(await receive())
    chunks = iter([{"type": "http.request", "body": b'{"x":', "more_body": True},
                   {"type": "http.request", "body": b'1}', "more_body": False}])
    async def receive():
        return next(chunks)
    async def send(message):
        pass
    await BoundedRequestBody(app, limit=20)({"type": "http", "method": "POST", "headers": []}, receive, send)
    assert seen == [{"type": "http.request", "body": b'{"x":1}', "more_body": False}]


def test_model_ids_cannot_cross_project_before_backend(service):
    # Native run identity lookups are checked against locally persisted project correlations.
    backend = ContractBackend()
    service.backend = backend
    a, b = project(service, "first"), project(service, "second")
    r = recipe(service, a)
    run = service.invoke("run.submit", {"project_id": a["project_id"], "recipe_id": r["recipe_id"],
                         "expected_revision": 1, "trusted": True, "idempotency_key": "submit"}, OWNER)
    backend.calls.clear()
    for operation in ("run.status", "run.logs", "run.artifacts", "model.checkpoint.get"):
        with pytest.raises(ServiceError) as error:
            service.invoke(operation, {"project_id": b["project_id"], "backend_run_id": run["backend_run_id"]}, OWNER)
        assert error.value.code == "not_found"
    assert backend.calls == []


@pytest.mark.parametrize("bad", [{"request_id": {}}, {"project_id": []}, {"project_id": {}, "request_id": []}])
def test_invalid_audit_metadata_returns_structured_error(service, bad):
    with pytest.raises(ServiceError) as error:
        service.invoke("project.get", bad, OWNER)
    assert error.value.code == "validation_error"


def test_live_evidence_metadata_is_not_automatically_persisted(service, monkeypatch):
    p = project(service)
    records = [{"title": "Metadata only", "source_url": "https://openalex.org/W123"}]
    monkeypatch.setattr("modelbuilder.literature.search_openalex", lambda query, limit: records)
    result = service.invoke("evidence.search", {"project_id": p["project_id"], "provider": "openalex", "query": "calibration"}, OWNER)
    assert result["evidence"] == records
    assert result["persisted"] is False
    assert result["claim_verified"] is False
    assert service.invoke("evidence.search", {"project_id": p["project_id"]}, OWNER)["evidence"] == []


def test_unavailable_scholarly_source_fails_truthfully(service, monkeypatch):
    from modelbuilder.literature import LiteratureUnavailable
    p = project(service)
    def fail(query, limit):
        raise LiteratureUnavailable("OpenAlex unavailable")
    monkeypatch.setattr("modelbuilder.literature.search_openalex", fail)
    with pytest.raises(ServiceError) as error:
        service.invoke("evidence.search", {"project_id": p["project_id"], "provider": "openalex", "query": "calibration"}, OWNER)
    assert error.value.code == "source_unavailable"
    assert error.value.status_code == 503


def test_openapi_documents_the_same_typed_operation_payloads(service):
    schema = create_app(service, {}).openapi()
    for name, operation in OPERATIONS.items():
        endpoint = schema["paths"][f"/v1/operations/{name}"]["post"]
        assert endpoint["requestBody"]["content"]["application/json"]["schema"] == operation.input_schema
        assert endpoint["x-required-scope"] == operation.scope
        assert endpoint["security"] == [{"BearerAuth": []}]
    assert schema["components"]["schemas"]["ModelProject"]["properties"]["model_name"]


@pytest.mark.parametrize("operation", ["run.plan", "run.submit", "run.resume"])
def test_silent_execution_parameter_overrides_are_rejected(service, operation):
    p = project(service)
    r = recipe(service, p)
    payload = {"project_id": p["project_id"], "parameters": {"steps": 999}}
    if operation in {"run.plan", "run.submit"}:
        payload["recipe_id"] = r["recipe_id"]
    if operation == "run.submit":
        payload.update(expected_revision=1, trusted=True, idempotency_key="submit")
    if operation == "run.resume":
        payload.update(operation_id="not-executed", mode="exact_state", steps=999, trusted=True, idempotency_key="resume")
    with pytest.raises(ServiceError) as error:
        service.invoke(operation, payload, OWNER)
    assert error.value.code == "validation_error"


def test_execute_only_cannot_resume_and_modify_canonical_recipe(service):
    backend = ContractBackend()
    service.backend = backend
    p = project(service)
    current = recipe(service, p)
    run = service.invoke("run.submit", {"project_id": p["project_id"], "recipe_id": current["recipe_id"],
                         "expected_revision": current["revision"], "trusted": True, "idempotency_key": "submit"}, OWNER)
    backend.calls.clear()
    executor = Actor("executor", {"read", "execute"})
    with pytest.raises(ServiceError) as error:
        service.invoke("run.resume", {"project_id": p["project_id"], "operation_id": run["operation_id"],
                       "expected_recipe_revision": current["revision"], "mode": "exact_state", "steps": 6,
                       "trusted": True, "idempotency_key": "resume"}, executor)
    assert error.value.status_code == 403
    assert error.value.details["missing_scopes"] == ["modify"]
    assert service.invoke("recipe.get", {"project_id": p["project_id"], "recipe_id": current["recipe_id"]}, OWNER) == current
    assert backend.calls == []
    entry = next(item for item in service.catalog(OWNER) if item["name"] == "run.resume")
    assert entry["required_scopes"] == ["execute", "modify"]
    assert create_app(service, {}).openapi()["paths"]["/v1/operations/run.resume"]["post"]["x-required-scopes"] == ["execute", "modify"]
