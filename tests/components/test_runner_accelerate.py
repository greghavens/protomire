"""Real non-Trainer Accelerate API training through the pinned runner boundary."""
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from modelbuilder.backend.components import read_checkpoint
from modelbuilder.backend.process_execution import execute
from test_runner_contract import setup, spec_for

ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(os.environ.get("MODELBUILDER_RUNNER_TEST_PYTHON", ROOT / ".venv-backend/bin/python"))
pytestmark = [pytest.mark.integration, pytest.mark.skipif(sys.platform != "linux", reason="Linux CPU runner acceptance"),
              pytest.mark.skipif(not PYTHON.is_file(), reason="Existing pinned CPU/Accelerate interpreter required")]


HARNESS = '''import hashlib,json
from pathlib import Path

def run(*, inputs, parameters, context):
    import torch
    from accelerate import Accelerator
    from accelerate.optimizer import AcceleratedOptimizer
    from safetensors.torch import save_model
    torch.set_num_threads(1)
    torch.manual_seed(712)
    accelerator = Accelerator(cpu=True, mixed_precision="no", gradient_accumulation_steps=2)
    assert accelerator.num_processes == 1
    assert str(accelerator.device) == context["device"] == "cpu"
    model = torch.nn.Sequential(torch.nn.Linear(4, 3), torch.nn.Tanh(), torch.nn.Linear(3, 1))
    initial = [parameter.detach().clone() for parameter in model.parameters()]
    optimizer = torch.optim.AdamW(model.parameters(), lr=.03)
    xs = torch.tensor(inputs["data"]["features"], dtype=torch.float32)
    ys = torch.tensor(inputs["data"]["targets"], dtype=torch.float32)
    data = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(xs, ys), batch_size=1, shuffle=False)
    model, optimizer, data = accelerator.prepare(model, optimizer, data)
    assert isinstance(optimizer, AcceleratedOptimizer)
    updates = 0
    for features, target in data:
        with accelerator.accumulate(model):
            loss = torch.nn.functional.mse_loss(model(features), target)
            accelerator.backward(loss)
            optimizer.step()
            optimizer.zero_grad()
            updates += int(accelerator.sync_gradients)
    raw_model = accelerator.unwrap_model(model)
    delta = sum(float((before - after.detach()).abs().sum()) for before, after in zip(initial, raw_model.parameters()))
    assert delta > 0 and updates == 2
    output = Path(context["output_dir"])
    save_model(raw_model, str(output / "weights.safetensors"))
    manifest = {"schema_version":"modelbuilder.checkpoint/v1", "purpose":"last", "step":updates,
        "identities":context["identities"], "resume_modes":["warm_start"], "state":["model"],
        "assets":[{"path":"weights.safetensors", "sha256":hashlib.sha256((output / "weights.safetensors").read_bytes()).hexdigest()}],
        "metrics":{"parameter_delta_l1":delta, "accelerator_device":str(accelerator.device),
                   "accelerator_processes":accelerator.num_processes, "native_optimizer":"AcceleratedOptimizer",
                   "updates":updates, "source_owns_optimizer":True}}
    (output / "checkpoint.manifest.json").write_text(json.dumps(manifest))
    accelerator.end_training()
    return {"trained":output}
'''


def test_actual_accelerate_prepare_backward_and_native_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    backend, recipe, profile = setup(tmp_path, code=HARNESS, python=str(PYTHON))
    node = recipe["nodes"][0]
    node["component"].update(role="training")
    node["component"]["capabilities"].update(frameworks=["pytorch"], resume_modes=["warm_start"])
    node["component"]["runner"]["stack"] = [
        {"id": "pytorch", "category": "framework", "distribution": "torch", "version": profile["packages"]["torch"]},
        {"id": "accelerate", "category": "launcher", "distribution": "accelerate", "version": profile["packages"]["accelerate"]}]
    node["inputs"] = [{"name": "data", "type": "dict"}]
    node["outputs"] = [{"name": "trained", "type": "Path"}]
    specification = spec_for(tmp_path, backend, recipe)
    result = execute("component", {"data": {"features": [[1, 0, 2, 1], [0, 1, 2, 0], [1, 2, 0, 1], [2, 0, 1, 1]],
                                           "targets": [[1], [2], [0], [1]]}}, specification)
    checkpoint = read_checkpoint(result)
    assert checkpoint["metrics"]["parameter_delta_l1"] > 0
    assert checkpoint["metrics"]["updates"] == 2
    assert checkpoint["metrics"]["native_optimizer"] == "AcceleratedOptimizer"
    assert checkpoint["metrics"]["accelerator_device"] == "cpu"
    assert checkpoint["resume_modes"] == ["warm_start"] and checkpoint["state"] == ["model"]
    assert hashlib.sha256((result / "weights.safetensors").read_bytes()).hexdigest() == checkpoint["assets"][0]["sha256"]
    owner = json.loads(next((tmp_path / "job/processes").glob("*/owner.json")).read_text())
    assert owner["reaped"] and owner["exit_code"] == 0 and not owner["descendant_cleanup"]["remaining_pids"]
