"""Actual native ZenML parent/output IDs, blended checkpoint and evaluation."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
pytest.importorskip("zenml")
pytest.importorskip("torch")
from modelbuilder.backend.common import write_json
from modelbuilder.backend.components import component_catalog, read_checkpoint
from tests.integration.native_fixture_support import synthetic_review, register_project_review


def source_recipe(tmp_path, project_id="pending"):
    source = tmp_path / "source"
    source.mkdir()
    shutil.copyfile(Path(__file__).with_name("tiny_harness.py"), source / "tiny_harness.py")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=Acceptance", "-c", "user.email=test@example.invalid", "commit", "-qm", "Independent scientific fixture"]):
        subprocess.run(["git", "-C", str(source), *args], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    sha = hashlib.sha256((source / "tiny_harness.py").read_bytes()).hexdigest()
    ref = {"path": "tiny_harness.py", "sha256": sha, "module": "tiny_harness", "commit": commit}
    def node(name, role, parameters, inputs, entrypoint):
        return {"id": name, "kind": "component.run", "implementation_ref": {**ref, "entrypoint": entrypoint},
            "inputs": inputs, "outputs": [{"name": "metrics", "type": "dict"}] if role == "evaluation" else [{"name": "checkpoint", "type": "Path"}],
            "parameters": parameters, "timeout": 90.0, "retry": {"max_attempts": 1, "backoff_seconds": 0}, "environment_ref": "default", "resource_request": {"gpu": 0}, "cache_policy": "disabled", "provenance": {"intended_use": "evaluate" if role == "evaluation" else "train"},
            "component": {"id": "tiny." + role, "version": "1", "protocol": "modelbuilder.component/v1", "role": role, "parameter_schema": {"type": "object"}, "capabilities": {"resume_modes": [], "export_formats": [], "frameworks": ["pytorch"]}}}
    nodes = [node("left", "training", {"architecture": {"task": "tiny"}, "steps": 2, "target": 1.}, [], "train"),
             node("right", "training", {"architecture": {"task": "tiny"}, "steps": 2, "target": 3.}, [], "train"),
             node("blend", "derivation", {"alpha": .5, "max_asset_bytes": 10000, "max_tensor_elements": 100}, [{"name": n, "type": "Path"} for n in ("left", "right")], "blend"),
             node("evaluate", "evaluation", {}, [{"name": "checkpoint", "type": "Path"}], "evaluate")]
    entry = next(e for e in component_catalog() if e["id"] == "modelbuilder.checkpoint.blend")
    nodes[2]["component"] = {k: entry[k] for k in ("id", "version", "protocol", "role", "description", "parameter_schema", "capabilities")}
    recipe = {"schema_version": "0", "project_id": "tiny", "name": "Tiny derivation", "recipe_id": "tiny", "revision": 1,
        "source": {"commit": commit, "repository": "urn:sha256:" + sha, "files": [{"path": "tiny_harness.py", "sha256": sha}]}, "nodes": nodes,
        "edges": [{"source_node": n, "source_port": "checkpoint", "target_node": "blend", "target_port": n} for n in ("left", "right")] + [{"source_node": "blend", "source_port": "checkpoint", "target_node": "evaluate", "target_port": "checkpoint"}],
        "resources": [], "external_dependencies": [], "extensions": {}, "environment": {"python": "3.12", "requirements": ["zenml==0.97.0"]}, "evaluation": {}, "outputs": []}
    review = synthetic_review(recipe, project_id, source)
    return source, recipe, review


@pytest.mark.integration
def test_actual_native_blend_and_evaluation_owner(tmp_path):
    source, recipe, review = source_recipe(tmp_path, "tiny")
    root = tmp_path / "runtime"
    job = root / "jobs" / "derive"
    from tests.components.rights_test_support import authorize_test_recipe
    authorization = authorize_test_recipe(tmp_path, recipe, {review["policy_id"]: review}, project_id="tiny")
    write_json(job / "request.json", {**authorization, "recipe": recipe, "root": str(root), "project_id": "tiny", "operation_id": "derive", "run_name": "derive", "source_root": str(source), "trusted_code_roots": [str(source)]})
    env = {**os.environ, "ZENML_CONFIG_PATH": str(root / "zenml-config"), "ZENML_LOCAL_STORES_PATH": str(root / "zenml-stores"), "ZENML_ANALYTICS_OPT_IN": "false", "ZENML_LOGGING_VERBOSITY": "ERROR", "OMP_NUM_THREADS": "1"}
    completed = subprocess.run([sys.executable, "-m", "modelbuilder.backend.worker", str(job)], env=env, capture_output=True, text=True, timeout=180)
    (tmp_path / "native.log").write_text(completed.stdout + completed.stderr)
    assert completed.returncode == 0, (completed.stdout + completed.stderr)[-10000:]
    ck = read_checkpoint(job / "outputs" / "blend")
    native = json.loads((job / "native.json").read_text())
    assert ck["derivation"]["producer"]["model_version_ref"] == native["model_version_ref"]
    assert len({p["artifact_id"] for p in ck["derivation"]["parents"]}) == 2
    script = '''import json,sys
from pathlib import Path
from modelbuilder.backend.sdk import native_run, evaluation_outputs
from modelbuilder.backend.common import read_json
job=Path(sys.argv[1]); _, run=native_run(job)
rows=evaluation_outputs(run, read_json(job/'request.json'))
assert len(rows)==1, rows
assert rows[0]['checkpoint_verification']['verified'], rows
assert rows[0]['model_version_ref']==str(run.model_version.id), rows
print(json.dumps(rows))
'''
    verification_script = tmp_path / "verify.py"
    verification_script.write_text(script)
    checked = subprocess.run([sys.executable, str(verification_script), str(job)], env=env, capture_output=True, text=True, timeout=60)
    (tmp_path / "verification.log").write_text(checked.stdout + checked.stderr)
    assert checked.returncode == 0, (checked.stdout + checked.stderr)[-10000:]


@pytest.mark.integration
@pytest.mark.parametrize("smoke_parent,custom_method", [(False, False), (True, False), (False, True)])
def test_cross_run_derived_comparison_policy_promotion_and_integrity(tmp_path, smoke_parent, custom_method):
    import time
    from copy import deepcopy
    from modelbuilder.backend import ZenMLBackend
    from modelbuilder.models import Actor, ServiceError
    from modelbuilder.service import OperationService
    from .test_policy import policy
    source, recipe, review = source_recipe(tmp_path)
    baseline_eval = deepcopy(recipe["nodes"][-1])
    baseline_eval["id"] = "baseline_eval"
    recipe["nodes"].append(baseline_eval)
    recipe["edges"].append({"source_node": "left", "source_port": "checkpoint", "target_node": "baseline_eval", "target_port": "checkpoint"})
    recipe["nodes"][0]["component"]["capabilities"]["smoke_budget"] = {"schema": "modelbuilder.smoke-budget/v1", "limits": [
        {"id": "updates", "unit": "optimizer_steps", "parameter_path": ["steps"], "semantics": "absolute_integer_cap", "minimum": 1}]}
    from modelbuilder.backend.components import identity
    p = policy()
    p["rules"] = [p["rules"][0]]
    if custom_method:
        gate_entry = next(e for e in component_catalog() if e["id"] == "modelbuilder.scientific.gate")
        component = {k: gate_entry[k] for k in ("id", "version", "protocol", "role", "description", "parameter_schema", "capabilities")}
        component["id"] = "independent.scientific.gate"
        implementation = {**recipe["nodes"][0]["implementation_ref"], "entrypoint": "custom_gate"}
        p["rules"][0]["method"] = {"id": "independent.paired_median", "version": "1", "implementation": {"source_sha256": implementation["sha256"], "entrypoint": "custom_gate.independent_rule"},
                                 "parameters": {"margin": .1, "assumptions": {"paired_descriptive_only": True}}}
        p["evaluator"] = {"component_digest": identity(component), "implementation_ref": implementation}
        gate_node = {**deepcopy(baseline_eval), "id": "gate", "component": component, "implementation_ref": implementation,
                     "parameters": {"policy": p}, "provenance": {"intended_use": "evaluate"}, "inputs": gate_entry["inputs"], "outputs": gate_entry["outputs"]}
        recipe["nodes"].append(gate_node)
        recipe["edges"].extend([{"source_node": source_node, "source_port": "metrics", "target_node": "gate", "target_port": port}
                                for source_node, port in (("baseline_eval", "baseline"), ("evaluate", "candidate"))])
    backend = ZenMLBackend(tmp_path / "state", python_executable=sys.executable, source_root=source, trusted_code_roots=[str(source)])
    service = OperationService(tmp_path / "service.sqlite", backend=backend, source_root=source)
    actor = Actor("owner", {"read", "modify", "execute"})
    project = service.invoke("project.create", {"slug": "tiny-scientific", "model_name": "Tiny", "recipe": recipe, "idempotency_key": "create"}, actor)
    review = register_project_review(service, review, project["project_id"])
    base = {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}
    if smoke_parent:
        prepared = service.invoke("recipe.smoke.prepare", {**base, "expected_revision": 1, "limits": [{"node_id": "left", "limit_id": "updates", "unit": "optimizer_steps", "maximum": 1}], "idempotency_key": "smoke"}, actor)
        assert prepared["recipe"]["revision"] == 2
    def run(name, nodes, bindings=()):
        revision = service.invoke("recipe.get", base, actor)["revision"]
        submitted = service.invoke("run.submit", {**base, "expected_revision": revision, "trusted": True, "execution": {"node_ids": nodes, "input_bindings": list(bindings)}, "idempotency_key": name}, actor)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = service.invoke("run.status", {"project_id": base["project_id"], "operation_id": submitted["operation_id"]}, actor)
            if status["status"] in {"succeeded", "failed", "cancelled"} and not status.get("worker_alive"):
                assert status["status"] == "succeeded", status
                return status
            time.sleep(.3)
        raise AssertionError("Native tiny run did not finish")
    left = run("left", ["left", "baseline_eval"])
    if smoke_parent:
        saved = service.invoke("recipe.get", base, actor)
        production = deepcopy(saved)
        production.pop("execution", None)
        service.invoke("recipe.update", {**base, "expected_revision": saved["revision"], "recipe": production, "idempotency_key": "return-production"}, actor)
    right = run("right", ["right"])
    def binding(status, node, port):
        result = service.invoke("run.artifacts", {"project_id": base["project_id"], "backend_run_id": status["backend_run_id"], "include_binding_descriptors": True}, actor)
        rows = [r["binding_descriptor"] for r in result["artifacts"] if "binding_descriptor" in r]
        # Exact native binding descriptors are returned separately by the SDK.
        item = next(r for r in rows if r.get("source_node_id") == node and r.get("source_port") == "checkpoint")
        return {"node_id": "blend", "input_port": port, **{k: item[k] for k in ("backend_run_id", "source_node_id", "source_port", "artifact_id", "content_sha256")}}
    bindings = [binding(left, "left", "left"), binding(right, "right", "right")]
    derived = run("derived", ["blend", "evaluate"], bindings)
    assert derived["model_version_ref"] not in {left["model_version_ref"], right["model_version_ref"]}
    checkpoint = service.invoke("model.checkpoint.get", {"project_id": base["project_id"], "backend_run_id": derived["backend_run_id"], "checkpoint_kind": "inference"}, actor)
    assert checkpoint["resume_modes"] == [] and checkpoint["descriptor"]["producer_kind"] == "derivation"
    lineage = service.invoke("model.version.lineage", {"project_id": base["project_id"], "model_version_ref": derived["model_version_ref"]}, actor)
    assert lineage["checkpoint_derivations"][0]["production_eligible"] is (not smoke_parent)
    assert {p["artifact_id"] for p in lineage["checkpoint_derivations"][0]["derivation"]["parents"]} == {b["artifact_id"] for b in bindings}
    registered = service.invoke("scientific.policy.register", {"project_id": base["project_id"], "policy": p, "idempotency_key": "policy"}, actor)
    project = service.invoke("scientific.policy.activate", {"project_id": base["project_id"], "policy_digest": registered["policy_digest"], "expected_revision": project["revision"], "idempotency_key": "activate"}, actor)
    selectors = [{k: row["evaluations"][0][k] for k in ("backend_run_id", "node_id", "output_port", "artifact_id", "metric")} for row in (left, derived)]
    comparisons = {"project_id": base["project_id"], "run_ids": [left["backend_run_id"], derived["backend_run_id"]], "metric": "loss", "direction": "minimize", "evaluations": selectors,
        "scientific_policy_digest": registered["policy_digest"], "scientific_evaluations": [{"rule_id": "loss", "baseline": selectors[0], "candidate": selectors[1]}]}
    if custom_method:
        with pytest.raises(ServiceError, match="requires its completed"):
            service.invoke("run.compare", comparisons, actor)
        evaluation_bindings = []
        for status, port in ((left, "baseline"), (derived, "candidate")):
            values = service.invoke("run.artifacts", {"project_id": base["project_id"], "backend_run_id": status["backend_run_id"], "include_binding_descriptors": True}, actor)
            item = next(a["binding_descriptor"] for a in values["artifacts"] if a["port"] == "metrics")
            evaluation_bindings.append({"node_id": "gate", "input_port": port, **{k: item[k] for k in ("backend_run_id", "source_node_id", "source_port", "artifact_id", "content_sha256")}})
        gate_run = run("custom-gate", ["gate"], evaluation_bindings)
        values = service.invoke("run.artifacts", {"project_id": base["project_id"], "backend_run_id": gate_run["backend_run_id"], "include_binding_descriptors": True}, actor)
        gate_artifact = next(a for a in values["artifacts"] if a["port"] == "result")
        descriptor = gate_artifact["binding_descriptor"]
        comparisons["scientific_gate"] = {"backend_run_id": gate_run["backend_run_id"], "node_id": "gate", "output_port": "result", "artifact_id": gate_artifact["artifact_id"], "content_sha256": descriptor["content_sha256"]}
    comparison = service.invoke("run.compare", comparisons, actor)
    assert comparison["compatible"] and comparison["scientific_result"]["passed"], comparison
    assert comparison["selected_model_version_ref"] == derived["model_version_ref"]
    hypothesis = service.invoke("hypothesis.create", {"project_id": base["project_id"], "proposed_change": "Blend two checkpoints", "expected_outcome": "Lower heldout error", "acceptance_metric": {"name": "loss"}, "idempotency_key": "hypothesis"}, actor)
    current = service.invoke("recipe.get", base, actor)
    rule = {k: comparisons[k] for k in ("metric", "direction", "evaluations", "scientific_policy_digest", "scientific_evaluations")}
    if custom_method:
        rule["scientific_gate"] = comparisons["scientific_gate"]
        assert comparison["scientific_result"]["native_result_digest"]
    rule.update(comparison_digest=comparison["comparison_digest"], scientific_result_digest=comparison["scientific_result"]["digest"])
    decision = service.invoke("decision.record", {**base, "recipe_revision": current["revision"], "hypothesis_id": hypothesis["hypothesis_id"], "run_ids": comparisons["run_ids"], "selection_rule": rule, "outcome": "accepted", "rationale": "Passing native policy result", "idempotency_key": "decision"}, actor)
    promotion = {"project_id": base["project_id"], "model_version_ref": derived["model_version_ref"], "decision_id": decision["decision_id"], "expected_revision": project["revision"], "stage": "staging", "idempotency_key": "promote"}
    if smoke_parent:
        with pytest.raises(ServiceError) as error:
            service.invoke("model.version.promote", promotion, actor)
        assert error.value.code == "smoke_promotion_denied", error.value.as_dict()
    else:
        promoted = service.invoke("model.version.promote", promotion, actor)
        assert promoted["model_version_ref"] == derived["model_version_ref"], promoted
    if custom_method:
        forged = deepcopy(comparisons)
        forged["scientific_gate"]["content_sha256"] = "0" * 64
        with pytest.raises(ServiceError):
            service.invoke("run.compare", forged, actor)
    # Mutating an ancestor after derivation must invalidate comparisons as well.
    artifact = next(r for r in left["artifacts"] if r["node_id"] == "left")
    with (Path(artifact["uri"]) / "data.tar.gz").open("ab") as stream:
        stream.write(b"changed-after-derivation")
    try:
        changed = service.invoke("run.compare", comparisons, actor)
    except ServiceError as error:
        assert error.code in {"evaluation_checkpoint_unverified", "backend_error"}
    else:
        assert not changed["compatible"]
    service.store.close()
