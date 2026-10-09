"""A registered arbitrary text dataset consumed by a real independent Torch model."""
import asyncio
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
import hashlib
import json
from pathlib import Path
import subprocess
import time

import pytest

import httpx

from test_registry import make_manifest

ROOT = Path(__file__).resolve().parents[2]
BACKEND_PYTHON = ROOT / ".venv-backend/bin/python"
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not BACKEND_PYTHON.exists(), reason="Pinned native backend required")]
HARNESS = '''from pathlib import Path
import json
import hashlib
import torch

def fit(inputs, parameters, context):
    train, dev, heldout = (inputs[key] for key in ('train', 'dev', 'heldout'))
    torch.set_num_threads(1)
    torch.manual_seed(41)
    model = torch.nn.Sequential(torch.nn.Linear(1, 4), torch.nn.Tanh(), torch.nn.Linear(4, 1))
    optimizer = torch.optim.Adam(model.parameters(), lr=0.02)
    rows = train['rows']
    x = torch.tensor([[len(row['text']) / 10] for row in rows], dtype=torch.float32)
    y = torch.tensor([[row['label'] / 10] for row in rows], dtype=torch.float32)
    before = torch.nn.functional.mse_loss(model(x), y).item()
    consumed = []
    for step in range(300):
        consumed.extend(row['id'] for row in rows)
        optimizer.zero_grad()
        loss = torch.nn.functional.mse_loss(model(x), y)
        loss.backward()
        optimizer.step()
    seen = set(consumed)
    assert not seen.intersection(dev['split']['sample_ids'])
    assert not seen.intersection(heldout['split']['sample_ids'])
    destination = Path(context['output_dir'])
    destination.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), destination / 'model.pt')
    final = torch.nn.functional.mse_loss(model(x), y).item()
    report = {'initial_loss': before, 'final_loss': final, 'actual_consumed_ids': consumed,
              'data_identity': train['dataset'], 'training_lineage': train['consumption'],
              'dev_identity': dev['split'], 'heldout_identity': heldout['split']}
    (destination / 'report.json').write_text(json.dumps(report))
    checkpoint = {'schema_version': 'modelbuilder.checkpoint/v1', 'identities': context['identities'],
                  'resume_modes': [], 'assets': [{'path': 'model.pt', 'sha256': hashlib.sha256((destination / 'model.pt').read_bytes()).hexdigest()}],
                  'step': 300, 'purpose': 'inference', 'metrics': {'loss': final}}
    (destination / 'checkpoint.manifest.json').write_text(json.dumps(checkpoint))
    return {'trained': destination}
'''


async def test_registered_dataset_native_import_and_actual_model_consumption(tmp_path, monkeypatch):
    source = tmp_path / "independent-model"
    source.mkdir()
    (source / "harness.py").write_text(HARNESS)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Data Acceptance", "-c", "user.email=data@example.invalid",
                    "commit", "-qm", "Independent text harness"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    code_digest = hashlib.sha256((source / "harness.py").read_bytes()).hexdigest()
    data_root = tmp_path / "external-data"
    data_root.mkdir()
    rows = [{"id": f"{split}-{i}", "text": "x" * i, "label": 2 * i + 1}
            for split, indices in [("train", range(1, 7)), ("dev", range(7, 9)), ("heldout", range(9, 11))] for i in indices]
    manifest = make_manifest(data_root, rows=rows)
    spec = spec_from_file_location("native_dataset_mcp_harness", ROOT / "scripts/mcp_smoke.py")
    harness = module_from_spec(spec)
    spec.loader.exec_module(harness)
    state = tmp_path / "service-state"
    state.mkdir()
    process = harness.ServiceProcess(state)
    config = tmp_path / "operator.json"
    operator = {"source_root": str(source), "trusted_code_roots": [str(source)], "python_executable": str(BACKEND_PYTHON)}
    config.write_text(json.dumps(operator))
    monkeypatch.setenv("MODELBUILDER_OPERATOR_CONFIG", str(config))
    monkeypatch.setenv("MODELBUILDER_BACKEND", "zenml")
    process.start()
    try:
        async with httpx.AsyncClient(base_url=process.base_url, trust_env=False, timeout=180,
                                     headers={"Authorization": "Bearer " + process.owner}) as http:
            async def call_http(name, payload):
                response = await http.post("/v1/operations/" + name, json=payload)
                assert response.status_code == 200, response.text
                return response.json()
            project = await call_http("project.create", {"slug": "native-data", "model_name": "Text length predictor", "idempotency_key": "p"})
            pid = project["project_id"]
            # Operator admission config supplies root aliases only. It never supplies
            # a portable-reference file or dependency map, and never writes the DB.
            operator["dataset_roots"] = {pid: {"data": str(data_root)}}
            config.write_text(json.dumps(operator))
            process.restart()
            version = await call_http("dataset.register", {"project_id": pid, "manifest": manifest, "idempotency_key": "register"})
            changed = deepcopy(manifest)
            changed["description"] = "A newer revision must not silently redirect the selected version"
            newer = await call_http("dataset.version", {"project_id": pid, "dataset_id": version["dataset_id"],
                "expected_revision": 1, "manifest": changed, "idempotency_key": "next"})
            assert newer["revision"] == 2
            shared = {"environment_ref": "default", "resource_request": {"gpu": 0}, "timeout": 120,
                      "retry": {"max_attempts": 1, "backoff_seconds": 0}, "cache_policy": "disabled", "provenance": {}}
            node = {**shared, "id": "fit", "kind": "component.run", "implementation_ref": {"module": "harness", "entrypoint": "fit",
                        "path": "harness.py", "sha256": code_digest, "commit": commit},
                    "inputs": [{"name": name, "type": "dict", "required": False} for name in ("train", "dev", "heldout")],
                    "outputs": [{"name": "trained", "type": "Path"}], "parameters": {},
                    "component": {"id": "independent.text-fit", "version": "1", "protocol": "modelbuilder.component/v1",
                        "role": "training", "parameter_schema": {"type": "object", "additionalProperties": False},
                        "capabilities": {"checkpoint_events": False, "resume_modes": [], "frameworks": ["pytorch"], "export_formats": []}}}
            recipe = {"schema_version": "0", "project_id": pid, "recipe_id": project["recipe_id"], "revision": 1,
                      "name": "Registered data into independent model", "source": {"repository": "urn:sha256:" + code_digest,
                        "commit": commit, "files": [{"path": "harness.py", "sha256": code_digest}]},
                      "nodes": [node], "edges": [], "resources": [], "external_dependencies": [],
                      "environment": {"python": "3.12", "requirements": ["zenml==0.97.0"]}, "evaluation": {}, "outputs": []}
            current = await call_http("recipe.update", {"project_id": pid, "recipe_id": project["recipe_id"], "recipe": recipe,
                                     "expected_revision": 1, "idempotency_key": "recipe"})
            async with process.mcp() as session:
                await session.initialize()
                assert "dataset_bind" in {tool.name for tool in (await session.list_tools()).tools}
                async def call_mcp(name, payload):
                    result = await session.call_tool(name, payload)
                    assert not result.isError, result.structuredContent
                    return result.structuredContent
                for name in ("train", "dev", "heldout"):
                    request = {"project_id": pid, "recipe_id": current["recipe_id"], "expected_revision": current["revision"],
                        "dataset_id": version["dataset_id"], "dataset_revision": 1, "split_id": name,
                        "purpose": "training" if name == "train" else "evaluation", "node_id": name,
                        "target_node": "fit", "target_port": name, "idempotency_key": "bind-" + name}
                    bound = await call_mcp("dataset_bind", request)
                    assert bound["execution_started"] is False
                    assert await call_mcp("dataset_bind", request) == bound
                    current = bound["recipe"]
                assert current["recipe_id"] == project["recipe_id"] and current["revision"] == 5
                assert str(data_root) not in json.dumps(current) and str(source) not in json.dumps(current)
                reference = next(item["metadata"]["dataset_reference"] for item in current["external_dependencies"]
                                 if "dataset_reference" in item.get("metadata", {}))
                assert reference["manifest"]["row_schema"] == manifest["row_schema"]
                assert reference["manifest"]["sources"] == manifest["sources"]
                assert reference["manifest"]["provenance"] == manifest["provenance"]
                payload = {"project_id": pid, "recipe_id": current["recipe_id"], "trusted": True}
                plan = await call_mcp("run_plan", payload)
                assert plan["executable"], json.dumps(plan, indent=2)
                submitted = await call_mcp("run_submit", {**payload, "expected_revision": current["revision"], "idempotency_key": "run"})
                deadline = time.monotonic() + 180
                status = submitted
                while time.monotonic() < deadline:
                    status = await call_mcp("run_status", {"project_id": pid, "operation_id": submitted["operation_id"]})
                    if status["status"] in {"succeeded", "failed", "cancelled"}:
                        break
                    await asyncio.sleep(.25)
                job = state / "backend" / "jobs" / submitted["operation_id"]
                output = job / "outputs" / "fit"
                assert status["status"] == "succeeded", (status, (job / "worker.log").read_text()[-12000:])
                report = json.loads((output / "report.json").read_text())
                assert report["final_loss"] < report["initial_loss"] * .01
                assert len(report["actual_consumed_ids"]) == 300 * 6
                assert set(report["actual_consumed_ids"]) == set(version["splits"][0]["sample_ids"])
                assert report["data_identity"]["content_digest"] == version["content_digest"]
                assert report["training_lineage"]["version_id"] == version["version_id"]
                assert report["training_lineage"]["dataset"]["revision"] == "1"
                assert len(status["artifacts"]) >= 4 and status["backend_run_id"] and (output / "model.pt").is_file()
                bundle = await call_mcp("recipe_export", {"project_id": pid, "recipe_id": current["recipe_id"]})
                assert str(data_root) not in json.dumps(bundle) and str(source) not in json.dumps(bundle)
                assert not any(path.endswith(".jsonl") for path in bundle["files"])
                assert bundle["recipe"]["external_dependencies"] == current["external_dependencies"]
                job_request = json.loads((job / "request.json").read_text())
                assert job_request["dataset_source_guards"]  # captured roots reach actual worker
                assert not operator.get("dependency_paths")
    finally:
        process.stop()
