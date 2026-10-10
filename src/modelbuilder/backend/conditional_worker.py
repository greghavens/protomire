"""Sequential native dynamic execution for explicit terminal boolean gates."""
from __future__ import annotations

from pathlib import Path
from zenml import log_metadata, pipeline
from zenml.client import Client

from modelbuilder.conditions import condition_decision
from modelbuilder.backend.common import write_json
from modelbuilder.backend.native_cache import value_digest
from modelbuilder.backend.native_inputs import validate_native_type


@pipeline(name="modelbuilder_conditional_recipe", dynamic=True, enable_cache=True)
def execute_conditional_recipe(job_directory: str):
    # The public dynamic pipeline must have an importable top-level definition.
    # No submit/thread calls: source imports, deadlines and job state are shared.
    from modelbuilder.backend.worker import prepare_run, invoke_nodes
    job = Path(job_directory)
    prepared = prepare_run(job)
    request = prepared["request"]
    native = Client().get_pipeline_run(request["run_name"], project=prepared["project"].id)
    write_json(job / "native.json", {"backend_run_id": str(native.id),
        "model_version_ref": str(native.model_version.id) if native.model_version else None,
        "backend_project_id": str(prepared["project"].id)})
    outcomes = {}

    def decide(node, artifact):
        validate_native_type(artifact, "bool")
        if str(artifact.project.id) != str(prepared["project"].id):
            raise ValueError("Conditional gate artifact belongs to another project")
        value = artifact.load()
        allowed = condition_decision(node["condition"], value)
        outcomes[node["id"]] = {"status": "eligible" if allowed else "skipped",
            "condition": node["condition"], "gate_artifact_id": str(artifact.id),
            "gate_value": value, "gate_value_sha256": value_digest(value),
            "status_source": "native_run_metadata"}
        # A skipped function creates no fabricated StepRun or placeholder output.
        # The decision itself is persisted on the authoritative native run.
        log_metadata({"conditional_execution": outcomes}, run_id_name_or_prefix=native.id)
        return allowed

    invoke_nodes(prepared, condition_callback=decide)
