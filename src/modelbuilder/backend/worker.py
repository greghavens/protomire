"""Translate the canonical recipe DAG into native typed ZenML pipeline steps."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Annotated

from zenml import ArtifactConfig, Model, get_step_context, log_metadata, pipeline, step
from zenml.client import Client
from zenml.config import StepRetryConfig

from . import fixtures
from .common import read_json, write_json


def context(spec: dict) -> tuple[dict, Path]:
    job = Path(spec["job_dir"])
    ctx = get_step_context()
    write_json(job / "native.json", {"backend_run_id": str(ctx.pipeline_run.id),
                                    "model_version_ref": str(ctx.model_version.id) if ctx.model_version else None,
                                    "backend_project_id": str(ctx.pipeline_run.project.id)})
    log_metadata({"application_operation_id": job.name, "recipe_digest": spec["recipe_digest"],
                  "node_id": spec["node_id"], "parent_run_id": spec.get("parent_run_id", ""),
                  "parent_checkpoint_ref": spec.get("parent_checkpoint_ref", ""),
                  "resume_mode": spec.get("resume_mode", "")})
    out = job / "outputs" / spec["node_id"]
    return spec["parameters"], out


@step(enable_cache=False)
def dataset_step(spec: dict) -> Annotated[dict, "data"]:
    parameters, _ = context(spec)
    return fixtures.dataset(parameters)


@step(enable_cache=False)
def import_data_step(spec: dict) -> Annotated[dict, "data"]:
    parameters, _ = context(spec)
    identifier = parameters["resource_id"]
    path = Path(spec["dependency_paths"][identifier])
    dependency = next(d for d in spec["dependencies"] if d["id"] == identifier)
    if path.stat().st_size > 16 * 1024 * 1024 or hashlib.sha256(path.read_bytes()).hexdigest() != dependency["sha256"]:
        raise ValueError("Imported dataset exceeds limit or fails declared SHA256")
    data = json.loads(path.read_text())
    fixtures.verify_data(data)
    return data


@step(enable_cache=False)
def validate_data_step(data: dict, spec: dict) -> Annotated[dict, "data"]:
    context(spec)
    fixtures.verify_data(data)
    return data


@step(enable_cache=False)
def initialize_step(data: dict, spec: dict) -> Annotated[Path, ArtifactConfig(name="initial", is_model_artifact=True)]:
    parameters, out = context(spec)
    return fixtures.initialize(data, parameters, out)


@step(enable_cache=False)
def train_step(data: dict, initial: Path, spec: dict) -> Annotated[Path, ArtifactConfig(name="checkpoint", is_model_artifact=True)]:
    parameters, out = context(spec)
    parameters = {**parameters, "_cancel_file": str(Path(spec["job_dir"]) / "cancel.json"),
                  "resume_mode": spec.get("resume_mode", "exact_state"), "checkpoint_kind": spec.get("checkpoint_kind", "last")}
    result = fixtures.train(data, initial, parameters, out, resume=Path(spec["resume_path"]) if spec.get("resume_path") else None)
    ck = fixtures.load_checkpoint(result)
    log_metadata({"dataset_sha256": ck["dataset_sha256"], "best_validation_loss": ck["best_validation_loss"],
                  "best_step": ck["best_step"], "kl": ck["training_contract"]}, infer_model=True)
    return result


@step(enable_cache=False)
def evaluate_step(data: dict, checkpoint: Path, spec: dict) -> Annotated[dict, "metrics"]:
    parameters, out = context(spec)
    metrics = fixtures.evaluate(data, checkpoint, parameters, out)
    log_metadata({k: v for k, v in metrics.items() if k not in {"heldout_logits", "resume_mode"}}, infer_model=True)
    return metrics


@step(enable_cache=False)
def checkpoint_step(checkpoint: Path, spec: dict) -> Annotated[Path, ArtifactConfig(name="checkpoint", is_model_artifact=True)]:
    context(spec)
    fixtures.load_checkpoint(checkpoint)
    return checkpoint


@step(enable_cache=False)
def export_step(data: dict, checkpoint: Path, spec: dict) -> Annotated[Path, ArtifactConfig(name="export", is_model_artifact=True)]:
    parameters, out = context(spec)
    return fixtures.export_model(data, checkpoint, parameters, out)


@step(enable_cache=False)
def compare_step(left: dict, right: dict, spec: dict) -> Annotated[dict, "comparison"]:
    parameters, _ = context(spec)
    metric = parameters.get("metric", "validation_loss")
    maximize = parameters.get("direction", "minimize") == "maximize"
    winner = "left" if ((left[metric] > right[metric]) if maximize else (left[metric] < right[metric])) else "right"
    return {"metric": metric, "direction": "maximize" if maximize else "minimize", "winner": winner,
            "left": left[metric], "right": right[metric], "delta": right[metric] - left[metric]}


STEPS = {"dataset.import": import_data_step, "dataset.synthetic": dataset_step, "dataset.validate": validate_data_step,
         "model.initialize": initialize_step, "model.train": train_step, "model.evaluate": evaluate_step,
         "model.checkpoint": checkpoint_step, "model.export": export_step, "model.compare": compare_step}


def topological_nodes(recipe: dict) -> list[dict]:
    nodes = {n["id"]: n for n in recipe["nodes"]}
    pending, result = set(nodes), []
    while pending:
        ready = sorted(n for n in pending if all(e["source_node"] not in pending for e in recipe["edges"] if e["target_node"] == n))
        if not ready:
            raise ValueError("Recipe graph contains a cycle")
        for n in ready:
            result.append(nodes[n])
            pending.remove(n)
    return result


def python_step(node: dict, roots: list[str], source_root: str):
    reference = node["implementation_ref"]
    path = (Path(source_root) / reference["path"]).resolve()
    if not any(path.is_relative_to(Path(root)) for root in roots) or hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
        raise ValueError("Python source is not within a configured trusted root or changed after approval")
    spec = importlib.util.spec_from_file_location(reference["module"], path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[reference["module"]] = module
    spec.loader.exec_module(module)
    return step(getattr(module, reference["entrypoint"]), enable_cache=False)


def run(job: Path) -> None:
    request = read_json(job / "request.json")
    client = Client()
    project_name = f"modelbuilder-{request['project_id']}"
    try:
        project = client.get_project(project_name)
    except KeyError:
        project = client.create_project(project_name, "Application model project " + request["project_id"])
    os.environ["ZENML_ACTIVE_PROJECT_ID"] = str(project.id)
    recipe = request["recipe"]
    digest = recipe.get("digest") or hashlib.sha256(json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    ordered = topological_nodes(recipe)
    functions = {n["id"]: (python_step(n, request.get("trusted_code_roots", []), request["source_root"]) if n["kind"] == "code.python" else STEPS[n["kind"]]) for n in ordered}

    @pipeline(name="modelbuilder_recipe", enable_cache=False)
    def execute_recipe():
        outputs = {}
        for node in ordered:
            kwargs = {e["target_port"]: outputs[(e["source_node"], e["source_port"])]
                      for e in recipe["edges"] if e["target_node"] == node["id"]}
            function = functions[node["id"]]
            retry = node.get("retry", {})
            retries = int(retry.get("max_attempts", 1)) - 1
            if retries > 0:
                function = function.with_options(retry=StepRetryConfig(max_retries=retries, delay=int(retry.get("backoff_seconds", 0))))
            if node["kind"] == "code.python":
                kwargs.update(node.get("parameters", {}))
            else:
                kwargs["spec"] = {"job_dir": str(job), "node_id": node["id"], "parameters": node.get("parameters", {}),
                                  "recipe_digest": digest, "dependency_paths": request.get("dependency_paths", {}),
                                  "dependencies": recipe.get("resources", []) + recipe.get("external_dependencies", []), **{k: request[k] for k in ("resume_path", "resume_mode", "checkpoint_kind", "parent_run_id", "parent_checkpoint_ref") if k in request}}
            result = function(id=node["id"], **kwargs)
            declared = node["outputs"]
            values = result if isinstance(result, tuple) else (result,)
            if len(values) != len(declared):
                raise ValueError("Native node outputs disagree with declared typed ports")
            for port, value in zip(declared, values):
                outputs[(node["id"], port["name"])] = value

    model_name = f"model-{request['project_id']}"
    native = execute_recipe.with_options(model=Model(name=model_name, version=request["operation_id"],
                                                     description=request.get("model_name", "Modelbuilder model lifecycle")),
                                         run_name=request["run_name"])()
    write_json(job / "native.json", {"backend_run_id": str(native.id),
                                     "model_version_ref": str(native.model_version.id) if native.model_version else None,
                                     "backend_project_id": str(project.id)})
    if native.model_version:
        log_metadata({"application_project_id": request["project_id"], "recipe_digest": digest,
                      "parent_run_id": request.get("parent_run_id", ""),
                      "parent_checkpoint_ref": request.get("parent_checkpoint_ref", ""),
                      "resume_mode": request.get("resume_mode", ""), "selection_decision": request.get("decision", {})},
                     model_version_id=native.model_version.id)


if __name__ == "__main__":
    run(Path(sys.argv[1]))
