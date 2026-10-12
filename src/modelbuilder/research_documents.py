"""Explicit user-authorized PDF ingestion into the existing research text store.

There is no URL fetching, filesystem-path API, cloud OCR, model inference, source
execution, or implicit claim verification. Poppler/Tesseract are optional local
system tools, not new embedded research/model frameworks.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .models import ServiceError
from .research import MAX_TEXT_BYTES, _hash
from .research_content import ContentRetrieval

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_BASE64_LENGTH = 4 * ((MAX_DOCUMENT_BYTES + 2) // 3)
MAX_OUTPUT_BYTES = 1_400_000
WALL_SECONDS = 90
_SLOTS = threading.BoundedSemaphore(2)
ERRORS = {
    "document_capability_unavailable": "Bounded PDF extraction requires Linux and installed /usr/bin Poppler tools; local OCR additionally requires Tesseract with English data.",
    "document_configuration_invalid": "The configured research document byte limit is invalid; no document was processed.",
    "document_limit_exceeded": "The PDF exceeded the configured byte/page, 200000-byte text, or local extraction resource limit. No partial text was retained.",
    "document_timeout": "Local document extraction exceeded its 90-second wall deadline. No partial text was retained.",
    "document_busy": "Two local document decoders are active; retry explicitly later.",
    "invalid_document": "The PDF or extraction result is invalid, unsupported or damaged. No partial text was retained.",
    "document_access_restricted": "The PDF is encrypted or access-restricted; no password or restriction override was attempted.",
    "document_no_text": "No readable text was extracted. For scanned pages, explicitly enable local OCR and retry with a new request key.",
}


class DocumentUnavailable(RuntimeError):
    def __init__(self, code):
        self.code = code if code in ERRORS else "invalid_document"
        super().__init__(ERRORS[self.code])


def document_byte_limit():
    value = os.environ.get("MODELBUILDER_RESEARCH_MAX_DOCUMENT_BYTES", str(MAX_DOCUMENT_BYTES))
    if not value.isascii() or not value.isdecimal() or not 1024 <= int(value) <= MAX_DOCUMENT_BYTES:
        raise DocumentUnavailable("document_configuration_invalid")
    return int(value)


def document_capabilities():
    """Read-only discovery: no parser invocation, document read, or network I/O."""
    error = None
    try:
        maximum = document_byte_limit()
    except DocumentUnavailable as exc:
        maximum, error = None, exc.code
    linux = sys.platform == "linux"
    tools = {name: linux and os.path.isfile(f"/usr/bin/{name}") and os.access(f"/usr/bin/{name}", os.X_OK)
             for name in ("pdfinfo", "pdftotext", "pdftoppm", "tesseract")}
    available = linux and all(tools[name] for name in ("pdfinfo", "pdftotext")) and error is None
    return {"available": available, "platform": sys.platform, "format": "application/pdf", "tools": tools,
            "ocr_available": available and tools["pdftoppm"] and tools["tesseract"],
            "ocr_languages": ["eng"], "ocr_language_data_verified": False,
            "max_document_bytes": maximum, "max_text_bytes": MAX_TEXT_BYTES,
            "max_pages": 100, "max_ocr_pages": 20, "wall_timeout_seconds": WALL_SECONDS,
            "network_transmission": False, "containment": "linux_resource_limits_not_hostile_code_sandbox",
            "error": error or (None if available else "document_capability_unavailable"),
            "claim_verified": False}


def _validate_extracted(value, options):
    if not isinstance(value, dict) or set(value) != {"text", "pages", "tools"}:
        raise DocumentUnavailable("invalid_document")
    text, pages, tools = value["text"], value["pages"], value["tools"]
    if (not isinstance(text, str) or not text.strip() or "\x00" in text
            or len(text.encode("utf-8")) > MAX_TEXT_BYTES or not isinstance(pages, list)
            or not 1 <= len(pages) <= options["max_pages"] or not isinstance(tools, dict)
            or set(tools) != ({"pdfinfo", "pdftotext"} if options["ocr_mode"] == "disabled"
                              else {"pdfinfo", "pdftotext", "pdftoppm", "tesseract"})
            or any(not isinstance(version, str) or not version.startswith(name + " ") or len(version) > 200
                   for name, version in tools.items())):
        raise DocumentUnavailable("invalid_document")
    cursor, ocr_count = 0, 0
    for number, page in enumerate(pages, 1):
        if (not isinstance(page, dict) or set(page) != {"page_number", "start", "end", "sha256", "byte_length", "extraction_method", "empty"}
                or type(page["page_number"]) is not int or page["page_number"] != number
                or type(page["start"]) is not int or page["start"] != cursor
                or type(page["end"]) is not int or not cursor <= page["end"] <= len(text)
                or page["extraction_method"] not in {"embedded_text", "local_ocr"}
                or type(page["empty"]) is not bool or type(page["byte_length"]) is not int):
            raise DocumentUnavailable("invalid_document")
        passage = text[cursor:page["end"]]
        raw = passage.encode("utf-8")
        if (page["sha256"] != hashlib.sha256(raw).hexdigest() or page["byte_length"] != len(raw)
                or page["empty"] != (not bool(passage))):
            raise DocumentUnavailable("invalid_document")
        if page["extraction_method"] == "local_ocr":
            ocr_count += 1
        if number < len(pages):
            if text[page["end"]:page["end"] + 2] != "\n\n":
                raise DocumentUnavailable("invalid_document")
            cursor = page["end"] + 2
        else:
            cursor = page["end"]
    if (cursor != len(text) or ocr_count > options["max_ocr_pages"]
            or (options["ocr_mode"] == "disabled" and ocr_count)
            or (options["ocr_mode"] == "always" and ocr_count != len(pages))):
        raise DocumentUnavailable("invalid_document")
    return value


def _wait_unreaped(process, seconds):
    """Keep the session leader's PID reserved until group cleanup is done.

    Popen.communicate/wait reap immediately. Signaling its group after that
    would permit a PID-reuse race with an unrelated process. Linux WNOWAIT
    observes completion without releasing the exact child identity.
    """
    deadline = time.monotonic() + seconds
    while True:
        if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(.02, max(0, deadline - time.monotonic())))


def extract_pdf(data, options):
    if sys.platform != "linux":
        raise DocumentUnavailable("document_capability_unavailable")
    if not _SLOTS.acquire(blocking=False):
        raise DocumentUnavailable("document_busy")
    process = None
    try:
        with (tempfile.TemporaryDirectory(prefix="modelbuilder-document-") as directory,
              tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as source):
            environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                           "HOME": directory, "TMPDIR": directory, "OMP_THREAD_LIMIT": "1",
                           "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
            wire = json.dumps(options, separators=(",", ":")).encode() + b"\n" + data
            source.write(wire)
            source.seek(0)
            try:
                process = subprocess.Popen([sys.executable, "-I", "-B",
                    str(Path(__file__).with_name("research_document_decode.py"))], stdin=source,
                    stdout=output, stderr=subprocess.DEVNULL, env=environment, cwd=directory,
                    start_new_session=True)
                if not _wait_unreaped(process, WALL_SECONDS):
                    raise DocumentUnavailable("document_timeout") from None
                if os.fstat(output.fileno()).st_size > MAX_OUTPUT_BYTES:
                    raise DocumentUnavailable("document_limit_exceeded")
                output.seek(0)
                raw = output.read(MAX_OUTPUT_BYTES + 1)
            except OSError:
                raise DocumentUnavailable("document_capability_unavailable") from None
            finally:
                if process is not None:
                    # Terminate the complete private session, including any
                    # Poppler/Tesseract descendant, before deleting raw input.
                    if not _wait_unreaped(process, 0):
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                            _wait_unreaped(process, 3)
                        except ProcessLookupError:
                            pass
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                raise DocumentUnavailable("document_limit_exceeded" if process.returncode else "invalid_document") from None
            if isinstance(value, dict) and value.get("error") in ERRORS:
                raise DocumentUnavailable(value["error"])
            if process.returncode:
                raise DocumentUnavailable("invalid_document")
            return _validate_extracted(value, options)
    finally:
        _SLOTS.release()


class DocumentImport(ContentRetrieval):
    """Reuse current exact-grant leases, immutable citations, audit and replay."""
    lease_seconds = 120
    failure_types = (DocumentUnavailable,)
    errors = ERRORS

    def prepare(self, payload, evidence):
        if not payload["license"].strip() or payload["rights_confirmed"] is not True:
            raise ServiceError("rights_required", "Confirm permission to retain and extract this document")
        filename = payload["filename"]
        if (not filename.lower().endswith(".pdf") or any(ord(char) < 32 or ord(char) == 127 for char in filename)
                or "/" in filename or "\\" in filename):
            raise ServiceError("validation_error", "Document filename must be a PDF basename without control characters")
        try:
            encoded = payload["content_base64"].encode("ascii")
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, UnicodeError, binascii.Error):
            raise ServiceError("validation_error", "Document content must be canonical base64") from None
        if base64.b64encode(data) != encoded:
            raise ServiceError("validation_error", "Document content must be canonical base64")
        if not data or len(data) > document_byte_limit():
            raise DocumentUnavailable("document_limit_exceeded")
        digest = hashlib.sha256(data).hexdigest()
        if digest != payload["expected_sha256"]:
            raise ServiceError("content_hash_mismatch", "Supplied PDF bytes differ from the expected SHA-256")
        options = {"ocr_mode": payload.get("ocr_mode", "disabled"), "max_pages": payload.get("max_pages", 100),
                   "max_ocr_pages": payload.get("max_ocr_pages", 20)}
        extracted = extract_pdf(data, options)
        ocr = any(page["extraction_method"] == "local_ocr" for page in extracted["pages"])
        return {"text": extracted["text"], "pages": extracted["pages"],
                "sections": [{"section_id": f"page-{page['page_number']}", "title": f"Page {page['page_number']}",
                              "kind": "page", "page_number": page["page_number"],
                              "start": page["start"], "end": page["end"]} for page in extracted["pages"]],
                "license": payload["license"], "rights_basis": payload["rights_basis"], "rights_confirmed": True,
                "rights_validation": "user_attested", "source_validation": "user_supplied_not_independently_verified",
                "claim_verified": False,
                "attribution": {"title": evidence["title"], "authors": evidence.get("authors", []),
                                "license_statement": payload["license"]},
                "provenance": {"provider": "local_document", "format": "pdf", "filename": payload["filename"],
                               "raw_sha256": digest, "raw_byte_length": len(data),
                               "extractor": "modelbuilder-poppler-tesseract-v1", "tools": extracted["tools"],
                               "extraction_options": options, "ocr_used": ocr, "ocr_language": "eng" if ocr else None,
                               "page_count": len(extracted["pages"]), "coverage": "normalized_page_text",
                               "normalization": "UTF-8; CRLF to LF; strip page boundary whitespace; join pages with two LF",
                               "network_transmission": False, "raw_document_retained": False,
                               "complete_publisher_rendering": False,
                               "omitted": ["images", "layout", "formula_layout", "attachments", "annotations"],
                               "warnings": (["OCR may misrecognize words, numbers, equations and reading order."] if ocr else [])
                                  + (["Some pages contain no extracted text."] if any(page["empty"] for page in extracted["pages"]) else [])}}

    def attempt_source(self, payload):
        return {"provider": "local_document", "source_identifier": payload["expected_sha256"],
                "source_url": None, "filename": payload["filename"], "ocr_mode": payload.get("ocr_mode", "disabled")}

    def text_identity(self, payload, evidence, document):
        return "document:" + _hash({"project_id": payload["project_id"], "evidence_id": evidence["evidence_id"],
            "evidence_revision": evidence["revision"], "raw_sha256": document["provenance"]["raw_sha256"],
            "extractor": document["provenance"]["extractor"], "tools": document["provenance"]["tools"],
            "options": document["provenance"]["extraction_options"], "text_sha256": hashlib.sha256(document["text"].encode()).hexdigest()})

    def validate_existing(self, text, document):
        if (text["license"], text["rights_basis"]) != (document["license"], document["rights_basis"]):
            raise ServiceError("rights_conflict", "Identical PDF extraction already has a different rights declaration", status_code=409)
