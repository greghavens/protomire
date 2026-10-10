"""Durable evidence-linked research review, without an embedded research agent.

The operation service owns authorization, transactions, audit and idempotency.
This module stores citations, source text, watches and staged recipe changes. It
never trains a model, executes imported text, verifies
paper conclusions or promotes model versions. Source matches mean only that the
quoted characters occur in the exact local source snapshot, with its recorded
source and rights validation.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import difflib
import hashlib
from http.client import HTTPException
import json
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from . import recipe as recipe_module
from .models import Evidence, ServiceError, new_id, utcnow
from .research_contract import WATCH_FIELDS

MAX_TEXT_BYTES = 200_000
MAX_METADATA_BYTES = 64_000
METADATA_FIELDS = ("source_url", "source_identifier", "title", "authors", "publication_date", "citation_metadata")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _date(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Research timestamps must have a timezone")
    return parsed.astimezone(timezone.utc)


def _identity(evidence):
    """Prefer provider identity; normalize stable DOI spelling when available."""
    metadata = evidence.get("citation_metadata", {})
    candidates = [str(value).strip() for value in (metadata.get("doi"), evidence.get("source_identifier"),
                                                  evidence.get("source_url")) if value]
    for candidate in candidates:
        lowered = candidate.casefold()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if lowered.startswith(prefix):
                return "doi:" + lowered[len(prefix):].strip().rstrip("/")
        if lowered.startswith("10.") and "/" in lowered:
            return "doi:" + lowered.rstrip("/")
    return candidates[0].rstrip("/").casefold() if candidates else ""


def _evidence_key(evidence, text=None):
    # Re-adding the same citation with a fresh UUID or retrieval timestamp does
    # not turn it into new evidence for reconsidering a rejected experiment.
    content = text["sha256"] if text else evidence.get("content_hash")
    return _hash({"source": _identity(evidence), "content": content})


def _citation_keys(citations):
    # A passage includes its underlying citation, not a replacement identity.
    # Dropping the quote after rejection must never manufacture "new" evidence.
    return {key for citation in citations for key in
            (citation["evidence_key"], _evidence_key(citation["snapshot"]))}


def _intent(hypothesis):
    return _hash({"change": " ".join(hypothesis["proposed_change"].casefold().split()),
                  "scope": " ".join(hypothesis.get("scope", "").casefold().split()),
                  "metric": hypothesis["acceptance_metric"]})


def _recipe_body(recipe):
    return {key: value for key, value in recipe.items() if key not in {"digest", "import_report"}}


def _diff(before, after):
    return "\n".join(difflib.unified_diff(
        json.dumps(before, indent=2, sort_keys=True, ensure_ascii=False).splitlines(),
        json.dumps(after, indent=2, sort_keys=True, ensure_ascii=False).splitlines(),
        fromfile="base recipe", tofile="proposed recipe", lineterm=""))


class ResearchWorkflow:
    def __init__(self, service):
        self.service = service
        self.store = service.store

    def dispatch(self, operation, payload, actor):
        _, kind, action = operation.split(".")
        if kind == "watch":
            return self._watch(action, payload, actor)
        if kind == "finding":
            return self._finding(action, payload, actor)
        if kind == "text":
            return self._text(action, payload, actor)
        if kind == "proposal":
            return self._proposal(action, payload, actor)
        raise ServiceError("unknown_operation", "Research operation is not available", status_code=404)

    def _get(self, kind, identity, project_id):
        return self.service._get("research_" + kind, identity, project_id)

    def _save(self, kind, body, previous=None):
        return self.store.put("research_" + kind, body[kind + "_id"], body["project_id"], body,
                              previous["revision"] if previous else None)

    def _watch(self, action, p, actor):
        project_id = p["project_id"]
        if action == "list":
            return {"watches": [{**watch, "scheduler_running": self.service.research_scheduler_running}
                                for watch in self.store.list("research_watch", project_id)],
                    "scheduler_running": self.service.research_scheduler_running}
        if action == "create":
            now = utcnow()
            body = {"watch_id": new_id(), "project_id": project_id, "name": p["name"].strip(),
                    "query": p["query"].strip(), "provider": p.get("provider", "openalex"),
                    "limit": p.get("limit", 5), "enabled": p.get("enabled", True),
                    "interval_seconds": p.get("interval_seconds", 86400), "created_at": now,
                    "updated_at": now, "created_by": actor.id, "revision": 1,
                    "next_due_at": now, "last_refresh": None, "refresh_count": 0,
                    "scheduler_running": False, "schedule": {"enabled": False}, "claim_verified": False}
            self._validate_watch(body)
            return self._save("watch", body)
        original = self._get("watch", p["watch_id"], project_id)
        if action == "get":
            refreshes = [record for record in self.store.list("research_refresh", project_id)
                         if record["watch_id"] == original["watch_id"]]
            return {**original, "scheduler_running": self.service.research_scheduler_running, "refresh_history": refreshes[-20:]}
        self.service._revision(original, p["expected_revision"])
        if action in {"schedule_enable", "schedule_disable"}:
            from .research_scheduler import authorized
            if action == "schedule_enable":
                if not self.service.research_authorizer or not authorized(self.service, actor, project_id):
                    raise ServiceError("scheduler_authority_unavailable", "A current authenticated actor registry is required to enable automatic research", status_code=403)
                if not original["enabled"] or self.service._get("project", project_id, project_id)["status"] != "active":
                    raise ServiceError("watch_disabled", "Enable this watch in an active project before scheduling", status_code=409)
                if any(p[field] != original[field] for field in ("provider", "query", "limit", "interval_seconds")):
                    raise ServiceError("schedule_disclosure_changed", "Review the current provider, query, result limit and frequency before enabling", status_code=409)
                schedule = {"enabled": True, "actor_id": actor.id, "enabled_at": utcnow(),
                            "disclosure": {field: original[field] for field in ("provider", "query", "limit", "interval_seconds")}}
            else:
                schedule = {**original.get("schedule", {}), "enabled": False,
                            "disabled_at": utcnow(), "disabled_by": actor.id, "reason": "explicit_disable"}
            body = {**original, "schedule": schedule, "revision": original["revision"] + 1, "updated_at": utcnow()}
            return self._save("watch", body, original)
        if action == "update":
            self.service._allowed_changes(p["changes"], WATCH_FIELDS)
            now = utcnow()
            body = {**original, **p["changes"], "revision": original["revision"] + 1, "updated_at": now}
            body["query"], body["name"] = body["query"].strip(), body["name"].strip()
            self._validate_watch(body)
            if any(body[field] != original[field] for field in ("query", "provider", "limit", "interval_seconds", "enabled")):
                body["schedule"] = {**original.get("schedule", {}), "enabled": False,
                                    "disabled_at": now, "reason": "configuration_changed"}
            if any(field in p["changes"] for field in ("query", "provider", "limit", "enabled")):
                body["next_due_at"] = now
            elif "interval_seconds" in p["changes"] and original["last_refresh"]:
                body["next_due_at"] = (_date(original["last_refresh"]["finished_at"])
                                      + timedelta(seconds=body["interval_seconds"])).isoformat()
            return self._save("watch", body, original)
        raise ServiceError("unknown_operation", "Research watch action is not available", status_code=404)

    @staticmethod
    def _validate_watch(body):
        if not body["query"] or not body["name"]:
            raise ServiceError("validation_error", "A watch requires a nonblank name and query")
        if body["provider"] != "openalex":
            raise ServiceError("validation_error", "Only bounded OpenAlex metadata discovery is supported")

    def _metadata(self, raw):
        if not isinstance(raw, dict):
            raise ValueError("Metadata must be an object")
        self.service._reject_secrets(raw)
        body = {field: copy.deepcopy(raw[field]) for field in METADATA_FIELDS if field in raw}
        body.setdefault("citation_metadata", {})
        if not isinstance(body["citation_metadata"], dict):
            raise ValueError("Citation metadata must be an object")
        body["citation_metadata"].update(provider="OpenAlex", metadata_only=True, claim_verified=False)
        # Types must be validated before URL parsing or string methods; source
        # schema failures are persisted as failed attempts, not server errors.
        validated = Evidence(evidence_id="validation", project_id="validation", retrieved_at=utcnow(), **body).model_dump()
        body = {field: validated[field] for field in METADATA_FIELDS}
        if not body.get("source_url") and not body.get("source_identifier"):
            raise ValueError("Metadata has no source identity")
        if body.get("source_url"):
            url = urlsplit(body["source_url"])
            if url.scheme not in {"http", "https"} or not url.hostname:
                raise ValueError("Metadata source URL is invalid")
        if not body.get("title", "").strip():
            raise ValueError("Metadata has no title")
        if len(json.dumps(body, ensure_ascii=False).encode()) > MAX_METADATA_BYTES:
            raise ValueError("Metadata record is too large")
        return body

    def _fetch(self, watch, actor):
        from .literature import LiteratureUnavailable, search_openalex
        project_id, started = watch["project_id"], utcnow()
        refresh = {"refresh_id": new_id(), "project_id": project_id, "watch_id": watch["watch_id"],
                   "watch_revision": watch["revision"], "query": watch["query"], "provider": watch["provider"],
                   "limit": watch["limit"], "actor": actor.id, "started_at": started, "revision": 1,
                   "status": "succeeded", "new_findings": [], "changed_findings": [], "unchanged": 0,
                   "coverage": {"kind": "bounded_ranked_query", "complete": False,
                                "deletions_inferred": False}, "claim_verified": False}
        try:
            raw = search_openalex(watch["query"], watch["limit"])
            if not isinstance(raw, list) or len(raw) > watch["limit"]:
                raise ValueError("Unexpected metadata result count")
            records = [self._metadata(item) for item in raw]
        except (LiteratureUnavailable, ValueError, TypeError, AttributeError, OSError, HTTPException, ServiceError, RuntimeError):
            # Return a persisted failed attempt, not an exception that the
            # enclosing service transaction would roll back. No upstream URL,
            # exception text, response body or potential credentials are saved.
            refresh.update(status="failed", error={"code": "source_unavailable",
                           "message": "Metadata retrieval or source validation failed; retry explicitly."})
            records = []
        return refresh, records

    def _commit_refresh(self, watch, refresh, records):
        project_id, started = watch["project_id"], refresh["started_at"]
        evidence_cache = {(_identity(item), item.get("content_hash")): item
                          for item in self.store.list("evidence", project_id)}
        existing = [finding for finding in self.store.list("research_finding", project_id)
                    if finding["watch_id"] == watch["watch_id"]]
        seen = {(finding["source_key"], finding["metadata_hash"]) for finding in existing}
        latest = {finding["source_key"]: finding for finding in existing}
        for metadata in records:
            source_key, content_hash = _identity(metadata), _hash(metadata)
            if (source_key, content_hash) in seen:
                refresh["unchanged"] += 1
                continue
            previous = latest.get(source_key)
            # Immutable snapshots: a changed title/date/author does not rewrite
            # evidence cited by an existing proposal or scientific decision.
            evidence = evidence_cache.get((source_key, content_hash))
            if evidence is not None and (evidence.get("excerpt") is not None or any(
                    evidence.get(key) != value for key, value in metadata.items())):
                # Manually added evidence may assert any content_hash. It must
                # never substitute different text or verification flags for the
                # metadata we actually retrieved.
                evidence = None
            if evidence is None:
                evidence = Evidence(evidence_id=new_id(), project_id=project_id, retrieved_at=started,
                                    content_hash=content_hash, **metadata).model_dump()
                self.store.put("evidence", evidence["evidence_id"], project_id, evidence)
                evidence_cache[(source_key, content_hash)] = evidence
            finding = {"finding_id": str(uuid5(NAMESPACE_URL, f"modelbuilder:research:{watch['watch_id']}:{source_key}:{content_hash}")),
                       "project_id": project_id, "watch_id": watch["watch_id"], "refresh_id": refresh["refresh_id"],
                       "source_key": source_key, "metadata_hash": content_hash,
                       "evidence_id": evidence["evidence_id"], "evidence_revision": evidence["revision"],
                       "citation": copy.deepcopy(evidence), "status": "unreviewed", "revision": 1,
                       "kind": "changed" if previous else "new", "created_at": started,
                       "previous_finding_id": previous["finding_id"] if previous else None,
                       "previous_evidence_id": previous["evidence_id"] if previous else None,
                       "change_diff": _diff({key: previous["citation"].get(key) for key in metadata},
                                            metadata) if previous else "",
                       "claim_verified": False, "review": None}
            self._save("finding", finding)
            refresh["changed_findings" if previous else "new_findings"].append(finding["finding_id"])
            seen.add((source_key, content_hash))
            latest[source_key] = finding
        refresh["finished_at"] = utcnow()
        self.store.put("research_refresh", refresh["refresh_id"], project_id, refresh)
        updated = {**watch, "revision": watch["revision"] + 1, "updated_at": refresh["finished_at"],
                   "last_refresh": refresh, "refresh_count": watch["refresh_count"] + 1,
                   "next_due_at": (_date(refresh["finished_at"])
                                   + timedelta(seconds=watch["interval_seconds"])).isoformat()}
        self._save("watch", updated, watch)
        return {"watch": updated, "refresh": refresh, "watch_id": watch["watch_id"],
                "project_id": project_id, "revision": updated["revision"]}

    def _finding(self, action, p, actor):
        project_id = p["project_id"]
        if action == "list":
            if p.get("watch_id"):
                self._get("watch", p["watch_id"], project_id)
            findings = self.store.list("research_finding", project_id)
            for field in ("watch_id", "status"):
                if field in p:
                    findings = [finding for finding in findings if finding[field] == p[field]]
            return {"findings": findings, "claim_verified": False}
        if not p["rationale"].strip():
            raise ServiceError("validation_error", "Review rationale must be nonblank")
        original = self._get("finding", p["finding_id"], project_id)
        self.service._revision(original, p["expected_revision"])
        body = {**original, "revision": original["revision"] + 1, "status": p["status"],
                "review": {"actor": actor.id, "at": utcnow(), "rationale": p["rationale"],
                           "claim_verified": False}}
        return self._save("finding", body, original)

    def _text(self, action, p, actor):
        project_id = p["project_id"]
        if action == "list":
            if p.get("evidence_id"):
                self.service._get("evidence", p["evidence_id"], project_id)
            texts = self.store.list("research_text", project_id)
            retrievals = self.store.list("research_retrieval", project_id)
            if p.get("evidence_id"):
                texts = [item for item in texts if item["evidence_id"] == p["evidence_id"]]
                retrievals = [item for item in retrievals if item["evidence_id"] == p["evidence_id"]]
            return {"texts": [{key: value for key, value in item.items() if key != "text"} for item in texts],
                    "retrievals": retrievals, "claim_verified": False}
        if action in {"get", "search", "inspect"}:
            text = self._get("text", p["text_id"], project_id)
            if action == "get":
                return text
            from .research_content import inspect_text, search_text
            return (search_text if action == "search" else inspect_text)(text, p)
        evidence = self.service._get("evidence", p["evidence_id"], project_id)
        self.service._revision(evidence, p["expected_evidence_revision"])
        content = p["text"]
        if not content.strip() or "\x00" in content or len(content.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ServiceError("validation_error", "Source text must be nonblank UTF-8 without NUL and at most 200000 bytes")
        if not p["license"].strip() or p.get("rights_confirmed") is not True:
            raise ServiceError("rights_required", "Confirm the license or permission to retain this text")
        content_hash = _text_hash(content)
        if p.get("expected_sha256", content_hash) != content_hash:
            raise ServiceError("content_hash_mismatch", "Imported source text does not match the expected SHA-256")
        text_id = str(uuid5(NAMESPACE_URL, f"modelbuilder:research-text:{project_id}:{evidence['evidence_id']}:{content_hash}"))
        try:
            prior = self._get("text", text_id, project_id)
        except ServiceError as error:
            if error.code != "not_found":
                raise
        else:
            if (prior["license"], prior["rights_basis"]) != (p["license"], p["rights_basis"]):
                raise ServiceError("rights_conflict", "Identical source text was already imported with a different rights declaration", status_code=409)
            return prior
        body = {"text_id": text_id, "project_id": project_id, "evidence_id": evidence["evidence_id"],
                "evidence_revision": evidence["revision"], "citation": copy.deepcopy(evidence),
                "text": content, "sha256": content_hash, "byte_length": len(content.encode("utf-8")),
                "license": p["license"], "rights_basis": p["rights_basis"], "rights_confirmed": True,
                "rights_validation": "user_attested", "source_validation": "user_supplied_not_independently_verified",
                "imported_at": utcnow(), "imported_by": actor.id, "revision": 1, "claim_verified": False}
        return self._save("text", body)

    def _citations(self, p):
        citations, unique = [], set()
        for reference in p["citations"]:
            evidence = self.service._get("evidence", reference["evidence_id"], p["project_id"])
            citation = {"evidence_id": evidence["evidence_id"], "evidence_revision": evidence["revision"],
                        "snapshot": copy.deepcopy(evidence), "validation_status": "citation_only",
                        "claim_verified": False}
            text = None
            if "text_id" in reference:
                text = self._get("text", reference["text_id"], p["project_id"])
                if text["evidence_id"] != evidence["evidence_id"]:
                    raise ServiceError("citation_mismatch", "Source text belongs to another evidence record")
                start, end = reference["start"], reference["end"]
                if start >= end or end > len(text["text"]) or text["text"][start:end] != reference["quote"]:
                    raise ServiceError("passage_mismatch", "Quoted passage does not match the exact local source-text offsets")
                citation.update(text_id=text["text_id"], text_sha256=text["sha256"],
                                quote=reference["quote"], start=start, end=end,
                                validation_status=("passage_matched_retrieved_text" if text.get("source_validation") == "provider_xml_identifier_matched"
                                                   else "passage_matched_user_supplied_text"),
                                source_validation=text["source_validation"], rights_validation=text["rights_validation"],
                                rights_basis=text["rights_basis"], license=text["license"])
            citation["evidence_key"] = _evidence_key(evidence, text)
            key = (citation["evidence_key"], citation.get("start"), citation.get("end"))
            if key in unique:
                raise ServiceError("duplicate_citation", "Proposal citations must be distinct")
            unique.add(key)
            citations.append(citation)
        return citations

    def _at_time(self, kind, identity, project_id, at):
        current = self.service._get(kind, identity, project_id)
        if not at:
            return current
        versions = self.store.resource_versions(kind, identity)
        eligible = [item for item in versions
                    if _date(item.get("updated_at", item.get("created_at", at))) <= _date(at)]
        # Old externally imported decisions may have incomplete timestamps;
        # retain a conservative current snapshot rather than invent history.
        return eligible[-1] if eligible else current

    def _hypothesis_evidence_keys(self, hypothesis, project_id, at=None):
        identifiers = set(hypothesis.get("evidence_ids", []))
        for claim_id in hypothesis.get("claim_ids", []):
            claim = self._at_time("claim", claim_id, project_id, at)
            identifiers.update(claim.get("evidence_ids", []))
        return {_evidence_key(self.service._get("evidence", identifier, project_id)) for identifier in identifiers}

    def _prior_rejections(self, project_id, hypothesis, intent):
        previous = []
        for proposal in self.store.list("research_proposal", project_id):
            if proposal["status"] == "rejected" and proposal["intent_fingerprint"] == intent:
                previous.append({"kind": "proposal", "id": proposal["proposal_id"],
                                 "rationale": proposal["review"]["rationale"],
                                 "evidence_keys": sorted(_citation_keys(proposal["citations"]))})
        for decision in self.store.list("decision", project_id):
            if decision["outcome"] != "rejected":
                continue
            related = self._at_time("hypothesis", decision["hypothesis_id"], project_id, decision["timestamp"])
            if related["hypothesis_id"] == hypothesis["hypothesis_id"] or _intent(related) == intent:
                evidence_keys = self._hypothesis_evidence_keys(related, project_id, decision["timestamp"])
                executed = {(item["recipe_id"], item["recipe_revision"]) for item in decision.get("run_provenance", [])}
                executed.add((decision["recipe_id"], decision["recipe_revision"]))
                for proposal in self.store.list("research_proposal", project_id):
                    applied = proposal.get("applied")
                    if (proposal["hypothesis_id"] == related["hypothesis_id"] and applied
                            and (applied["recipe_id"], applied["recipe_revision"]) in executed):
                        evidence_keys.update(_citation_keys(proposal["citations"]))
                previous.append({"kind": "decision", "id": decision["decision_id"],
                                 "rationale": decision["rationale"], "evidence_keys": sorted(evidence_keys)})
        for current_hypothesis in self.store.list("hypothesis", project_id):
            last_status, last_intent = None, None
            for snapshot in self.store.resource_versions("hypothesis", current_hypothesis["hypothesis_id"]):
                snapshot_intent = _intent(snapshot)
                rejection_transition = snapshot["status"] == "rejected" and (
                    last_status != "rejected" or last_intent != snapshot_intent)
                last_status, last_intent = snapshot["status"], snapshot_intent
                if not rejection_transition:
                    continue
                if snapshot["hypothesis_id"] != hypothesis["hypothesis_id"] and _intent(snapshot) != intent:
                    continue
                previous.append({"kind": "hypothesis", "id": snapshot["hypothesis_id"],
                                 "revision": snapshot["revision"], "rationale": "Hypothesis was marked rejected",
                                 "evidence_keys": sorted(self._hypothesis_evidence_keys(
                                     snapshot, project_id, snapshot["updated_at"]))})
        return previous

    def _check_reconsideration(self, previous, citations, reason):
        if not previous:
            return
        old_keys = {key for rejection in previous for key in rejection["evidence_keys"]}
        new_keys = {citation["evidence_key"] for citation in citations} - old_keys
        if not new_keys or not reason or not reason.strip():
            raise ServiceError("new_evidence_required", "A previously rejected change requires new source evidence and an explicit reconsideration reason",
                               {"prior_rejections": [{"kind": r["kind"], "id": r["id"], "rationale": r["rationale"]}
                                                      for r in previous], "new_evidence_present": bool(new_keys)}, 409)

    def _proposal(self, action, p, actor):
        project_id = p["project_id"]
        if action == "list":
            return {"proposals": self.store.list("research_proposal", project_id)}
        if action == "create":
            return self._create_proposal(p, actor)
        original = self._get("proposal", p["proposal_id"], project_id)
        if action == "get":
            return original
        self.service._revision(original, p["expected_revision"])
        if action == "review":
            if not p["rationale"].strip():
                raise ServiceError("validation_error", "Review rationale must be nonblank")
            if original["status"] != "proposed":
                raise ServiceError("proposal_state_conflict", "Only an unreviewed proposal can be reviewed; create a new proposal to reconsider", status_code=409)
            if p["outcome"] == "approved":
                self._check_proposal_current(original)
            body = {**original, "status": p["outcome"], "revision": original["revision"] + 1,
                    "review": {"outcome": p["outcome"], "actor": actor.id, "at": utcnow(),
                               "rationale": p["rationale"], "proposal_digest": original["proposal_digest"],
                               "claim_verified": False}}
            return self._save("proposal", body, original)
        if action == "apply":
            if p.get("confirm_apply") is not True or original["status"] != "approved":
                raise ServiceError("proposal_review_required", "Explicitly approve the staged proposal before applying it", status_code=409)
            current = self._check_proposal_current(original)
            self.service._revision(current, p["expected_recipe_revision"])
            if original["review"]["proposal_digest"] != original["proposal_digest"]:
                raise ServiceError("proposal_review_required", "The review does not cover this exact proposal", status_code=409)
            recipe = self.service._save_recipe(original["staged_recipe"], project_id, current["recipe_id"],
                                                current["revision"] + 1, current["revision"])
            self.service._confirm_recipe_review(project_id)
            body = {**original, "status": "applied", "revision": original["revision"] + 1,
                    "applied": {"recipe_id": recipe["recipe_id"], "recipe_revision": recipe["revision"],
                                "recipe_digest": recipe["digest"], "actor": actor.id, "at": utcnow(),
                                "executed": False}}
            self._save("proposal", body, original)
            return {"proposal": body, "recipe": recipe, "project_id": project_id,
                    "proposal_id": body["proposal_id"], "revision": body["revision"], "execution_started": False}
        raise ServiceError("unknown_operation", "Research proposal action is not available", status_code=404)

    def _create_proposal(self, p, actor):
        project_id = p["project_id"]
        if not p["title"].strip() or not p["rationale"].strip():
            raise ServiceError("validation_error", "Proposal title and rationale must be nonblank")
        current = self.service._current_recipe(project_id)
        self.service._revision(current, p["expected_recipe_revision"])
        hypothesis = self.service._get("hypothesis", p["hypothesis_id"], project_id)
        claims = [self.service._get("claim", identifier, project_id) for identifier in hypothesis.get("claim_ids", [])]
        citations = self._citations(p)
        cited_ids = {citation["evidence_id"] for citation in citations}
        linked_ids = set(hypothesis.get("evidence_ids", []))
        for claim in claims:
            linked_ids.update(claim.get("evidence_ids", []))
        if linked_ids and not linked_ids.intersection(cited_ids):
            raise ServiceError("citation_mismatch", "At least one proposal citation must be linked to its hypothesis or claims")
        intent = _intent(hypothesis)
        previous = self._prior_rejections(project_id, hypothesis, intent)
        self._check_reconsideration(previous, citations, p.get("reconsideration_reason"))
        incoming = _recipe_body(copy.deepcopy(p["recipe"]))
        for field in ("recipe_id", "project_id"):
            if field in incoming and incoming[field] != current[field]:
                raise ServiceError("validation_error", "Research proposals cannot replace project or recipe identity")
        if incoming.get("revision", current["revision"]) not in {current["revision"], current["revision"] + 1}:
            raise ServiceError("validation_error", "Staged recipe revision must match its base or next revision")
        incoming.update(recipe_id=current["recipe_id"], project_id=project_id, revision=current["revision"] + 1)
        staged = recipe_module.normalize_recipe(incoming)
        before = _recipe_body(current)
        before["revision"] = staged["revision"]
        if before == staged:
            raise ServiceError("empty_proposal", "The staged recipe contains no configuration or source change")
        proposal_id = new_id()
        # The provenance additions are staged before review, so the approved
        # diff and digest are exactly the bytes later saved as a recipe revision.
        staged["evidence_links"] = sorted(set(staged.get("evidence_links", [])) | cited_ids)
        staged.setdefault("extensions", {})["modelbuilder_research"] = {
            "proposal_id": proposal_id, "hypothesis_id": hypothesis["hypothesis_id"],
            "hypothesis_revision": hypothesis["revision"],
            "evidence_ids": sorted(cited_ids), "claim_verified": False}
        staged = recipe_module.normalize_recipe(staged)
        now = utcnow()
        body = {"proposal_id": proposal_id, "project_id": project_id, "revision": 1,
                "title": p["title"], "rationale": p["rationale"], "hypothesis_id": hypothesis["hypothesis_id"],
                "hypothesis_revision": hypothesis["revision"], "hypothesis_snapshot": hypothesis,
                "claim_snapshots": claims, "citations": citations, "intent_fingerprint": intent,
                "prior_rejections": previous, "reconsideration_reason": p.get("reconsideration_reason"),
                "base_recipe_id": current["recipe_id"], "base_recipe_revision": current["revision"],
                "base_recipe_digest": current["digest"], "staged_recipe": staged,
                "staged_recipe_digest": recipe_module.recipe_digest(staged),
                "recipe_diff": _diff(_recipe_body(current), staged), "status": "proposed",
                "review": None, "applied": None, "created_at": now, "created_by": actor.id,
                "claim_verified": False, "execution_started": False}
        body["proposal_digest"] = _hash(body)
        return self._save("proposal", body)

    def _check_proposal_current(self, proposal):
        project_id = proposal["project_id"]
        current = self.service._current_recipe(project_id)
        self.service._revision(current, proposal["base_recipe_revision"])
        if current["recipe_id"] != proposal["base_recipe_id"] or current["digest"] != proposal["base_recipe_digest"]:
            raise ServiceError("proposal_stale", "The proposal was staged against a different recipe", status_code=409)
        hypothesis = self.service._get("hypothesis", proposal["hypothesis_id"], project_id)
        if hypothesis["revision"] != proposal["hypothesis_revision"]:
            raise ServiceError("proposal_stale", "The hypothesis changed after this proposal was staged", status_code=409)
        for claim in proposal["claim_snapshots"]:
            current_claim = self.service._get("claim", claim["claim_id"], project_id)
            if current_claim["revision"] != claim["revision"]:
                raise ServiceError("proposal_stale", "A linked claim changed after this proposal was staged", status_code=409)
        for citation in proposal["citations"]:
            evidence = self.service._get("evidence", citation["evidence_id"], project_id)
            if evidence["revision"] != citation["evidence_revision"]:
                raise ServiceError("proposal_stale", "Cited evidence changed after this proposal was staged", status_code=409)
        previous = self._prior_rejections(project_id, hypothesis, proposal["intent_fingerprint"])
        self._check_reconsideration(previous, proposal["citations"], proposal.get("reconsideration_reason"))
        return current
