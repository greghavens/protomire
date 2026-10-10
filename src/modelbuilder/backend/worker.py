"""Translate the canonical recipe DAG into native typed ZenML pipeline steps."""
from __future__ import annotations
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Annotated

from zenml import ArtifactConfig, Model, get_step_context, log_metadata, pipeline, step
from zenml.client import Client
from zenml.config import StepRetryConfig

from . import fixtures
from .components.runtime import compile_component, compile_python
from .components import live_evaluator
from .execution_limits import deadline_start, deadline_end
from ..evaluation import fixture_evaluation_records, compare_records
from .common import recipe_provenance, read_json, write_json


def context(spec: dict) -> tuple[dict, Path]:
    job = Path(spec["job_dir"])
    ctx = get_step_context()
    write_json(job / "native.json", {"backend_run_id": str(ctx.pipeline_run.id),
                                    "model_version_ref": str(ctx.model_version.id) if ctx.model_version else None,
                                    "backend_project_id": str(ctx.pipeline_run.project.id)})
    log_metadata({"application_operation_id": job.name, "recipe_digest": spec["recipe_digest"],
                  "node_id": spec["node_id"], "parent_run_id": spec.get("parent_run_id", ""),
                  "parent_checkpoint_ref": spec.get("parent_checkpoint_ref", ""),
                  "resume_mode": spec.get("resume_mode", ""), "placement": spec.get("placement", {"device": "cpu"}), **spec.get("recipe_provenance", {})})
    log_metadata(spec.get("recipe_provenance", {}), run_id_name_or_prefix=ctx.pipeline_run.id)
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
def import_dataset_version_step(spec: dict) -> Annotated[dict, "data"]:
    from ..dataset import load_portable_split
    parameters, _ = context(spec)
    declared = {dependency["id"]: dependency for dependency in spec.get("dependencies", [])}
    identifier = parameters["reference_id"]
    reference = Path(spec["dependency_paths"][identifier])
    if identifier not in declared:
        raise ValueError("Dataset portable reference is undeclared")
    with reference.open("rb") as stream:
        content = stream.read(16 * 1024 * 1024 + 1)
    if len(content) > 16 * 1024 * 1024:
        raise ValueError("Dataset portable reference is oversized")
    if hashlib.sha256(content).hexdigest() != declared[identifier]["sha256"]:
        raise ValueError("Dataset portable reference changed after admission")
    return load_portable_split(json.loads(content),
        {identifier: path for identifier, path in spec["dependency_paths"].items() if identifier in declared},
        parameters["split_id"], purpose=parameters.get("purpose", "training"),
        expected_project_id=spec["recipe_provenance"]["application_project_id"],
        source_guards=(spec.get("dataset_source_guards") or None)
        if declared[identifier].get("metadata", {}).get("dataset_reference") else None)


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
    parameters = {**parameters, "_execution_device": spec.get("placement", {}).get("device", "cpu"), "_cancel_file": str(Path(spec["job_dir"]) / "cancel.json"),
                  "resume_mode": spec.get("resume_mode", "exact_state"), "checkpoint_kind": spec.get("checkpoint_kind", "last")}
    coordinator = None
    if spec.get("live_evaluation"):
        from .live_evaluation import LiveEvaluationCoordinator
        live = spec["live_evaluation"]
        settings = live["settings"]
        coordinator = LiveEvaluationCoordinator(Path(spec["job_dir"]), spec["node_id"],
            {"kind": "fixture", "data": data, "parameters": live["node"]["parameters"]},
            max_concurrency=settings.get("max_concurrency", 1), timeout_seconds=settings.get("timeout_seconds", 300))
        parameters["_checkpoint_ready"] = coordinator.checkpoint_ready
    try:
        result = fixtures.train(data, initial, parameters, out, resume=Path(spec["resume_path"]) if spec.get("resume_path") else None)
    except BaseException:
        if coordinator:
            coordinator.close(cancel=True)
        raise
    else:
        if coordinator:
            coordinator.close()

    ck = fixtures.load_checkpoint(result)
    log_metadata({"dataset_sha256": ck["dataset_sha256"], "best_validation_loss": ck["best_validation_loss"],
                  "best_step": ck["best_step"], "kl": ck["training_contract"]}, infer_model=True)
    return result


@step(enable_cache=False)
def evaluate_step(data: dict, checkpoint: Path, spec: dict) -> Annotated[dict, "metrics"]:
    parameters, out = context(spec)
    metrics = fixtures.evaluate(data, checkpoint, parameters, out)
    metrics["evaluation_records"] = fixture_evaluation_records(data, checkpoint, parameters, metrics)
    log_metadata({k: v for k, v in metrics.items() if k not in {"heldout_logits", "resume_mode", "evaluation_records"}}, infer_model=True)
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
    comparison = compare_records([
        {"backend_run_id": "left", "status": "succeeded", "metrics": left},
        {"backend_run_id": "right", "status": "succeeded", "metrics": right}], metric,
        parameters.get("direction", "minimize"), parameters.get("comparison_contract"))
    return {**comparison, "winner": comparison["selected_run_id"], "left": left.get(metric), "right": right.get(metric)}



STEPS = {"dataset.version.import": import_dataset_version_step, "dataset.import": import_data_step, "dataset.synthetic": dataset_step, "dataset.validate": validate_data_step,
         "model.initialize": initialize_step, "model.train": train_step, "model.evaluate": evaluate_step,
         "model.checkpoint": checkpoint_step, "model.export": export_step, "model.compare": compare_step}


def topological_nodes(recipe: dict) -> list[dict]:
    from ..conditions import control_dependencies
    nodes = {n["id"]: n for n in recipe["nodes"]}
    dependencies = [(e["source_node"], e["target_node"]) for e in recipe["edges"]] + control_dependencies(recipe)
    pending, result = set(nodes), []
    while pending:
        ready = sorted(n for n in pending if all(source not in pending for source, target in dependencies if target == n))
        if not ready:
            raise ValueError("Recipe graph contains a cycle")
        for n in ready:
            result.append(nodes[n])
            pending.remove(n)
    return result


def prepare_run(job: Path) -> dict:
    request = read_json(job / "request.json")
    os.environ["MODELBUILDER_JOB_DIR"] = str(job)
    client = Client()
    project_name = f"modelbuilder-{request['project_id']}"
    try:
        project = client.get_project(project_name)
    except KeyError:
        project = client.create_project(project_name, "Application model project " + request["project_id"])
    os.environ["ZENML_ACTIVE_PROJECT_ID"] = str(project.id)
    recipe = request["recipe"]
    identity = recipe_provenance(recipe)
    digest = identity["recipe_digest"]
    provenance = {key: value for key, value in {
        **identity, "application_project_id": request["project_id"],
        "parent_recipe_id": request.get("parent_recipe_id"), "parent_recipe_revision": request.get("parent_recipe_revision"),
        "parent_recipe_digest": request.get("parent_recipe_digest")}.items() if value is not None}
    from ..execution import selection, selected_recipe
    from .native_inputs import resolve_bindings
    execution = selection(recipe, request.get("execution"))
    resolved, bound_inputs = (resolve_bindings(request, request.get("input_binding_jobs", []), native_objects=True)
                              if execution["input_bindings"] else ([], {}))
    if resolved != request.get("resolved_input_bindings", []):
        raise ValueError("Native artifact binding provenance changed after admission")
    execution_provenance = {**execution, "input_bindings": resolved}
    provenance["execution"] = execution_provenance
    provenance["placement"] = request.get("placement", {"device": "cpu"})
    active_recipe = selected_recipe(recipe, execution)
    ordered = [node for node in topological_nodes(recipe) if node["id"] in execution["node_ids"]]
    functions = {n["id"]: (compile_python(n, job) if n["kind"] == "code.python" else compile_component(n, job) if n["kind"] == "component.run" else STEPS[n["kind"]]) for n in ordered}

    from .native_cache import cache_identity
    from .components.runtime import runtime_environment
    actual_environment = runtime_environment() if any(n.get("cache_policy") == "enabled" for n in ordered) else None
    cache_identities = {n["id"]: cache_identity(request, n, execution_provenance, actual_environment)
                        for n in ordered if n.get("cache_policy") == "enabled"}

    return {"job": job, "request": request, "project": project, "recipe": recipe, "digest": digest, "provenance": provenance, "execution": execution, "execution_provenance": execution_provenance, "active_recipe": active_recipe, "ordered": ordered, "functions": functions, "bound_inputs": bound_inputs, "cache_identities": cache_identities}


def invoke_nodes(prepared: dict, condition_callback=None) -> None:
    """Bind typed calls; native ZenML alone executes/schedules each invocation."""
    from .native_cache import configure_cache
    job = prepared["job"]
    request = prepared["request"]
    recipe = prepared["recipe"]
    digest = prepared["digest"]
    provenance = prepared["provenance"]
    execution = prepared["execution"]
    execution_provenance = prepared["execution_provenance"]
    active_recipe = prepared["active_recipe"]
    ordered = prepared["ordered"]
    functions = prepared["functions"]
    bound_inputs = prepared["bound_inputs"]
    cache_identities = prepared["cache_identities"]
    outputs = {}
    for node in ordered:
        if "condition" in node:
            if condition_callback is None:
                raise ValueError("Conditional nodes require the supported native dynamic executor")
            gate = node["condition"]
            artifact = outputs[(gate["source_node"], gate["source_port"])]
            if not condition_callback(node, artifact):
                continue
        kwargs = {e["target_port"]: outputs[(e["source_node"], e["source_port"])]
                  for e in recipe["edges"] if e["target_node"] == node["id"] and e["source_node"] in execution["node_ids"]}
        kwargs.update({port: artifact for (target, port), artifact in bound_inputs.items() if target == node["id"]})
        function = functions[node["id"]].with_options(
            on_start=deadline_start, on_end=deadline_end, enable_cache=False)
        if node["id"] in cache_identities:
            function = configure_cache(function, node, job, cache_identities[node["id"]])
        retry = node.get("retry", {})
        retries = int(retry.get("max_attempts", 1)) - 1
        if retries > 0:
            function = function.with_options(retry=StepRetryConfig(max_retries=retries, delay=int(retry.get("backoff_seconds", 0))))
        if node["kind"] == "code.python":
            kwargs.update(node.get("parameters", {}))
        kwargs["spec"] = {"job_dir": str(job), "node_id": node["id"], "parameters": node.get("parameters", {}),
                              "placement": request.get("placement", {"device": "cpu"}),
                              "recipe_digest": digest, "recipe_provenance": provenance, "dependency_paths": request.get("dependency_paths", {}),
                              "dataset_source_guards": request.get("dataset_source_guards", {}),
                              "dependencies": active_recipe.get("resources", []) + active_recipe.get("external_dependencies", []),
                              "execution": execution_provenance, "component_node": node, "source": recipe.get("source", {}), "environment": recipe.get("environment", {}),
                              "source_root": request["source_root"], "trusted_code_roots": request.get("trusted_code_roots", []), **{k: request[k] for k in ("resume_path", "resume_mode", "checkpoint_kind", "parent_run_id", "parent_checkpoint_ref") if k in request}}
        kwargs["spec"]["live_evaluation"] = live_evaluator(node, active_recipe, input_bindings=execution_provenance["input_bindings"])
        kwargs["spec"]["cache_identity"] = cache_identities.get(node["id"])
        result = function(id=node["id"], **kwargs)
        declared = node["outputs"]
        values = result if isinstance(result, (tuple, list)) else (result,)
        if len(values) != len(declared):
            raise ValueError("Native node outputs disagree with declared typed ports")
        for port, value in zip(declared, values):
            outputs[(node["id"], port["name"])] = value


def run(job: Path) -> None:
    prepared = prepare_run(job)
    request, project, provenance = (prepared[key] for key in ("request", "project", "provenance"))
    if any("condition" in node for node in prepared["ordered"]):
        from .conditional_worker import execute_conditional_recipe
        execute_recipe = execute_conditional_recipe
        arguments = {"job_directory": str(job)}
    else:
        @pipeline(name="modelbuilder_recipe", enable_cache=True)
        def execute_recipe():
            invoke_nodes(prepared)
        arguments = {}

    model_name = f"model-{request['project_id']}"
    native = execute_recipe.with_options(model=Model(name=model_name, version=request["operation_id"],
                                                     description=request.get("model_name", "Modelbuilder model lifecycle")),
                                         run_name=request["run_name"], extra=provenance)(**arguments)
    log_metadata(provenance, run_id_name_or_prefix=native.id)
    write_json(job / "native.json", {"backend_run_id": str(native.id),
                                     "model_version_ref": str(native.model_version.id) if native.model_version else None,
                                     "backend_project_id": str(project.id)})
    if native.model_version:
        log_metadata({**provenance,
                      "parent_run_id": request.get("parent_run_id", ""),
                      "parent_checkpoint_ref": request.get("parent_checkpoint_ref", ""),
                      "resume_mode": request.get("resume_mode", ""), "selection_decision": request.get("decision", {})},
                     model_version_id=native.model_version.id)


if __name__ == "__main__":
    run(Path(sys.argv[1]))
