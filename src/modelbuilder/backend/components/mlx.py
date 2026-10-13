"""Inert MLX catalog and source-bound component ABI; native ZenML retains ownership."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess

from . import identity
from . import mlx_bridge as bridge

PROFILE_SHA256 = bridge.PROFILE_SHA256
STRING = {"type": "string", "minLength": 1}
POSITIVE = {"type": "integer", "minimum": 1}


def schema(properties):
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


ASSETS = {
    "type": "array",
    "minItems": 1,
    "maxItems": 10000,
    "items": schema({"path": STRING, "sha256": {"type": "string", "minLength": 64, "maxLength": 64}}),
}
LIMITS = schema(
    {
        "max_asset_bytes": {"type": "integer", "minimum": 1, "maximum": bridge.MAX_ASSET_BYTES},
        "max_parameters": POSITIVE,
        "max_rows": POSITIVE,
    }
)
TRAIN = schema(
    {
        "model_dependency": STRING,
        "assets": ASSETS,
        "mode": {"enum": ["full", "lora"]},
        "updates": {"type": "integer", "minimum": 1, "maximum": 10000},
        "accumulation": {"type": "integer", "minimum": 1, "maximum": 64},
        "batch_size": {"type": "integer", "minimum": 1, "maximum": 64},
        "max_length": {"type": "integer", "minimum": 2, "maximum": 4096},
        "seed": {"type": "integer", "minimum": 0, "maximum": 2147483647},
        "learning_rate": {"type": "number", "exclusiveMinimum": 0},
        "num_layers": POSITIVE,
        "lora": schema(
            {
                "rank": POSITIVE,
                "scale": {"type": "number", "exclusiveMinimum": 0},
                "dropout": {"type": "number", "minimum": 0, "maximum": 1},
            }
        ),
        "limits": LIMITS,
    }
)
EVALUATE = schema({"max_length": {"type": "integer", "minimum": 2, "maximum": 4096}, "max_rows": POSITIVE})
GENERATE = schema(
    {
        "prompt": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4096,
            "items": {"type": "integer", "minimum": 0},
        },
        "max_tokens": {"type": "integer", "minimum": 1, "maximum": 4096},
    }
)
EXPORT = schema(
    {
        "parity_tokens": {
            "description": "The complete batch × sequence × vocabulary must fit 1,000,000 logits.",
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4096,
                "items": {"type": "integer", "minimum": 0},
            },
        },
        "atol": {"type": "number", "minimum": 0},
    }
)


def catalog_entries():
    result = []
    for name, role, params, inputs, outputs in [
        ("train", "training", TRAIN, [("train", "dict")], [("trained", "Path")]),
        ("evaluate", "evaluation", EVALUATE, [("trained", "Path"), ("data", "dict")], [("metrics", "dict")]),
        ("generate", "utility", GENERATE, [("trained", "Path")], [("generation", "dict")]),
        ("export", "export", EXPORT, [("trained", "Path")], [("exported", "Path")]),
    ]:
        descriptor = {
            "id": "modelbuilder.mlx." + name,
            "version": "1",
            "protocol": "modelbuilder.component/v1",
            "role": role,
            "description": "Offline native MLX "
            + name
            + " in a separately verified operator-configured worker.",
            "parameter_schema": deepcopy(params),
            "capabilities": {
                "devices": ["mlx-cpu", "mlx-metal"],
                "resume_modes": ["warm_start"] if name == "train" else [],
                "export_formats": ["mlx-safetensors"] if name == "export" else [],
                "checkpoint_events": name == "train",
                "frameworks": ["mlx", "mlx-lm"],
            },
        }
        ports = [{"name": port, "type": kind} for port, kind in inputs]
        if name == "train":
            ports.append({"name": "validation", "type": "dict", "required": False})
        result.append(
            {
                **descriptor,
                "inputs": ports,
                "outputs": [{"name": port, "type": kind} for port, kind in outputs],
                "adapter": {
                    "module": __name__,
                    "entrypoint": name,
                    "source_binding": "Pin the adapter in the approved source checkout; configure the reviewed isolated MLX interpreter.",
                },
                "component_digest": identity(descriptor),
            }
        )
    return result


def selected_device_for_component(component, selected):
    mlx = component.get("id", "").startswith("modelbuilder.mlx.") and "mlx" in component.get(
        "capabilities", {}
    ).get("frameworks", [])
    if mlx:
        # Live evaluation remains CPU-only, in the MLX CPU backend rather than Torch.
        return selected if selected.startswith("mlx-") else "mlx-cpu" if selected == "cpu" else selected
    return selected if component.get("role") == "training" else "cpu"


def architecture_identity(parameters):
    return identity({key: parameters[key] for key in ("assets", "mode", "num_layers", "lora")})


def probe(python, device, *, allocate=False):
    if not python or not Path(python).is_file():
        return {
            "available": False,
            "device": device,
            "reason": "Operator-configured MLX interpreter is unavailable",
        }
    try:
        result = subprocess.run(
            [python, "-I", "-B", str(Path(__file__).with_name("mlx_probe.py"))],
            input=json.dumps({"device": device, "allocate": allocate}),
            capture_output=True,
            text=True,
            timeout=30,
            env={key: value for key, value in os.environ.items() if not key.startswith("PYTHON")},
        )
        value = json.loads(result.stdout)
        if (
            result.returncode
            or len(result.stdout) > 65536
            or not isinstance(value, dict)
            or value.get("device") != device
            or type(value.get("available")) is not bool
            or value.get("available")
            and allocate
            and value.get("allocation_verified") is not True
        ):
            raise ValueError("Invalid isolated MLX probe")
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {
            "available": False,
            "device": device,
            "reason": "Configured isolated MLX probe failed or exceeded its deadline",
        }


def registered_tokens(data, roles, max_rows, max_length):
    from .transformers_training import _split

    rows, lineage = _split(
        data,
        roles,
        {
            "max_length": max_length,
            "limits": {"max_rows": max_rows, "max_tokenized_tokens": max_rows * max_length},
        },
    )
    samples = [
        {"id": identifier, "tokens": row["tokens"], "prompt_offset": row["prompt_offset"]}
        for identifier, row in zip(lineage["split"]["sample_ids"], rows)
    ]
    return {"identity": identity(lineage), "samples": samples, "lineage": lineage, "rights": data.get("rights")}


def train(*, inputs, parameters, context):
    from jsonschema import Draft202012Validator

    Draft202012Validator(TRAIN).validate(parameters)
    bound = registered_tokens(
        inputs["train"], {"train"}, parameters["limits"]["max_rows"], parameters["max_length"]
    )
    return bridge.train(
        inputs={"train": bound}, parameters={**parameters, "device": context["device"]}, context=context
    )


def evaluate(*, inputs, parameters, context):
    from jsonschema import Draft202012Validator
    from ...evaluation import make_evaluation_record
    from . import read_checkpoint

    Draft202012Validator(EVALUATE).validate(parameters)
    bound = registered_tokens(
        inputs["data"], {"validation", "test"}, parameters["max_rows"], parameters["max_length"]
    )
    device = "mlx-cpu" if context["device"] == "cpu" else context["device"]
    result = bridge.evaluate(
        inputs={"trained": inputs["trained"], "data": bound},
        parameters={**parameters, "device": device},
        context=context,
    )
    metrics = result["metrics"]
    checkpoint = read_checkpoint(inputs["trained"])
    lineage = bound["lineage"]
    record = make_evaluation_record(
        dataset=lineage["dataset"],
        split=lineage["split"],
        protocol={
            "implementation": "modelbuilder.mlx.evaluate",
            "version": "1",
            "configuration": parameters,
            "preprocessing": {"format": "token_ids", "padding_target_excluded": True},
            "seed": 0,
        },
        metric={
            "name": "cross_entropy",
            "implementation": "mlx_lm.tuner.trainer.default_loss",
            "version": "0.32.0",
            "parameters": {},
            "aggregation": "token_weighted_mean",
            "direction": "minimize",
        },
        treatment={
            "model": checkpoint["identities"],
            "checkpoint": identity(checkpoint),
            "calibration": None,
            "reference": None,
        },
        value=metrics["cross_entropy"],
        predictions=metrics["predictions"],
        provenance={
            "packages": metrics["execution"]["packages"],
            "execution": metrics["execution"],
            "consumption": lineage,
            "source": context["source"],
        },
    )
    metrics["evaluation_records"] = {"cross_entropy": record}
    return result


def generate(*, inputs, parameters, context):
    from jsonschema import Draft202012Validator

    Draft202012Validator(GENERATE).validate(parameters)
    return bridge.generate(
        inputs=inputs, parameters={**parameters, "device": context["device"]}, context=context
    )


def export(*, inputs, parameters, context):
    from jsonschema import Draft202012Validator

    Draft202012Validator(EXPORT).validate(parameters)
    return bridge.export(
        inputs=inputs, parameters={**parameters, "device": context["device"]}, context=context
    )
