"""Shared, inert operation schemas for evidence-linked research review."""
from __future__ import annotations

STRING = {"type": "string", "minLength": 1}
REVISION = {"type": "integer", "minimum": 1}
PROJECT = {"project_id": STRING}
WATCH_FIELDS = {
    "name": {"type": "string", "minLength": 1, "maxLength": 200},
    "query": {"type": "string", "minLength": 1, "maxLength": 300},
    "provider": {"const": "openalex"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
    "enabled": {"type": "boolean"},
    "interval_seconds": {"type": "integer", "minimum": 900, "maximum": 2592000},
}
WATCH_CHANGES = {"type": "object", "additionalProperties": False, "minProperties": 1,
                 "properties": WATCH_FIELDS}
CITATION = {"type": "object", "additionalProperties": False, "required": ["evidence_id"], "properties": {
    "evidence_id": STRING, "text_id": STRING,
    "quote": {"type": "string", "minLength": 1, "maxLength": 4000},
    "start": {"type": "integer", "minimum": 0}, "end": {"type": "integer", "minimum": 1}},
    "dependentRequired": {"text_id": ["quote", "start", "end"], "quote": ["text_id", "start", "end"],
                          "start": ["text_id", "quote", "end"], "end": ["text_id", "quote", "start"]}}


def operation_specs(spec):
    """Use the application's OperationSpec factory; no alternate transport."""
    return [
        spec("research.watch.create", "modify", "Create a project-scoped bounded metadata query watch; no scheduler starts.",
             {**PROJECT, **WATCH_FIELDS}, ("project_id", "name", "query"), True),
        spec("research.watch.get", "read", "Read a watch, refresh status and next due time.",
             {**PROJECT, "watch_id": STRING}, ("project_id", "watch_id")),
        spec("research.watch.list", "read", "List project research watches; never fetch implicitly.",
             PROJECT, ("project_id",)),
        spec("research.watch.update", "modify", "Edit or disable a watch using optimistic concurrency.",
             {**PROJECT, "watch_id": STRING, "expected_revision": REVISION, "changes": WATCH_CHANGES},
             ("project_id", "watch_id", "expected_revision", "changes"), True),
        spec("research.watch.refresh", "modify", "Explicitly fetch one configured watch, persisting citation snapshots and deduplicated findings.",
             {**PROJECT, "watch_id": STRING, "expected_revision": REVISION},
             ("project_id", "watch_id", "expected_revision"), True),
        spec("research.watch.refresh_due", "modify", "Explicit bounded scheduler tick: refresh up to five enabled due watches; does not start a scheduler.",
             {**PROJECT, "max_watches": {"type": "integer", "minimum": 1, "maximum": 5, "default": 1}}, ("project_id",), True),
        spec("research.finding.list", "read", "Read watch findings with immutable citation and change provenance.",
             {**PROJECT, "watch_id": STRING, "status": {"enum": ["unreviewed", "reviewed", "dismissed"]}},
             ("project_id",)),
        spec("research.finding.review", "modify", "Record human or external-assistant triage without verifying a scientific claim.",
             {**PROJECT, "finding_id": STRING, "expected_revision": REVISION,
              "status": {"enum": ["reviewed", "dismissed"]}, "rationale": STRING},
             ("project_id", "finding_id", "expected_revision", "status", "rationale"), True),
        spec("research.text.import", "modify", "Import bounded user-supplied licensed text, pin bytes to a citation and preserve rights attestation; never fetch a URL.",
             {**PROJECT, "evidence_id": STRING, "expected_evidence_revision": REVISION,
              "text": {"type": "string", "minLength": 1, "maxLength": 200000},
              "license": {"type": "string", "minLength": 1, "maxLength": 300},
              "rights_basis": {"enum": ["self_authored", "public_domain", "licensed", "permission"]},
              "rights_confirmed": {"const": True},
              "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
             ("project_id", "evidence_id", "expected_evidence_revision", "text", "license", "rights_basis", "rights_confirmed"), True),
        spec("research.text.get", "read", "Read locally imported source text and its exact content hash and rights declaration.",
             {**PROJECT, "text_id": STRING}, ("project_id", "text_id")),
        spec("research.proposal.create", "modify", "Stage a citation-linked recipe revision and exact diff for explicit review, without applying or training.",
             {**PROJECT, "hypothesis_id": STRING, "title": {"type": "string", "minLength": 1, "maxLength": 200},
              "rationale": {"type": "string", "minLength": 1, "maxLength": 16000},
              "citations": {"type": "array", "items": CITATION, "minItems": 1, "maxItems": 50},
              "recipe": {"type": "object"}, "expected_recipe_revision": REVISION,
              "reconsideration_reason": {"type": "string", "minLength": 1, "maxLength": 8000}},
             ("project_id", "hypothesis_id", "title", "rationale", "citations", "recipe", "expected_recipe_revision"), True),
        spec("research.proposal.get", "read", "Read a staged proposal with immutable citation snapshots, review and applied revision linkage.",
             {**PROJECT, "proposal_id": STRING}, ("project_id", "proposal_id")),
        spec("research.proposal.list", "read", "List retained research proposals, including rejected and applied proposals.",
             PROJECT, ("project_id",)),
        spec("research.proposal.review", "modify", "Explicitly approve or reject the exact staged proposal; approval does not apply or validate claims.",
             {**PROJECT, "proposal_id": STRING, "expected_revision": REVISION,
              "outcome": {"enum": ["approved", "rejected"]}, "rationale": STRING},
             ("project_id", "proposal_id", "expected_revision", "outcome", "rationale"), True),
        spec("research.proposal.apply", "modify", "Explicitly apply a reviewed proposal as the next revision of the same recipe; never execute training.",
             {**PROJECT, "proposal_id": STRING, "expected_revision": REVISION,
              "expected_recipe_revision": REVISION, "confirm_apply": {"const": True}},
             ("project_id", "proposal_id", "expected_revision", "expected_recipe_revision", "confirm_apply"), True),
    ]
