"""Capture native execution identity before stage isolation; verify it in children."""

from copy import deepcopy
from pathlib import Path

from .common import read_json


def capture(spec):
    from zenml import get_step_context

    ctx = get_step_context()
    run = ctx.pipeline_run
    if (
        run.config.extra.get("application_project_id") != spec["project_id"]
        or run.config.extra.get("recipe_digest") != spec["recipe_digest"]
    ):
        raise ValueError("Epoch native subject differs from immutable run provenance")
    return {
        "project_id": spec["project_id"],
        "run_id": str(run.id),
        "backend_project_id": str(run.project.id),
        "config_digest": spec["recipe_digest"],
        "step_id": str(ctx.step_run.id),
        "node_id": spec["node_id"],
        "invocation_id": spec.get("invocation", {}).get("invocation_id", spec["node_id"]),
    }


def verify(spec, native):
    from zenml.client import Client

    if (
        not isinstance(native, dict)
        or set(native)
        != {
            "project_id",
            "run_id",
            "backend_project_id",
            "config_digest",
            "step_id",
            "node_id",
            "invocation_id",
        }
        or native["project_id"] != spec["project_id"]
        or native["config_digest"] != spec["recipe_digest"]
        or native["node_id"] != spec["node_id"]
        or native["invocation_id"] != spec.get("invocation", {}).get("invocation_id", spec["node_id"])
    ):
        raise ValueError("Epoch subject is missing or conflicts with dispatch identity")
    receipt = read_json(Path(spec["job_dir"]) / "native.json")
    if (
        receipt.get("backend_run_id") != native["run_id"]
        or receipt.get("backend_project_id") != native["backend_project_id"]
    ):
        raise ValueError("Epoch subject differs from native job ownership")
    run = Client().get_pipeline_run(native["run_id"])
    if (
        str(run.project.id) != native["backend_project_id"]
        or run.config.extra.get("application_project_id") != native["project_id"]
        or run.config.extra.get("recipe_digest") != native["config_digest"]
        or native["invocation_id"] not in run.steps
        or str(run.steps[native["invocation_id"]].id) != native["step_id"]
    ):
        raise ValueError("Epoch subject does not belong to the immutable native run/step")
    return deepcopy(native)
