"""Explicit legal source retrieval and inert, offset-preserving paper inspection.

Only Europe PMC OA XML is fetched; arbitrary URLs, PDFs and provider inference
are unsupported. The source is untrusted data, never code or instructions.
"""
from __future__ import annotations

import copy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree
from xml.parsers import expat

from .models import ServiceError, new_id, utcnow
from .research import MAX_TEXT_BYTES, _date, _text_hash

MAX_RESPONSE_BYTES = 4_000_000
FETCH_TIMEOUT_SECONDS = 20
PMC_PATTERN = r"PMC[1-9][0-9]{0,9}"
ERRORS = {
    "source_unavailable": "The public source is unavailable; retry explicitly with a new request key.",
    "fetch_timeout": "The public source exceeded the 20-second retrieval window.",
    "response_too_large": "The source exceeded the 4 MB XML or 200000-byte extracted-text limit.",
    "redirect_refused": "The source redirected; no redirect was followed.",
    "unsupported_format": "Only uncompressed UTF-8 Europe PMC JATS XML is supported.",
    "access_denied": "The source denied access. No alternate route was attempted.",
    "rights_unknown": "A supported explicit article license was not found; no article text was retained.",
    "identity_mismatch": "The returned paper does not match the requested PMC identifier and saved citation.",
    "invalid_document": "The source XML is invalid, unsafe, incomplete or exceeds structural limits.",
}


class ContentUnavailable(RuntimeError):
    def __init__(self, code):
        self.code = code if code in ERRORS else "source_unavailable"
        super().__init__(ERRORS[self.code])


def license_url(value):
    """A narrow license allowlist, never a substring or an OA/free-text claim."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (parsed.scheme not in {"https", "http"} or parsed.netloc.lower() not in
            {"creativecommons.org", "www.creativecommons.org"} or parsed.query or parsed.fragment):
        return None
    path = parsed.path.rstrip("/")
    match = re.fullmatch(r"/licenses/(by|by-sa)/(1\.0|2\.0|2\.5|3\.0|4\.0)(?:/legalcode)?", path)
    if match:
        return f"https://creativecommons.org/licenses/{match[1]}/{match[2]}/"
    if path in {"/publicdomain/zero/1.0", "/publicdomain/zero/1.0/legalcode"}:
        return "https://creativecommons.org/publicdomain/zero/1.0/"
    return None


def fetch_xml(identifier):
    if not isinstance(identifier, str) or not re.fullmatch(PMC_PATTERN, identifier):
        raise ContentUnavailable("identity_mismatch")
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    try:
        response = subprocess.run([sys.executable, "-I", str(Path(__file__).with_name("research_content_fetch.py"))],
                                  input=json.dumps({"source_identifier": identifier}).encode(),
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment,
                                  timeout=FETCH_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ContentUnavailable("fetch_timeout") from exc
    except OSError as exc:
        raise ContentUnavailable("source_unavailable") from exc
    if response.returncode:
        raise ContentUnavailable({2: "response_too_large", 3: "redirect_refused", 4: "unsupported_format",
                                  5: "access_denied", 124: "fetch_timeout"}.get(response.returncode,
                                                                             "source_unavailable"))
    if len(response.stdout) > MAX_RESPONSE_BYTES:
        raise ContentUnavailable("response_too_large")
    return response.stdout


def _plain(element):
    if element is None:
        return ""
    blocks = {"p", "title", "sec", "td", "th", "tr", "table", "list-item", "list", "caption", "label", "break"}
    def pieces(node):
        yield node.text or ""
        for child in node:
            if child.tag in blocks:
                yield " "
            yield from pieces(child)
            if child.tag in blocks:
                yield " "
            yield child.tail or ""
    return " ".join("".join(pieces(element)).split())


def _doi(value):
    value = value.strip().lower() if isinstance(value, str) else ""
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value.rstrip("/") if re.fullmatch(r"10\.[0-9]{4,9}/\S+", value) else ""


def _pmc(value):
    if not isinstance(value, str):
        return ""
    if re.fullmatch(PMC_PATTERN, value):
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme in {"http", "https"} and parsed.netloc.lower() in {
            "europepmc.org", "www.europepmc.org", "pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"}:
        match = re.fullmatch(r"/(?:articles|pmc/articles)/(PMC[1-9][0-9]{0,9})/?", parsed.path)
        if match and not parsed.query and not parsed.fragment:
            return match[1]
    return ""


def parse_article(raw, identifier, evidence):
    """Reject entities/internal DTDs; external DTD declarations are never loaded."""
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ContentUnavailable("response_too_large")
    try:
        xml = raw.decode("utf-8-sig")
        # Accept only UTF-8. This also prevents alternate-encoding declaration
        # tricks and ensures exact wire hashing is separate from normalized text.
        declaration = re.match(r"\s*<\?xml[^?]*encoding\s*=\s*['\"]([^'\"]+)", xml, re.I)
        if declaration and declaration[1].lower() not in {"utf-8", "utf8"}:
            raise ContentUnavailable("unsupported_format")
        parser = expat.ParserCreate()
        def deny(*_):
            raise ContentUnavailable("invalid_document")
        def doctype(_name, _system, _public, internal):
            if internal:
                deny()
        parser.StartDoctypeDeclHandler = doctype
        parser.EntityDeclHandler = deny
        parser.ExternalEntityRefHandler = deny
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        parser.Parse(raw, True)
        root = ElementTree.fromstring(xml)
    except (UnicodeError, expat.ExpatError, ElementTree.ParseError, ValueError) as exc:
        raise ContentUnavailable("invalid_document") from exc
    if root.tag != "article":
        raise ContentUnavailable("invalid_document")
    count, stack = 0, [(root, 0)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > 40000 or depth > 64:
            raise ContentUnavailable("invalid_document")
        stack.extend((child, depth + 1) for child in element)
    meta = root.find("./front/article-meta")
    if meta is None:
        raise ContentUnavailable("invalid_document")
    ids = list(meta.findall("article-id"))
    pmcs = {(_plain(item) if _plain(item).startswith("PMC") else "PMC" + _plain(item))
            for item in ids if item.get("pub-id-type") in {"pmc", "pmcid"}}
    dois = {_doi(_plain(item)) for item in ids if item.get("pub-id-type") == "doi"} - {""}
    values = [evidence.get("source_identifier"), evidence.get("source_url"),
              evidence.get("citation_metadata", {}).get("doi")]
    if identifier not in pmcs or not (any(_pmc(value) == identifier for value in values) or
                                     any(_doi(value) in dois for value in values if _doi(value))):
        raise ContentUnavailable("identity_mismatch")
    licenses = meta.findall("./permissions/license")
    accepted = set()
    for item in licenses:
        if item.get("specific-use"):
            raise ContentUnavailable("rights_unknown")
        hrefs = [value for node in item.iter() for key, value in node.attrib.items()
                 if key in {"href", "{http://www.w3.org/1999/xlink}href"}]
        for href in hrefs:
            try:
                target = urlsplit(href)
            except ValueError:
                raise ContentUnavailable("rights_unknown") from None
            if (target.netloc.lower() in {"creativecommons.org", "www.creativecommons.org"}
                    and target.path.startswith(("/licenses/", "/publicdomain/")) and not license_url(href)):
                raise ContentUnavailable("rights_unknown")
        links = {license_url(value) for value in hrefs}
        links.discard(None)
        if len(links) != 1:
            raise ContentUnavailable("rights_unknown")
        accepted.update(links)
    if len(accepted) != 1:
        raise ContentUnavailable("rights_unknown")
    license_value = accepted.pop()
    title = _plain(meta.find("./title-group/article-title"))
    authors = []
    for item in meta.findall("./contrib-group/contrib"):
        if item.get("contrib-type", "author") != "author":
            continue
        name = item.find("name")
        author = " ".join(_plain(part) for part in name) if name is not None else _plain(item.find("collab"))
        if author:
            authors.append(author)
    statement = "\n".join(_plain(item) for item in licenses)
    copyright_statement = _plain(meta.find("./permissions/copyright-statement"))
    if not title or len(statement) > 8000 or len(json.dumps([title, authors, copyright_statement]).encode()) > 64000:
        raise ContentUnavailable("invalid_document")
    parts, sections = [], []
    length = 0
    def append(value):
        nonlocal length
        if value:
            parts.append(value + "\n\n")
            length += len(value) + 2
    def section(element, label, kind):
        start = length
        append(label)
        record = {"section_id": f"section-{len(sections) + 1}", "title": label, "kind": kind, "start": start}
        sections.append(record)
        if len(sections) > 1000:
            raise ContentUnavailable("invalid_document")
        for child in element:
            if child.tag == "title":
                continue
            if child.tag == "sec":
                section(child, _plain(child.find("title")) or "Untitled section", child.get("sec-type", "section"))
            else:
                append(_plain(child))
        record["end"] = length
    append(title)
    for abstract in meta.findall("abstract"):
        section(abstract, _plain(abstract.find("title")) or "Abstract", "abstract")
    body = root.find("body")
    if body is None or not _plain(body):
        raise ContentUnavailable("invalid_document")
    section(body, "Body", "body")
    text = "".join(parts)
    if "\x00" in text or len(text.encode()) > MAX_TEXT_BYTES:
        raise ContentUnavailable("response_too_large")
    return {"text": text, "sections": sections, "license": license_value,
            "rights_basis": "public_domain" if "/publicdomain/" in license_value else "licensed",
            "rights_validation": "explicit_article_license_uri", "rights_confirmed": True,
            "source_validation": "provider_xml_identifier_matched", "claim_verified": False,
            "attribution": {"title": title, "authors": authors, "copyright": copyright_statement,
                            "license_statement": statement, "license_url": license_value},
            "provenance": {"provider": "europe_pmc", "source_identifier": identifier,
                           "source_url": f"https://www.ebi.ac.uk/europepmc/webservices/rest/{identifier}/fullTextXML",
                           "article_url": f"https://europepmc.org/articles/{identifier}",
                           "format": "jats_xml", "extractor": "modelbuilder-jats-text-v1",
                           "raw_sha256": hashlib.sha256(raw).hexdigest(), "raw_byte_length": len(raw),
                           "coverage": "article_title_abstract_body_text", "complete_publisher_rendering": False,
                           "omitted": ["reference_list", "supplementary_files", "images", "layout", "formula_layout"]}}


def page_references(body, start, end):
    """Intersect a Unicode passage with physical PDF pages, without inference."""
    return [{**page, "passage_start": max(start, page["start"]), "passage_end": min(end, page["end"])}
            for page in body.get("pages", []) if page["start"] < end and page["end"] > start]


def inspect_text(body, payload):
    """Local section/passage inspection with stable Unicode offsets and hash."""
    content = body["text"]
    sections = body.get("sections") or [{"section_id": "document", "title": "Imported text", "kind": "document",
                                        "start": 0, "end": len(content)}]
    selected = None
    if payload.get("section_id"):
        selected = next((item for item in sections if item["section_id"] == payload["section_id"]), None)
        if selected is None:
            raise ServiceError("not_found", "Section is not part of this text snapshot", status_code=404)
    lower, upper = (selected["start"], selected["end"]) if selected else (0, len(content))
    start = payload.get("start", lower)
    if start < lower or start > upper or (start == upper and lower != upper):
        raise ServiceError("validation_error", "Start offset is outside the selected text or section")
    end = min(upper, start + payload.get("length", 8000))
    pages = page_references(body, start, end)
    if selected and selected.get("kind") == "page" and lower == upper:
        pages = [{**page, "passage_start": start, "passage_end": end} for page in body.get("pages", [])
                 if page["page_number"] == selected.get("page_number")]
    return {"project_id": body["project_id"], "text_id": body["text_id"], "evidence_id": body["evidence_id"],
            "sha256": body["sha256"], "title": body.get("attribution", {}).get("title") or body["citation"]["title"], "text": content[start:end],
            "start": start, "end": end, "next_start": end if end < upper else None, "sections": sections,
            "section_id": selected["section_id"] if selected else None, "license": body["license"],
            "rights_validation": body["rights_validation"], "source_validation": body["source_validation"],
            "attribution": body.get("attribution", {}), "provenance": body.get("provenance", {}),
            "pages": pages,
            "claim_verified": False}


def literal_matches(content, query, *, case_sensitive=False, start=0):
    """Shared literal matcher; Unicode offsets always refer to the original text."""
    if not query.strip():
        raise ServiceError("validation_error", "Local search needs a nonblank literal query")
    return re.compile(re.escape(query), 0 if case_sensitive else re.IGNORECASE).finditer(content, start)


def search_text(body, payload):
    query = payload["query"]
    if not query.strip():
        raise ServiceError("validation_error", "Within-paper search needs a nonblank literal query")
    content, limit, matches = body["text"], payload.get("limit", 20), []
    for match in literal_matches(content, query, case_sensitive=payload.get("case_sensitive", False),
                                 start=payload.get("start", 0)):
        if len(matches) == limit:
            break
        start, end = match.span()
        left, right = max(0, start - 120), min(len(content), end + 120)
        matches.append({"start": start, "end": end, "quote": content[start:end],
                        "context": content[left:right], "context_start": left, "context_end": right,
                        "pages": page_references(body, start, end),
                        "section_ids": [item["section_id"] for item in body.get("sections", [])
                                        if item["start"] <= start < item["end"]]})
    else:
        match = None
    return {"project_id": body["project_id"], "text_id": body["text_id"], "evidence_id": body["evidence_id"],
            "sha256": body["sha256"], "query": query, "case_sensitive": payload.get("case_sensitive", False), "matches": matches,
            "next_start": match.start() if match is not None and len(matches) == limit else None,
            "search_kind": "literal_local_text", "claim_verified": False}


class ContentRetrieval:
    """Short claim/commit transactions, with provider I/O outside store locks."""
    lease_seconds = 40
    failure_types = (ContentUnavailable,)
    errors = ERRORS

    def __init__(self, service):
        self.service, self.store = service, service.store

    def prepare(self, payload, evidence):
        return parse_article(fetch_xml(payload["source_identifier"]), payload["source_identifier"], evidence)

    def attempt_source(self, payload):
        return {"source_identifier": payload["source_identifier"], "provider": "europe_pmc",
                "source_url": f"https://www.ebi.ac.uk/europepmc/webservices/rest/{payload['source_identifier']}/fullTextXML"}

    def text_identity(self, payload, evidence, document):
        return (f"{payload['project_id']}:{evidence['evidence_id']}:{document['provenance']['raw_sha256']}:"
                f"{document['license']}:{document['provenance']['extractor']}")

    def validate_existing(self, text, document):
        pass

    def invoke(self, operation, payload, actor, payload_hash, request_id):
        from .research_scheduler import authorized
        key, project_id, token = payload["idempotency_key"], payload["project_id"], new_id()
        with self.store.transaction():
            # This explicit request belongs to its initiating grant, unlike an
            # actor-owned schedule. A second token cannot revive a revoked one.
            self.service._require_current_authority(operation, actor, project_id)
            project = self.service._get("project", project_id, project_id)
            if not authorized(self.service, actor, project_id) or project["status"] != "active":
                raise ServiceError("forbidden", "Research authority or project access was revoked", status_code=403)
            previous = self.store.idempotent_result(actor.id, key, operation, payload_hash, allow_pending=True)
            if previous is not None:
                return previous
            pending = self.store.connection.execute("SELECT * FROM research_requests WHERE actor_id=? AND key=?",
                                                     (actor.id, key)).fetchone()
            if pending and pending["expires_at"] > utcnow():
                raise ServiceError("request_in_progress", "This content retrieval is already running", status_code=409)
            evidence = self.service._get("evidence", payload["evidence_id"], project_id)
            self.service._revision(evidence, payload["expected_evidence_revision"])
            active = self.store.connection.execute(
                "SELECT COUNT(*) FROM research_requests WHERE operation=? AND expires_at>?", (operation, utcnow())).fetchone()[0]
            if active >= 2:
                raise ServiceError("source_busy", "Two source-content requests are already running; retry later", status_code=409)
            expiry = (_date(utcnow()) + timedelta(seconds=self.lease_seconds)).isoformat()
            self.store.connection.execute("INSERT OR REPLACE INTO research_requests VALUES(?,?,?,?,?,?,?)",
                (actor.id, key, operation, payload_hash, token, expiry, "{}"))
        started, document, error = utcnow(), None, None
        try:
            try:
                document = self.prepare(payload, evidence)
            except self.failure_types as exc:
                error = {"code": exc.code, "message": self.errors[exc.code]}
            with self.store.transaction():
                pending = self.store.connection.execute("SELECT * FROM research_requests WHERE actor_id=? AND key=?",
                                                         (actor.id, key)).fetchone()
                if pending is None or pending["token"] != token or pending["expires_at"] <= utcnow():
                    raise ServiceError("research_lease_lost", "Content retrieval lease expired; retry explicitly", status_code=409)
                self.service._require_current_authority(operation, actor, project_id)
                if not authorized(self.service, actor, project_id) or self.service._get("project", project_id, project_id)["status"] != "active":
                    raise ServiceError("forbidden", "Research authority or project access was revoked", status_code=403)
                current = self.service._get("evidence", evidence["evidence_id"], project_id)
                self.service._revision(current, evidence["revision"])
                attempt = {"retrieval_id": new_id(), "project_id": project_id, "evidence_id": evidence["evidence_id"],
                           "evidence_revision": evidence["revision"], **self.attempt_source(payload),
                           "actor": actor.id, "started_at": started, "finished_at": utcnow(),
                           "status": "failed" if error else "succeeded", "error": error, "revision": 1,
                           "text_id": None, "claim_verified": False}
                if document is not None:
                    content_hash = _text_hash(document["text"])
                    identity = self.text_identity(payload, evidence, document)
                    text_id = str(uuid5(NAMESPACE_URL, "modelbuilder:fetched-text:" + identity))
                    try:
                        text = self.service._get("research_text", text_id, project_id)
                    except ServiceError as exc:
                        if exc.code != "not_found":
                            raise
                        text = {**document, "text_id": text_id, "project_id": project_id,
                                "evidence_id": evidence["evidence_id"], "evidence_revision": evidence["revision"],
                                "citation": copy.deepcopy(evidence), "sha256": content_hash,
                                "byte_length": len(document["text"].encode()), "revision": 1,
                                "imported_at": started, "imported_by": actor.id}
                        self.store.put("research_text", text_id, project_id, text)
                    self.validate_existing(text, document)
                    attempt["text_id"] = text_id
                self.store.put("research_retrieval", attempt["retrieval_id"], project_id, attempt)
                result = {"project_id": project_id, "retrieval": attempt, "text_id": attempt["text_id"],
                          "status": attempt["status"], "claim_verified": False}
                self.store.save_idempotent(actor.id, key, operation, payload_hash, result)
                self.store.audit(actor_id=actor.id, operation=operation, request_id=request_id,
                                 outcome=attempt["status"], project_id=project_id, target_id=attempt["retrieval_id"])
                return result
        finally:
            with self.store.transaction():
                self.store.connection.execute("DELETE FROM research_requests WHERE actor_id=? AND key=? AND token=?",
                                              (actor.id, key, token))
