"""Actual local Poppler/Tesseract fixtures plus controlled authority/race tests.

No provider request, licensed third-party paper, external OCR, or model inference
is used. Tool absence skips native extraction unless explicitly required.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from uuid import uuid4
import zlib

from fastapi.testclient import TestClient
import pytest

from modelbuilder import research_documents as documents
from modelbuilder.api import create_app
from modelbuilder.auth import TokenAuth
from modelbuilder.models import Actor, OPERATIONS, ServiceError
from modelbuilder.research import ResearchWorkflow
from modelbuilder.service import OperationService

OWNER = Actor("document-owner", {"read", "modify", "execute"})
OPTIONS = {"ocr_mode": "disabled", "max_pages": 100, "max_ocr_pages": 20}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def assemble_pdf(objects):
    result, offsets = b"%PDF-1.4\n", []
    for index, value in enumerate(objects, 1):
        offsets.append(len(result))
        result += f"{index} 0 obj\n".encode() + value + b"\nendobj\n"
    xref = len(result)
    result += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        result += f"{offset:010d} 00000 n \n".encode()
    result += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return result


def stream(value, metadata=b""):
    return b"<< /Length " + str(len(value)).encode() + b" " + metadata + b" >>\nstream\n" + value + b"\nendstream"


def pdf(pages=("Methods: compare model calibration.", "Limitations: this is only a fixture.")):
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    ids = []
    for text in pages:
        page_id = len(objects) + 1
        ids.append(page_id)
        escaped = text.encode("cp1252").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
        instructions = b"BT /F1 20 Tf 45 700 Td (" + escaped + b") Tj ET"
        objects += [f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>".encode(), stream(instructions)]
    objects[1] = f"<< /Type /Pages /Count {len(pages)} /Kids [".encode() + b" ".join(f"{index} 0 R".encode() for index in ids) + b"] >>"
    return assemble_pdf(objects)


def scanned_pdf(tmp_path):
    source = tmp_path / "original.pdf"
    source.write_bytes(pdf(["SCANNED METHODS: COMPARE CALIBRATION"]))
    # Create a real raster-only PDF fixture without adding a Python PDF/image
    # dependency. This render is fixture preparation, not the tested decoder.
    result = subprocess.run(["/usr/bin/pdftoppm", "-r", "150", "-gray", "-singlefile", str(source)],
                            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    header = re.match(rb"P5\s+(\d+)\s+(\d+)\s+255\s", result.stdout)
    assert header
    width, height = map(int, header.groups())
    image = result.stdout[header.end():]
    assert len(image) == width * height
    return assemble_pdf([b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>",
        stream(b"q 612 0 0 792 0 0 cm /Im0 Do Q"),
        stream(zlib.compress(image), f"/Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode".encode())])


@pytest.fixture
def native_tools():
    capabilities = documents.document_capabilities()
    if not capabilities["available"] or not capabilities["ocr_available"]:
        if os.environ.get("MODELBUILDER_REQUIRE_DOCUMENTS") == "1":
            pytest.fail("The required actual Poppler/Tesseract tools are unavailable")
        pytest.skip("Actual local PDF/OCR tools require a supported Linux host")


@pytest.fixture
def service(tmp_path):
    instance = OperationService(tmp_path / "research-documents.sqlite3")
    yield instance
    instance.store.close()


def invoke(service, operation, payload, actor=OWNER):
    if OPERATIONS[operation].mutating:
        payload = {"idempotency_key": str(uuid4()), **payload}
    return service.invoke(operation, payload, actor)


def setup(service):
    project = invoke(service, "project.create", {"slug": "documents", "model_name": "Document fixture"})
    evidence = invoke(service, "evidence.add", {"project_id": project["project_id"], "title": "Self-authored controlled fixture",
                                               "source_identifier": "document-fixture"})
    return project, evidence


def request(evidence, raw=None, **changes):
    raw = pdf() if raw is None else raw
    return {"project_id": evidence["project_id"], "evidence_id": evidence["evidence_id"],
            "expected_evidence_revision": evidence["revision"], "filename": "fixture.pdf", "media_type": "application/pdf",
            "content_base64": base64.b64encode(raw).decode(), "expected_sha256": sha(raw),
            "license": "Self-authored test fixture; permitted local extraction", "rights_basis": "self_authored",
            "rights_confirmed": True, **changes}


def controlled_extraction(raw=None, options=None):
    text = "A controlled method."
    return {"text": text, "pages": [{"page_number": 1, "start": 0, "end": len(text),
            "sha256": sha(text.encode()), "byte_length": len(text), "extraction_method": "embedded_text", "empty": False}],
            "tools": {"pdfinfo": "pdfinfo test-double", "pdftotext": "pdftotext test-double"}}


def test_actual_poppler_preserves_pages_unicode_hashes_and_empty_pages(native_tools):
    raw = pdf(["M\u00e9thods: compare calibration.", "", "Third page conclusion."])
    value = documents.extract_pdf(raw, OPTIONS)
    assert value["text"] == "M\u00e9thods: compare calibration.\n\n\n\nThird page conclusion."
    assert len(value["pages"]) == 3
    for page in value["pages"]:
        passage = value["text"][page["start"]:page["end"]]
        assert page["sha256"] == sha(passage.encode())
        assert page["extraction_method"] == "embedded_text"
    assert value["pages"][1]["empty"] is True
    assert value["tools"]["pdftotext"].startswith("pdftotext version ")


def test_actual_scanned_pdf_requires_explicit_ocr_and_records_real_tesseract(native_tools, tmp_path):
    raw = scanned_pdf(tmp_path)
    with pytest.raises(documents.DocumentUnavailable) as error:
        documents.extract_pdf(raw, OPTIONS)
    assert error.value.code == "document_no_text"
    value = documents.extract_pdf(raw, {**OPTIONS, "ocr_mode": "auto"})
    assert "SCANNED METHODS" in value["text"] and "CALIBRATION" in value["text"]
    assert value["pages"][0]["extraction_method"] == "local_ocr"
    assert value["tools"]["tesseract"].startswith("tesseract ")


def test_actual_page_limit_and_ocr_limit_fail_without_partial_text(native_tools):
    for options in [{**OPTIONS, "max_pages": 1}, {**OPTIONS, "ocr_mode": "always", "max_ocr_pages": 1}]:
        with pytest.raises(documents.DocumentUnavailable) as error:
            documents.extract_pdf(pdf(), options)
        assert error.value.code == "document_limit_exceeded"


def test_actual_import_http_search_page_inspection_and_citation_chain(native_tools, service):
    project, evidence = setup(service)
    raw = pdf(["Methods: compare calibration.", "Uncertainty remains."])
    auth = TokenAuth({"owner": OWNER})
    with TestClient(create_app(service, auth)) as client:
        result = client.post("/v1/operations/research.document.import", headers={"Authorization": "Bearer owner"},
                             json=request(evidence, raw, idempotency_key="actual-import"))
        assert result.status_code == 200, result.text
        result = result.json()
        assert result["status"] == "succeeded"
    body = invoke(service, "research.text.get", {"project_id": project["project_id"], "text_id": result["text_id"]})
    assert body["provenance"]["raw_sha256"] == sha(raw)
    assert body["provenance"]["raw_document_retained"] is False
    assert body["rights_validation"] == "user_attested" and body["claim_verified"] is False
    listed = invoke(service, "research.text.list", {"project_id": project["project_id"]})
    assert "text" not in listed["texts"][0] and "content_base64" not in json.dumps(listed)
    found = invoke(service, "research.text.search", {"project_id": project["project_id"], "text_id": result["text_id"], "query": "Uncertainty"})
    match = found["matches"][0]
    assert match["pages"][0]["page_number"] == 2
    inspected = invoke(service, "research.text.inspect", {"project_id": project["project_id"], "text_id": result["text_id"], "section_id": "page-2"})
    assert inspected["text"] == "Uncertainty remains." and inspected["pages"][0]["sha256"] == body["pages"][1]["sha256"]
    citation = ResearchWorkflow(service)._citations({"project_id": project["project_id"], "citations": [{"evidence_id": evidence["evidence_id"], "text_id": result["text_id"], **{key: match[key] for key in ("start", "end", "quote")}}]})[0]
    assert citation["document_sha256"] == sha(raw) and citation["pages"][0]["page_number"] == 2
    assert citation["text_sha256"] == body["sha256"] and citation["claim_verified"] is False
    assert citation["validation_status"] == "passage_matched_user_supplied_text"
    for kind in ["claim", "hypothesis", "research_proposal", "operation"]:
        assert service.store.list(kind, project["project_id"]) == []


def test_import_replays_deduplicates_and_rejects_rights_conflict(service, monkeypatch):
    project, evidence = setup(service)
    calls = []
    monkeypatch.setattr(documents, "extract_pdf", lambda *args: calls.append(args) or controlled_extraction())
    first = invoke(service, "research.document.import", request(evidence, idempotency_key="first"))
    assert invoke(service, "research.document.import", request(evidence, idempotency_key="first")) == first
    assert len(calls) == 1
    second = invoke(service, "research.document.import", request(evidence))
    assert second["text_id"] == first["text_id"]
    with pytest.raises(ServiceError, match="rights declaration"):
        invoke(service, "research.document.import", request(evidence, license="Other rights"))
    assert len(service.store.list("research_text", project["project_id"])) == 1
    assert len(service.store.list("research_retrieval", project["project_id"])) == 2


@pytest.mark.parametrize("changes", [
    {"rights_confirmed": False}, {"license": " "}, {"expected_evidence_revision": 2},
    {"content_base64": "not canonical base64"}, {"expected_sha256": "0" * 64},
    {"filename": "../../private.pdf"}, {"filename": "file.pdf\n"}, {"filename": "file\x00.pdf"}, {"media_type": "text/html"},
    {"url": "http://127.0.0.1/secret"}, {"ocr_mode": "cloud"}, {"max_pages": 101}, {"max_ocr_pages": 21},
])
def test_invalid_authority_rights_identity_and_inputs_never_parse(service, monkeypatch, changes):
    _, evidence = setup(service)
    monkeypatch.setattr(documents, "extract_pdf", lambda *_: pytest.fail("Unauthorized parsing"))
    with pytest.raises(ServiceError):
        invoke(service, "research.document.import", request(evidence, **changes))
    assert not service.store.list("research_text", evidence["project_id"])


def test_read_scope_project_isolation_and_capabilities_are_inert(service, monkeypatch):
    project, evidence = setup(service)
    monkeypatch.setattr(documents, "extract_pdf", lambda *_: pytest.fail("Unexpected parsing"))
    reader = Actor("reader", {"read"}, {project["project_id"]})
    assert invoke(service, "research.document.capabilities", {"project_id": project["project_id"]}, reader)["network_transmission"] is False
    for actor in [reader, Actor("other", {"read", "modify"}, {"other-project"})]:
        with pytest.raises(ServiceError) as error:
            invoke(service, "research.document.import", request(evidence), actor)
        assert error.value.code == "forbidden"
    monkeypatch.setattr(documents.sys, "platform", "darwin")
    assert not invoke(service, "research.document.capabilities", {"project_id": project["project_id"]})["available"]


@pytest.mark.parametrize("code", ["invalid_document", "document_timeout", "document_limit_exceeded", "document_access_restricted", "document_capability_unavailable"])
def test_extraction_failures_are_durable_redacted_and_replayable(service, monkeypatch, code):
    project, evidence = setup(service)
    def failure(*_):
        raise documents.DocumentUnavailable(code)
    monkeypatch.setattr(documents, "extract_pdf", failure)
    result = invoke(service, "research.document.import", request(evidence, idempotency_key="failed"))
    assert result["status"] == "failed" and result["retrieval"]["error"]["code"] == code
    monkeypatch.setattr(documents, "extract_pdf", lambda *_: pytest.fail("No automatic retry"))
    assert invoke(service, "research.document.import", request(evidence, idempotency_key="failed")) == result
    assert service.store.list("research_text", project["project_id"]) == []


def test_configured_reduced_byte_cap_precedes_parser_and_invalid_config_fails_closed(service, monkeypatch):
    project, evidence = setup(service)
    monkeypatch.setattr(documents, "extract_pdf", lambda *_: pytest.fail("Oversize parsing"))
    monkeypatch.setenv("MODELBUILDER_RESEARCH_MAX_DOCUMENT_BYTES", "1024")
    result = invoke(service, "research.document.import", request(evidence, pdf() + b" " * 2048))
    assert result["retrieval"]["error"]["code"] == "document_limit_exceeded"
    for setting in ["0", "-1", str(documents.MAX_DOCUMENT_BYTES + 1), "junk", "\u0661\u0660\u0662\u0664"]:
        monkeypatch.setenv("MODELBUILDER_RESEARCH_MAX_DOCUMENT_BYTES", setting)
        capability = invoke(service, "research.document.capabilities", {"project_id": project["project_id"]})
        assert not capability["available"] and capability["error"] == "document_configuration_invalid"


def test_unlocked_extraction_conflicting_keys_and_late_authority(service, monkeypatch):
    project, evidence = setup(service)
    entered, release = threading.Event(), threading.Event()
    def slow(*_):
        entered.set()
        assert release.wait(3)
        return controlled_extraction()
    monkeypatch.setattr(documents, "extract_pdf", slow)
    other = OperationService(service.store.path)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(invoke, service, "research.document.import", request(evidence, idempotency_key="pending"))
        assert entered.wait(2)
        try:
            assert not invoke(service, "research.text.list", {"project_id": project["project_id"]})["texts"]
            invoke(other, "evidence.add", {"project_id": project["project_id"], "title": "Unlocked writer", "source_identifier": "during-extraction"})
            with pytest.raises(ServiceError) as error:
                invoke(other, "research.document.import", request(evidence, idempotency_key="pending"))
            assert error.value.code == "request_in_progress"
            with pytest.raises(ServiceError) as error:
                invoke(other, "research.document.import", request(evidence, idempotency_key="pending", ocr_mode="auto"))
            assert error.value.code == "idempotency_conflict"
            service.research_authorizer = lambda *_: False
        finally:
            release.set()
        with pytest.raises(ServiceError, match="revoked"):
            pending.result(3)
    other.store.close()
    assert not service.store.list("research_text", project["project_id"])


@pytest.mark.parametrize("field,value", [("sha256", "wrong"), ("start", 1), ("end", 1000), ("empty", True), ("byte_length", 5), ("page_number", 2), ("extraction_method", "cloud_ocr")])
def test_decoder_output_page_integrity_is_rechecked(field, value):
    extracted = controlled_extraction()
    extracted["pages"][0][field] = value
    with pytest.raises(documents.DocumentUnavailable):
        documents._validate_extracted(extracted, OPTIONS)


def test_real_timeout_terminates_and_reaps_worker_and_cleans_private_files(monkeypatch, tmp_path):
    if sys.platform != "linux":
        pytest.skip("Linux process group boundary")
    original = subprocess.Popen
    pidfile = tmp_path / "worker-pid"
    seen = []
    code = f"import os,time;open({str(pidfile)!r},'w').write(str(os.getpid()));open('source.pdf','wb').write(b'private');time.sleep(30)"
    def slow(command, **options):
        seen.append(options)
        return original([sys.executable, "-I", "-c", code], **options)
    monkeypatch.setattr(documents.subprocess, "Popen", slow)
    monkeypatch.setattr(documents, "WALL_SECONDS", .2)
    monkeypatch.setenv("MODELBUILDER_TOKEN", "never-inherited")
    monkeypatch.setenv("HTTPS_PROXY", "never-inherited")
    with pytest.raises(documents.DocumentUnavailable) as error:
        documents.extract_pdf(pdf(), OPTIONS)
    assert error.value.code == "document_timeout"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
    assert not Path(seen[0]["cwd"]).exists()
    assert "MODELBUILDER_TOKEN" not in seen[0]["env"] and "HTTPS_PROXY" not in seen[0]["env"]
    assert seen[0]["start_new_session"] is True


@pytest.mark.asyncio
async def test_official_mcp_import_actual_pdf_search_and_stage_proposal(native_tools, service):
    import httpx
    from mcp.shared.memory import create_connected_server_and_client_session
    from modelbuilder.mcp_server import OperationGateway, create_server
    project, evidence = setup(service)
    app = create_app(service, TokenAuth({"owner": OWNER}))
    async with httpx.AsyncClient(base_url="http://test", headers={"Authorization": "Bearer owner"}, transport=httpx.ASGITransport(app=app)) as client:
        gateway = OperationGateway(client)
        async with create_connected_server_and_client_session(create_server(gateway, await gateway.catalog())) as session:
            async def call(operation, payload):
                if OPERATIONS[operation].mutating:
                    payload = {"idempotency_key": str(uuid4()), **payload}
                result = await session.call_tool(operation.replace('.', '_'), payload)
                assert not result.isError, result
                return json.loads(result.content[0].text)
            imported = await call("research.document.import", request(evidence))
            assert imported["status"] == "succeeded"
            found = await call("research.text.search", {"project_id": project["project_id"], "text_id": imported["text_id"], "query": "calibration"})
            match = found["matches"][0]
            hypothesis = await call("hypothesis.create", {"project_id": project["project_id"], "evidence_ids": [evidence["evidence_id"]], "proposed_change": "Measure calibration", "expected_outcome": "Test the method", "acceptance_metric": {"name": "brier", "direction": "minimize"}})
            recipe = invoke(service, "recipe.get", {"project_id": project["project_id"], "recipe_id": project["recipe_id"]})
            proposal = await call("research.proposal.create", {"project_id": project["project_id"], "hypothesis_id": hypothesis["hypothesis_id"], "title": "PDF cited method", "rationale": "Measure rather than assume scientific validity", "citations": [{"evidence_id": evidence["evidence_id"], "text_id": imported["text_id"], **{key: match[key] for key in ("quote", "start", "end")}}], "recipe": {**deepcopy(recipe), "description": "PDF-cited experiment"}, "expected_recipe_revision": 1})
            assert proposal["citations"][0]["pages"][0]["page_number"] == 1
            assert proposal["citations"][0]["document_sha256"] == sha(pdf())
            assert proposal["citations"][0]["claim_verified"] is False
            assert not service.store.list("operation", project["project_id"])


@pytest.mark.asyncio
async def test_mcp_document_request_has_sufficient_bounded_timeout_only_for_import():
    import httpx
    from modelbuilder.mcp_server import OperationGateway
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})
    async with httpx.AsyncClient(base_url="http://test", timeout=30, transport=httpx.MockTransport(respond)) as client:
        gateway = OperationGateway(client)
        await gateway.invoke("research.document.import", {})
        await gateway.invoke("research.document.capabilities", {})
    assert requests[0].extensions["timeout"]["read"] == 120
    assert requests[1].extensions["timeout"]["read"] == 30


def test_actual_empty_page_reader_keeps_empty_page_hash(native_tools, service):
    project, evidence = setup(service)
    result = invoke(service, "research.document.import", request(evidence, pdf(["First page.", "", "Last page."])))
    inspected = invoke(service, "research.text.inspect", {"project_id": project["project_id"], "text_id": result["text_id"], "section_id": "page-2"})
    assert inspected["text"] == "" and inspected["next_start"] is None
    assert inspected["pages"][0]["page_number"] == 2 and inspected["pages"][0]["sha256"] == sha(b"")


@pytest.mark.parametrize("change", ["evidence", "lease", "archive"])
def test_document_commit_rechecks_evidence_revision_lease_and_project(service, monkeypatch, change):
    project, evidence = setup(service)
    def extract(*_):
        with service.store.transaction():
            if change == "evidence":
                service.store.put("evidence", evidence["evidence_id"], project["project_id"], {**evidence, "revision": 2, "title": "Changed"}, 1)
            elif change == "lease":
                service.store.connection.execute("UPDATE research_requests SET expires_at='2000-01-01T00:00:00+00:00'")
            else:
                service.store.put("project", project["project_id"], project["project_id"], {**project, "revision": 2, "status": "archived"}, 1)
        return controlled_extraction()
    monkeypatch.setattr(documents, "extract_pdf", extract)
    with pytest.raises(ServiceError):
        invoke(service, "research.document.import", request(evidence))
    assert not service.store.list("research_text", project["project_id"])
    assert not service.store.list("research_retrieval", project["project_id"])


def test_document_import_rechecks_exact_initiating_grant_with_another_actor_token(service, monkeypatch):
    project, evidence = setup(service)
    auth = TokenAuth({"first": OWNER, "second": OWNER})
    app = create_app(service, auth)
    def extract(*_):
        auth._tokens.pop("first")
        return controlled_extraction()
    monkeypatch.setattr(documents, "extract_pdf", extract)
    with TestClient(app) as client:
        result = client.post("/v1/operations/research.document.import", headers={"Authorization": "Bearer first"},
                             json=request(evidence, idempotency_key="revoked"))
    assert result.status_code == 403
    assert not service.store.list("research_text", project["project_id"])


def test_document_admission_is_bounded_across_store_connections(service, monkeypatch):
    project, evidence = setup(service)
    entered, release = threading.Barrier(3), threading.Event()
    def extract(*_):
        entered.wait(3)
        assert release.wait(3)
        return controlled_extraction()
    monkeypatch.setattr(documents, "extract_pdf", extract)
    other = OperationService(service.store.path)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(invoke, service, "research.document.import", request(evidence, idempotency_key="first"))
        second = pool.submit(invoke, other, "research.document.import", request(evidence, idempotency_key="second"))
        entered.wait(3)
        try:
            with pytest.raises(ServiceError) as error:
                invoke(service, "research.document.import", request(evidence, idempotency_key="third"))
            assert error.value.code == "source_busy"
        finally:
            release.set()
        assert first.result(3)["text_id"] == second.result(3)["text_id"]
    other.store.close()
    assert len(service.store.list("research_text", project["project_id"])) == 1


def test_actual_worker_limits_are_set_before_decoder_calls(tmp_path):
    if sys.platform != "linux":
        pytest.skip("Linux resource boundary")
    module = Path(documents.__file__).with_name("research_document_decode.py")
    code = f'''import importlib.util,json,resource
spec=importlib.util.spec_from_file_location("decoder",{str(module)!r})
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
def check(data,options,directory):
 return {{"limits":{{name:resource.getrlimit(getattr(resource,name)) for name in ("RLIMIT_AS","RLIMIT_CPU","RLIMIT_FSIZE","RLIMIT_NOFILE","RLIMIT_CORE")}}}}
module.decode=check
module.main()
'''
    result = subprocess.run([sys.executable, "-I", "-c", code], input=json.dumps(OPTIONS).encode() + b"\n" + pdf(),
                            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmp_path, timeout=5)
    limits = json.loads(result.stdout)["limits"]
    assert limits == {"RLIMIT_AS": [768 * 1024 * 1024] * 2, "RLIMIT_CPU": [60, 61],
                      "RLIMIT_FSIZE": [16 * 1024 * 1024] * 2, "RLIMIT_NOFILE": [64, 64], "RLIMIT_CORE": [0, 0]}


def test_actual_timeout_worker_reaps_its_running_tool(monkeypatch, tmp_path):
    if sys.platform != "linux":
        pytest.skip("Linux process group boundary")
    original = subprocess.Popen
    child_pidfile, worker_pidfile = tmp_path / "tool-pid", tmp_path / "worker-pid"
    module = Path(documents.__file__).with_name("research_document_decode.py")
    code = f'''import importlib.util,os,sys
spec=importlib.util.spec_from_file_location("decoder",{str(module)!r})
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
open({str(worker_pidfile)!r},"w").write(str(os.getpid()))
def check(data,options,directory):
 module.run_tool([sys.executable,"-I","-c",{f'import os,time;open({str(child_pidfile)!r},"w").write(str(os.getpid()));time.sleep(30)'!r}])
module.decode=check
try: module.main()
except module.DecodeError: pass
'''
    def run(command, **options):
        return original([sys.executable, "-I", "-c", code], **options)
    monkeypatch.setattr(documents.subprocess, "Popen", run)
    monkeypatch.setattr(documents, "WALL_SECONDS", .5)
    with pytest.raises(documents.DocumentUnavailable) as error:
        documents.extract_pdf(pdf(), OPTIONS)
    assert error.value.code == "document_timeout"
    for pidfile in (child_pidfile, worker_pidfile):
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)


def test_successful_worker_pid_remains_reserved_until_group_cleanup(native_tools, monkeypatch):
    original = os.killpg
    held = []
    def signal_group(pid, signal_value):
        # A completed but unreaped leader still owns this exact process-group
        # identity. No unrelated process can acquire its PID before the signal.
        state = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        held.append(state is not None and state.si_pid == pid)
        return original(pid, signal_value)
    monkeypatch.setattr(documents.os, "killpg", signal_group)
    documents.extract_pdf(pdf(), OPTIONS)
    assert held and all(held)
