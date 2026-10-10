"""Controlled local fixtures only: no live providers, private queries or uploads."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from email.message import Message
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from modelbuilder import literature, research_content as content, research_content_fetch as fetcher
from modelbuilder.api import create_app
from modelbuilder.auth import TokenAuth
from modelbuilder.models import Actor, OPERATIONS, ServiceError
from modelbuilder.service import OperationService

OWNER = Actor("content-owner", {"read", "modify", "execute"})
LICENSE = "https://creativecommons.org/licenses/by/4.0/"


def article(*, license_uri=LICENSE, identifier="PMC123", body=None, doctype=""):
    return f'''<?xml version="1.0" encoding="UTF-8"?>{doctype}
<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta>
<article-id pub-id-type="pmc">{identifier}</article-id><article-id pub-id-type="doi">10.1234/fixture</article-id>
<title-group><article-title>A controlled calibration study</article-title></title-group>
<contrib-group><contrib contrib-type="author"><name><given-names>Alex</given-names><surname>Example</surname></name></contrib></contrib-group>
<permissions><copyright-statement>Copyright 2026 the authors</copyright-statement><license xlink:href="{license_uri}"><license-p>Credit the authors. No warranty.</license-p></license></permissions>
<abstract><p>We compare calibration methods in a controlled fixture.</p></abstract></article-meta></front>
<body>{body or '<sec sec-type="methods"><title>Methods</title><p>We use α calibration with three classes.</p><sec><title>Evaluation procedure</title><p>Measure Brier score with held-out rows.</p></sec></sec><sec><title>Limitations</title><p>This calibration result is a fixture, not a finding.</p></sec>'}</body>
<back><ref-list><ref>Reference list deliberately omitted by text extraction.</ref></ref-list></back></article>'''.encode()


def citation(**values):
    return {"evidence_id": "fixture-evidence", "project_id": "fixture-project", "revision": 1,
            "source_identifier": "PMC123", "source_url": "https://europepmc.org/articles/PMC123",
            "title": "A controlled calibration study", "citation_metadata": {}, **values}


@pytest.fixture
def service(tmp_path):
    result = OperationService(tmp_path / "content.sqlite3")
    yield result
    result.store.close()


def invoke(service, operation, payload, actor=OWNER):
    if OPERATIONS[operation].mutating:
        payload = {"idempotency_key": str(uuid4()), **payload}
    return service.invoke(operation, payload, actor)


def setup(service):
    project = invoke(service, "project.create", {"slug": "paper", "model_name": "Paper fixture"})
    evidence = invoke(service, "evidence.add", {"project_id": project["project_id"], "title": "A fixture paper",
                                               "source_identifier": "PMC123"})
    return project, evidence


def request(evidence, **options):
    return {"project_id": evidence["project_id"], "evidence_id": evidence["evidence_id"],
            "expected_evidence_revision": evidence["revision"], "provider": "europe_pmc",
            "source_identifier": "PMC123", **options}


def test_licensed_xml_extracts_abstract_methods_offsets_and_provenance():
    raw = article(doctype='<!DOCTYPE article PUBLIC "fixture" "https://forbidden.invalid/never-load.dtd">')
    parsed = content.parse_article(raw, "PMC123", citation())
    assert parsed["license"] == LICENSE
    assert "Reference list deliberately" not in parsed["text"]
    assert "We compare calibration" in parsed["text"]
    methods = next(item for item in parsed["sections"] if item["kind"] == "methods")
    assert parsed["text"][methods["start"]:methods["end"]].startswith("Methods\n\nWe use α")
    assert "Measure Brier" in parsed["text"][methods["start"]:methods["end"]]
    assert parsed["provenance"]["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert parsed["attribution"]["copyright"] == "Copyright 2026 the authors"
    assert parsed["provenance"]["complete_publisher_rendering"] is False
    assert not parsed["claim_verified"]


@pytest.mark.parametrize("license_uri", ["", "unknown", "http://creativecommons.org.evil/licenses/by/4.0/",
    "https://evil@creativecommons.org/licenses/by/4.0/", "https://creativecommons.org:444/licenses/by/4.0/",
    "https://creativecommons.org/licenses/by-nc/4.0/", "https://creativecommons.org/licenses/by/4.0/?evil",
    "https://creativecommons.org/licenses/by/4.0/#rights", "https://creativecommons.org/licenses/by-nd/4.0/"])
def test_missing_or_unsupported_rights_fail_closed(license_uri):
    with pytest.raises(content.ContentUnavailable) as error:
        content.parse_article(article(license_uri=license_uri), "PMC123", citation())
    assert error.value.code == "rights_unknown"


@pytest.mark.parametrize("uri", ["http://creativecommons.org/licenses/by-sa/3.0/", "https://www.creativecommons.org/publicdomain/zero/1.0/legalcode"])
def test_supported_licenses_are_canonicalized(uri):
    assert content.parse_article(article(license_uri=uri), "PMC123", citation())["license"].startswith("https://creativecommons.org/")


def test_doi_and_pmc_identity_are_exact_not_title_matching():
    assert content.parse_article(article(), "PMC123", citation(source_identifier="https://openalex.org/W1", source_url="https://doi.org/10.1234/FIXTURE"))
    for evidence in [citation(source_identifier="PMC12", source_url=""), citation(source_identifier="PMC1234", source_url=""), citation(source_identifier="W1", source_url="https://evil.invalid/PMC123")]:
        with pytest.raises(content.ContentUnavailable, match="does not match"):
            content.parse_article(article(), "PMC123", evidence)
    with pytest.raises(content.ContentUnavailable, match="does not match"):
        content.parse_article(article(identifier="PMC124"), "PMC123", citation())


@pytest.mark.parametrize("raw", [b"<article>", b"<html>denied</html>", b"\xff\xfe", article().replace(b"UTF-8", b"ISO-8859-1"),
    article(doctype='<!DOCTYPE article [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'),
    article(body="<sec>" * 70 + "x" + "</sec>" * 70), article(body="<p/>" * 40001),
    article().replace(b"<body>", b"<not-body>").replace(b"</body>", b"</not-body>")])
def test_unsafe_or_unsupported_xml_is_never_accepted(raw):
    with pytest.raises(content.ContentUnavailable):
        content.parse_article(raw, "PMC123", citation())


def test_both_wire_and_decoded_text_are_bounded():
    for raw in [b"x" * (content.MAX_RESPONSE_BYTES + 1), article(body="<p>" + "α" * 100001 + "</p>")]:
        with pytest.raises(content.ContentUnavailable) as error:
            content.parse_article(raw, "PMC123", citation())
        assert error.value.code == "response_too_large"


class Response(io.BytesIO):
    def __init__(self, raw, *, url=None, content_type="application/xml", length=None, encoding=None):
        super().__init__(raw)
        self.url = url or "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC123/fullTextXML"
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if length is not None:
            self.headers["Content-Length"] = str(length)
        if encoding:
            self.headers["Content-Encoding"] = encoding
    def geturl(self):
        return self.url


class Opener:
    def __init__(self, response):
        self.response, self.requests = response, []
    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_fixed_origin_ignores_environment_and_sends_only_identifier(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://private:secret@127.0.0.1:1")
    opener = Opener(Response(article()))
    assert fetcher.retrieve("PMC123", opener)[0] == 0
    sent, timeout = opener.requests[0]
    assert sent.full_url == "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC123/fullTextXML"
    assert sent.get_header("Accept-encoding") == "identity" and timeout == 18
    assert fetcher.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "http://127.0.0.1/secret") is None
    for identifier in ["https://127.0.0.1/", "PMC123/../../secret", "PMC123?token=private", "PMC0", "PMC123\nHeader: evil"]:
        assert fetcher.retrieve(identifier, opener) == (1, b"")
    assert len(opener.requests) == 1


@pytest.mark.parametrize("response,code", [
    (Response(article(), url="http://127.0.0.1/private"), 3), (Response(article(), content_type="text/html"), 4),
    (Response(article(), encoding="gzip"), 4), (Response(b"x", length=4_000_001), 2),
    (Response(b"x" * 4_000_001), 2), (Response(b"x", length="invalid"), 2),
    (HTTPError("fixture", 302, "redirect", {}, None), 3), (HTTPError("fixture", 403, "forbidden", {}, None), 5)])
def test_wire_guards_return_no_source_bytes(response, code):
    assert fetcher.retrieve("PMC123", Opener(response)) == (code, b"")


def test_redirect_handler_does_not_visit_local_controlled_target():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    visited = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visited.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/should-not-follow")
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        opener = build_opener(ProxyHandler({}), fetcher.NoRedirect())
        with pytest.raises(HTTPError):
            opener.open(Request(f"http://127.0.0.1:{server.server_port}/fixture"), timeout=1)
        assert visited == ["/fixture"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_isolated_child_has_sanitized_environment_and_bounded_output(monkeypatch):
    recorded = []
    monkeypatch.setenv("MODELBUILDER_TOKEN", "never-forward")
    monkeypatch.setenv("HTTPS_PROXY", "http://credential-proxy.invalid")
    def run(command, **kwargs):
        recorded.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=article())
    monkeypatch.setattr(content.subprocess, "run", run)
    assert content.fetch_xml("PMC123") == article()
    command, options = recorded[0]
    assert command[1] == "-I" and Path(command[2]).name == "research_content_fetch.py"
    assert set(options["env"]) <= {"SYSTEMROOT", "WINDIR"}
    assert json.loads(options["input"]) == {"source_identifier": "PMC123"}
    assert options["timeout"] == 20
    monkeypatch.setattr(content.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=b"x" * 4_000_001))
    with pytest.raises(content.ContentUnavailable, match="exceeded"):
        content.fetch_xml("PMC123")


def test_real_slow_child_is_killed_and_reaped(monkeypatch, tmp_path):
    original = subprocess.run
    pidfile = tmp_path / "pid"
    code = f"import os,time;open({str(pidfile)!r},'w').write(str(os.getpid()));time.sleep(30)"
    def slow(_command, **options):
        return original([sys.executable, "-I", "-c", code], **options)
    monkeypatch.setattr(content, "FETCH_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(content.subprocess, "run", slow)
    with pytest.raises(content.ContentUnavailable) as error:
        content.fetch_xml("PMC123")
    assert error.value.code == "fetch_timeout"
    if os.name != "nt":
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)


def test_fetch_list_inspect_search_and_retry_are_local_and_immutable(service, monkeypatch):
    calls = []
    monkeypatch.setattr(content, "fetch_xml", lambda identifier: calls.append(identifier) or article())
    project, evidence = setup(service)
    result = invoke(service, "research.text.fetch", request(evidence, idempotency_key="once"))
    assert result["status"] == "succeeded"
    assert invoke(service, "research.text.fetch", request(evidence, idempotency_key="once")) == result
    assert calls == ["PMC123"]
    text_id = result["text_id"]
    p = {"project_id": project["project_id"], "text_id": text_id}
    text = invoke(service, "research.text.get", p)
    assert text["sha256"] == hashlib.sha256(text["text"].encode()).hexdigest()
    listed = invoke(service, "research.text.list", {"project_id": project["project_id"]})
    assert "text" not in listed["texts"][0] and listed["retrievals"][0]["text_id"] == text_id
    methods = next(item for item in text["sections"] if item["kind"] == "methods")
    inspected = invoke(service, "research.text.inspect", {**p, "section_id": methods["section_id"], "length": 10})
    assert inspected["text"] == text["text"][methods["start"]:methods["start"] + 10]
    assert inspected["next_start"] == methods["start"] + 10
    found = invoke(service, "research.text.search", {**p, "query": "CALIBRATION", "limit": 1})
    assert len(found["matches"]) == 1 and found["next_start"] is not None
    match = found["matches"][0]
    assert text["text"][match["start"]:match["end"]] == match["quote"]
    remaining = invoke(service, "research.text.search", {**p, "query": "CALIBRATION", "start": found["next_start"]})
    assert remaining["matches"][0]["start"] > match["start"] and remaining["next_start"] is None
    assert not invoke(service, "research.text.search", {**p, "query": ".*"})["matches"]
    assert not invoke(service, "research.text.search", {**p, "query": "CALIBRATION", "case_sensitive": True})["matches"]
    assert calls == ["PMC123"]
    again = invoke(service, "research.text.fetch", request(evidence))
    assert again["text_id"] == text_id and len(service.store.list("research_text", project["project_id"])) == 1
    assert len(service.store.list("research_retrieval", project["project_id"])) == 2
    for kind in ["operation", "claim", "hypothesis", "research_proposal"]:
        assert service.store.list(kind, project["project_id"]) == []


def test_missing_rights_and_provider_failures_are_durable_and_redacted(service, monkeypatch):
    project, evidence = setup(service)
    monkeypatch.setattr(content, "fetch_xml", lambda *_: article(license_uri="unknown"))
    result = invoke(service, "research.text.fetch", request(evidence))
    assert result["status"] == "failed" and result["retrieval"]["error"]["code"] == "rights_unknown"
    assert not service.store.list("research_text", project["project_id"])
    def denied(*_):
        raise content.ContentUnavailable("access_denied")
    monkeypatch.setattr(content, "fetch_xml", denied)
    failure = invoke(service, "research.text.fetch", request(evidence, idempotency_key="failed-once"))
    assert failure["retrieval"]["error"]["code"] == "access_denied"
    monkeypatch.setattr(content, "fetch_xml", lambda *_: pytest.fail("No fallback or duplicate retry"))
    assert invoke(service, "research.text.fetch", request(evidence, idempotency_key="failed-once")) == failure
    assert len(service.store.list("research_retrieval", project["project_id"])) == 2


def test_revision_authorization_and_input_guards_precede_network(service, monkeypatch):
    project, evidence = setup(service)
    monkeypatch.setattr(content, "fetch_xml", lambda *_: pytest.fail("Unauthorized network"))
    for changes in [{"expected_evidence_revision": 2}, {"source_identifier": "https://127.0.0.1/private"}, {"provider": "arbitrary"}]:
        with pytest.raises(ServiceError):
            invoke(service, "research.text.fetch", request(evidence, **changes))
    for actor in [Actor("reader", {"read"}), Actor("other", {"modify"}, {"another-project"})]:
        with pytest.raises(ServiceError) as error:
            invoke(service, "research.text.fetch", request(evidence), actor)
        assert error.value.code == "forbidden"
    service.research_authorizer = lambda *_: False
    with pytest.raises(ServiceError, match="revoked"):
        invoke(service, "research.text.fetch", request(evidence))


def test_provider_io_releases_store_and_sqlite_locks_and_duplicate_is_fenced(service, monkeypatch):
    project, evidence = setup(service)
    entered, release = threading.Event(), threading.Event()
    def slow(*_):
        entered.set()
        assert release.wait(5)
        return article()
    monkeypatch.setattr(content, "fetch_xml", slow)
    other = OperationService(service.store.path)
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(invoke, service, "research.text.fetch", request(evidence, idempotency_key="pending"))
        assert entered.wait(2)
        try:
            assert invoke(service, "research.text.list", {"project_id": project["project_id"]})["texts"] == []
            invoke(other, "evidence.add", {"project_id": project["project_id"], "title": "During I/O", "source_identifier": "PMC9"})
            with pytest.raises(ServiceError) as error:
                invoke(other, "research.text.fetch", request(evidence, idempotency_key="pending"))
            assert error.value.code == "request_in_progress"
            with pytest.raises(ServiceError) as error:
                invoke(other, "research.text.fetch", request(evidence, idempotency_key="pending", source_identifier="PMC124"))
            assert error.value.code == "idempotency_conflict"
        finally:
            release.set()
            other.store.close()
        assert pending.result(2)["status"] == "succeeded"


@pytest.mark.parametrize("change", ["authority", "lease", "archive"])
def test_late_response_cannot_commit_after_revocation(service, monkeypatch, change):
    project, evidence = setup(service)
    def fetch(*_):
        if change == "authority":
            service.research_authorizer = lambda *_: False
        elif change == "lease":
            with service.store.transaction():
                service.store.connection.execute("UPDATE research_requests SET expires_at='2000-01-01T00:00:00+00:00'")
        else:
            with service.store.transaction():
                latest = service.store.get("project", project["project_id"])
                service.store.put("project", project["project_id"], project["project_id"], {**latest, "revision": 2, "status": "archived"}, 1)
        return article()
    monkeypatch.setattr(content, "fetch_xml", fetch)
    with pytest.raises(ServiceError):
        invoke(service, "research.text.fetch", request(evidence))
    assert not service.store.list("research_text", project["project_id"])
    assert not service.store.list("research_retrieval", project["project_id"])


def test_read_scope_isolation_and_imported_text_search(service, monkeypatch):
    project, evidence = setup(service)
    imported = invoke(service, "research.text.import", {**{key: value for key, value in request(evidence).items() if key not in {"provider", "source_identifier"}},
        "text": "An independently supplied α method. α method.", "license": "Self-authored fixture", "rights_basis": "self_authored", "rights_confirmed": True})
    p = {"project_id": project["project_id"], "text_id": imported["text_id"]}
    reader = Actor("reader", {"read"}, {project["project_id"]})
    assert invoke(service, "research.text.inspect", p, reader)["sections"][0]["section_id"] == "document"
    assert len(invoke(service, "research.text.search", {**p, "query": "α"}, reader)["matches"]) == 2
    for operation, changes in [("research.text.inspect", {"section_id": "nonexistent"}), ("research.text.inspect", {"start": 200000}), ("research.text.search", {"query": " "}), ("research.text.search", {"query": "x", "limit": 51})]:
        with pytest.raises(ServiceError):
            invoke(service, operation, {**p, **changes}, reader)
    with pytest.raises(ServiceError) as error:
        invoke(service, "research.text.get", p, Actor("other", {"read"}, {"other-project"}))
    assert error.value.code == "forbidden"


def test_openalex_abstracts_are_rights_aware_bounded_and_unverified(monkeypatch):
    item = {"id": "https://openalex.org/W123", "title": "Fixture", "abstract_inverted_index": {"A": [0], "method.": [1]}, "primary_location": {"license": "cc-by"}}
    def search(value):
        monkeypatch.setattr(literature, "urlopen", lambda *a, **k: io.BytesIO(json.dumps({"results": [value]}).encode()))
        return literature.search_openalex("public fixture")[0]["citation_metadata"]["abstract"]
    assert search(item)["text"] == "A method."
    for label in [None, "cc-by-nc", "publisher-specific", "unknown", {"license": "cc-by"}]:
        unknown = search({**item, "primary_location": {"license": label}})
        assert unknown["status"] == "rights_unknown" and "text" not in unknown
    for index in [{"x": [True]}, {"x": [2]}, {"x": [0, 0]}, {"x": [10000]}, {"x": [0], "y": [0]}, {"x" * 50001: [0]}, {"word" * 10000: list(range(10000))}, {"\ud800": [0]}]:
        invalid = search({**item, "abstract_inverted_index": index})
        assert invalid["status"] == "invalid" and "text" not in invalid
    assert search({**item, "abstract_inverted_index": None})["status"] == "missing"


def test_http_matches_service_reads_and_failures(service, monkeypatch):
    monkeypatch.setattr(content, "fetch_xml", lambda *_: article())
    project, evidence = setup(service)
    app = create_app(service, TokenAuth({"fixture-token": OWNER}))
    with TestClient(app) as client:
        def call(operation, payload):
            return client.post('/v1/operations/' + operation, json=payload, headers={"Authorization": "Bearer fixture-token"})
        fetched = call("research.text.fetch", request(evidence, idempotency_key="http-fetch")).json()
        p = {"project_id": project["project_id"], "text_id": fetched["text_id"]}
        for operation, args in [("research.text.get", p), ("research.text.inspect", p), ("research.text.search", {**p, "query": "method"}), ("research.text.list", {"project_id": project["project_id"]})]:
            assert call(operation, args).json() == invoke(service, operation, args)
        assert call("research.text.fetch", request(evidence, source_identifier="file:///private", idempotency_key="invalid")).status_code == 400


@pytest.mark.asyncio
async def test_official_mcp_fetch_inspect_search_proposal_passage_matches_http(service, monkeypatch):
    import httpx
    from mcp.shared.memory import create_connected_server_and_client_session
    from modelbuilder.mcp_server import OperationGateway, create_server
    monkeypatch.setattr(content, "fetch_xml", lambda *_: article())
    project, evidence = setup(service)
    app = create_app(service, TokenAuth({"mcp-token": OWNER}))
    async with httpx.AsyncClient(base_url="http://test", headers={"Authorization": "Bearer mcp-token"}, transport=httpx.ASGITransport(app=app)) as client:
        gateway = OperationGateway(client)
        server = create_server(gateway, await gateway.catalog())
        async with create_connected_server_and_client_session(server) as session:
            async def call(operation, payload):
                if OPERATIONS[operation].mutating:
                    payload = {"idempotency_key": str(uuid4()), **payload}
                result = await session.call_tool(operation.replace('.', '_'), payload)
                assert not result.isError, result
                return json.loads(result.content[0].text)
            fetched = await call("research.text.fetch", request(evidence))
            p = {"project_id": project["project_id"], "text_id": fetched["text_id"]}
            inspected = await call("research.text.inspect", p)
            assert inspected == invoke(service, "research.text.inspect", p)
            found = await call("research.text.search", {**p, "query": "α calibration"})
            match = found["matches"][0]
            h = await call("hypothesis.create", {"project_id": project["project_id"], "evidence_ids": [evidence["evidence_id"]], "proposed_change": "Test calibration", "expected_outcome": "A measured score change", "acceptance_metric": {"name": "brier", "direction": "minimize"}})
            recipe = invoke(service, "recipe.get", {"project_id": project["project_id"], "recipe_id": project["recipe_id"]})
            proposal = await call("research.proposal.create", {"project_id": project["project_id"], "hypothesis_id": h["hypothesis_id"], "title": "Controlled proposal", "rationale": "Evaluate source method without assuming its claim", "citations": [{"evidence_id": evidence["evidence_id"], "text_id": p["text_id"], **{key: match[key] for key in ('quote', 'start', 'end')}}], "recipe": {**deepcopy(recipe), "description": "An explicit test"}, "expected_recipe_revision": 1})
            assert proposal["citations"][0]["validation_status"] == "passage_matched_retrieved_text"
            assert proposal["citations"][0]["claim_verified"] is False
            assert not service.store.list("operation", project["project_id"])


def test_attribution_word_boundaries_and_table_cells_do_not_merge():
    parsed = content.parse_article(article(body='<sec><title>Methods</title><p>calib<italic>ration</italic></p><table-wrap><table><tr><td>0.2</td><td>0.4</td></tr></table></table-wrap></sec>'), "PMC123", citation())
    assert parsed["attribution"]["authors"] == ["Alex Example"]
    assert "calibration" in parsed["text"] and "0.2 0.4" in parsed["text"]


def test_specific_use_and_multiple_or_malformed_licenses_fail_closed():
    for raw in [article().replace(b'<license xlink:', b'<license specific-use="text-and-data-mining" xlink:'),
                article().replace(b'</permissions>', b'<license>Unknown alternative terms</license></permissions>'),
                article(license_uri="https://[malformed"),
                article().replace(b"</license-p>", b'<ext-link xlink:href="https://creativecommons.org/licenses/by-nc/4.0/">Conflicting license</ext-link></license-p>')]:
        with pytest.raises(content.ContentUnavailable) as error:
            content.parse_article(raw, "PMC123", citation())
        assert error.value.code == "rights_unknown"


def test_global_content_admission_is_two_across_connections(service, monkeypatch):
    project, evidence = setup(service)
    entered, release = threading.Barrier(3), threading.Event()
    def slow(*_):
        entered.wait(3)
        assert release.wait(3)
        return article()
    monkeypatch.setattr(content, "fetch_xml", slow)
    other = OperationService(service.store.path)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(invoke, service, "research.text.fetch", request(evidence, idempotency_key="first"))
        second = pool.submit(invoke, other, "research.text.fetch", request(evidence, idempotency_key="second"))
        entered.wait(3)
        try:
            with pytest.raises(ServiceError) as error:
                invoke(service, "research.text.fetch", request(evidence, idempotency_key="third"))
            assert error.value.code == "source_busy"
        finally:
            release.set()
        assert first.result(3)["text_id"] == second.result(3)["text_id"]
    other.store.close()
    assert len(service.store.list("research_text", project["project_id"])) == 1


def test_source_evidence_change_fences_fetch_commit(service, monkeypatch):
    project, evidence = setup(service)
    def fetch(*_):
        with service.store.transaction():
            service.store.put("evidence", evidence["evidence_id"], project["project_id"], {**evidence, "revision": 2, "title": "Changed"}, 1)
        return article()
    monkeypatch.setattr(content, "fetch_xml", fetch)
    with pytest.raises(ServiceError) as error:
        invoke(service, "research.text.fetch", request(evidence))
    assert error.value.code == "revision_conflict"
    assert not service.store.list("research_text", project["project_id"])


@pytest.mark.parametrize("change", ["remove", "replace", "downgrade"])
def test_http_fetch_rechecks_initiating_grant_even_with_second_actor_token(service, monkeypatch, change):
    project, evidence = setup(service)
    # Reusing the same immutable Actor value must still create distinct grants.
    auth = TokenAuth({"original-token": OWNER, "second-token": OWNER})
    original = auth.authenticate("Bearer original-token")
    app = create_app(service, auth)
    calls = []

    def fetch(identifier):
        calls.append(identifier)
        if change == "remove":
            auth._tokens.pop("original-token", None)
        else:
            auth._tokens["original-token"] = Actor(OWNER.id, {"read"} if change == "downgrade" else OWNER.scopes)
        assert auth.authorize_research(original.id, project["project_id"])
        assert not auth.authorize_operation("research.text.fetch", original, project["project_id"])
        return article()

    monkeypatch.setattr(content, "fetch_xml", fetch)
    with TestClient(app) as client:
        response = client.post("/v1/operations/research.text.fetch",
            json=request(evidence, idempotency_key="revoked-fetch"),
            headers={"Authorization": "Bearer original-token"})
        assert response.status_code == 403 and response.json()["error"]["code"] == "forbidden"
        assert calls == ["PMC123"]
        assert not service.store.list("research_text", project["project_id"])
        assert not service.store.list("research_retrieval", project["project_id"])
        assert service.store.connection.execute("SELECT COUNT(*) FROM research_requests").fetchone()[0] == 0
        # The other grant may make a fresh explicit request, but cannot authorize
        # the first request's late commit or supply an old successful receipt.
        response = client.post("/v1/operations/research.text.fetch",
            json=request(evidence, idempotency_key="fresh-second-grant"),
            headers={"Authorization": "Bearer second-token"})
        assert response.status_code == 200 and response.json()["status"] == "succeeded"
        assert len(service.store.list("research_text", project["project_id"])) == 1
        assert len(service.store.list("research_retrieval", project["project_id"])) == 1


def test_fetch_rechecks_initiating_grant_before_claim(service, monkeypatch):
    project, evidence = setup(service)
    auth = TokenAuth({"original-token": OWNER, "second-token": OWNER})
    original = auth.authenticate("Bearer original-token")
    create_app(service, auth)
    auth._tokens.pop("original-token")
    monkeypatch.setattr(content, "fetch_xml", lambda *_: pytest.fail("revoked grant must not start retrieval"))
    with pytest.raises(ServiceError) as failure:
        invoke(service, "research.text.fetch", request(evidence), original)
    assert failure.value.code == "forbidden"
    assert service.store.connection.execute("SELECT COUNT(*) FROM research_requests").fetchone()[0] == 0
