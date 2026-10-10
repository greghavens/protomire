"""Local temperature calibration using PyTorch's optimizer and cross entropy.

The derived artifact pins fit data and the evaluated model. It is not trainable
model state and cannot be silently reused with another checkpoint or fit split.
"""
from __future__ import annotations
from copy import deepcopy
import math
from ..evaluation import digest

SCHEMA = "modelbuilder.calibration/temperature-v1"


def fit_temperature(logits, targets, *, model_identity: str, dataset_identity: dict,
                    fit_sample_ids: list[str], evaluation_sample_ids: list[str], max_iter: int = 20) -> dict:
    import torch
    if not model_identity or not dataset_identity.get("content_digest"):
        raise ValueError("Calibration requires pinned model and dataset identity")
    if (not fit_sample_ids or len(set(fit_sample_ids)) != len(fit_sample_ids)
            or not evaluation_sample_ids or len(set(evaluation_sample_ids)) != len(evaluation_sample_ids)):
        raise ValueError("Calibration fit and evaluation require unique sample IDs")
    if set(fit_sample_ids) & set(evaluation_sample_ids):
        raise ValueError("Calibration fit/evaluation sample leakage")
    if not isinstance(max_iter, int) or isinstance(max_iter, bool) or not 1 <= max_iter <= 100:
        raise ValueError("Calibration max_iter must be 1..100")
    values = torch.as_tensor(logits, dtype=torch.float64).detach()
    labels = torch.as_tensor(targets, dtype=torch.long).detach()
    if values.ndim != 2 or len(values) != len(fit_sample_ids) or labels.shape != (len(values),) or not torch.isfinite(values).all():
        raise ValueError("Calibration requires finite class logits and one target per fit sample")
    if values.shape[1] < 2 or (labels < 0).any() or (labels >= values.shape[1]).any():
        raise ValueError("Calibration labels are outside the class vocabulary")
    log_temperature = torch.zeros((), dtype=torch.float64, requires_grad=True)
    optimizer = torch.optim.LBFGS([log_temperature], max_iter=max_iter, line_search_fn="strong_wolfe")
    def closure():
        optimizer.zero_grad()
        loss = torch.nn.functional.cross_entropy(values / log_temperature.clamp(-6, 6).exp(), labels)
        loss.backward()
        return loss
    initial = float(torch.nn.functional.cross_entropy(values, labels))
    optimizer.step(closure)
    temperature = float(log_temperature.detach().clamp(-6, 6).exp())
    artifact = {"schema": SCHEMA, "implementation": "torch.optim.LBFGS+torch.nn.functional.cross_entropy",
                "implementation_version": torch.__version__, "configuration": {"max_iter": max_iter, "log_temperature_bounds": [-6, 6]},
                "model_identity": model_identity, "dataset_identity": deepcopy(dataset_identity),
                "fit_split_role": "calibration", "fit_sample_ids": list(fit_sample_ids),
                "evaluation_sample_ids": list(evaluation_sample_ids),
                "fit_inputs_digest": digest({"logits":values.tolist(), "targets":labels.tolist()}),
                "temperature": temperature, "fit_nll_before": initial,
                "fit_nll_after": float(torch.nn.functional.cross_entropy(values / temperature, labels))}
    artifact["digest"] = digest(artifact)
    return artifact


def apply_temperature(logits, artifact: dict, *, model_identity: str, sample_ids: list[str]):
    import torch
    if artifact.get("schema") != SCHEMA or artifact.get("digest") != digest({k:v for k,v in artifact.items() if k != "digest"}):
        raise ValueError("Calibration artifact identity mismatch")
    temperature = artifact.get("temperature")
    try:
        valid_temperature = type(temperature) in (int, float) and math.isfinite(temperature) and temperature > 0
    except OverflowError:
        valid_temperature = False
    if not valid_temperature:
        raise ValueError("Calibration temperature must be finite and positive")
    if model_identity != artifact["model_identity"]:
        raise ValueError("Calibration belongs to another model/checkpoint")
    if set(sample_ids) & set(artifact["fit_sample_ids"]):
        raise ValueError("Calibration fit/evaluation sample leakage")
    values = torch.as_tensor(logits)
    if len(values) != len(sample_ids) or not torch.isfinite(values).all():
        raise ValueError("Calibration predictions do not match sample identities")
    result = values / temperature
    if not torch.isfinite(result).all():
        raise ValueError("Calibration produced nonfinite predictions")
    return result
