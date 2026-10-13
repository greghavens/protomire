"""Independent acceptance workload copied to a separate user Git repository by tests.

This file is not installed in Modelbuilder and imports no modelbuilder modules.
It owns the model, loss, optimizer, scheduler, serialization and inference code.
"""
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import torch
from torch import nn


class Segmenter(nn.Module):
    def __init__(self, width):
        super().__init__()
        self.stem = nn.Conv2d(1, width, 3, padding=1)
        self.residual = nn.Conv2d(width, width, 3, padding=1)
        self.head = nn.Conv2d(width, 1, 1)

    def forward(self, x):
        hidden = self.stem(x).relu()
        return self.head((hidden + self.residual(hidden)).relu())


class Forecaster(nn.Module):
    def __init__(self, width):
        super().__init__()
        self.gru = nn.GRU(3, width, batch_first=True)
        self.quantiles = nn.Linear(width, 3)

    def forward(self, x):
        _, hidden = self.gru(x)
        return self.quantiles(hidden[-1])


def setup(config):
    torch.manual_seed(421)
    torch.set_num_threads(1)
    generator = torch.Generator().manual_seed(911)
    if config["task"] == "image":
        model = Segmenter(config["width"])
        samples = [torch.randn(1, 1, 16, 16, generator=generator) for _ in range(32)]
        targets = [(x > 0).float() for x in samples]
    else:
        model = Forecaster(config["width"])
        samples = [torch.randn(1, 8 + i % 13, 3, generator=generator) for i in range(48)]
        targets = [x[:, -3:, :].mean().reshape(1, 1) for x in samples]
    return model, samples, targets


def loss_for(prediction, target, config):
    if config["task"] == "image":
        p = prediction.sigmoid()
        dice = 1 - (2 * (p * target).sum() + 1) / (p.sum() + target.sum() + 1)
        return nn.functional.binary_cross_entropy_with_logits(prediction, target) + config["loss_weight"] * dice
    residual = target - prediction
    quantiles = torch.tensor([0.1, 0.5, 0.9])
    return torch.maximum(quantiles * residual, (quantiles - 1) * residual).mean() * config["loss_weight"]


def assets(directory, names):
    return [{"path": name, "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest()} for name in names]


def lineage(context):
    # Explicit reviewed declarations and resumed parents; no inferred ownership.
    parents = [context.get("declared_artifact_rights")]
    if context.get("resume"):
        parents.append(json.loads((Path(context["resume"]["path"]) / "checkpoint.manifest.json").read_text()).get("rights"))
    sources, unresolved, digests = {}, [], set()
    for index, parent in enumerate(parents):
        if parent is None:
            unresolved.append({"artifact_id": "independent-origin-" + str(index), "reason": "No reviewed local-origin evidence"})
            continue
        def canonical(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        digests.add(canonical(parent))
        digests.update(parent["parent_rights_digests"])
        unresolved.extend(parent["unresolved"])
        sources.update({canonical(source): source for source in parent["sources"]})
    return {"schema_version": "modelbuilder.artifact-rights/v1", "sources": [sources[key] for key in sorted(sources)],
            "unresolved": unresolved, "parent_rights_digests": sorted(digests)}


def train(*, inputs, parameters, context):
    config = parameters["architecture"]
    model, xs, ys = setup(config)
    optimizer = torch.optim.AdamW(model.parameters(), lr=parameters["learning_rate"])
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=2, gamma=0.7)
    initial = {k: v.clone() for k, v in model.state_dict().items()}
    losses, start = [], 0
    if context.get("resume"):
        saved = torch.load(Path(context["resume"]["path"]) / "complete-state.bin", weights_only=True)
        model.load_state_dict(saved["model"])
        if context["resume_mode"] == "exact_state":
            optimizer.load_state_dict(saved["optimizer"])
            scheduler.load_state_dict(saved["scheduler"])
            torch.set_rng_state(saved["rng"])
            losses, start = saved["history"], saved["data_cursor"]
    output = Path(context["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    (output / "configuration.json").write_text(json.dumps(config))
    for i in range(start, parameters["steps"]):
        optimizer.zero_grad()
        loss = loss_for(model(xs[i]), ys[i], config)
        loss.backward()
        assert model.stem.weight.grad is not None if config["task"] == "image" else model.gru.weight_ih_l0.grad is not None
        optimizer.step()
        scheduler.step()
        losses.append(float(loss.detach()))
        changed = sum(float((initial[k] - value).abs().sum()) for k, value in model.state_dict().items())
        assert changed > 0
        torch.save(model.state_dict(), output / "inference-tensors.bin")
        state = {"model": model.state_dict(), "optimizer": optimizer.state_dict(), "scheduler": scheduler.state_dict(),
                 "rng": torch.get_rng_state(), "data_cursor": i + 1, "history": losses}
        torch.save(state, output / "complete-state.bin")
        manifest = {"schema_version": "modelbuilder.checkpoint/v1", "purpose": "last", "step": i + 1,
                    "resume_modes": ["exact_state", "warm_start"], "state": ["model", "optimizer", "scheduler", "rng", "data_cursor"],
                    "identities": context["identities"], "assets": assets(output, ["inference-tensors.bin", "configuration.json", "complete-state.bin"]),
                    "rights": lineage(context), "metrics": {"final_loss": losses[-1], "parameter_change": changed}, "history": losses}
        temporary = output / "checkpoint.manifest.partial"
        temporary.write_text(json.dumps(manifest))
        temporary.replace(output / "checkpoint.manifest.json")
        # The mutable root is a convenient final output, not a durable prior
        # checkpoint: the next step overwrites its assets. Preserve each ready
        # step in a fresh immutable directory; publish its manifest only after
        # every asset copy closes. Recovery ignores an interrupted partial step.
        snapshot = output / "checkpoints" / f"{i + 1:08d}"
        snapshot.mkdir(parents=True)
        for asset in manifest["assets"]:
            shutil.copyfile(output / asset["path"], snapshot / asset["path"])
        committed = snapshot / "checkpoint.manifest.partial"
        committed.write_text(json.dumps(manifest))
        committed.replace(snapshot / "checkpoint.manifest.json")
        if context.get("checkpoint_ready"):
            context["checkpoint_ready"](path=snapshot, step=i + 1, purpose="last")
        time.sleep(parameters.get("step_delay", 0))
    return {"trained": output}


def load(directory):
    config = json.loads((directory / "configuration.json").read_text())
    model, xs, ys = setup(config)
    model.load_state_dict(torch.load(directory / "inference-tensors.bin", weights_only=True))
    model.eval()
    return model, xs, ys, config


def evaluate(*, inputs, parameters, context):
    model, xs, ys, config = load(inputs["trained"])
    with torch.no_grad():
        losses = [float(nn.functional.binary_cross_entropy_with_logits(model(x), y) if config["task"] == "image"
                        else nn.functional.mse_loss(model(x)[:, 1:2], y)) for x, y in zip(xs[-4:], ys[-4:])]
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    sample_ids = [str(i) for i in range(len(xs) - 4, len(xs))]
    checkpoint = json.loads((inputs["trained"] / "checkpoint.manifest.json").read_text())
    value = sum(losses) / len(losses)
    record = {"schema": "modelbuilder.evaluation/v1",
        "dataset": {"id": "independent-" + config["task"], "revision": "1", "benchmark": "synthetic-independent",
                    "content_digest": digest([x.tolist() for x in xs])},
        "split": {"id": "heldout", "role": "test", "sample_ids": sample_ids},
        "protocol": {"implementation": "independent.evaluate", "version": "1", "configuration": {"task": config["task"]}, "preprocessing": {}, "seed": 911},
        "metric": {"name": "loss", "implementation": "pytorch.bce_logits" if config["task"] == "image" else "pytorch.mse",
                   "version": torch.__version__, "parameters": {}, "aggregation": "mean", "direction": "minimize"},
        "treatment": {"model": checkpoint["identities"], "checkpoint": digest(checkpoint), "calibration": None, "reference": None},
        "value": value, "predictions": [{"item_id": item_id, "value": loss} for item_id, loss in zip(sample_ids, losses)]}
    record["digest"] = digest(record)
    return {"metrics": {"loss": value, "evaluation_records": {"loss": record}}}


def export(*, inputs, parameters, context):
    selected = inputs["trained"]
    output = Path(context["output_dir"])
    for name in ("inference-tensors.bin", "configuration.json"):
        shutil.copy2(selected / name, output / name)
    shutil.copy2(Path(__file__), output / "loader.py")
    model, xs, _, _ = load(selected)
    with torch.no_grad():
        expected = model(xs[-1]).tolist()
    (output / "expected.json").write_text(json.dumps(expected))
    subprocess.run([sys.executable, str(output / "loader.py"), str(output)], check=True, capture_output=True)
    verification = json.loads((output / "verification.json").read_text())
    descriptor = {"schema_version": "modelbuilder.export/v1", "selected_checkpoint": hashlib.sha256(json.dumps(json.loads((selected / "checkpoint.manifest.json").read_text()), sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                  "loader": {"path": "loader.py", "entrypoint": "load", "format": "pytorch-state-dict"},
                  "signature": {"input": "float32 tensor", "output": "float32 tensor"},
                  "dependencies": {"torch": torch.__version__}, "source": context["source"], "verification": verification,
                  "assets": assets(output, ["loader.py", "inference-tensors.bin", "configuration.json", "expected.json", "verification.json"])}
    descriptor["rights"] = json.loads((selected / "checkpoint.manifest.json").read_text())["rights"]
    (output / "export.manifest.json").write_text(json.dumps(descriptor))
    return {"exported": output}


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    model, xs, _, _ = load(directory)
    with torch.no_grad():
        actual = model(xs[-1])
    expected = torch.tensor(json.loads((directory / "expected.json").read_text()))
    torch.testing.assert_close(actual, expected, atol=1e-7, rtol=0)
    (directory / "verification.json").write_text(json.dumps({"passed": True, "atol": 1e-7, "max_abs_difference": float((actual - expected).abs().max()), "fresh_process": True}))
