"""Research workflow contracts use deterministic metadata, never live feeds.

These prove real SQLite/service/HTTP behavior. They do not claim scientific
verification, automated hosting, scheduler deployment or training acceptance.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import io
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from modelbuilder.api import create_app
from modelbuilder.auth import TokenAuth
from modelbuilder.models import Actor, OPERATIONS, ServiceError
from modelbuilder.service import OperationService

OWNER = Actor("research-owner", {"read", "modify", "execute"})


@pytest.fixture
def service(tmp_path):
    app = OperationService(tmp_path / "research.sqlite3")
    yield app
    app.store.close()


def invoke(service, operation, p, actor=OWNER):
    if OPERATIONS[operation].mutating:
        p = {"idempotency_key": str(uuid4()), **p}
    return service.invoke(operation, p, actor)


def project(service, slug="research"):
    return invoke(service, "project.create", {"slug": slug, "model_name": "Research model"})


def watch(service, project_id, **fields):
    return invoke(service, "research.watch.create", {"project_id": project_id, "name": "Calibration",
                  "query": "calibration", **fields})


def refresh(service, w, **fields):
    return invoke(service, "research.watch.refresh", {"project_id": w["project_id"], "watch_id": w["watch_id"],
                  "expected_revision": w["revision"], **fields})


def evidence(service, project_id, identity="1", **fields):
    return invoke(service, "evidence.add", {"project_id": project_id, "title": "Metadata citation " + identity,
                  "source_identifier": "https://openalex.org/W" + identity,
                  "source_url": "https://openalex.org/W" + identity, **fields})


def hypothesis(service, project_id, citations, **fields):
    return invoke(service, "hypothesis.create", {"project_id": project_id,
                  "evidence_ids": [item["evidence_id"] for item in citations],
                  "proposed_change": "Evaluate a calibrated checkpoint", "expected_outcome": "Lower Brier score",
                  "acceptance_metric": {"name": "brier", "direction": "minimize"}, **fields})


def proposed(service, p=None, e=None, h=None, **fields):
    p = p or project(service)
    e = e or evidence(service, p["project_id"])
    h = h or hypothesis(service, p["project_id"], [e])
    r = invoke(service, "recipe.get", {"project_id": p["project_id"], "recipe_id": p["recipe_id"]})
    changed = deepcopy(r)
    changed["description"] = "Controlled calibration experiment"
    proposal = invoke(service, "research.proposal.create", {"project_id": p["project_id"],
        "hypothesis_id": h["hypothesis_id"], "title": "Try calibration", "rationale": "Test a cited method locally",
        "citations": [{"evidence_id": e["evidence_id"]}], "recipe": changed,
        "expected_recipe_revision": r["revision"], **fields})
    return p, e, h, proposal


def review(service, p, proposal, outcome="approved", **fields):
    return invoke(service, "research.proposal.review", {"project_id": p["project_id"],
        "proposal_id": proposal["proposal_id"], "expected_revision": proposal["revision"],
        "outcome": outcome, "rationale": "Reviewed exact staged diff and evidence limits", **fields})


def apply(service, p, proposal, **fields):
    return invoke(service, "research.proposal.apply", {"project_id": p["project_id"],
        "proposal_id": proposal["proposal_id"], "expected_revision": proposal["revision"],
        "expected_recipe_revision": proposal["base_recipe_revision"], "confirm_apply": True, **fields})


def metadata(identity="1", title="A calibration method"):
    return {"title": title, "source_identifier": "https://openalex.org/W" + identity,
            "source_url": "https://openalex.org/W" + identity, "authors": ["A. Author"],
            "citation_metadata": {"provider": "OpenAlex", "claim_verified": False, "metadata_only": True}}


def test_watch_creation_and_reads_do_not_fetch_or_launch_scheduler(service, monkeypatch):
    monkeypatch.setattr("modelbuilder.literature.search_openalex", lambda *args: pytest.fail("Unexpected fetch"))
    p = project(service)
    w = watch(service, p["project_id"])
    assert w["revision"] == 1 and w["refresh_count"] == 0 and not w["scheduler_running"]
    assert invoke(service, "research.watch.get", {"project_id": p["project_id"], "watch_id": w["watch_id"]})["refresh_history"] == []
    assert invoke(service, "research.watch.list", {"project_id": p["project_id"]})["watches"] == [w]
    assert service.store.list("operation", p["project_id"]) == []


def test_refresh_deduplicates_and_preserves_changed_evidence_history(service, monkeypatch):
    records = [metadata(), metadata()]
    monkeypatch.setattr("modelbuilder.literature.search_openalex", lambda *args: deepcopy(records))
    p = project(service)
    w = watch(service, p["project_id"])
    first = refresh(service, w)
    assert len(first["refresh"]["new_findings"]) == 1 and first["refresh"]["unchanged"] == 1
    second = refresh(service, first["watch"])
    assert not second["refresh"]["new_findings"] and second["refresh"]["unchanged"] == 2
    records[:] = [metadata(title="Corrected title")]
    third = refresh(service, second["watch"])
    assert len(third["refresh"]["changed_findings"]) == 1
    findings = invoke(service, "research.finding.list", {"project_id": p["project_id"]})["findings"]
    assert len(findings) == 2
    assert findings[1]["previous_finding_id"] == findings[0]["finding_id"]
    assert findings[1]["previous_evidence_id"] == findings[0]["evidence_id"]
    assert "Corrected title" in findings[1]["change_diff"]
    assert service.store.get("evidence", findings[0]["evidence_id"])["title"] == "A calibration method"
    records[:] = [metadata()]
    fourth = refresh(service, third["watch"])
    assert fourth["refresh"]["unchanged"] == 1
    assert len(service.store.list("evidence", p["project_id"])) == 2
    assert not fourth["refresh"]["coverage"]["complete"]
    assert not fourth["refresh"]["coverage"]["deletions_inferred"]


def test_watch_refresh_idempotent_retry_does_not_refetch(service, monkeypatch):
    calls = []
    monkeypatch.setattr("modelbuilder.literature.search_openalex", lambda *args: calls.append(args) or [metadata()])
    p = project(service)
    w = watch(service, p["project_id"])
    first = refresh(service, w, idempotency_key="refresh-once")
    assert refresh(service, w, idempotency_key="refresh-once") == first
    assert len(calls) == 1
    with pytest.raises(ServiceError) as error:
        refresh(service, w)
    assert error.value.code == "revision_conflict" and len(calls) == 1


def test_watch_due_tick_is_bounded_durable_and_disabled_safe(service, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("modelbuilder.literature.search_openalex", lambda *args: calls.append(args) or [])
    p = project(service)
    watches = [watch(service, p["project_id"], name=str(i)) for i in range(4)]
    disabled = watch(service, p["project_id"], enabled=False)
    first = invoke(service, "research.watch.refresh_due", {"project_id": p["project_id"], "max_watches": 2})
    assert first["refreshed"] == 2 and first["remaining_due"] == 2
    second = invoke(service, "research.watch.refresh_due", {"project_id": p["project_id"], "max_watches": 5})
    assert second["refreshed"] == 2 and len(calls) == 4
    third = invoke(service, "research.watch.refresh_due", {"project_id": p["project_id"]})
    assert third["refreshed"] == 0
    with pytest.raises(ServiceError) as error:
        refresh(service, disabled)
    assert error.value.code == "watch_disabled"
    restarted = OperationService(service.store.path)
    try:
        assert invoke(restarted, "research.watch.refresh_due", {"project_id": p["project_id"]})["refreshed"] == 0
        assert len(invoke(restarted, "research.watch.list", {"project_id": p["project_id"]})["watches"]) == len(watches) + 1
    finally:
        restarted.store.close()


def test_failed_refresh_is_persisted_and_can_recover(service, monkeypatch):
    from modelbuilder.literature import LiteratureUnavailable
    def failed(*args):
        raise LiteratureUnavailable("private URL or token must not be saved")
    monkeypatch.setattr("modelbuilder.literature.search_openalex", failed)
    p = project(service)
    w = watch(service, p["project_id"])
    failure = refresh(service, w)
    assert failure["refresh"]["status"] == "failed"
    assert "private URL" not in json.dumps(failure)
    assert failure["watch"]["revision"] == 2
    assert service.store.list("research_refresh", p["project_id"])[0]["status"] == "failed"
    assert not service.store.list("evidence", p["project_id"])
    monkeypatch.setattr("modelbuilder.literature.search_openalex", lambda *args: [metadata()])
    result = refresh(service, failure["watch"])
    assert result["refresh"]["status"] == "succeeded"
    assert len(service.store.list("research_refresh", p["project_id"])) == 2


@pytest.mark.parametrize("bad", [[{"title": "No identity"}], [{**metadata(), "source_url": "file:///etc/passwd"}],
                                    [metadata(), {"title": "Broken"}], [{**metadata(), "api_key": "must-not-persist"}]])
def test_invalid_provider_response_never_partially_persists(service, monkeypatch, bad):
    monkeypatch.setattr("modelbuilder.literature.search_openalex", lambda *args: bad)
    p = project(service)
    result = refresh(service, watch(service, p["project_id"]))
    assert result["refresh"]["status"] == "failed"
    assert not service.store.list("evidence", p["project_id"])
    assert not service.store.list("research_finding", p["project_id"])


def test_provider_cannot_mark_claim_verified(service, monkeypatch):
    monkeypatch.setattr("modelbuilder.literature.search_openalex", lambda *args: [
        {**metadata(), "citation_metadata": {"claim_verified": True, "metadata_only": False}}])
    p = project(service)
    refresh(service, watch(service, p["project_id"]))
    saved = service.store.list("evidence", p["project_id"])[0]
    assert saved["citation_metadata"]["claim_verified"] is False
    assert saved["citation_metadata"]["metadata_only"] is True


def test_finding_review_is_versioned_and_has_no_scientific_side_effects(service, monkeypatch):
    monkeypatch.setattr("modelbuilder.literature.search_openalex", lambda *args: [metadata()])
    p = project(service)
    refresh(service, watch(service, p["project_id"]))
    finding = service.store.list("research_finding", p["project_id"])[0]
    result = invoke(service, "research.finding.review", {"project_id": p["project_id"],
        "finding_id": finding["finding_id"], "expected_revision": 1, "status": "reviewed", "rationale": "Worth evaluating"})
    assert result["revision"] == 2 and not result["review"]["claim_verified"]
    assert service.store.get("research_finding", finding["finding_id"], 1) == finding
    assert service.store.list("claim", p["project_id"]) == []
    with pytest.raises(ServiceError) as error:
        invoke(service, "research.finding.review", {"project_id": p["project_id"],
            "finding_id": finding["finding_id"], "expected_revision": 1, "status": "dismissed", "rationale": "Stale edit"})
    assert error.value.code == "revision_conflict"


@pytest.mark.parametrize("fields", [{"query": "   "}, {"name": " "}, {"interval_seconds": 60},
                                    {"limit": 11}, {"provider": "unconfigured"}])
def test_watch_bounds_rejected(service, fields):
    p = project(service)
    with pytest.raises(ServiceError) as error:
        watch(service, p["project_id"], **fields)
    assert error.value.code == "validation_error"


def test_watch_updates_keep_history_and_reset_due_on_query_change(service, monkeypatch):
    monkeypatch.setattr("modelbuilder.literature.search_openalex", lambda *args: [])
    p = project(service)
    original = watch(service, p["project_id"])
    w = refresh(service, original)["watch"]
    changed = invoke(service, "research.watch.update", {"project_id": p["project_id"], "watch_id": w["watch_id"],
        "expected_revision": w["revision"], "changes": {"query": "new search", "enabled": False}})
    assert changed["query"] == "new search" and not changed["enabled"]
    assert changed["next_due_at"] < w["next_due_at"]
    assert service.store.get("research_watch", w["watch_id"], 1) == original


def test_text_import_pins_user_supplied_rights_and_exact_passage(service):
    p = project(service)
    e = evidence(service, p["project_id"])
    text = "α Beta. This result still requires local testing."
    imported = invoke(service, "research.text.import", {"project_id": p["project_id"], "evidence_id": e["evidence_id"],
        "expected_evidence_revision": 1, "text": text, "license": "CC-BY-4.0", "rights_basis": "licensed",
        "rights_confirmed": True, "expected_sha256": hashlib.sha256(text.encode()).hexdigest()})
    assert imported["rights_validation"] == "user_attested"
    assert imported["byte_length"] == len(text.encode()) and not imported["claim_verified"]
    assert invoke(service, "research.text.get", {"project_id": p["project_id"], "text_id": imported["text_id"]}) == imported
    quote = "This result still requires local testing."
    start = text.index(quote)
    _, _, _, proposal = proposed(service, p, e, citations=[{"evidence_id": e["evidence_id"],
        "text_id": imported["text_id"], "quote": quote, "start": start, "end": len(text)}])
    citation = proposal["citations"][0]
    assert citation["validation_status"] == "passage_matched_user_supplied_text"
    assert citation["text_sha256"] == imported["sha256"] and not citation["claim_verified"]
    with pytest.raises(ServiceError) as error:
        proposed(service, p, e, citations=[{"evidence_id": e["evidence_id"], "text_id": imported["text_id"],
                                          "quote": "Invented conclusion", "start": start, "end": len(text)}])
    assert error.value.code == "passage_mismatch"


@pytest.mark.parametrize("fields,code", [({"rights_confirmed": False}, "validation_error"),
    ({"license": " "}, "rights_required"), ({"expected_sha256": "0" * 64}, "content_hash_mismatch"),
    ({"text": "é" * 100001}, "validation_error"), ({"text": "\x00bad"}, "validation_error"),
    ({"text": " "}, "validation_error"), ({"expected_evidence_revision": 2}, "revision_conflict")])
def test_text_import_bounds_rights_and_identity(service, fields, code):
    p = project(service)
    e = evidence(service, p["project_id"])
    with pytest.raises(ServiceError) as error:
        invoke(service, "research.text.import", {"project_id": p["project_id"], "evidence_id": e["evidence_id"],
            "expected_evidence_revision": 1, "text": "User supplied source", "license": "CC0-1.0",
            "rights_basis": "public_domain", "rights_confirmed": True, **fields})
    assert error.value.code == code
    assert service.store.list("research_text", p["project_id"]) == []


def test_text_reimport_deduplicates_and_rejects_rights_relabel(service):
    p = project(service)
    e = evidence(service, p["project_id"])
    body = {"project_id": p["project_id"], "evidence_id": e["evidence_id"], "expected_evidence_revision": 1,
            "text": "User supplied source", "license": "CC0-1.0", "rights_basis": "public_domain", "rights_confirmed": True}
    first = invoke(service, "research.text.import", body)
    assert invoke(service, "research.text.import", body) == first
    with pytest.raises(ServiceError) as error:
        invoke(service, "research.text.import", {**body, "license": "CC-BY-4.0"})
    assert error.value.code == "rights_conflict"


def test_proposal_staging_review_and_apply_are_distinct_atomic_steps(service):
    p, e, h, proposal = proposed(service)
    original = service.store.get("recipe", p["recipe_id"])
    assert original["revision"] == 1 and proposal["status"] == "proposed"
    assert proposal["citations"][0]["validation_status"] == "citation_only"
    assert proposal["base_recipe_digest"] == original["digest"]
    assert "Controlled calibration experiment" in proposal["recipe_diff"]
    assert proposal["staged_recipe"]["revision"] == 2
    with pytest.raises(ServiceError) as error:
        apply(service, p, proposal)
    assert error.value.code == "proposal_review_required"
    approved = review(service, p, proposal)
    assert approved["review"]["proposal_digest"] == proposal["proposal_digest"]
    assert service.store.get("recipe", p["recipe_id"])["revision"] == 1
    result = apply(service, p, approved, idempotency_key="apply-once")
    assert result["recipe"]["recipe_id"] == p["recipe_id"] and result["recipe"]["revision"] == 2
    assert result["recipe"]["digest"] == proposal["staged_recipe_digest"]
    assert result["proposal"]["applied"]["recipe_digest"] == result["recipe"]["digest"]
    assert result["proposal"]["status"] == "applied" and not result["execution_started"]
    assert apply(service, p, approved, idempotency_key="apply-once") == result
    assert service.store.get("recipe", p["recipe_id"], 1) == original
    assert service.store.list("operation", p["project_id"]) == []
    assert service.store.get("hypothesis", h["hypothesis_id"])["status"] == "proposed"
    assert len(service.store.resource_versions("research_proposal", proposal["proposal_id"])) == 3


def test_apply_requires_explicit_confirmation(service):
    p, _, _, proposal = proposed(service)
    approved = review(service, p, proposal)
    with pytest.raises(ServiceError) as error:
        apply(service, p, approved, confirm_apply=False)
    assert error.value.code == "validation_error"
    assert service.store.get("recipe", p["recipe_id"])["revision"] == 1


def test_recipe_change_after_review_blocks_apply_without_overwrite(service):
    p, _, _, proposal = proposed(service)
    approved = review(service, p, proposal)
    current = service.store.get("recipe", p["recipe_id"])
    invoke(service, "recipe.update", {"project_id": p["project_id"], "recipe_id": p["recipe_id"],
        "expected_revision": 1, "recipe": {**current, "description": "Other editor won"}})
    with pytest.raises(ServiceError) as error:
        apply(service, p, approved, expected_recipe_revision=2)
    assert error.value.code == "revision_conflict"
    assert service.store.get("recipe", p["recipe_id"])["description"] == "Other editor won"
    assert service.store.get("research_proposal", proposal["proposal_id"])["status"] == "approved"


def test_hypothesis_change_stales_review_and_apply(service):
    p, _, h, proposal = proposed(service)
    approved = review(service, p, proposal)
    invoke(service, "hypothesis.update", {"project_id": p["project_id"], "hypothesis_id": h["hypothesis_id"],
        "expected_revision": h["revision"], "changes": {"expected_outcome": "Changed outcome"}})
    with pytest.raises(ServiceError) as error:
        apply(service, p, approved)
    assert error.value.code == "proposal_stale"
    assert service.store.get("recipe", p["recipe_id"])["revision"] == 1


def test_claim_change_stales_approval_but_stale_proposal_can_be_rejected(service):
    p = project(service)
    e = evidence(service, p["project_id"])
    claim = invoke(service, "claim.create", {"project_id": p["project_id"], "evidence_ids": [e["evidence_id"]],
                                             "proposition": "Source reports a possible improvement"})
    h = hypothesis(service, p["project_id"], [e], claim_ids=[claim["claim_id"]])
    _, _, _, proposal = proposed(service, p, e, h)
    invoke(service, "claim.update", {"project_id": p["project_id"], "claim_id": claim["claim_id"],
        "expected_revision": 1, "changes": {"review_status": "contradicted"}})
    with pytest.raises(ServiceError) as error:
        review(service, p, proposal)
    assert error.value.code == "proposal_stale"
    assert review(service, p, proposal, "rejected")["status"] == "rejected"


def test_rejected_proposal_cannot_be_silently_reapproved_or_repeated(service):
    p, e, h, proposal = proposed(service)
    rejected = review(service, p, proposal, "rejected")
    with pytest.raises(ServiceError) as error:
        review(service, p, rejected)
    assert error.value.code == "proposal_state_conflict"
    with pytest.raises(ServiceError) as error:
        proposed(service, p, e, h)
    assert error.value.code == "new_evidence_required"
    # An identical citation recreated under a new UUID is still old evidence.
    duplicate = evidence(service, p["project_id"])
    with pytest.raises(ServiceError) as error:
        proposed(service, p, e, h, citations=[{"evidence_id": e["evidence_id"]}, {"evidence_id": duplicate["evidence_id"]}],
                 reconsideration_reason="Recreated citation")
    assert error.value.code in {"new_evidence_required", "duplicate_citation"}
    fresh = evidence(service, p["project_id"], "2")
    with pytest.raises(ServiceError) as error:
        proposed(service, p, e, h, citations=[{"evidence_id": e["evidence_id"]}, {"evidence_id": fresh["evidence_id"]}])
    assert error.value.code == "new_evidence_required"
    _, _, _, reconsidered = proposed(service, p, e, h,
        citations=[{"evidence_id": e["evidence_id"]}, {"evidence_id": fresh["evidence_id"]}],
        reconsideration_reason="New source addresses earlier failure; still requires a controlled experiment")
    assert reconsidered["prior_rejections"][0]["id"] == rejected["proposal_id"]
    assert review(service, p, reconsidered)["status"] == "approved"


def test_rejected_hypothesis_needs_new_evidence_even_with_new_proposal(service):
    p = project(service)
    e = evidence(service, p["project_id"])
    h = hypothesis(service, p["project_id"], [e], status="rejected")
    with pytest.raises(ServiceError) as error:
        proposed(service, p, e, h)
    assert error.value.code == "new_evidence_required"


def test_rejection_recorded_after_staging_blocks_late_approval(service):
    p, e, h, first = proposed(service)
    _, _, _, second = proposed(service, p, e, h)
    review(service, p, first, "rejected")
    with pytest.raises(ServiceError) as error:
        review(service, p, second)
    assert error.value.code == "new_evidence_required"


def test_noop_and_recipe_identity_change_proposals_are_rejected(service):
    p = project(service)
    e = evidence(service, p["project_id"])
    r = service.store.get("recipe", p["recipe_id"])
    with pytest.raises(ServiceError) as error:
        proposed(service, p, e, recipe=r)
    assert error.value.code == "empty_proposal"
    with pytest.raises(ServiceError) as error:
        proposed(service, p, e, recipe={**r, "recipe_id": "another-recipe"})
    assert error.value.code == "validation_error"


def test_proposal_and_watch_project_isolation(service):
    p, e, h, proposal = proposed(service)
    q = project(service, "other")
    scoped = Actor("other-editor", {"read", "modify"}, {q["project_id"]})
    for op, fields in [("research.proposal.get", {"proposal_id": proposal["proposal_id"]}),
                       ("research.watch.get", {"watch_id": watch(service, p["project_id"])["watch_id"]})]:
        with pytest.raises(ServiceError) as error:
            invoke(service, op, {"project_id": q["project_id"], **fields}, scoped)
        assert error.value.code == "not_found"
        with pytest.raises(ServiceError) as error:
            invoke(service, op, {"project_id": p["project_id"], **fields}, scoped)
        assert error.value.code == "forbidden"
    other_h = hypothesis(service, q["project_id"], [])
    with pytest.raises(ServiceError) as error:
        proposed(service, q, e, other_h)
    assert error.value.code == "not_found"


def test_concurrent_apply_has_single_revision_winner(service):
    p, e, h, first = proposed(service)
    _, _, _, second = proposed(service, p, e, h, title="Alternative staged change")
    approved = [review(service, p, value) for value in (first, second)]
    def apply_one(proposal):
        connection = OperationService(service.store.path)
        try:
            return apply(connection, p, proposal)
        except ServiceError as error:
            return error.code
        finally:
            connection.store.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply_one, approved))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("revision_conflict") == 1
    assert service.store.get("recipe", p["recipe_id"])["revision"] == 2
    assert len(service.store.list("recipe", p["project_id"])) == 1


def test_openalex_adapter_to_persisted_watch_uses_bounded_real_parser(service, monkeypatch):
    from modelbuilder import literature
    payload = {"results": [{"id": "https://openalex.org/W123", "title": "Local parser fixture", "doi": None,
                            "authorships": [{"author": {"display_name": "A. Author"}}]}]}
    requests = []
    def open_fixture(request, timeout):
        requests.append((request.full_url, timeout))
        return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(literature, "urlopen", open_fixture)
    p = project(service)
    w = watch(service, p["project_id"], limit=1)
    result = refresh(service, w)
    assert result["refresh"]["status"] == "succeeded"
    assert "per-page=1" in requests[0][0] and requests[0][1] == 20
    saved = service.store.list("evidence", p["project_id"])[0]
    assert saved["source_identifier"] == "https://openalex.org/W123"
    assert saved["title"] == "Local parser fixture" and not saved["citation_metadata"]["claim_verified"]


def test_research_http_contract_and_auth(service):
    app = create_app(service, TokenAuth({"owner-token": OWNER, "reader-token": Actor("reader", {"read"})}))
    with TestClient(app) as client:
        catalog = client.get("/v1/catalog", headers={"Authorization": "Bearer owner-token"}).json()
        operations = catalog.get("operations", catalog.get("tools", []))
        assert any(spec["name"] == "research.proposal.apply" for spec in operations)
        p = project(service)
        body = {"project_id": p["project_id"], "name": "HTTP query", "query": "calibration", "idempotency_key": "http"}
        response = client.post("/v1/operations/research.watch.create", json=body, headers={"Authorization": "Bearer owner-token"})
        assert response.status_code == 200
        denied = client.post("/v1/operations/research.watch.create", json=body, headers={"Authorization": "Bearer reader-token"})
        assert denied.status_code == 403
        assert service.store.list("research_watch", p["project_id"])[0]["watch_id"] == response.json()["watch_id"]


def test_overlapping_watches_share_identical_citation_snapshots(service, monkeypatch):
    monkeypatch.setattr("modelbuilder.literature.search_openalex", lambda *args: [metadata()])
    p = project(service)
    refresh(service, watch(service, p["project_id"], query="first query"))
    refresh(service, watch(service, p["project_id"], query="second query"))
    findings = service.store.list("research_finding", p["project_id"])
    assert len(findings) == 2 and findings[0]["evidence_id"] == findings[1]["evidence_id"]
    assert len(service.store.list("evidence", p["project_id"])) == 1


def test_applied_recipe_contains_reviewed_provenance(service):
    p, e, h, proposal = proposed(service)
    staged = proposal["staged_recipe"]
    assert staged["extensions"]["modelbuilder_research"]["proposal_id"] == proposal["proposal_id"]
    assert staged["extensions"]["modelbuilder_research"]["hypothesis_id"] == h["hypothesis_id"]
    assert e["evidence_id"] in staged["evidence_links"]
    applied = apply(service, p, review(service, p, proposal))
    assert applied["recipe"]["extensions"] == staged["extensions"]


def test_apply_rolls_back_recipe_when_proposal_save_fails(service, monkeypatch):
    p, _, _, proposal = proposed(service)
    approved = review(service, p, proposal)
    put = service.store.put
    def fail_proposal(kind, identity, project_id, body, expected_revision=None):
        if kind == "research_proposal" and body["status"] == "applied":
            raise ValueError("Simulated persistence failure")
        return put(kind, identity, project_id, body, expected_revision)
    monkeypatch.setattr(service.store, "put", fail_proposal)
    with pytest.raises(ServiceError):
        apply(service, p, approved)
    assert service.store.get("recipe", p["recipe_id"])["revision"] == 1
    assert service.store.get("research_proposal", proposal["proposal_id"])["status"] == "approved"
    assert len(service.store.resource_versions("recipe", p["recipe_id"])) == 1


def test_negative_decision_uses_historical_hypothesis_and_claim_snapshots(service):
    p = project(service)
    e = evidence(service, p["project_id"])
    claim = invoke(service, "claim.create", {"project_id": p["project_id"], "evidence_ids": [e["evidence_id"]],
                                             "proposition": "A paper reports a possible improvement"})
    h = hypothesis(service, p["project_id"], [], claim_ids=[claim["claim_id"]])
    r = service.store.get("recipe", p["recipe_id"])
    # This is a contract fixture, explicitly not a real training acceptance.
    with service.store.transaction():
        service.store.put("operation", "contract-operation", p["project_id"], {
            "project_id": p["project_id"], "operation_id": "contract-operation", "backend_run_id": "contract-run",
            "recipe_id": r["recipe_id"], "recipe_revision": r["revision"], "recipe_digest": r["digest"], "revision": 1})
    decision = invoke(service, "decision.record", {"project_id": p["project_id"], "hypothesis_id": h["hypothesis_id"],
        "recipe_id": r["recipe_id"], "recipe_revision": r["revision"], "run_ids": ["contract-run"],
        "selection_rule": {"metric": "brier", "direction": "minimize"}, "outcome": "rejected",
        "rationale": "The contract fixture records an unsuccessful experiment"})
    new_evidence = evidence(service, p["project_id"], "2")
    invoke(service, "hypothesis.update", {"project_id": p["project_id"], "hypothesis_id": h["hypothesis_id"],
        "expected_revision": 1, "changes": {"proposed_change": "Unrelated next question"}})
    invoke(service, "claim.update", {"project_id": p["project_id"], "claim_id": claim["claim_id"],
        "expected_revision": 1, "changes": {"evidence_ids": [new_evidence["evidence_id"]]}})
    equivalent = hypothesis(service, p["project_id"], [e])
    with pytest.raises(ServiceError) as error:
        proposed(service, p, e, equivalent)
    assert error.value.code == "new_evidence_required"
    assert error.value.details["prior_rejections"][0]["id"] == decision["decision_id"]
    _, _, _, retry = proposed(service, p, e, equivalent,
        citations=[{"evidence_id": e["evidence_id"]}, {"evidence_id": new_evidence["evidence_id"]}],
        reconsideration_reason="A later source may explain the failed result")
    assert retry["prior_rejections"][0]["kind"] == "decision"


def test_research_native_mcp_catalog_keeps_shared_schemas_and_scopes(service):
    from modelbuilder.mcp_server import catalog_tools
    tools, aliases = catalog_tools({"operations": service.catalog(OWNER)})
    research = [item for item in tools if aliases[item.name].startswith("research.")]
    assert len(research) == 15
    for tool in research:
        operation = OPERATIONS[aliases[tool.name]]
        assert tool.inputSchema == operation.input_schema
        assert tool.annotations.readOnlyHint is (not operation.mutating)
        assert ("idempotency_key" in tool.inputSchema["required"]) is operation.mutating


def test_periodic_due_refresh_uses_persisted_time_boundary(service, monkeypatch):
    from modelbuilder import research
    clock = ["2026-10-09T00:00:00+00:00"]
    monkeypatch.setattr(research, "utcnow", lambda: clock[0])
    monkeypatch.setattr("modelbuilder.literature.search_openalex", lambda *args: [])
    p = project(service)
    w = watch(service, p["project_id"], interval_seconds=900)
    first = invoke(service, "research.watch.refresh_due", {"project_id": p["project_id"]})
    assert first["refreshed"] == 1
    clock[0] = "2026-10-09T00:14:59+00:00"
    assert invoke(service, "research.watch.refresh_due", {"project_id": p["project_id"]})["refreshed"] == 0
    clock[0] = "2026-10-09T00:15:00+00:00"
    assert invoke(service, "research.watch.refresh_due", {"project_id": p["project_id"]})["refreshed"] == 1
    assert service.store.get("research_watch", w["watch_id"])["refresh_count"] == 2


def test_doi_alias_does_not_turn_a_duplicate_into_new_evidence(service):
    p = project(service)
    e = evidence(service, p["project_id"], source_url="https://doi.org/10.1234/method")
    h = hypothesis(service, p["project_id"], [e])
    _, _, _, proposal = proposed(service, p, e, h)
    review(service, p, proposal, "rejected")
    alias = evidence(service, p["project_id"], "99", source_identifier="doi:10.1234/METHOD",
                     source_url="https://doi.org/10.1234/METHOD")
    with pytest.raises(ServiceError) as error:
        proposed(service, p, e, h, citations=[{"evidence_id": e["evidence_id"]}, {"evidence_id": alias["evidence_id"]}],
                 reconsideration_reason="Different spelling, same source")
    assert error.value.code == "duplicate_citation"


@pytest.mark.parametrize("record", [{**metadata(), "title": 4}, {**metadata(), "source_url": 3}])
def test_malformed_metadata_types_are_durable_failures(service, monkeypatch, record):
    monkeypatch.setattr("modelbuilder.literature.search_openalex", lambda *args: [record])
    p = project(service)
    result = refresh(service, watch(service, p["project_id"]))
    assert result["refresh"]["status"] == "failed"
    assert len(service.store.list("research_refresh", p["project_id"])) == 1
    assert not service.store.list("evidence", p["project_id"])


@pytest.mark.parametrize("authorships", [[None], [{"author": None}], None])
def test_malformed_openalex_authors_are_durable_failures(service, monkeypatch, authorships):
    from modelbuilder import literature
    payload = {"results": [{"id": "https://openalex.org/W123", "title": "Malformed authors",
                            "authorships": authorships}]}
    monkeypatch.setattr(literature, "urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()))
    p = project(service)
    result = refresh(service, watch(service, p["project_id"]))
    assert result["refresh"]["status"] == "failed"
    assert len(service.store.list("research_refresh", p["project_id"])) == 1


def test_cloning_or_resetting_rejected_hypothesis_cannot_erase_negative_history(service):
    p = project(service)
    e = evidence(service, p["project_id"])
    original = hypothesis(service, p["project_id"], [e], status="rejected")
    clone = hypothesis(service, p["project_id"], [e])
    with pytest.raises(ServiceError) as error:
        proposed(service, p, e, clone)
    assert error.value.code == "new_evidence_required"
    revived = invoke(service, "hypothesis.update", {"project_id": p["project_id"],
        "hypothesis_id": original["hypothesis_id"], "expected_revision": 1, "changes": {"status": "proposed"}})
    with pytest.raises(ServiceError) as error:
        proposed(service, p, e, revived)
    assert error.value.code == "new_evidence_required"
    assert error.value.details["prior_rejections"][0]["id"] == original["hypothesis_id"]
    fresh = evidence(service, p["project_id"], "2")
    _, _, _, retry = proposed(service, p, e, revived,
        citations=[{"evidence_id": e["evidence_id"]}, {"evidence_id": fresh["evidence_id"]}],
        reconsideration_reason="A genuinely new source addresses the retained rejection")
    assert review(service, p, retry)["status"] == "approved"


def test_removing_exact_passage_does_not_manufacture_new_evidence(service):
    p = project(service)
    e = evidence(service, p["project_id"])
    h = hypothesis(service, p["project_id"], [e])
    text = invoke(service, "research.text.import", {"project_id": p["project_id"], "evidence_id": e["evidence_id"],
        "expected_evidence_revision": 1, "text": "One reported result.", "license": "CC0-1.0",
        "rights_basis": "public_domain", "rights_confirmed": True})
    citation = {"evidence_id": e["evidence_id"], "text_id": text["text_id"],
                "quote": "One reported result.", "start": 0, "end": 20}
    _, _, _, proposal = proposed(service, p, e, h, citations=[citation])
    review(service, p, proposal, "rejected")
    with pytest.raises(ServiceError) as error:
        proposed(service, p, e, h, reconsideration_reason="Dropped the same passage")
    assert error.value.code == "new_evidence_required"


def test_negative_experiment_retains_the_applied_proposals_exact_passage_evidence(service):
    p = project(service)
    e = evidence(service, p["project_id"])
    h = hypothesis(service, p["project_id"], [e])
    text = invoke(service, "research.text.import", {"project_id": p["project_id"], "evidence_id": e["evidence_id"],
        "expected_evidence_revision": 1, "text": "One reported result.", "license": "CC0-1.0",
        "rights_basis": "public_domain", "rights_confirmed": True})
    citation = {"evidence_id": e["evidence_id"], "text_id": text["text_id"],
                "quote": "One reported result.", "start": 0, "end": 20}
    _, _, _, proposal = proposed(service, p, e, h, citations=[citation])
    result = apply(service, p, review(service, p, proposal))
    r = result["recipe"]
    with service.store.transaction():
        service.store.put("operation", "contract-operation", p["project_id"], {
            "project_id": p["project_id"], "operation_id": "contract-operation", "backend_run_id": "contract-run",
            "recipe_id": r["recipe_id"], "recipe_revision": r["revision"], "recipe_digest": r["digest"], "revision": 1})
    invoke(service, "decision.record", {"project_id": p["project_id"], "hypothesis_id": h["hypothesis_id"],
        "recipe_id": r["recipe_id"], "recipe_revision": r["revision"], "run_ids": ["contract-run"],
        "selection_rule": {"metric": "brier", "direction": "minimize"}, "outcome": "rejected",
        "rationale": "Contract fixture experiment rejected this cited proposal"})
    with pytest.raises(ServiceError) as error:
        proposed(service, p, e, h, citations=[citation], reconsideration_reason="Same exact already-tested passage")
    assert error.value.code == "new_evidence_required"


@pytest.mark.parametrize("error", [ConnectionResetError("private endpoint"), OSError("network interrupted")])
def test_transport_read_errors_are_durable_failed_refreshes(service, monkeypatch, error):
    from modelbuilder import literature
    def broken_request(*args, **kwargs):
        raise error
    monkeypatch.setattr(literature, "urlopen", broken_request)
    p = project(service)
    result = refresh(service, watch(service, p["project_id"]))
    assert result["refresh"]["status"] == "failed"
    assert "private endpoint" not in json.dumps(result)
    assert len(service.store.list("research_refresh", p["project_id"])) == 1


def test_truncated_http_body_is_a_durable_failed_refresh(service, monkeypatch):
    from http.client import IncompleteRead
    from modelbuilder import literature
    class Truncated(io.BytesIO):
        def read(self, *args):
            raise IncompleteRead(b'{"private":', 100)
    monkeypatch.setattr(literature, "urlopen", lambda *args, **kwargs: Truncated())
    p = project(service)
    result = refresh(service, watch(service, p["project_id"]))
    assert result["refresh"]["status"] == "failed"
    assert "private" not in json.dumps(result)
    assert len(service.store.list("research_refresh", p["project_id"])) == 1


@pytest.mark.parametrize("poison", [{"title": "Invented conclusion"},
    {"citation_metadata": {"provider": "OpenAlex", "claim_verified": True, "metadata_only": False}},
    {"excerpt": "Invented source passage"}])
def test_watch_never_reuses_a_client_asserted_hash_with_wrong_source_content(service, monkeypatch, poison):
    from modelbuilder.research import ResearchWorkflow, _hash
    source = metadata()
    monkeypatch.setattr("modelbuilder.literature.search_openalex", lambda *args: [source])
    p = project(service)
    normalized = ResearchWorkflow(service)._metadata(source)
    impostor = evidence(service, p["project_id"], **{**source, "content_hash": _hash(normalized), **poison})
    result = refresh(service, watch(service, p["project_id"]))
    assert result["refresh"]["status"] == "succeeded"
    finding = service.store.list("research_finding", p["project_id"])[0]
    assert finding["evidence_id"] != impostor["evidence_id"]
    assert finding["citation"]["title"] == source["title"]
    assert finding["citation"]["excerpt"] is None
    assert finding["citation"]["citation_metadata"]["claim_verified"] is False


def test_reused_openalex_parser_filters_entries_without_work_identity(service, monkeypatch):
    from modelbuilder import literature
    payload = {"results": [None, {"title": "No work identity"},
        {"id": "https://openalex.org/W123", "title": "Valid filtered result", "authorships": []}]}
    monkeypatch.setattr(literature, "urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()))
    p = project(service)
    result = refresh(service, watch(service, p["project_id"], limit=3))
    assert result["refresh"]["status"] == "succeeded"
    assert len(result["refresh"]["new_findings"]) == 1
    assert service.store.list("evidence", p["project_id"])[0]["title"] == "Valid filtered result"


def test_modified_intent_still_marked_rejected_is_not_erased_by_a_clone(service):
    p = project(service)
    e = evidence(service, p["project_id"])
    original = hypothesis(service, p["project_id"], [e], status="rejected")
    invoke(service, "hypothesis.update", {"project_id": p["project_id"], "hypothesis_id": original["hypothesis_id"],
        "expected_revision": 1, "changes": {"proposed_change": "A different rejected change"}})
    clone = hypothesis(service, p["project_id"], [e], proposed_change="A different rejected change")
    with pytest.raises(ServiceError) as error:
        proposed(service, p, e, clone)
    assert error.value.code == "new_evidence_required"


def test_watch_normalizes_missing_optional_fields_before_snapshot_reuse(service, monkeypatch):
    from modelbuilder.research import ResearchWorkflow, _hash
    p = project(service)
    source = {"title": "Title only", "source_identifier": "https://openalex.org/W1"}
    normalized = ResearchWorkflow(service)._metadata(source)
    impostor = evidence(service, p["project_id"], **{**normalized, "content_hash": _hash(normalized),
                                                  "publication_date": "1900-01-01", "authors": ["Invented author"]})
    monkeypatch.setattr("modelbuilder.literature.search_openalex", lambda *args: [source])
    refresh(service, watch(service, p["project_id"]))
    finding = service.store.list("research_finding", p["project_id"])[0]
    assert finding["evidence_id"] != impostor["evidence_id"]
    assert finding["citation"]["publication_date"] is None
    assert finding["citation"]["authors"] == []


async def test_official_native_mcp_research_review_apply_matches_shared_state(service, monkeypatch):
    """Real SDK session over memory streams and HTTP/ASGI; not a vendor session."""
    import httpx
    from mcp.shared.memory import create_connected_server_and_client_session
    from modelbuilder.mcp_server import OperationGateway, create_server
    monkeypatch.setattr("modelbuilder.literature.search_openalex", lambda *args: [metadata()])
    app = create_app(service, TokenAuth({"mcp-contract-token": OWNER}))
    async with httpx.AsyncClient(base_url="http://test", headers={"Authorization": "Bearer mcp-contract-token"},
                                 transport=httpx.ASGITransport(app=app)) as client:
        gateway = OperationGateway(client)
        server = create_server(gateway, await gateway.catalog())
        async with create_connected_server_and_client_session(server) as session:
            async def call(operation, **payload):
                if OPERATIONS[operation].mutating:
                    payload.setdefault("idempotency_key", str(uuid4()))
                result = await session.call_tool(operation.replace(".", "_"), payload)
                assert not result.isError, result.structuredContent
                return result.structuredContent
            tools = await session.list_tools()
            assert any(tool.name == "research_proposal_apply" for tool in tools.tools)
            p = await call("project.create", slug="native-mcp-research", model_name="Native MCP research")
            watch_result = await call("research.watch.create", project_id=p["project_id"], name="Configured query",
                                      query="calibration", limit=1)
            fetched = await call("research.watch.refresh", project_id=p["project_id"],
                                 watch_id=watch_result["watch_id"], expected_revision=1)
            assert fetched["refresh"]["status"] == "succeeded"
            findings = await call("research.finding.list", project_id=p["project_id"])
            e = findings["findings"][0]["citation"]
            reviewed_finding = await call("research.finding.review", project_id=p["project_id"],
                finding_id=findings["findings"][0]["finding_id"], expected_revision=1, status="reviewed",
                rationale="Read bibliographic metadata; local experimental verification remains necessary")
            assert not reviewed_finding["review"]["claim_verified"]
            h = await call("hypothesis.create", project_id=p["project_id"], evidence_ids=[e["evidence_id"]],
                proposed_change="Evaluate a calibrated checkpoint", expected_outcome="Lower Brier score",
                acceptance_metric={"name": "brier", "direction": "minimize"})
            r = await call("recipe.get", project_id=p["project_id"], recipe_id=p["recipe_id"])
            proposal = await call("research.proposal.create", project_id=p["project_id"],
                hypothesis_id=h["hypothesis_id"], title="Controlled experiment", rationale="Evaluate cited method",
                citations=[{"evidence_id": e["evidence_id"]}], expected_recipe_revision=1,
                recipe={**r, "description": "A reviewed research-driven change"})
            assert service.store.get("recipe", p["recipe_id"])["revision"] == 1
            approved = await call("research.proposal.review", project_id=p["project_id"],
                proposal_id=proposal["proposal_id"], expected_revision=1, outcome="approved",
                rationale="Reviewed exact recipe diff and unverified evidence")
            assert service.store.get("recipe", p["recipe_id"])["revision"] == 1
            applied = await call("research.proposal.apply", project_id=p["project_id"],
                proposal_id=proposal["proposal_id"], expected_revision=approved["revision"],
                expected_recipe_revision=1, confirm_apply=True)
            current = invoke(service, "recipe.get", {"project_id": p["project_id"], "recipe_id": p["recipe_id"]})
            assert current == applied["recipe"]
            assert current["digest"] == proposal["staged_recipe_digest"]
            assert not applied["execution_started"] and service.store.list("operation", p["project_id"]) == []
            retry = await session.call_tool("research_proposal_apply", {"project_id": p["project_id"],
                "proposal_id": proposal["proposal_id"], "expected_revision": approved["revision"],
                "expected_recipe_revision": 1, "confirm_apply": True, "idempotency_key": "stale-mcp-apply"})
            assert retry.isError and retry.structuredContent["http_status"] == 409
            assert retry.structuredContent["error"]["code"] == "revision_conflict"
