"""Native scientific gate attestations for source-pinned extensible methods.

The verifier checks execution identity and evidence, not the scientific honesty
of reviewed project code. It never accepts an operation payload's passed flag.
"""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path

from .components import identity
from .native_inputs import storage_identity, validate_native_type
from ..evaluation import digest, evaluation_for, compare_records
from ..scientific import validate_policy


def evidence_from_inputs(policy, inputs):
    return {rule["id"]: {side: {"record": deepcopy(evaluation_for(inputs.get(side, {}), rule["metric"]) or {})}
                        for side in ("baseline", "candidate")} for rule in policy["rules"]}


def validate_result(result, policy, evidence):
    """Typed outcomes, exact policy/evidence/methods, and self-consistent digest."""
    validate_policy(policy)
    if (not isinstance(result, dict) or result.get("schema") != "modelbuilder.scientific-result/v1"
            or result.get("policy") != policy or result.get("policy_digest") != digest(policy)
            or result.get("digest") != digest({k: v for k, v in result.items() if k != "digest"})):
        raise ValueError("Native scientific result differs from its policy or content digest")
    expected_multiplicity = {"method": "bonferroni", "family_error_rate": policy["family_error_rate"],
                             "rule_error_rate": policy["family_error_rate"] / len(policy["rules"]), "family_power_claim": False}
    if result.get("multiple_testing") != expected_multiplicity or type(result.get("multiple_testing", {}).get("family_power_claim")) is not bool:
        raise ValueError("Scientific multiplicity receipt differs from the declared policy")
    rows = result.get("rules")
    if not isinstance(rows, list) or len(rows) != len(policy["rules"]):
        raise ValueError("Scientific result must report every declared rule")
    for rule, row in zip(policy["rules"], rows):
        if not isinstance(row, dict):
            raise ValueError("Scientific rule result must be an object")
        if "significance_claim" in row and type(row["significance_claim"]) is not bool:
            raise ValueError("Scientific significance claim must be a typed boolean")
        for key in ("sample_adequacy", "uncertainty"):
            if row.get(key) is not None and not isinstance(row[key], dict):
                raise ValueError("Scientific adequacy and uncertainty must be structured objects or null")
        expected = {"rule_id": rule["id"], "metric": rule["metric"], "direction": rule["direction"],
                    "method": rule["method"], "threshold": rule["minimum_improvement"], "evidence": evidence[rule["id"]]}
        if any(row.get(k) != v for k, v in expected.items()) or row.get("outcome") not in {"passed", "failed", "unknown"}:
            raise ValueError("Scientific rule result differs from its declared method/evidence/threshold")
        pair = evidence[rule["id"]]
        records = [{"backend_run_id": side, "model_version_ref": side, "status": "succeeded", "metrics": {rule["metric"]: pair[side]["record"].get("value"), "evaluation_record": pair[side]["record"]}}
                   for side in ("baseline", "candidate")]
        comparison = compare_records(records, rule["metric"], rule["direction"], {"allowed_variations": policy["allowed_variations"]})
        if not comparison["compatible"] and row["outcome"] != "unknown":
            raise ValueError("Scientific result cannot accept incompatible or missing evaluation evidence")
        if row.get("alpha") != policy["family_error_rate"] / len(policy["rules"]):
            raise ValueError("Scientific rule error rate differs from declared multiplicity policy")
        if row.get("cohort_digest") != digest(evidence[rule["id"]]["baseline"]["record"].get("split")):
            raise ValueError("Scientific cohort identity differs")
        if row.get("significance_claim") is True:
            uncertainty = row.get("uncertainty")
            if not isinstance(uncertainty, dict) or not isinstance(rule["method"]["parameters"].get("assumptions"), dict):
                raise ValueError("Significance claims require explicit method assumptions and uncertainty")
    outcome = "failed" if any(r["outcome"] == "failed" for r in rows) else "unknown" if any(r["outcome"] == "unknown" for r in rows) else "passed"
    if result.get("outcome") != outcome or type(result.get("passed")) is not bool or result["passed"] != (outcome == "passed"):
        raise ValueError("Scientific aggregate outcome contradicts its individual rules")


def input_receipts(step, root):
    receipts = []
    for port in ("baseline", "candidate"):
        artifacts = step.inputs.get(port, [])
        if len(artifacts) != 1:
            raise ValueError("Scientific gate requires one exact native evaluation artifact per side")
        artifact = artifacts[0]
        validate_native_type(artifact, "dict")
        if not artifact.producer_pipeline_run_id:
            raise ValueError("Scientific gate input has no native producer run")
        receipts.append({"input_port": port, "artifact_id": str(artifact.id), "backend_project_id": str(artifact.project.id),
                         "backend_run_id": str(artifact.producer_pipeline_run_id), "content_sha256": storage_identity(artifact, root)})
    return receipts


def native_context(spec):
    from zenml import get_step_context
    ctx = get_step_context()
    producer = {"project_id": spec["recipe_provenance"]["application_project_id"], "backend_project_id": str(ctx.pipeline_run.project.id),
                "backend_run_id": str(ctx.pipeline_run.id), "node_id": spec["node_id"],
                "invocation_id": spec.get("invocation", {}).get("invocation_id", spec["node_id"])}
    return {"producer": producer, "inputs": input_receipts(ctx.step_run, Path(spec["job_dir"]).parent.parent)}


def seal_results(results, node, native, inputs, source):
    if set(results) != {"passed", "result"} or type(results["passed"]) is not bool:
        raise ValueError("Scientific gate must emit exactly typed passed and result outputs")
    policy = node["parameters"]["policy"]
    evaluator = {"component_digest": identity(node["component"]), "implementation_ref": node["implementation_ref"]}
    if policy.get("evaluator") != evaluator:
        raise ValueError("Native gate policy must pin this exact reviewed component and source entrypoint")
    result = results["result"]
    validate_result(result, policy, evidence_from_inputs(policy, inputs))
    if results["passed"] != result["passed"]:
        raise ValueError("Native gate bool differs from scientific report")
    sealed = deepcopy(result)
    sealed["native_execution"] = {**deepcopy(native), "evaluator": evaluator, "source_digest": identity(source)}
    if any(r["backend_project_id"] != native["producer"]["backend_project_id"] for r in native["inputs"]):
        raise ValueError("Scientific gate input belongs to another native project")
    sealed["digest"] = digest({k: v for k, v in sealed.items() if k != "digest"})
    return {"passed": results["passed"], "result": sealed}


def load_gate(policy, selector, root, project_id):
    """Load a completed actual native gate with current bytes and immutable pins."""
    from .common import read_json, recipe_provenance
    from .sdk import native_run
    from .native_invocations import output_step
    matches = [(job, read_json(job / "request.json")) for job in (root / "jobs").glob("*")
               if read_json(job / "request.json").get("project_id") == project_id
               and read_json(job / "native.json").get("backend_run_id") == selector["backend_run_id"]]
    if len(matches) != 1:
        raise ValueError("Scientific gate run is unknown or belongs to another project")
    job, request = matches[0]
    _, run = native_run(job)
    if not run or run.status.value != "completed" or request.get("trusted") is not True:
        raise ValueError("Scientific gate requires a completed explicitly trusted native execution")
    extra = run.config.extra
    if extra.get("application_project_id") != project_id or any(extra.get(k) != v for k, v in recipe_provenance(request["recipe"]).items()):
        raise ValueError("Scientific gate native recipe/project provenance differs")
    node = next((n for n in request["recipe"]["nodes"] if n["id"] == selector["node_id"]), None)
    if not node or node.get("component", {}).get("role") != "scientific_gate" or selector["output_port"] != "result":
        raise ValueError("Scientific gate selector is not a declared result output")
    info, step = output_step(run, request["recipe"], node["id"], "result", selector["artifact_id"])
    if getattr(step.status, "value", None) != "completed":
        raise ValueError("Scientific gate output is cached or incomplete")
    artifact = next(a for a in step.outputs["result"] if str(a.id) == selector["artifact_id"])
    validate_native_type(artifact, "dict")
    if str(artifact.project.id) != str(run.project.id) or storage_identity(artifact, root) != selector["content_sha256"]:
        raise ValueError("Scientific gate output project/hash changed")
    result = artifact.load()
    evaluator = {"component_digest": identity(node["component"]), "implementation_ref": node["implementation_ref"]}
    if node["parameters"].get("policy") != policy or policy.get("evaluator") != evaluator:
        raise ValueError("Scientific gate uses a different policy, component or source pin")
    expected = {"producer": {"project_id": project_id, "backend_project_id": str(run.project.id), "backend_run_id": str(run.id),
                            "node_id": node["id"], "invocation_id": info["invocation_id"]},
                "inputs": input_receipts(step, root), "evaluator": evaluator, "source_digest": identity(request["recipe"]["source"])}
    if result.get("native_execution") != expected:
        raise ValueError("Scientific gate execution or native inputs changed")
    validate_result(result, policy, {row["rule_id"]: row["evidence"] for row in result["rules"]})
    bool_outputs = step.outputs.get("passed", [])
    if len(bool_outputs) != 1:
        raise ValueError("Scientific gate has no unique typed bool output")
    validate_native_type(bool_outputs[0], "bool")
    if str(bool_outputs[0].project.id) != str(run.project.id) or bool_outputs[0].load() is not result["passed"]:
        raise ValueError("Native scientific gate bool disagrees with its verified result")
    if storage_identity(artifact, root) != selector["content_sha256"]:
        raise ValueError("Scientific gate changed during verification")
    return result
