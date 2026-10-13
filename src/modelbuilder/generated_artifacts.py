"""Inert generated-data lineage; evidence is never an execution approval.

Seals bind canonical payload bytes and exact parent evidence without recursive
hashes. Only current project-owned policy admission can authorize their use.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from .artifact_rights import RIGHTS_SCHEMA, combine_parent_rights
from .evaluation import digest

GENERATED_SCHEMAS = frozenset({"modelbuilder.generated-candidates/v1", "modelbuilder.generated-candidates/v2",
                               "modelbuilder.model-judgments/v1", "modelbuilder.model-judgments/v2"})
PREPARED_SOURCES = frozenset({"modelbuilder.local.prepare-dataset/v1", "modelbuilder.transformers.prepare-dataset/v1",
                              "modelbuilder.moonshiner.prepare-dataset/v1"})
SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
LINEAGE_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["schema_version", "content_digest", "parents"], "properties": {
        "schema_version": {"const": "modelbuilder.generated-lineage/v1"}, "content_digest": SHA,
        "parents": {"type": "array", "minItems": 1, "maxItems": 4096, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["artifact_id", "content_digest", "rights"], "properties": {
                "artifact_id": {"type": "string", "minLength": 1}, "content_digest": SHA,
                "rights": {"anyOf": [RIGHTS_SCHEMA, {"type": "null"}]}}}}}}
MANIFEST_FIELDS = {"rights": RIGHTS_SCHEMA, "lineage": LINEAGE_SCHEMA, "digest": SHA}


def _content(value):
    return {key: item for key, item in value.items() if key not in {"digest", "rights", "lineage"}}


def attach_lineage(value, inputs, context):
    """Seal new adapter output only; never retrofit a historic native artifact."""
    if value.get("schema") not in GENERATED_SCHEMAS and value.get("provenance", {}).get("source") not in PREPARED_SOURCES:
        raise ValueError("Only known generated-data contracts can acquire adapter lineage")
    parents = deepcopy(context.get("declared_rights_parents", [{
        "artifact_id": "declared-source-and-models", "content_digest": digest(context.get("source", {})),
        "rights": context.get("declared_artifact_rights")}]))
    for name, parent in sorted(inputs.items()):
        parents.append({"artifact_id": name + ":" + digest(parent), "content_digest": digest(parent),
                        "rights": deepcopy(parent.get("rights"))})
    result = {**deepcopy(value), "lineage": {"schema_version": "modelbuilder.generated-lineage/v1",
        "content_digest": digest(_content(value)), "parents": parents}, "rights": combine_parent_rights(parents)}
    result["digest"] = digest({key: item for key, item in result.items() if key != "digest"})
    return result


def validate_lineage(value):
    """Validate content/evidence consistency, not legal permissions or ownership."""
    if not isinstance(value, dict) or not {"rights", "lineage", "digest"} <= value.keys():
        raise ValueError("Generated artifact has no complete parent rights lineage")
    Draft202012Validator(LINEAGE_SCHEMA).validate(value["lineage"])
    Draft202012Validator(RIGHTS_SCHEMA).validate(value["rights"])
    if (value["lineage"]["content_digest"] != digest(_content(value))
            or value["rights"] != combine_parent_rights(value["lineage"]["parents"])
            or value["digest"] != digest({key: item for key, item in value.items() if key != "digest"})):
        raise ValueError("Generated artifact content or parent rights integrity mismatch")
    return value["rights"]


def manifest_rights(manifest):
    if (manifest.get("provenance", {}).get("source") in PREPARED_SOURCES
            or any(key in manifest for key in ("lineage", "rights", "digest"))):
        return validate_lineage(manifest)
    return None


def is_protected_dict(value):
    """Known data contracts are protected even when carried by native dict ports."""
    return (value.get("schema") in GENERATED_SCHEMAS | {"modelbuilder.dataset/v1", "modelbuilder.dataset-consumption/v1"}
            or value.get("schema_version") in {"modelbuilder.checkpoint/v1", "modelbuilder.checkpoint/v2"}
            or isinstance(value.get("consumption"), dict)
            and value["consumption"].get("schema") == "modelbuilder.dataset-consumption/v1"
            or isinstance(value.get("stream"), dict)
            and value["stream"].get("schema") == "modelbuilder.dataset-stream/v1"
            or "rights" in value)


def dict_rights(value):
    if value.get("schema") in GENERATED_SCHEMAS:
        return validate_lineage(value)
    return value.get("rights")


def prepared_rights(directory):
    """Inspect a prepared dataset Path without loading producer code or policies."""
    from .dataset import validate_manifest
    from .export_packages import safe_file
    directory = Path(directory)
    with safe_file(directory / "dataset.manifest.json", max_bytes=8 * 1024 * 1024) as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Prepared dataset manifest exceeds its inspection bound")
    manifest = json.loads(raw)
    validate_manifest(manifest)
    rights = manifest_rights(manifest)
    for source in manifest["sources"]:
        # Native prepared output owns only its local, manifest-declared files.
        with safe_file(directory / source["path"]) as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != source["sha256"]:
            raise ValueError("Prepared dataset bytes differ from their parent rights seal")
    return rights
