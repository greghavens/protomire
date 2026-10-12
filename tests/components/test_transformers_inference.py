"""Real CPU generation/scoring through multiple installed Transformers families."""
from copy import deepcopy
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator, ValidationError

from modelbuilder.backend.components import component_catalog, identity, validate_component
from modelbuilder.backend.components import transformers_inference as adapters
from modelbuilder.backend.components.local_inference import _seal
from modelbuilder.dataset import DatasetRegistry
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService


def dependency(name):
    if os.environ.get("MODELBUILDER_REQUIRE_LOCAL_INFERENCE") == "1":
        return importlib.import_module(name)
    return pytest.importorskip(name)


@pytest.fixture(params=[("gpt2", "bert"), ("llama", "distilbert")])
def local(request, tmp_path):
    torch = dependency("torch")
    dependency("transformers")
    from transformers import (GPT2Config, GPT2LMHeadModel, LlamaConfig, LlamaForCausalLM,
        BertConfig, BertForSequenceClassification, DistilBertConfig, DistilBertForSequenceClassification)
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from safetensors.torch import save_model
    assets = tmp_path / "assets"
    assets.mkdir()
    tokenizer = Tokenizer(WordLevel({word: i for i, word in enumerate(
        ["[UNK]", "[PAD]", "rain", "sun", "warm", "cold", "weather", "report", "change", "snow", "forecast", "wind"])},
        unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.save(str(assets / "tokenizer.json"))
    torch.manual_seed(137)
    generators = {
        "gpt2": lambda: GPT2LMHeadModel(GPT2Config(vocab_size=12, n_positions=128, n_ctx=128, n_embd=8,
            n_layer=1, n_head=2, bos_token_id=None, eos_token_id=None, pad_token_id=1,
            resid_pdrop=0, embd_pdrop=0, attn_pdrop=0)),
        "llama": lambda: LlamaForCausalLM(LlamaConfig(vocab_size=12, hidden_size=8, intermediate_size=16,
            num_hidden_layers=1, num_attention_heads=2, num_key_value_heads=2, max_position_embeddings=128,
            bos_token_id=None, eos_token_id=None, pad_token_id=1, attention_dropout=0)),
    }
    judges = {
        "bert": lambda: BertForSequenceClassification(BertConfig(vocab_size=12, hidden_size=8,
            num_hidden_layers=1, num_attention_heads=2, intermediate_size=16, max_position_embeddings=128,
            num_labels=2, pad_token_id=1, hidden_dropout_prob=0, attention_probs_dropout_prob=0)),
        "distilbert": lambda: DistilBertForSequenceClassification(DistilBertConfig(vocab_size=12, dim=8,
            hidden_dim=16, n_layers=1, n_heads=2, max_position_embeddings=128,
            num_labels=2, pad_token_id=1, dropout=0, attention_dropout=0, seq_classif_dropout=0)),
    }
    models, dependencies, instances = {}, {}, {}
    for name, model in [("generator", generators[request.param[0]]()), ("judge", judges[request.param[1]]())]:
        instances[name] = model
        config, weights = assets / (name + ".json"), assets / (name + ".safetensors")
        model.config.to_json_file(config, use_diff=False)
        save_model(model, str(weights))
        refs = {}
        for kind, path in [("config", config), ("weights", weights), ("tokenizer", assets / "tokenizer.json")]:
            key = name + "-" + kind
            dependencies[key] = str(path)
            refs[kind] = {"dependency_id": key, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        models[name] = refs
    service = OperationService(tmp_path / "service.sqlite")
    actor = Actor("owner", {"read", "modify", "execute"})
    project = service.invoke("project.create", {"slug": "general-inference", "model_name": "Inference",
        "idempotency_key": "project"}, actor)
    data_root = tmp_path / "data"
    data_root.mkdir()
    data = data_root / "rows.jsonl"
    records = [{"id": "a", "text": "rain forecast"}, {"id": "b", "text": "sun report"},
               {"id": "dev", "text": "private heldout validation"}, {"id": "test", "text": "private heldout test"}]
    data.write_text("".join(json.dumps(row) + "\n" for row in records))
    manifest = {"name": "Authored data", "benchmark": "independent-weather-v2", "license": "CC0-1.0",
        "provenance": {"source": "independently authored"},
        "sources": [{"id": "data", "root_id": "data", "path": data.name, "format": "jsonl",
                     "sha256": hashlib.sha256(data.read_bytes()).hexdigest()}],
        "id_column": "id", "row_schema": {"type": "object", "required": ["id", "text"], "additionalProperties": False,
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}}},
        "splits": [{"id": name, "role": role, "sample_ids": ids} for name, role, ids in
                   [("train", "train", ["a", "b"]), ("dev", "validation", ["dev"]), ("test", "test", ["test"])]]}
    registry = DatasetRegistry(service.store, project_roots={project["project_id"]: {"data": str(data_root)}})
    record = registry.register(project["project_id"], manifest)
    splits = {name: registry.read_split(project["project_id"], record["dataset_id"], 1, split, purpose=purpose)
        for name, split, purpose in [("prompts", "train", "training"), ("validation", "dev", "evaluation"),
                                    ("test", "test", "evaluation")]}
    limits = {"max_parameters": 1000000, "max_asset_bytes": 1048576, "max_rows": 1000,
              "max_tokenized_tokens": 100000, "max_text_chars": 16384, "max_artifact_bytes": 16777216}
    parameters = {"model": models["generator"], "limits": limits, "max_length": 24, "pad_token_id": 1,
        "seed": 42, "text_field": "text", "prompt_prefix": "change ", "max_new_tokens": 3,
        "samples_per_prompt": 2, "temperature": .8, "top_k": 12, "eos_token_ids": []}
    judge = {"model": models["judge"], "limits": limits, "max_length": 24, "pad_token_id": 1,
             "labels": ["ordinary", "difficult"], "score_label": "difficult", "rank_direction": "highest_first",
             "batch_size": 2}
    context = {"dependencies": dependencies, "device": "cpu", "source": {"commit": "test-source"},
               "identities": {"source": "test-source"}, "output_dir": str(tmp_path / "prepared")}
    try:
        yield {"context": context, "parameters": parameters, "judge": judge, "inputs": splits,
               "models": instances, "tokenizer": tokenizer, "tmp": tmp_path, "service": service,
               "actor": actor, "project": project, "registry": registry, "record": record, "families": request.param}
    finally:
        service.store.close()


def _results(local):
    candidates = adapters.generate(inputs=local["inputs"], parameters=local["parameters"], context=local["context"])["candidates"]
    judgments = adapters.judge(inputs={"candidates": candidates}, parameters=local["judge"], context=local["context"])["judgments"]
    return {"candidates": candidates, "judgments": judgments}


def _review(results):
    return {"candidate_digest": results["candidates"]["digest"], "judgment_digest": results["judgments"]["digest"],
        "approved_ids": results["judgments"]["ranked_ids"][:2], "name": "Reviewed generated rows",
        "benchmark": "reviewed-synthetic-v1", "license": "CC0-1.0", "root_id": "generated",
        "review_note": "Explicit review of selected rows", "limits": {"max_rows": 1000,
        "max_artifact_bytes": 16777216, "max_text_chars": 16384}}


def test_inert_catalog_and_source_authoring_contract():
    entries = adapters.catalog_entries()
    assert {entry["id"] for entry in entries} <= {entry["id"] for entry in component_catalog()}
    assert entries[1]["role"] == "utility" and entries[1]["outputs"] == [{"name": "judgments", "type": "dict"}]
    for entry in entries:
        assert entry["capabilities"]["devices"] == ["cpu"]
        assert "implementation_ref" not in entry
        Draft202012Validator.check_schema(entry["parameter_schema"])
    script = "from modelbuilder.backend.components import component_catalog; import sys; component_catalog(); assert not {'torch','transformers','accelerate'} & sys.modules.keys()"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_real_general_generation_judging_provenance_and_model_effect(local):
    import torch
    from safetensors.torch import save_model
    rng, threads = torch.get_rng_state().clone(), torch.get_num_threads()
    results = _results(local)
    assert results == _results(local)
    assert torch.equal(torch.get_rng_state(), rng) and torch.get_num_threads() == threads
    candidates, judgments = results["candidates"], results["judgments"]
    assert candidates["provenance"]["generator"]["model_type"] == local["families"][0]
    assert judgments["provenance"]["judge"]["model_type"] == local["families"][1]
    assert len(candidates["rows"]) == 4 and candidates["status"] == "candidates_only"
    assert all(len(row["generated_token_ids"]) == 3 for row in candidates["rows"])
    assert judgments["status"] == "model_predictions_not_ground_truth"
    assert all(row["prediction_kind"] == "model_prediction" for row in judgments["predictions"])
    assert set(judgments["ranked_ids"]) == {row["id"] for row in candidates["rows"]}
    serialized = json.dumps(results)
    assert "private heldout" not in serialized and str(local["tmp"]) not in serialized
    assert candidates["provenance"]["runtime"]["packages"]["transformers"]
    changed = adapters.generate(inputs=local["inputs"], parameters={**local["parameters"], "seed": 43}, context=local["context"])["candidates"]
    assert [row["generated_token_ids"] for row in changed["rows"]] != [row["generated_token_ids"] for row in candidates["rows"]]
    model = local["models"]["judge"]
    with torch.no_grad():
        model.classifier.weight.zero_()
        model.classifier.bias.copy_(torch.tensor([-8., 8.]))
    path = local["tmp"] / "changed.safetensors"
    save_model(model, str(path))
    parameters = deepcopy(local["judge"])
    parameters["model"]["weights"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    context = {**local["context"], "dependencies": {**local["context"]["dependencies"], "judge-weights": str(path)}}
    changed = adapters.judge(inputs={"candidates": candidates}, parameters=parameters, context=context)["judgments"]
    assert all(row["score"] > .99 for row in changed["predictions"])
    assert changed["predictions"] != judgments["predictions"]
    assert changed["provenance"]["judge"]["digest"] != judgments["provenance"]["judge"]["digest"]


def test_explicit_budgets_replace_fixture_token_and_sample_ceilings(local):
    parameters = {**local["parameters"], "max_length": 96, "max_new_tokens": 65, "samples_per_prompt": 1}
    result = adapters.generate(inputs=local["inputs"], parameters=parameters, context=local["context"])["candidates"]
    assert all(len(row["generated_token_ids"]) == 65 for row in result["rows"])
    parameters = {**local["parameters"], "samples_per_prompt": 9}
    assert len(adapters.generate(inputs=local["inputs"], parameters=parameters, context=local["context"])["candidates"]["rows"]) == 18
    parameters = {**local["parameters"], "samples_per_prompt": 65, "max_new_tokens": 1}
    assert len(adapters.generate(inputs=local["inputs"], parameters=parameters, context=local["context"])["candidates"]["rows"]) == 130
    parameters["limits"] = {**parameters["limits"], "max_parameters": 1_000_000_000, "max_asset_bytes": 8_000_000_000}
    Draft202012Validator(adapters.GENERATION_PARAMETERS).validate(parameters)


@pytest.mark.parametrize("change,match", [
    (lambda p: p.update(device="cuda"), "Additional properties"),
    (lambda p: p["model"].update(trust_remote_code=True), "Additional properties"),
    (lambda p: p["limits"].update(max_parameters=10), "parameter count"),
    (lambda p: p["limits"].update(max_asset_bytes=1), "resource limit"),
    (lambda p: p["limits"].update(max_rows=3), "row limit"),
    (lambda p: p["limits"].update(max_tokenized_tokens=50), "token budget"),
    (lambda p: p["limits"].update(max_text_chars=5), "character limit"),
    (lambda p: p["limits"].update(max_artifact_bytes=10), "byte limit"),
    (lambda p: p.update(max_length=200), "configured context"),
    (lambda p: p.update(max_length=4), "declared context"),
    (lambda p: p.update(top_k=13), "vocabulary"),
    (lambda p: p.update(eos_token_ids=[12]), "vocabulary"),
    (lambda p: p["model"]["weights"].update(sha256="0" * 64), "admitted SHA256"),
    (lambda p: p["model"]["weights"].update(dependency_id="missing"), "admitted dependency"),
])
def test_generation_budget_and_safety_refusals(local, change, match):
    parameters = deepcopy(local["parameters"])
    change(parameters)
    with pytest.raises((ValueError, ValidationError), match=match):
        adapters.generate(inputs=local["inputs"], parameters=parameters, context=local["context"])


def test_remote_code_symlink_and_accelerator_refused(local):
    parameters = deepcopy(local["parameters"])
    context = deepcopy(local["context"])
    source = Path(context["dependencies"]["generator-config"])
    config = json.loads(source.read_text())
    config["auto_map"] = {"AutoModelForCausalLM": "evil.Model"}
    source.write_text(json.dumps(config))
    parameters["model"]["config"]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="installed, unquantized"):
        adapters.generate(inputs=local["inputs"], parameters=parameters, context=context)
    with pytest.raises(ValueError, match="CPU float32"):
        adapters.generate(inputs=local["inputs"], parameters=parameters, context={**context, "device": "cuda"})
    link = local["tmp"] / "link.json"
    link.symlink_to(source)
    context["dependencies"]["generator-config"] = str(link)
    with pytest.raises(ValueError, match="Symlinked"):
        adapters.generate(inputs=local["inputs"], parameters=parameters, context=context)


def _reseal_split(data):
    lineage = data["consumption"]
    lineage["sample_digests"] = [identity({k: v for k, v in row.items() if k != "id"}) for row in data["rows"]]
    lineage["sample_ids"] = list(data["split"]["sample_ids"])
    lineage["split"] = deepcopy(data["split"])
    data["consumption"] = _seal({key: value for key, value in lineage.items() if key != "digest"})


def test_registered_lineage_heldout_isolation_and_generated_overlap(local):
    inputs = deepcopy(local["inputs"])
    inputs["prompts"]["rows"][0]["text"] = "changed without registration"
    with pytest.raises(ValueError, match="registered content hash"):
        adapters.generate(inputs=inputs, parameters=local["parameters"], context=local["context"])
    inputs = deepcopy(local["inputs"])
    inputs["prompts"]["consumption"]["purpose"] = "evaluation"
    _reseal_split(inputs["prompts"])
    with pytest.raises(ValueError, match="registered row lineage"):
        adapters.generate(inputs=inputs, parameters=local["parameters"], context=local["context"])
    inputs = deepcopy(local["inputs"])
    inputs["validation"]["rows"][0]["text"] = "  RAIN  forecast "
    _reseal_split(inputs["validation"])
    with pytest.raises(ValueError, match="overlap protected"):
        adapters.generate(inputs=inputs, parameters=local["parameters"], context=local["context"])
    inputs = deepcopy(local["inputs"])
    inputs["validation"]["rows"][0]["text"] = "change rain forecast"
    _reseal_split(inputs["validation"])
    with pytest.raises(ValueError, match="overlap protected"):
        adapters.generate(inputs=inputs, parameters=local["parameters"], context=local["context"])
    initial = adapters.generate(inputs=local["inputs"], parameters=local["parameters"], context=local["context"])["candidates"]
    inputs = deepcopy(local["inputs"])
    overlap = initial["rows"][0]["text"]
    inputs["validation"]["rows"][0]["text"] = overlap
    _reseal_split(inputs["validation"])
    guarded = adapters.generate(inputs=inputs, parameters=local["parameters"], context=local["context"])["candidates"]
    assert guarded["rejected"] and all(row["text"] != overlap for row in guarded["rows"])
    assert all(row["reason"] == "protected_content_overlap" for row in guarded["rejected"])
    inputs = deepcopy(local["inputs"])
    inputs["validation"]["split"]["sample_ids"] = ["a"]
    inputs["validation"]["rows"][0]["id"] = "a"
    _reseal_split(inputs["validation"])
    with pytest.raises(ValueError, match="identities overlap"):
        adapters.generate(inputs=inputs, parameters=local["parameters"], context=local["context"])


def test_judge_integrity_mapping_separation_and_context_limits(local):
    results = _results(local)
    candidates = deepcopy(results["candidates"])
    candidates["rows"][0]["text"] += " changed"
    with pytest.raises(ValueError, match="integrity mismatch"):
        adapters.judge(inputs={"candidates": candidates}, parameters=local["judge"], context=local["context"])
    candidates = results["candidates"]
    cases = [("labels", ["ordinary", "difficult", "extra"], "label"), ("score_label", "absent", "score_label"),
             ("max_length", 2, "max_length")]
    for key, value, match in cases:
        parameters = deepcopy(local["judge"])
        parameters[key] = value
        with pytest.raises(ValueError, match=match):
            adapters.judge(inputs={"candidates": candidates}, parameters=parameters, context=local["context"])
    parameters = deepcopy(local["judge"])
    parameters["model"]["weights"] = local["parameters"]["model"]["weights"]
    with pytest.raises(ValueError, match="separate model weights"):
        adapters.judge(inputs={"candidates": candidates}, parameters=parameters, context=local["context"])


def test_review_to_real_registry_is_train_only_and_explicit(local):
    results = _results(local)
    parameters = _review(results)
    registry, pid = local["registry"], local["project"]["project_id"]
    before = registry.list(pid)
    output = adapters.prepare_dataset(inputs=results, parameters=parameters, context=local["context"])["prepared"]
    assert registry.list(pid) == before
    assert output == adapters.prepare_dataset(inputs=results, parameters=parameters, context=local["context"])["prepared"]
    manifest = json.loads((output / "dataset.manifest.json").read_text())
    assert "label" not in manifest["row_schema"]["properties"]
    assert manifest["splits"] == [{"id": "train", "role": "train", "source_ids": ["reviewed"]}]
    generated = DatasetRegistry(local["service"].store, project_roots={pid: {"generated": str(output)}})
    record = generated.register(pid, manifest)
    assert record["row_count"] == 2
    rows = generated.read_split(pid, record["dataset_id"], 1, "train", purpose="training")["rows"]
    assert all(row["judge_predicted_label"] in local["judge"]["labels"] for row in rows)
    with pytest.raises(ServiceError, match="purpose"):
        generated.read_split(pid, record["dataset_id"], 1, "train", purpose="evaluation")
    for key, value, match in [("candidate_digest", "0" * 64, "identities changed"),
                              ("approved_ids", ["absent"], "exact candidates"),
                              ("benchmark", "independent-weather-v2", "separate benchmark")]:
        with pytest.raises(ValueError, match=match):
            adapters.prepare_dataset(inputs=results, parameters={**parameters, key: value}, context=local["context"])


def test_authoring_validation_and_cpu_only_cancel(local):
    entry = adapters.catalog_entries()[0]
    descriptor = {key: entry[key] for key in ("id", "version", "protocol", "role", "description", "parameter_schema", "capabilities")}
    node = {"component": descriptor, "parameters": local["parameters"], "inputs": entry["inputs"], "outputs": entry["outputs"]}
    with pytest.raises(ValueError, match="source-pinned"):
        validate_component(node)
    node["implementation_ref"] = {"module": "adapter", "entrypoint": "generate", "source_file": "adapter.py"}
    assert validate_component(node) == descriptor
    cancel = local["tmp"] / "cancel.json"
    cancel.write_text("{}")
    with pytest.raises(InterruptedError, match="cancelled"):
        adapters.generate(inputs=local["inputs"], parameters=local["parameters"], context={**local["context"], "cancel_file": str(cancel)})


def test_no_network_or_pickle_load_and_explicit_eos(local, monkeypatch):
    import requests
    from transformers import AutoModelForCausalLM, AutoModelForSequenceClassification
    from safetensors import SafetensorError
    def denied(*args, **kwargs):
        raise AssertionError("Inference must not retrieve a remote model or contact a service")
    monkeypatch.setattr(requests.Session, "request", denied)
    monkeypatch.setattr(AutoModelForCausalLM, "from_pretrained", denied)
    monkeypatch.setattr(AutoModelForSequenceClassification, "from_pretrained", denied)
    _results(local)
    parameters = {**local["parameters"], "eos_token_ids": list(range(12))}
    result = adapters.generate(inputs=local["inputs"], parameters=parameters, context=local["context"])["candidates"]
    assert all(len(row["generated_token_ids"]) == 1 for row in result["rows"])
    parameters = deepcopy(local["parameters"])
    weights = local["tmp"] / "pickle.bin"
    weights.write_bytes(b"\x80\x04not a safetensors model")
    parameters["model"]["weights"]["sha256"] = hashlib.sha256(weights.read_bytes()).hexdigest()
    context = {**local["context"], "dependencies": {**local["context"]["dependencies"], "generator-weights": str(weights)}}
    with pytest.raises(SafetensorError):
        adapters.generate(inputs=local["inputs"], parameters=parameters, context=context)


def test_model_specific_generation_defaults_cannot_override_recipe(local):
    before = adapters.generate(inputs=local["inputs"], parameters=local["parameters"], context=local["context"])["candidates"]
    parameters = deepcopy(local["parameters"])
    config_path = Path(local["context"]["dependencies"]["generator-config"])
    config = json.loads(config_path.read_text())
    config.update(eos_token_id=list(range(12)), forced_eos_token_id=11, num_beams=3, top_k=1)
    config_path.write_text(json.dumps(config))
    parameters["model"]["config"]["sha256"] = hashlib.sha256(config_path.read_bytes()).hexdigest()
    after = adapters.generate(inputs=local["inputs"], parameters=parameters, context=local["context"])["candidates"]
    assert [row["generated_token_ids"] for row in after["rows"]] == [row["generated_token_ids"] for row in before["rows"]]
    assert all(len(row["generated_token_ids"]) == parameters["max_new_tokens"] for row in after["rows"])


@pytest.mark.integration
def test_real_native_general_generation_judge_pipeline(local):
    dependency("zenml")
    from modelbuilder.backend.common import write_json
    source = local["tmp"] / "source"
    source.mkdir()
    wrapper = source / "adapter.py"
    wrapper.write_text("from modelbuilder.backend.components.transformers_inference import generate, judge, prepare_dataset\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "Independent inference wrapper"], check=True)
    code = {"commit": subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip(),
            "files": [{"path": "adapter.py", "sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest()}]}
    registry, pid, record = local["registry"], local["project"]["project_id"], local["record"]
    reference = registry.portable_reference(pid, record["dataset_id"], 1)
    paths, guards = registry.execution_sources(pid, record["dataset_id"], 1)
    reference_path = local["tmp"] / "dataset-reference.json"
    reference_path.write_text(json.dumps(reference, sort_keys=True))
    dependencies = reference["dependencies"] + [{"id": "dataset-reference", "sha256": hashlib.sha256(reference_path.read_bytes()).hexdigest()}]
    paths["dataset-reference"] = str(reference_path)
    paths.update(local["context"]["dependencies"])
    dependencies.extend({"id": key, "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}
                        for key, path in local["context"]["dependencies"].items())
    nodes, edges = [], []
    for name, split in [("prompts", "train"), ("validation", "dev"), ("test", "test")]:
        nodes.append({"id": name, "kind": "dataset.version.import", "timeout": 120, "inputs": [],
            "outputs": [{"name": "data", "type": "dict"}], "parameters": {"reference_id": "dataset-reference",
                "split_id": split, "purpose": "training" if split == "train" else "evaluation"}})
        edges.append({"source_node": name, "source_port": "data", "target_node": "generate", "target_port": name})
    for index, name, params in [(0, "generate", local["parameters"]), (1, "judge", local["judge"])]:
        entry = adapters.catalog_entries()[index]
        descriptor = {key: entry[key] for key in ("id", "version", "protocol", "role", "description", "parameter_schema", "capabilities")}
        node = {"id": name, "kind": "component.run", "inputs": entry["inputs"], "outputs": entry["outputs"],
            "component": descriptor, "parameters": params, "timeout": 120, "retry": {"max_attempts": 1},
            "implementation_ref": {**code["files"][0], "module": "adapter", "entrypoint": name, "commit": code["commit"]}}
        validate_component(node)
        nodes.append(node)
    edges.append({"source_node": "generate", "source_port": "candidates", "target_node": "judge", "target_port": "candidates"})
    recipe = {"recipe_id": "native-general-inference", "revision": 1, "source": code, "nodes": nodes,
              "edges": edges, "external_dependencies": dependencies, "evaluation": {}}
    job = local["tmp"] / "native-job"
    write_json(job / "request.json", {"recipe": recipe, "project_id": pid, "operation_id": "native-general-inference",
        "run_name": "native-general-inference", "source_root": str(source), "trusted_code_roots": [str(source)],
        "dependency_paths": paths, "dataset_source_guards": guards})
    env = {**os.environ, "ZENML_CONFIG_PATH": str(local["tmp"] / "zenml"), "ZENML_LOCAL_STORES_PATH": str(local["tmp"] / "stores"),
           "ZENML_ANALYTICS_OPT_IN": "false", "ZENML_LOGGING_VERBOSITY": "ERROR", "OMP_NUM_THREADS": "1",
           "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    result = subprocess.run([sys.executable, "-m", "modelbuilder.backend.worker", str(job)], env=env,
                            capture_output=True, text=True, timeout=180)
    (local["tmp"] / "native.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, (result.stdout + result.stderr)[-14000:]
    inspection = """import json, sys
from pathlib import Path
from zenml.client import Client
job = Path(sys.argv[1])
native = json.loads((job / 'native.json').read_text())
run = Client().get_pipeline_run(native['backend_run_id'], include_full_metadata=True)
assert str(run.status) == 'completed', str(run.status)
outputs = {port: versions[-1].load() for step in run.steps.values() for port, versions in step.outputs.items() if port in {'candidates','judgments'}}
(job / 'verified-outputs.json').write_text(json.dumps(outputs))
"""
    inspection_path = local["tmp"] / "inspect_native.py"
    inspection_path.write_text(inspection)
    inspected = subprocess.run([sys.executable, str(inspection_path), str(job)], env=env, capture_output=True, text=True, timeout=60)
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    actual = json.loads((job / "verified-outputs.json").read_text())
    assert len(actual["candidates"]["rows"]) == 4
    assert actual["judgments"]["candidate_digest"] == actual["candidates"]["digest"]
    assert len(actual["judgments"]["predictions"]) == 4
    assert actual["candidates"]["provenance"]["splits"]["prompts"]["version_id"] == record["version_id"]
    assert actual["candidates"]["provenance"]["runtime"]["runtime_source"]
    assert actual["candidates"]["provenance"]["runtime"]["source"] == code


def test_general_judge_and_review_accept_legacy_candidates(local):
    from modelbuilder.backend.components import local_inference as legacy
    parameters = {key: deepcopy(local["parameters"][key]) for key in
        ("model", "seed", "text_field", "prompt_prefix", "max_new_tokens", "samples_per_prompt", "temperature", "top_k")}
    parameters["model"]["adapter"] = "gpt2-causal-lm"
    if local["families"][0] != "gpt2":
        # General support does not silently change the legacy architecture contract.
        with pytest.raises(ValueError, match="architecture does not match"):
            legacy.generate(inputs=local["inputs"], parameters=parameters, context=local["context"])
        return
    candidates = legacy.generate(inputs=local["inputs"], parameters=parameters, context=local["context"])["candidates"]
    judgments = adapters.judge(inputs={"candidates": candidates}, parameters=local["judge"], context=local["context"])["judgments"]
    results = {"candidates": candidates, "judgments": judgments}
    output = adapters.prepare_dataset(inputs=results, parameters=_review(results), context=local["context"])["prepared"]
    assert (output / "dataset.manifest.json").is_file()


def test_preparation_budget_and_prediction_integrity_fail_before_writes(local):
    results = _results(local)
    parameters = _review(results)
    parameters["limits"]["max_artifact_bytes"] = 100
    with pytest.raises(ValueError, match="byte limit"):
        adapters.prepare_dataset(inputs=results, parameters=parameters, context=local["context"])
    assert not Path(local["context"]["output_dir"]).exists()
    parameters = _review(results)
    results["judgments"]["predictions"][0]["prediction_kind"] = "ground_truth"
    results["judgments"] = _seal({key: value for key, value in results["judgments"].items() if key != "digest"})
    parameters["judgment_digest"] = results["judgments"]["digest"]
    with pytest.raises(ValueError, match="explicit model predictions"):
        adapters.prepare_dataset(inputs=results, parameters=parameters, context=local["context"])
    assert not Path(local["context"]["output_dir"]).exists()
