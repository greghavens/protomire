"""Canonical recipe v0, typed DAG validation, and inert portable bundles.

Importing a bundle only parses JSON, verifies bytes, and optionally writes ordinary
files into a new directory. It never imports a module, invokes pip, opens a URL,
or executes a command. A valid digest provides integrity, not publisher trust.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, NotRequired, TypedDict
from urllib.parse import parse_qsl, unquote, urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from .backend.components import COMPONENT_SCHEMA, validate_component
from .conditions import CONDITION_SCHEMA, ConditionError, control_dependencies, validate_conditions
from .iterations import ITERATIONS_SCHEMA, validate_iterations
from .execution import RECIPE_EXECUTION_SCHEMA, max_parallel_stages

MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_BUNDLE_FILES = 1024
MAX_JSON_DEPTH = 64
MAX_JSON_ITEMS = 100_000
NODE_KINDS = [
    "dataset.import", "dataset.version.import", "dataset.synthetic", "dataset.validate", "model.initialize",
    "model.train", "model.evaluate", "model.compare", "model.checkpoint", "model.export",
    "code.python", "component.run", "training.kl_anchor",
]
HEX_SHA = r"^[0-9a-f]{64}$"
GIT_SHA = r"^[0-9a-f]{40}([0-9a-f]{24})?$"
IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
PORT_TYPE = r"^[A-Za-z][A-Za-z0-9_.\[\], -]{0,127}$"
SECRET_KEYS = re.compile(
    r"^(?:password|passwd|secret|client_secret|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|auth[_-]?token|token|authorization|private[_-]?key|credentials|"
    r"aws_access_key_id|aws_secret_access_key|session[_-]?token)$", re.I
)
SECRET_BYTES = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|"
    rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bgh[pousr]_[A-Za-z0-9]{30,}\b|"
    rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b|"
    rb"(?im:^\s*(?:api_key|access_token|password|client_secret|aws_secret_access_key)\s*[=:]\s*[\"']?[^\s\"']{8,})"
)
SAFE_FILE_SUFFIXES = {".py", ".pyi", ".json", ".toml", ".txt", ".lock", ".md", ".yaml", ".yml", ".cfg", ".ini"}
FORBIDDEN_PARTS = {".git", ".ssh", ".aws", ".venv", "venv", "__pycache__", ".env"}


class TypedPort(TypedDict):
    name: str
    type: str
    required: NotRequired[bool]


class RecipeEdge(TypedDict):
    source_node: str
    source_port: str
    target_node: str
    target_port: str


class RecipeExecution(TypedDict):
    max_parallel_stages: NotRequired[int]


class PythonImplementation(TypedDict):
    module: str
    entrypoint: str
    commit: str
    path: NotRequired[str]
    sha256: NotRequired[str]


class RecipeNode(TypedDict):
    id: str
    kind: str
    implementation_ref: str | PythonImplementation
    inputs: list[TypedPort]
    outputs: list[TypedPort]
    parameters: dict[str, Any]
    environment_ref: str
    resource_request: dict[str, Any]
    retry: dict[str, Any]
    timeout: float
    cache_policy: str
    provenance: dict[str, Any]
    parameter_schema: NotRequired[dict[str, Any]]
    condition: NotRequired[dict[str, Any]]


class RecipeValidationError(ValueError):
    """A stable code and machine-readable details for API/MCP clients."""

    def __init__(self, code: str, message: str | None = None, details: Any = None):
        self.code = code
        self.message = message or code
        self.details = details if details is not None else {}
        super().__init__(self.message)


def _object(properties: dict, required: list[str], **extra: Any) -> dict:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False, **extra}


FILE_REF_SCHEMA = _object({"path": {"type": "string", "minLength": 1},
                           "sha256": {"type": "string", "pattern": HEX_SHA}}, ["path", "sha256"])
PORT_SCHEMA = _object({"name": {"type": "string", "pattern": IDENTIFIER},
                       "type": {"type": "string", "pattern": PORT_TYPE},
                       "required": {"type": "boolean"}}, ["name", "type"])
REFERENCE_SCHEMA = _object({
    "id": {"type": "string", "pattern": IDENTIFIER},
    "kind": {"enum": ["dataset", "model", "checkpoint", "source", "artifact", "environment"]},
    "url": {"type": "string", "minLength": 1},
    "revision": {"type": "string", "minLength": 1},
    "sha256": {"type": "string", "pattern": HEX_SHA},
    "license": {"type": "string", "minLength": 1},
    "private": {"type": "boolean"},
    "required": {"type": "boolean"},
    "redistribution_allowed": {"type": "boolean"},
    "metadata": {"type": "object"},
}, ["id", "kind", "url", "license"], anyOf=[{"required": ["revision"]}, {"required": ["sha256"]}])
IMPLEMENTATION_SCHEMA = _object({
    "module": {"type": "string", "pattern": r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$"},
    "entrypoint": {"type": "string", "pattern": r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$"},
    "commit": {"type": "string", "pattern": GIT_SHA},
    "path": {"type": "string", "minLength": 1},
    "sha256": {"type": "string", "pattern": HEX_SHA},
}, ["module", "entrypoint", "commit"], dependentRequired={"path": ["sha256"], "sha256": ["path"]})
NODE_SCHEMA = _object({
    "id": {"type": "string", "pattern": IDENTIFIER},
    "kind": {"enum": NODE_KINDS},
    "implementation_ref": {"oneOf": [{"type": "string", "pattern": r"^builtin:[a-z][a-z_.]+$"}, IMPLEMENTATION_SCHEMA]},
    "inputs": {"type": "array", "items": PORT_SCHEMA, "maxItems": 256},
    "outputs": {"type": "array", "items": PORT_SCHEMA, "maxItems": 256},
    "parameters": {"type": "object"},
    "parameter_schema": {"type": "object"},
    "component": COMPONENT_SCHEMA,
    "condition": CONDITION_SCHEMA,
    "environment_ref": {"type": "string", "minLength": 1},
    "resource_request": _object({"cpu": {"type": "number", "exclusiveMinimum": 0},
                                  "memory_mb": {"type": "integer", "minimum": 1},
                                  "gpu": {"type": "integer", "minimum": 0}}, []),
    "retry": _object({"max_attempts": {"type": "integer", "minimum": 1, "maximum": 100},
                      "backoff_seconds": {"type": "number", "minimum": 0}}, ["max_attempts", "backoff_seconds"]),
    "timeout": {"type": "number", "exclusiveMinimum": 0},
    "cache_policy": {"enum": ["enabled", "disabled"]},
    "provenance": {"type": "object"},
}, ["id", "kind", "implementation_ref", "inputs", "outputs", "parameters", "environment_ref",
    "resource_request", "retry", "timeout", "cache_policy", "provenance"])
RECIPE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://modelbuilder.local/schemas/recipe.v0.schema.json",
    "title": "ModelBuilder recipe v0",
    "description": "A typed acyclic graph. Import is inert; execution requires separate authorization and isolation.",
    **_object({
        "schema_version": {"const": "0"},
        "recipe_id": {"type": "string", "pattern": IDENTIFIER},
        "project_id": {"type": "string", "pattern": IDENTIFIER},
        "revision": {"type": "integer", "minimum": 1},
        "name": {"type": "string", "minLength": 1, "maxLength": 256},
        "description": {"type": "string", "maxLength": 16384},
        "source": _object({"repository": {"type": "string", "minLength": 1},
                            "commit": {"type": "string", "pattern": GIT_SHA},
                            "files": {"type": "array", "items": FILE_REF_SCHEMA, "maxItems": MAX_BUNDLE_FILES}},
                           ["repository", "commit"]),
        "nodes": {"type": "array", "items": NODE_SCHEMA, "maxItems": 4096},
        "bounded_iterations": ITERATIONS_SCHEMA,
        "execution": RECIPE_EXECUTION_SCHEMA,
        "edges": {"type": "array", "items": _object({key: {"type": "string", "pattern": IDENTIFIER}
                  for key in ["source_node", "source_port", "target_node", "target_port"]},
                 ["source_node", "source_port", "target_node", "target_port"]), "maxItems": 16384},
        "resources": {"type": "array", "items": REFERENCE_SCHEMA},
        "environment": _object({"python": {"type": "string", "pattern": r"^3\.\d{1,2}(\.\d{1,2})?$"},
                                "requirements": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                                "lock_file": FILE_REF_SCHEMA,
                                "mlx_profile": FILE_REF_SCHEMA,
                                "device": {"type": "string", "pattern": r"^(cpu|mps|mlx-cpu|mlx-metal|cuda:(0|[1-9][0-9]{0,2}))$"},
                                "oci_digest": {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}},
                               ["python", "requirements"]),
        "evaluation": {"type": "object"},
        "outputs": {"type": "array", "items": PORT_SCHEMA},
        "external_dependencies": {"type": "array", "items": REFERENCE_SCHEMA},
        "capabilities": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "licenses": {"type": "array", "items": _object({"scope": {"type": "string"},
                     "license": {"type": "string", "minLength": 1}, "redistribution_allowed": {"type": "boolean"}},
                     ["scope", "license", "redistribution_allowed"])},
        "permissions": {"type": "object"},
        "evidence_links": {"type": "array", "items": {"type": "string"}},
        "extensions": {"type": "object"},
    }, ["schema_version", "recipe_id", "project_id", "revision", "name", "source", "nodes", "edges",
        "resources", "environment", "evaluation", "outputs", "external_dependencies"]),
}


def _fail(code: str, message: str, **details: Any) -> None:
    raise RecipeValidationError(code, message, details)


def _json_value(value: Any, path: str = "$", depth: int = 0, budget: list[int] | None = None) -> None:
    budget = budget if budget is not None else [MAX_JSON_ITEMS]
    budget[0] -= 1
    if depth > MAX_JSON_DEPTH or budget[0] < 0:
        _fail("json_limit", "Recipe exceeds JSON complexity limits", path=path)
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            _fail("invalid_json", "Unpaired Unicode surrogates cannot be canonical UTF-8 JSON", path=path)
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail("invalid_json", "Non-finite numbers are not canonical JSON", path=path)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail("invalid_json", "JSON object keys must be strings", path=path)
            _json_value(key, path, depth + 1, budget)
            _json_value(item, f"{path}.{key}", depth + 1, budget)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _json_value(item, f"{path}[{index}]", depth + 1, budget)
    else:
        _fail("invalid_json", "Only JSON values are accepted", path=path)


def canonical_json(value: Any) -> str:
    """Deterministic UTF-8 JSON (sorted keys, no whitespace, no NaN)."""
    _json_value(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value or ":" in value:
        _fail("unsafe_path", "Bundle file paths must be ordinary relative POSIX paths")
    decoded = unquote(value)
    path = PurePosixPath(value)
    if (path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/"))
            or decoded != value or any(part.lower() in FORBIDDEN_PARTS for part in path.parts)
            or any(part.startswith(".env") for part in path.parts)):
        _fail("unsafe_path", "Absolute, traversal, encoded, or secret file paths are forbidden", path=value)
    if not re.fullmatch(r"[A-Za-z0-9_.\-/]+", value):
        _fail("unsafe_path", "Unsupported file path characters", path=value)
    windows_devices = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    if any(part.endswith(".") or part.split(".")[0].lower() in windows_devices for part in path.parts):
        _fail("unsafe_path", "Reserved cross-platform filenames are forbidden", path=value)
    return value


def _safe_url(value: str, path: str) -> None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        _fail("unsafe_url", "Resource URL is malformed", path=path)
    if parsed.scheme not in {"https", "git+https", "hf", "bundle", "urn"}:
        _fail("unsafe_url", "Only HTTPS, Hugging Face, bundle, and content-addressed URN references are supported", path=path)
    sensitive_query = {"x-amz-signature", "x-amz-credential", "x-amz-security-token", "sig", "signature"}
    if parsed.username or parsed.password or any(
        SECRET_KEYS.match(key) or key.lower() in sensitive_query for key, _ in parse_qsl(parsed.query)
    ):
        _fail("secret_detected", "Credentials must not occur in recipe URLs", path=path)
    if parsed.scheme in {"https", "git+https", "hf"} and not parsed.hostname:
        _fail("unsafe_url", "Resource URL has no host", path=path)
    if parsed.scheme == "bundle":
        _safe_path((parsed.netloc + parsed.path).lstrip("/"))
    if parsed.scheme == "urn" and not re.fullmatch(r"urn:sha256:[0-9a-f]{64}", value):
        _fail("unsafe_url", "Only SHA256 content-addressed URNs are accepted", path=path)


def _scan_manifest(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_KEYS.match(key) and item not in (None, "", False):
                _fail("secret_detected", "Secrets cannot be stored in recipes", path=f"{path}.{key}")
            _scan_manifest(item, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _scan_manifest(item, f"{path}[{i}]")
    elif isinstance(value, str):
        if value.startswith(("/", "~/", "~\\", "\\\\")) or re.match(r"^[A-Za-z]:[\\/]", value):
            _fail("absolute_path", "Recipes cannot contain absolute host paths", path=path)
        if SECRET_BYTES.search(value.encode("utf-8")):
            _fail("secret_detected", "Potential credential material found in recipe", path=path)
        if "://" in value and not any(char.isspace() for char in value):
            _safe_url(value, path)


def validate_pinned_requirements(requirements: list[str]) -> list[str]:
    """Accept only exact registry pins; never execute or resolve requirement text."""
    result = []
    seen = set()
    for item in requirements:
        if not isinstance(item, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9][A-Za-z0-9_.+!-]*", item
        ):
            _fail("unpinned_dependency", "Requirements must be simple exact package==version pins", requirement=item)
        name = re.split(r"[=\[]", item)[0].lower().replace("_", "-").replace(".", "-")
        if name in seen:
            _fail("duplicate_dependency", "A package is pinned more than once", package=name)
        seen.add(name)
        result.append(item)
    return sorted(result, key=str.casefold)


def _local_schema(schema: dict, path: str) -> None:
    """Keep untrusted schemas inert and bounded.

    Regex evaluation and schema references are deliberately unsupported in v0:
    Python's regex engine has no execution deadline, and recursive schema
    references can exhaust the validator. Primitive types, enums, numeric
    bounds, object/array structure, and composition remain available.
    """
    def inspect(item: Any) -> None:
        if isinstance(item, dict):
            for key, value in item.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef", "pattern", "patternProperties"}:
                    _fail("unsafe_schema", "Schema references and regular expressions are unsupported in v0 parameter schemas", path=path)
                inspect(value)
        elif isinstance(item, list):
            for value in item:
                inspect(value)
    inspect(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        _fail("invalid_parameter_schema", "Invalid node parameter schema", path=path, reason=exc.message)


def _validate_kl(node: dict, refs: set[str]) -> None:
    kl = node["parameters"] if node["kind"] == "training.kl_anchor" else node["parameters"].get("kl")
    if kl is None:
        return
    required = {"enabled", "coefficient", "direction", "reference_model_ref", "applies_to"}
    if not isinstance(kl, dict) or not required.issubset(kl):
        _fail("invalid_kl", "KL declares enabled, coefficient, direction, reference_model_ref, and applies_to", node_id=node["id"])
    coefficient = kl["coefficient"]
    if (type(kl["enabled"]) is not bool or type(coefficient) not in (int, float) or coefficient < 0
            or not isinstance(kl["direction"], str) or kl["direction"] not in {"reference_to_model", "model_to_reference"}
            or not isinstance(kl["applies_to"], str) or not kl["applies_to"].strip()
            or not isinstance(kl["reference_model_ref"], str) or not kl["reference_model_ref"]):
        _fail("invalid_kl", "KL direction must name its two distributions and coefficient must be nonnegative", node_id=node["id"])
    if kl["enabled"] and (coefficient <= 0 or kl.get("frozen") is not True or kl["reference_model_ref"] not in refs):
        _fail("invalid_kl", "Enabled KL requires positive coefficient and an explicit frozen declared reference", node_id=node["id"])
    if not kl["enabled"] and coefficient != 0:
        _fail("invalid_kl", "Disabled KL must have coefficient zero, so the manifest describes executed loss", node_id=node["id"])


def normalize_recipe(recipe: dict) -> dict:
    """Validate and normalize a recipe without mutating the caller's object."""
    _json_value(recipe)
    if not isinstance(recipe, dict):
        _fail("invalid_recipe", "Recipe must be a JSON object")
    if recipe.get("schema_version") != "0":
        _fail("unsupported_schema_version", "Only recipe schema_version '0' is supported", received=recipe.get("schema_version"))
    result = copy.deepcopy(recipe)
    result.pop("digest", None)  # Derived service metadata is never included in its own digest.
    if isinstance(result.get("nodes"), list):
        for node in result["nodes"]:
            if isinstance(node, dict) and {"loop", "repeat", "iterations"}.intersection(node):
                _fail("unsupported_control_flow", "Loop/repeat stages are not supported. Use explicit stages or separate immutable recipe revisions; no implicit iteration is executed.", node_id=node.get("id"))
    errors = sorted(Draft202012Validator(RECIPE_SCHEMA).iter_errors(result), key=lambda e: str(list(e.path)))
    if errors:
        _fail("invalid_recipe", "Recipe does not match schema v0", errors=[
            {"path": "/" + "/".join(map(str, e.path)), "message": e.message} for e in errors[:32]
        ])
    try:
        max_parallel_stages(result)
    except ValueError as error:
        _fail("invalid_recipe", str(error), field="execution.max_parallel_stages")
    try:
        validate_conditions(result)
        validate_iterations(result)
    except ConditionError as error:
        _fail(error.code, error.message, **error.details)
    _scan_manifest(result)
    _safe_url(result["source"]["repository"], "source.repository")
    result["environment"]["requirements"] = validate_pinned_requirements(result["environment"]["requirements"])
    resources = result["resources"] + result["external_dependencies"]
    resource_ids = [ref["id"] for ref in resources]
    if len(set(resource_ids)) != len(resource_ids):
        _fail("duplicate_reference", "Resource and external dependency IDs must be unique")
    for ref in resources:
        _safe_url(ref["url"], f"resource.{ref['id']}.url")
        if "sha256" not in ref and not re.fullmatch(GIT_SHA, ref.get("revision", "")):
            _fail("unpinned_reference", "External references require immutable revision or SHA256", reference=ref["id"])
    file_refs = _file_references(result)
    if len(file_refs) > MAX_BUNDLE_FILES:
        _fail("bundle_limit", "Too many declared source files")
    nodes = {node["id"]: node for node in result["nodes"]}
    if len(nodes) != len(result["nodes"]):
        _fail("duplicate_node", "Node IDs must be unique")
    if set(nodes) & set(resource_ids):
        _fail("ambiguous_reference", "Node and resource IDs must not collide")
    valid_kl_refs = {ref["id"] for ref in resources if ref["kind"] in {"model", "checkpoint"}} | {
        node["id"] for node in nodes.values() if node["kind"] == "model.initialize"
    }
    environment_ids = {ref["id"] for ref in resources if ref["kind"] == "environment"}
    ports: dict[tuple[str, str], dict[str, str]] = {}
    for node in nodes.values():
        for direction in ("inputs", "outputs"):
            declarations = {port["name"]: port["type"] for port in node[direction]}
            if len(declarations) != len(node[direction]):
                _fail("duplicate_port", "Port names must be unique within each direction", node_id=node["id"])
            ports[node["id"], direction] = declarations
            # Native Python multi-output functions may return positional tuples.
            # Preserve declared port order; sorting it silently remaps outputs.
        if node["environment_ref"] != "default" and node["environment_ref"] not in environment_ids:
            _fail("unresolved_environment", "Node environment reference is not declared", node_id=node["id"])
        if node["kind"] == "component.run":
            _local_schema(node.get("component", {}).get("parameter_schema", {}), f"node.{node['id']}.component.parameter_schema")
            try:
                validate_component(node)
            except (ValueError, ValidationError, SchemaError) as error:
                _fail("invalid_component", "Component descriptor or parameters are invalid", node_id=node["id"], reason=str(error))
        elif "component" in node:
            _fail("invalid_component", "Component descriptor requires component.run kind", node_id=node["id"])
        implementation = node["implementation_ref"]
        if node["kind"] in {"code.python", "component.run"} and not isinstance(implementation, dict):
            _fail("invalid_python_reference", "Python nodes need an immutable module and entrypoint reference", node_id=node["id"])
        if isinstance(implementation, str) and implementation != f"builtin:{node['kind']}":
            _fail("implementation_mismatch", "Built-in implementation must match node kind", node_id=node["id"])
        if isinstance(implementation, dict) and implementation["commit"] != result["source"]["commit"]:
            _fail("source_revision_mismatch", "Python node and recipe source revisions must agree", node_id=node["id"])
        if "parameter_schema" in node:
            _local_schema(node["parameter_schema"], f"node.{node['id']}.parameter_schema")
            param_errors = list(Draft202012Validator(node["parameter_schema"]).iter_errors(node["parameters"]))
            if param_errors:
                _fail("invalid_parameters", "Node parameters do not match their schema", node_id=node["id"],
                      errors=[e.message for e in param_errors[:16]])
        _validate_kl(node, valid_kl_refs)
    adjacency = {node_id: [] for node_id in nodes}
    indegree = {node_id: 0 for node_id in nodes}
    incoming: set[tuple[str, str]] = set()
    edge_keys = set()
    for edge in result["edges"]:
        source, target = edge["source_node"], edge["target_node"]
        if source not in nodes or target not in nodes:
            _fail("unknown_node", "An edge refers to an unknown node", edge=edge)
        edge_key = tuple(edge[key] for key in ("source_node", "source_port", "target_node", "target_port"))
        if edge_key in edge_keys:
            _fail("duplicate_edge", "An edge is declared twice", edge=edge)
        edge_keys.add(edge_key)
        source_type = ports[source, "outputs"].get(edge["source_port"])
        target_type = ports[target, "inputs"].get(edge["target_port"])
        if source_type is None or target_type is None:
            _fail("unknown_port", "An edge refers to an undeclared port", edge=edge)
        if source_type != target_type:
            _fail("port_type_mismatch", "Connected port types must match exactly", edge=edge,
                  source_type=source_type, target_type=target_type)
        target_key = (target, edge["target_port"])
        if target_key in incoming:
            _fail("multiple_input_sources", "A typed input port may have only one producer", edge=edge)
        incoming.add(target_key)
        adjacency[source].append(target)
        indegree[target] += 1
    for source, target in control_dependencies(result):
        adjacency[source].append(target)
        indegree[target] += 1
    ready = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for target in adjacency[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited != len(nodes):
        _fail("graph_cycle", "Ordinary data/control edges must remain acyclic; use explicit bounded_iterations for feedback")
    for node in nodes.values():
        for port in node["inputs"]:
            if port.get("required", False) and (node["id"], port["name"]) not in incoming:
                _fail("unconnected_input", "A required input is not connected", node_id=node["id"], port=port["name"])
    for block in result.get("bounded_iterations", []):
        block["node_ids"].sort()
        block["state"].sort(key=lambda state: (state["target_node"], state["target_port"]))
    if "bounded_iterations" in result:
        result["bounded_iterations"].sort(key=lambda block: block["id"])
    result["nodes"].sort(key=lambda node: node["id"])
    result["edges"].sort(key=lambda edge: tuple(edge[key] for key in ("source_node", "source_port", "target_node", "target_port")))
    result["resources"].sort(key=lambda ref: ref["id"])
    result["external_dependencies"].sort(key=lambda ref: ref["id"])
    result["outputs"].sort(key=lambda port: port["name"])
    if "files" in result["source"]:
        result["source"]["files"].sort(key=lambda ref: ref["path"])
    if "capabilities" in result:
        result["capabilities"].sort()
    return result


def validate_recipe(recipe: dict) -> dict:
    """Public service boundary; returns normalized recipe or raises a typed error."""
    return normalize_recipe(recipe)


def validation_report(recipe: dict) -> dict:
    try:
        normalized = normalize_recipe(recipe)
        return {"valid": True, "errors": [], "normalized": normalized, "digest": recipe_digest(normalized)}
    except RecipeValidationError as exc:
        return {"valid": False, "errors": [{"code": exc.code, "message": str(exc), "details": exc.details}]}


def recipe_digest(recipe: dict) -> str:
    return _sha(canonical_json(normalize_recipe(recipe)).encode("utf-8"))


def default_recipe(project_id: str, recipe_id: str, name: str = "New recipe") -> dict:
    """An explicit draft with unresolved source/license, not a replay-ready claim."""
    return {"schema_version": "0", "recipe_id": recipe_id, "project_id": project_id, "revision": 1,
            "name": name, "source": {"repository": "https://example.invalid/unconfigured.git", "commit": "0" * 40, "files": []},
            "nodes": [], "edges": [], "resources": [],
            "environment": {"python": "3.12", "requirements": []}, "evaluation": {}, "outputs": [],
            "external_dependencies": [], "capabilities": ["local_cpu"],
            "licenses": [{"scope": "source", "license": "NOASSERTION", "redistribution_allowed": False}],
            "permissions": {"network": False, "execute_on_import": False}}


def _file_references(recipe: dict) -> dict[str, str]:
    refs = list(recipe["source"].get("files", []))
    if "lock_file" in recipe["environment"]:
        refs.append(recipe["environment"]["lock_file"])
    if "mlx_profile" in recipe["environment"]:
        refs.append(recipe["environment"]["mlx_profile"])
    for node in recipe["nodes"]:
        implementation = node["implementation_ref"]
        if isinstance(implementation, dict) and "path" in implementation:
            refs.append({"path": implementation["path"], "sha256": implementation["sha256"]})
    collected = {}
    casefold_paths = {}
    for ref in refs:
        path = _safe_path(ref["path"])
        if path in {"manifest.json", "recipe.json"}:
            _fail("reserved_path", "Recipe bundle metadata paths cannot be source files", path=path)
        if path in collected and collected[path] != ref["sha256"]:
            _fail("conflicting_file_hash", "A source file has contradictory hashes", path=path)
        if path.casefold() in casefold_paths and casefold_paths[path.casefold()] != path:
            _fail("ambiguous_path", "Source filenames must remain distinct on case-insensitive filesystems", path=path)
        casefold_paths[path.casefold()] = path
        collected[path] = ref["sha256"]
    return collected


def _read_source_file(root: Path, relative: str) -> bytes:
    _safe_path(relative)
    root = root.resolve(strict=True)
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            _fail("unsafe_symlink", "Source packaging does not follow symlinks", path=relative)
    if not current.is_file() or not current.resolve().is_relative_to(root):
        _fail("missing_source_file", "Declared source file is unavailable", path=relative)
    if current.stat().st_size > MAX_FILE_BYTES:
        _fail("bundle_limit", "Source file exceeds bundle size limit", path=relative)
    fd = os.open(current, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            _fail("unsafe_file", "Only ordinary files can be packaged", path=relative)
        data = handle.read(MAX_FILE_BYTES + 1)
    _validate_source_bytes(relative, data)
    return data


def _validate_source_bytes(path: str, data: bytes) -> None:
    if len(data) > MAX_FILE_BYTES:
        _fail("bundle_limit", "Source file exceeds bundle size limit", path=path)
    if PurePosixPath(path).suffix.lower() not in SAFE_FILE_SUFFIXES and PurePosixPath(path).name not in {"LICENSE", "NOTICE"}:
        _fail("unsafe_file_type", "Bundles include source/configuration text, not weights, datasets or executables", path=path)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        _fail("unsafe_file_type", "Bundled source must be UTF-8 text", path=path)
    if b"\x00" in data or SECRET_BYTES.search(data):
        _fail("secret_detected", "Potential secret or binary content in bundled source", path=path)


def _bundle_digest(recipe_hash: str, files: dict) -> str:
    file_index = {name: item["sha256"] for name, item in files.items()}
    return _sha(canonical_json({"bundle_version": "0", "recipe_digest": recipe_hash, "files": file_index}).encode())


def export_bundle(recipe: dict, destination: str | Path | None = None, source_root: str | Path | None = None) -> dict:
    normalized = normalize_recipe(recipe)
    files = {}
    if source_root is not None:
        for path, expected in _file_references(normalized).items():
            data = _read_source_file(Path(source_root), path)
            actual = _sha(data)
            if actual != expected:
                _fail("source_hash_mismatch", "Source bytes do not match the recipe manifest", path=path, expected=expected, actual=actual)
            files[path] = {"sha256": actual, "encoding": "base64", "content": base64.b64encode(data).decode("ascii")}
    if sum(len(item["content"]) for item in files.values()) > MAX_BUNDLE_BYTES:
        _fail("bundle_limit", "Source bundle exceeds size limit")
    digest = recipe_digest(normalized)
    bundle = {"bundle_version": "0", "recipe": normalized, "digest": digest, "files": files,
              "bundle_digest": _bundle_digest(digest, files)}
    if destination is not None:
        write_bundle_archive(bundle, destination)
    return bundle


def build_manifest(recipe: dict, **kwargs: Any) -> dict:
    return export_bundle(recipe, **kwargs)


def pin_source_files(recipe: dict, source_root: str | Path, paths: list[str], commit: str) -> dict:
    """Bind explicitly selected source files to a real local Git commit.

    This is an explicit authoring/export helper, never called on bundle import.
    Git only reads blob objects; no user module, hooks, shell, or network runs.
    Dirty files are rejected rather than recording a misleading source revision.
    """
    if not re.fullmatch(GIT_SHA, commit):
        _fail("invalid_source_revision", "A full immutable Git commit is required")
    root = Path(source_root).resolve(strict=True)
    selected = []
    for path in sorted(set(paths)):
        data = _read_source_file(root, path)
        try:
            blob = subprocess.run(
                ["git", "--no-pager", "-c", "protocol.allow=never", "-C", str(root), "cat-file", "blob", f"{commit}:{path}"],
                env={**{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
                     "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=True, timeout=30,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            _fail("source_revision_unavailable", "Declared source path is absent from the local Git revision", path=path)
        if blob != data:
            _fail("dirty_source", "Source differs from the declared Git revision; commit it before export", path=path)
        selected.append({"path": path, "sha256": _sha(data)})
    result = copy.deepcopy(recipe)
    result.pop("digest", None)
    result["source"]["commit"] = commit
    result["source"]["files"] = selected
    hashes = {item["path"]: item["sha256"] for item in selected}
    for node in result["nodes"]:
        implementation = node["implementation_ref"]
        if isinstance(implementation, dict):
            implementation["commit"] = commit
            if implementation.get("path") in hashes:
                implementation["sha256"] = hashes[implementation["path"]]
    for profile in ("lock_file", "mlx_profile"):
        if result["environment"].get(profile, {}).get("path") in hashes:
            result["environment"][profile]["sha256"] = hashes[result["environment"][profile]["path"]]
    return normalize_recipe(result)


def _strict_json(data: bytes) -> dict:
    def unique_pairs(pairs: list) -> dict:
        obj = {}
        for key, value in pairs:
            if key in obj:
                _fail("duplicate_json_key", "Duplicate JSON keys are forbidden", key=key)
            obj[key] = value
        return obj
    if len(data) > MAX_BUNDLE_BYTES:
        _fail("bundle_limit", "Manifest exceeds bundle size limit")
    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=unique_pairs,
                            parse_constant=lambda value: _fail("invalid_json", "Non-finite JSON numbers are forbidden"))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        if isinstance(exc, RecipeValidationError):
            raise
        _fail("invalid_json", "Bundle manifest is not valid JSON")
    _json_value(result)
    return result


def parse_bundle_json(text: str, *, max_bytes: int = 8 * 1024 * 1024) -> dict:
    """Decode lossless transport text without changing existing numeric canonicalization.

    This is an inert, bounded JSON parser, never a path or archive opener. It uses
    the same duplicate-key, non-finite-number, depth and item checks as archives.
    """
    if not isinstance(text, str):
        _fail("invalid_json", "Raw bundle JSON must be a string")
    if len(text) > max_bytes:
        _fail("bundle_limit", "Raw bundle JSON exceeds the UTF-8 size limit", max_bytes=max_bytes)
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        _fail("invalid_json", "Raw bundle JSON must be valid UTF-8")
    if len(data) > max_bytes:
        _fail("bundle_limit", "Raw bundle JSON exceeds the UTF-8 size limit", max_bytes=max_bytes)
    result = _strict_json(data)
    if not isinstance(result, dict):
        _fail("invalid_bundle", "Raw bundle JSON must contain an object envelope")
    return result


def _validated_bundle(bundle: dict) -> tuple[dict, dict[str, bytes]]:
    _json_value(bundle)
    expected_keys = {"bundle_version", "recipe", "digest", "files", "bundle_digest"}
    if not isinstance(bundle, dict) or set(bundle) != expected_keys or bundle.get("bundle_version") != "0":
        _fail("invalid_bundle", "Bundle must have the version-0 integrity envelope")
    normalized = normalize_recipe(bundle["recipe"])
    digest = recipe_digest(normalized)
    if bundle["digest"] != digest:
        _fail("recipe_hash_mismatch", "Recipe was changed after export", expected=bundle["digest"], actual=digest)
    if not isinstance(bundle["files"], dict) or len(bundle["files"]) > MAX_BUNDLE_FILES:
        _fail("bundle_limit", "Invalid source file count")
    declared = _file_references(normalized)
    decoded = {}
    total = 0
    for path, item in bundle["files"].items():
        _safe_path(path)
        if path not in declared:
            _fail("undeclared_file", "Bundle contains a file absent from the recipe", path=path)
        if (not isinstance(item, dict) or set(item) != {"sha256", "encoding", "content"}
                or item["encoding"] != "base64" or not isinstance(item["content"], str)):
            _fail("invalid_bundle_file", "Bundled files require a SHA256 and base64 bytes", path=path)
        if len(item["content"]) > 4 * ((MAX_FILE_BYTES + 2) // 3):
            _fail("bundle_limit", "Encoded file exceeds source size limit", path=path)
        try:
            data = base64.b64decode(item["content"], validate=True)
        except (ValueError, binascii.Error):
            _fail("invalid_bundle_file", "Invalid base64 file content", path=path)
        total += len(data)
        if total > MAX_BUNDLE_BYTES:
            _fail("bundle_limit", "Expanded bundle exceeds size limit")
        _validate_source_bytes(path, data)
        actual = _sha(data)
        if actual != item["sha256"] or actual != declared[path]:
            _fail("file_hash_mismatch", "Bundled source failed integrity verification", path=path)
        decoded[path] = data
    if bundle["bundle_digest"] != _bundle_digest(digest, bundle["files"]):
        _fail("bundle_hash_mismatch", "Bundle inventory was changed after export")
    return normalized, decoded


def write_bundle_archive(bundle: dict, destination: str | Path) -> None:
    """Write a deterministic tar, with no executable bits or symlink members."""
    normalized, decoded = _validated_bundle(bundle)
    output = Path(destination)
    if output.exists() and output.is_dir():
        output = output / "recipe.tar"
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        _fail("unsafe_symlink", "Archive destination cannot be a symlink")
    manifest = {**bundle, "files": {path: {"sha256": _sha(data), "encoding": "base64", "content": ""}
                                   for path, data in decoded.items()}}
    # The archive manifest indexes physical members; content is reconstructed only after all members pass checks.
    manifest["files"] = {path: {"sha256": _sha(data)} for path, data in decoded.items()}
    payloads = {"manifest.json": canonical_json(manifest).encode("utf-8"),
                **{f"files/{path}": data for path, data in decoded.items()}}
    fd, temporary = tempfile.mkstemp(prefix=".recipe-", suffix=".tar", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            with tarfile.open(fileobj=handle, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for name in sorted(payloads):
                    data = payloads[name]
                    member = tarfile.TarInfo(name)
                    member.size, member.mode, member.mtime = len(data), 0o644, 0
                    member.uid = member.gid = 0
                    member.uname = member.gname = ""
                    archive.addfile(member, io.BytesIO(data))
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_bundle_archive(path: str | Path) -> dict:
    path = Path(path)
    if path.is_symlink():
        _fail("unsafe_symlink", "Bundle archive cannot be a symlink")
    if not path.is_file() or path.stat().st_size > MAX_BUNDLE_BYTES:
        _fail("bundle_limit", "Archive is unavailable or exceeds size limit")
    members: dict[str, bytes] = {}
    total = 0
    try:
        # Only our bounded, uncompressed USTAR transport is accepted. Letting
        # tarfile auto-decompress a hostile PAX header bypasses expanded member
        # limits before the first TarInfo is returned.
        with tarfile.open(path, "r:") as archive:
            for member in archive:
                _safe_path(member.name)
                if not member.isfile() or member.issparse() or member.linkname:
                    _fail("unsafe_archive_member", "Only ordinary non-sparse file members are accepted", path=member.name)
                if member.name in members:
                    _fail("duplicate_archive_member", "Archive members must have unique names", path=member.name)
                if len(members) >= MAX_BUNDLE_FILES + 1 or member.size > MAX_BUNDLE_BYTES or member.size < 0:
                    _fail("bundle_limit", "Archive member exceeds bounds")
                total += member.size
                if total > MAX_BUNDLE_BYTES:
                    _fail("bundle_limit", "Expanded archive exceeds bounds")
                if member.name != "manifest.json" and not member.name.startswith("files/"):
                    _fail("unexpected_archive_member", "Archive contains an unexpected file", path=member.name)
                handle = archive.extractfile(member)
                if handle is None:
                    _fail("invalid_archive", "Archive member is unreadable")
                data = handle.read(member.size + 1)
                if len(data) != member.size:
                    _fail("invalid_archive", "Archive member length is inconsistent")
                members[member.name] = data
    except (tarfile.TarError, OSError) as exc:
        _fail("invalid_archive", "Cannot read bundle archive", reason=type(exc).__name__)
    if "manifest.json" not in members:
        _fail("invalid_bundle", "Archive is missing manifest.json")
    manifest = _strict_json(members.pop("manifest.json"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        _fail("invalid_bundle", "Archive manifest has no file inventory")
    files = {}
    for name, item in manifest["files"].items():
        _safe_path(name)
        if not isinstance(item, dict) or set(item) != {"sha256"}:
            _fail("invalid_bundle_file", "Archive file inventory must contain SHA256 values")
        content = members.pop(f"files/{name}", None)
        if content is None:
            _fail("missing_bundle_file", "A declared archive file is missing", path=name)
        files[name] = {"sha256": item["sha256"], "encoding": "base64", "content": base64.b64encode(content).decode()}
    if members:
        _fail("undeclared_file", "Archive contains files absent from its integrity inventory")
    return {**manifest, "files": files}


def _write_imported_files(destination: str | Path, recipe: dict, files: dict[str, bytes]) -> None:
    target = Path(destination).absolute()
    for parent in [target, *target.parents]:
        if parent.is_symlink():
            _fail("unsafe_symlink", "Import destination cannot have symlink components")
    if target.exists():
        _fail("destination_exists", "Import writes to a new directory only; existing contents are never overwritten")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".recipe-import-", dir=target.parent))
    try:
        for name, data in {"recipe.json": canonical_json(recipe).encode(), **files}.items():
            output = staging / name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
            output.chmod(0o600)
        os.rename(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def resolve_dependencies(recipe: dict, base_path: str | Path | None = None, installed: bool = False,
                         *, bundled_files: dict[str, bytes] | None = None) -> dict:
    """Check declared local bytes and exact installed pins, without network or execution.

    External network references are explicitly unresolved until a separate,
    authorized resolver downloads and verifies them. License names are metadata,
    not a legal clearance claim.
    """
    normalized = normalize_recipe(recipe)
    resolved, unresolved = [], []
    available = dict(bundled_files or {})
    source_files = _file_references(normalized)
    for name, expected in source_files.items():
        data = available.get(name)
        if data is None and base_path is not None:
            try:
                data = _read_source_file(Path(base_path), name)
            except (RecipeValidationError, FileNotFoundError):
                data = None
        if data is not None and _sha(data) == expected:
            available[name] = data
            resolved.append({"id": name, "kind": "file", "sha256": expected})
        else:
            unresolved.append({"id": name, "reason": "missing_or_hash_mismatch", "required": True})
    for node in normalized["nodes"]:
        implementation = node["implementation_ref"]
        if node["kind"] == "code.python" and "path" not in implementation:
            unresolved.append({"id": node["id"], "reason": "python_module_not_packaged", "required": True})
    source = normalized["source"]
    if source["commit"] == "0" * len(source["commit"]) or not source_files:
        unresolved.append({"id": "source", "reason": "source_not_packaged", "required": True})
    elif all(any(item["id"] == name for item in resolved) for name in source_files):
        resolved.append({"id": "source", "kind": "pinned_source_snapshot", "commit": source["commit"],
                         "note": "File bytes verified; Git repository authenticity is not established by a bundle digest."})
    for reference in normalized["resources"] + normalized["external_dependencies"]:
        parsed = urlsplit(reference["url"])
        name = (parsed.netloc + parsed.path).lstrip("/")
        if parsed.scheme == "bundle" and name in available and _sha(available[name]) == reference.get("sha256"):
            resolved.append({"id": reference["id"], "kind": reference["kind"], "sha256": reference["sha256"]})
        else:
            unresolved.append({"id": reference["id"], "url": reference["url"],
                               "reason": "private_reference" if reference.get("private") else "external_reference_not_fetched",
                               "required": reference.get("required", True)})
        if reference["license"].upper() in {"UNKNOWN", "NOASSERTION", "UNLICENSED"}:
            unresolved.append({"id": reference["id"], "reason": "license_review_required", "required": True})
    licenses = normalized.get("licenses", [])
    if not licenses or any(item["license"].upper() in {"UNKNOWN", "NOASSERTION", "UNLICENSED"} for item in licenses):
        unresolved.append({"id": "source_license", "reason": "license_review_required", "required": True})
    for pin in normalized["environment"]["requirements"]:
        package, version = pin.split("==")
        package = package.split("[")[0]
        if installed:
            try:
                actual = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                actual = None
            if actual == version:
                resolved.append({"id": package, "kind": "python_package", "version": version})
                continue
            reason = "package_missing" if actual is None else "package_version_mismatch"
            unresolved.append({"id": package, "reason": reason, "expected": version, "actual": actual, "required": True})
        else:
            unresolved.append({"id": package, "reason": "environment_not_checked", "expected": version, "required": True})
    if installed:
        expected_python = normalized["environment"]["python"]
        actual_python = ".".join(map(str, sys.version_info[:len(expected_python.split("."))]))
        if actual_python != expected_python:
            unresolved.append({"id": "python", "reason": "python_version_mismatch", "expected": expected_python,
                               "actual": actual_python, "required": True})
        else:
            resolved.append({"id": "python", "kind": "runtime", "version": actual_python})
    return {"resolved": resolved, "unresolved": unresolved, "ready": not any(item.get("required", True) for item in unresolved),
            "network_accessed": False, "execution_allowed": False}


def import_bundle(bundle: dict | str | Path, destination: str | Path | None = None) -> dict:
    transport = _read_bundle_archive(bundle) if isinstance(bundle, (str, Path)) else bundle
    normalized, files = _validated_bundle(transport)
    report = resolve_dependencies(normalized, bundled_files=files)
    if destination is not None:
        _write_imported_files(destination, normalized, files)
    return {"recipe": normalized, "digest": recipe_digest(normalized),
            "bundle_digest": transport["bundle_digest"], "unresolved_dependencies": report["unresolved"],
            "resolved_dependencies": report["resolved"], "ready": report["ready"], "execution_allowed": False,
            "trust_required": True}


def validate_manifest(manifest: dict) -> dict:
    return import_bundle(manifest)
