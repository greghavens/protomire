"""Opt-in actual offline Moonshiner through service, ZenML and a pinned runner.

Only test-authored traces and a teacher identity fixture are reviewed. This does
not exercise provider generation, a real teacher model, training or GUI flows.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.components import identity
from modelbuilder.backend.components.moonshiner import catalog_entries, UPSTREAM_VERSION, MESSAGE_SCHEMA
from modelbuilder.backend.fixture_artifacts import attach_declared_rights
from modelbuilder.backend.runner import inspect_environment
from modelbuilder.dataset import DatasetRegistry
from modelbuilder.generated_artifacts import prepared_rights
from modelbuilder.models import Actor, OPERATIONS
from modelbuilder.service import OperationService

ROOT = Path(__file__).resolve().parents[2]
BACKEND = Path(os.environ.get("MODELBUILDER_BACKEND_PYTHON", ROOT / ".venv-backend/bin/python"))
INSTALLED = os.environ.get("MODELBUILDER_MOONSHINER_TEST_PYTHON")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    sys.platform != "linux" or not INSTALLED or not BACKEND.is_file(),
    reason="Explicit reviewed Moonshiner and existing native backend interpreters required")]
OWNER = Actor("fixture-owner", {"read", "modify", "execute", "publish"})
USAGE = {"purpose": "Offline owned Moonshiner native acceptance", "commercial": False,
         "deployment": "isolated local test", "conditions": ["Test-authored fixtures only; no provider execution"]}
TERMS = ("Test-authored synthetic fixture bytes only. Permission to download locally, train, "
         "evaluate, generate and export these exact fixture bytes in isolated offline tests. "
         "No external assets, real teacher weights, provider calls or publication permission.\n")
HARNESS = """from modelbuilder.backend.components.moonshiner import prepare_dataset


def compose(*, inputs, parameters, context):
    return prepare_dataset(inputs=inputs, parameters=parameters, context=context)
"""


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def test_official_installed_compose_service_zenml_source_bound_runner(tmp_path):
    source, data, teacher = (tmp_path / name for name in ("source", "data", "teacher"))
    for root in (source, data, teacher):
        root.mkdir()
        (root / "LICENSE.txt").write_text(TERMS)
    (source / "harness.py").write_text(HARNESS)
    rows = [{"id": "trace-" + str(i), "task": "owned-trace-" + str(i), "split": "train",
             "teacher_model": "owned-fixture-teacher", "teacher_runtime": "fixture",
             "source_trajectory_id": "owned-trajectory-" + str(i),
             "source_trajectory_sha256": hashlib.sha256(str(i).encode()).hexdigest(),
             "messages": [{"role": "system", "content": "Answer the prompt"},
                          {"role": "user", "content": "Question " + str(i)},
                          {"role": "assistant", "content": "Previous assistant context", "reasoning_content": "not a label"},
                          {"role": "tool", "content": "Observed result", "tool_call_id": "call-1"},
                          {"role": "assistant", "content": "Final answer " + str(i), "reasoning_content": "also not a label"}],
             "target_message_index": 4} for i in range(2)]
    (data / "traces.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    input_manifest = {"name": "Owned input traces", "benchmark": "owned-source-traces", "license": "test-only",
        "provenance": {"source": "Test-authored synthetic canonical traces", "source_revision": "1"},
        "sources": [{"id": "traces", "root_id": "data", "path": "traces.jsonl", "format": "jsonl",
                     "sha256": hashlib.sha256((data / "traces.jsonl").read_bytes()).hexdigest()}],
        "id_column": "id", "row_schema": {"type": "object", "additionalProperties": False,
            "required": list(rows[0]), "properties": {**{key: {"type": "string"} for key in rows[0]
                if key not in {"messages", "target_message_index"}},
                "messages": {"type": "array", "items": MESSAGE_SCHEMA}, "target_message_index": {"type": "integer"}}},
        "splits": [{"id": "train", "role": "train", "source_ids": ["traces"]}], "transformations": []}
    dump(teacher / "teacher.json", {"id": "owned-fixture-teacher", "runtime": "fixture", "real_model": False})
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Owned Acceptance", "-c",
                    "user.email=owned@example.invalid", "commit", "-qm", "Owned Moonshiner workflow fixture"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    file_ref = {"path": "harness.py", "sha256": hashlib.sha256((source / "harness.py").read_bytes()).hexdigest()}
    source_contract = {"repository": "urn:sha256:" + file_ref["sha256"], "commit": commit, "files": [file_ref]}
    profile = inspect_environment(INSTALLED)
    assert profile["packages"]["moonshiner"] == UPSTREAM_VERSION
    profile_path = tmp_path / "runner-environment.json"
    dump(profile_path, profile)
    profile_hash = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    probe = subprocess.run([INSTALLED, "-I", "-B", "-c",
        "import sys,json;sys.path.insert(0,sys.argv[1]);from modelbuilder.backend.components.moonshiner_process import installation_identity;print(json.dumps(installation_identity()))",
        str(ROOT / "src")], capture_output=True, text=True, timeout=60)
    assert probe.returncode == 0, probe.stdout + probe.stderr
    installation = json.loads(probe.stdout)
    dump(tmp_path / "installation.json", installation)
    backend = ZenMLBackend(tmp_path / "state", python_executable=str(BACKEND), source_root=source,
        trusted_code_roots=[str(source)], dependency_paths={"moonshiner": str(profile_path)},
        runner_environments={"moonshiner": {"python_executable": INSTALLED, "profile_sha256": profile_hash}})
    service = OperationService(tmp_path / "service.sqlite", backend=backend, source_roots={
        "owned": {"path": str(source), "trusted": True, "dependency_paths": {"moonshiner": str(profile_path)}}})
    project = None
    submitted = None
    def call(name, payload=None):
        request = {**({"project_id": project["project_id"]} if project else {}), **(payload or {})}
        if OPERATIONS[name].mutating:
            request.setdefault("idempotency_key", str(uuid4()))
        return service.invoke(name, request, OWNER)
    def review(alias, kind, names, **extra):
        evidence = call("hub.license.local.inspect", {"source_alias": alias, "repo_type": kind,
            "files": [*names, "LICENSE.txt"], "max_bytes": 1024 * 1024, "declared_license": "unknown", **extra})
        assert evidence["evidence"]["documents"][0]["text"] == TERMS
        policy = call("hub.license.review", {"evidence_id": evidence["evidence_id"],
            "evidence_digest": evidence["evidence_digest"], "license_document_paths": ["LICENSE.txt"],
            "permitted_uses": ["download", "train", "evaluate", "generate", "export"],
            "intended_context": "Explicit review only of these test-authored fixture bytes",
            "reviewed_usage_context": USAGE, "required_files": [], "required_attributions": [],
            "allowed_destination_licenses": [], "gated_terms_acknowledged": False, "confirm_review": True})
        return evidence, policy
    try:
        repository = service.source_workspaces.inspect("owned")
        project = call("project.create", {"slug": "moonshiner-native", "model_name": "Owned offline compose",
            "source_alias": "owned", "source_repository_id": repository["repository_id"], "trusted": True})
        service.hub.publication_roots[project["project_id"]] = {"source": str(source), "data": str(data), "teacher": str(teacher)}
        policies = {}
        recipe = call("recipe.get", {"recipe_id": project["recipe_id"]})
        service._dataset_registry = DatasetRegistry(service.store, project_roots={project["project_id"]: {"data": str(data)}})
        registered_input = call("dataset.register", {"manifest": input_manifest})
        _, policies["data"] = review("data", "dataset", ["traces.jsonl"])
        call("hub.dataset.policy.bind", {"dataset_id": registered_input["dataset_id"], "dataset_revision": 1,
            "source_policies": {"traces": policies["data"]["policy_id"]}, "intended_use": "train", "usage_context": USAGE})
        evidence, policies["teacher"] = review("teacher", "model", ["teacher.json"])
        teacher_asset = call("hub.local.register", {"evidence_id": evidence["evidence_id"],
            "license_policy_id": policies["teacher"]["policy_id"]})
        recipe = call("hub.asset.bind", {"asset_id": teacher_asset["asset_id"], "expected_revision": recipe["revision"]})
        teacher_resource = next(item for item in teacher_asset["resources"] if item["metadata"]["path"] == "teacher.json")
        parameters = {"mode": "offline-compose", "installation": installation, "traces_digest": "0" * 64,
            "teacher": {"model": "owned-fixture-teacher", "runtime": "fixture", "rights_parent_id": teacher_resource["id"]},
            "limits": {"max_records": 4, "max_bytes": 1024 * 1024, "timeout_seconds": 60, "max_cost_usd": 0},
            "seed": 17, "name": "Owned composed traces", "benchmark": "owned-composed-traces", "license": "test-only",
            "root_id": "prepared", "review_note": "Exact synthetic trace and teacher identity fixture review",
            "split_policy": "train-only", "label_policy": "final-assistant-content-only"}
        shared = {"kind": "component.run", "environment_ref": "default", "resource_request": {"gpu": 0},
            "timeout": 120, "retry": {"max_attempts": 1, "backoff_seconds": 0}, "cache_policy": "disabled",
            "provenance": {"intended_use": "train"}}
        reference = {**file_ref, "module": "owned_moonshiner_workflow", "commit": commit}
        catalog = call("component.get", {"component_id": catalog_entries()[0]["id"]})
        descriptor = {key: catalog[key] for key in ("id", "version", "protocol", "role", "description", "parameter_schema", "capabilities")}
        descriptor["runner"] = {"protocol": "modelbuilder.runner/v1",
            "stack": [{"id": "moonshiner", "category": "training-library", "distribution": "moonshiner", "version": UPSTREAM_VERSION}],
            "launcher": {"kind": "python", "processes": 1},
            "contracts": {"model": "source-owned-native", "dataset": "native-component-inputs",
                          "checkpoint": "modelbuilder.checkpoint/v1", "evaluation": "modelbuilder.evaluation/v1"}}
        compose = {**shared, "id": "compose", "environment_ref": "moonshiner",
            "implementation_ref": {**reference, "entrypoint": "compose"}, "inputs": catalog["inputs"],
            "outputs": catalog["outputs"], "parameters": parameters, "component": descriptor}
        recipe.update(source=source_contract, nodes=[compose], edges=[],
            environment={"python": "3.12", "requirements": ["zenml==0.97.0"]})
        recipe["resources"].append({"id": "moonshiner", "kind": "environment", "url": "urn:sha256:" + profile_hash,
                                    "sha256": profile_hash, "license": "operator-reviewed-runtime"})
        recipe = call("recipe.update", {"recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"], "recipe": recipe})
        _, policies["source"] = review("source", "source", ["harness.py"],
            source_repository_id=repository["repository_id"], source_commit=commit)
        recipe = call("hub.source.policy.bind", {"recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"],
            "expected_project_revision": project["revision"], "license_policy_id": policies["source"]["policy_id"],
            "intended_use": "train", "usage_context": USAGE})["recipe"]
        bound_input = call("dataset.bind", {"recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"],
            "dataset_id": registered_input["dataset_id"], "dataset_revision": 1, "split_id": "train",
            "purpose": "training", "node_id": "traces", "target_node": "compose", "target_port": "traces"})
        assert not bound_input["execution_started"]
        recipe = bound_input["recipe"]
        assert recipe["edges"] == [{"source_node": "traces", "source_port": "data", "target_node": "compose", "target_port": "traces"}]
        traces = service._dataset_registry.read_split(project["project_id"], registered_input["dataset_id"], 1, "train", purpose="training")
        traces = attach_declared_rights(traces, {"node_id": "traces", "source": recipe["source"],
            "rights_extensions": recipe["extensions"], "dependencies": recipe["resources"] + recipe["external_dependencies"]})
        next(node for node in recipe["nodes"] if node["id"] == "compose")["parameters"]["traces_digest"] = identity(traces)
        dump(tmp_path / "reviewed-input-artifact.json", traces)
        recipe = call("recipe.update", {"recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"], "recipe": recipe})
        before = sorted(str(path) for path in source.rglob("*"))
        request = {"recipe_id": recipe["recipe_id"], "trusted": True}
        plan = call("run.plan", request)
        dump(tmp_path / "plan.json", plan)
        assert plan["executable"], plan
        assert sorted(str(path) for path in source.rglob("*")) == before
        assert not (backend.root / "jobs").exists()
        submitted = call("run.submit", {**request, "expected_revision": recipe["revision"]})
        job = backend.root / "jobs" / submitted["operation_id"]
        operation = {"operation_id": submitted["operation_id"]}
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status = call("run.status", operation)
            if status["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(.25)
        dump(tmp_path / "status.json", status)
        assert status["status"] == "succeeded", (status, (job / "worker.log").read_text()[-14000:])
        assert status["backend_run_id"]
        prepared = job / "outputs/compose/prepared"
        manifest = json.loads((prepared / "dataset.manifest.json").read_text())
        actual_rows = [json.loads(line) for line in (prepared / manifest["sources"][0]["path"]).read_text().splitlines()]
        assert len(actual_rows) == 2
        for row in actual_rows:
            original = next(item for item in rows if identity(item) == row["source_row_sha256"])
            assert row["messages"] == original["messages"]
            assert row["target_message_index"] == 4 and row["message_loss_mask"] == [0, 0, 0, 0, 1]
            assert row["target_text"] == original["messages"][4]["content"]
            assert row["target_sha256"] == hashlib.sha256(row["target_text"].encode()).hexdigest()
            assert row["source_trajectory_sha256"] == original["source_trajectory_sha256"]
        rights = prepared_rights(prepared)
        assert {item["license_policy_id"] for item in rights["sources"]} == {item["policy_id"] for item in policies.values()}
        assert not rights["unresolved"]
        owners = [json.loads(path.read_text()) for path in (job / "processes").glob("*/owner.json")]
        assert len(owners) == 1
        assert all(row["reaped"] and row["direct_child_reaped"] and row["exit_code"] == 0
                   and not row["descendant_cleanup"]["remaining_pids"] for row in owners)
        journal = json.loads((job / "request.json").read_text())
        assert journal["source_binding"] == project["source_binding"]
        assert journal["runner_environments"]["compose"]["python"] == INSTALLED
        assert journal["runner_environments"]["compose"]["profile_sha256"] == profile_hash
        artifacts = call("run.artifacts", operation)["artifacts"]
        assert any(item.get("node_id") == "compose" for item in artifacts), artifacts
        # Operator maps the returned proposal directory; public operations still
        # require an explicit registration and per-source output policy review.
        service._dataset_registry = DatasetRegistry(service.store, project_roots={project["project_id"]: {"prepared": str(prepared)}})
        assert call("dataset.validate", {"manifest": manifest})["valid"]
        registered = call("dataset.register", {"manifest": manifest})
        assert registered["row_count"] == 2 and registered["manifest"]["rights"] == rights
        assert call("dataset.validate", {"dataset_id": registered["dataset_id"], "revision": 1})["valid"]
        (prepared / "LICENSE.txt").write_text(TERMS)
        service.hub.publication_roots[project["project_id"]]["prepared"] = str(prepared)
        _, output_policy = review("prepared", "dataset", [manifest["sources"][0]["path"]])
        output_binding = call("hub.dataset.policy.bind", {"dataset_id": registered["dataset_id"], "dataset_revision": 1,
            "source_policies": {"composed": output_policy["policy_id"]}, "intended_use": "train", "usage_context": USAGE})
        bound = call("dataset.bind", {"recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"],
            "dataset_id": registered["dataset_id"], "dataset_revision": 1, "split_id": "train",
            "purpose": "training", "node_id": "reviewed_composed"})
        assert not bound["execution_started"]
        assert len(service.store.list("operation", project["project_id"])) == 1
        assert {item["license_policy_id"] for item in output_binding["rights"]["sources"]} == {
            *[item["policy_id"] for item in policies.values()], output_policy["policy_id"]}
        receipt = {"status": "passed", "scope": "service-to-ZenML source-bound installed offline composition and explicit dataset registration/binding",
            "backend_run_id": status["backend_run_id"], "operation_id": submitted["operation_id"], "installation": installation,
            "runner_profile_sha256": profile_hash,
            "adapter_source_sha256": hashlib.sha256((ROOT / "src/modelbuilder/backend/components/moonshiner.py").read_bytes()).hexdigest(),
            "source": source_contract, "source_binding": project["source_binding"],
            "row_count": len(actual_rows), "prepared_manifest_sha256": hashlib.sha256((prepared / "dataset.manifest.json").read_bytes()).hexdigest(),
            "rights": rights, "owners": owners, "dataset_id": registered["dataset_id"], "dataset_version_id": registered["version_id"],
            "output_binding_rights": output_binding["rights"], "registration_started_automatically": False,
            "training_started": False, "provider_execution": False, "gui_acceptance": False,
            "input_boundary": "ordinary dataset.register, reviewed dataset policy and dataset.bind target compose.traces",
            "input_dataset_id": registered_input["dataset_id"], "input_version_id": registered_input["version_id"]}
        dump(tmp_path / "native-workflow-acceptance.json", receipt)
    finally:
        if submitted:
            status = call("run.status", {"operation_id": submitted["operation_id"]})
            if status["status"] not in {"succeeded", "failed", "cancelled"}:
                call("run.cancel", {"operation_id": submitted["operation_id"]})
        service.hub.close()
        service.store.close()
