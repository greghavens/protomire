"""Independent real GPT-2/BERT inference using locally created safetensors only."""
from copy import deepcopy
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from modelbuilder.backend.components import component_catalog, identity, validate_component
from modelbuilder.backend.components.local_inference import (ROW_SCHEMA, catalog_entries, generate, judge, prepare_dataset, _seal)
from modelbuilder.dataset import DatasetRegistry
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService

OWNER = Actor("owner", {"read", "modify", "execute"})


def model_dependency(name):
    # The service-only suite can omit model libraries. CPU acceptance cannot.
    if os.environ.get("MODELBUILDER_REQUIRE_LOCAL_INFERENCE") == "1":
        return importlib.import_module(name)
    return pytest.importorskip(name)


def test_required_cpu_dependencies_cannot_silently_skip(monkeypatch):
    monkeypatch.setenv("MODELBUILDER_REQUIRE_LOCAL_INFERENCE", "1")
    def missing(name):
        raise ModuleNotFoundError(name)
    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(ModuleNotFoundError, match="torch"):
        model_dependency("torch")


def test_inert_catalog_and_strict_schemas():
    assert {entry["id"] for entry in component_catalog()} >= {
        "modelbuilder.examples", "modelbuilder.local.generate", "modelbuilder.local.judge", "modelbuilder.local.prepare-dataset"}
    for entry in catalog_entries():
        assert "implementation_ref" not in entry
        assert entry["capabilities"]["resume_modes"] == []
        assert entry["parameter_schema"]["additionalProperties"] is False
    result = subprocess.run([sys.executable, "-c", "from modelbuilder.backend.components import component_catalog; import sys; component_catalog(); assert 'torch' not in sys.modules; assert 'transformers' not in sys.modules"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def local(tmp_path):
    torch = model_dependency("torch")
    model_dependency("transformers")
    from transformers import GPT2Config, GPT2LMHeadModel, BertConfig, BertForSequenceClassification
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from safetensors.torch import save_model
    dependencies = {}
    assets = tmp_path / "assets"
    assets.mkdir()
    tokenizer = Tokenizer(WordLevel({word: i for i, word in enumerate(
        ["[UNK]", "rain", "sun", "warm", "cold", "weather", "report", "change", "snow", "forecast", "wind", "clear"])}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.save(str(assets / "tokenizer.json"))
    torch.manual_seed(137)
    generator = GPT2LMHeadModel(GPT2Config(vocab_size=12, n_positions=32, n_ctx=32, n_embd=8,
        n_layer=1, n_head=2, bos_token_id=None, eos_token_id=None, resid_pdrop=0, embd_pdrop=0, attn_pdrop=0))
    classifier = BertForSequenceClassification(BertConfig(vocab_size=12, hidden_size=8,
        num_hidden_layers=1, num_attention_heads=2, intermediate_size=12, max_position_embeddings=32,
        num_labels=2, hidden_dropout_prob=0, attention_probs_dropout_prob=0))
    models = {}
    for name, model, adapter in [("generator", generator, "gpt2-causal-lm"), ("judge", classifier, "bert-sequence-classifier")]:
        config, weights = assets / (name + ".json"), assets / (name + ".safetensors")
        model.config.to_json_file(config, use_diff=False)
        save_model(model, str(weights))
        refs = {}
        for key, path in [("config", config), ("weights", weights), ("tokenizer", assets / "tokenizer.json")]:
            identifier = name + "-" + key
            dependencies[identifier] = str(path)
            refs[key] = {"dependency_id": identifier, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        models[name] = {"adapter": adapter, **refs}
    source = tmp_path / "source"
    source.mkdir()
    (source / "adapter.py").write_text("from modelbuilder.backend.components.local_inference import generate, judge, prepare_dataset\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Independent", "-c", "user.email=independent@example.invalid", "commit", "-qm", "Local model wrapper"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    code_hash = hashlib.sha256((source / "adapter.py").read_bytes()).hexdigest()
    source_identity = {"commit": commit, "files": [{"path": "adapter.py", "sha256": code_hash}]}
    service = OperationService(tmp_path / "service.sqlite")
    project = service.invoke("project.create", {"slug": "generation", "model_name": "Generated examples", "idempotency_key": "p"}, OWNER)
    data_root = tmp_path / "data"
    data_root.mkdir()
    rows = [{"id": "train-a", "text": "rain forecast"}, {"id": "train-b", "text": "sun report"},
            {"id": "dev-a", "text": "heldout validation unavailable"}, {"id": "test-a", "text": "heldout test unavailable"}]
    data = data_root / "rows.jsonl"
    data.write_text("".join(json.dumps(row) + "\n" for row in rows))
    manifest = {"name": "Independent prompts", "benchmark": "independent-weather-v1", "license": "CC0-1.0",
        "provenance": {"source": "independently authored test"}, "sources": [{"id": "data", "root_id": "data",
            "path": data.name, "format": "jsonl", "sha256": hashlib.sha256(data.read_bytes()).hexdigest()}],
        "id_column": "id", "row_schema": {"type": "object", "required": ["id", "text"], "additionalProperties": False,
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}}},
        "splits": [{"id": key, "role": role, "sample_ids": ids} for key, role, ids in
                   [("train", "train", ["train-a", "train-b"]), ("dev", "validation", ["dev-a"]), ("test", "test", ["test-a"])]]}
    registry = DatasetRegistry(service.store, project_roots={project["project_id"]: {"data": str(data_root)}})
    record = registry.register(project["project_id"], manifest)
    splits = {key: registry.read_split(project["project_id"], record["dataset_id"], 1, split,
        purpose="training" if key == "prompts" else "evaluation") for key, split in [("prompts", "train"), ("validation", "dev"), ("test", "test")]}
    params = {"model": models["generator"], "seed": 42, "text_field": "text", "prompt_prefix": "change ",
              "max_new_tokens": 3, "samples_per_prompt": 2, "temperature": 0.8, "top_k": 12}
    judge_params = {"model": models["judge"], "labels": ["ordinary", "difficult"],
                    "score_label": "difficult", "rank_direction": "highest_first"}
    from modelbuilder.backend.components.runtime import runtime_environment
    context = {"dependencies": dependencies, "source": source_identity, "source_root": str(source),
               "identities": {"source": identity(source_identity)}, "environment": runtime_environment(),
               "output_dir": str(tmp_path / "prepared")}
    state = {"context": context, "params": params, "judge_params": judge_params, "inputs": splits,
           "source": source, "record": record, "registry": registry, "project": project, "service": service, "tmp": tmp_path}
    try:
        yield state
    finally:
        if state.get("http_process"):
            state["http_process"].terminate()
            state["http_process"].wait(timeout=15)
            state["http_log"].close()
        service.store.close()


def test_actual_generation_judging_seed_provenance_and_model_effect(local):
    import torch
    from safetensors.torch import save_model
    from transformers import BertConfig, BertForSequenceClassification
    context, params = local["context"], local["params"]
    rng = torch.get_rng_state().clone()
    first = generate(inputs=local["inputs"], parameters=params, context=context)["candidates"]
    repeated = generate(inputs=local["inputs"], parameters=params, context=context)["candidates"]
    assert torch.equal(rng, torch.get_rng_state())
    assert first == repeated and first["status"] == "candidates_only"
    assert first["row_schema"] == ROW_SCHEMA and len(first["rows"]) == 4
    assert all(len(row["generated_token_ids"]) == 3 for row in first["rows"])
    changed = generate(inputs=local["inputs"], parameters={**params, "seed": 43}, context=context)["candidates"]
    assert first["digest"] != changed["digest"]
    assert [row["generated_token_ids"] for row in first["rows"]] != [row["generated_token_ids"] for row in changed["rows"]]
    judgments = judge(inputs={"candidates": first}, parameters=local["judge_params"], context=context)["judgments"]
    assert judgments == judge(inputs={"candidates": first}, parameters=local["judge_params"], context=context)["judgments"]
    assert judgments["status"] == "model_predictions_not_ground_truth"
    assert all(row["prediction_kind"] == "model_prediction" for row in judgments["predictions"])
    assert set(judgments["ranked_ids"]) == {row["id"] for row in first["rows"]}
    assert first["provenance"]["generator"]["digest"] != judgments["provenance"]["judge"]["digest"]
    serialized = json.dumps(first) + json.dumps(judgments)
    assert str(local["tmp"]) not in serialized
    # An independent changed classifier changes predictions; no precomputed fixture scores.
    config = BertConfig.from_json_file(context["dependencies"]["judge-config"])
    model = BertForSequenceClassification(config)
    with torch.no_grad():
        model.classifier.weight.zero_()
        model.classifier.bias.copy_(torch.tensor([-8.0, 8.0]))
    path = local["tmp"] / "changed.safetensors"
    save_model(model, str(path))
    changed_params = deepcopy(local["judge_params"])
    changed_params["model"]["weights"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    changed_context = {**context, "dependencies": {**context["dependencies"], "judge-weights": str(path)}}
    changed_judgments = judge(inputs={"candidates": first}, parameters=changed_params, context=changed_context)["judgments"]
    assert changed_judgments["provenance"]["judge"]["digest"] != judgments["provenance"]["judge"]["digest"]
    assert all(row["score"] > .99 for row in changed_judgments["predictions"])
    assert changed_judgments["predictions"] != judgments["predictions"]


def _results(local):
    candidates = generate(inputs=local["inputs"], parameters=local["params"], context=local["context"])["candidates"]
    judgments = judge(inputs={"candidates": candidates}, parameters=local["judge_params"], context=local["context"])["judgments"]
    return {"candidates": candidates, "judgments": judgments}


def _review(results):
    return {"candidate_digest": results["candidates"]["digest"], "judgment_digest": results["judgments"]["digest"],
        "approved_ids": results["judgments"]["ranked_ids"][:2], "name": "Reviewed generated examples",
        "benchmark": "generated-weather-v1", "license": "CC0-1.0", "root_id": "generated",
        "review_note": "Explicit test review of these two model-predicted examples"}


def test_review_registration_immutable_versions_and_training_gate(local):
    results = _results(local)
    service, registry, pid = local["service"], local["registry"], local["project"]["project_id"]
    before = registry.list(pid)
    review = _review(results)
    prepared = prepare_dataset(inputs=results, parameters=review, context=local["context"])["prepared"]
    assert registry.list(pid) == before  # No automatic registration or training.
    manifest = json.loads((prepared / "dataset.manifest.json").read_text())
    from modelbuilder.generated_artifacts import prepared_rights
    assert prepared_rights(prepared) == manifest["rights"]
    assert manifest["splits"] == [{"id": "train", "role": "train", "source_ids": ["reviewed"]}]
    assert "label" not in manifest["row_schema"]["properties"]
    generated = DatasetRegistry(service.store, project_roots={pid: {"generated": str(prepared)}})
    one = generated.register(pid, manifest)
    assert one["row_count"] == 2
    assert generated.read_split(pid, one["dataset_id"], 1, "train", purpose="training")["rows"][0]["judge_predicted_label"]
    with pytest.raises(ServiceError, match="purpose"):
        generated.read_split(pid, one["dataset_id"], 1, "train", purpose="evaluation")
    next_manifest = deepcopy(manifest)
    next_manifest["description"] += " Explicit subsequent reviewed version."
    # This explicit declaration revision retains the same exact parent evidence.
    next_manifest["lineage"]["content_digest"] = identity({key: value for key, value in next_manifest.items()
        if key not in {"rights", "lineage", "digest"}})
    next_manifest["digest"] = identity({key: value for key, value in next_manifest.items() if key != "digest"})
    two = generated.version(pid, one["dataset_id"], 1, next_manifest)
    assert two["revision"] == 2 and two["parent_version_id"] == one["version_id"]
    assert generated.get(pid, one["dataset_id"], 1) == one
    assert generated.validate(pid, dataset_id=one["dataset_id"], revision=1)["valid"]
    rows_path = prepared / manifest["sources"][0]["path"]
    rows_path.write_text(rows_path.read_text() + " ")
    assert generated.validate(pid, dataset_id=one["dataset_id"], revision=1)["valid"] is False


@pytest.mark.parametrize("change,match", [
    (lambda p: p.update(device="cuda"), "Additional properties"),
    (lambda p: p["model"].update(adapter="auto-remote-model"), "not one of"),
    (lambda p: p["model"].update(trust_remote_code=True), "Additional properties"),
    (lambda p: p.update(max_new_tokens=65), "greater than"),
    (lambda p: p.update(samples_per_prompt=100), "greater than"),
    (lambda p: p.update(top_k=13), "vocabulary"),
    (lambda p: p["model"]["weights"].update(sha256="0" * 64), "pinned bytes"),
    (lambda p: p["model"]["weights"].update(dependency_id="remote-model"), "admitted dependency"),
    (lambda p: p.update(prompt_prefix="rain " * 100), "context"),
])
def test_unsupported_and_tampered_models_fail_closed(local, change, match):
    parameters = deepcopy(local["params"])
    change(parameters)
    with pytest.raises(Exception, match=match):
        generate(inputs=local["inputs"], parameters=parameters, context=local["context"])


@pytest.mark.parametrize("case", ["role", "benchmark", "row_tamper", "identity_overlap", "content_overlap"])
def test_protected_split_isolation(local, case):
    data = deepcopy(local["inputs"])
    if case == "role":
        data["prompts"] = data["test"]
    elif case == "benchmark":
        data["test"]["dataset"]["benchmark"] = "other"
    elif case == "row_tamper":
        data["prompts"]["rows"][0]["text"] = "tampered"
    else:
        target = data["test"]
        if case == "identity_overlap":
            target["split"]["sample_ids"][0] = "train-a"
            target["rows"][0]["id"] = "train-a"
        else:
            target["rows"][0]["text"] = "  RAIN   forecast "
        lineage = target["consumption"]
        lineage["split"] = deepcopy(target["split"])
        lineage["sample_ids"] = target["split"]["sample_ids"]
        lineage["sample_digests"] = [identity({"text": target["rows"][0]["text"]})]
        lineage["digest"] = identity({key: value for key, value in lineage.items() if key != "digest"})
    with pytest.raises(ValueError):
        generate(inputs=data, parameters=local["params"], context=local["context"])


@pytest.mark.parametrize("case", ["changed_candidate", "changed_judgment", "unreviewed", "same_benchmark", "schema", "truth"])
def test_review_refuses_stale_unknown_or_misrepresented_artifacts(local, case):
    results = _results(local)
    params = _review(results)
    if case == "changed_candidate":
        results["candidates"]["rows"][0]["text"] = "altered"
    elif case == "changed_judgment":
        results["judgments"]["predictions"][0]["score"] = .9
    elif case == "unreviewed":
        params["approved_ids"] = ["unknown"]
    elif case == "same_benchmark":
        params["benchmark"] = "independent-weather-v1"
    elif case == "schema":
        results["candidates"]["rows"][0]["ground_truth"] = True
        results["candidates"] = _seal({key: value for key, value in results["candidates"].items() if key != "digest"})
    else:
        results["judgments"]["predictions"][0]["prediction_kind"] = "ground_truth"
        results["judgments"] = _seal({key: value for key, value in results["judgments"].items() if key != "digest"})
        params["judgment_digest"] = results["judgments"]["digest"]
    with pytest.raises(Exception):
        prepare_dataset(inputs=results, parameters=params, context=local["context"])
    assert not Path(local["context"]["output_dir"]).exists()


def _local_reviews(local, *, source_alias="test-source"):
    from tests.components.rights_test_support import local_review, local_source_review
    pid = local["project"]["project_id"]
    # The fixture above creates random model weights and authored rows locally.
    # These policies review those exact bytes; no pretrained grant is fabricated.
    model_files = [{"path": key, "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                    "size_bytes": Path(path).stat().st_size}
                   for key, path in local["context"]["dependencies"].items()]
    dataset_files = []
    for item in local["record"]["manifest"]["sources"]:
        path = local["tmp"] / "data" / item["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
        dataset_files.append({"path": item["path"], "sha256": item["sha256"], "size_bytes": path.stat().st_size})
    model_policy, model_rights = local_review(pid, policy_id="test-random-models", source_files=model_files)
    dataset_policy, dataset_rights = local_review(pid, policy_id="test-authored-data", repo_type="dataset", source_files=dataset_files)
    source_policy, source_rights = local_source_review(local["source"], local["context"]["source"], pid, source_alias=source_alias)
    policies = {policy["policy_id"]: policy for policy in (source_policy, model_policy, dataset_policy)}
    return policies, {"source": source_rights, "model": model_rights, "dataset": dataset_rights}


@pytest.mark.integration
def test_real_native_zenml_generation_judge_pipeline(local):
    model_dependency("zenml")
    from modelbuilder.backend.common import write_json
    registry, pid, record = local["registry"], local["project"]["project_id"], local["record"]
    reference = registry.portable_reference(pid, record["dataset_id"], 1)
    dependency_paths, guards = registry.execution_sources(pid, record["dataset_id"], 1)
    reference_path = local["tmp"] / "dataset-reference.json"
    reference_path.write_text(json.dumps(reference, sort_keys=True))
    from tests.components.rights_test_support import USAGE_CONTEXT, authorize_test_recipe
    policies, rights = _local_reviews(local)
    dependency_paths["dataset-reference"] = str(reference_path)
    dependency_paths.update(local["context"]["dependencies"])
    dependencies = [{"id": key, "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                     "kind": "model" if key in local["context"]["dependencies"] else "dataset",
                     "metadata": {"rights": rights["model" if key in local["context"]["dependencies"] else "dataset"],
                                  **({"dataset_reference": reference} if key == "dataset-reference" else
                                     {"dataset_source": True} if key not in local["context"]["dependencies"] else {})}}
                    for key, path in dependency_paths.items()]
    nodes, edges = [], []
    for name, split in [("prompts", "train"), ("validation", "dev"), ("test", "test")]:
        nodes.append({"id": name, "kind": "dataset.version.import", "timeout": 120, "inputs": [], "outputs": [{"name": "data", "type": "dict"}],
            "parameters": {"reference_id": "dataset-reference", "split_id": split,
                           "purpose": "training" if split == "train" else "evaluation"}})
        edges.append({"source_node": name, "source_port": "data", "target_node": "generate", "target_port": name})
    for index, name, params in [(0, "generate", local["params"]), (1, "judge", local["judge_params"])]:
        entry = catalog_entries()[index]
        descriptor = {key: entry[key] for key in ("id", "version", "protocol", "role", "description", "parameter_schema", "capabilities")}
        node = {"id": name, "kind": "component.run", "inputs": entry["inputs"], "outputs": entry["outputs"],
            "provenance": {"intended_use": "generate" if name == "generate" else "evaluate"},
            "component": descriptor, "parameters": params, "timeout": 120, "retry": {"max_attempts": 1},
            "implementation_ref": {**local["context"]["source"]["files"][0], "module": "adapter", "entrypoint": name,
                "commit": local["context"]["source"]["commit"]}}
        validate_component(node)
        nodes.append(node)
    edges.append({"source_node": "generate", "source_port": "candidates", "target_node": "judge", "target_port": "candidates"})
    recipe = {"recipe_id": "native-generation", "revision": 1, "source": local["context"]["source"], "nodes": nodes,
              "edges": edges, "external_dependencies": dependencies, "evaluation": {},
              "extensions": {"artifact_rights": rights["source"], "usage_context": USAGE_CONTEXT}}
    authorization = authorize_test_recipe(local["tmp"], recipe, policies, project_id=pid)
    root = local["tmp"] / "native-backend"
    job = root / "jobs" / "native-job"
    write_json(job / "request.json", {**authorization, "root": str(root), "recipe": recipe, "project_id": pid, "operation_id": "native-inference",
        "run_name": "native-inference", "source_root": str(local["source"]), "trusted_code_roots": [str(local["source"])],
        "dependency_paths": dependency_paths, "dataset_source_guards": guards})
    env = {**os.environ, "ZENML_CONFIG_PATH": str(root / "zenml-config"), "ZENML_LOCAL_STORES_PATH": str(root / "zenml-stores"),
           "ZENML_ANALYTICS_OPT_IN": "false", "ZENML_LOGGING_VERBOSITY": "ERROR", "OMP_NUM_THREADS": "1",
           "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    result = subprocess.run([sys.executable, "-m", "modelbuilder.backend.worker", str(job)], env=env,
                            capture_output=True, text=True, timeout=180)
    (local["tmp"] / "native.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, (result.stdout + result.stderr)[-14000:]
    assert (job / "native.json").is_file()
    inspection = """import json, sys
from pathlib import Path
from zenml.client import Client
job = Path(sys.argv[1])
native = json.loads((job / 'native.json').read_text())
run = Client().get_pipeline_run(native['backend_run_id'], include_full_metadata=True)
assert str(run.status) == 'completed', str(run.status)
outputs = {port: versions[-1].load() for step in run.steps.values() for port, versions in step.outputs.items() if port in {'candidates','judgments'}}
(job / 'verified-outputs.json').write_text(json.dumps(outputs))
from modelbuilder.backend.native_inputs import output_descriptor
bindings = []
for node, port in [('generate', 'candidates'), ('judge', 'judgments')]:
    artifact = run.steps[node].outputs[port][-1]
    descriptor, _ = output_descriptor(job, run, node, port, str(artifact.id))
    bindings.append({'node_id': 'prepare', 'input_port': port, **{key: descriptor[key] for key in
        ('backend_run_id', 'source_node_id', 'source_port', 'artifact_id', 'content_sha256')}})
(job / 'reviewed-bindings.json').write_text(json.dumps(bindings))
"""
    inspection_path = local["tmp"] / "inspect_native.py"
    inspection_path.write_text(inspection)
    inspected = subprocess.run([sys.executable, str(inspection_path), str(job)], env=env, capture_output=True, text=True, timeout=60)
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    actual = json.loads((job / "verified-outputs.json").read_text())
    assert len(actual["candidates"]["rows"]) == 4
    assert actual["judgments"]["candidate_digest"] == actual["candidates"]["digest"]
    assert actual["judgments"]["status"] == "model_predictions_not_ground_truth"
    assert len(actual["judgments"]["predictions"]) == 4
    assert actual["candidates"]["provenance"]["splits"]["prompts"]["version_id"] == record["version_id"]
    entry = catalog_entries()[2]
    descriptor = {key: entry[key] for key in ("id", "version", "protocol", "role", "description", "parameter_schema", "capabilities")}
    prepared_node = {"id": "prepare", "kind": "component.run", "inputs": entry["inputs"], "outputs": entry["outputs"],
        "provenance": {"intended_use": "generate"}, "component": descriptor, "parameters": _review(actual), "timeout": 120,
        "implementation_ref": {**local["context"]["source"]["files"][0], "module": "adapter", "entrypoint": "prepare_dataset",
                               "commit": local["context"]["source"]["commit"]}}
    reviewed_recipe = deepcopy(recipe)
    reviewed_recipe["revision"] = 2
    reviewed_recipe["nodes"].append(prepared_node)
    reviewed_recipe["edges"].extend([{ "source_node": name, "source_port": port, "target_node": "prepare", "target_port": port}
                                    for name, port in [("generate", "candidates"), ("judge", "judgments")]])
    reviewed_job = root / "jobs" / "reviewed-native-job"
    request = json.loads((job / "request.json").read_text())
    # A new authorization changes execution provenance even when sampled tokens
    # are equal. Review the original exact native artifacts; do not regenerate
    # them under a different receipt and pretend the prior review still applies.
    request.update(recipe=reviewed_recipe, operation_id="native-reviewed", run_name="native-reviewed",
        execution={"node_ids": ["prepare"], "input_bindings": json.loads((job / "reviewed-bindings.json").read_text())},
        input_binding_jobs=[str(job)])
    from modelbuilder.execution import selected_recipe, selection
    active_recipe = selected_recipe(reviewed_recipe, selection(reviewed_recipe, request["execution"]))
    request.update(authorize_test_recipe(local["tmp"], active_recipe, policies, project_id=pid))
    write_json(reviewed_job / "request.json", request)
    binding_inspection = """import json,sys
from pathlib import Path
from modelbuilder.backend.native_inputs import resolve_bindings
job=Path(sys.argv[1]); request=json.loads((job/'request.json').read_text())
resolved=resolve_bindings(request, request['input_binding_jobs'])
(job/'resolved-inputs.json').write_text(json.dumps(resolved['input_bindings']))
"""
    binding_inspection_path = local["tmp"] / "inspect_bindings.py"
    binding_inspection_path.write_text(binding_inspection)
    resolved = subprocess.run([sys.executable, str(binding_inspection_path), str(reviewed_job)], env=env,
                              capture_output=True, text=True, timeout=60)
    assert resolved.returncode == 0, resolved.stdout + resolved.stderr
    request["resolved_input_bindings"] = json.loads((reviewed_job / "resolved-inputs.json").read_text())
    assert not [item for item in active_recipe["external_dependencies"] if item["kind"] == "dataset"]
    assert all(item["rights_required"] for item in request["resolved_input_bindings"])
    for item in request["resolved_input_bindings"]:
        assert {"test-authored-data", "test-random-models"} <= {source["license_policy_id"] for source in item["rights"]["sources"]}
    # Service-like admission includes the exact resolved native evidence, even
    # though the original dataset importer and its resources are absent.
    from modelbuilder.backend.execution_rights import bind_authority, prepare_authorization
    from types import SimpleNamespace
    backend = SimpleNamespace(_execution_rights_authority=bind_authority(pid, policies, local["tmp"] / "test-review.sqlite3"))
    request["rights_authorization"] = prepare_authorization(backend,
        {"project_id": pid, "resolved_input_bindings": request["resolved_input_bindings"]}, active_recipe, durable=True)
    write_json(reviewed_job / "request.json", request)
    reviewed = subprocess.run([sys.executable, "-m", "modelbuilder.backend.worker", str(reviewed_job)], env=env,
                              capture_output=True, text=True, timeout=180)
    (local["tmp"] / "reviewed-native.log").write_text(reviewed.stdout + reviewed.stderr)
    assert reviewed.returncode == 0, (reviewed.stdout + reviewed.stderr)[-14000:]
    check_selection = """import json,sys
from pathlib import Path
from zenml.client import Client
native=json.loads((Path(sys.argv[1])/'native.json').read_text())
run=Client().get_pipeline_run(native['backend_run_id'],include_full_metadata=True)
assert set(run.steps)=={'prepare'}, set(run.steps)
"""
    selected = subprocess.run([sys.executable, "-c", check_selection, str(reviewed_job)], env=env,
                              capture_output=True, text=True, timeout=60)
    assert selected.returncode == 0, selected.stdout + selected.stderr
    prepared = reviewed_job / "outputs" / "prepare"
    proof = json.loads((prepared / "review.provenance.json").read_text())
    assert proof["candidates"] == actual["candidates"] and proof["judgments"] == actual["judgments"]
    assert proof["registration_started"] is False and proof["training_started"] is False
    generated_registry = DatasetRegistry(local["service"].store, project_roots={pid: {"generated": str(prepared)}})
    registered = generated_registry.register(pid, json.loads((prepared / "dataset.manifest.json").read_text()))
    assert registered["row_count"] == 2 and registered["benchmark"] == "generated-weather-v1"
    assert registry.get(pid, record["dataset_id"], 1) == record
    manifest = json.loads((prepared / "dataset.manifest.json").read_text())
    assert {"test-authored-data", "test-random-models"} <= {source["license_policy_id"] for source in manifest["rights"]["sources"]}
    assert generated_registry.read_split(pid, registered["dataset_id"], 1, "train", purpose="training")["rights"] == manifest["rights"]
    # Revocation after successful generation/preparation blocks another exact
    # native binding before its selected source function can run.
    from modelbuilder.store import SQLiteStore
    from modelbuilder.hub_rights import digest
    store = SQLiteStore(local["tmp"] / "test-review.sqlite3")
    policy = deepcopy(policies["test-authored-data"])
    policy.update(revision=2, status="revoked")
    policy["review_digest"] = digest({key: value for key, value in policy.items() if key != "review_digest"})
    store.put("hub_license_policy", policy["policy_id"], pid, policy, expected_revision=1)
    store.close()
    blocked_job = root / "jobs" / "revoked-generated-parent"
    write_json(blocked_job / "request.json", {**request, "operation_id": "revoked-parent", "run_name": "revoked-parent"})
    blocked = subprocess.run([sys.executable, "-m", "modelbuilder.backend.worker", str(blocked_job)], env=env,
                             capture_output=True, text=True, timeout=180)
    (local["tmp"] / "revoked-native.log").write_text(blocked.stdout + blocked.stderr)
    assert blocked.returncode != 0
    assert not (blocked_job / "outputs" / "prepare" / "dataset.manifest.json").exists()
    assert "policy" in (blocked.stdout + blocked.stderr).lower()
    # A foreign project cannot reuse these native artifact IDs, independently
    # of whether it happens to hold a similarly named review.
    foreign_job = root / "jobs" / "foreign-generated-parent"
    write_json(foreign_job / "request.json", {**request, "project_id": "foreign-project"})
    foreign_inspection = local["tmp"] / "inspect_foreign_bindings.py"
    foreign_inspection.write_text("from zenml.client import Client\nClient().create_project('modelbuilder-foreign-project', 'Independent synthetic test project')\n" + binding_inspection)
    foreign = subprocess.run([sys.executable, str(foreign_inspection), str(foreign_job)], env=env,
                             capture_output=True, text=True, timeout=60)
    assert foreign.returncode != 0 and "not owned by this project" in foreign.stderr


@pytest.mark.parametrize("kind", ["custom_code", "huge_model", "pickle", "symlink"])
def test_unsafe_asset_formats_and_configurations_are_refused(local, kind):
    params, context = deepcopy(local["params"]), deepcopy(local["context"])
    key = "config" if kind in {"custom_code", "huge_model"} else "weights"
    reference = params["model"][key]
    original = Path(context["dependencies"][reference["dependency_id"]])
    path = local["tmp"] / ("unsafe-" + key)
    if kind == "symlink":
        path.symlink_to(original)
    elif kind == "pickle":
        path.write_bytes(b"\x80\x04not a safetensors model")
    else:
        value = json.loads(original.read_text())
        value.update({"auto_map": {"AutoModel": "evil.code"}} if kind == "custom_code" else {"n_embd": 1000000})
        path.write_text(json.dumps(value))
    context["dependencies"][reference["dependency_id"]] = str(path)
    reference["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(Exception):
        generate(inputs=local["inputs"], parameters=params, context=context)


def test_adapters_do_not_use_network_and_protected_predictions_are_rejected(local, monkeypatch):
    import requests
    def denied(*args, **kwargs):
        raise AssertionError("Inference must never contact an external service")
    monkeypatch.setattr(requests.Session, "request", denied)
    results = _results(local)
    # Keep real inference and reseal a test-only protected split with one actual
    # generated text. The repeat must withhold this overlap, not silently admit it.
    inputs = deepcopy(local["inputs"])
    protected = inputs["test"]
    protected["rows"][0]["text"] = results["candidates"]["rows"][0]["text"]
    lineage = protected["consumption"]
    lineage["sample_digests"] = [identity({"text": protected["rows"][0]["text"]})]
    lineage["digest"] = identity({key: value for key, value in lineage.items() if key != "digest"})
    repeated = generate(inputs=inputs, parameters=local["params"], context=local["context"])["candidates"]
    assert repeated["rejected"] and all(row["reason"] == "protected_content_overlap" for row in repeated["rejected"])
    assert all(row["text"] != protected["rows"][0]["text"] for row in repeated["rows"])


@pytest.mark.integration
def test_human_library_source_pin_http_native_path_in_arbitrary_project(local):
    """Compiled authoring controls to source pin/save/bind/plan/run, no product source copy."""
    import shutil
    import time
    import requests
    import socket
    frontend = Path(__file__).resolve().parents[2] / "packages" / "ide_feature"
    if not shutil.which("node") or not (frontend / "out/componentLibrary.js").is_file():
        pytest.skip("Compile the frontend first to test its actual exported authoring transforms")
    service_python = Path(os.environ.get("MODELBUILDER_SERVICE_PYTHON", str(Path(sys.executable).parents[2] / ".venv/bin/python")))
    if not service_python.is_file():
        pytest.skip("Set MODELBUILDER_SERVICE_PYTHON to the installed service interpreter")
    pid, project = local["project"]["project_id"], local["project"]
    from tests.components.rights_test_support import USAGE_CONTEXT, write_reviewed_policies
    policies, rights = _local_reviews(local, source_alias="independent")
    write_reviewed_policies(local["service"].store.path, policies)
    config = local["tmp"] / "operator.json"
    config.write_text(json.dumps({"python_executable": sys.executable,
        "source_roots": {"independent": {"path": str(local["source"]), "trusted": True,
                                        "dependency_paths": local["context"]["dependencies"]}},
        "dataset_roots": {pid: {"data": str(local["tmp"] / "data")}}}))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = "http://127.0.0.1:" + str(port)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "MODELBUILDER_DB_PATH": str(local["tmp"] / "service.sqlite"), "MODELBUILDER_BACKEND": "zenml",
        "MODELBUILDER_HOST": "127.0.0.1", "MODELBUILDER_PORT": str(port),
        "MODELBUILDER_OPERATOR_CONFIG": str(config), "MODELBUILDER_TOKENS_JSON": json.dumps({
            "test-local-owner": {"id": "owner", "scopes": ["read", "modify", "execute"]}})}
    log = (local["tmp"] / "human-http.log").open("w")
    process = subprocess.Popen([str(service_python), "-m", "modelbuilder.api"], cwd=local["tmp"], env=env,
                               stdout=log, stderr=subprocess.STDOUT)
    local["http_process"] = process
    local["http_log"] = log
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if requests.get(url + "/health", timeout=1).ok:
                break
        except requests.RequestException:
            pass
        if process.poll() is not None:
            pytest.fail((local["tmp"] / "human-http.log").read_text())
        time.sleep(.1)
    def raw(operation, payload):
        return requests.post(url + "/v1/operations/" + operation, json=payload,
                             headers={"Authorization": "Bearer test-local-owner"}, timeout=180)
    def call(operation, payload):
        response = raw(operation, payload)
        assert response.status_code == 200, response.text
        return response.json()
    source = call("project.source.list", {})["sources"][0]
    bound = call("project.source.bind", {"project_id": pid, "expected_revision": project["revision"],
        "source_alias": "independent", "source_repository_id": source["repository_id"], "trusted": True,
        "idempotency_key": "bind-independent"})
    pin = {"project_id": pid, "recipe_id": project["recipe_id"], "expected_revision": 1,
        "expected_project_revision": bound["revision"], "source_repository_id": source["repository_id"],
        "commit": local["context"]["source"]["commit"], "paths": ["adapter.py"]}
    preview = call("recipe.source.pin.preview", pin)
    pinned = call("recipe.source.pin", {**pin, "review_digest": preview["review_digest"],
        "confirm_pin": True, "idempotency_key": "pin-independent"})
    assert not pinned["git_modified"] and not pinned["execution_allowed"]
    recipe = pinned["recipe"]
    catalog = call("component.list", {"project_id": pid})
    author = r'''
const fs=require('node:fs'),{addLibraryComponent}=require('./out/componentLibrary'),
 {applyComponentFields,componentFields,componentDraftProblems}=require('./out/componentAuthoring'),
 {replaceNode}=require('./out/document');
const data=JSON.parse(fs.readFileSync(0,'utf8'));let recipe=data.recipe;let incomplete;
for(const [name,parameters]of [['generate',data.generate],['judge',data.judge]]){
 const entry=data.catalog.components.find(item=>item.id==='modelbuilder.local.'+name&&item.adapter);
 recipe=addLibraryComponent(recipe,entry,name,recipe.recipe_id,recipe.revision);
 let node=recipe.nodes.find(node=>node.id===name);
 if(!componentDraftProblems(recipe,node).length)throw Error('Library must be incomplete before source binding');
 if(!incomplete)incomplete=structuredClone(recipe);
 const fields={...componentFields(node),sourcePath:'adapter.py',module:'adapter',entrypoint:name};
 node=applyComponentFields(recipe,{...node,parameters},fields);
 recipe=replaceNode(recipe,node);
}
recipe.edges.push({source_node:'generate',source_port:'candidates',target_node:'judge',target_port:'candidates'});
process.stdout.write(JSON.stringify({recipe,incomplete}));
'''
    result = subprocess.run(["node", "-e", author], cwd=frontend,
        input=json.dumps({"recipe": recipe, "catalog": catalog, "generate": local["params"], "judge": local["judge_params"]}),
        capture_output=True, text=True, check=True)
    authored = json.loads(result.stdout)
    rejected = raw("recipe.update", {"project_id": pid, "recipe_id": recipe["recipe_id"],
        "expected_revision": recipe["revision"], "recipe": authored["incomplete"], "idempotency_key": "incomplete"})
    assert rejected.status_code != 200
    recipe = authored["recipe"]
    recipe.setdefault("extensions", {}).update(artifact_rights=rights["source"], usage_context=USAGE_CONTEXT)
    for node in recipe["nodes"]:
        node["provenance"] = {"intended_use": "generate" if node["id"] == "generate" else "evaluate"}
    recipe["external_dependencies"] = [{"id": name, "kind": "model", "license": "locally authored test model",
        "required": True, "url": "urn:sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(), "metadata": {"rights": rights["model"]}}
        for name, path in local["context"]["dependencies"].items()]
    recipe["environment"] = {"python": "3.12", "requirements": ["zenml==0.97.0", "transformers==4.57.1",
        "torch==2.9.0+cpu", "tokenizers==0.22.2", "safetensors==0.8.0"]}
    saved = call("recipe.update", {"project_id": pid, "recipe_id": recipe["recipe_id"], "expected_revision": recipe["revision"],
        "recipe": recipe, "idempotency_key": "save-complete"})
    call("hub.dataset.policy.bind", {"project_id": pid, "dataset_id": local["record"]["dataset_id"],
        "dataset_revision": local["record"]["revision"],
        "source_policies": {item["id"]: "test-authored-data" for item in local["record"]["manifest"]["sources"]},
        "intended_use": "train", "usage_context": USAGE_CONTEXT, "idempotency_key": "review-exact-data"})
    for name, split in [("prompts", "train"), ("validation", "dev"), ("test", "test")]:
        response = call("dataset.bind", {"project_id": pid, "recipe_id": saved["recipe_id"], "expected_revision": saved["revision"],
            "dataset_id": local["record"]["dataset_id"], "dataset_revision": 1, "split_id": split,
            "purpose": "training" if name == "prompts" else "evaluation", "node_id": name,
            "target_node": "generate", "target_port": name, "idempotency_key": "bind-" + name})
        assert response["execution_started"] is False
        saved = response["recipe"]
    request = {"project_id": pid, "recipe_id": saved["recipe_id"], "trusted": True}
    assert call("run.list", {"project_id": pid})["runs"] == []
    plan = call("run.plan", request)
    assert plan["executable"], plan
    assert saved["source"]["files"] == local["context"]["source"]["files"]
    assert sorted(path.name for path in local["source"].iterdir()) == [".git", "adapter.py"]
    assert subprocess.check_output(["git", "-C", str(local["source"]), "status", "--porcelain"], text=True) == ""
    run = call("run.submit", {**request, "expected_revision": saved["revision"], "idempotency_key": "native-ui-run"})
    deadline = time.monotonic() + 120
    status = run
    while time.monotonic() < deadline:
        status = call("run.status", {"project_id": pid, "operation_id": run["operation_id"]})
        if status["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(.25)
    assert status["status"] == "succeeded", status
    assert status["backend_run_id"] and {"generate", "judge"} <= {item["node_id"] for item in status["artifacts"]}
    assert call("project.open", {"project_id": pid})["recipe"]["recipe_id"] == project["recipe_id"]
    # Source drift after this success still blocks plan; the packaged adapter
    # cannot turn the library preset into an admission bypass.
    (local["source"] / "adapter.py").write_text("raise RuntimeError('must not be imported')\n")
    denied = call("run.plan", request)
    assert denied["executable"] is False



def test_persisted_tokenizer_truncation_cannot_hide_an_oversized_prompt(local):
    from tokenizers import Tokenizer
    params, context = deepcopy(local["params"]), deepcopy(local["context"])
    ref = params["model"]["tokenizer"]
    tokenizer = Tokenizer.from_file(context["dependencies"][ref["dependency_id"]])
    tokenizer.enable_truncation(max_length=4)
    path = local["tmp"] / "truncating-tokenizer.json"
    tokenizer.save(str(path))
    ref["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    context["dependencies"][ref["dependency_id"]] = str(path)
    params["prompt_prefix"] = "rain " * 100
    with pytest.raises(ValueError, match="context"):
        generate(inputs=local["inputs"], parameters=params, context=context)



def test_multilabel_judge_is_not_silently_scored_as_single_label(local):
    results = _results(local)
    params, context = deepcopy(local["judge_params"]), deepcopy(local["context"])
    reference = params["model"]["config"]
    original = Path(context["dependencies"][reference["dependency_id"]])
    config = json.loads(original.read_text())
    config["problem_type"] = "multi_label_classification"
    path = local["tmp"] / "multilabel.json"
    path.write_text(json.dumps(config))
    reference["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    context["dependencies"][reference["dependency_id"]] = str(path)
    with pytest.raises(ValueError, match="single-label"):
        judge(inputs={"candidates": results["candidates"]}, parameters=params, context=context)
