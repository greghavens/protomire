"""Real independent MCP client starts ZenML work and survives service termination."""
from importlib.util import module_from_spec, spec_from_file_location
import json
import hashlib
import subprocess
from pathlib import Path
import time

import pytest

from tests.integration.native_fixture_support import native_backend_profile, synthetic_review
from tests.components.rights_test_support import write_reviewed_policies

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not (ROOT / ".venv-backend/bin/python").is_file(), reason="Pinned backend is not installed")]


async def test_mcp_real_training_and_service_crash(tmp_path):
    spec = spec_from_file_location("mcp_harness", ROOT / "scripts/mcp_smoke.py")
    harness = module_from_spec(spec)
    spec.loader.exec_module(harness)
    directory = tmp_path / "mcp-training"
    directory.mkdir()
    service = harness.ServiceProcess(directory)
    service.start()
    transcript = []
    try:
        async with service.mcp() as session:
            await session.initialize()

            async def call(name, payload):
                result = await session.call_tool(name, payload)
                body = result.structuredContent
                transcript.append({"tool": name, "request": payload, "response": body, "is_error": result.isError})
                assert not result.isError, body
                return body

            project = await call("project_create", {"slug": "mcp-trained", "model_name": "MCP CPU model", "idempotency_key": "p"})
            project_id = project["project_id"]
            draft = await call("recipe_get", {"project_id": project_id, "recipe_id": project["recipe_id"]})
            assert draft["revision"] == 1
            manifest = native_backend_profile(
                json.loads((ROOT / "examples/custom-pytorch/recipe.json").read_text()), ROOT)
            # This probes transport/restart, not clean portability. Pin the checked-out workload.
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            source_path = "src/modelbuilder/backend/fixtures.py"
            source = subprocess.check_output(["git", "show", f"{commit}:{source_path}"], cwd=ROOT)
            assert source == (ROOT / source_path).read_bytes()
            manifest["source"]["commit"] = commit
            manifest["source"]["files"] = [{"path": source_path, "sha256": hashlib.sha256(source).hexdigest()}]
            manifest.pop("digest", None)
            for node in manifest["nodes"]:
                if node["kind"] == "model.train":
                    node["parameters"]["steps"] = 30
                    node["parameters"]["step_delay"] = 0.15
            policy = synthetic_review(manifest, project_id, ROOT)
            write_reviewed_policies(directory / "service.sqlite3", {policy["policy_id"]: policy})
            recipe = await call("recipe_create", {"project_id": project_id, "recipe": manifest,
                "expected_revision": draft["revision"], "idempotency_key": "r"})
            assert recipe["recipe_id"] == draft["recipe_id"] == project["recipe_id"]
            assert recipe["revision"] == draft["revision"] + 1
            run_payload = {"project_id": project_id, "recipe_id": recipe["recipe_id"],
                           "expected_revision": recipe["revision"], "trusted": True, "idempotency_key": "submit"}
            submitted = await call("run_submit", run_payload)
            assert submitted["status"] in {"submitted", "running"}
            assert await call("run_submit", run_payload) == submitted
            # Verify the native job actually reached running before killing control plane.
            running_deadline = time.monotonic() + 90
            before_restart = None
            while time.monotonic() < running_deadline:
                before_restart = await call("run_status", {"project_id": project_id, "operation_id": submitted["operation_id"]})
                if before_restart["status"] == "running":
                    break
                assert before_restart["status"] not in {"failed", "cancelled", "succeeded"}, before_restart
                await harness.asyncio.sleep(0.1)
            assert before_restart and before_restart["status"] == "running"
            assert before_restart["backend_run_id"]
            service.restart()  # SIGKILL of the real service, not the training supervisor.
            deadline = time.monotonic() + 180
            status = None
            while time.monotonic() < deadline:
                status = await call("run_status", {"project_id": project_id, "operation_id": submitted["operation_id"]})
                if status["status"] in {"succeeded", "failed", "cancelled"}:
                    break
                await harness.asyncio.sleep(0.2)
            assert status and status["status"] == "succeeded", status
            assert status["backend_run_id"]
            assert status["backend_version"] == "0.97.0"
            assert status["recipe_id"] == recipe["recipe_id"]
            assert status["recipe_revision"] == recipe["revision"]
            assert status["recipe_digest"] == recipe["digest"]
            assert await call("recipe_get", {"project_id": project_id, "recipe_id": recipe["recipe_id"]}) == recipe
            assert await call("recipe_get", {"project_id": project_id, "recipe_id": recipe["recipe_id"],
                                             "revision": draft["revision"]}) == draft
            history = await call("recipe_history", {"project_id": project_id})
            assert history["revisions"] == [draft, recipe]
            assert history["legacy_recipes"] == []
            artifacts = await call("run_artifacts", {"project_id": project_id, "operation_id": submitted["operation_id"]})
            assert artifacts["artifacts"]
            assert all(a["artifact_id"] for a in artifacts["artifacts"])
            assert status["metrics"]["consumed_dataset_artifact"] is True
            output = ROOT / "evidence/mcp/single-model/mcp-training.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"status": "PASS", "transport": "independent SDK stdio client",
                                          "service_generations": service.generation, "pre_restart_status": before_restart, "transcript": transcript}, indent=2) + "\n")
    finally:
        service.stop()
        (directory / "transcript.json").write_text(json.dumps(transcript, indent=2) + "\n")
