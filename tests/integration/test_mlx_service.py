"""Required-hardware MLX acceptance through the shared project operation service."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest
from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.components import identity, read_checkpoint, read_export
from modelbuilder.backend.components import mlx
from modelbuilder.backend.dependency_files import dependency_sha256
from modelbuilder.backend.common import read_json
from modelbuilder.models import Actor
from modelbuilder.service import OperationService

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / ".venv-backend/bin/python"
OWNER = Actor("mlx-native-owner", {"read", "modify", "execute"})


def file_ref(path, root):
    return {"path": str(path.relative_to(root)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def deadline_worker(python, destination, marker):
    """Operator-owned fixture: pause only after real export logits were computed."""
    program = """import os,runpy,sys
from pathlib import Path
REAL_PYTHON = {python!r}
MARKER = {marker!r}
if Path(sys.argv[-1]).name != 'mlx_worker.py':
    os.execv(REAL_PYTHON,[REAL_PYTHON,*sys.argv[1:]])
namespace=runpy.run_path(sys.argv[-1])
components=namespace['components']
original_loader=components.export_loader_source
def paused_loader():
    code=original_loader()
    needle="    (root/'parity-actual.json').write_text(result)"
    assert needle in code
    pause="    import os,time\\n    Path("+repr(MARKER)+").write_text(json.dumps({{'pid':os.getpid(),'parent_pid':os.getppid(),'native_logits_completed':True}}))\\n    time.sleep(600)\\n"
    return code.replace(needle,pause+needle)
components.export_loader_source=paused_loader
# Keep the export helper alive for the real native node deadline to supervise.
# Otherwise its ordinary 60-second inner guard could win this deliberate race.
import subprocess
original_run=subprocess.run
def run(command,*args,**kwargs):
    if any(Path(str(arg)).name=='loader.py' for arg in command):
        kwargs['timeout']=600
    return original_run(command,*args,**kwargs)
subprocess.run=run
try:
    raise SystemExit(namespace['main']())
except Exception as error:
    namespace['emit']({{'type':'error','message':str(error)}})
    raise SystemExit(1)
""".format(python=str(python), marker=str(marker))
    destination.write_text("#!" + str(python) + " -I\n" + program)
    destination.chmod(0o755)
    return destination


def deadline_acceptance(call, backend, python, tmp_path, pid, rid):
    """Exercise real MLX plus its export descendant under G11's native deadline."""
    import psutil

    marker = tmp_path / "export-deadline-descendant.json"
    wrapper = deadline_worker(python, tmp_path / "mlx-deadline-python", marker)
    backend.mlx_python = str(wrapper)
    recipe = call("recipe.get", project_id=pid, recipe_id=rid)
    for node in recipe["nodes"]:
        if node["id"] == "train":
            node["parameters"]["updates"] = 1
        if node["id"] == "export":
            node["timeout"] = 90
    recipe["evaluation"] = {"live": {"enabled": False}}
    recipe["environment"]["device"] = "mlx-metal"
    recipe = call(
        "recipe.update",
        project_id=pid,
        recipe_id=rid,
        expected_revision=recipe["revision"],
        recipe=recipe,
        idempotency_key="deadline-revision",
    )
    launched = call(
        "run.submit",
        project_id=pid,
        recipe_id=rid,
        expected_revision=recipe["revision"],
        target="local-mlx-metal",
        trusted=True,
        idempotency_key="deadline-run",
    )
    job = backend.root / "jobs" / launched["operation_id"]
    try:
        deadline = time.monotonic() + 150
        while not marker.exists() and time.monotonic() < deadline:
            result = call("run.status", project_id=pid, operation_id=launched["operation_id"])
            if result["status"] in {"failed", "cancelled"}:
                break
            time.sleep(0.05)
        assert marker.exists(), (
            "Native export did not reach the synchronized parity pause",
            result,
            (job / "worker.log").read_text()[-16000:],
        )
        observed = json.loads(marker.read_text())
        assert observed["native_logits_completed"]
        identities = {
            name: {"pid": observed[name], "created": psutil.Process(observed[name]).create_time()}
            for name in ("pid", "parent_pid")
        }
        active = [json.loads(path.read_text()) for path in (job / "active-nodes").glob("*.json")]
        assert any(item["node_id"] == "export" and item["timeout_seconds"] == 90 for item in active), active
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            result = call("run.status", project_id=pid, operation_id=launched["operation_id"])
            if (
                result["status"] in {"succeeded", "failed", "cancelled"}
                and not result["worker_alive"]
                and not result["supervisor_alive"]
            ):
                break
            time.sleep(0.25)
        cancellation = read_json(job / "cancel.json")
        state = read_json(job / "state.json")
        assert result["status"] == "cancelled" and cancellation["reason"] == "node_timeout_exceeded", result
        assert cancellation["node_id"] == "export" and cancellation["invocation_id"]
        assert (
            state["worker_reaped"] and state["process_group_terminated"] and state["termination"]["confirmed"]
        )
        for owner in identities.values():
            try:
                process = psutil.Process(owner["pid"])
                assert process.create_time() != owner["created"], (
                    "Owned export descendant survived deadline",
                    owner,
                )
            except psutil.NoSuchProcess:
                pass
        # A late explicit cancel must not overwrite the first writer's deadline reason.
        call(
            "run.cancel",
            project_id=pid,
            operation_id=launched["operation_id"],
            idempotency_key="late-deadline-cancel",
        )
        assert read_json(job / "cancel.json") == cancellation
        assert not list((job / "outputs/export").glob("**/export.manifest.json"))
        return {
            "run": result,
            "state": state,
            "cancellation": cancellation,
            "observed_native_descendants": identities,
            "native_logits_completed": True,
        }
    finally:
        backend.mlx_python = str(python)
        if not read_json(job / "state.json").get("process_group_terminated"):
            call(
                "run.cancel",
                project_id=pid,
                operation_id=launched["operation_id"],
                idempotency_key="deadline-fixture-cleanup",
            )
            cleanup_deadline = time.monotonic() + 120
            while time.monotonic() < cleanup_deadline:
                result = call("run.status", project_id=pid, operation_id=launched["operation_id"])
                if not result["worker_alive"] and not result["supervisor_alive"]:
                    break
                time.sleep(0.25)
            assert read_json(job / "state.json").get("process_group_terminated"), (
                "Native deadline fixture cleanup remains unconfirmed"
            )


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["full", "lora"])
def test_registered_project_mlx_native_pipeline(tmp_path, mode):
    if os.environ.get("MODELBUILDER_REQUIRE_MLX") != "1":
        pytest.skip("Set MODELBUILDER_REQUIRE_MLX=1 with the reviewed operator MLX interpreter")
    python = os.environ["MODELBUILDER_MLX_PYTHON"]
    inspected = mlx.probe(python, "mlx-metal", allocate=True)
    assert inspected["available"] and inspected["allocation_verified"], inspected
    source = tmp_path / "source"
    source.mkdir()
    (source / "adapter.py").write_text(
        "from modelbuilder.backend.components.mlx import train, evaluate, generate, export\n"
    )
    shutil.copy2(ROOT / "requirements/profiles/backend-darwin-arm64.lock", source / "backend.lock")
    shutil.copy2(ROOT / "requirements/profiles/mlx-darwin-arm64.lock", source / "mlx.lock")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Modelbuilder Test",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "Pinned user MLX adapter fixture",
        ],
        check=True,
    )
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    files = [file_ref(file, source) for file in sorted(source.iterdir()) if file.is_file()]
    import importlib.util

    fixture_spec = importlib.util.spec_from_file_location(
        "mlx_native_fixture", ROOT / "tests/components/mlx_fixture.py"
    )
    fixture = importlib.util.module_from_spec(fixture_spec)
    fixture_spec.loader.exec_module(fixture)
    create_model = fixture.create_model
    model = create_model(python, tmp_path / "assets")
    assets = [file_ref(file, model) for file in sorted(model.iterdir()) if file.is_file()]
    manifest_path = tmp_path / "model-assets.json"
    manifest_entries = [
        {**asset, "dependency_id": "asset-" + str(index)} for index, asset in enumerate(assets)
    ]
    manifest_path.write_text(json.dumps({"files": manifest_entries}))
    model_paths = {
        "model-assets": str(manifest_path),
        **{entry["dependency_id"]: str(model / entry["path"]) for entry in manifest_entries},
    }
    parameters = {
        "model_dependency": "model-assets",
        "assets": assets,
        "mode": mode,
        "updates": 2,
        "accumulation": 2,
        "batch_size": 1,
        "max_length": 32,
        "seed": 7,
        "learning_rate": 0.01,
        "num_layers": 1,
        "lora": {"rank": 2, "scale": 2.0, "dropout": 0.0},
        "limits": {"max_asset_bytes": 1000000, "max_parameters": 100000, "max_rows": 10},
    }
    parameter_sets = [
        parameters,
        {"max_length": 32, "max_rows": 10},
        {"prompt": [2, 4, 5], "max_tokens": 3},
        {"parity_tokens": [[2, 4, 5]], "atol": 1e-6},
    ]
    names = ["train", "evaluate", "generate", "export"]
    entries = mlx.catalog_entries()
    nodes = []
    for name, entry, params in zip(names, entries, parameter_sets):
        nodes.append(
            {
                "id": name,
                "kind": "component.run",
                "inputs": entry["inputs"],
                "outputs": entry["outputs"],
                "component": {
                    key: entry[key]
                    for key in (
                        "id",
                        "version",
                        "protocol",
                        "role",
                        "description",
                        "parameter_schema",
                        "capabilities",
                    )
                },
                "parameters": params,
                "implementation_ref": {
                    **next(file for file in files if file["path"] == "adapter.py"),
                    "module": "adapter",
                    "entrypoint": name,
                    "commit": commit,
                },
                "timeout": 120,
                "retry": {"max_attempts": 1, "backoff_seconds": 0},
                "resource_request": {"gpu": 0},
                "cache_policy": "disabled",
                "environment_ref": "default",
                "provenance": {"intended_use": "generate"} if name == "generate" else {},
            }
        )
    body = {
        "schema_version": "0",
        "name": "Native MLX authored model",
        "source": {
            "repository": "urn:sha256:" + hashlib.sha256((source / "adapter.py").read_bytes()).hexdigest(),
            "commit": commit,
            "files": files,
        },
        "nodes": nodes,
        "edges": [
            {"source_node": "train", "source_port": "trained", "target_node": name, "target_port": "trained"}
            for name in names[1:]
        ],
        "environment": {
            "python": "3.12.14",
            "requirements": ["zenml==0.97.0", "torch==2.9.0"],
            "lock_file": file_ref(source / "backend.lock", source),
            "device": "mlx-metal",
            "mlx_profile": file_ref(source / "mlx.lock", source),
        },
        "resources": [],
        "external_dependencies": [
            {
                "id": identifier,
                "kind": "model",
                "url": "urn:sha256:" + dependency_sha256(path),
                "license": "CC0-1.0",
                "sha256": dependency_sha256(path),
                "required": True,
            }
            for identifier, path in model_paths.items()
        ],
        "evaluation": {},
        "outputs": [],
        "permissions": {"network": False, "execute_on_import": False},
        "licenses": [],
    }
    from tests.components.rights_test_support import USAGE_CONTEXT, local_review
    from tests.integration.native_fixture_support import register_project_review
    inventory = [{"path": Path(path).name, "sha256": dependency_sha256(path), "size_bytes": Path(path).stat().st_size}
                 for path in model_paths.values()]
    model_review, model_rights = local_review("pending", commit=identity(inventory), policy_id="mlx-authored-model",
        source_files=inventory, repo_id="urn:modelbuilder:test-authored-mlx-model")
    body["extensions"] = {"usage_context": USAGE_CONTEXT}
    for dependency in body["external_dependencies"]:
        dependency["metadata"] = {"rights": model_rights}
    roots = {"authored": {"path": str(source), "trusted": True, "dependency_paths": model_paths}}
    backend = ZenMLBackend(tmp_path / "native", python_executable=str(BACKEND), mlx_python_executable=python)
    service = OperationService(tmp_path / "service.sqlite", backend, source_roots=roots)
    source_id = service.source_workspaces.inspect("authored")["repository_id"]
    project = service.invoke(
        "project.create",
        {
            "slug": "mlx-native",
            "model_name": "One MLX model",
            "recipe": body,
            "source_alias": "authored",
            "source_repository_id": source_id,
            "trusted": True,
            "idempotency_key": "project",
        },
        OWNER,
    )
    pid = project["project_id"]
    register_project_review(service, model_review, pid)
    rid = project["recipe_id"]
    data = tmp_path / "data"
    data.mkdir()
    rows = [{"id": f"train-{i}", "tokens": [2, 4 + i, 5, 6, 3], "prompt_offset": 0} for i in range(3)] + [
        {"id": f"eval-{i}", "tokens": [2, 10 + i, 12, 13, 3], "prompt_offset": 4 if i == 1 else 1}
        for i in range(2)
    ]
    path = data / "rows.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    data_review, _ = local_review(pid, policy_id="mlx-authored-dataset", repo_type="dataset",
        source_files=[{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size}],
        repo_id="urn:modelbuilder:test-authored-mlx-data")
    register_project_review(service, data_review, pid)
    manifest = {
        "name": "Independent token splits",
        "benchmark": "mlx-authored-v1",
        "license": "CC0-1.0",
        "provenance": {"source": "authored offline"},
        "sources": [
            {
                "id": "rows",
                "root_id": "data",
                "path": "rows.jsonl",
                "format": "jsonl",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        ],
        "id_column": "id",
        "row_schema": {
            "type": "object",
            "required": ["id", "tokens", "prompt_offset"],
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string"},
                "tokens": {"type": "array", "items": {"type": "integer"}},
                "prompt_offset": {"type": "integer"},
            },
        },
        "splits": [
            {"id": "train", "role": "train", "sample_ids": [f"train-{i}" for i in range(3)]},
            {"id": "eval", "role": "validation", "sample_ids": [f"eval-{i}" for i in range(2)]},
        ],
    }
    service.store.close()
    service = OperationService(
        tmp_path / "service.sqlite", backend, source_roots=roots, dataset_roots={pid: {"data": str(data)}}
    )

    def call(operation, **payload):
        return service.invoke(operation, payload, OWNER)

    try:
        call(
            "policy.update",
            project_id=pid,
            expected_revision=1,
            changes={"allowed_targets": ["local-cpu", "local-mlx-metal", "local-mlx-cpu"]},
            idempotency_key="local-mlx-policy",
        )
        version = call("dataset.register", project_id=pid, manifest=manifest, idempotency_key="dataset")
        call("hub.dataset.policy.bind", project_id=pid, dataset_id=version["dataset_id"], dataset_revision=1,
             source_policies={"rows": data_review["policy_id"]}, intended_use="train", usage_context=USAGE_CONTEXT,
             idempotency_key="dataset-rights")
        recipe = call("recipe.get", project_id=pid, recipe_id=rid)
        for split, name, target, port, purpose in [
            ("train", "training_data", "train", "train", "training"),
            ("eval", "evaluation_data", "evaluate", "data", "evaluation"),
        ]:
            bound = call(
                "dataset.bind",
                project_id=pid,
                recipe_id=rid,
                expected_revision=recipe["revision"],
                dataset_id=version["dataset_id"],
                dataset_revision=1,
                split_id=split,
                purpose=purpose,
                node_id=name,
                target_node=target,
                target_port=port,
                idempotency_key="bind-" + split,
            )
            recipe = bound["recipe"]
        recipe["edges"].append(
            {
                "source_node": "evaluation_data",
                "source_port": "data",
                "target_node": "train",
                "target_port": "validation",
            }
        )
        recipe["evaluation"] = {"live": {"enabled": True, "max_concurrency": 1, "timeout_seconds": 90}}
        recipe = call(
            "recipe.update",
            project_id=pid,
            recipe_id=rid,
            expected_revision=recipe["revision"],
            recipe=recipe,
            idempotency_key="live-revision",
        )
        payload = {
            "project_id": pid,
            "recipe_id": rid,
            "expected_revision": recipe["revision"],
            "target": "local-mlx-metal",
            "trusted": True,
        }
        plan = call("run.plan", **payload)
        assert plan["executable"], plan
        assert not (backend.root / "jobs").exists()
        launched = call("run.submit", **payload, idempotency_key="native-pipeline")
        job = backend.root / "jobs" / launched["operation_id"]
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            result = call("run.status", project_id=pid, operation_id=launched["operation_id"])
            if (
                result["status"] in {"succeeded", "failed", "cancelled"}
                and not result["worker_alive"]
                and not result["supervisor_alive"]
            ):
                break
            time.sleep(0.25)
        assert result["status"] == "succeeded", (result, (job / "worker.log").read_text()[-16000:])
        state = read_json(job / "state.json")
        assert state["worker_reaped"] and state["process_group_terminated"]
        assert result["backend_run_id"]
        exported = read_export(job / "outputs/export/model")
        trained = read_checkpoint(job / "outputs/train/checkpoint-000002")
        assert exported["selected_checkpoint"] == identity(trained)
        assert trained["execution"]["device"] == "mlx-metal" and trained["resume_modes"] == ["warm_start"]
        assert (
            trained["data"]["sample_count"]
            == parameters["updates"] * parameters["accumulation"] * parameters["batch_size"]
        )
        assert set(trained["data"]["sample_counts"]) <= {"train-0", "train-1", "train-2"}
        assert not set(trained["data"]["sample_counts"]) & {"eval-0", "eval-1"}
        assert trained["identities"]["environment"] and exported["verification"]["fresh_process"]
        from modelbuilder.backend.live_evaluation import list_evaluations

        live = list_evaluations(job)["evaluations"]
        assert live and all(e["status"] == "completed" for e in live), live
        for evaluation in live:
            evidence = evaluation["evaluation_records"]["cross_entropy"]
            assert evidence["provenance"]["execution"]["device"] == "mlx-cpu"
            assert evidence["provenance"]["execution"]["framework"] == "mlx"
            assert evidence["provenance"]["execution"]["profile_sha256"] == mlx.PROFILE_SHA256
            assert len(evidence["provenance"]["packages"]) == 34
            assert evidence["split"]["sample_ids"] == ["eval-0", "eval-1"]
            assert {item["item_id"]: item["token_count"] for item in evidence["predictions"]} == {
                "eval-0": 4,
                "eval-1": 1,
            }
            assert not set(evidence["split"]["sample_ids"]) & {row["id"] for row in rows[:3]}
            assert evidence["provenance"]["packages"]["mlx"] == "0.32.3"
        # New immutable training rows preserve the old source and held-out split.
        new_rows = rows + [{"id": "train-new", "tokens": [2, 14, 4, 5, 3], "prompt_offset": 0}]
        path2 = data / "rows-v2.jsonl"
        path2.write_text("".join(json.dumps(row) + "\n" for row in new_rows))
        manifest2 = json.loads(json.dumps(manifest))
        manifest2["sources"][0].update(path=path2.name, sha256=hashlib.sha256(path2.read_bytes()).hexdigest())
        manifest2["splits"][0]["sample_ids"].append("train-new")
        call(
            "dataset.version",
            project_id=pid,
            dataset_id=version["dataset_id"],
            expected_revision=1,
            manifest=manifest2,
            idempotency_key="new-train-data",
        )
        # A new immutable row source is outside the earlier review's byte
        # closure, even though this test authored both versions.
        from modelbuilder.models import ServiceError
        policy_binding = {"project_id": pid, "dataset_id": version["dataset_id"], "dataset_revision": 2,
                          "intended_use": "train", "usage_context": USAGE_CONTEXT}
        with pytest.raises(ServiceError, match="outside the selected reviewed policy"):
            call("hub.dataset.policy.bind", **policy_binding, source_policies={"rows": data_review["policy_id"]},
                 idempotency_key="reject-old-dataset-rights")
        files2 = [{"path": path2.name, "sha256": hashlib.sha256(path2.read_bytes()).hexdigest(),
                   "size_bytes": path2.stat().st_size}]
        data_review2, _ = local_review(pid, commit=identity(files2), policy_id="mlx-authored-dataset-v2", repo_type="dataset",
            source_files=files2, repo_id="urn:modelbuilder:test-authored-mlx-data")
        register_project_review(service, data_review2, pid)
        call("hub.dataset.policy.bind", **policy_binding, source_policies={"rows": data_review2["policy_id"]},
             idempotency_key="dataset-v2-rights")
        rebound = call(
            "dataset.bind",
            project_id=pid,
            recipe_id=rid,
            expected_revision=recipe["revision"],
            dataset_id=version["dataset_id"],
            dataset_revision=2,
            split_id="train",
            purpose="training",
            node_id="training_data",
            target_node="train",
            target_port="train",
            idempotency_key="warm-new-data",
        )
        recipe = rebound["recipe"]
        # Change the canonical recipe, never an execution-time override.
        revised = json.loads(json.dumps(recipe))
        next(node for node in revised["nodes"] if node["id"] == "train")["parameters"]["updates"] = 1
        revised["environment"]["device"] = "mlx-cpu"
        revised = call(
            "recipe.update",
            project_id=pid,
            recipe_id=rid,
            expected_revision=recipe["revision"],
            recipe=revised,
            idempotency_key="warm-budget-device",
        )
        resumed = call(
            "run.resume",
            project_id=pid,
            operation_id=launched["operation_id"],
            expected_recipe_revision=revised["revision"],
            mode="warm_start",
            trusted=True,
            idempotency_key="native-warm",
        )
        resumed_job = backend.root / "jobs" / resumed["operation_id"]
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            warm = call("run.status", project_id=pid, operation_id=resumed["operation_id"])
            if (
                warm["status"] in {"succeeded", "failed", "cancelled"}
                and not warm["worker_alive"]
                and not warm["supervisor_alive"]
            ):
                break
            time.sleep(0.25)
        assert warm["status"] == "succeeded", (warm, (resumed_job / "worker.log").read_text()[-16000:])
        warm_checkpoint = read_checkpoint(resumed_job / "outputs/train/checkpoint-000001")
        assert not warm_checkpoint["rights"]["unresolved"]
        assert {item["license_policy_id"] for item in warm_checkpoint["rights"]["sources"]} == {
            model_review["policy_id"], data_review["policy_id"], data_review2["policy_id"]}
        assert warm_checkpoint["identities"]["architecture"] == trained["identities"]["architecture"]
        assert warm_checkpoint["identities"]["training"] != trained["identities"]["training"]
        assert warm_checkpoint["identities"]["data"] != trained["identities"]["data"]
        assert warm_checkpoint["identities"]["environment"] != trained["identities"]["environment"]
        assert warm_checkpoint["execution"]["device"] == "mlx-cpu" and warm_checkpoint["step"] == 1
        current = call("recipe.get", project_id=pid, recipe_id=rid)
        from modelbuilder.models import ServiceError

        with pytest.raises(ServiceError, match="exact-state|unsupported|warm_start|Accelerator"):
            call(
                "run.resume",
                project_id=pid,
                operation_id=launched["operation_id"],
                expected_recipe_revision=current["revision"],
                mode="exact_state",
                trusted=True,
                idempotency_key="reject-exact",
            )
        cancel_recipe = json.loads(json.dumps(current))
        next(node for node in cancel_recipe["nodes"] if node["id"] == "train")["parameters"]["updates"] = 1000
        cancel_recipe["environment"]["device"] = "mlx-metal"
        cancel_recipe = call(
            "recipe.update",
            project_id=pid,
            recipe_id=rid,
            expected_revision=current["revision"],
            recipe=cancel_recipe,
            idempotency_key="cancel-budget",
        )
        cancelling = call(
            "run.submit",
            project_id=pid,
            recipe_id=rid,
            expected_revision=cancel_recipe["revision"],
            target="local-mlx-metal",
            trusted=True,
            idempotency_key="cancel-run",
        )
        cancel_job = backend.root / "jobs" / cancelling["operation_id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if list((cancel_job / "live_evaluation/checkpoints").glob("*/ready.json")):
                break
            time.sleep(0.02)
        else:
            raise AssertionError("No safe checkpoint published before cancellation")
        call(
            "run.cancel",
            project_id=pid,
            operation_id=cancelling["operation_id"],
            idempotency_key="cancel-request",
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            cancelled = call("run.status", project_id=pid, operation_id=cancelling["operation_id"])
            if (
                cancelled["status"] in {"succeeded", "failed", "cancelled"}
                and not cancelled["worker_alive"]
                and not cancelled["supervisor_alive"]
            ):
                break
            time.sleep(0.25)
        assert cancelled["status"] == "cancelled", cancelled
        cancelled_state = read_json(cancel_job / "state.json")
        assert (
            cancelled_state["worker_reaped"]
            and cancelled_state["process_group_terminated"]
            and cancelled_state["termination"]["confirmed"]
        )
        deadline_report = (
            deadline_acceptance(call, backend, python, tmp_path, pid, rid) if mode == "full" else None
        )
        report = {
            "project_id": pid,
            "recipe_id": rid,
            "revision": recipe["revision"],
            "operation_id": launched["operation_id"],
            "backend_run_id": result["backend_run_id"],
            "state": state,
            "run": result,
            "live_evaluations": live,
            "export": exported,
            "checkpoint": trained,
            "warm_start": {"run": warm, "checkpoint": warm_checkpoint},
            "cancelled": {"run": cancelled, "state": cancelled_state},
            "deadline": deadline_report,
            "allocation_probe": inspected,
            "train_ids": [r["id"] for r in rows[:3]],
            "evaluation_ids": [r["id"] for r in rows[3:]],
        }
        (tmp_path / "acceptance.json").write_text(json.dumps(report, indent=2) + "\n")
    finally:
        service.store.close()
