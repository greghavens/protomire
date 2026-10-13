"""Independent scratch-regression harness; not installed as platform training code.

Training owns its native PyTorch model and optimizer. Data and held-out evaluation
use the standard library so their environment demonstrably need not contain Torch.
"""
import hashlib
import json
import os
from pathlib import Path
import sys


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def data(*, inputs, parameters, context):
    assert "torch" not in sys.modules
    return {"data": {"rows": [[1., 2., 5.], [2., 1., 4.], [0., 1., 2.], [3., 1., 5.]],
                     "train_ids": ["0", "1", "2"], "test_ids": ["3"], "producer_pid": os.getpid()}}


def train(*, inputs, parameters, context):
    import torch
    torch.set_num_threads(1)
    torch.manual_seed(30)
    assert context["device"] == "cpu"
    model = torch.nn.Linear(2, 1, bias=False).to("cpu")
    if context.get("resume"):
        assert context["resume_mode"] == "warm_start"
        values = json.loads((Path(context["resume"]["path"]) / "weights.json").read_text())
        with torch.no_grad():
            model.weight.copy_(torch.tensor([values["weights"]]))
    initial = model.weight.detach().clone()
    optimizer = torch.optim.SGD(model.parameters(), lr=0.03)
    train_rows = [inputs["data"]["rows"][int(index)] for index in inputs["data"]["train_ids"]]
    x, y = torch.tensor([row[:2] for row in train_rows]), torch.tensor([[row[2]] for row in train_rows])
    for _ in range(3):
        optimizer.zero_grad()
        loss = torch.nn.functional.mse_loss(model(x), y)
        loss.backward()
        optimizer.step()
    changed = float((model.weight.detach() - initial).abs().sum())
    assert changed > 0
    output = Path(context["output_dir"])
    values = {"weights": model.weight.detach().tolist()[0], "pid": os.getpid(),
              "python": sys.executable, "changed": changed, "loss": float(loss.detach())}
    (output / "weights.json").write_text(json.dumps(values))
    manifest = {"schema_version": "modelbuilder.checkpoint/v1", "purpose": "last", "step": 3,
        "identities": context["identities"], "rights": context["declared_artifact_rights"], "resume_modes": ["warm_start"], "state": ["model"],
        "assets": [{"path": "weights.json", "sha256": hashlib.sha256((output / "weights.json").read_bytes()).hexdigest()}]}
    (output / "checkpoint.manifest.json").write_text(json.dumps(manifest))
    return {"trained": output}


def evaluate(*, inputs, parameters, context):
    assert "torch" not in sys.modules
    trained = inputs["trained"]
    weights = json.loads((trained / "weights.json").read_text())
    checkpoint = json.loads((trained / "checkpoint.manifest.json").read_text())
    dataset = inputs["data"]
    test = dataset["rows"][3]
    prediction = sum(a * b for a, b in zip(weights["weights"], test[:2]))
    loss = (prediction - test[2]) ** 2
    record = {"schema": "modelbuilder.evaluation/v1", "value": loss,
        "dataset": {"id": "scratch-regression", "revision": "1", "content_digest": digest(dataset["rows"]),
                    "benchmark": "independent-scratch-regression"},
        "split": {"id": "test", "role": "test", "sample_ids": dataset["test_ids"]},
        "protocol": {"implementation": "independent-harness", "version": "1", "configuration": {},
                     "preprocessing": {}, "seed": 30},
        "metric": {"name": "mse", "implementation": "squared-error", "version": "1", "parameters": {},
                   "aggregation": "mean", "direction": "minimize"},
        "treatment": {"model": "scratch-linear", "checkpoint": digest(checkpoint), "calibration": None,
                      "reference": None}}
    record["digest"] = digest(record)
    return {"metrics": {"mse": loss, "evaluation_records": {"mse": record}, "python": sys.executable,
                        "pid": os.getpid(), "torch_imported": False, "parameter_change": weights["changed"]}}
