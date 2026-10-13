"""Bound authored model + immutable datasets + checkpoint cuts + native release.

No framework/runtime doubles: all work crosses OperationService and real native
ZenML steps. The authored model and its portable loader are independent of the
repository's classifier fixtures.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import threading
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import zipfile

import pytest

from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.common import read_json
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService
from tests.components.rights_test_support import USAGE_CONTEXT, local_source_review, review_local_dataset

ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(os.environ.get("MODELBUILDER_BACKEND_PYTHON", str(ROOT / ".venv-backend/bin/python")))
OWNER = Actor("bound-release-owner", {"read", "modify", "execute"})
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not PYTHON.is_file(), reason="Pinned backend required")]

# This committed source owns the optimization loop, evaluation protocol, and
# loader. Platform helpers supply only their published evidence contracts.
HARNESS = r'''from pathlib import Path
import hashlib
import importlib.util
import json
import shutil
import torch
from modelbuilder.backend.components import identity, read_checkpoint
from modelbuilder.evaluation import make_evaluation_record
from modelbuilder.artifact_rights import combine_parent_rights

LOADER = """from pathlib import Path
import torch

def load(root):
    model = torch.nn.Linear(1, 1).eval()
    model.load_state_dict(torch.load(Path(root) / 'model.pt', map_location='cpu', weights_only=True))
    return model
"""

def tensors(data):
    return (torch.tensor([[len(row['text']) / 10] for row in data['rows']], dtype=torch.float32),
            torch.tensor([[row['label'] / 10] for row in data['rows']], dtype=torch.float32))

def load(root):
    model = torch.nn.Linear(1, 1).eval()
    model.load_state_dict(torch.load(Path(root) / 'model.pt', map_location='cpu', weights_only=True))
    return model

def asset(root, name):
    return {'path': name, 'sha256': hashlib.sha256((root / name).read_bytes()).hexdigest()}

def fit(*, inputs, parameters, context):
    data = inputs['data']
    assert data['split']['role'] == 'train'
    torch.set_num_threads(1)
    torch.manual_seed(41)
    model = torch.nn.Linear(1, 1)
    optimizer = torch.optim.Adam(model.parameters(), lr=.03)
    x, y = tensors(data)
    initial = float(torch.nn.functional.mse_loss(model(x), y).detach())
    consumed = []
    for _ in range(parameters['updates']):
        consumed.extend(row['id'] for row in data['rows'])
        optimizer.zero_grad()
        loss = torch.nn.functional.mse_loss(model(x), y)
        loss.backward()
        optimizer.step()
    final = float(torch.nn.functional.mse_loss(model(x), y).detach())
    output = Path(context['output_dir'])
    torch.save(model.state_dict(), output / 'model.pt')
    report = {'initial_loss': initial, 'final_loss': final, 'actual_consumed_ids': consumed,
              'dataset': data['dataset'], 'consumption': data['consumption']}
    (output / 'report.json').write_text(json.dumps(report))
    checkpoint = {'schema_version': 'modelbuilder.checkpoint/v1', 'identities': context['identities'],
                  'resume_modes': [], 'assets': [asset(output, 'model.pt'), asset(output, 'report.json')],
                  'step': parameters['updates'], 'purpose': 'inference', 'metrics': {'loss': final},
                  'rights': combine_parent_rights([{'artifact_id': 'declared-origin', 'rights': context['declared_artifact_rights']},
                      {'artifact_id': 'input-dataset', 'rights': data['rights']}])}
    (output / 'checkpoint.manifest.json').write_text(json.dumps(checkpoint))
    return {'checkpoint': output, 'metrics': {'train_loss': final, 'initial_loss': initial,
        'updates': parameters['updates'], 'dataset_version_id': data['consumption']['version_id']}}

def evaluate(*, inputs, parameters, context):
    data, checkpoint = inputs['data'], inputs['checkpoint']
    assert data['split']['role'] in ('validation', 'test')
    torch.set_num_threads(1)
    model = load(checkpoint)
    x, y = tensors(data)
    with torch.no_grad():
        predictions = model(x)
        errors = ((predictions - y) ** 2).flatten().tolist()
    measured = sum(errors) / len(errors)
    record = make_evaluation_record(dataset=data['dataset'], split=data['split'],
        protocol={'implementation': 'authored-text-length', 'version': '1', 'configuration': {},
                  'preprocessing': {'text_length_divisor': 10, 'label_divisor': 10}, 'seed': 41},
        metric={'name': 'mse', 'implementation': 'torch.squared_error', 'version': '1',
                'parameters': {}, 'aggregation': 'mean', 'direction': 'minimize'},
        treatment={'model': 'authored-affine', 'checkpoint': parameters.get('claimed_checkpoint', identity(read_checkpoint(checkpoint))),
                   'calibration': None, 'reference': None}, value=measured,
        predictions=[{'item_id': row['id'], 'value': error} for row, error in zip(data['rows'], errors)])
    return {'metrics': {'mse': measured, 'predictions': predictions.tolist(),
                        'sample_ids': data['split']['sample_ids'], 'evaluation_record': record}}

def export(*, inputs, parameters, context):
    checkpoint, data = inputs['checkpoint'], inputs['data']
    output = Path(context['output_dir'])
    shutil.copy2(checkpoint / 'model.pt', output / 'model.pt')
    (output / 'loader.py').write_text(LOADER)
    spec = importlib.util.spec_from_file_location('authored_saved_loader', output / 'loader.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    x, _ = tensors(data)
    with torch.no_grad():
        expected, actual = load(checkpoint)(x), module.load(output)(x)
    difference = float((expected - actual).abs().max())
    assert difference == 0.0
    (output / 'parity.json').write_text(json.dumps({'predictions': actual.tolist(),
        'sample_ids': data['split']['sample_ids'], 'max_abs_difference': difference}))
    manifest = {'schema_version': 'modelbuilder.export/v1',
        'selected_checkpoint': identity(read_checkpoint(checkpoint)), 'rights': read_checkpoint(checkpoint)['rights'],
        'loader': {'path': 'loader.py', 'entrypoint': 'load'},
        'signature': {'inputs': [{'name': 'text_length', 'shape': [None, 1], 'dtype': 'float32'}],
                      'outputs': [{'name': 'prediction', 'shape': [None, 1], 'dtype': 'float32'}]},
        'dependencies': {'torch': torch.__version__}, 'source': context['source'],
        'verification': {'passed': True, 'atol': 0.0, 'max_abs_difference': difference},
        'assets': [asset(output, name) for name in ('model.pt', 'loader.py', 'parity.json')]}
    (output / 'export.manifest.json').write_text(json.dumps(manifest))
    return {'package': output}
'''


def authored_recipe(source):
    source.mkdir()
    (source / "harness.py").write_text(HARNESS)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Acceptance", "-c",
                    "user.email=acceptance@example.invalid", "commit", "-qm", "Authored bound release"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    digest = hashlib.sha256((source / "harness.py").read_bytes()).hexdigest()
    nodes = []
    for name, role, entrypoint in (("train", "training", "fit"), ("dev_eval", "evaluation", "evaluate"),
                                   ("heldout_eval", "evaluation", "evaluate"), ("export", "export", "export")):
        nodes.append({"id": name, "kind": "component.run", "implementation_ref": {
            "module": "bound_release_harness", "entrypoint": entrypoint, "path": "harness.py",
            "commit": commit, "sha256": digest},
            "inputs": [{"name": "data", "type": "dict", "required": False}] +
                      ([] if role == "training" else [{"name": "checkpoint", "type": "Path"}]),
            "outputs": [{"name": "checkpoint" if role == "training" else "package" if role == "export" else "metrics",
                         "type": "dict" if role == "evaluation" else "Path"}] +
                       ([{"name": "metrics", "type": "dict"}] if role == "training" else []),
            "parameters": {"updates": 400} if role == "training" else {}, "environment_ref": "default",
            "resource_request": {"gpu": 0}, "cache_policy": "disabled", "timeout": 60,
            "retry": {"max_attempts": 1, "backoff_seconds": 0}, "provenance": {},
            "component": {"id": "authored.text-length." + entrypoint, "version": "1",
                "protocol": "modelbuilder.component/v1", "role": role, "parameter_schema": {"type": "object"},
                "capabilities": {"resume_modes": [], "checkpoint_events": False,
                                 "export_formats": ["pytorch"] if role == "export" else [], "frameworks": ["pytorch"]}}})
    return {"schema_version": "0", "name": "Authored bound dataset release", "revision": 1,
            "source": {"repository": "urn:sha256:" + digest, "commit": commit,
                       "files": [{"path": "harness.py", "sha256": digest}]}, "nodes": nodes,
            "edges": [{"source_node": "train", "source_port": "checkpoint", "target_node": node,
                       "target_port": "checkpoint"} for node in ("dev_eval", "heldout_eval", "export")],
            "resources": [], "external_dependencies": [],
            "environment": {"python": "3.12", "requirements": ["zenml==0.97.0"]}, "evaluation": {}, "outputs": []}


def dataset_manifest(root):
    root.mkdir()
    rows = [{"id": f"{split}-{i}", "text": "x" * i, "label": 2 * i + 1}
            for split, indices in (("train", range(1, 7)), ("dev", range(7, 9)), ("heldout", range(9, 11)))
            for i in indices]
    target = root / "lengths.jsonl"
    target.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return {"name": "Independent text lengths", "benchmark": "text-length-v1", "license": "CC0-1.0",
        "provenance": {"source": "authored acceptance samples", "source_revision": "1"},
        "sources": [{"id": "lengths", "root_id": "data", "path": target.name, "format": "jsonl",
                     "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}],
        "id_column": "id", "row_schema": {"type": "object", "additionalProperties": False,
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "label": {"type": "integer"}},
            "required": ["id", "text", "label"]},
        "splits": [{"id": split, "role": role, "sample_ids": [row["id"] for row in rows if row["id"].startswith(split)]}
                   for split, role in (("train", "train"), ("dev", "validation"), ("heldout", "test"))],
        "transformations": []}


def binding(artifact, node, port):
    return {"node_id": node, "input_port": port, **{key: artifact["binding_descriptor"][key]
        for key in ("backend_run_id", "source_node_id", "source_port", "artifact_id", "content_sha256")}}


def selector(run, node):
    evaluation = next(item for item in run["evaluations"] if item["node_id"] == node and item["metric"] == "mse")
    return {key: evaluation[key] for key in ("backend_run_id", "node_id", "output_port", "artifact_id", "metric")}


def tree_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("stale_evidence", [False, True])
def test_bound_dataset_checkpoint_evaluation_promotion_and_saved_release(tmp_path, stale_evidence, monkeypatch):
    source, data_root = tmp_path / "authored-model", tmp_path / "external-data"
    body, manifest = authored_recipe(source), dataset_manifest(data_root)
    _, origin = local_source_review(source, body["source"], policy_id="test-bound-source")
    body["extensions"] = {"usage_context": deepcopy(USAGE_CONTEXT), "artifact_rights": origin}
    if stale_evidence:
        for node in body["nodes"]:
            if node.get("component", {}).get("role") == "evaluation":
                node["parameters"]["claimed_checkpoint"] = "stale-unrelated-checkpoint-B"
    roots = {"authored": {"path": str(source), "trusted": True}}
    backend = ZenMLBackend(tmp_path / "native", python_executable=str(PYTHON))
    service = OperationService(tmp_path / "service.sqlite", backend, source_roots=roots)
    source_identity = service.source_workspaces.inspect("authored")
    project = service.invoke("project.create", {"slug": "bound-release", "model_name": "Text length model",
        "recipe": body, "source_alias": "authored", "source_repository_id": source_identity["repository_id"],
        "trusted": True, "idempotency_key": "project"}, OWNER)
    pid = project["project_id"]
    policy, _ = local_source_review(source, body["source"], pid, policy_id="test-bound-source")
    service.store.put("hub_license_policy", policy["policy_id"], pid, policy)
    service.store.close()
    service = OperationService(tmp_path / "service.sqlite", backend, source_roots=roots,
                               dataset_roots={pid: {"data": str(data_root)}})
    def call(operation, **payload):
        return service.invoke(operation, payload, OWNER)
    request = {"project_id": pid, "recipe_id": project["recipe_id"]}
    try:
        original_recipe = call("recipe.get", **request)
        pin = {**request, "expected_revision": 1, "expected_project_revision": project["revision"],
               "source_repository_id": source_identity["repository_id"], "commit": body["source"]["commit"],
               "paths": ["harness.py"]}
        review = call("recipe.source.pin.preview", **pin)
        pinned = call("recipe.source.pin", **pin, review_digest=review["review_digest"],
                      confirm_pin=True, idempotency_key="pin")
        assert pinned["revision"] == 2 and not pinned["execution_allowed"]
        version = call("dataset.register", project_id=pid, manifest=manifest, idempotency_key="dataset")
        review_local_dataset(service, version, data_root, OWNER)
        changed = deepcopy(manifest)
        changed["description"] = "Newer metadata must not redirect a pinned dataset revision"
        newer = call("dataset.version", project_id=pid, dataset_id=version["dataset_id"], expected_revision=1,
                     manifest=changed, idempotency_key="dataset-next")
        assert newer["revision"] == 2 and newer["version_id"] != version["version_id"]
        current = call("recipe.get", **request)
        for split, node, purpose in (("train", "train", "training"), ("dev", "dev_eval", "evaluation"),
                                      ("heldout", "heldout_eval", "evaluation")):
            payload = {**request, "expected_revision": current["revision"], "dataset_id": version["dataset_id"],
                "dataset_revision": 1, "split_id": split, "purpose": purpose, "node_id": split + "_data",
                "target_node": node, "target_port": "data", "idempotency_key": "bind-" + split}
            bound = call("dataset.bind", **payload)
            assert not bound["execution_started"] and call("dataset.bind", **payload) == bound
            assert bound["binding"]["version_id"] == version["version_id"]
            current = bound["recipe"]
        assert current["revision"] == 5
        assert call("run.list", project_id=pid)["runs"] == []
        assert str(source) not in json.dumps(current) and str(data_root) not in json.dumps(current)
        baseline_backend = (backend.repository, deepcopy(backend.dependency_paths))

        def execute(key, nodes, bindings=()):
            payload = {**request, "expected_revision": current["revision"], "trusted": True,
                "execution": {"node_ids": list(nodes), "input_bindings": list(bindings)}}
            before = set((backend.root / "jobs").glob("*"))
            plan = call("run.plan", **payload)
            assert plan["executable"], plan
            assert set((backend.root / "jobs").glob("*")) == before
            submitted = call("run.submit", **payload, idempotency_key=key)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                status = call("run.status", project_id=pid, operation_id=submitted["operation_id"])
                if status["status"] in {"succeeded", "failed", "cancelled"} and not status["worker_alive"] and not status["supervisor_alive"]:
                    break
                time.sleep(.25)
            else:
                raise AssertionError(f"Native run did not finish cleanly: {status}")
            job = backend.root / "jobs" / submitted["operation_id"]
            assert status["status"] == "succeeded", (status, (job / "worker.log").read_text()[-16000:])
            assert set(status["executed_node_ids"]) == set(nodes)
            assert not status.get("parent_run_id")
            assert status["source_binding"] == project["source_binding"]
            assert status["recipe_revision"] == current["revision"] and status["recipe_digest"] == current["digest"]
            saved = read_json(job / "request.json")
            assert saved["source_root"] == str(source) and saved["source_binding"] == project["source_binding"]
            assert (backend.repository, backend.dependency_paths) == baseline_backend
            assert not hasattr(backend, "dataset_source_guards")
            if any(node.endswith("_data") for node in nodes):
                assert saved["dataset_source_guards"]
            else:
                assert saved["dataset_source_guards"] == {}
            return status, job

        trained, training_job = execute("train-once", ["train_data", "train"])
        original_request = (training_job / "request.json").read_bytes()
        original_metrics = deepcopy(trained["metrics"])
        assert original_metrics["train_loss"] < original_metrics["initial_loss"] * .01
        assert original_metrics["updates"] == 400
        assert original_metrics["dataset_version_id"] == version["version_id"]
        original_outputs = tree_bytes(training_job / "outputs" / "train")
        original_version = call("model.version.get", project_id=pid, model_version_id=trained["model_version_ref"])
        report = read_json(training_job / "outputs/train/report.json")
        assert report["final_loss"] < report["initial_loss"] * .01
        assert set(report["actual_consumed_ids"]) == set(version["splits"][0]["sample_ids"])
        assert len(report["actual_consumed_ids"]) == 400 * 6
        assert report["consumption"]["version_id"] == version["version_id"]
        assert report["dataset"]["revision"] == "1"
        artifacts = call("run.artifacts", project_id=pid, operation_id=trained["operation_id"], include_binding_descriptors=True)["artifacts"]
        checkpoint = next(item for item in artifacts if item.get("node_id") == "train" and item["port"] == "checkpoint")
        checkpoint_bytes = tree_bytes(Path(checkpoint["uri"]))
        evaluations = []
        for index in range(2):
            run, job = execute(f"evaluate-{index}", ["dev_data", "heldout_data", "dev_eval", "heldout_eval"],
                [binding(checkpoint, node, "checkpoint") for node in ("dev_eval", "heldout_eval")])
            assert not (job / "outputs/train").exists()
            assert "mse" not in run["metrics"] and len(run["evaluations"]) == 2
            assert {item["model_version_ref"] for item in run["evaluations"]} == {None}
            assert all(item["checkpoint_verification"]["code"] == "checkpoint_verification_pending" for item in run["evaluations"])
            assert {item["record"]["split"]["role"] for item in run["evaluations"]} == {"validation", "test"}
            assert all(item["record"]["dataset"]["revision"] == "1" for item in run["evaluations"])
            assert all(item["checkpoint_bindings"][0]["artifact_id"] == checkpoint["artifact_id"] for item in run["evaluations"])
            evaluations.append(run)
        assert call("model.version.get", project_id=pid, model_version_id=trained["model_version_ref"]) == original_version
        run_ids = [run["backend_run_id"] for run in evaluations]
        common = {"project_id": pid, "run_ids": run_ids, "metric": "mse", "direction": "minimize"}
        assert not call("run.compare", **common)["compatible"]
        explicit = [selector(run, "dev_eval") for run in evaluations]
        compared = call("run.compare", **common, evaluations=explicit)
        if stale_evidence:
            assert not compared["compatible"] and compared["selected_model_version_ref"] is None
            assert any(item["code"] == "evaluation_checkpoint_unverified" for item in compared["diagnostics"])
            hypothesis = call("hypothesis.create", project_id=pid, proposed_change="Reject stale checkpoint evidence",
                expected_outcome="No unrelated model can be promoted", acceptance_metric={"name": "mse", "direction": "minimize"},
                idempotency_key="stale-hypothesis")
            # Even an old accepted decision without a comparison digest must be
            # freshly checked against actual native inputs at promotion time.
            decision = call("decision.record", **request, recipe_revision=current["revision"],
                hypothesis_id=hypothesis["hypothesis_id"], run_ids=run_ids,
                selection_rule={"metric": "mse", "direction": "minimize", "evaluations": explicit},
                outcome="accepted", rationale="Legacy decision must not bypass native attribution", idempotency_key="stale-decision")
            with pytest.raises(ServiceError) as error:
                call("model.version.promote", project_id=pid, model_version_id=trained["model_version_ref"],
                    decision_id=decision["decision_id"], expected_revision=project["revision"], stage="staging", idempotency_key="stale-promote")
            assert error.value.code == "incompatible_evaluation"
            assert call("model.version.get", project_id=pid, model_version_id=trained["model_version_ref"]) == original_version
            assert tree_bytes(training_job / "outputs/train") == original_outputs
            return
        assert compared["compatible"] and compared["selected_model_version_ref"] == trained["model_version_ref"]
        assert compared["selected_evaluation"] in explicit
        incompatible = call("run.compare", **common, evaluations=[explicit[0], selector(evaluations[1], "heldout_eval")])
        assert not incompatible["compatible"] and incompatible["selected_run_id"] is None
        hypothesis = call("hypothesis.create", project_id=pid, proposed_change="Reuse the trained checkpoint",
            expected_outcome="Reproduce validation performance without another training execution",
            acceptance_metric={"name": "mse", "direction": "minimize"}, idempotency_key="hypothesis")
        decision = call("decision.record", **request, recipe_revision=current["revision"],
            hypothesis_id=hypothesis["hypothesis_id"], run_ids=run_ids,
            selection_rule={"metric": "mse", "direction": "minimize", "evaluations": explicit,
                "comparison_contract": compared["comparison_contract"], "comparison_digest": compared["comparison_digest"]},
            outcome="accepted", rationale="Explicit native validation artifacts agree on one immutable checkpoint",
            idempotency_key="decision")
        # Hold the real backend preparation before its expensive native reads.
        # Live journal polls and an independent SQLite writer must still finish.
        entered, release = threading.Event(), threading.Event()
        original_prepare = type(backend).prepare_promotion
        def held_prepare(authorized_backend, payload, actor=None):
            entered.set()
            assert release.wait(20), "Preparation release timed out"
            return original_prepare(authorized_backend, payload, actor)
        monkeypatch.setattr(type(backend), "prepare_promotion", held_prepare)
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(call, "model.version.promote", project_id=pid,
                model_version_id=trained["model_version_ref"], decision_id=decision["decision_id"],
                expected_revision=project["revision"], stage="staging", idempotency_key="promote")
            try:
                assert entered.wait(10)
                call("run.events.list", project_id=pid, operation_id=trained["operation_id"])
                call("run.evaluations.list", project_id=pid, operation_id=trained["operation_id"])
                independent = OperationService(tmp_path / "service.sqlite", backend, source_roots=roots)
                try:
                    independent.invoke("hypothesis.create", {"project_id": pid, "proposed_change": "Independent writer",
                        "expected_outcome": "No database lock during native preparation", "acceptance_metric": {"name": "mse"},
                        "idempotency_key": "concurrent-writer"}, OWNER)
                finally:
                    independent.store.close()
                assert not future.done()
            finally:
                release.set()
            promoted = future.result(timeout=90)
        monkeypatch.setattr(type(backend), "prepare_promotion", original_prepare)
        assert promoted["model_version_id"] == trained["model_version_ref"]
        assert promoted["selected_run_id"] == compared["selected_run_id"]
        assert promoted["metadata"]["selection_decision"]["selection_rule"]["evaluations"] == explicit

        # Export keeps its own native run/version ownership and pins the original
        # trained model through an exact, verified checkpoint input artifact.
        artifacts = call("run.artifacts", project_id=pid, operation_id=evaluations[0]["operation_id"],
                         include_binding_descriptors=True)["artifacts"]
        heldout = next(item for item in artifacts if item.get("node_id") == "heldout_data")
        exported, export_job = execute("export-only", ["export"],
            [binding(checkpoint, "export", "checkpoint"), binding(heldout, "export", "data")])
        assert not (export_job / "outputs/train").exists()
        release = call("model.export.list", project_id=pid, backend_run_id=exported["backend_run_id"],
                       model_version_id=exported["model_version_ref"])
        assert len(release["exports"]) == 1 and release["diagnostics"] == [], release
        descriptor = release["exports"][0]
        selected = descriptor["provenance"]["selected_checkpoints"]
        assert selected == [{"artifact_id": checkpoint["artifact_id"], "input_port": "checkpoint",
                             "identity": descriptor["manifest"]["selected_checkpoint"]}]
        identity = {key: descriptor["provenance"][key] for key in ("project_id", "backend_run_id", "model_version_id", "artifact_id")}
        assert call("model.export.inspect", **identity) == descriptor
        # Cross-version access must not be relaxed to make downstream export work.
        with pytest.raises(ServiceError) as wrong_owner:
            call("model.export.inspect", **{**identity, "model_version_id": trained["model_version_ref"]})
        assert wrong_owner.value.code == "not_found"
        reader = Actor("package-reader", {"read"}, {pid})
        transfer = service.invoke("model.export.package", {**identity, "expected_export_digest": descriptor["export_digest"]}, reader)
        archive_path = tmp_path / "saved-model.zip"
        with archive_path.open("wb") as output:
            for index, expected in enumerate(transfer["chunk_sha256"]):
                chunk = service.invoke("model.export.read", {**identity, "package_id": transfer["package_id"], "chunk_index": index}, reader)
                content = base64.b64decode(chunk["data_base64"])
                assert hashlib.sha256(content).hexdigest() == expected
                output.write(content)
        assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == transfer["sha256"]
        extracted = tmp_path / "saved-model"
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(extracted)
        provenance = read_json(extracted / "modelbuilder.provenance.json")
        assert provenance["selected_checkpoints"] == selected
        assert provenance["source"] == current["source"]
        assert provenance["environment"] == current["environment"]
        assert provenance["recipe_id"] == current["recipe_id"]
        assert provenance["recipe_revision"] == current["revision"]
        assert provenance["recipe_digest"] == current["digest"]
        assert provenance["backend_run_id"] == exported["backend_run_id"]
        assert provenance["model_version_id"] == exported["model_version_ref"]
        # Promotion metadata belongs to the trained version, not the export version.
        assert not provenance["selection"].get("selection_decision")
        assert "selection_comparison_digest" not in provenance["selection"]
        assert provenance["execution"]["node_ids"] == ["export"]
        saved_checkpoint = next(item for item in provenance["execution"]["input_bindings"]
                                if item["input_port"] == "checkpoint")
        assert saved_checkpoint["artifact_id"] == checkpoint["artifact_id"]
        assert saved_checkpoint["backend_run_id"] == trained["backend_run_id"]
        assert saved_checkpoint["producer_model_version_ref"] == trained["model_version_ref"]
        assert saved_checkpoint["content_sha256"] == checkpoint["binding_descriptor"]["content_sha256"]
        expected = read_json(export_job / "outputs/export/parity.json")
        script = '''import importlib.util,json,sys,torch
from pathlib import Path
root=Path(sys.argv[1]); spec=importlib.util.spec_from_file_location('saved_loader',root/'loader.py'); module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
assert 'modelbuilder' not in sys.modules
with torch.no_grad(): actual=module.load(root)(torch.tensor([[.9],[1.]],dtype=torch.float32))
expected=torch.tensor(json.loads(sys.argv[2])); difference=float((actual-expected).abs().max());print(json.dumps({'max_abs_difference':difference}));assert difference==0.0
'''
        loaded = subprocess.run([str(PYTHON), "-I", "-c", script, str(extracted), json.dumps(expected["predictions"])],
                                cwd=tmp_path, capture_output=True, text=True, timeout=60)
        assert loaded.returncode == 0, loaded.stdout + loaded.stderr

        history = call("run.list", project_id=pid)["runs"]
        assert len(history) == 4 and sum("train" in item["executed_node_ids"] for item in history) == 1
        assert len({item["backend_run_id"] for item in history}) == 4
        assert len({item["model_version_ref"] for item in history}) == 4
        unchanged = next(item for item in history if item["backend_run_id"] == trained["backend_run_id"])
        assert unchanged["metrics"] == original_metrics and unchanged["status"] == "succeeded"
        assert unchanged["execution"] == trained["execution"]
        assert (training_job / "request.json").read_bytes() == original_request
        assert tree_bytes(training_job / "outputs/train") == original_outputs
        assert tree_bytes(Path(checkpoint["uri"])) == checkpoint_bytes
        assert call("recipe.get", **request, revision=1) == original_recipe
        assert call("recipe.get", **request, revision=current["revision"]) == current
        assert call("dataset.get", project_id=pid, dataset_id=version["dataset_id"], revision=1) == version
        evidence = {"training_run": trained["backend_run_id"], "evaluation_runs": run_ids,
            "export_run": exported["backend_run_id"], "trained_model_version": trained["model_version_ref"],
            "export_model_version": exported["model_version_ref"], "selected_checkpoint": selected,
            "dataset_version": version["version_id"], "comparison_digest": compared["comparison_digest"],
            "decision_id": decision["decision_id"], "package_sha256": transfer["sha256"],
            "fresh_reload": json.loads(loaded.stdout), "training_executions": 1,
            "original_request_metrics_checkpoint_unchanged": True}
        (tmp_path / "bound-release-evidence.json").write_text(json.dumps(evidence, indent=2))
        print(json.dumps(evidence))
    finally:
        service.store.close()
