"""Public domain types and the shared transport-independent operation contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

MAX_BUNDLE_JSON_BYTES = 8 * 1024 * 1024

SCOPES = frozenset({"read", "modify", "execute", "spend", "delete", "publish"})


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid4())


class ServiceError(Exception):
    """An error safe to return through HTTP and MCP without leaking internals."""

    def __init__(self, code: str, message: str, details: Any = None, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details if details is not None else {}
        self.status_code = status_code

    def as_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


@dataclass(frozen=True)
class Actor:
    id: str
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({"read"}))
    project_ids: frozenset[str] | None = None

    def __post_init__(self):
        object.__setattr__(self, "scopes", frozenset(self.scopes))
        if self.project_ids is not None:
            object.__setattr__(self, "project_ids", frozenset(self.project_ids))
        if not self.id or not self.scopes.issubset(SCOPES):
            raise ValueError("Actor requires an identity and known scopes")

    @property
    def actor_id(self) -> str:
        return self.id


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModelProject(DomainModel):
    schema_version: Literal["0"] = "0"
    project_id: str
    id: str
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    model_name: str = Field(min_length=1, max_length=200)
    description: str = ""
    created_at: str
    updated_at: str
    git_remote: str | None = None
    head_commit: str | None = None
    recipe_id: str
    active_recipe_id: str
    recipe_migration: dict = Field(default_factory=dict)
    active_model_version_ref: str | None = None
    research_watch_config: dict | None = None
    status: Literal["active", "archived"] = "active"
    revision: int = Field(ge=1)


class Evidence(DomainModel):
    evidence_id: str
    project_id: str
    source_url: str | None = None
    source_identifier: str | None = None
    title: str = Field(min_length=1)
    authors: list[str] = Field(default_factory=list)
    publication_date: str | None = None
    retrieved_at: str
    content_hash: str | None = None
    excerpt: str | None = None
    citation_metadata: dict = Field(default_factory=dict)
    revision: int = 1


class Claim(DomainModel):
    claim_id: str
    project_id: str
    evidence_ids: list[str]
    proposition: str = Field(min_length=1)
    supports: list[str] = Field(default_factory=list)
    contradicts: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    uncertainty: str = ""
    review_status: Literal["unreviewed", "supported", "contradicted", "uncertain"] = "unreviewed"
    revision: int = 1
    created_at: str
    updated_at: str


class Hypothesis(DomainModel):
    hypothesis_id: str
    project_id: str
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    proposed_change: str = Field(min_length=1)
    expected_outcome: str = Field(min_length=1)
    acceptance_metric: dict
    scope: str = ""
    status: Literal["proposed", "testing", "accepted", "rejected", "inconclusive", "abandoned"] = "proposed"
    revision: int = 1
    created_at: str
    updated_at: str


class Decision(DomainModel):
    decision_id: str
    project_id: str
    hypothesis_id: str
    recipe_id: str
    recipe_revision: int = Field(ge=1)
    run_ids: list[str] = Field(min_length=1)
    selection_rule: dict
    outcome: Literal["accepted", "rejected", "inconclusive"]
    rationale: str = Field(min_length=1)
    actor: str
    timestamp: str
    revision: int = 1


# Shared JSON Schemas describe exactly the payload accepted by service/API/MCP.
S = {"type": "string", "minLength": 1}
POSITIVE_INT = {"type": "integer", "minimum": 1}
OBJECT = {"type": "object"}
NO_OVERRIDES = {"type": "object", "maxProperties": 0, "description": "Edit canonical recipe node parameters instead of execution-time overrides."}
A = {"type": "array", "items": S}
B = {"type": "boolean"}
NULLABLE_STRING = {"type": ["string", "null"]}


@dataclass(frozen=True)
class OperationSpec:
    name: str
    scope: str
    description: str
    properties: dict
    required: tuple[str, ...] = ()
    mutating: bool = False
    additional_scopes: tuple[str, ...] = ()

    @property
    def required_scopes(self) -> tuple[str, ...]:
        return (self.scope, *self.additional_scopes)

    @property
    def input_schema(self) -> dict:
        properties = dict(self.properties)
        properties["request_id"] = S
        if self.mutating:
            properties["idempotency_key"] = {"type": "string", "minLength": 1, "maxLength": 200}
        schema = {"type": "object", "properties": properties,
                  "required": list(self.required) + (["idempotency_key"] if self.mutating else []),
                  "additionalProperties": False}
        if self.name == "recipe.import":
            schema["oneOf"] = [{"required": ["bundle"], "not": {"required": ["bundle_json"]}},
                               {"required": ["bundle_json"], "not": {"required": ["bundle"]}}]
        return schema

    def as_dict(self) -> dict:
        return {"name": self.name, "operation": self.name, "scope": self.scope,
                "description": self.description, "mutating": self.mutating, "required_scopes": list(self.required_scopes),
                "input_schema": self.input_schema}


def _spec(name, scope, description, properties=None, required=(), mutating=False, additional_scopes=()):
    return OperationSpec(name, scope, description, properties or {}, tuple(required), mutating, tuple(additional_scopes))


P = {"project_id": S}
R = {**P, "recipe_id": S}
RUN = {**P, "operation_id": S, "backend_run_id": S}
VERSION = {**P, "model_version_id": S, "model_version_ref": S}
RECIPE_MUTATION = {**R, "recipe": OBJECT, "expected_revision": POSITIVE_INT}
EVIDENCE_FIELDS = {"source_url": NULLABLE_STRING, "source_identifier": NULLABLE_STRING,
                   "title": S, "authors": A, "publication_date": NULLABLE_STRING,
                   "content_hash": NULLABLE_STRING, "excerpt": NULLABLE_STRING, "citation_metadata": OBJECT}
CLAIM_FIELDS = {"evidence_ids": A, "proposition": S, "supports": A, "contradicts": A,
                "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                "uncertainty": {"type": "string"}, "review_status": S}
HYPOTHESIS_FIELDS = {"claim_ids": A, "evidence_ids": A, "proposed_change": S, "expected_outcome": S,
                     "acceptance_metric": OBJECT, "scope": {"type": "string"}, "status": S}
PROJECT_FIELDS = {"slug": S, "model_name": S, "description": {"type": "string"},
                  "git_remote": NULLABLE_STRING, "head_commit": NULLABLE_STRING,
                  "active_recipe_id": NULLABLE_STRING, "active_model_version_ref": NULLABLE_STRING,
                  "research_watch_config": {"type": ["object", "null"]}, "status": S}

OPERATIONS = {s.name: s for s in [
    _spec("project.create", "modify", "Create one model project with its one stable current recipe; optionally supply the initial recipe document.", {**PROJECT_FIELDS, "recipe": OBJECT},
          ("slug", "model_name"), True),
    _spec("project.list", "read", "List authorized model projects."),
    _spec("project.get", "read", "Read a project.", P, ("project_id",)),
    _spec("project.open", "read", "Open a project and its lifecycle resources.", P, ("project_id",)),
    _spec("project.update", "modify", "Update project with optimistic concurrency.",
          {**P, "expected_revision": POSITIVE_INT, "changes": OBJECT}, ("project_id", "expected_revision", "changes"), True),
    _spec("project.history", "read", "Read append-only project audit history.",
          {**P, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ("project_id",)),
    _spec("recipe.create", "modify", "Author the project recipe at the expected revision; never create a second recipe identity.",
          {**P, "recipe": OBJECT, "name": S, "expected_revision": POSITIVE_INT}, ("project_id", "expected_revision"), True),
    _spec("recipe.get", "read", "Read a recipe at its current or historical revision.",
          {**R, "revision": POSITIVE_INT}, ("project_id", "recipe_id")),
    _spec("recipe.list", "read", "Compatibility list containing exactly the project current recipe.", P, ("project_id",)),
    _spec("recipe.history", "read", "Read immutable recipe revisions and retained legacy provenance, not selectable current recipes.", P, ("project_id",)),
    _spec("recipe.validate", "read", "Validate a recipe without executing any code.",
          {**R, "recipe": OBJECT}, ("project_id",)),
    _spec("recipe.update", "modify", "Atomically replace the same recipe using its expected revision.",
          RECIPE_MUTATION, ("project_id", "recipe_id", "recipe", "expected_revision"), True),
    _spec("recipe.diff", "read", "Compare revisions or a draft against the current recipe.",
          {**R, "recipe": OBJECT, "from_revision": POSITIVE_INT, "to_revision": POSITIVE_INT}, ("project_id", "recipe_id")),
    _spec("recipe.clone", "modify", "Compatibility historical restore: copy a prior revision into the same stable recipe identity.",
          {**R, "name": S, "expected_revision": POSITIVE_INT, "source_revision": POSITIVE_INT}, ("project_id", "recipe_id", "expected_revision"), True),
    _spec("recipe.export", "read", "Export a portable bundle without execution; format=json returns lossless bundle_json for browser round trips.",
          {**R, "format": {"enum": ["object", "json"]}}, ("project_id", "recipe_id")),
    _spec("recipe.import", "modify", "Open a verified bundle as the next untrusted revision of this model's single recipe.",
          {**P, "bundle": OBJECT, "bundle_json": {"type": "string", "minLength": 1, "maxLength": MAX_BUNDLE_JSON_BYTES,
              "description": "Exact exported JSON text; do not parse and reserialize in JavaScript. Maximum 8 MiB UTF-8."},
           "name": S, "expected_revision": POSITIVE_INT},
          ("project_id", "expected_revision"), True),
    _spec("recipe.resolve_dependencies", "read", "Inspect immutable dependency references and readiness.",
          R, ("project_id", "recipe_id")),
    _spec("evidence.add", "modify", "Store a citation; a citation alone does not prove a claim.",
          {**P, **EVIDENCE_FIELDS}, ("project_id", "title"), True),
    _spec("evidence.get", "read", "Read a cited evidence record.", {**P, "evidence_id": S},
          ("project_id", "evidence_id")),
    _spec("evidence.search", "read", "Search saved evidence or retrieve public OpenAlex metadata without persisting it.",
          {**P, "query": {"type": "string"}, "provider": {"enum": ["local", "openalex"]},
           "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, ("project_id",)),
    _spec("claim.create", "modify", "Create a proposition with evidence and contradiction links.",
          {**P, **CLAIM_FIELDS}, ("project_id", "evidence_ids", "proposition"), True),
    _spec("claim.get", "read", "Read a research claim.", {**P, "claim_id": S}, ("project_id", "claim_id")),
    _spec("claim.list", "read", "List project claims.", P, ("project_id",)),
    _spec("claim.update", "modify", "Revise a claim and preserve review history.",
          {**P, "claim_id": S, "expected_revision": POSITIVE_INT, "changes": OBJECT},
          ("project_id", "claim_id", "expected_revision", "changes"), True),
    _spec("hypothesis.create", "modify", "Create an evidence-linked measurable hypothesis.",
          {**P, **HYPOTHESIS_FIELDS},
          ("project_id", "proposed_change", "expected_outcome", "acceptance_metric"), True),
    _spec("hypothesis.get", "read", "Read a hypothesis.", {**P, "hypothesis_id": S},
          ("project_id", "hypothesis_id")),
    _spec("hypothesis.list", "read", "List hypotheses including negative and abandoned outcomes.", P, ("project_id",)),
    _spec("hypothesis.update", "modify", "Revise a hypothesis without losing previous outcomes.",
          {**P, "hypothesis_id": S, "expected_revision": POSITIVE_INT, "changes": OBJECT},
          ("project_id", "hypothesis_id", "expected_revision", "changes"), True),
    _spec("decision.record", "modify", "Record a decision tied to real runs and an immutable recipe revision.",
          {**R, "recipe_revision": POSITIVE_INT, "hypothesis_id": S, "run_ids": {"type": "array", "items": S, "minItems": 1},
           "selection_rule": OBJECT, "outcome": {"enum": ["accepted", "rejected", "inconclusive"]}, "rationale": S},
          ("project_id", "recipe_id", "recipe_revision", "hypothesis_id", "run_ids", "selection_rule", "outcome", "rationale"), True),
    _spec("decision.list", "read", "List positive, negative, and inconclusive research decisions.", P, ("project_id",)),
    _spec("policy.get", "read", "Read project permissions and fail-closed remote-execution policy.", P, ("project_id",)),
    _spec("policy.update", "modify", "Update bounded local policy; remote spending remains disabled.",
          {**P, "expected_revision": POSITIVE_INT, "changes": OBJECT}, ("project_id", "expected_revision", "changes"), True),
    _spec("execution.targets", "read", "List actual supported targets and capabilities.", P, ("project_id",)),
    _spec("cost.estimate", "read", "Inspect local cost limits; remote estimates unavailable without a provider.",
          {**R, "target": S}, ("project_id",)),
    _spec("run.plan", "read", "Validate an execution plan; never launch from this operation.",
          {**R, "target": S, "parameters": NO_OVERRIDES}, ("project_id", "recipe_id")),
    _spec("run.submit", "execute", "Submit an explicitly permitted local fixture to the real backend.",
          {**R, "expected_revision": POSITIVE_INT, "target": S, "parameters": NO_OVERRIDES, "trusted": B},
          ("project_id", "recipe_id", "expected_revision"), True),
    _spec("run.status", "read", "Reconcile status with the authoritative backend.", RUN, ("project_id",)),
    _spec("run.list", "read", "List and reconcile project operation correlations with the backend.", P, ("project_id",)),
    _spec("operation.get", "read", "Read a stored application-operation correlation.",
          {**P, "operation_id": S}, ("project_id", "operation_id")),
    _spec("run.logs", "read", "Read actual backend logs.", {**RUN, "cursor": NULLABLE_STRING}, ("project_id",)),
    _spec("run.cancel", "execute", "Request real cancellation; requested is not confirmed termination.",
          RUN, ("project_id",), True),
    _spec("run.resume", "execute", "Resume an actual checkpoint and advance this recipe revision; requires execute and modify. Exact state differs from warm start.",
          {**RUN, "checkpoint_ref": S, "checkpoint_id": S, "mode": {"enum": ["exact_state", "warm_start"]},
           "resume_mode": {"enum": ["exact_state", "warm_start"]}, "parameters": NO_OVERRIDES, "steps": POSITIVE_INT,
           "checkpoint_kind": {"enum": ["last", "best"]}, "trusted": B, "expected_recipe_revision": POSITIVE_INT},
          ("project_id", "expected_recipe_revision"), True, additional_scopes=("modify",)),
    _spec("run.artifacts", "read", "Read authoritative artifact identifiers and hashes.", RUN, ("project_id",)),
    _spec("run.compare", "read", "Compare metrics from actual backend runs by a declared criterion.",
          {**P, "run_ids": {"type": "array", "items": S, "minItems": 2}, "metric": S,
           "direction": {"enum": ["minimize", "maximize"]}}, ("project_id", "run_ids", "metric", "direction")),
    _spec("model.version.list", "read", "List authoritative ZenML model versions.", P, ("project_id",)),
    _spec("model.version.get", "read", "Read an authoritative model version.", VERSION, ("project_id",)),
    _spec("model.version.promote", "modify", "Promote a version using a recorded, measurable decision.",
          {**VERSION, "decision_id": S, "expected_revision": POSITIVE_INT, "stage": S},
          ("project_id", "decision_id", "expected_revision"), True),
    _spec("model.version.lineage", "read", "Read actual model-version parents and artifacts.", VERSION, ("project_id",)),
    _spec("model.checkpoint.get", "read", "Read a checkpoint and its verified resume capabilities.",
          {**RUN, "checkpoint_ref": S, "checkpoint_id": S, "checkpoint_kind": {"enum": ["last", "best"]}}, ("project_id",)),
]}


def operation_catalog() -> list[dict]:
    return [OPERATIONS[name].as_dict() for name in sorted(OPERATIONS)]
