"""Reviewed Moonshiner local composition inside the existing native component ABI.

This adapter never starts Moonshiner's trace, daemon, setup or publisher paths.
Provider distillation is deliberately unavailable in v1. Prepared files still
require the existing explicit dataset.register / dataset.bind operations.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from . import PROTOCOL, identity
from ...artifact_rights import RIGHTS_SCHEMA
from ...dataset import validate_manifest
from ...generated_artifacts import attach_lineage

UPSTREAM_COMMIT = "7abb66ac5328db6a5d5f3990cf85ddc40310f0b8"
UPSTREAM_VERSION = "0.8.23"
SOURCE = "modelbuilder.moonshiner.prepare-dataset/v1"
MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 1024
_TEXT = {"type": "string", "minLength": 1, "maxLength": 2048}
_HASH = {"type": "string", "minLength": 64, "maxLength": 64}


def _object(properties):
    return {"type": "object", "additionalProperties": False,
            "required": list(properties), "properties": properties}


PARAMETERS = _object({
    "mode": {"enum": ["offline-compose"]},
    "installation": _object({"version": {"enum": [UPSTREAM_VERSION]},
        "reviewed_upstream_commit": {"enum": [UPSTREAM_COMMIT]},
        "source_sha256": _HASH, "environment_sha256": _HASH}),
    "traces_digest": _HASH,
    "teacher": _object({"model": _TEXT, "runtime": _TEXT,
        "rights_parent_id": _TEXT}),
    "limits": _object({"max_records": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        "max_bytes": {"type": "integer", "minimum": 1024, "maximum": MAX_BYTES},
        "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 300},
        "max_cost_usd": {"enum": [0]}}),
    "seed": {"type": "integer", "minimum": 0, "maximum": 2147483647},
    "name": _TEXT, "benchmark": _TEXT, "license": _TEXT, "root_id": _TEXT,
    "review_note": _TEXT,
    "split_policy": {"enum": ["train-only"]},
    "label_policy": {"enum": ["final-assistant-content-only"]},
})
MESSAGE_SCHEMA = _object({"role": {"type": "string", "enum": ["system", "user", "assistant", "tool"]},
    "content": {"type": "string", "maxLength": MAX_BYTES}})
MESSAGE_SCHEMA["properties"].update({"reasoning_content": {"type": "string", "maxLength": MAX_BYTES},
    "tool_call_id": {"type": "string"}, "name": {"type": "string"},
    "tool_calls": {"type": "array", "maxItems": 128, "items": {"type": "object"}}})
ROW_SCHEMA = _object({"id": _TEXT,
    "messages": {"type": "array", "minItems": 2, "maxItems": 256, "items": MESSAGE_SCHEMA},
    "target_message_index": {"type": "integer", "minimum": 1, "maximum": 255},
    "message_loss_mask": {"type": "array", "minItems": 2, "maxItems": 256,
                          "items": {"type": "integer", "enum": [0, 1]}},
    "target_text": {"type": "string", "minLength": 1, "maxLength": MAX_BYTES},
    "target_sha256": _HASH, "source_row_sha256": _HASH,
    "source_trajectory_id": _TEXT, "source_trajectory_sha256": _HASH,
    "teacher_model": _TEXT, "teacher_runtime": _TEXT,
    "label_policy": {"type": "string", "enum": ["final-assistant-content-only"]}})


def catalog_entries():
    descriptor = {"id": "modelbuilder.moonshiner.prepare-dataset", "version": "1", "protocol": PROTOCOL,
        "role": "dataset", "description": "Linux-only reviewed Moonshiner 0.8.23 local composition of train-only accepted traces. Provider distillation is unavailable; no network, publishing or automatic training.",
        "parameter_schema": deepcopy(PARAMETERS),
        "capabilities": {"resume_modes": [], "export_formats": [], "frameworks": ["moonshiner"],
                         "devices": ["cpu"]}}
    return [{**descriptor, "inputs": [{"name": "traces", "type": "DatasetArtifact"}],
        "outputs": [{"name": "prepared", "type": "Path"}],
        "adapter": {"module": __name__, "entrypoint": "prepare_dataset",
            "source_binding": "Pin a wrapper in the existing trusted source closure and review the installed Moonshiner source/environment receipt.",
            "example_source": "examples/moonshiner/adapter.py"}, "component_digest": identity(descriptor)}]


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _input_rows(inputs, parameters, context):
    if set(inputs) != {"traces"} or not isinstance(inputs["traces"], dict):
        raise ValueError("Moonshiner requires one native traces dataset artifact")
    traces = inputs["traces"]
    if identity(traces) != parameters["traces_digest"]:
        raise ValueError("Reviewed trace dataset identity changed")
    if traces.get("schema") != "modelbuilder.dataset/v1" or traces.get("split", {}).get("role") != "train":
        raise ValueError("Only an explicitly bound train split may be composed; held-out data is refused")
    if traces.get("dataset", {}).get("benchmark") == parameters["benchmark"]:
        raise ValueError("Prepared training data requires a distinct benchmark identity")
    Draft202012Validator(RIGHTS_SCHEMA).validate(traces.get("rights"))
    teacher = parameters["teacher"]
    parents = context.get("declared_rights_parents", [])
    parent = next((item for item in parents if item["artifact_id"] == teacher["rights_parent_id"]), None)
    if parent is None or not parent.get("rights"):
        raise ValueError("Teacher/model rights must name an admitted declared parent; row labels grant no rights")
    Draft202012Validator(RIGHTS_SCHEMA).validate(parent["rights"])
    rows = traces.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= parameters["limits"]["max_records"]:
        raise ValueError("Trace count is empty or exceeds the reviewed record bound")
    if len(_json(rows)) > parameters["limits"]["max_bytes"]:
        raise ValueError("Trace bytes exceed the reviewed byte bound")
    for row in rows:
        if not isinstance(row, dict) or row.get("split") != "train":
            raise ValueError("Every canonical trace must explicitly belong to train")
        if row.get("teacher_model") != teacher["model"] or row.get("teacher_runtime") != teacher["runtime"]:
            raise ValueError("Canonical trace teacher differs from its reviewed teacher identity")
        messages = row.get("messages")
        Draft202012Validator(ROW_SCHEMA["properties"]["messages"]).validate(messages)
        target = row.get("target_message_index")
        if type(target) is not int or target != len(messages) - 1:
            raise ValueError("Explicit final target_message_index is required; it is never guessed")
        last = messages[target]
        if last["role"] != "assistant" or not last["content"].strip() or last.get("tool_calls"):
            raise ValueError("Only final assistant text targets are supported, not tool-call targets")
        if not any(message["role"] == "user" for message in messages[:target]):
            raise ValueError("A final assistant target requires preceding user context")
        if (not isinstance(row.get("source_trajectory_id"), str) or not row["source_trajectory_id"]
                or len(row["source_trajectory_id"]) > 2048
                or not _is_hash(row.get("source_trajectory_sha256"))):
            raise ValueError("Canonical trajectory identity and SHA256 are required")
    return rows


def _is_hash(value):
    return isinstance(value, str) and len(value) == 64 and set(value) <= set("0123456789abcdef")


def _prepared_rows(composed, originals, source):
    """Restore only verified target identities dropped by upstream compose."""
    result, seen = [], set()
    for row in composed:
        if not isinstance(row, dict) or not isinstance(row.get("meta"), dict):
            raise ValueError("Malformed Moonshiner composition row")
        meta = row["meta"]
        index = meta.get("source_row")
        if (type(index) is not int or not 0 <= index < len(originals)
                or meta.get("source") != str(source) or index in seen):
            raise ValueError("Moonshiner output has unknown or duplicate source identity")
        original = originals[index]
        # Sanitized/rewritten content must receive a new explicit review. Matching
        # the full conversation prevents the dropped target index attaching to
        # another assistant message, even after upstream shuffling/deduplication.
        if row.get("messages") != original["messages"]:
            raise ValueError("Moonshiner changed reviewed messages; review the sanitized trace separately")
        target = original["target_message_index"]
        target_text = original["messages"][target]["content"]
        source_digest = identity(original)
        record = {"id": "moonshiner-" + source_digest, "messages": deepcopy(row["messages"]),
            "target_message_index": target, "message_loss_mask": [int(i == target) for i in range(len(row["messages"]))],
            "target_text": target_text, "target_sha256": hashlib.sha256(target_text.encode()).hexdigest(),
            "source_row_sha256": source_digest, "source_trajectory_id": original["source_trajectory_id"],
            "source_trajectory_sha256": original["source_trajectory_sha256"],
            "teacher_model": original["teacher_model"], "teacher_runtime": original["teacher_runtime"],
            "label_policy": "final-assistant-content-only"}
        Draft202012Validator(ROW_SCHEMA).validate(record)
        result.append(record)
        seen.add(index)
    if not result:
        raise ValueError("Moonshiner produced no training rows")
    return result


def prepare_dataset(*, inputs, parameters, context):
    """Finite local CLI composition. Never authorizes network/provider execution."""
    Draft202012Validator(PARAMETERS).validate(parameters)
    _json(parameters)
    rows = _input_rows(inputs, parameters, context)
    from .moonshiner_process import compose, installation_identity, read_jsonl
    installed = installation_identity()
    if installed != parameters["installation"]:
        raise ValueError("Moonshiner installed source/version/environment differs from its review")
    output = Path(context["output_dir"]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    work = output / "moonshiner-work"
    work.mkdir(exist_ok=False)
    source = work / "accepted-traces.jsonl"
    source.write_bytes(b"".join(_json(row) + b"\n" for row in rows))
    composed_path, receipt = compose(source, work, parameters, context)
    if installation_identity() != installed:
        raise ValueError("Moonshiner installed identity changed during composition")
    selected = _prepared_rows(read_jsonl(composed_path, parameters["limits"]), rows, source)
    content = b"".join(_json(row) + b"\n" for row in selected)
    if len(content) > parameters["limits"]["max_bytes"]:
        raise ValueError("Prepared dataset exceeds its reviewed byte bound")
    prepared = output / "prepared"
    filename = "train-" + hashlib.sha256(content).hexdigest() + ".jsonl"
    provenance = {"installation": installed, "configuration": receipt["configuration"],
        "configuration_sha256": identity(receipt["configuration"]), "seed": parameters["seed"],
        "traces_digest": parameters["traces_digest"], "teacher": parameters["teacher"],
        "limits": parameters["limits"], "split_policy": parameters["split_policy"],
        "label_policy": parameters["label_policy"], "review_note": parameters["review_note"],
        "provider_distillation": "unavailable", "network": "disabled",
        "network_enforcement": "python-audit-defense-in-depth-for-reviewed-code; not an OS sandbox",
        "registration_started": False,
        "training_started": False, "tokenization_performed": False,
        "consumer_requirement": "Validate final-target metadata and apply content-only token masking in a compatible source-owned conversation adapter",
        "row_count": len(selected),
        "runtime_identities": context.get("identities", {})}
    manifest = {"name": parameters["name"], "benchmark": parameters["benchmark"], "license": parameters["license"],
        "description": "Reviewed local Moonshiner train-only composition; only target_text is supervised. Historical assistant/reasoning/tool messages are context, never labels.",
        "provenance": {"source": SOURCE, "source_revision": parameters["traces_digest"],
                       "description": _json(provenance).decode()},
        "sources": [{"id": "composed", "root_id": parameters["root_id"], "path": filename,
                     "format": "jsonl", "sha256": hashlib.sha256(content).hexdigest()}],
        "id_column": "id", "row_schema": deepcopy(ROW_SCHEMA),
        "splits": [{"id": "train", "role": "train", "source_ids": ["composed"]}], "transformations": []}
    manifest = attach_lineage(manifest, inputs, context)
    validate_manifest(manifest)
    prepared.mkdir(exist_ok=False)
    (prepared / filename).write_bytes(content)
    (prepared / "dataset.manifest.json").write_bytes(_json(manifest))
    (prepared / "moonshiner.provenance.json").write_bytes(_json(provenance))
    return {"prepared": prepared}
