"""A registered arbitrary text dataset consumed by a real independent Torch model."""
import hashlib
import json
from pathlib import Path
import subprocess
import time

import pytest

from modelbuilder.backend import ZenMLBackend
from modelbuilder.dataset import DatasetRegistry
from modelbuilder.models import Actor
from modelbuilder.service import OperationService

from test_registry import make_manifest

ROOT = Path(__file__).resolve().parents[2]
BACKEND_PYTHON = ROOT / ".venv-backend/bin/python"
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not BACKEND_PYTHON.exists(), reason="Pinned native backend required")]
OWNER = Actor("native-data-owner", {"read", "modify", "execute"})
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


def test_registered_dataset_native_import_and_actual_model_consumption(tmp_path):
    source = tmp_path / "independent-model"
    source.mkdir()
    (source / "harness.py").write_text(HARNESS)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Data Acceptance", "-c", "user.email=data@example.invalid",
                    "commit", "-qm", "Independent text harness"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    code_digest = hashlib.sha256((source / "harness.py").read_bytes()).hexdigest()
    service = OperationService(tmp_path / "service.sqlite3", source_root=source)
    project = service.invoke("project.create", {"slug": "native-data", "model_name": "Text length predictor", "idempotency_key": "p"}, OWNER)
    pid = project["project_id"]
    service.store.close()
    data_root = tmp_path / "external-data"
    data_root.mkdir()
    rows = [{"id": f"{split}-{i}", "text": "x" * i, "label": 2 * i + 1}
            for split, indices in [("train", range(1, 7)), ("dev", range(7, 9)), ("heldout", range(9, 11))] for i in indices]
    manifest = make_manifest(data_root, rows=rows)
    roots = {pid: {"data": str(data_root)}}
    backend = ZenMLBackend(tmp_path / "state", python_executable=str(BACKEND_PYTHON), source_root=source,
                           trusted_code_roots=[str(source)])
    service = OperationService(tmp_path / "service.sqlite3", source_root=source, dataset_roots=roots, backend=backend)
    try:
        version = service.invoke("dataset.register", {"project_id": pid, "manifest": manifest, "idempotency_key": "register"}, OWNER)
        portable = DatasetRegistry(service.store, project_roots=roots).portable_reference(pid, version["dataset_id"], 1)
        reference_path = data_root / "registered-version.json"
        reference_path.write_text(json.dumps(portable))
        reference_sha = hashlib.sha256(reference_path.read_bytes()).hexdigest()
        dependencies = [{"id": "registered-version", "kind": "dataset", "url": "urn:sha256:" + reference_sha,
                         "sha256": reference_sha, "license": "CC0-1.0", "required": True}]
        paths = {"registered-version": str(reference_path)}
        for item in portable["dependencies"]:
            dependencies.append({key: value for key, value in item.items() if key != "source_id"} | {"url": "urn:sha256:" + item["sha256"]})
            paths[item["id"]] = str(data_root / "weather.jsonl")
        backend.dependency_paths = paths
        shared = {"environment_ref": "default", "resource_request": {"gpu": 0}, "timeout": 120,
                  "retry": {"max_attempts": 1, "backoff_seconds": 0}, "cache_policy": "disabled", "provenance": {}}
        nodes = [{**shared, "id": name, "kind": "dataset.version.import", "implementation_ref": "builtin:dataset.version.import",
                  "inputs": [], "outputs": [{"name": "data", "type": "dict"}],
                  "parameters": {"reference_id": "registered-version", "split_id": name,
                                 "purpose": "training" if name == "train" else "evaluation"}}
                 for name in ("train", "dev", "heldout")]
        nodes.append({**shared, "id": "fit", "kind": "component.run", "implementation_ref": {"module": "harness", "entrypoint": "fit",
                      "path": "harness.py", "sha256": code_digest, "commit": commit},
                      "inputs": [{"name": name, "type": "dict"} for name in ("train", "dev", "heldout")],
                      "outputs": [{"name": "trained", "type": "Path"}], "parameters": {},
                      "component": {"id": "independent.text-fit", "version": "1", "protocol": "modelbuilder.component/v1",
                         "role": "training", "parameter_schema": {"type": "object", "additionalProperties": False},
                         "capabilities": {"checkpoint_events": False, "resume_modes": [], "frameworks": ["pytorch"], "export_formats": []}}})
        recipe = {"schema_version": "0", "project_id": pid, "recipe_id": project["recipe_id"], "revision": 1,
                  "name": "Registered data into independent model", "source": {"repository": "urn:sha256:" + code_digest,
                    "commit": commit, "files": [{"path": "harness.py", "sha256": code_digest}]},
                  "nodes": nodes, "edges": [{"source_node": name, "source_port": "data", "target_node": "fit", "target_port": name}
                                              for name in ("train", "dev", "heldout")],
                  "resources": [], "external_dependencies": dependencies, "environment": {"python": "3.12", "requirements": ["zenml==0.97.0"]},
                  "evaluation": {}, "outputs": []}
        current = service.invoke("recipe.update", {"project_id": pid, "recipe_id": project["recipe_id"], "recipe": recipe,
                                   "expected_revision": 1, "idempotency_key": "recipe"}, OWNER)
        payload = {"project_id": pid, "recipe_id": current["recipe_id"], "trusted": True}
        plan = service.invoke("run.plan", payload, OWNER)
        assert plan["executable"], plan
        submitted = service.invoke("run.submit", {**payload, "expected_revision": current["revision"], "idempotency_key": "run"}, OWNER)
        deadline = time.monotonic() + 180
        status = submitted
        while time.monotonic() < deadline:
            status = service.invoke("run.status", {"project_id": pid, "operation_id": submitted["operation_id"]}, OWNER)
            if status["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(.25)
        job = backend.root / "jobs" / submitted["operation_id"]
        output = job / "outputs" / "fit"
        assert status["status"] == "succeeded", (status, (job / "worker.log").read_text()[-12000:])
        report = json.loads((output / "report.json").read_text())
        assert report["final_loss"] < report["initial_loss"] * .01
        assert len(report["actual_consumed_ids"]) == 300 * 6
        assert set(report["actual_consumed_ids"]) == set(version["splits"][0]["sample_ids"])
        assert report["data_identity"]["content_digest"] == version["content_digest"]
        assert report["training_lineage"]["version_id"] == version["version_id"]
        assert len(status["artifacts"]) >= 4
        assert status["backend_run_id"] and (output / "model.pt").is_file()
    finally:
        service.store.close()
