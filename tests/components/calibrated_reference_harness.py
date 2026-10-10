"""Independently authored tiny CPU acceptance model, copied to a user repository.

The framework owns optimization and serialization. Platform imports only supply
calibration and evidence contracts; no built-in model or training code is used.
"""
import hashlib
import json
from pathlib import Path
import shutil
import time

import torch
from modelbuilder.backend.calibration import SCHEMA, apply_temperature, fit_temperature
from modelbuilder.backend.components import identity, read_checkpoint
from modelbuilder.evaluation import digest, make_evaluation_record


def model(seed=71):
    torch.set_num_threads(1)
    torch.manual_seed(seed)
    return torch.nn.Sequential(torch.nn.Linear(3, 6), torch.nn.Tanh(), torch.nn.Linear(6, 3)).double()


def samples(split):
    generator = torch.Generator().manual_seed(941)
    x = torch.randn(48, 3, generator=generator, dtype=torch.float64)
    y = (x @ torch.tensor([[1., -.7, .1], [-.4, 1., -.3], [.2, -.5, 1.]], dtype=torch.float64)).argmax(-1)
    selection = {"train": slice(0, 24), "fit": slice(24, 36), "eval": slice(36, 48)}[split]
    return x[selection], y[selection], [f"synthetic-{i}" for i in range(48)[selection]]


def weight_identity(root):
    return hashlib.sha256((Path(root) / "weights.pt").read_bytes()).hexdigest()


def asset(root, name):
    return {"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()}


def load(root):
    value = model()
    value.load_state_dict(torch.load(Path(root) / "weights.pt", weights_only=True, map_location="cpu"))
    return value


def save_checkpoint(root, context, step, modes, extra=()):
    names = ["weights.pt", *extra]
    descriptor = {"schema_version": "modelbuilder.checkpoint/v1", "identities": context["identities"],
        "step": step, "purpose": "last" if modes else "inference", "resume_modes": modes,
        "state": ["model", "optimizer", "scheduler", "rng", "data_cursor"] if modes else [],
        "assets": [asset(root, name) for name in names]}
    path = root / "checkpoint.manifest.partial"
    path.write_text(json.dumps(descriptor))
    path.replace(root / "checkpoint.manifest.json")


def reference(*, inputs, parameters, context):
    value = model(parameters["seed"])
    # This independently selected synthetic checkpoint intentionally starts
    # overconfident; no external weights or datasets are used.
    with torch.no_grad():
        value[-1].weight.mul_(5)
        value[-1].bias.mul_(5)
    x, y, _ = samples("train")
    optimizer = torch.optim.SGD(value.parameters(), lr=.03)
    for _ in range(2):
        optimizer.zero_grad()
        torch.nn.functional.cross_entropy(value(x), y).backward()
        optimizer.step()
    root = Path(context["output_dir"])
    torch.save(value.state_dict(), root / "weights.pt")
    save_checkpoint(root, context, 2, [])
    return {"checkpoint": root}


def calibrate(*, inputs, parameters, context):
    checkpoint = inputs["checkpoint"]
    reference_id = weight_identity(checkpoint)
    x, y, fit_ids = samples("fit")
    _, _, eval_ids = samples("eval")
    if parameters.get("leakage_probe"):
        eval_ids = fit_ids
    if parameters["mode"] == "fit":
        with torch.no_grad():
            logits = load(checkpoint)(x)
        calibration = fit_temperature(logits, y, model_identity=reference_id,
            dataset_identity={"id": "independent-three-class-fit", "revision": "1", "content_digest": digest({"x": x.tolist(), "y": y.tolist()})},
            fit_sample_ids=fit_ids, evaluation_sample_ids=eval_ids)
    else:
        calibration = {"schema": SCHEMA, "implementation": "declared-fixed-temperature",
            "implementation_version": "1", "configuration": {"fitted": False},
            "model_identity": reference_id, "fit_split_role": None, "fit_sample_ids": [],
            "evaluation_sample_ids": eval_ids, "temperature": parameters["temperature"]}
        calibration["digest"] = digest(calibration)
    root = Path(context["output_dir"])
    (root / "calibration.json").write_text(json.dumps(calibration))
    return {"calibration": root}


def train(*, inputs, parameters, context):
    reference_root = inputs["reference"]
    calibration = json.loads((inputs["calibration"] / "calibration.json").read_text())
    frozen = load(reference_root).eval().requires_grad_(False)
    before = {k: v.clone() for k, v in frozen.state_dict().items()}
    policy = model()
    optimizer = torch.optim.SGD(policy.parameters(), lr=.09, momentum=.8)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=2, gamma=.8)
    history, cursor = [], 0
    if context.get("resume"):
        saved = torch.load(Path(context["resume"]["path"]) / "training-state.pt", weights_only=True)
        policy.load_state_dict(saved["model"])
        if context["resume_mode"] == "exact_state":
            optimizer.load_state_dict(saved["optimizer"])
            scheduler.load_state_dict(saved["scheduler"])
            torch.set_rng_state(saved["rng"])
            history, cursor = saved["history"], saved["data_cursor"]
    reference_id = weight_identity(reference_root)
    x, y, ids = samples("train")
    with torch.no_grad():
        ref_logits = apply_temperature(frozen(x), calibration, model_identity=reference_id, sample_ids=ids)
        same_logits = apply_temperature(frozen(x), calibration, model_identity=reference_id, sample_ids=ids)
        same_kl = float(torch.nn.functional.kl_div(same_logits.log_softmax(-1), ref_logits.softmax(-1), reduction="batchmean"))
    root = Path(context["output_dir"])
    for step in range(cursor, parameters["updates"]):
        start = step * 4 % len(x)
        raw = policy(x[start:start + 4])
        policy_log = (raw / calibration["temperature"]).log_softmax(-1)
        reference_log = ref_logits[start:start + 4].log_softmax(-1)
        direction = parameters["direction"]
        if direction == "policy_to_reference":
            kl = torch.nn.functional.kl_div(reference_log, policy_log.exp(), reduction="batchmean")
        elif direction == "reference_to_policy":
            kl = torch.nn.functional.kl_div(policy_log, reference_log.exp(), reduction="batchmean")
        else:
            raise ValueError("Unknown declared KL direction")
        ce = torch.nn.functional.cross_entropy(raw, y[start:start + 4])
        loss = ce + parameters["coefficient"] * kl
        history.append({"step": step + 1, "loss": float(loss.detach()), "ce": float(ce.detach()),
            "kl": float(kl.detach()), "policy_logits": (raw / calibration["temperature"]).detach().tolist(),
            "reference_logits": ref_logits[start:start + 4].tolist()})
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        scheduler.step()
        assert all(value.grad is None for value in frozen.parameters())
        assert all(torch.equal(before[key], value) for key, value in frozen.state_dict().items())
        torch.save(policy.state_dict(), root / "weights.pt")
        torch.save({"model": policy.state_dict(), "optimizer": optimizer.state_dict(), "scheduler": scheduler.state_dict(),
            "rng": torch.get_rng_state(), "data_cursor": step + 1, "history": history}, root / "training-state.pt")
        report = {"objective": {"implementation": "torch.nn.functional.kl_div", "version": torch.__version__,
            "direction": direction, "coefficient": parameters["coefficient"], "output_semantics": "three-class-logits", "temperature_scope": "both_distributions"},
            "reference_identity": reference_id, "reference_checkpoint": identity(read_checkpoint(reference_root)),
            "calibration_identity": calibration["digest"], "calibration_temperature": calibration["temperature"],
            "same_distribution_kl": same_kl, "reference_gradients_absent": True,
            "reference_parameters_unchanged": True, "history": history}
        (root / "training-report.json").write_text(json.dumps(report))
        save_checkpoint(root, context, step + 1, ["exact_state", "warm_start"], ["training-state.pt", "training-report.json"])
        time.sleep(parameters.get("delay", 0))
    return {"checkpoint": root}


def evaluate(*, inputs, parameters, context):
    checkpoint, calibration_root = inputs["checkpoint"], inputs["calibration"]
    calibration = json.loads((calibration_root / "calibration.json").read_text())
    value = load(checkpoint).eval()
    training_report = json.loads((checkpoint / "training-report.json").read_text())
    training_reference = {"checkpoint": training_report["reference_checkpoint"],
        "model_identity": training_report["reference_identity"], "calibration": training_report["calibration_identity"]}
    x, y, ids = samples("eval")
    with torch.no_grad():
        raw = value(x)
        calibrated = apply_temperature(raw, calibration, model_identity=weight_identity(checkpoint), sample_ids=ids)
    records, measurements = {}, {}
    bins = 5
    for label, logits in (("raw", raw), ("calibrated", calibrated)):
        nll = torch.nn.functional.cross_entropy(logits, y, reduction="none")
        confidence, prediction = logits.softmax(-1).max(-1)
        correct = prediction.eq(y).double()
        ece = 0.
        for index in range(bins):
            selected = (confidence > index / bins) & (confidence <= (index + 1) / bins)
            if selected.any():
                ece += float(selected.double().mean() * (confidence[selected].mean() - correct[selected].mean()).abs())
        measurements[label] = {"nll": float(nll.mean()), "accuracy": float(correct.mean()), "ece": ece,
            "ece_configuration": {"bins": bins, "binning": "equal-width", "norm": "l1", "lower_open_upper_closed": True},
            "predictions": logits.tolist()}
        record = make_evaluation_record(dataset={"id": "three-class-eval", "revision": "1", "benchmark": "independent-synthetic",
                "content_digest": digest({"x": x.tolist(), "y": y.tolist()})},
            split={"id": "untouched-eval", "role": "test", "sample_ids": ids},
            protocol={"implementation": "independent-three-class", "version": "1", "configuration": {}, "preprocessing": {}, "seed": 941},
            metric={"name": "nll", "implementation": "torch.nn.functional.cross_entropy", "version": torch.__version__,
                "parameters": {}, "aggregation": "mean", "direction": "minimize"},
            treatment={"model": "independent-three-class", "checkpoint": identity(read_checkpoint(checkpoint)),
                "reference": training_reference, "objective": training_report["objective"],
                "calibration": calibration["digest"] if label == "calibrated" else None},
            value=float(nll.mean()), predictions=[{"item_id": key, "value": float(loss)} for key, loss in zip(ids, nll)])
        records[label] = record
    (Path(context["output_dir"]) / "measurements.json").write_text(json.dumps(measurements))
    chosen = parameters["mode"]
    return {"metrics": {"measurements": measurements, "nll": measurements[chosen]["nll"], "evaluation_record": records[chosen]}}


def export(*, inputs, parameters, context):
    checkpoint, calibration_root = inputs["checkpoint"], inputs["calibration"]
    root = Path(context["output_dir"])
    for source, name in ((checkpoint, "weights.pt"), (calibration_root, "calibration.json")):
        shutil.copy2(source / name, root / name)
    shutil.copy2(Path(__file__).with_name("portable_loader.py"), root / "loader.py")
    calibration = json.loads((root / "calibration.json").read_text())
    x, _, ids = samples("eval")
    with torch.no_grad():
        expected = apply_temperature(load(checkpoint)(x), calibration, model_identity=weight_identity(checkpoint), sample_ids=ids)
    (root / "parity.json").write_text(json.dumps({"inputs": x.tolist(), "predictions": expected.tolist(), "sample_ids": ids}))
    training_report = json.loads((checkpoint / "training-report.json").read_text())
    manifest = {"schema_version": "modelbuilder.export/v1", "selected_checkpoint": identity(read_checkpoint(checkpoint)),
        "training_reference": {"checkpoint": training_report["reference_checkpoint"],
            "model_identity": training_report["reference_identity"], "calibration": training_report["calibration_identity"]},
        "objective": training_report["objective"],
        "loader": {"path": "loader.py", "entrypoint": "load"},
        "signature": {"input": "float64[N,3]", "output": "calibrated float64[N,3] logits"},
        "dependencies": {"torch": torch.__version__}, "source": context["source"],
        "calibration": {"identity": calibration["digest"], "asset": "calibration.json", "model_identity": calibration["model_identity"]},
        "verification": {"passed": True, "atol": 0., "max_abs_difference": 0.},
        "assets": [asset(root, name) for name in ("weights.pt", "calibration.json", "loader.py", "parity.json")]}
    (root / "export.manifest.json").write_text(json.dumps(manifest))
    # Real fresh-process verification is required, before an export can succeed.
    import subprocess
    import sys
    verified = subprocess.run([sys.executable, "-I", str(root / "loader.py"), str(root)], check=True, capture_output=True, text=True)
    manifest["verification"] = json.loads(verified.stdout)
    (root / "export.manifest.json").write_text(json.dumps(manifest))
    return {"package": root}
