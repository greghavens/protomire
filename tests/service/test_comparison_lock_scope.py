"""Service concurrency contracts, using a blocked nonexecuting backend."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest

from modelbuilder.models import Actor, ServiceError
from modelbuilder.auth import TokenAuth
from modelbuilder.api import create_app
from modelbuilder.service import OperationService


OWNER = Actor("owner", {"read", "modify", "execute"})


class BlockingComparison:
    def __init__(self):
        self.entered, self.release = Event(), Event()
        self.calls = []

    def invoke(self, operation, payload, actor):
        self.calls.append(operation)
        if operation == "run.compare":
            self.entered.set()
            if not self.release.wait(10):
                raise RuntimeError("Test did not release the comparison")
            return {"compatible": True, "selected_run_id": payload["run_ids"][0], "comparison_digest": "verified"}
        if operation == "run.status":
            return {"operation_id": payload["operation_id"], "backend_run_id": payload["backend_run_id"],
                    "status": "succeeded"}
        return {"evaluations": []} if operation == "run.evaluations.list" else {"events": []}


def seed(service):
    project = service.invoke("project.create", {"slug": "comparison", "model_name": "Comparison",
                                               "idempotency_key": "project"}, OWNER)
    recipe = service.store.get("recipe", project["recipe_id"])
    with service.store.transaction():
        for index in range(2):
            operation_id = f"operation-{index}"
            service.store.put("operation", operation_id, project["project_id"], {
                "operation_id": operation_id, "project_id": project["project_id"], "backend_run_id": f"run-{index}",
                "recipe_id": recipe["recipe_id"], "recipe_revision": recipe["revision"],
                "recipe_digest": recipe["digest"], "status": "succeeded", "revision": 1})
    return {"project_id": project["project_id"], "run_ids": ["run-0", "run-1"],
            "metric": "validation_loss", "direction": "minimize"}


@pytest.mark.parametrize("separate_connection", [False, True])
@pytest.mark.parametrize("poll_operation", ["run.evaluations.list", "run.events.list", "run.status"])
def test_comparison_does_not_block_live_polls(tmp_path, separate_connection, poll_operation):
    backend = BlockingComparison()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    payload = seed(service)
    polling = OperationService(tmp_path / "service.sqlite3", backend=backend) if separate_connection else service
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            comparison = pool.submit(service.invoke, "run.compare", payload, OWNER)
            try:
                assert backend.entered.wait(2)
                assert not service.store.connection.in_transaction
                poll = pool.submit(polling.invoke, poll_operation,
                                   {"project_id": payload["project_id"], "operation_id": "operation-0"}, OWNER)
                assert isinstance(poll.result(timeout=2), dict)
                assert not comparison.done()
            finally:
                backend.release.set()
            result = comparison.result(timeout=2)
            assert result["compatible"]
            assert len(result["run_provenance"]) == 2
            assert all("operation_id" not in record for record in result["run_provenance"])
            assert result["configuration_diffs"][0]["diff"] == ""
    finally:
        if separate_connection:
            polling.store.close()
        service.store.close()


def test_comparison_revalidates_execution_identity(tmp_path):
    backend = BlockingComparison()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    payload = seed(service)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            comparison = pool.submit(service.invoke, "run.compare", payload, OWNER)
            try:
                assert backend.entered.wait(2)
                with service.store.transaction():
                    previous = service.store.get("operation", "operation-0")
                    service.store.put("operation", "operation-0", payload["project_id"],
                                      {**previous, "recipe_digest": "changed", "revision": 2}, 1)
            finally:
                backend.release.set()
            with pytest.raises(ServiceError) as error:
                comparison.result(timeout=2)
            assert error.value.code == "comparison_changed"
    finally:
        service.store.close()


def test_comparison_rejects_foreign_run_before_backend(tmp_path):
    backend = BlockingComparison()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    payload = seed(service)
    try:
        with pytest.raises(ServiceError) as error:
            service.invoke("run.compare", {**payload, "run_ids": ["run-0", "foreign-run"]}, OWNER)
        assert error.value.code == "not_found"
        assert backend.calls == []
    finally:
        service.store.close()


class PreparedBackend(BlockingComparison):
    def __init__(self):
        super().__init__()
        self.guard, self.two_prepared = Lock(), Event()
        self.handles, self.applied = [], []
        self.comparisons = 0
        self.service = None

    def prepare_promotion(self, payload, actor):
        with self.guard:
            handle = object()
            self.handles.append(handle)
            if len(self.handles) == 2:
                self.two_prepared.set()
        self.entered.set()
        if not self.release.wait(10):
            raise RuntimeError("Test did not release preparation")
        return handle

    def invoke(self, operation, payload, actor, *, prepared_promotion=None):
        if operation == "model.version.promote":
            assert self.service.store.connection.in_transaction
            assert any(prepared_promotion is handle for handle in self.handles)
            assert "prepared_promotion" not in payload
            self.applied.append(prepared_promotion)
            return {"model_version_id": "model-1", "selected_run_id": "run-0"}
        if operation == "run.compare":
            with self.guard:
                self.comparisons += 1
                if self.comparisons == 2:
                    self.two_prepared.set()
        return super().invoke(operation, payload, actor)


def seed_evidence(service):
    comparison = seed(service)
    project_id = comparison["project_id"]
    project = service.store.get("project", project_id)
    hypothesis = service.invoke("hypothesis.create", {"project_id": project_id, "proposed_change": "Change model",
        "expected_outcome": "Improve score", "acceptance_metric": {"name": "validation_loss"},
        "idempotency_key": "hypothesis"}, OWNER)
    decision_payload = {"project_id": project_id, "hypothesis_id": hypothesis["hypothesis_id"],
        "recipe_id": project["recipe_id"], "recipe_revision": 1, "run_ids": comparison["run_ids"],
        "selection_rule": {"metric": "validation_loss", "direction": "minimize"}, "outcome": "accepted",
        "rationale": "Measured minimum", "idempotency_key": "initial-decision"}
    decision = service.invoke("decision.record", decision_payload, OWNER)
    return {
        "run.compare": comparison,
        "decision.record": {**decision_payload, "idempotency_key": "decision",
                            "selection_rule": {**decision_payload["selection_rule"], "comparison_digest": "verified"}},
        "model.version.promote": {"project_id": project_id, "model_version_id": "model-1",
            "decision_id": decision["decision_id"], "expected_revision": 1, "idempotency_key": "promotion"}}


@pytest.mark.parametrize("operation", ["decision.record", "model.version.promote"])
@pytest.mark.parametrize("separate_connection", [False, True])
def test_evidence_preparation_allows_polls_and_unrelated_writer(tmp_path, operation, separate_connection):
    backend = PreparedBackend()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed_evidence(service)[operation]
    polling = OperationService(tmp_path / "service.sqlite3", backend=backend) if separate_connection else service
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(service.invoke, operation, payload, OWNER)
            try:
                assert backend.entered.wait(2)
                assert not service.store.connection.in_transaction
                for poll_operation in ("run.evaluations.list", "run.events.list", "run.status"):
                    poll = pool.submit(polling.invoke, poll_operation,
                        {"project_id": payload["project_id"], "operation_id": "operation-0"}, OWNER)
                    assert isinstance(poll.result(timeout=2), dict)
                writer = pool.submit(polling.invoke, "project.create",
                    {"slug": "other", "model_name": "Other", "idempotency_key": "other"}, OWNER)
                assert writer.result(timeout=2)["slug"] == "other"
                assert not future.done()
            finally:
                backend.release.set()
            result = future.result(timeout=2)
            if operation == "model.version.promote":
                assert result["model_version_id"] == "model-1"
                assert len(backend.applied) == 1
                assert service.store.get("project", payload["project_id"])["active_model_version_ref"] == "model-1"
            else:
                assert result["selection_rule"]["comparison_digest"] == "verified"
                assert backend.comparisons == 1
    finally:
        if separate_connection:
            polling.store.close()
        service.store.close()


@pytest.mark.parametrize("operation", ["decision.record", "model.version.promote"])
@pytest.mark.parametrize("change", ["project", "provenance", "execution", "source", "research"])
def test_evidence_preparation_rechecks_authoritative_inputs(tmp_path, operation, change):
    backend = PreparedBackend()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed_evidence(service)[operation]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(service.invoke, operation, payload, OWNER)
            try:
                assert backend.entered.wait(2)
                if change == "project":
                    service.invoke("project.update", {"project_id": payload["project_id"], "expected_revision": 1,
                        "changes": {"model_name": "Changed"}, "idempotency_key": "update"}, OWNER)
                else:
                    if change in {"provenance", "execution", "source"}:
                        field = {"provenance": "recipe_digest", "execution": "execution", "source": "source_binding"}[change]
                        kind, resource_id, changes = "operation", "operation-0", {field: "changed"}
                    elif operation == "decision.record":
                        kind, resource_id, changes = "hypothesis", payload["hypothesis_id"], {"expected_outcome": "Changed"}
                    else:
                        kind, resource_id, changes = "decision", payload["decision_id"], {"rationale": "Changed"}
                    with service.store.transaction():
                        previous = service.store.get(kind, resource_id)
                        service.store.put(kind, resource_id, payload["project_id"],
                            {**previous, **changes, "revision": previous["revision"] + 1}, previous["revision"])
            finally:
                backend.release.set()
            with pytest.raises(ServiceError) as error:
                future.result(timeout=2)
            assert error.value.code == ("revision_conflict" if change == "project" and operation == "model.version.promote"
                                        else "comparison_changed")
            assert backend.applied == []
            assert len(service.store.list("decision", payload["project_id"])) == 1
    finally:
        service.store.close()


@pytest.mark.parametrize("operation", ["run.compare", "decision.record", "model.version.promote"])
@pytest.mark.parametrize("change", ["remove", "scopes", "project", "replace", "remove_shared_actor"])
def test_preparation_rechecks_original_authenticated_grant(tmp_path, operation, change):
    backend = PreparedBackend()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed_evidence(service)[operation]
    auth = TokenAuth({"request-token": OWNER,
                      "other-token": OWNER if change == "remove_shared_actor" else Actor("owner", OWNER.scopes)})
    create_app(service, auth)
    actor = auth.authenticate("Bearer request-token")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(service.invoke, operation, payload, actor)
            try:
                assert backend.entered.wait(2)
                if change in {"remove", "remove_shared_actor"}:
                    del auth._tokens["request-token"]
                elif change == "scopes":
                    auth._tokens["request-token"] = Actor("owner", frozenset())
                elif change == "project":
                    auth._tokens["request-token"] = Actor("owner", OWNER.scopes, {"different-project"})
                else:
                    auth._tokens["request-token"] = Actor("owner", OWNER.scopes)
            finally:
                backend.release.set()
            with pytest.raises(ServiceError) as error:
                future.result(timeout=2)
            assert error.value.code == "forbidden"
            assert backend.applied == []
            assert len(service.store.list("decision", payload["project_id"])) == 1
    finally:
        service.store.close()


@pytest.mark.parametrize("operation", ["decision.record", "model.version.promote"])
def test_duplicate_preparations_commit_once_and_replay_without_preparation(tmp_path, operation):
    backend = PreparedBackend()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed_evidence(service)[operation]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.invoke, operation, payload, OWNER)
            try:
                assert backend.entered.wait(2)
                second = pool.submit(service.invoke, operation, payload, OWNER)
                assert backend.two_prepared.wait(2)
            finally:
                backend.release.set()
            assert first.result(timeout=2) == second.result(timeout=2)
            assert service.invoke(operation, {**payload, "request_id": "retry"}, OWNER) == first.result()
        assert len(backend.applied) == (1 if operation == "model.version.promote" else 0)
        assert len(backend.handles) == (2 if operation == "model.version.promote" else 0)
        assert backend.comparisons == (2 if operation == "decision.record" else 0)
        history = service.invoke("project.history", {"project_id": payload["project_id"]}, OWNER)["events"]
        assert sum(event["operation"] == operation and event["outcome"] == "succeeded" for event in history) == (
            2 if operation == "decision.record" else 1)
    finally:
        service.store.close()


@pytest.mark.parametrize("operation", ["decision.record", "model.version.promote"])
def test_public_request_cannot_supply_prepared_evidence(tmp_path, operation):
    backend = PreparedBackend()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed_evidence(service)[operation]
    try:
        for field in ("prepared_evidence", "prepared_promotion", "_prepared_promotion"):
            with pytest.raises(ServiceError) as error:
                service.invoke(operation, {**payload, field: "forged"}, OWNER)
            assert error.value.code == "validation_error"
        assert not backend.entered.is_set()
        assert backend.applied == []
    finally:
        service.store.close()


@pytest.mark.parametrize("missing_handle", [False, True])
def test_promotion_fails_closed_without_backend_preparation(tmp_path, missing_handle):
    backend = BlockingComparison()
    if missing_handle:
        backend.prepare_promotion = lambda payload, actor: None
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    payload = seed_evidence(service)["model.version.promote"]
    try:
        with pytest.raises(ServiceError) as error:
            service.invoke("model.version.promote", payload, OWNER)
        assert error.value.code == ("backend_contract_error" if missing_handle else "capability_unavailable")
        assert error.value.status_code == (502 if missing_handle else 503)
        assert backend.calls == []
        assert service.store.get("project", payload["project_id"])["revision"] == 1
    finally:
        service.store.close()


def test_competing_promotion_keys_cannot_both_use_same_project_revision(tmp_path):
    backend = PreparedBackend()
    service = OperationService(tmp_path / "service.sqlite3", backend=backend)
    backend.service = service
    payload = seed_evidence(service)["model.version.promote"]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.invoke, "model.version.promote", payload, OWNER)
            try:
                assert backend.entered.wait(2)
                second = pool.submit(service.invoke, "model.version.promote",
                    {**payload, "idempotency_key": "competing", "stage": "production"}, OWNER)
                assert backend.two_prepared.wait(2)
            finally:
                backend.release.set()
            successes, failures = [], []
            for future in (first, second):
                try:
                    successes.append(future.result(timeout=2))
                except ServiceError as error:
                    failures.append(error.code)
        assert len(successes) == 1 and failures == ["revision_conflict"]
        assert len(backend.applied) == 1
        assert service.store.get("project", payload["project_id"])["revision"] == 2
    finally:
        service.store.close()
