"""Source-bound Modelbuilder component bridge to operator-configured MLX.

No scheduler or run store is introduced. Called inside the existing native
worker, the child inherits that worker's supervised process group. Shared
placement and catalog route reviewed MLX components into this bridge.
"""

from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import time

PROFILE_SHA256 = "f408bef35d4c867e2b634ac066cc73a4197bc60214dfd1f22cb57683fb2b68bb"
PROTOCOL = "modelbuilder.mlx-ipc/v1"
MAX_MESSAGE = 4 * 1024 * 1024
MAX_ASSET_BYTES = 512 * 1024 * 1024


def stage_model(identifier, assets, context, output, limit):
    """Stage individually admitted regular-file assets, never a recipe path."""
    from .transformers_training import _copy_dependency

    if type(limit) is not int or not 0 < limit <= MAX_ASSET_BYTES:
        raise ValueError("MLX asset byte budget must be between 1 and 512 MiB")
    manifest_path = context.get("dependencies", {}).get(identifier)
    if not manifest_path or Path(manifest_path).stat().st_size > MAX_MESSAGE:
        raise ValueError("Bounded admitted MLX model manifest required")
    manifest = json.loads(Path(manifest_path).read_text())
    entries = manifest["files"]
    if [{key: entry[key] for key in ("path", "sha256")} for entry in entries] != assets:
        raise ValueError("MLX manifest differs from recipe asset identity")
    directory = output / "bound-model"
    directory.mkdir(exist_ok=False)
    remaining = limit
    for entry in entries:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe MLX asset relative path")
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_dependency(entry, target, context, remaining)
        remaining -= target.stat().st_size
    return directory


def stage_checkpoint(path, context, output):
    """Copy only manifest assets; live snapshot readiness files remain inert."""
    from . import read_checkpoint
    from .transformers_training import _copy_dependency

    path = Path(path).resolve(strict=True)
    record = read_checkpoint(path)
    destination = output / "admitted-checkpoint"
    destination.mkdir(exist_ok=False)
    remaining = record.get("resource_limits", {}).get("max_asset_bytes", MAX_ASSET_BYTES)
    if type(remaining) is not int or not 0 < remaining <= MAX_ASSET_BYTES:
        raise ValueError("MLX checkpoint asset byte budget must be between 1 and 512 MiB")
    for index, asset in enumerate(record["assets"]):
        identifier = "checkpoint-asset-" + str(index)
        target = destination / asset["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_dependency(
            {**asset, "dependency_id": identifier},
            target,
            {"dependencies": {identifier: str(Path(path) / asset["path"])}},
            remaining,
        )
        remaining -= target.stat().st_size
    (destination / "checkpoint.manifest.json").write_text(json.dumps(record, sort_keys=True, allow_nan=False))
    return destination, record


def training_rights(inputs, context):
    """Union exact declared model/data/resume parents before isolated MLX execution."""
    from ...artifact_rights import combine_parent_rights
    from . import identity, read_checkpoint
    parents = [{"artifact_id": "declared-model-inputs", "rights": context.get("declared_artifact_rights")},
               {"artifact_id": "training-data:" + identity(inputs["train"]), "rights": inputs["train"].get("rights")}]
    if context.get("resume"):
        parent = read_checkpoint(Path(context["resume"]["path"]))
        parents.append({"artifact_id": "warm-start:" + identity(parent), "rights": parent.get("rights")})
    return combine_parent_rights(parents)


def execute(operation, *, inputs, parameters, context):
    python = os.environ.get("MODELBUILDER_MLX_PYTHON")
    if not python or not Path(python).is_file():
        raise ValueError("Operator-configured MLX interpreter is unavailable")
    source = Path(__file__).resolve().parent
    profile = source / "mlx-darwin-arm64.lock"
    if hashlib.sha256(profile.read_bytes()).hexdigest() != PROFILE_SHA256:
        raise ValueError("MLX dependency profile changed")
    # The interpreter is never supplied by the recipe. Model directories are
    # operator-bound declared dependencies, checked by the existing runtime.
    params = dict(parameters)
    output = Path(context["output_dir"]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    child_dependencies = dict(context.get("dependencies", {}))
    child_inputs = dict(inputs)
    if operation == "train":
        model = params["model_dependency"]
        bound = stage_model(model, params["assets"], context, output, params["limits"]["max_asset_bytes"])
        params["model_path"] = str(bound)
        child_dependencies[model] = str(bound)
    elif isinstance(inputs.get("trained"), Path):
        admitted, checkpoint = stage_checkpoint(inputs["trained"], context, output)
        child_inputs["trained"] = admitted
        if checkpoint["mode"] == "lora":
            base = checkpoint["base"]
            child_dependencies[base["dependency_id"]] = str(
                stage_model(base["dependency_id"], base["assets"], context, output, 512 * 1024 * 1024)
            )
    timeout = context.get("timeout_seconds", 300)
    if type(timeout) not in (float, int) or not 0 < timeout <= 3600:
        raise ValueError("Bounded MLX timeout required")
    serial_context = {
        name: context[name]
        for name in ["identities", "resume", "cancel_file", "dependencies"]
        if name in context
    }
    if operation == "train":
        serial_context["output_artifact_rights"] = training_rights(inputs, context)
    if serial_context.get("resume"):
        serial_context["resume"] = {
            **serial_context["resume"],
            "mode": context.get("resume_mode", serial_context["resume"].get("mode")),
        }
    serial_context["output_dir"] = str(output)
    serial_context["dependencies"] = child_dependencies
    serial_inputs = {
        name: str(value) if isinstance(value, Path) else value for name, value in child_inputs.items()
    }
    request = {
        "protocol": PROTOCOL,
        "operation": operation,
        "profile_sha256": PROFILE_SHA256,
        "inputs": serial_inputs,
        "parameters": params,
        "context": serial_context,
    }
    payload = json.dumps(request, allow_nan=False).encode() + b"\n"
    if len(payload) > MAX_MESSAGE:
        raise ValueError("MLX IPC request exceeds bound")
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("PYTHON")},
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    deadline = time.monotonic() + timeout
    with (
        (output / "mlx-worker.log").open("wb") as log,
        subprocess.Popen(
            [python, "-I", "-B", str(source / "mlx_worker.py")],
            cwd=source,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
        ) as process,
    ):
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        buffer = b""
        result = None
        cancelled = False
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                selector.register(process.stdin, selectors.EVENT_WRITE)
                transmitted = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("MLX component deadline exceeded")
                    events = selector.select(min(remaining, 1))
                    if not events:
                        if process.poll() is not None:
                            break
                        continue
                    for key, mask in events:
                        if key.fileobj is process.stdin:
                            try:
                                transmitted += os.write(
                                    process.stdin.fileno(), payload[transmitted : transmitted + 65536]
                                )
                            except BrokenPipeError as error:
                                raise ValueError("MLX worker closed its request channel") from error
                            if transmitted == len(payload):
                                selector.unregister(process.stdin)
                                process.stdin.close()
                    if not any(key.fileobj is process.stdout for key, mask in events):
                        continue
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    buffer += chunk
                    if len(buffer) > MAX_MESSAGE:
                        raise ValueError("MLX IPC message exceeds bound")
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        event = json.loads(line)
                        kind = event.get("type")
                        if kind == "checkpoint":
                            path = Path(event["path"]).resolve(strict=True)
                            if not path.is_relative_to(output):
                                raise ValueError("MLX checkpoint outside artifact root")
                            from modelbuilder.backend.components import read_checkpoint

                            read_checkpoint(path)
                            if context.get("checkpoint_ready"):
                                context["checkpoint_ready"](
                                    path=path, step=event["step"], purpose=event["purpose"]
                                )
                        elif kind == "result":
                            if result is not None:
                                raise ValueError("Duplicate MLX result")
                            result = event["outputs"]
                        elif kind == "cancelled":
                            cancelled = True
                        elif kind == "error":
                            raise ValueError("MLX worker rejected request: " + event["message"])
                        else:
                            raise ValueError("Unknown MLX IPC event")
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if cancelled:
                raise InterruptedError("MLX cancelled after checkpoint publication")
            if code or result is None or buffer:
                raise ValueError("Incomplete MLX worker result")
            expected = {
                "train": {"trained"},
                "evaluate": {"metrics"},
                "generate": {"generation"},
                "export": {"exported"},
            }[operation]
            if set(result) != expected:
                raise ValueError("MLX output ports do not match operation")
            for name in ["trained", "exported"]:
                if name in result:
                    path = Path(result[name]).resolve(strict=True)
                    if not path.is_relative_to(output):
                        raise ValueError("MLX result outside artifact root")
                    from modelbuilder.backend.components import read_checkpoint, read_export

                    (read_checkpoint if name == "trained" else read_export)(path)
                    result[name] = path
            return result
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)


def train(**kwargs):
    return execute("train", **kwargs)


def evaluate(**kwargs):
    return execute("evaluate", **kwargs)


def generate(**kwargs):
    return execute("generate", **kwargs)


def export(**kwargs):
    return execute("export", **kwargs)
