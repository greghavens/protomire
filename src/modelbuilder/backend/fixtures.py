"""Two deliberately tiny CPU examples, not a model-training framework.

Only this module owns model mathematics. ZenML adapter functions orchestrate it.
Checkpoints are trusted local artifacts and loaded with weights_only=True.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import shutil
import time
from pathlib import Path

import torch
from torch import nn

MODEL_ID = "prajjwal1/bert-tiny"
MODEL_REVISION = "6f75de8b60a9f8a2fdf7b69cbd86d9e64bcb3837"
MODEL_FILES = {
    "config.json": "cf3179684f7c360dd6185962d480e7d426ceb8fe49f5db7b64bdc15dba0b8798",
    "vocab.txt": "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3",
    "pytorch_model.bin": "dab2c2bddcfb48ea430ef63fd76d46d67d704487844d967256a50dd7d7fd0a66",
}


def canonical_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def model_directory() -> Path:
    default = Path(__file__).resolve().parents[3] / ".cache/models/bert-tiny"
    directory = Path(os.environ.get("MODELBUILDER_PRETRAINED_PATH", str(default)))
    for name, digest in MODEL_FILES.items():
        path = directory / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Pinned pretrained dependency missing or hash mismatch: {name}; run scripts/download_model.py")
    return directory


class GatedResidual(nn.Module):
    def __init__(self, width: int):
        super().__init__()
        self.content = nn.Linear(width, width)
        self.gate = nn.Linear(width, width)

    def forward(self, x):
        return x + self.content(x).tanh() * self.gate(x).sigmoid()


class CustomModel(nn.Module):
    def __init__(self, width: int = 12):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(6, width), nn.Tanh(), GatedResidual(width), nn.Dropout(0.08), nn.Linear(width, 2))

    def forward(self, x):
        return self.net(x)


def setup(seed: int) -> None:
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    torch.manual_seed(seed)
    random.seed(seed)


def dataset(parameters: dict) -> dict:
    fixture = parameters.get("fixture", "custom")
    if fixture == "custom":
        seed, count = int(parameters.get("seed", 1234)), int(parameters.get("count", 240))
        if count < 40 or count > 10000:
            raise ValueError("Fixture dataset count must be between 40 and 10000")
        x = torch.randn(count, 6, generator=torch.Generator().manual_seed(seed))
        y = (x[:, 0] * x[:, 1] + 0.6 * x[:, 2] - 0.4 * x[:, 3] > 0).long()
        train_n, val_n = count * 3 // 4, count // 8
        data = {"fixture": fixture, "seed": seed, "x": x.tolist(), "labels": y.tolist(),
                "train_ids": list(range(train_n)), "validation_ids": list(range(train_n, train_n + val_n)),
                "heldout_ids": list(range(train_n + val_n, count)),
                "tensor_sha256": hashlib.sha256(x.numpy().tobytes() + y.numpy().tobytes()).hexdigest()}
    elif fixture == "pretrained":
        from transformers import BertTokenizer
        tokenizer = BertTokenizer.from_pretrained(str(model_directory()), local_files_only=True, do_lower_case=True)
        texts, labels = [], []
        for adjective, label in [("wonderful", 1), ("excellent", 1), ("great", 1), ("pleasant", 1),
                                  ("awful", 0), ("terrible", 0), ("bad", 0), ("dreadful", 0)]:
            for noun in ["movie", "meal", "book", "experience", "story", "performance"]:
                texts.append(f"This {noun} was {adjective}.")
                labels.append(label)
        tokens = tokenizer(texts, padding=True, truncation=True, max_length=20, return_tensors="pt")
        assert len(tokenizer) == 30522
        assert torch.unique(tokens["input_ids"], dim=0).shape[0] == len(texts)
        data = {"fixture": fixture, "texts": texts, "labels": labels,
                "tokens": {k: v.tolist() for k, v in tokens.items()}, "vocabulary_size": len(tokenizer),
                "train_ids": [i for i in range(48) if i % 6 < 4],
                "validation_ids": [i for i in range(48) if i % 6 == 4],
                "heldout_ids": [i for i in range(48) if i % 6 == 5],
                "model_id": MODEL_ID, "model_revision": MODEL_REVISION,
                "upstream_weights_sha256": MODEL_FILES["pytorch_model.bin"]}
    else:
        raise ValueError(f"Unsupported example fixture: {fixture}")
    return {**data, "sha256": canonical_hash(data)}


def verify_data(data: dict) -> str:
    actual = canonical_hash({k: v for k, v in data.items() if k != "sha256"})
    if actual != data["sha256"]:
        raise ValueError("Consumed upstream dataset artifact hash mismatch")
    return actual


def build_model(fixture: str, width: int):
    if fixture == "custom":
        return CustomModel(width)
    if fixture != "pretrained":
        raise ValueError("Only explicit custom or pretrained fixture implementations are available")
    from transformers import BertForSequenceClassification
    return BertForSequenceClassification.from_pretrained(str(model_directory()), num_labels=2,
                                                         local_files_only=True, use_safetensors=False)


def atomic_save(value: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    torch.save(value, temporary)
    os.replace(temporary, path)


def load_checkpoint(path: Path, selected: bool = False) -> dict:
    if path.is_dir():
        path = path / ("best.pt" if selected else "checkpoint.pt")
    return torch.load(path, map_location="cpu", weights_only=True)


def initialize(data: dict, parameters: dict, out: Path) -> Path:
    verify_data(data)
    fixture = parameters.get("fixture")
    if fixture not in {"custom", "pretrained"}:
        raise ValueError("model.initialize parameters.fixture must explicitly select custom or pretrained")
    if data.get("fixture") != fixture:
        raise ValueError("The selected model initialization is incompatible with the supplied dataset representation")
    width = int(parameters.get("width", 12))
    if width < 1 or width > 512:
        raise ValueError("Custom fixture width must be between 1 and 512")
    if fixture == "pretrained" and width != 12:
        raise ValueError("Pretrained architecture is pinned; width changes apply only to the custom model")
    seed = int(parameters.get("seed", 701 if fixture == "pretrained" else 44))
    setup(seed)
    model = build_model(fixture, width)
    out.mkdir(parents=True, exist_ok=True)
    atomic_save({"fixture": fixture, "width": width, "seed": seed, "model": model.state_dict(),
                 "reference": copy.deepcopy(model.state_dict()), "step": 0,
                 "torch_rng": torch.get_rng_state(), "python_rng": random.getstate(),
                 "dataset_sha256": data["sha256"],
                 "initialization": {"fixture": fixture, "width": width if fixture == "custom" else None,
                                    "model_id": MODEL_ID if fixture == "pretrained" else None,
                                    "model_revision": MODEL_REVISION if fixture == "pretrained" else None}}, out / "checkpoint.pt")
    return out


def _batch(data: dict, indices: torch.Tensor):
    if data["fixture"] == "custom":
        return torch.tensor(data["x"], dtype=torch.float32)[indices]
    return {k: torch.tensor(v, dtype=torch.long)[indices] for k, v in data["tokens"].items()}


def _logits(model, batch):
    return model(**batch).logits if isinstance(batch, dict) else model(batch)


def train(data: dict, initial: Path, parameters: dict, out: Path, resume: Path | None = None) -> Path:
    digest = verify_data(data)
    mode = parameters.get("resume_mode", "exact_state")
    selected = parameters.get("checkpoint_kind", "last") == "best"
    ck = load_checkpoint(resume or initial, selected=selected and resume is not None)
    fixture, width = ck["fixture"], int(parameters.get("width", ck["width"]))
    if data.get("fixture") != fixture or ck["width"] != width or ck["dataset_sha256"] != digest:
        raise ValueError("Checkpoint architecture/dataset identity mismatch")
    setup(ck["seed"])
    model = build_model(fixture, width)
    model.load_state_dict(ck["model"])
    reference = copy.deepcopy(model).eval()
    reference.load_state_dict(ck["reference"])
    for parameter in reference.parameters():
        parameter.requires_grad_(False)
    lr = float(parameters.get("learning_rate", 0.001 if fixture == "pretrained" else 0.025))
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr) if fixture == "pretrained" else torch.optim.Adam(model.parameters(), lr=lr)
    scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=float(parameters.get("scheduler_gamma", 0.99)))
    kl_config = parameters.get("kl", {})
    coefficient = float(kl_config.get("coefficient", 0.0)) if kl_config.get("enabled", False) else 0.0
    direction = kl_config.get("direction", "model_to_reference" if fixture == "pretrained" else "reference_to_model")
    if direction not in {"reference_to_model", "model_to_reference", "forward", "reverse"}:
        raise ValueError("Unsupported explicit KL direction")
    if coefficient and (kl_config.get("frozen") is not True or not kl_config.get("reference_model_ref")):
        raise ValueError("Enabled KL requires a declared frozen reference")
    train_ids = torch.tensor(data["train_ids"], dtype=torch.long)
    val_ids = torch.tensor(data["validation_ids"], dtype=torch.long)
    y = torch.tensor(data["labels"], dtype=torch.long)
    batch_size = min(int(parameters.get("batch_size", len(train_ids))), len(train_ids))
    start, epoch, cursor, order = 0, 0, 0, []
    best, best_step, history = float("inf"), 0, []
    if resume and mode == "exact_state":
        for field in ("optimizer", "scheduler", "torch_rng", "python_rng", "order", "cursor", "epoch"):
            if field not in ck:
                raise ValueError(f"Exact continuation checkpoint missing {field}")
        expected = {"coefficient": coefficient, "direction": direction, "batch_size": batch_size, "learning_rate": lr,
                    "scheduler_gamma": float(parameters.get("scheduler_gamma", 0.99))}
        if ck["training_contract"] != expected:
            raise ValueError("Exact continuation training contract differs from parent; use warm_start explicitly")
        optimizer.load_state_dict(ck["optimizer"])
        scheduler.load_state_dict(ck["scheduler"])
        start, epoch, cursor, order = ck["step"], ck["epoch"], ck["cursor"], ck["order"]
        best, best_step, history = ck["best_validation_loss"], ck["best_step"], ck["history"]
    elif resume and mode != "warm_start":
        raise ValueError("resume_mode must be exact_state or warm_start")
    torch.set_rng_state(ck["torch_rng"])
    random.setstate(ck["python_rng"])
    out.mkdir(parents=True, exist_ok=True)
    if resume and mode == "exact_state" and (Path(resume) / "best.pt").is_file():
        shutil.copy2(Path(resume) / "best.pt", out / "best.pt")
    steps = int(parameters.get("steps", 18 if fixture == "pretrained" else 30))
    if steps <= start or steps > 100000:
        raise ValueError("Final steps must exceed checkpoint step and remain within fixture bound")
    contract = {"coefficient": coefficient, "direction": direction, "batch_size": batch_size, "learning_rate": lr,
                "scheduler_gamma": float(parameters.get("scheduler_gamma", 0.99))}
    for step_index in range(start, steps):
        if not order or cursor >= len(order):
            order = torch.randperm(len(train_ids)).tolist()
            cursor, epoch = 0, epoch + 1
        ids = train_ids[torch.tensor(order[cursor:cursor + batch_size])]
        cursor += len(ids)
        model.train()
        optimizer.zero_grad()
        logits = _logits(model, _batch(data, ids))
        with torch.no_grad():
            ref_logits = _logits(reference, _batch(data, ids))
        logp, logq = logits.log_softmax(-1), ref_logits.log_softmax(-1)
        if direction in {"reference_to_model", "forward"}:
            kl = (logq.exp() * (logq - logp)).sum(-1).mean()
        else:
            kl = (logp.exp() * (logp - logq)).sum(-1).mean()
        ce = nn.functional.cross_entropy(logits, y[ids])
        # Exercise Python RNG as well as dropout/minibatch Torch RNG in exact-resume tests.
        loss = ce + coefficient * kl + logits.square().mean() * (0.0004 + random.random() * 0.0002)
        if parameters.get("intentional_failure_step") == step_index + 1:
            raise ValueError("Intentional fixture failure for preserved failure evidence")
        loss.backward()
        optimizer.step()
        scheduler.step()
        model.eval()
        with torch.no_grad():
            validation_loss = float(nn.functional.cross_entropy(_logits(model, _batch(data, val_ids)), y[val_ids]))
        history.append({"step": step_index + 1, "loss": float(loss.detach()), "cross_entropy": float(ce.detach()),
                        "kl": float(kl.detach()), "validation_loss": validation_loss})
        is_best = validation_loss < best
        if is_best:
            best, best_step = validation_loss, step_index + 1
        state = {"fixture": fixture, "width": width, "seed": ck["seed"], "model": model.state_dict(),
                 "reference": reference.state_dict(), "optimizer": optimizer.state_dict(), "scheduler": scheduler.state_dict(),
                 "torch_rng": torch.get_rng_state(), "python_rng": random.getstate(), "step": step_index + 1,
                 "epoch": epoch, "cursor": cursor, "order": order, "training_contract": contract,
                 "dataset_sha256": digest, "best_validation_loss": best, "best_step": best_step, "history": history,
                 "resume_mode": mode if resume else None, "start_step": start, "initialization": ck.get("initialization")}
        atomic_save(state, out / "checkpoint.pt")
        if is_best:
            atomic_save(state, out / "best.pt")
        if parameters.get("_checkpoint_ready") and (step_index + 1) % parameters.get("checkpoint_interval_steps", 1) == 0:
            parameters["_checkpoint_ready"](path=out, step=step_index + 1, purpose="last")
        print(f"CHECKPOINT step={step_index + 1} validation_loss={validation_loss:.8f}", flush=True)
        cancel_file = parameters.get("_cancel_file")
        if cancel_file and Path(cancel_file).exists():
            raise InterruptedError("Cancellation requested; complete training-state checkpoint saved")
        time.sleep(float(parameters.get("step_delay", 0)))
    return out


def evaluate(data: dict, checkpoint: Path, parameters: dict, out: Path) -> dict:
    digest = verify_data(data)
    selected = parameters.get("checkpoint_kind", "best") == "best"
    ck = load_checkpoint(checkpoint, selected=selected)
    setup(ck["seed"])
    model = build_model(ck["fixture"], ck["width"]).eval()
    model.load_state_dict(ck["model"])
    ids, val_ids = torch.tensor(data["heldout_ids"]), torch.tensor(data["validation_ids"])
    labels = torch.tensor(data["labels"])
    with torch.no_grad():
        logits = _logits(model, _batch(data, ids))
        probabilities = logits.softmax(-1)
        validation_loss = float(nn.functional.cross_entropy(_logits(model, _batch(data, val_ids)), labels[val_ids]))
        accuracy = float((probabilities.argmax(-1) == labels[ids]).float().mean())
        brier = float(((probabilities[:, 1] - labels[ids].float()) ** 2).mean())
    result = {"fixture": ck["fixture"], "width": ck["width"], "step": ck["step"], "start_step": ck["start_step"],
              "validation_loss": validation_loss, "best_validation_loss": ck["best_validation_loss"],
              "best_step": ck["best_step"], "accuracy": accuracy, "brier": brier,
              "dataset_sha256": digest, "consumed_dataset_artifact": True, "kl": ck["training_contract"],
              "train_count": len(data["train_ids"]), "validation_count": len(data["validation_ids"]),
              "heldout_count": len(data["heldout_ids"]), "heldout_logits": logits.tolist(),
              "resume_mode": ck["resume_mode"], "checkpoint_kind": "best" if selected else "last", "device": "cpu"}
    if ck["fixture"] == "pretrained":
        result.update(model_id=MODEL_ID, revision=MODEL_REVISION, upstream_weights_sha256=MODEL_FILES["pytorch_model.bin"],
                      vocabulary_size=data["vocabulary_size"], distinct_tokenized_examples=len(data["texts"]))
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(result, indent=2))
    return result


def export_model(data: dict, checkpoint: Path, parameters: dict, out: Path) -> Path:
    verify_data(data)
    ck = load_checkpoint(checkpoint, selected=parameters.get("checkpoint_kind", "best") == "best")
    setup(ck["seed"])
    model = build_model(ck["fixture"], ck["width"]).eval()
    model.load_state_dict(ck["model"])
    out.mkdir(parents=True, exist_ok=True)
    ids = torch.tensor(data["heldout_ids"])
    with torch.no_grad():
        before = _logits(model, _batch(data, ids))
    if ck["fixture"] == "pretrained":
        from transformers import BertForSequenceClassification, BertTokenizer
        model.save_pretrained(out, safe_serialization=True)
        BertTokenizer.from_pretrained(str(model_directory()), local_files_only=True).save_pretrained(out)
        reloaded = BertForSequenceClassification.from_pretrained(str(out), local_files_only=True).eval()
    else:
        atomic_save({"model": ck["model"], "width": ck["width"]}, out / "model.pt")
        reloaded = CustomModel(ck["width"]).eval()
        reloaded.load_state_dict(load_checkpoint(out / "model.pt")["model"])
    with torch.no_grad():
        difference = float((before - _logits(reloaded, _batch(data, ids))).abs().max())
    if difference != 0.0:
        raise ValueError("Exported model changed heldout logits")
    (out / "parity.json").write_text(json.dumps({"max_abs_difference": difference, "dataset_sha256": data["sha256"]}))
    return out
