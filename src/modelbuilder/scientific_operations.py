"""Shared HTTP/MCP operation contracts for versioned scientific policies."""
from __future__ import annotations

from .evaluation import digest
from .scientific import POLICY_SCHEMA, KNOWN_SD, validate_policy, known_sd_adequacy, method_catalog
from .execution import EVALUATION_SELECTOR_SCHEMA

SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
GATE_SELECTOR = {"type": "object", "additionalProperties": False, "properties": {**{key: {"type": "string", "minLength": 1} for key in ("backend_run_id", "node_id", "output_port", "artifact_id")}, "content_sha256": SHA}, "required": ["backend_run_id", "node_id", "output_port", "artifact_id", "content_sha256"]}
BINDINGS = {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "object", "additionalProperties": False,
    "properties": {"rule_id": {"type": "string", "minLength": 1}, "baseline": EVALUATION_SELECTOR_SCHEMA, "candidate": EVALUATION_SELECTOR_SCHEMA},
    "required": ["rule_id", "baseline", "candidate"]}}


def operation_specs(spec):
    p = {"project_id": {"type": "string", "minLength": 1}}
    return [spec("scientific.methods", "read", "List verified built-in scientific methods; custom methods run in reviewed source-bound components.", p, ("project_id",)),
        spec("scientific.policy.register", "modify", "Register an immutable, content-addressed scientific policy; registration does not activate it.", {**p, "policy": POLICY_SCHEMA}, ("project_id", "policy"), True),
        spec("scientific.policy.get", "read", "Read a pinned scientific policy.", {**p, "policy_digest": SHA}, ("project_id", "policy_digest")),
        spec("scientific.policy.activate", "modify", "Require this immutable scientific policy for accepted decisions and model promotion.", {**p, "policy_digest": SHA, "expected_revision": {"type": "integer", "minimum": 1}}, ("project_id", "policy_digest", "expected_revision"), True),
        spec("scientific.sample.plan", "read", "Calculate prospective paired-normal known-SD sample adequacy from explicit effect, power, alpha and assumptions; never infer scientific assumptions.",
             {**p, "method": {"const": "normal-known-sd-one-sided/v1"}, "parameters": KNOWN_SD,
              "alpha": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
              "available_pairs": {"type": "integer", "minimum": 0, "maximum": 10**9}}, ("project_id", "method", "parameters", "alpha", "available_pairs"))]


def get_policy(service, project_id, policy_digest):
    body = service._get("scientific_policy", project_id + ":" + policy_digest, project_id)
    if body["policy_digest"] != policy_digest or digest(body["policy"]) != policy_digest:
        raise ValueError("Stored scientific policy digest differs")
    validate_policy(body["policy"])
    return body["policy"]


def dispatch(service, operation, payload):
    pid = payload["project_id"]
    if operation == "scientific.methods":
        return {"methods": method_catalog(), "extension_contract": "Trusted source-bound component supplies Method implementation, version and parameter schema; no imports from operation payloads."}
    if operation == "scientific.sample.plan":
        return known_sd_adequacy(payload["parameters"], payload["alpha"], payload["available_pairs"])
    if operation == "scientific.policy.register":
        validate_policy(payload["policy"])
        fingerprint = digest(payload["policy"])
        key = pid + ":" + fingerprint
        existing = [item for item in service.store.list("scientific_policy", pid) if item["policy_digest"] == fingerprint]
        if existing:
            return existing[0]
        return service.store.put("scientific_policy", key, pid, {"project_id": pid, "policy_digest": fingerprint, "policy": payload["policy"], "revision": 1})
    policy = get_policy(service, pid, payload["policy_digest"])
    if operation == "scientific.policy.get":
        return {"project_id": pid, "policy_digest": payload["policy_digest"], "policy": policy}
    if operation == "scientific.policy.activate":
        project = service._get("project", pid, pid)
        service._revision(project, payload["expected_revision"])
        return service.store.put("project", pid, pid, {**project, "scientific_policy_digest": payload["policy_digest"], "revision": project["revision"] + 1}, project["revision"])
    raise ValueError("Unknown scientific operation")


def enrich_comparison(service, payload):
    result = dict(payload)
    if payload.get("scientific_policy_digest"):
        result["scientific_policy"] = get_policy(service, payload["project_id"], payload["scientific_policy_digest"])
        if result["scientific_policy"].get("evaluator") and not payload.get("scientific_gate"):
            raise ValueError("This policy requires its completed source-pinned native scientific gate artifact")
        if payload.get("scientific_gate"):
            service._find_operation(payload["project_id"], backend_run_id=payload["scientific_gate"]["backend_run_id"])
        if not payload.get("scientific_evaluations"):
            raise ValueError("Scientific comparison requires exact native evaluation selectors for each rule")
    elif payload.get("scientific_evaluations") or payload.get("scientific_gate"):
        raise ValueError("Scientific evaluation selectors require a pinned policy digest")
    return result


def require_decision_policy(service, project_id, rule, outcome):
    """An accepted free-text decision cannot bypass a currently required gate."""
    from .models import ServiceError
    project = service._get("project", project_id, project_id)
    required = project.get("scientific_policy_digest")
    if outcome == "accepted" and required and (rule.get("scientific_policy_digest") != required or not rule.get("scientific_result_digest") or not rule.get("comparison_digest")):
        raise ServiceError("scientific_gate_required", "Accepted decisions require the active policy's passing native evaluation result and comparison digest")
