"""Independent CPU-only derivation fixture. No external models or datasets."""
import hashlib
import json
from pathlib import Path

import torch
from safetensors.torch import save_file, load_file
from modelbuilder.backend.components import identity, read_checkpoint
from modelbuilder.backend.components.weight_blend import blend
from modelbuilder.backend.components.scientific_gate import gate
from modelbuilder.evaluation import make_evaluation_record


def train(*, inputs, parameters, context):
    weight = torch.nn.Parameter(torch.tensor([0.]))
    optimizer = torch.optim.SGD([weight], lr=.5)
    for _ in range(parameters["steps"]):
        optimizer.zero_grad()
        (weight - parameters["target"]).square().sum().backward()
        optimizer.step()
    root = Path(context["output_dir"])
    root.mkdir(parents=True, exist_ok=True)
    save_file({"weight": weight.detach()}, root / "weights.safetensors")
    manifest = {"schema_version": "modelbuilder.checkpoint/v1", "purpose": "best", "step": parameters["steps"],
        "resume_modes": [], "state": ["model"], "identities": context["identities"],
        "rights": context["declared_artifact_rights"],
        "weight_layout": {"format": "safetensors-state-dict/v1", "weights": "weights.safetensors", "auxiliary_assets": [], "model_signature": {"task": "tiny-regression"}},
        "assets": [{"path": "weights.safetensors", "sha256": hashlib.sha256((root / "weights.safetensors").read_bytes()).hexdigest()}]}
    (root / "checkpoint.manifest.json").write_text(json.dumps(manifest))
    return {"checkpoint": root}


def evaluate(*, inputs, parameters, context):
    root = inputs["checkpoint"]
    ck = read_checkpoint(root)
    weight = load_file(root / "weights.safetensors")["weight"].item()
    values = [(weight - 2.)**2 + i * .001 for i in range(100)]
    record = make_evaluation_record(dataset={"id": "tiny-heldout", "revision": "1", "content_digest": "c" * 64, "benchmark": "tiny-100/v1"},
        split={"id": "heldout", "role": "validation", "sample_ids": [str(i) for i in range(100)]},
        protocol={"implementation": "tiny_harness.evaluate", "version": "1", "configuration": {}, "preprocessing": {}, "seed": 0},
        metric={"name": "loss", "implementation": "squared-error", "version": "1", "parameters": {}, "aggregation": "mean", "direction": "minimize"},
        treatment={"model": "tiny", "checkpoint": identity(ck), "calibration": None, "reference": None},
        value=sum(values)/len(values), predictions=[{"item_id": str(i), "value": v} for i, v in enumerate(values)])
    return {"metrics": {"loss": record["value"], "evaluation_records": {"loss": record}}}


__all__ = ["train", "evaluate", "blend", "gate"]


def custom_gate(*, inputs, parameters, context):
    """A project-authored rule added without changing the product method catalog."""
    from statistics import median
    from modelbuilder.scientific import Method
    def independent_rule(*, baseline, candidate, rule, alpha):
        x = {p["item_id"]: p["value"] for p in baseline["predictions"]}
        deltas = [x[p["item_id"]] - p["value"] for p in candidate["predictions"]]
        improvement = median(deltas)
        return {"outcome": "passed" if improvement >= rule["method"]["parameters"]["margin"] else "failed",
                "improvement": improvement, "uncertainty": None, "significance_claim": False,
                "sample_adequacy": None, "limitations": "Independent fixture descriptive paired-median rule; no significance claim."}
    method = parameters["policy"]["rules"][0]["method"]
    extension = Method(method["id"], method["version"], method["implementation"],
        {"type": "object", "additionalProperties": False, "properties": {"margin": {"type": "number"}, "assumptions": {"type": "object"}}, "required": ["margin", "assumptions"]}, independent_rule)
    return gate(inputs=inputs, parameters=parameters, context=context, extensions=[extension])
