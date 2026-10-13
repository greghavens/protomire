"""Offline process contracts only: no installed upstream or provider acceptance."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import psutil
import pytest

from modelbuilder.backend.components import identity, validate_component
from modelbuilder.backend.components import moonshiner as adapter
from modelbuilder.backend.components import moonshiner_process as process
from modelbuilder.dataset import DatasetRegistry
from modelbuilder.generated_artifacts import prepared_rights, validate_lineage
from modelbuilder.store import SQLiteStore
from rights_test_support import local_review

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Moonshiner v1 foreground lifecycle is Linux-only")

INSTALLATION = {"version": adapter.UPSTREAM_VERSION, "reviewed_upstream_commit": adapter.UPSTREAM_COMMIT,
                "source_sha256": "1" * 64, "environment_sha256": "2" * 64}


def example(index=0):
    return {"id": "trace-" + str(index), "task": "fixture-" + str(index), "split": "train", "teacher_model": "owned-fixture-teacher",
        "teacher_runtime": "fixture", "source_trajectory_id": "owned-trajectory-" + str(index),
        "source_trajectory_sha256": hashlib.sha256(str(index).encode()).hexdigest(),
        "messages": [{"role": "system", "content": "Answer the prompt"},
            {"role": "user", "content": "Question " + str(index)},
            {"role": "assistant", "content": "Previous assistant context", "reasoning_content": "not a label"},
            {"role": "tool", "content": "Observed result", "tool_call_id": "call-1"},
            {"role": "assistant", "content": "Final answer " + str(index), "reasoning_content": "also not a label"}],
        "target_message_index": 4}


def registered_traces(root, rows, rights):
    """Use the real registry split shape, not a synthetic top-level schema marker."""
    root.mkdir()
    source = root / "traces.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))
    manifest = {"name": "Owned traces", "benchmark": "source-traces", "license": "test-only",
        "provenance": {"source": "Owned fixture traces"}, "id_column": "id",
        "sources": [{"id": "traces", "root_id": "data", "path": source.name, "format": "jsonl",
                     "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}],
        "row_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}, "splits": [{"id": "train", "role": "train", "source_ids": ["traces"]}],
        "transformations": []}
    store = SQLiteStore(root / "input-registry.sqlite")
    try:
        store.put("project", "p", "p", {"project_id": "p", "revision": 1})
        registry = DatasetRegistry(store, project_roots={"p": {"data": str(root)}})
        version = registry.register("p", manifest)
        result = registry.read_split("p", version["dataset_id"], 1, "train", purpose="training")
        result["rights"] = deepcopy(rights)
        return result
    finally:
        store.close()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    _, data_rights = local_review(policy_id="owned-traces", repo_type="dataset")
    _, teacher_rights = local_review(policy_id="owned-teacher")
    traces = registered_traces(tmp_path / "input", [example(0), example(1)], data_rights)
    parameters = {"mode": "offline-compose", "installation": INSTALLATION, "traces_digest": identity(traces),
        "teacher": {"model": "owned-fixture-teacher", "runtime": "fixture", "rights_parent_id": "teacher-model"},
        "limits": {"max_records": 4, "max_bytes": 1024 * 1024, "timeout_seconds": 5, "max_cost_usd": 0},
        "seed": 17, "name": "Composed fixture", "benchmark": "composed-traces", "license": "test-only",
        "root_id": "prepared", "review_note": "Explicit owned fixture review", "split_policy": "train-only",
        "label_policy": "final-assistant-content-only"}
    context = {"output_dir": str(tmp_path / "output"), "cancel_file": str(tmp_path / "cancel.json"),
        "identities": {"source": "owned-fixture", "environment": "test-only"},
        "declared_rights_parents": [{"artifact_id": "teacher-model", "content_digest": "a" * 64,
                                    "rights": teacher_rights}]}
    mode = ["normal"]
    original_command = process.command
    fixture = Path(__file__).with_name("moonshiner_fixture_cli.py")
    commands = []
    def command(source, output, seed):
        real = original_command(source, output, seed)
        commands.append(real)
        return [sys.executable, "-I", str(fixture), mode[0], *real[4:]]
    monkeypatch.setattr(process, "installation_identity", lambda: deepcopy(INSTALLATION))
    monkeypatch.setattr(process, "command", command)
    return SimpleNamespace(inputs={"traces": traces}, parameters=parameters, context=context,
                           mode=mode, commands=commands, root=tmp_path)


def run(setup):
    return adapter.prepare_dataset(inputs=setup.inputs, parameters=setup.parameters, context=setup.context)["prepared"]


def test_finite_completion_final_content_labels_and_determinism(setup):
    first = run(setup)
    manifest = json.loads((first / "dataset.manifest.json").read_text())
    rows = [json.loads(line) for line in (first / manifest["sources"][0]["path"]).read_text().splitlines()]
    assert len(rows) == 2
    for row in rows:
        assert row["message_loss_mask"] == [0, 0, 0, 0, 1]
        assert row["target_message_index"] == 4
        assert row["target_text"].startswith("Final answer")
        assert "reasoning" not in row["target_text"] and "context" not in row["target_text"]
        original = next(item for item in setup.inputs["traces"]["rows"] if identity(item) == row["source_row_sha256"])
        assert row["messages"] == original["messages"]
        assert row["source_trajectory_sha256"] == original["source_trajectory_sha256"]
    assert manifest["splits"] == [{"id": "train", "role": "train", "source_ids": ["composed"]}]
    provenance = json.loads(manifest["provenance"]["description"])
    assert provenance["installation"] == INSTALLATION
    assert provenance["provider_distillation"] == "unavailable"
    assert provenance["limits"]["max_cost_usd"] == 0
    assert not provenance["registration_started"] and not provenance["training_started"]
    assert setup.commands[0][4:7] == ["dataset", "compose", "--source"]
    assert not set(setup.commands[0]) & {"--tokenizer", "--detach", "--resume", "run", "publish", "setup"}
    setup.context["output_dir"] = str(setup.root / "second")
    second = run(setup)
    assert (first / "dataset.manifest.json").read_bytes() == (second / "dataset.manifest.json").read_bytes()


def test_prepared_registration_retains_teacher_and_data_rights(setup):
    prepared = run(setup)
    manifest = json.loads((prepared / "dataset.manifest.json").read_text())
    inherited = validate_lineage(manifest)
    assert prepared_rights(prepared) == inherited
    assert {row["license_policy_id"] for row in inherited["sources"]} == {"owned-traces", "owned-teacher"}
    store = SQLiteStore(setup.root / "registry.sqlite")
    try:
        store.put("project", "p", "p", {"project_id": "p", "revision": 1})
        registry = DatasetRegistry(store, project_roots={"p": {"prepared": str(prepared)}})
        record = registry.register("p", manifest)
        assert record["row_count"] == 2
        assert record["manifest"]["rights"] == inherited
        assert registry.validate("p", dataset_id=record["dataset_id"], revision=1)["valid"]
        from modelbuilder.hub import HubTransfers
        _, output_rights = local_review(policy_id="output-label")
        result = HubTransfers._inherited_dataset_rights({"manifest": manifest, "version_id": identity(manifest),
            "content_digest": "a" * 64}, output_rights)
        assert {row["license_policy_id"] for row in result["sources"]} == {"owned-traces", "owned-teacher", "output-label"}
    finally:
        store.close()


@pytest.mark.parametrize("mode", ["malformed", "nonfinite", "duplicate-key", "bad-manifest", "mutated",
    "bad-index", "duplicate", "empty", "symlink", "failure", "oversized", "remote-source", "log-flood"])
def test_invalid_output_never_publishes_prepared_dataset(setup, mode):
    setup.mode[0] = mode
    with pytest.raises((ValueError, OSError)):
        run(setup)
    assert not (Path(setup.context["output_dir"]) / "prepared").exists()


@pytest.mark.parametrize("case", ["heldout", "row-heldout", "target-missing", "target-history", "target-bool",
    "target-tool", "teacher", "missing-parent", "too-many", "same-benchmark", "changed-review"])
def test_source_review_and_split_target_gates(setup, case):
    traces, parameters = setup.inputs["traces"], setup.parameters
    if case == "heldout":
        traces["split"]["role"] = "test"
    elif case == "row-heldout":
        traces["rows"][0]["split"] = "validation"
    elif case == "target-missing":
        del traces["rows"][0]["target_message_index"]
    elif case == "target-history":
        traces["rows"][0]["target_message_index"] = 2
    elif case == "target-bool":
        traces["rows"][0]["target_message_index"] = True
    elif case == "target-tool":
        traces["rows"][0]["messages"][-1]["tool_calls"] = [{"id": "unexpected-call"}]
    elif case == "teacher":
        traces["rows"][0]["teacher_model"] = "different-model"
    elif case == "missing-parent":
        setup.context["declared_rights_parents"] = []
    elif case == "too-many":
        parameters["limits"]["max_records"] = 1
    elif case == "same-benchmark":
        parameters["benchmark"] = "source-traces"
    else:
        parameters["traces_digest"] = "0" * 64
    if case in {"row-heldout", "target-missing", "target-history", "target-bool", "target-tool", "teacher"}:
        traces = registered_traces(setup.root / "changed-input", traces["rows"], traces["rights"])
        setup.inputs["traces"] = traces
    if case != "changed-review":
        parameters["traces_digest"] = identity(traces)
    with pytest.raises(ValueError):
        run(setup)
    assert setup.commands == []


@pytest.mark.parametrize("field", ["version", "source_sha256", "environment_sha256", "reviewed_upstream_commit"])
def test_changed_installation_is_refused_before_process(setup, field):
    setup.parameters["installation"] = {**INSTALLATION, field: "0" * 64}
    with pytest.raises(Exception):
        run(setup)
    assert setup.commands == []


@pytest.mark.parametrize("cause", ["timeout", "cancel"])
def test_timeout_cancellation_stop_descendants(setup, cause):
    setup.mode[0] = "spawn"
    if cause == "timeout":
        setup.parameters["limits"]["timeout_seconds"] = .3
    timer = None
    if cause == "cancel":
        timer = threading.Timer(.3, lambda: Path(setup.context["cancel_file"]).touch())
        timer.start()
    try:
        with pytest.raises((TimeoutError, RuntimeError), match="deadline|cancelled"):
            run(setup)
    finally:
        if timer:
            timer.join()
    pid = int((Path(setup.context["output_dir"]) / "moonshiner-work/descendant.pid").read_text())
    # Linux can briefly expose either terminal state while the parent reaps.
    try:
        status = psutil.Process(pid).status()
    except psutil.NoSuchProcess:
        status = None
    assert status in {None, psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
    assert not (Path(setup.context["output_dir"]) / "prepared").exists()


def test_catalog_is_inert_and_provider_flags_unavailable(setup, monkeypatch):
    monkeypatch.setattr(process, "installation_identity", lambda: (_ for _ in ()).throw(AssertionError("inspection")))
    entry = adapter.catalog_entries()[0]
    descriptor = {key: entry[key] for key in ("id", "version", "protocol", "role", "description", "parameter_schema", "capabilities")}
    node = {"component": descriptor, "parameters": setup.parameters, "implementation_ref": {},
            "inputs": entry["inputs"], "outputs": entry["outputs"]}
    assert validate_component(node)["role"] == "dataset"
    for changed in [{"mode": "provider-distill"}, {"argv": ["run", "--limit", "1"]}, {"publish": True}]:
        with pytest.raises(Exception):
            validate_component({**node, "parameters": {**setup.parameters, **changed}})


def test_child_environment_has_no_credentials_or_user_config(setup, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-a-secret")
    monkeypatch.setenv("HF_TOKEN", "test-only-not-a-secret")
    original = process._execute
    observed = {}
    def execute(argv, **kwargs):
        observed.update(kwargs["env"])
        return original(argv, **kwargs)
    monkeypatch.setattr(process, "_execute", execute)
    run(setup)
    assert "OPENAI_API_KEY" not in observed and "HF_TOKEN" not in observed and "PYTHONPATH" not in observed
    assert observed["HF_HUB_OFFLINE"] == "1"
    assert Path(observed["XDG_RUNTIME_DIR"]).is_relative_to(Path(observed["HOME"]))
    assert not Path(observed["HOME"]).is_relative_to(Path(observed["MOONSHINER_HOME"]).parent)
    config = json.loads((Path(observed["MOONSHINER_HOME"]) / "config.json").read_text())
    assert config["publish"]["hf_dataset"] is None
    assert config["pipeline"]["queues"] == {"seed_authoring": False, "tracing": False}
    assert not config["synthetic_corrections"]["enabled"]


@pytest.mark.parametrize("action", ["import socket; socket.socket()",
    "import socket; socket.getaddrinfo('localhost', 1)",
    "import socket; socket.gethostbyname('localhost')",
    "import subprocess; subprocess.run(['false'])"])
def test_offline_worker_denies_network_and_subprocess_without_upstream(action):
    worker = Path(process.__file__).with_name("moonshiner_worker.py")
    code = ("import importlib.util, sys; "
            f"s=importlib.util.spec_from_file_location('guard',{str(worker)!r}); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "sys.addaudithook(m.offline_guard); " + action)
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0 and "denies network and child commands" in result.stderr


def test_installation_receipt_hashes_actual_local_files(tmp_path, monkeypatch):
    root = tmp_path / "moonshiner_app"
    for name in ("cli.py", "bundle/moonshiner.py", "bundle/src/dataset_prep.py", "bundle/config.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("owned fixture, never executed")
    monkeypatch.setattr(process.importlib.metadata, "distribution", lambda _: SimpleNamespace(
        version=adapter.UPSTREAM_VERSION, locate_file=lambda name: tmp_path / name))
    from modelbuilder.backend import runner
    monkeypatch.setattr(runner, "environment_snapshot", lambda: {"packages": {"moonshiner": adapter.UPSTREAM_VERSION}})
    one = process.installation_identity()
    (root / "cli.py").write_text("modified owned fixture")
    assert process.installation_identity()["source_sha256"] != one["source_sha256"]
    (root / "leak.py").symlink_to(root / "cli.py")
    with pytest.raises(ValueError, match="symlink"):
        process.installation_identity()


def test_plain_text_trainer_refuses_structured_targets_before_tokenization(setup, monkeypatch):
    from modelbuilder.backend.components import transformers_training
    monkeypatch.setattr(transformers_training.loader, "encode", lambda *a: (_ for _ in ()).throw(AssertionError("tokenized")))
    with pytest.raises(ValueError, match="compatible conversation formatter"):
        transformers_training._features([{"label_policy": "final-assistant-content-only", "target_text": "answer"}],
                                        None, {"task": "causal-lm", "text_field": "target_text"})


def test_native_stage_process_reaps_fixture_descendants_and_hands_off_artifact(setup):
    """Actual existing native process ABI, still using only our owned CLI double."""
    from modelbuilder.backend.process_execution import execute
    source = setup.root / "source"
    source.mkdir()
    implementation = source / "owned_boundary.py"
    implementation.write_text('''import sys
from pathlib import Path
from modelbuilder.backend.components import moonshiner as adapter
from modelbuilder.backend.components import moonshiner_process as process

def run(request):
    process.installation_identity = lambda: request["parameters"]["installation"]
    original = process.command
    fixture = Path(__file__).with_name("fixture.py")
    process.command = lambda source, output, seed: [sys.executable, "-I", str(fixture), "spawn-return", *original(source, output, seed)[4:]]
    return adapter.prepare_dataset(**request)
''')
    (source / "fixture.py").write_bytes(Path(__file__).with_name("moonshiner_fixture_cli.py").read_bytes())
    files = [{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in source.iterdir()]
    ref = next(item for item in files if item["path"] == implementation.name)
    spec = {"job_dir": str(setup.root / "native-job"), "node_id": "compose", "parameters": {},
        "component_node": {"implementation_ref": {**ref, "module": "owned_moonshiner_boundary", "entrypoint": "run"}},
        "source_root": str(source), "trusted_code_roots": [str(source)], "source": {"files": files}}
    prepared = execute("python", {"request": {"inputs": setup.inputs, "parameters": setup.parameters,
        "context": setup.context}}, spec)["prepared"]
    assert prepared.is_dir() and prepared_rights(prepared)
    pid = int((Path(setup.context["output_dir"]) / "moonshiner-work/descendant.pid").read_text())
    assert not psutil.pid_exists(pid)
    receipt = json.loads(next((setup.root / "native-job/processes").glob("*/owner.json")).read_text())
    assert receipt["reaped"] and receipt["direct_child_reaped"] and receipt["exit_code"] == 0
    assert receipt["descendant_cleanup"]["remaining_pids"] == []


def test_already_cancelled_never_starts_cli(setup, monkeypatch):
    Path(setup.context["cancel_file"]).touch()
    monkeypatch.setattr(process.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    with pytest.raises(RuntimeError, match="cancelled before process startup"):
        run(setup)


def test_deduplication_keeps_the_exact_first_reviewed_target(setup):
    duplicate = deepcopy(setup.inputs["traces"]["rows"][0])
    duplicate["source_trajectory_id"] = "another-reviewed-trajectory-same-conversation"
    duplicate["id"] = "distinct-row-same-conversation"
    original = setup.inputs["traces"]
    setup.inputs["traces"] = registered_traces(setup.root / "duplicate-input", [*original["rows"], duplicate], original["rights"])
    setup.parameters["traces_digest"] = identity(setup.inputs["traces"])
    prepared = run(setup)
    manifest = json.loads((prepared / "dataset.manifest.json").read_text())
    rows = process.read_jsonl(prepared / manifest["sources"][0]["path"], setup.parameters["limits"])
    assert len(rows) == 2
    assert identity(duplicate) not in {row["source_row_sha256"] for row in rows}


def test_unresolved_parent_rights_are_never_replaced_by_output_license(setup):
    setup.context["declared_rights_parents"][0]["rights"]["unresolved"] = [
        {"artifact_id": "teacher-restriction", "reason": "No redistribution review"}]
    setup.parameters["license"] = "CC0-1.0"
    prepared = run(setup)
    inherited = prepared_rights(prepared)
    assert inherited["unresolved"]
    assert {row["license_policy_id"] for row in inherited["sources"]} == {"owned-traces", "owned-teacher"}


@pytest.mark.parametrize("task", ["causal-lm", "sequence-classification"])
def test_ordinary_plain_text_trainer_labels_are_unchanged(task):
    from modelbuilder.backend.components.transformers_training import _features
    tokenizer = SimpleNamespace(encode=lambda *args, **kwargs: SimpleNamespace(ids=[1, 2]))
    settings = {"task": task, "text_field": "text", "label_field": "label", "labels": ["yes", "no"],
                "max_length": 4, "truncate": False, "pad_token_id": 0}
    result = _features([{"text": "ordinary text", "label": "yes"}], tokenizer, settings)
    assert result[0]["labels"] == ([1, 2, -100, -100] if task == "causal-lm" else 0)


def test_offline_worker_allows_only_local_hostname_inspection():
    worker = Path(process.__file__).with_name("moonshiner_worker.py")
    code = ("import importlib.util, sys, socket; "
            f"s=importlib.util.spec_from_file_location('guard',{str(worker)!r}); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "sys.addaudithook(m.offline_guard); assert isinstance(socket.gethostname(), str)")
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not os.environ.get("MODELBUILDER_MOONSHINER_TEST_PYTHON"),
                    reason="Explicit reviewed installed Moonshiner interpreter required")
def test_official_installed_compose_through_native_process_abi(setup):
    """Opt-in genuine upstream execution, only local test-owned records."""
    request = setup.root / "request.json"
    request.write_text(json.dumps({"inputs": setup.inputs, "parameters": setup.parameters, "context": setup.context}))
    code = r'''
import sys, json, hashlib
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from modelbuilder.backend.components import identity
from modelbuilder.backend.components.moonshiner_process import installation_identity
from modelbuilder.backend.process_execution import execute
from modelbuilder.generated_artifacts import prepared_rights
request = json.loads(Path(sys.argv[2]).read_text())
request["parameters"]["installation"] = installation_identity()
request["parameters"]["limits"]["timeout_seconds"] = 60
source = Path(sys.argv[2]).parent / "owned-source"
source.mkdir()
implementation = source / "adapter.py"
implementation.write_text("from modelbuilder.backend.components.moonshiner import prepare_dataset\ndef run(request):\n    return prepare_dataset(**request)\n")
ref = {"path": "adapter.py", "sha256": hashlib.sha256(implementation.read_bytes()).hexdigest()}
spec = {"job_dir": str(source.parent / "native-job"), "node_id": "compose", "parameters": {},
    "component_node": {"implementation_ref": {**ref, "module": "owned_installed_compose", "entrypoint": "run"}},
    "source_root": str(source), "trusted_code_roots": [str(source)], "source": {"files": [ref]}}
prepared = execute("python", {"request": request}, spec)["prepared"]
manifest = json.loads((prepared / "dataset.manifest.json").read_text())
rows = [json.loads(line) for line in (prepared / manifest["sources"][0]["path"]).read_text().splitlines()]
assert len(rows) == 2
assert all(row["message_loss_mask"] == [0,0,0,0,1] and row["target_message_index"] == 4 for row in rows)
assert {row["license_policy_id"] for row in prepared_rights(prepared)["sources"]} == {"owned-traces", "owned-teacher"}
owner = json.loads(next((source.parent / "native-job/processes").glob("*/owner.json")).read_text())
assert owner["reaped"] and owner["direct_child_reaped"] and owner["exit_code"] == 0
assert owner["descendant_cleanup"]["remaining_pids"] == []
print(json.dumps({"installation": request["parameters"]["installation"], "owner": owner, "rows": len(rows),
                  "prepared": str(prepared), "provider_execution": False}))
'''
    result = subprocess.run([os.environ["MODELBUILDER_MOONSHINER_TEST_PYTHON"], "-I", "-B", "-c", code,
        str(Path(adapter.__file__).resolve().parents[3]), str(request)], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["installation"]["version"] == adapter.UPSTREAM_VERSION
    (setup.root / "installed-acceptance.json").write_text(json.dumps(receipt, indent=2))


def test_registry_split_has_no_top_level_schema_and_is_accepted(setup):
    assert "schema" not in setup.inputs["traces"]
    assert setup.inputs["traces"]["consumption"]["schema"] == "modelbuilder.dataset-consumption/v1"
    assert adapter.catalog_entries()[0]["inputs"] == [{"name": "traces", "type": "dict"}]
    assert run(setup).is_dir()


@pytest.mark.parametrize("case", ["missing-consumption", "schema", "purpose", "project", "dataset", "split",
    "revision", "content-digest", "version", "count", "bool-count", "unique-count", "ids", "duplicate-ids",
    "row-order", "row-content", "row-digest", "seal", "unregistered-schema-marker"])
def test_registered_input_lineage_is_required_even_after_digest_review(setup, case):
    traces = setup.inputs["traces"]
    lineage = traces["consumption"]
    if case == "missing-consumption":
        del traces["consumption"]
    elif case == "schema":
        lineage["schema"] = "unsupported"
    elif case == "purpose":
        lineage["purpose"] = "evaluation"
    elif case == "project":
        lineage["project_id"] = ""
    elif case == "dataset":
        lineage["dataset"] = {**lineage["dataset"], "id": "other"}
    elif case == "split":
        lineage["split"] = {**lineage["split"], "id": "other"}
    elif case == "revision":
        traces["dataset"]["revision"] = "0"
    elif case == "content-digest":
        traces["dataset"]["content_digest"] = "invalid"
    elif case == "version":
        lineage["version_id"] = "invalid"
    elif case in {"count", "bool-count", "unique-count"}:
        lineage["unique_count" if case == "unique-count" else "count"] = True if case == "bool-count" else 1
    elif case == "ids":
        lineage["sample_ids"] = list(reversed(lineage["sample_ids"]))
    elif case == "duplicate-ids":
        traces["split"]["sample_ids"] = ["trace-0", "trace-0"]
    elif case == "row-order":
        traces["rows"].reverse()
    elif case == "row-content":
        traces["rows"][0]["messages"][-1]["content"] = "Substituted target"
    elif case == "row-digest":
        lineage["sample_digests"][0] = "0" * 64
    elif case == "unregistered-schema-marker":
        traces["schema"] = "modelbuilder.dataset/v1"
        del traces["consumption"]
    if case == "seal":
        lineage["digest"] = "0" * 64
    else:
        lineage["digest"] = identity({key: value for key, value in lineage.items() if key != "digest"})
    setup.parameters["traces_digest"] = identity(traces)
    with pytest.raises(ValueError):
        run(setup)
    assert setup.commands == []
