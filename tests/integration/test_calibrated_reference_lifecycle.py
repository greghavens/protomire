"""Native selected frozen reference -> resume -> calibrated reference -> export.

All tensors are independently generated. No Biff/source copy, model API, external
weights, hosted data, or provider calls are used. Raw proof stays in pytest temp.
"""
import hashlib
import base64
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import zipfile

import pytest

pytest.importorskip("torch")
pytest.importorskip("zenml")
import torch
from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.admission import BackendError
from modelbuilder.backend.components import identity, read_checkpoint, read_export
from modelbuilder.backend.common import read_json
from modelbuilder.evaluation import compare_records
from modelbuilder.models import Actor
from modelbuilder.service import OperationService

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
OWNER = Actor("independent-calibration-owner", {"read", "modify", "execute"})


def binding(artifact, node, port):
    return {"node_id": node, "input_port": port, **{key: artifact["binding_descriptor"][key] for key in
        ("backend_run_id", "source_node_id", "source_port", "artifact_id", "content_sha256")}}


def equal(left, right):
    if isinstance(left, torch.Tensor):
        return torch.equal(left, right)
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(equal(left[key], right[key]) for key in left)
    if isinstance(left, (tuple, list)):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    return left == right


def independent_kl(policy, reference, direction):
    def probabilities(row):
        values = [math.exp(value - max(row)) for value in row]
        return [value / sum(values) for value in values]
    rows = []
    for p, r in zip(policy, reference):
        p, r = probabilities(p), probabilities(r)
        if direction == "reference_to_policy":
            p, r = r, p
        rows.append(sum(a * math.log(a / b) for a, b in zip(p, r)))
    return sum(rows) / len(rows)


def authored_recipe(source):
    source.mkdir()
    for original, target in (("calibrated_reference_harness.py", "harness.py"), ("calibrated_reference_loader.py", "portable_loader.py")):
        shutil.copy2(ROOT / "tests/components" / original, source / target)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Acceptance", "-c", "user.email=acceptance@example.invalid", "commit", "-qm", "Independent calibrated reference model"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    files = [{"path": name, "sha256": hashlib.sha256((source / name).read_bytes()).hexdigest()} for name in ("harness.py", "portable_loader.py")]
    reference = {"module": "authored_calibrated_reference", "path": "harness.py", "commit": commit, "sha256": files[0]["sha256"]}
    nodes = []
    def node(name, entrypoint, role, inputs, output, parameters):
        nodes.append({"id": name, "kind": "component.run", "implementation_ref": {**reference, "entrypoint": entrypoint},
            "inputs": [{"name": port, "type": "Path"} for port in inputs],
            "outputs": [{"name": output, "type": "dict" if role == "evaluation" else "Path"}],
            "parameters": parameters, "environment_ref": "default", "resource_request": {"gpu": 0},
            "cache_policy": "disabled", "timeout": 90, "retry": {"max_attempts": 1, "backoff_seconds": 0}, "provenance": {},
            "component": {"id": "independent.calibrated." + entrypoint, "version": "1", "protocol": "modelbuilder.component/v1",
                "role": role, "parameter_schema": {"type": "object"}, "capabilities": {"resume_modes": ["exact_state", "warm_start"] if role == "training" else [],
                    "checkpoint_events": False, "export_formats": ["pytorch"] if role == "export" else [], "frameworks": ["pytorch"]}}})
    for name, seed in (("reference_a", 13), ("reference_b", 29)):
        node(name, "reference", "training", [], "checkpoint", {"seed": seed})
    node("fixed", "calibrate", "utility", ["checkpoint"], "calibration", {"mode": "fixed", "temperature": 1.7})
    node("fitted", "calibrate", "utility", ["checkpoint"], "calibration", {"mode": "fit"})
    node("leaking", "calibrate", "utility", ["checkpoint"], "calibration", {"mode": "fit", "leakage_probe": True})
    node("policy", "train", "training", ["reference", "calibration"], "checkpoint",
         {"architecture": {"input": 3, "hidden": 6, "classes": 3}, "updates": 6, "coefficient": 2., "direction": "policy_to_reference", "delay": 3.0})
    for mode in ("raw", "calibrated"):
        node("evaluate_" + mode, "evaluate", "evaluation", ["checkpoint", "calibration"], "metrics", {"mode": mode})
    node("export", "export", "export", ["checkpoint", "calibration"], "package", {})
    edges = []
    def edge(source, port, target, target_port):
        edges.append({"source_node": source, "source_port": port, "target_node": target, "target_port": target_port})
    for target in ("fixed", "fitted", "leaking"):
        edge("reference_a", "checkpoint", target, "checkpoint")
    edge("reference_a", "checkpoint", "policy", "reference")
    edge("fixed", "calibration", "policy", "calibration")
    for target in ("evaluate_raw", "evaluate_calibrated", "export"):
        edge("policy", "checkpoint", target, "checkpoint")
        edge("fitted", "calibration", target, "calibration")
    return {"schema_version": "0", "name": "Independent calibrated reference", "revision": 1,
        "source": {"repository": "urn:sha256:" + files[0]["sha256"], "commit": commit, "files": files},
        "nodes": nodes, "edges": edges, "resources": [], "external_dependencies": [],
        "environment": {"python": "3.12", "requirements": ["zenml==0.97.0"]}, "evaluation": {}, "outputs": []}


def test_native_calibrated_reference_resume_and_selected_export(tmp_path):
    source = tmp_path / "authored-repository"
    recipe = authored_recipe(source)
    backend = ZenMLBackend(tmp_path / "native", python_executable=sys.executable, source_root=source, trusted_code_roots=[str(source)])
    service = OperationService(tmp_path / "service.sqlite3", backend=backend, source_root=source)
    def call(operation, **payload):
        return service.invoke(operation, payload, OWNER)
    project = call("project.create", slug="calibrated-reference", model_name="One three-class model", recipe=recipe, idempotency_key="project")
    common = {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}
    records = []
    def current():
        return call("recipe.get", **common)
    def wait(submitted, expected="succeeded"):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            result = call("run.status", project_id=common["project_id"], operation_id=submitted["operation_id"])
            if result["status"] in {"succeeded", "failed", "cancelled"} and not result["worker_alive"] and not result["supervisor_alive"]:
                break
            time.sleep(.25)
        job = backend.root / "jobs" / submitted["operation_id"]
        assert result["status"] == expected, (result, (job / "worker.log").read_text()[-16000:])
        records.append(result)
        return result, job
    def submit(key, nodes, bindings=(), complete=True, expected="succeeded"):
        payload = {**common, "expected_revision": current()["revision"], "trusted": True,
            "execution": {"node_ids": nodes, "input_bindings": list(bindings)}}
        plan = call("run.plan", **payload)
        assert plan["executable"], plan
        run = call("run.submit", **payload, idempotency_key=key)
        return wait(run, expected) if complete else run
    def artifacts(run):
        return call("run.artifacts", project_id=common["project_id"], operation_id=run["operation_id"], include_binding_descriptors=True)["artifacts"]
    def find(run, node):
        return next(item for item in artifacts(run) if item.get("node_id") == node)
    def edit(**parameters):
        body = current()
        next(node for node in body["nodes"] if node["id"] == "policy")["parameters"].update(parameters)
        return call("recipe.update", **common, expected_revision=body["revision"], recipe=body, idempotency_key="edit-" + str(body["revision"]))
    try:
        prepared, prep_job = submit("separate-references", ["reference_a", "reference_b", "fixed"])
        ref_a, ref_b, fixed = (find(prepared, name) for name in ("reference_a", "reference_b", "fixed"))
        reference_snapshots = {name: {str(path.relative_to(prep_job / "outputs" / name)): path.read_bytes() for path in (prep_job / "outputs" / name).rglob("*") if path.is_file()} for name in ("reference_a", "reference_b", "fixed")}
        pins = [binding(ref_a, "policy", "reference"), binding(fixed, "policy", "calibration")]
        uninterrupted, full_job = submit("uninterrupted", ["policy"], pins)
        assert uninterrupted["executed_node_ids"] == ["policy"]
        full = torch.load(full_job / "outputs/policy/training-state.pt", weights_only=True)
        full_report = read_json(full_job / "outputs/policy/training-report.json")
        for item in full_report["history"]:
            assert independent_kl(item["policy_logits"], item["reference_logits"], "policy_to_reference") == pytest.approx(item["kl"], abs=1e-13)
        assert abs(full_report["same_distribution_kl"]) < 1e-14
        assert full_report["reference_parameters_unchanged"] and full_report["reference_gradients_absent"]

        interrupted = submit("interrupt", ["policy"], pins, complete=False)
        partial_job = backend.root / "jobs" / interrupted["operation_id"]
        deadline = time.monotonic() + 90
        checkpoint = partial_job / "outputs/policy/checkpoint.manifest.json"
        while not checkpoint.exists() and time.monotonic() < deadline:
            time.sleep(.03)
        assert checkpoint.exists()
        cancelled = call("run.cancel", project_id=common["project_id"], operation_id=interrupted["operation_id"], idempotency_key="cancel")
        assert cancelled["status"] == "cancelled"
        recovered = call("model.checkpoint.get", project_id=common["project_id"], operation_id=interrupted["operation_id"])
        assert recovered["descriptor"]["step"] < 6
        original_request = (partial_job / "request.json").read_bytes()
        resumed = call("run.resume", project_id=common["project_id"], operation_id=interrupted["operation_id"], mode="exact_state",
            expected_recipe_revision=current()["revision"], trusted=True, idempotency_key="exact-resume")
        resumed, resume_job = wait(resumed)
        assert resumed["executed_node_ids"] == ["policy"]
        assert equal(full, torch.load(resume_job / "outputs/policy/training-state.pt", weights_only=True))
        assert read_json(resume_job / "request.json")["execution"]["input_bindings"] == read_json(partial_job / "request.json")["execution"]["input_bindings"]
        assert (partial_job / "request.json").read_bytes() == original_request

        # Backend boundary rejects explicit input substitution before creating a
        # child job; public exact continuation itself always retains parent pins.
        replacement_ref = [binding(ref_b, "policy", "reference"), binding(fixed, "policy", "calibration")]
        fitted_ref, fitted_job = submit("fit-selected-reference", ["fitted"], [binding(ref_a, "fitted", "checkpoint")])
        reference_calibration = find(fitted_ref, "fitted")
        replacement_calibration = [binding(ref_a, "policy", "reference"), binding(reference_calibration, "policy", "calibration")]
        for changed in (replacement_ref, replacement_calibration):
            operation_id = str(uuid.uuid4())
            with pytest.raises(BackendError, match="Exact continuation must retain"):
                backend.invoke("run.resume", {"project_id": common["project_id"], "parent_operation_id": interrupted["operation_id"],
                    "operation_id": operation_id, "idempotency_key": operation_id, "trusted": True, "mode": "exact_state",
                    "execution": {"node_ids": ["policy"], "input_bindings": changed}})
            assert not (backend.root / "jobs" / operation_id).exists()

        # Same initialized policy and batch order: coefficient 0 versus 2 changes
        # the actual update and objective; reverse KL is independently checked.
        edit(coefficient=0., delay=0.)
        zero, zero_job = submit("coefficient-zero", ["policy"], pins)
        zero_state = torch.load(zero_job / "outputs/policy/training-state.pt", weights_only=True)
        assert not equal(full["model"], zero_state["model"])
        assert full["history"][0]["ce"] == zero_state["history"][0]["ce"]
        assert full["history"][0]["loss"] - zero_state["history"][0]["loss"] == pytest.approx(2 * full["history"][0]["kl"])
        warm = call("run.resume", project_id=common["project_id"], operation_id=uninterrupted["operation_id"],
            mode="warm_start", expected_recipe_revision=current()["revision"], trusted=True, idempotency_key="warm-current-objective")
        warm, warm_job = wait(warm)
        assert warm["executed_node_ids"] == ["policy"]
        assert read_json(warm_job / "outputs/policy/training-report.json")["objective"]["coefficient"] == 0.
        warm_state = torch.load(warm_job / "outputs/policy/training-state.pt", weights_only=True)
        assert warm_state["data_cursor"] == 6 and not equal(full["model"], warm_state["model"])
        assert warm["parent_run_id"] == uninterrupted["backend_run_id"]
        edit(coefficient=2., direction="reference_to_policy")
        reverse, reverse_job = submit("reverse-kl", ["policy"], pins)
        reverse_report = read_json(reverse_job / "outputs/policy/training-report.json")
        for item in reverse_report["history"]:
            assert independent_kl(item["policy_logits"], item["reference_logits"], "reference_to_policy") == pytest.approx(item["kl"], abs=1e-13)
        assert reverse_report["history"][0]["kl"] != pytest.approx(full["history"][0]["kl"])

        # Calibrate an explicitly selected earlier policy, not the newest run.
        selected = find(uninterrupted, "policy")
        fitted, calibration_job = submit("fit-selected-policy", ["fitted"], [binding(selected, "fitted", "checkpoint")])
        calibration_artifact = find(fitted, "fitted")
        calibration = read_json(calibration_job / "outputs/fitted/calibration.json")
        assert calibration["fit_nll_after"] <= calibration["fit_nll_before"]
        assert not set(calibration["fit_sample_ids"]) & set(calibration["evaluation_sample_ids"])
        leaked, leak_job = submit("refuse-fit-eval-leakage", ["leaking"], [binding(selected, "leaking", "checkpoint")], expected="failed")
        assert "Calibration fit/evaluation sample leakage" in (leak_job / "worker.log").read_text()
        evaluations = []
        for mode in ("raw", "calibrated"):
            node = "evaluate_" + mode
            run, job = submit(mode + "-evaluation", [node], [binding(selected, node, "checkpoint"), binding(calibration_artifact, node, "calibration")])
            evaluations.append(run)
        compared = call("run.compare", project_id=common["project_id"], run_ids=[run["backend_run_id"] for run in evaluations],
            metric="nll", direction="minimize", comparison_contract={"allowed_variations": ["calibration"], "paired": True})
        assert compared["compatible"], compared
        assert not compare_records(evaluations, "nll", "minimize")["compatible"]
        measures = evaluations[0]["metrics"]["measurements"]
        assert measures["raw"]["accuracy"] == measures["calibrated"]["accuracy"]
        assert measures["calibrated"]["ece_configuration"]["bins"] == 5

        edit(direction="policy_to_reference")
        later, later_job = submit("new-stage-calibrated-reference", ["policy"],
            [binding(selected, "policy", "reference"), binding(calibration_artifact, "policy", "calibration")])
        later_report = read_json(later_job / "outputs/policy/training-report.json")
        assert later_report["reference_checkpoint"] == identity(read_checkpoint(full_job / "outputs/policy"))
        assert later_report["calibration_identity"] == calibration["digest"]
        assert later["parent_run_id"] is None  # New stage; no false exact-resume claim.

        exported, export_job = submit("export-earlier-selected-policy", ["export"],
            [binding(selected, "export", "checkpoint"), binding(calibration_artifact, "export", "calibration")])
        manifest = read_export(export_job / "outputs/export")
        assert manifest["selected_checkpoint"] == identity(read_checkpoint(full_job / "outputs/policy"))
        assert manifest["selected_checkpoint"] != identity(read_checkpoint(later_job / "outputs/policy"))
        assert manifest["calibration"]["identity"] == calibration["digest"]
        assert manifest["verification"]["fresh_process"] and manifest["verification"]["max_abs_difference"] == 0.
        releases = call("model.export.list", project_id=common["project_id"], backend_run_id=exported["backend_run_id"])
        assert len(releases["exports"]) == 1 and not releases["diagnostics"]
        native_export = releases["exports"][0]
        assert native_export["provenance"]["selected_checkpoints"][0]["artifact_id"] == selected["artifact_id"]
        export_identity = {key: native_export["provenance"][key] for key in ("project_id", "backend_run_id", "model_version_id", "artifact_id")}
        package = call("model.export.package", **export_identity, expected_export_digest=native_export["export_digest"])
        archive_path = tmp_path / "selected-calibrated-model.zip"
        with archive_path.open("wb") as stream:
            for index, expected_hash in enumerate(package["chunk_sha256"]):
                chunk = call("model.export.read", **export_identity, package_id=package["package_id"], chunk_index=index)
                content = base64.b64decode(chunk["data_base64"])
                assert hashlib.sha256(content).hexdigest() == expected_hash
                stream.write(content)
        assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == package["sha256"]
        extracted = tmp_path / "selected-calibrated-model"
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(extracted)
        provenance = read_json(extracted / "modelbuilder.provenance.json")
        assert provenance["selected_checkpoints"][0]["artifact_id"] == selected["artifact_id"]
        loaded = subprocess.run([sys.executable, "-I", str(extracted / "loader.py"), str(extracted)], capture_output=True, text=True, timeout=60)
        assert loaded.returncode == 0, loaded.stdout + loaded.stderr
        assert json.loads(loaded.stdout)["max_abs_difference"] == 0.
        for change in ("remove", "tamper"):
            copied = tmp_path / ("bad-export-" + change)
            shutil.copytree(extracted, copied)
            path = copied / "calibration.json"
            if change == "remove":
                path.unlink()
            else:
                changed = read_json(path)
                changed["temperature"] += 1
                path.write_text(json.dumps(changed))
            with pytest.raises((ValueError, FileNotFoundError)):
                read_export(copied)
            failed = subprocess.run([sys.executable, "-I", str(copied / "loader.py"), str(copied)], capture_output=True, text=True)
            assert failed.returncode != 0
        # Native binding digest stops tampered calibration before user code loads.
        native_calibration = next(path for path in Path(calibration_artifact["uri"]).rglob("*") if path.is_file())
        saved_bytes = native_calibration.read_bytes()
        try:
            native_calibration.write_bytes(saved_bytes + b" ")
            plan = call("run.plan", **common, expected_revision=current()["revision"], trusted=True,
                execution={"node_ids": ["policy"], "input_bindings": [binding(selected, "policy", "reference"), binding(calibration_artifact, "policy", "calibration")]})
            assert not plan["executable"] and plan["blocked_reasons"][0]["code"] == "artifact_content_mismatch"
        finally:
            native_calibration.write_bytes(saved_bytes)
        for name, snapshot in reference_snapshots.items():
            assert {str(path.relative_to(prep_job / "outputs" / name)): path.read_bytes() for path in (prep_job / "outputs" / name).rglob("*") if path.is_file()} == snapshot
        assert len(call("recipe.list", project_id=common["project_id"])["recipes"]) == 1
        evidence = {"source": current()["source"], "project_id": common["project_id"], "recipe_id": common["recipe_id"],
            "native_runs": [{key: run.get(key) for key in ("backend_run_id", "model_version_ref", "recipe_revision", "executed_node_ids", "parent_run_id")} for run in records],
            "selected_reference": ref_a["binding_descriptor"], "fixed_temperature": 1.7,
            "objective_forward": full_report, "objective_reverse": reverse_report,
            "calibration": calibration, "untouched_evaluation": measures, "comparison_digest": compared["comparison_digest"],
            "same_reference_resume_state_equal": True, "warm_start_uses_current_objective_and_preserved_cut": True, "replaced_reference_and_calibration_exact_resume_rejected": True,
            "new_stage_calibrated_reference": later_report, "selected_export": native_export, "package_sha256": package["sha256"], "package_fresh_reload": json.loads(loaded.stdout),
            "missing_tampered_calibration_rejected": True, "native_fit_eval_leakage_rejected": True, "reference_artifacts_unchanged": True}
        (tmp_path / "calibrated-reference-evidence.json").write_text(json.dumps(evidence, indent=2))
        print(json.dumps({"native_runs": len(records), "fit_temperature": calibration["temperature"], "evaluation": measures,
            "selected_checkpoint": manifest["selected_checkpoint"], "fresh_reload": manifest["verification"]}))
    finally:
        service.store.close()
