"""Service-bound declared-input rights checks at native execution boundaries.

The service attaches policy authority to a request-local backend view, never to
an operation payload. The native worker reopens only that captured service store
read-only and compares exact current policy revisions before using source code.
This is declared-input admission, not legal interpretation or an I/O sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote

PROTOCOL = "modelbuilder.execution-rights/v1"
MAX_BYTES = 4 * 1024 * 1024
MAX_POLICIES = 1024
ASSET_KINDS = {"model", "dataset", "checkpoint"}
ACTIONS = {"train", "evaluate", "generate", "export"}
DATASET_PURPOSE_ACTIONS = {"training": "train", "calibration": "train", "evaluation": "evaluate"}
RESERVED_FIELDS = {"reviewed_license_policies", "rights_authorization", "execution_rights",
                   "policy_store_path", "license_admission", "policy_store_guard", "rights_extensions",
                   "_execution_rights_authorization"}


def _error(code, message, **details):
    from .admission import BackendError
    raise BackendError(code, message, {"declared_inputs_only": True,
                                      "permissions_inferred_from_label": False, **details})


def _json(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        _error("license_rights_unreviewed", "Rights authorization must be bounded inert JSON", reason=str(error))
    if len(raw.encode()) > MAX_BYTES:
        _error("license_rights_unreviewed", "Rights authorization exceeds its 4 MiB bound")
    return raw


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class Authority:
    project_id: str
    policies_json: str
    store_guard_json: str


def _store_guard(path):
    if str(path) == ":memory:":
        return None
    raw = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in [raw, *raw.parents]):
        _error("license_authority_unavailable", "Policy authority store cannot use a symlink")
    try:
        info = raw.stat()
        if not raw.is_file():
            raise ValueError("not a regular file")
    except (OSError, ValueError) as error:
        _error("license_authority_unavailable", "Authoritative policy store is unavailable", reason=str(error))
    return {"path": str(raw), "device": info.st_dev, "inode": info.st_ino}


def bind_authority(project_id: str, policies: dict, policy_store_path) -> Authority:
    """Internal service call only; not exposed as a recipe/API setting."""
    if (not isinstance(project_id, str) or not project_id or not isinstance(policies, dict)
            or len(policies) > MAX_POLICIES or any(not isinstance(key, str) or not key for key in policies)):
        _error("license_authority_unavailable", "Invalid project-scoped policy authority")
    for key, policy in policies.items():
        if (not isinstance(policy, dict) or policy.get("project_id") != project_id
                or policy.get("policy_id") != key or type(policy.get("revision")) is not int
                or policy["revision"] < 1):
            _error("license_authority_unavailable", "Policy authority must contain exact project-owned revisions")
    guard = _store_guard(policy_store_path)
    result = Authority(project_id, _json(policies), _json(guard))
    if policies and guard:
        _current_policies(project_id, policies, guard, policies.keys())
    return result


def _current_policies(project_id, policies, guard, policy_ids):
    identifiers = sorted(set(policy_ids))
    if not identifiers:
        return
    if guard is None:
        _error("license_authority_unavailable", "Native asset use requires a durable service policy store; an in-memory review cannot be rechecked at dispatch")
    if _store_guard(guard["path"]) != guard:
        _error("license_policy_changed", "Authoritative policy store identity changed; prepare execution again")
    connection = None
    try:
        uri = "file:" + quote(guard["path"], safe="/") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        for identifier in identifiers:
            row = connection.execute("SELECT project_id,revision,body FROM resources WHERE kind=? AND id=?",
                                     ("hub_license_policy", identifier)).fetchone()
            if row is None or row[0] != project_id or len(row[2].encode()) > MAX_BYTES:
                _error("license_policy_changed", "An authoritative policy is missing or belongs to another project", policy_id=identifier)
            current = json.loads(row[2])
            if (not isinstance(current, dict) or current != policies.get(identifier) or current.get("project_id") != project_id
                    or current.get("revision") != row[1]):
                _error("license_policy_changed", "A reviewed policy revision or status changed; prepare execution again", policy_id=identifier)
    except (sqlite3.Error, OSError, ValueError, TypeError) as error:
        _error("license_authority_unavailable", "Current policy revisions could not be read safely", reason=str(error))
    finally:
        if connection is not None:
            connection.close()
    if _store_guard(guard["path"]) != guard:
        _error("license_policy_changed", "Authoritative policy store changed during dispatch validation")


def has_declared_assets(recipe):
    return (any(item.get("kind") in ASSET_KINDS for item in recipe.get("resources", []) + recipe.get("external_dependencies", []))
            or "artifact_rights" in recipe.get("extensions", {}))


def dataset_action(purpose):
    """Map an explicit data operation to its requested action; grants no rights."""
    if not isinstance(purpose, str) or purpose not in DATASET_PURPOSE_ACTIONS:
        _error("license_usage_unspecified", "Dataset imports require explicit training, calibration or evaluation purpose")
    return DATASET_PURPOSE_ACTIONS[purpose]


def _known_action(node):
    if node.get("kind") == "dataset.version.import":
        parameters = node.get("parameters", {})
        return dataset_action(parameters.get("purpose") if isinstance(parameters, dict) else None)
    if node.get("kind") == "component.run":
        return {"training": "train", "evaluation": "evaluate", "export": "export"}.get(node.get("component", {}).get("role"))
    return {"model.initialize": "train", "model.train": "train", "model.checkpoint": "train",
            "model.evaluate": "evaluate", "model.compare": "evaluate", "model.export": "export"}.get(node.get("kind"))


def node_action(node, *, required=False, inherited=()):
    known = _known_action(node)
    explicit = node.get("provenance", {}).get("intended_use")
    if explicit is not None and (not isinstance(explicit, str) or explicit not in ACTIONS):
        _error("license_usage_unspecified", "Declare provenance.intended_use as train, evaluate, generate or export", node_id=node.get("id"))
    if known and explicit is not None and explicit != known:
        _error("license_usage_conflict", "Requested intended use conflicts with the declared native operation", node_id=node.get("id"), declared=known)
    if explicit or known:
        return [explicit or known]
    if node.get("kind") in {"dataset.import", "dataset.version.import", "dataset.synthetic", "dataset.validate"} and inherited:
        return sorted(set(inherited))
    if required:
        _error("license_usage_unspecified", "Generic asset-using source requires explicit provenance.intended_use; source trust does not grant rights", node_id=node.get("id"))
    return []


def _declarations(recipe):
    from ..hub_rights import source_review_scope
    return {"_source_review_scope": source_review_scope(recipe), "source": recipe.get("source", {}), "resources": recipe.get("resources", []),
            "external_dependencies": recipe.get("external_dependencies", []),
            "extensions": {key: value for key, value in recipe.get("extensions", {}).items()
                           if key in {"artifact_rights", "usage_context"}}}


def _validate_declared(recipe, action, policies):
    from ..hub_rights import validate_execution_rights
    from ..models import ServiceError
    try:
        return validate_execution_rights(recipe, intended_use=action, policies=policies)
    except ServiceError as error:
        _error(error.code, error.message, **error.details)



def _validate_envelope(envelope, action, policies, usage_context):
    from ..hub_rights import validate_artifact_rights
    from ..models import ServiceError
    try:
        return validate_artifact_rights(envelope, action, policies, usage_context=usage_context)
    except ServiceError as error:
        _error(error.code, error.message, **error.details)


def _unreviewed_builtin_origins(recipe):
    """Known builtin producers attach unresolved lineage without declarations.

    Only their connected asset outputs are predictable before execution. Generic
    source code and asset-free utilities retain their existing native checks.
    """
    producers = {"dataset.synthetic": {"data"}, "dataset.import": {"data"},
                 "dataset.version.import": {"data"}, "model.initialize": {"initial"},
                 "model.train": {"checkpoint"}}
    nodes = {node["id"]: node for node in recipe.get("nodes", [])}
    edges = list(recipe.get("edges", []))
    edges.extend(edge for block in recipe.get("bounded_iterations", [])
                 if block.get("max_iterations", 1) > 1 for edge in block.get("state", []))
    return [{"reason": "missing_reviewed_generated_origin", "node_id": edge["source_node"],
             "output_port": edge["source_port"], "consumer_node_id": edge["target_node"]}
            for edge in edges
            if edge["source_port"] in producers.get(nodes.get(edge["source_node"], {}).get("kind"), set())]


def _current_snapshot(guard, policies):
    """Recheck imported bytes, root identity and selected terms at native use."""
    if guard is None:
        return
    from ..source_workspace import SourceWorkspaces
    from ..export_packages import safe_file
    from ..models import ServiceError
    try:
        source = SourceWorkspaces({"snapshot": {"path": guard["root"], "kind": "imported_snapshot"}}).inspect("snapshot")
        if source["repository_id"] != guard["repository_id"] or source["snapshot_digest"] != guard["snapshot_digest"]:
            _error("license_source_changed", "Reviewed imported source snapshot was substituted")
        for identifier in guard["policy_ids"]:
            for item in policies[identifier]["source_files"]:
                with safe_file(Path(guard["root"]) / item["path"], max_bytes=item["size_bytes"]) as stream:
                    if hashlib.sha256(stream.read(item["size_bytes"] + 1)).hexdigest() != item["sha256"]:
                        _error("license_evidence_changed", "Reviewed imported source or terms changed before execution")
    except ServiceError as error:
        _error(error.code, error.message, **error.details)
    except (OSError, ValueError, KeyError, TypeError) as error:
        _error("license_source_changed", "Reviewed imported source or terms cannot be safely verified", reason=str(error))


def _snapshot_guard(backend, recipe, policies):
    origins = [policies.get(item.get("license_policy_id"), {})
               for item in recipe.get("extensions", {}).get("artifact_rights", {}).get("sources", [])
               if item.get("repo_type") == "source"]
    snapshots = [policy for policy in origins if policy.get("source_origin", {}).get("source_kind") == "imported_snapshot"]
    if not snapshots:
        return None
    origin = snapshots[0]["source_origin"]
    if any(policy["source_origin"]["repository_id"] != origin["repository_id"]
           or policy["source_origin"]["snapshot_digest"] != origin["snapshot_digest"] for policy in snapshots):
        _error("license_source_changed", "Direct source reviews disagree on the current imported snapshot")
    guard = {"root": str(backend.repository), "repository_id": origin["repository_id"],
             "snapshot_digest": origin["snapshot_digest"], "policy_ids": sorted(policy["policy_id"] for policy in snapshots)}
    _current_snapshot(guard, policies)
    return guard


def prepare_authorization(backend, payload, recipe, *, durable=False):
    """Build an inert receipt from service authority, never caller policy fields."""
    if RESERVED_FIELDS & payload.keys():
        _error("license_authority_unavailable", "Rights policy maps and store guards are internal service authority, not operation payload fields")
    authority = getattr(backend, "_execution_rights_authority", None)
    project_id = payload.get("project_id")
    if authority is not None and (not isinstance(authority, Authority) or authority.project_id != project_id):
        _error("license_authority_unavailable", "Execution policy authority belongs to another project")
    policies = json.loads(authority.policies_json) if authority else {}
    guard = json.loads(authority.store_guard_json) if authority else None
    governed = has_declared_assets(recipe)
    extensions = recipe.get("extensions", {})
    if "artifact_rights" in extensions and not isinstance(extensions["artifact_rights"], dict):
        _error("license_rights_unreviewed", "An explicit artifact rights declaration must be a complete rights envelope")
    if not governed:
        unresolved = _unreviewed_builtin_origins(recipe)
        if unresolved:
            _error("license_rights_unreviewed",
                   "Connected builtin dataset/checkpoint outputs require an explicitly reviewed origin before execution",
                   unresolved=unresolved)
    bound = [item for item in payload.get("resolved_input_bindings", []) if item.get("rights_required") or "rights" in item]
    resume_nodes = [node for node in recipe.get("nodes", []) if node.get("id") == payload.get("resume_node_id")
                    or not payload.get("resume_node_id") and _known_action(node) == "train"] if payload.get("resume_path") else []
    if payload.get("resume_path") and len(resume_nodes) != 1:
        _error("license_usage_unspecified", "Resume rights admission needs one exact selected checkpoint consumer")
    input_envelopes = {}
    for item in bound:
        input_envelopes.setdefault(item["node_id"], []).append(item.get("rights"))
    for node in resume_nodes:
        input_envelopes.setdefault(node["id"], []).extend(_input_envelopes(
            {"__resume_checkpoint__": Path(payload["resume_path"])}, node).values())
    if (governed or input_envelopes) and authority is None:
        _error("license_rights_unreviewed", "Declared model/dataset/checkpoint inputs need authoritative reviewed rights before execution")
    inherited = {action for node in recipe.get("nodes", []) for action in node_action(node)}
    if (governed or policies or input_envelopes) and any(node.get("cache_policy") == "enabled" for node in recipe.get("nodes", [])):
        _error("license_cache_unavailable", "Reviewed asset execution requires cache_policy disabled so each operation rechecks current rights")
    decisions = {}
    for node in recipe.get("nodes", []):
        actions = node_action(node, required=governed or node["id"] in input_envelopes, inherited=inherited)
        evidence = [_validate_declared(recipe, action, policies) for action in actions] if governed else []
        for envelope in input_envelopes.get(node["id"], []):
            for action in actions:
                evidence.append(_validate_envelope(envelope, action, policies, recipe.get("extensions", {}).get("usage_context")))
        decisions[node["id"]] = {"node": node, "actions": actions, "admission": evidence}
    used = {identifier for value in decisions.values() for decision in value["admission"]
            for identifier in decision["policy_ids"]}
    if used and (guard is not None or durable):
        _current_policies(project_id, policies, guard, used)
    receipt = {"protocol": PROTOCOL, "project_id": project_id,
               "scope": "declared_asset_inputs" if governed or input_envelopes else "no_declared_external_asset_inputs",
               "declarations": _declarations(recipe), "nodes": decisions,
               "source_snapshot_guard": _snapshot_guard(backend, recipe, policies),
               "native_input_rights": {key: [_digest(value) for value in values] for key, values in input_envelopes.items()},
               "policy_snapshot_digest": _digest(policies), "policy_store_identity": _digest(guard),
               "permissions_inferred_from_label": False}
    receipt["digest"] = _digest(receipt)
    return {"receipt": receipt, "policies": policies, "policy_store_guard": guard}


def _input_envelopes(inputs, node):
    from .components import read_checkpoint, read_export
    from ..generated_artifacts import is_protected_dict, dict_rights, prepared_rights
    result = {}
    ports = {port["name"]: port["type"] for port in node.get("inputs", [])}
    for name, value in inputs.items():
        protected, rights = False, None
        if isinstance(value, Path):
            if (value / "checkpoint.manifest.json").is_file():
                protected, rights = True, read_checkpoint(value).get("rights")
            elif (value / "export.manifest.json").is_file():
                protected, rights = True, read_export(value).get("rights")
            elif (value / "dataset.manifest.json").is_file():
                protected, rights = True, prepared_rights(value)
            elif (node.get("kind", "").startswith("model.") or name == "__resume_checkpoint__"
                  or ports.get(name) in {"DatasetArtifact", "CheckpointArtifact"}):
                protected = True
        elif isinstance(value, dict):
            protected = ports.get(name) in {"DatasetArtifact", "CheckpointArtifact"} or is_protected_dict(value)
            try:
                rights = dict_rights(value)
            except Exception as error:
                _error("license_rights_unreviewed", "Generated input has invalid parent rights lineage", input_name=name, reason=str(error))
        if protected:
            if rights is None:
                _error("license_rights_unreviewed", "A connected model/dataset/checkpoint input has no reviewed rights lineage", input_name=name)
            result[name] = rights
    return result


def verify_dispatch(spec, inputs=None):
    """Recheck source/declarations and current policy revisions before execution."""
    node = spec["component_node"]
    supplied = dict(inputs or {})
    if spec.get("resume_path"):
        supplied["__resume_checkpoint__"] = Path(spec["resume_path"])
    envelopes = _input_envelopes(supplied, node)
    declared = {"source": spec.get("source", {}), "resources": spec.get("dependencies", []),
                "external_dependencies": [], "extensions": spec.get("rights_extensions", {})}
    authority = spec.get("rights_authorization")
    if authority is None:
        if envelopes or has_declared_assets(declared):
            _error("license_rights_unreviewed", "Native execution has no service-authorized rights receipt for its declared inputs")
        # Historical source-only executions are not upgraded into legal approval.
        node_action(node)
        return {"protocol": PROTOCOL, "scope": "no_declared_external_asset_inputs", "node_id": node.get("id", spec.get("node_id")),
                "permissions_inferred_from_label": False}
    receipt, policies, guard = authority.get("receipt", {}), authority.get("policies", {}), authority.get("policy_store_guard")
    if (receipt.get("protocol") != PROTOCOL or receipt.get("digest") != _digest({key: value for key, value in receipt.items() if key != "digest"})
            or receipt.get("policy_snapshot_digest") != _digest(policies)
            or receipt.get("policy_store_identity") != _digest(guard)):
        _error("license_authority_unavailable", "Native rights receipt or policy snapshot was altered")
    if spec.get("project_id") is not None and spec["project_id"] != receipt.get("project_id"):
        _error("license_authority_unavailable", "Native rights authority belongs to another project")
    saved = receipt["declarations"]
    if (saved["source"] != declared["source"]
            or saved["resources"] + saved["external_dependencies"] != declared["resources"]
            or saved["extensions"] != declared["extensions"]
            or receipt.get("nodes", {}).get(node["id"], {}).get("node") != node):
        _error("license_policy_changed", "Native source, requested action or resource declarations differ from the reviewed receipt")
    actions = receipt["nodes"][node["id"]]["actions"]
    if envelopes and not actions:
        actions = node_action(node, required=True)
    governed = has_declared_assets(saved)
    decisions = [_validate_declared(saved, action, policies) for action in actions] if governed else []
    for envelope in envelopes.values():
        for action in actions:
            decisions.append(_validate_envelope(envelope, action, policies, saved["extensions"].get("usage_context")))
    used = {identifier for decision in decisions for identifier in decision["policy_ids"]}
    _current_policies(receipt["project_id"], policies, guard, used)
    _current_snapshot(receipt.get("source_snapshot_guard"), policies)
    result = {"protocol": PROTOCOL, "node_id": node["id"], "requested_actions": actions,
              "scope": "declared_asset_inputs" if governed or envelopes else "no_declared_external_asset_inputs",
              "source_digest": _digest(saved["source"]), "receipt_digest": receipt["digest"],
              "policy_revisions": {key: {"revision": policies[key]["revision"], "review_digest": policies[key]["review_digest"]}
                                   for key in sorted(used)},
              "input_rights_digests": {name: _digest(value) for name, value in envelopes.items()},
              "usage_context": saved["extensions"].get("usage_context"), "permissions_inferred_from_label": False}
    result["digest"] = _digest(result)
    return result


def recheck_authorization(authorization, *, project_id=None):
    """Recheck a service-created execution receipt immediately before launch."""
    if not isinstance(authorization, dict):
        _error("license_authority_unavailable", "An internal service rights receipt is required")
    receipt = authorization.get("receipt", {})
    policies = authorization.get("policies", {})
    guard = authorization.get("policy_store_guard")
    if (receipt.get("protocol") != PROTOCOL
            or receipt.get("digest") != _digest({key: value for key, value in receipt.items() if key != "digest"})
            or receipt.get("policy_snapshot_digest") != _digest(policies)
            or receipt.get("policy_store_identity") != _digest(guard)
            or project_id is not None and project_id != receipt.get("project_id")):
        _error("license_authority_unavailable", "Native rights receipt or policy snapshot was altered")
    used = {identifier for node in receipt.get("nodes", {}).values() for decision in node["admission"]
            for identifier in decision["policy_ids"]}
    _current_policies(receipt["project_id"], policies, authorization.get("policy_store_guard"), used)
    _current_snapshot(receipt.get("source_snapshot_guard"), policies)


def artifact_authorization(backend, project_id, usage_context):
    """Internal adapter-to-SDK authority for a selected promotion/export action.

    The adapter must reject caller-supplied _execution_rights_authorization before
    adding this receipt to its private native request. Metadata inspection needs
    no receipt; promotion/export of an actual artifact does.
    """
    authority = getattr(backend, "_execution_rights_authority", None)
    if not isinstance(authority, Authority) or authority.project_id != project_id:
        _error("license_authority_unavailable", "Artifact use requires current project-scoped service rights authority")
    receipt = {"protocol": PROTOCOL, "project_id": project_id,
               "policies": json.loads(authority.policies_json),
               "policy_store_guard": json.loads(authority.store_guard_json),
               "usage_context": usage_context}
    receipt["digest"] = _digest(receipt)
    return json.loads(_json(receipt))


def verify_artifact_authorization(receipt, envelope, intended_use):
    """Native SDK pre-use check; receipt only comes from the internal adapter."""
    from ..hub_rights import validate_artifact_rights
    from ..models import ServiceError
    if (not isinstance(receipt, dict) or receipt.get("protocol") != PROTOCOL
            or receipt.get("digest") != _digest({key: value for key, value in receipt.items() if key != "digest"})):
        _error("license_authority_unavailable", "Artifact use requires an intact internal service rights receipt")
    policies, usage_context = receipt.get("policies", {}), receipt.get("usage_context")
    if any(policy.get("project_id") != receipt.get("project_id") for policy in policies.values()):
        _error("license_authority_unavailable", "Artifact policy authority belongs to another project")
    try:
        admission = validate_artifact_rights(envelope, intended_use, policies, usage_context=usage_context)
    except ServiceError as error:
        _error(error.code, error.message, **error.details)
    _current_policies(receipt["project_id"], policies, receipt.get("policy_store_guard"), admission["policy_ids"])
    return {"policies": policies, "usage_context": usage_context, "admission": admission}
