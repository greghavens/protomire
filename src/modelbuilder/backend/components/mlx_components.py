"""Offline MLX component implementation for an isolated, operator-selected worker.

The existing native worker owns scheduling and ZenML artifacts. This module is
loaded by the reviewed source-bound MLX IPC worker.
"""

from __future__ import annotations
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import sys

PROFILE_SHA256 = "f408bef35d4c867e2b634ac066cc73a4197bc60214dfd1f22cb57683fb2b68bb"
MAX_ASSET_BYTES = 512 * 1024 * 1024
MAX_PARITY_LOGITS = 1_000_000
MAX_PARITY_RESULT_BYTES = 32 * 1024 * 1024
PINS = {"mlx": "0.32.3", "mlx-metal": "0.32.3", "mlx-lm": "0.32.0", "transformers": "5.7.0"}


class Cancelled(InterruptedError):
    """Cancellation observed after a complete, published optimizer update."""


def digest(path):
    file = Path(path)
    if file.stat().st_size > 512 * 1024 * 1024:
        raise ValueError("MLX asset file exceeds bounded byte limit")
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n")


def manifest_files(path):
    root = Path(path).resolve(strict=True)
    files = []
    for file in sorted(root.rglob("*")):
        if file.is_symlink():
            raise ValueError("Symlinked model assets are unsupported")
        if file.is_file() and file.name not in {"checkpoint.manifest.json", "export.manifest.json"}:
            files.append({"path": str(file.relative_to(root)), "sha256": digest(file)})
            if len(files) > 10000:
                raise ValueError("Model asset file count exceeds bound")
    return files


def verify(path, assets, max_bytes=512 * 1024 * 1024):
    root = Path(path).resolve(strict=True)
    if not root.is_dir() or not assets or len(assets) > 10000:
        raise ValueError("Existing, bounded local asset directory required")
    sizes = [
        (root / asset["path"]).stat().st_size
        for asset in assets
        if not Path(asset["path"]).is_absolute() and ".." not in Path(asset["path"]).parts
    ]
    if len(sizes) != len(assets) or sum(sizes) > max_bytes:
        raise ValueError("MLX asset byte budget exceeded before hashing")
    for asset in assets:
        relative = Path(asset["path"])
        file = root / relative
        if relative.is_absolute() or ".." in relative.parts or file.is_symlink():
            raise ValueError("Invalid asset path")
        if not file.resolve().is_relative_to(root) or digest(file) != asset["sha256"]:
            raise ValueError("Changed model asset: " + str(relative))
    if manifest_files(root) != assets:
        raise ValueError("Unmanifested model assets")
    return root


def runtime(device):
    import mlx.core as mx
    import platform

    if platform.python_version() != "3.12.14":
        raise ValueError("Pinned MLX Python 3.12.14 required")
    versions = {name: metadata.version(name) for name in PINS}
    if versions != PINS:
        raise ValueError("MLX dependency profile mismatch")
    if device not in {"mlx-metal", "mlx-cpu"}:
        raise ValueError("MLX device must be explicit")
    if device == "mlx-metal" and not mx.metal.is_available():
        raise ValueError("MLX Metal backend unavailable")
    selected = mx.gpu if device == "mlx-metal" else mx.cpu
    mx.set_default_device(selected)
    with mx.stream(mx.new_stream(selected)):
        probe = mx.array([2.0]) * 3
        mx.eval(probe)
        assert probe.item() == 6
    profile = Path(__file__).with_name("mlx-darwin-arm64.lock")
    if digest(profile) != PROFILE_SHA256:
        raise ValueError("MLX dependency profile bytes changed")
    packages = {}
    for line in profile.read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^ \\]+)", line)
        if match:
            name, expected = match.groups()
            packages[name] = metadata.version(name)
            if packages[name] != expected:
                raise ValueError("MLX worker package differs from reviewed profile: " + name)
    return {
        "framework": "mlx",
        "device": device,
        "versions": versions,
        "packages": packages,
        "profile_sha256": PROFILE_SHA256,
        "python": sys.version.split()[0],
        "metal_available": mx.metal.is_available(),
    }


def validate_model_references(root):
    """Reject metadata that makes a local tokenizer consume unmanifested paths."""
    root = Path(root).resolve(strict=True)

    def reject_overrides(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    key.endswith(("_file", "_files"))
                    or key in {"vocab_files_names", "pretrained_vocab_files_map"}
                ) and item:
                    raise ValueError(
                        "Tokenizer file overrides are not supported in verified MLX assets: " + key
                    )
                reject_overrides(item)
        elif isinstance(value, list):
            for item in value:
                reject_overrides(item)

    config = root / "tokenizer_config.json"
    if config.exists():
        if config.is_symlink() or config.stat().st_size > 1024 * 1024:
            raise ValueError("Bounded local tokenizer metadata required")
        reject_overrides(json.loads(config.read_text()))
    for index in root.glob("*.safetensors.index.json"):
        if index.is_symlink() or index.stat().st_size > 1024 * 1024:
            raise ValueError("Bounded local weight index required")
        weights = json.loads(index.read_text()).get("weight_map", {})
        if not isinstance(weights, dict):
            raise ValueError("Invalid local weight index")
        for name in weights.values():
            if not isinstance(name, str) or not name or "\\" in name:
                raise ValueError("Invalid local weight shard reference")
            relative = Path(name)
            file = root / relative
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or file.is_symlink()
                or not file.resolve().is_relative_to(root)
                or not file.is_file()
            ):
                raise ValueError("Weight shard reference escapes verified MLX assets")


def load_local(path, assets, device, adapter=None):
    root = verify(path, assets)
    validate_model_references(root)
    runtime(device)
    from mlx_lm import load

    return load(
        str(root),
        trust_remote_code=False,
        tokenizer_config={"trust_remote_code": False, "local_files_only": True},
        adapter_path=str(adapter) if adapter else None,
    )


def dataset(value, vocab_size, max_length):
    samples = value["samples"]
    if not samples or len(samples) > 10000:
        raise ValueError("Bounded nonempty registered token dataset required")
    ids = [sample["id"] for sample in samples]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate dataset sample identity")
    result = []
    for sample in samples:
        tokens, offset = sample["tokens"], sample["prompt_offset"]
        if (
            not isinstance(tokens, list)
            or type(offset) is not int
            or not 2 <= len(tokens) <= max_length
            or not 0 <= offset < len(tokens)
        ):
            raise ValueError("Invalid token length or prompt offset")
        if any(type(token) is not int or not 0 <= token < vocab_size for token in tokens):
            raise ValueError("Invalid token ID")
        result.append((tokens, offset))
    return result


def cancelled(context):
    return bool(context.get("cancel_file") and Path(context["cancel_file"]).exists())


def batches(**kwargs):
    """Use the official iterator, excluding the first padded target from loss.

    v0.32.0 returns input lengths as inclusive target bounds. A sequence of
    N tokens has N-1 next-token targets, so the upper bound must be N-1.
    """
    from mlx_lm.tuner.trainer import iterate_batches
    import mlx.core as mx

    for batch, lengths in iterate_batches(**kwargs):
        yield batch, mx.stack([lengths[:, 0], lengths[:, 1] - 1], axis=1)


def checkpoint(path):
    root = Path(path)
    record = json.loads((root / "checkpoint.manifest.json").read_text())
    verify(root, record["assets"])
    if record["resume_modes"] != ["warm_start"]:
        raise ValueError("MLX supports warm_start only")
    return record


def snapshot(model, tokenizer, config, output, step, parameters, context, execution):
    import mlx.core as mx
    from mlx.utils import tree_flatten
    from mlx_lm.utils import save_model

    path = Path(output) / f"checkpoint-{step:06d}"
    path.mkdir(parents=True, exist_ok=False)
    mx.eval(model.parameters())
    if parameters["mode"] == "lora":
        mx.save_safetensors(
            str(path / "adapters.safetensors"), dict(tree_flatten(model.trainable_parameters()))
        )
        write_json(
            path / "adapter_config.json",
            {
                "fine_tune_type": "lora",
                "num_layers": parameters["num_layers"],
                "lora_parameters": parameters["lora"],
            },
        )
    else:
        save_model(path, model, donate_model=False)
        write_json(path / "config.json", config)
        tokenizer.save_pretrained(path)
    record = {
        "schema_version": "modelbuilder.checkpoint/v1",
        "purpose": "last",
        "step": step,
        "resume_modes": ["warm_start"],
        "identities": context["identities"],
        "execution": execution,
        "mode": parameters["mode"],
        "base": {"dependency_id": parameters["model_dependency"], "assets": parameters["assets"]},
        "resource_limits": parameters["limits"],
        "data": context.get("mlx_consumption", {}),
        "rights": context["output_artifact_rights"],
        "assets": manifest_files(path),
    }
    write_json(path / "checkpoint.manifest.json", record)
    if context.get("checkpoint_ready"):
        context["checkpoint_ready"](path=path, step=step, purpose="last")
    return path


def train(*, inputs, parameters, context):
    import mlx.core as mx
    import mlx.optimizers as optim
    from mlx_lm.tuner.trainer import train as official_train, TrainingArgs
    from mlx_lm.tuner.callbacks import TrainingCallback
    from mlx_lm.tuner.utils import linear_to_lora_layers

    execution = runtime(parameters["device"])
    if parameters["mode"] not in {"full", "lora"}:
        raise ValueError("Unsupported fine-tuning mode")
    if context.get("resume") and context["resume"]["mode"] != "warm_start":
        raise ValueError("MLX supports warm_start only")
    if not 1 <= parameters["updates"] <= 10000 or not 1 <= parameters["accumulation"] <= 64:
        raise ValueError("Unbounded update request")
    if not 1 <= parameters["batch_size"] <= 64 or not 2 <= parameters["max_length"] <= 4096:
        raise ValueError("Unbounded batch/sequence request")
    import math

    if not math.isfinite(parameters["learning_rate"]) or parameters["learning_rate"] <= 0:
        raise ValueError("Finite positive learning rate required")
    limits = parameters["limits"]
    if any(
        type(limits.get(name)) is not int or limits[name] <= 0
        for name in ["max_asset_bytes", "max_parameters", "max_rows"]
    ):
        raise ValueError("Explicit positive MLX resource limits required")
    if limits["max_asset_bytes"] > MAX_ASSET_BYTES:
        raise ValueError("MLX asset byte budget exceeds reviewed 512 MiB maximum")
    base = verify(parameters["model_path"], parameters["assets"], limits["max_asset_bytes"])
    if (
        sum((base / asset["path"]).stat().st_size for asset in parameters["assets"])
        > limits["max_asset_bytes"]
    ):
        raise ValueError("MLX asset byte budget exceeded")
    if len(inputs["train"]["samples"]) > limits["max_rows"]:
        raise ValueError("MLX dataset row budget exceeded")
    reviewed_config = json.loads((base / "config.json").read_text())
    if reviewed_config.get("model_type") != "llama":
        raise ValueError("Reviewed MLX architecture budget currently supports Llama")
    dims = [
        reviewed_config.get(name)
        for name in ["hidden_size", "vocab_size", "intermediate_size", "num_hidden_layers"]
    ]
    if any(type(value) is not int or not 0 < value <= 1000000 for value in dims):
        raise ValueError("Invalid bounded Llama architecture")
    hidden, vocab, intermediate, layers = dims
    heads = reviewed_config.get("num_attention_heads")
    kv_heads = reviewed_config.get("num_key_value_heads", heads)
    if (
        type(heads) is not int
        or type(kv_heads) is not int
        or not 0 < kv_heads <= heads <= hidden
        or hidden % heads
        or heads % kv_heads
        or reviewed_config.get("head_dim", hidden // heads) not in (None, hidden // heads)
    ):
        raise ValueError("Unsupported unbounded Llama attention dimensions")
    if parameters["mode"] == "lora" and (
        parameters["num_layers"] > layers or parameters["lora"]["rank"] > min(hidden, intermediate)
    ):
        raise ValueError("LoRA dimensions exceed the reviewed architecture budget")
    estimate = (
        2 * hidden * vocab + layers * (4 * hidden * hidden + 3 * hidden * intermediate + 2 * hidden) + hidden
    )
    # The public adapter converts all linear layers in the selected blocks.
    # Bound their rank matrices before LoRA allocates them as well.
    if parameters["mode"] == "lora":
        estimate += parameters["num_layers"] * parameters["lora"]["rank"] * (11 * hidden + 3 * intermediate)
    if estimate > limits["max_parameters"]:
        raise ValueError("MLX architecture budget exceeded before allocation")
    mx.random.seed(parameters["seed"])
    import numpy as np

    np.random.seed(parameters["seed"])
    model, tokenizer = load_local(parameters["model_path"], parameters["assets"], parameters["device"])
    from mlx.utils import tree_flatten

    if sum(value.size for _, value in tree_flatten(model.parameters())) > limits["max_parameters"]:
        raise ValueError("MLX parameter budget exceeded")
    config = json.loads((Path(parameters["model_path"]) / "config.json").read_text())
    rows = dataset(inputs["train"], config["vocab_size"], parameters["max_length"])
    from mlx_lm.tuner.datasets import CacheDataset

    consumption = {
        "schema": "modelbuilder.mlx-consumption/v1",
        "lineage": inputs["train"].get("lineage", {}),
        "sample_counts": {},
        "sample_count": 0,
        "order_sha256": None,
    }
    order = hashlib.sha256()
    context["mlx_consumption"] = consumption

    class ObservedRows(CacheDataset):
        def __init__(self):
            # Public CacheDataset's storage/tokenizer setup is replaced only by
            # this already-validated eager token/identity view.
            pass

        def __len__(self):
            return len(rows)

        def itemlen(self, index):
            return len(rows[index][0])

        def __getitem__(self, index):
            identifier = inputs["train"]["samples"][index]["id"]
            consumption["sample_counts"][identifier] = consumption["sample_counts"].get(identifier, 0) + 1
            consumption["sample_count"] += 1
            order.update(json.dumps(identifier, ensure_ascii=False).encode() + b"\n")
            consumption["order_sha256"] = order.hexdigest()
            return rows[index]

    observed_rows = ObservedRows()
    if parameters["mode"] == "full":
        model.unfreeze()
    else:
        model.freeze()
        linear_to_lora_layers(model, parameters["num_layers"], parameters["lora"])
    if context.get("resume"):
        prior = Path(context["resume"]["path"])
        record = checkpoint(prior)
        if (
            record["mode"] != parameters["mode"]
            or record["identities"]["architecture"] != context["identities"]["architecture"]
        ):
            raise ValueError("Warm-start identity mismatch")
        if parameters["mode"] == "lora":
            model.load_weights(str(prior / "adapters.safetensors"), strict=False)
        else:
            from mlx_lm import load

            validate_model_references(prior)
            model, tokenizer = load(
                str(prior),
                trust_remote_code=False,
                tokenizer_config={"trust_remote_code": False, "local_files_only": True},
            )
            model.unfreeze()
    output = Path(context["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    snapshots = []
    accumulation = parameters["accumulation"]

    class Callback(TrainingCallback):
        def on_train_loss_report(self, info):
            batch = info["iteration"]
            if batch % accumulation:
                return
            snapshots.append(
                snapshot(
                    model, tokenizer, config, output, batch // accumulation, parameters, context, execution
                )
            )
            if cancelled(context):
                raise Cancelled("MLX cancelled after safe warm-start snapshot")

    if cancelled(context):
        raise Cancelled("MLX cancelled before training")
    args = TrainingArgs(
        batch_size=parameters["batch_size"],
        iters=parameters["updates"] * accumulation,
        steps_per_report=1,
        steps_per_save=1000000,
        steps_per_eval=1000000,
        max_seq_length=parameters["max_length"],
        grad_accumulation_steps=accumulation,
        adapter_file=str(output / "upstream-final.safetensors"),
    )
    official_train(
        model,
        optim.Adam(parameters["learning_rate"]),
        observed_rows,
        args=args,
        training_callback=Callback(),
        iterate_batches=batches,
    )
    if len(snapshots) != parameters["updates"]:
        raise ValueError("Incomplete optimizer update accounting")
    return {"trained": snapshots[-1]}


def loaded_checkpoint(path, device, context):
    record = checkpoint(path)
    if record["mode"] == "lora":
        base = record["base"]
        model, tokenizer = load_local(
            context["dependencies"][base["dependency_id"]], base["assets"], device, adapter=path
        )
    else:
        model, tokenizer = load_local(path, record["assets"], device)
    return record, model, tokenizer


def evaluate(*, inputs, parameters, context):
    from mlx_lm.tuner.trainer import evaluate as official_evaluate

    execution = runtime(parameters["device"])
    record, model, _ = loaded_checkpoint(inputs["trained"], parameters["device"], context)
    base = record["base"]
    config = (
        json.loads((Path(context["dependencies"][base["dependency_id"]]) / "config.json").read_text())
        if record["mode"] == "lora"
        else json.loads((Path(inputs["trained"]) / "config.json").read_text())
    )
    rows = dataset(inputs["data"], config["vocab_size"], parameters["max_length"])
    loss = official_evaluate(
        model,
        rows,
        batch_size=1,
        num_batches=-1,
        max_seq_length=parameters["max_length"],
        iterate_batches=batches,
    )
    predictions = []
    for sample, row in zip(inputs["data"]["samples"], rows):
        item_loss = official_evaluate(
            model,
            [row],
            batch_size=1,
            num_batches=-1,
            max_seq_length=parameters["max_length"],
            iterate_batches=batches,
        )
        count = len(row[0]) - max(1, row[1])
        predictions.append(
            {"item_id": sample["id"], "negative_log_likelihood_sum": item_loss * count, "token_count": count}
        )
    return {
        "metrics": {
            "predictions": predictions,
            "cross_entropy": loss,
            "sample_ids": [s["id"] for s in inputs["data"]["samples"]],
            "sample_count": len(rows),
            "checkpoint_sha256": digest(Path(inputs["trained"]) / "checkpoint.manifest.json"),
            "dataset_identity": inputs["data"]["identity"],
            "execution": execution,
        }
    }


def checkpoint_config(path, context):
    record = checkpoint(path)
    base = (
        Path(context["dependencies"][record["base"]["dependency_id"]])
        if record["mode"] == "lora"
        else Path(path)
    )
    config_path = base / "config.json"
    if config_path.stat().st_size > 1024 * 1024:
        raise ValueError("Bounded MLX config required")
    return json.loads(config_path.read_text())


def validate_generation(inputs, parameters, context):
    config = checkpoint_config(inputs["trained"], context)
    vocab = config.get("vocab_size")
    prompt = parameters.get("prompt")
    if (
        type(vocab) is not int
        or vocab < 1
        or not isinstance(prompt, list)
        or not 1 <= len(prompt) <= 4096
        or type(parameters.get("max_tokens")) is not int
        or not 1 <= parameters["max_tokens"] <= 4096
    ):
        raise ValueError("Bounded generation and valid model vocabulary required")
    if any(type(token) is not int or not 0 <= token < vocab for token in prompt):
        raise ValueError("Generation token IDs must be integers within the verified model vocabulary")


def generate(*, inputs, parameters, context):
    validate_generation(inputs, parameters, context)
    import mlx.core as mx
    from mlx_lm import stream_generate
    from mlx_lm.sample_utils import make_sampler

    execution = runtime(parameters["device"])
    _, model, tokenizer = loaded_checkpoint(inputs["trained"], parameters["device"], context)
    responses = []
    for response in stream_generate(
        model,
        tokenizer,
        parameters["prompt"],
        max_tokens=parameters["max_tokens"],
        stream=mx.new_stream(mx.gpu if parameters["device"] == "mlx-metal" else mx.cpu),
        sampler=make_sampler(temp=0),
    ):
        if cancelled(context):
            raise Cancelled("Generation cancelled")
        responses.append({"token": response.token, "text": response.text})
    return {"generation": {"tokens": responses, "execution": execution}}


def parity_config(inputs, parameters, context):
    """Bound output cardinality before loading/evaluating any native model."""
    config = checkpoint_config(inputs["trained"], context)
    tokens = parameters.get("parity_tokens", [[2, 4, 5]])
    vocab = config.get("vocab_size")
    if type(vocab) is not int or vocab < 1:
        raise ValueError("Positive model vocabulary required for parity")
    if (
        not isinstance(tokens, list)
        or not tokens
        or len(tokens) > 32
        or any(not isinstance(row, list) or not row or len(row) > 4096 for row in tokens)
    ):
        raise ValueError("Bounded parity inputs required")
    if len({len(row) for row in tokens}) != 1 or any(
        type(token) is not int or not 0 <= token < vocab for row in tokens for token in row
    ):
        raise ValueError("Invalid parity token IDs")
    if len(tokens) * len(tokens[0]) * vocab > MAX_PARITY_LOGITS:
        raise ValueError("MLX parity output exceeds the 1,000,000-logit budget before model loading")
    return config, tokens


def read_parity_result(path):
    if Path(path).stat().st_size > MAX_PARITY_RESULT_BYTES:
        raise ValueError("MLX parity result exceeds its 32 MiB byte budget")
    return json.loads(Path(path).read_text())


def export_loader_source():
    loader = """from pathlib import Path
import json,sys
def predict(root, tokens, device):
    root=Path(root).resolve(strict=True)
    if not root.is_dir() or device not in {'mlx-metal','mlx-cpu'}:
        raise ValueError('Existing local model and explicit MLX device required')
    validate_model_references(root)
    config_path=root/'config.json'
    if config_path.stat().st_size>1024*1024:
        raise ValueError('Bounded export model config required')
    vocab=json.loads(config_path.read_text())['vocab_size']
    if (not tokens or len(tokens)>32 or any(not row or len(row)>4096 for row in tokens)
            or len({len(row) for row in tokens})!=1
            or any(type(token) is not int or not 0<=token<vocab for row in tokens for token in row)
            or len(tokens)*len(tokens[0])*vocab>1000000):
        raise ValueError('Export parity input/output budget exceeded')
    import mlx.core as mx
    from mlx_lm import load
    if device=='mlx-metal' and not mx.metal.is_available():
        raise ValueError('Metal unavailable')
    mx.set_default_device(mx.gpu if device=='mlx-metal' else mx.cpu)
    model,_=load(str(root),trust_remote_code=False,tokenizer_config={'trust_remote_code':False,'local_files_only':True})
    model.eval()
    logits=model(mx.array(tokens))
    if logits.size>1000000:
        raise ValueError('Export parity output shape exceeded')
    return logits.tolist()
if __name__=='__main__':
    root=Path(sys.argv[1])
    request=json.loads((root/'parity-input.json').read_text())
    result=json.dumps(predict(root,request['tokens'],request['device']),allow_nan=False)
    if len(result.encode())>32*1024*1024:
        raise ValueError('Export parity result byte budget exceeded')
    (root/'parity-actual.json').write_text(result)
"""
    import inspect

    return loader.replace("def predict(", inspect.getsource(validate_model_references) + "\n\ndef predict(")


def export(*, inputs, parameters, context):
    config, tokens = parity_config(inputs, parameters, context)
    import mlx.core as mx
    import subprocess
    from mlx_lm.utils import save_model

    record, model, tokenizer = loaded_checkpoint(inputs["trained"], parameters["device"], context)
    # A fused full-weight export remains reloadable without external base paths.
    if record["mode"] == "lora":
        from mlx.utils import tree_unflatten

        model.update_modules(
            tree_unflatten(
                [(name, layer.fuse()) for name, layer in model.named_modules() if hasattr(layer, "fuse")]
            )
        )
    path = Path(context["output_dir"]) / "model"
    path.mkdir(parents=True, exist_ok=False)
    save_model(path, model, donate_model=False)
    write_json(path / "config.json", config)
    tokenizer.save_pretrained(path)
    (path / "loader.py").write_text(export_loader_source())
    atol = parameters.get("atol", 1e-6)
    import math

    if type(atol) not in (float, int) or not math.isfinite(atol) or atol < 0:
        raise ValueError("Finite nonnegative parity tolerance required")
    model.eval()
    logits = model(mx.array(tokens))
    if logits.size > MAX_PARITY_LOGITS:
        raise ValueError("MLX model exceeded the bounded parity output shape")
    expected = logits.tolist()
    write_json(path / "parity-input.json", {"tokens": tokens, "device": parameters["device"]})
    subprocess.run(
        [sys.executable, "-I", str(path / "loader.py"), str(path)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    actual = read_parity_result(path / "parity-actual.json")
    import numpy as np

    if np.asarray(actual).shape != np.asarray(expected).shape or not np.allclose(
        expected, actual, atol=atol, rtol=0
    ):
        raise ValueError("Fresh-process MLX export parity failed")
    (path / "parity-actual.json").unlink()
    verification = {
        "passed": True,
        "atol": atol,
        "rtol": 0,
        "fresh_process": True,
        "max_abs_difference": float(np.max(np.abs(np.asarray(expected) - np.asarray(actual)))),
    }
    write_json(
        path / "export.manifest.json",
        {
            "schema_version": "modelbuilder.export/v1",
            "selected_checkpoint": hashlib.sha256(
                json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest(),
            "loader": {"path": "loader.py", "entrypoint": "predict", "format": "mlx-safetensors"},
            "signature": {"input": "list[list[int]]", "output": "float32 next-token logits"},
            "dependencies": PINS,
            "source": context["identities"]["source"],
            "verification": verification,
            "rights": record.get("rights"),
            "execution": runtime(parameters["device"]),
            "assets": manifest_files(path),
        },
    )
    return {"exported": path}
