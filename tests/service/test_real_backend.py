"""Actual shared-service execution acceptance; no fake backend or native run records."""
from __future__ import annotations

import json
import hashlib
import subprocess
import time
from pathlib import Path

import pytest

from modelbuilder.backend import ZenMLBackend
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService

ROOT = Path(__file__).resolve().parents[2]
BACKEND_PYTHON = ROOT / ".venv-backend/bin/python"
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not BACKEND_PYTHON.is_file(), reason="Pinned backend environment is not installed")]


def test_shared_service_native_execution_resume_research_and_scope(tmp_path):
    actor = Actor("integration-owner", {"read", "modify", "execute"})
    database = tmp_path / "service.sqlite3"
    backend = ZenMLBackend(tmp_path, python_executable=str(BACKEND_PYTHON))
    service = OperationService(database, backend)
    def invoke(operation, payload):
        return service.invoke(operation, payload, actor)
    project = invoke("project.create", {"slug": "service-native", "model_name": "Service native CPU fixture", "idempotency_key": "project"})
    project_id = project["project_id"]
    manifest = json.loads((ROOT / "examples/custom-pytorch/recipe.json").read_text())
    # This is service integration, not the full clean-replay gate. Pin the exact committed
    # scientific fixture while adapter hardening proceeds independently in this checkout.
    source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    fixture_path = "src/modelbuilder/backend/fixtures.py"
    committed_fixture = subprocess.check_output(["git", "show", f"{source_commit}:{fixture_path}"], cwd=ROOT)
    assert committed_fixture == (ROOT / fixture_path).read_bytes(), "Commit the scientific fixture before integration"
    manifest["source"]["commit"] = source_commit
    manifest["source"]["files"] = [{"path": fixture_path, "sha256": hashlib.sha256(committed_fixture).hexdigest()}]
    for node in manifest["nodes"]:
        if node["kind"] == "model.train":
            node["parameters"]["steps"] = 4
    recipe = invoke("recipe.create", {"project_id": project_id, "recipe": manifest, "expected_revision": 1, "idempotency_key": "recipe"})
    plan = invoke("run.plan", {"project_id": project_id, "recipe_id": recipe["recipe_id"]})
    assert plan["executable"] is True
    payload = {"project_id": project_id, "recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"],
               "trusted": True, "idempotency_key": "run-1"}
    submitted = invoke("run.submit", payload)
    assert invoke("run.submit", payload) == submitted
    # A new application service recovers the still-running supervisor by persisted identity.
    service.store.close()
    service = OperationService(database, ZenMLBackend(tmp_path, python_executable=str(BACKEND_PYTHON)))
    def completed(operation_id):
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            result = invoke("run.status", {"project_id": project_id, "operation_id": operation_id})
            if result["status"] in {"failed", "succeeded", "cancelled"}:
                assert result["status"] == "succeeded", result
                return result
            time.sleep(0.5)
        pytest.fail("Real backend did not finish before the fixture deadline")
    first = completed(submitted["operation_id"])
    assert first["backend_run_id"] and first["model_version_ref"]
    assert first["metrics"]["validation_loss"] > 0
    artifacts = invoke("run.artifacts", {"project_id": project_id, "backend_run_id": first["backend_run_id"]})
    assert len(artifacts["artifacts"]) >= 4
    assert all(artifact["artifact_id"] for artifact in artifacts["artifacts"])
    checkpoint = invoke("model.checkpoint.get", {"project_id": project_id, "operation_id": first["operation_id"]})
    resumed = invoke("run.resume", {"project_id": project_id, "operation_id": first["operation_id"],
                                    "mode": "exact_state", "steps": 6, "trusted": True, "expected_recipe_revision": recipe["revision"], "idempotency_key": "resume"})
    assert resumed["operation_id"] != first["operation_id"]
    second = completed(resumed["operation_id"])
    assert second["backend_run_id"] != first["backend_run_id"]
    assert second["parent_run_id"] == first["backend_run_id"]
    assert second["resume_mode"] == "exact_state"
    assert second["recipe_id"] == first["recipe_id"]
    assert second["recipe_revision"] == first["recipe_revision"] + 1
    assert invoke("recipe.get", {"project_id": project_id, "recipe_id": recipe["recipe_id"], "revision": first["recipe_revision"]}) == recipe
    assert second["recipe_digest"] != first["recipe_digest"]
    resumed_manifest = invoke("recipe.get", {"project_id": project_id, "recipe_id": second["recipe_id"]})
    assert next(n for n in resumed_manifest["nodes"] if n["kind"] == "model.train")["parameters"]["steps"] == 6
    # This test verifies the resume control path; independent tensor parity is a backend acceptance test.
    comparison = invoke("run.compare", {"project_id": project_id, "run_ids": [first["backend_run_id"], second["backend_run_id"]],
                                         "metric": "validation_loss", "direction": "minimize"})
    # Primary arXiv abstract/title/authors checked on 2026-10-09. The paper concerns
    # distillation; it does not prove this fixture's continuation or teacher suitability.
    evidence = invoke("evidence.add", {"project_id": project_id, "title": "Distilling the Knowledge in a Neural Network",
                         "source_url": "https://arxiv.org/abs/1503.02531", "source_identifier": "arXiv:1503.02531v1",
                         "authors": ["Geoffrey Hinton", "Oriol Vinyals", "Jeff Dean"], "publication_date": "2015-03-09",
                         "citation_metadata": {"metadata_verified_at": "2026-10-09", "provider": "arXiv",
                             "metadata_verification_url": "https://arxiv.org/abs/1503.02531",
                             "paper_result_reproduced": False, "claim_verified": False}, "idempotency_key": "evidence"})
    claim = invoke("claim.create", {"project_id": project_id, "evidence_ids": [evidence["evidence_id"]],
                         "proposition": "For this synthetic fixture, the lower-validation-loss checkpoint is a candidate reference for a later distillation experiment; suitability for distillation remains untested.",
                         "review_status": "unreviewed", "confidence": None,
                         "uncertainty": "The cited paper supplies distillation context. This continuation experiment measures only a fixed synthetic validation split and does not reproduce the paper or test distillation efficacy.",
                         "idempotency_key": "claim"})
    hypothesis = invoke("hypothesis.create", {"project_id": project_id, "evidence_ids": [evidence["evidence_id"]],
                         "claim_ids": [claim["claim_id"]],
                         "proposed_change": "Continue the tiny baseline to select a checkpoint candidate for a later distillation experiment",
                         "expected_outcome": "Lower validation loss after continuation from four to six steps",
                         "acceptance_metric": {"name": "validation_loss", "direction": "minimize"},
                         "scope": "Fixed synthetic validation split only; no distillation or generalization claim.",
                         "idempotency_key": "hypothesis"})
    assert invoke("claim.get", {"project_id": project_id, "claim_id": claim["claim_id"]}) == claim
    assert invoke("hypothesis.get", {"project_id": project_id, "hypothesis_id": hypothesis["hypothesis_id"]})["claim_ids"] == [claim["claim_id"]]
    assert second["metrics"]["validation_loss"] < first["metrics"]["validation_loss"]
    decision = invoke("decision.record", {"project_id": project_id, "hypothesis_id": hypothesis["hypothesis_id"],
                        "recipe_id": recipe["recipe_id"], "recipe_revision": recipe["revision"],
                        "run_ids": [first["backend_run_id"], second["backend_run_id"]],
                        "selection_rule": {"metric": "validation_loss", "direction": "minimize"}, "outcome": "accepted",
                        "rationale": "Select the measured minimum on the declared validation split; citation is context, not proof.",
                        "idempotency_key": "decision"})
    promoted = invoke("model.version.promote", {"project_id": project_id,
                        "model_version_ref": comparison["selected_model_version_ref"], "decision_id": decision["decision_id"],
                        "expected_revision": 1, "stage": "staging", "idempotency_key": "promote"})
    assert promoted["model_version_ref"] == comparison["selected_model_version_ref"]
    assert invoke("project.get", {"project_id": project_id})["active_model_version_ref"] == promoted["model_version_ref"]
    other = invoke("project.create", {"slug": "isolated", "model_name": "Unrelated", "idempotency_key": "other"})
    with pytest.raises(ServiceError) as error:
        invoke("model.version.get", {"project_id": other["project_id"], "model_version_ref": first["model_version_ref"]})
    assert error.value.code == "not_found"
    record = {"test": "shared_service_native_execution_resume_research_and_scope", "status": "passed",
              "project_id": project_id, "recipe_id": recipe["recipe_id"], "recipe_digest": recipe["digest"], "source_commit": source_commit,
              "recipe_revision": recipe["revision"], "continuation_recipe_id": second["recipe_id"],
              "continuation_recipe_revision": second["recipe_revision"], "continuation_recipe_digest": second["recipe_digest"],
              "operation_ids": [first["operation_id"], second["operation_id"]],
              "backend_run_ids": [first["backend_run_id"], second["backend_run_id"]],
              "artifact_ids": [a["artifact_id"] for a in artifacts["artifacts"]],
              "checkpoint_artifact_id": checkpoint["artifact_id"], "decision_id": decision["decision_id"],
              "evidence_id": evidence["evidence_id"], "claim_id": claim["claim_id"], "hypothesis_id": hypothesis["hypothesis_id"],
              "research_linkage": {"evidence": evidence, "claim": claim, "hypothesis": hypothesis,
                                   "decision": decision, "paper_result_reproduced": False},
              "selected_model_version_ref": promoted["model_version_ref"], "comparison": comparison}
    output = ROOT / "evidence/backend/service-integration.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2) + "\n")
    service.store.close()
