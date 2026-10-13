"""Public domain types and the shared transport-independent operation contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .tracking import TrackingConfig

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


class SourceBinding(DomainModel):
    alias: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
    repository_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    bound_at: str
    bound_by: str
    trust_acknowledged: Literal[True]


class ProjectIntent(DomainModel):
    """Owner-supplied quickstart choices, never inferred from research documents.

    These are planning metadata, not execution, download, sign-in or spending
    authorization. The project revision/history is the sole persistence contract.
    """
    schema_version: Literal["1"] = "1"
    goal: str = Field(default="", max_length=8000)
    starting_point: Literal["blank", "research", "base_model", "scratch"] = "blank"
    assistant: Literal["none", "codex", "claude", "triggerfish"] = "none"


class ModelProject(DomainModel):
    schema_version: Literal["0"] = "0"
    project_id: str
    id: str
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    model_name: str = Field(min_length=1, max_length=200)
    description: str = ""
    intent: ProjectIntent | None = None
    created_at: str
    updated_at: str
    source_binding: SourceBinding | None = None
    git_remote: str | None = None
    head_commit: str | None = None
    recipe_id: str
    active_recipe_id: str
    recipe_migration: dict = Field(default_factory=dict)
    active_model_version_ref: str | None = None
    research_watch_config: dict | None = None
    scientific_policy_digest: str | None = None
    rule_policy_activation: dict | None = None
    tracking: TrackingConfig = Field(default_factory=TrackingConfig)
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
    run_provenance: list[dict] = Field(default_factory=list)
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
        if self.name == "project.create":
            schema["dependentRequired"] = {"source_alias": ["source_repository_id", "trusted"],
                "source_repository_id": ["source_alias", "trusted"], "trusted": ["source_alias", "source_repository_id"]}
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


from .execution import EXECUTION_SCHEMA, EVALUATIONS_SCHEMA  # noqa: E402
from .scientific_operations import BINDINGS, GATE_SELECTOR, SHA as SCIENTIFIC_SHA  # noqa: E402

SOURCE_ALIAS = {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$"}
SOURCE_REPOSITORY_ID = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}
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
TRACKING_FIELDS = {"tracking": TrackingConfig.model_json_schema(), "tracking_online_consent": B}
PROJECT_FIELDS = {"slug": S, "model_name": S, "description": {"type": "string"},
                  "intent": {"anyOf": [ProjectIntent.model_json_schema(), {"type": "null"}]},
                  "git_remote": NULLABLE_STRING, "head_commit": NULLABLE_STRING,
                  "active_recipe_id": NULLABLE_STRING, "active_model_version_ref": NULLABLE_STRING,
                  "research_watch_config": {"type": ["object", "null"]}, "status": S,
                  "tracking": TrackingConfig.model_json_schema()}

OPERATIONS = {s.name: s for s in [
    _spec("project.create", "modify", "Create one model project with its one stable current recipe; optionally supply the initial recipe document.", {**PROJECT_FIELDS, "recipe": OBJECT, "source_alias": SOURCE_ALIAS, "source_repository_id": SOURCE_REPOSITORY_ID, "trusted": B},
          ("slug", "model_name"), True),
    _spec("project.source.list", "read", "List operator-approved source workspace aliases without importing code.", P),
    _spec("project.source.status", "read", "Inspect this project's bound checkout, trust and pinned source identity.", P, ("project_id",)),
    _spec("project.source.bind", "modify", "Bind an approved source alias with explicit trust and optimistic concurrency.",
          {**P, "source_alias": SOURCE_ALIAS, "source_repository_id": SOURCE_REPOSITORY_ID, "trusted": B, "expected_revision": POSITIVE_INT},
          ("project_id", "source_alias", "source_repository_id", "trusted", "expected_revision"), True),
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
    _spec("dataset.register", "modify", "Register an immutable local dataset manifest using operator-approved project root aliases.",
          {**P, "manifest": OBJECT}, ("project_id", "manifest"), True),
    _spec("dataset.version", "modify", "Create the next immutable dataset version with optimistic concurrency.",
          {**P, "dataset_id": S, "expected_revision": POSITIVE_INT, "manifest": OBJECT},
          ("project_id", "dataset_id", "expected_revision", "manifest"), True),
    _spec("dataset.bind", "modify", "Bind a verified dataset revision and split to this project's one recipe; optionally replace an explicit target input connection. Creates a recipe revision without execution.",
          {**R, "expected_revision": POSITIVE_INT, "dataset_id": S, "dataset_revision": POSITIVE_INT,
           "split_id": S, "purpose": {"enum": ["training", "evaluation", "calibration"]},
           "node_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", "maxLength": 128},
           "target_node": S, "target_port": S},
          ("project_id", "recipe_id", "expected_revision", "dataset_id", "dataset_revision", "split_id", "purpose", "node_id"), True),
    _spec("dataset.get", "read", "Read a project dataset at its current or historical immutable revision.",
          {**P, "dataset_id": S, "revision": POSITIVE_INT}, ("project_id", "dataset_id")),
    _spec("dataset.list", "read", "List datasets registered to this model project.", P, ("project_id",)),
    _spec("dataset.preview", "read", "Preview a bounded page from a verified immutable dataset split.",
          {**P, "dataset_id": S, "revision": POSITIVE_INT, "split_id": S,
           "purpose": {"enum": ["training", "evaluation", "calibration"]},
           "limit": {"type": "integer", "minimum": 1, "maximum": 100},
           "offset": {"type": "integer", "minimum": 0}},
          ("project_id", "dataset_id", "revision", "split_id", "purpose")),
    _spec("dataset.validate", "read", "Verify a dataset manifest or stored revision against its declared files and splits without training.",
          {**P, "manifest": OBJECT, "dataset_id": S, "revision": POSITIVE_INT}, ("project_id",)),
    _spec("component.list", "read", "Inspect inert component declarations in this project's recipe; never load user code.",
          {**P, "revision": POSITIVE_INT}, ("project_id",)),
    _spec("component.get", "read", "Read a declared component by identity and optional node identity.",
          {**P, "component_id": S, "node_id": S, "revision": POSITIVE_INT}, ("project_id", "component_id")),
    _spec("component.validate", "read", "Validate an inert component node declaration and parameters without execution.",
          {**P, "node": OBJECT}, ("project_id", "node")),
    _spec("evaluation.validate", "read", "Validate sealed evaluation evidence without executing a model.",
          {**P, "record": OBJECT}, ("project_id", "record")),
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
          {**R, "target": S, "expected_revision": POSITIVE_INT, "execution": EXECUTION_SCHEMA, "parameters": NO_OVERRIDES, "trusted": B, **TRACKING_FIELDS}, ("project_id", "recipe_id")),
    _spec("run.submit", "execute", "Submit an explicitly permitted source-bound local pipeline to the real backend.",
          {**R, "expected_revision": POSITIVE_INT, "execution": EXECUTION_SCHEMA, "target": S, "parameters": NO_OVERRIDES, "trusted": B, **TRACKING_FIELDS},
          ("project_id", "recipe_id", "expected_revision"), True),
    _spec("run.status", "read", "Reconcile status with the authoritative backend.", RUN, ("project_id",)),
    _spec("run.list", "read", "List and reconcile project operation correlations with the backend.", P, ("project_id",)),
    _spec("operation.get", "read", "Read a stored application-operation correlation.",
          {**P, "operation_id": S}, ("project_id", "operation_id")),
    _spec("run.logs", "read", "Read actual backend logs.", {**RUN, "cursor": NULLABLE_STRING}, ("project_id",)),
    _spec("run.evaluations.list", "read", "Read checkpoint evaluation results while training is running, including pending and failed evaluations.",
          {**RUN, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ("project_id",)),
    _spec("run.events.list", "read", "Read durable run events after a replayable cursor for live progress and notifications.",
          {**RUN, "after": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ("project_id",)),
    _spec("run.cancel", "execute", "Request real cancellation; requested is not confirmed termination.",
          RUN, ("project_id",), True),
    _spec("run.resume", "execute", "Resume an actual checkpoint and advance this recipe revision; requires execute and modify. Exact state differs from warm start.",
          {**RUN, "checkpoint_ref": S, "checkpoint_id": S, "mode": {"enum": ["exact_state", "warm_start"]},
           "resume_mode": {"enum": ["exact_state", "warm_start"]}, "parameters": NO_OVERRIDES, "steps": POSITIVE_INT,
           "checkpoint_kind": {"enum": ["last", "best"]}, "trusted": B, "expected_recipe_revision": POSITIVE_INT, **TRACKING_FIELDS},
          ("project_id", "expected_recipe_revision"), True, additional_scopes=("modify",)),
    _spec("run.artifacts", "read", "Read authoritative artifact identifiers and optional explicit binding content descriptors.", {**RUN, "include_binding_descriptors": B}, ("project_id",)),
    _spec("run.compare", "read", "Compare metrics from actual backend runs by a declared criterion.",
          {**P, "run_ids": {"type": "array", "items": S, "minItems": 2}, "metric": S,
           "direction": {"enum": ["minimize", "maximize"]}, "comparison_contract": OBJECT, "evaluations": EVALUATIONS_SCHEMA, "scientific_policy_digest": SCIENTIFIC_SHA, "scientific_evaluations": BINDINGS, "scientific_gate": GATE_SELECTOR}, ("project_id", "run_ids", "metric", "direction")),
    _spec("model.export.list", "read", "List verified existing native exports for one run or model version; never execute code.",
          {**P, "backend_run_id": S, "model_version_id": S}, ("project_id",)),
    _spec("model.export.inspect", "read", "Inspect a declared native export, checkpoint identity and provenance without running it.",
          {**P, "backend_run_id": S, "model_version_id": S, "artifact_id": S}, ("project_id", "artifact_id")),
    _spec("model.export.package", "read", "Snapshot only hash-verified declared export assets as a local ZIP; no training or publication.",
          {**P, "backend_run_id": S, "model_version_id": S, "artifact_id": S, "expected_export_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
          ("project_id", "artifact_id", "expected_export_digest")),
    _spec("model.export.read", "read", "Retrieve one authenticated, hash-verified package chunk, at most 4 MiB before base64 encoding.",
          {**P, "backend_run_id": S, "model_version_id": S, "artifact_id": S,
           "package_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
           "chunk_index": {"type": "integer", "minimum": 0, "maximum": 4096}},
          ("project_id", "backend_run_id", "model_version_id", "artifact_id", "package_id", "chunk_index")),
    _spec("model.version.list", "read", "List authoritative ZenML model versions.", P, ("project_id",)),
    _spec("model.version.get", "read", "Read an authoritative model version.", VERSION, ("project_id",)),
    _spec("model.version.promote", "modify", "Promote a version using a recorded, measurable decision.",
          {**VERSION, "decision_id": S, "expected_revision": POSITIVE_INT, "stage": S},
          ("project_id", "decision_id", "expected_revision"), True),
    _spec("model.version.lineage", "read", "Read actual model-version parents and artifacts.", VERSION, ("project_id",)),
    _spec("model.checkpoint.get", "read", "Read a checkpoint and its verified resume capabilities.",
          {**RUN, "checkpoint_ref": S, "checkpoint_id": S, "checkpoint_kind": {"enum": ["last", "best", "inference"]}}, ("project_id",)),
]}


from .research_contract import operation_specs as research_operation_specs  # noqa: E402
OPERATIONS.update({spec.name: spec for spec in research_operation_specs(_spec)})
from .research_context_contract import operation_specs as research_context_operation_specs  # noqa: E402
OPERATIONS.update({spec.name: spec for spec in research_context_operation_specs(_spec)})


from .recipe_registry_contract import operation_specs as registry_operation_specs  # noqa: E402
OPERATIONS.update({spec.name: spec for spec in registry_operation_specs(_spec)})


from .remote.operation_contract import operation_specs as remote_cost_operation_specs  # noqa: E402
OPERATIONS.update({spec.name: spec for spec in remote_cost_operation_specs(_spec)})


from .source_pinning_contract import operation_specs as source_pin_specs  # noqa: E402
OPERATIONS.update({spec.name: spec for spec in source_pin_specs(_spec)})


from .scientific_operations import operation_specs as scientific_specs  # noqa: E402
OPERATIONS.update({spec.name: spec for spec in scientific_specs(_spec)})
from .rule_operations import operation_specs as rule_specs  # noqa: E402
OPERATIONS.update({spec.name: spec for spec in rule_specs(_spec)})
from .rule_operations import GATE_SELECTOR as RULE_GATE_SELECTOR  # noqa: E402
for _rule_action in ('model.export.package', 'model.version.promote'):
    OPERATIONS[_rule_action].properties['rule_gate'] = RULE_GATE_SELECTOR


from .smoke import operation_specs as smoke_specs  # noqa: E402
OPERATIONS.update({spec.name: spec for spec in smoke_specs(_spec)})


from .hub_contract import operation_specs as hub_operation_specs  # noqa: E402
OPERATIONS.update({spec.name: spec for spec in hub_operation_specs(_spec)})


def operation_catalog() -> list[dict]:
    return [OPERATIONS[name].as_dict() for name in sorted(OPERATIONS)]
