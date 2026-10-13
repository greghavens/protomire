"""Real offline Trainer steps on independently authored user models and registry data."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from modelbuilder.backend.components import component_catalog, identity, read_checkpoint, read_export, validate_component
from modelbuilder.backend.components import transformers_training as adapters
from modelbuilder.backend.components import transformers_loader
from modelbuilder.dataset import DatasetRegistry
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService


def dependency(name):
    if os.environ.get("MODELBUILDER_REQUIRE_TRAINER") == "1":
        return importlib.import_module(name)
    return pytest.importorskip(name)


def test_catalog_inert_and_typed():
    entries = adapters.catalog_entries()
    assert {entry["id"] for entry in entries} <= {entry["id"] for entry in component_catalog()}
    assert entries[0]["capabilities"]["resume_modes"] == ["warm_start"]
    assert entries[0]["capabilities"]["checkpoint_events"]
    assert all("implementation_ref" not in entry for entry in entries)
    result = subprocess.run([sys.executable, "-c", "from modelbuilder.backend.components import component_catalog; import sys; component_catalog(); assert 'torch' not in sys.modules; assert 'transformers' not in sys.modules; assert 'accelerate' not in sys.modules"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def local(tmp_path):
    torch = dependency("torch")
    dependency("transformers")
    dependency("accelerate")
    from transformers import BertConfig, BertForSequenceClassification
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from safetensors.torch import save_model
    torch.set_num_threads(1)
    assets = tmp_path / "assets"
    assets.mkdir()
    tokenizer = Tokenizer(WordLevel({word: i for i, word in enumerate(
        ["[UNK]", "[PAD]", "sun", "rain", "warm", "cold", "sky", "cloud", "dark", "light"])}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.save(str(assets / "tokenizer.json"))
    torch.manual_seed(10)
    model = BertForSequenceClassification(BertConfig(vocab_size=10, hidden_size=8, num_hidden_layers=1,
        num_attention_heads=2, intermediate_size=12, max_position_embeddings=16, num_labels=2, pad_token_id=1,
        hidden_dropout_prob=0, attention_probs_dropout_prob=0))
    model.config.to_json_file(assets / "config.json", use_diff=False)
    save_model(model, str(assets / "weights.safetensors"))
    paths = {key: str(assets / file) for key, file in [("config", "config.json"), ("weights", "weights.safetensors"), ("tokenizer", "tokenizer.json")]}
    refs = {key: {"dependency_id": key, "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()} for key, path in paths.items()}
    settings = {"task": "sequence-classification", "model": refs, "text_field": "text", "label_field": "label",
        "labels": ["clear", "wet"], "max_length": 8, "truncate": False, "pad_token_id": 1,
        "limits": {"max_parameters": 100000, "max_asset_bytes": 1048576, "max_rows": 100, "max_tokenized_tokens": 1000}}
    parameters = {"architecture": settings, "seed": 123, "max_steps": 3, "batch_size": 2,
        "gradient_accumulation_steps": 1, "learning_rate": .005, "weight_decay": .01, "warmup_steps": 0,
        "lr_scheduler_type": "constant", "max_grad_norm": 1., "checkpoint_interval_steps": 1}
    service = OperationService(tmp_path / "service.sqlite")
    actor = Actor("owner", {"read", "modify", "execute"})
    project = service.invoke("project.create", {"slug": "trainer", "model_name": "User model", "idempotency_key": "p"}, actor)
    data_root = tmp_path / "data"
    data_root.mkdir()
    rows = [{"id": str(i), "text": text, "label": i % 2} for i, text in enumerate(
        ["sun warm", "rain cold", "sky light", "cloud dark", "sun sky", "rain cloud", "cold dark"])]
    data = data_root / "rows.jsonl"
    data.write_text("".join(json.dumps(row) + "\n" for row in rows))
    manifest = {"name": "User weather", "benchmark": "independent-weather-v1", "license": "CC0-1.0",
        "provenance": {"source": "independently authored"}, "sources": [{"id": "data", "root_id": "data",
            "path": data.name, "format": "jsonl", "sha256": hashlib.sha256(data.read_bytes()).hexdigest()}],
        "id_column": "id", "row_schema": {"type": "object", "required": ["id", "text", "label"],
            "additionalProperties": False, "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "label": {"type": "integer"}}},
        "splits": [{"id": name, "role": role, "sample_ids": ids} for name, role, ids in
            [("train", "train", ["0", "1", "2", "3"]), ("dev", "validation", ["4", "5"]), ("test", "test", ["6"])]]}
    registry = DatasetRegistry(service.store, project_roots={project["project_id"]: {"data": str(data_root)}})
    record = registry.register(project["project_id"], manifest)
    splits = {name: registry.read_split(project["project_id"], record["dataset_id"], 1, split, purpose=purpose)
              for name, split, purpose in [("train", "train", "training"), ("data", "dev", "evaluation")]}
    source = tmp_path / "source"
    source.mkdir()
    (source / "adapter.py").write_text("from modelbuilder.backend.components.transformers_training import train, evaluate, export\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "User-owned adapter"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    source_identity = {"commit": commit, "files": [{"path": "adapter.py", "sha256": hashlib.sha256((source / "adapter.py").read_bytes()).hexdigest()}]}
    context = {"dependencies": paths, "source": source_identity, "device": "cpu",
        "identities": {key: key for key in ["architecture", "source", "dependencies", "training", "environment", "data"]},
        "output_dir": str(tmp_path / "trained")}
    from tests.components.rights_test_support import local_review
    inventory = [{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "size_bytes": path.stat().st_size} for path in [*(Path(value) for value in paths.values()), data]]
    policy, rights = local_review(project["project_id"], commit=identity(inventory), source_files=inventory,
                                 repo_id="urn:modelbuilder:test-authored-tiny-transformers")
    context["declared_artifact_rights"] = rights
    splits = {key: {**value, "rights": rights} for key, value in splits.items()}
    try:
        yield {"rights": rights, "policy": policy, "tmp": tmp_path, "context": context, "parameters": parameters, "inputs": splits,
               "model": model, "source": source, "registry": registry, "record": record, "project": project, "service": service}
    finally:
        service.store.close()


def do_train(local, **kwargs):
    return adapters.train(inputs={"train": local["inputs"]["train"]}, parameters=local["parameters"],
        context={**local["context"], **kwargs})["trained"]


def test_actual_trainer_weights_metrics_and_fresh_export(local, monkeypatch):
    import torch
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", lambda *args, **kw: pytest.fail("Offline adapter contacted Hub"))
    rng = torch.get_rng_state().clone()
    trained = do_train(local)
    assert torch.equal(torch.get_rng_state(), rng)
    checkpoint = read_checkpoint(trained)
    assert checkpoint["step"] == 3 and checkpoint["state"] == ["model"]
    assert checkpoint["resume_modes"] == ["warm_start"]
    assert checkpoint["rights"]["sources"] == local["rights"]["sources"]
    assert checkpoint["rights"]["parent_rights_digests"] and not checkpoint["rights"]["unresolved"]
    model, _, _ = transformers_loader.load(trained)
    assert any(not torch.equal(value, model.state_dict()[key]) for key, value in local["model"].state_dict().items())
    evaluated = adapters.evaluate(inputs={"trained": trained, "data": local["inputs"]["data"]},
        parameters={"batch_size": 2, "metrics": ["accuracy", "cross_entropy"]},
        context={**local["context"], "output_dir": str(local["tmp"] / "evaluation")})["metrics"]
    assert 0 <= evaluated["accuracy"] <= 1 and evaluated["cross_entropy"] > 0
    from modelbuilder.evaluation import validate_evaluation_record
    for record in evaluated["evaluation_records"].values():
        assert not validate_evaluation_record(record)
        assert record["split"]["sample_ids"] == ["4", "5"]
        assert record["treatment"]["checkpoint"] == identity(checkpoint)
    exported = adapters.export(inputs={"trained": trained}, parameters={"parity_texts": ["rain cold", "sun"],
        "atol": 1e-7, "verification_timeout_seconds": 60},
        context={**local["context"], "output_dir": str(local["tmp"] / "exported")})["exported"]
    manifest = read_export(exported)
    assert manifest["verification"]["fresh_process"] and manifest["verification"]["max_abs_difference"] == 0
    assert manifest["selected_checkpoint"] == identity(checkpoint)
    assert "modelbuilder" not in (exported / "loader.py").read_text().lower().replace("modelbuilder dependency", "")
    assert not list(exported.glob("*.pt"))


@pytest.mark.parametrize("architecture", ["distilbert", "gpt2", "llama"])
def test_other_installed_architectures_and_causal_metrics(local, architecture):
    import torch
    from safetensors.torch import save_model
    from transformers import (DistilBertConfig, DistilBertForSequenceClassification, GPT2Config, GPT2LMHeadModel,
                              LlamaConfig, LlamaForCausalLM)
    configs = {
        "distilbert": (DistilBertConfig(vocab_size=10, dim=8, hidden_dim=12, n_layers=1, n_heads=2,
            max_position_embeddings=16, num_labels=2, pad_token_id=1, dropout=0, attention_dropout=0), DistilBertForSequenceClassification),
        "gpt2": (GPT2Config(vocab_size=10, n_embd=8, n_layer=1, n_head=2, n_positions=16, n_ctx=16,
            pad_token_id=1, bos_token_id=None, eos_token_id=None, resid_pdrop=0, embd_pdrop=0, attn_pdrop=0), GPT2LMHeadModel),
        "llama": (LlamaConfig(vocab_size=10, hidden_size=8, intermediate_size=12, num_hidden_layers=1,
            num_attention_heads=2, num_key_value_heads=2, max_position_embeddings=16, pad_token_id=1), LlamaForCausalLM),
    }
    config, cls = configs[architecture]
    torch.manual_seed(13)
    model = cls(config)
    assets = local["tmp"] / "assets"
    model.config.to_json_file(assets / "config.json", use_diff=False)
    save_model(model, str(assets / "weights.safetensors"))
    for key in ("config", "weights"):
        local["parameters"]["architecture"]["model"][key]["sha256"] = hashlib.sha256(Path(local["context"]["dependencies"][key]).read_bytes()).hexdigest()
    causal = architecture != "distilbert"
    local["parameters"]["architecture"].update(task="causal-lm" if causal else "sequence-classification", labels=[] if causal else ["clear", "wet"])
    trained = do_train(local)
    selected = ["cross_entropy", "perplexity"] if causal else ["accuracy"]
    metrics = adapters.evaluate(inputs={"trained": trained, "data": local["inputs"]["data"]},
        parameters={"batch_size": 2, "metrics": selected}, context={**local["context"], "output_dir": str(local["tmp"] / "evaluation")})["metrics"]
    if causal:
        import math
        assert math.isclose(metrics["perplexity"], math.exp(metrics["cross_entropy"]))
        assert metrics["evaluation_records"]["cross_entropy"]["metric"]["aggregation"] == "token_weighted_mean"
    else:
        assert 0 <= metrics["accuracy"] <= 1


def test_warm_start_cancellation_checkpoints_and_no_exact_claim(local):
    published = []
    cancel = local["tmp"] / "cancel.json"
    def ready(**event):
        published.append(event["path"])
        if event["step"] == 1:
            cancel.write_text("{}")
    with pytest.raises(InterruptedError, match="cancelled"):
        do_train(local, checkpoint_ready=ready, cancel_file=str(cancel))
    assert [read_checkpoint(path)["step"] for path in published] == [1, 2]
    parent = published[-1]
    resume = {"path": str(parent), "descriptor": read_checkpoint(parent)}
    with pytest.raises(ValueError, match="not exact"):
        do_train(local, resume=resume, resume_mode="exact_state", output_dir=str(local["tmp"] / "exact"))
    warm = do_train(local, resume=resume, resume_mode="warm_start", output_dir=str(local["tmp"] / "warm"))
    assert read_checkpoint(warm)["step"] == 3
    assert read_checkpoint(warm)["resume_modes"] == ["warm_start"]
    fresh = do_train(local, output_dir=str(local["tmp"] / "fresh"))
    assert hashlib.sha256((warm / "model.safetensors").read_bytes()).hexdigest() != hashlib.sha256((fresh / "model.safetensors").read_bytes()).hexdigest()


@pytest.mark.parametrize("case", ["wrong-role", "tampered-row", "tampered-weight", "too-many-parameters", "too-many-rows", "remote-code", "wrong-label", "oversized-text", "gpu"])
def test_fail_closed(local, case):
    settings = local["parameters"]["architecture"]
    if case == "wrong-role":
        local["inputs"]["train"] = local["inputs"]["data"]
    elif case == "tampered-row":
        local["inputs"]["train"]["rows"][0]["text"] = "replaced"
    elif case == "tampered-weight":
        Path(local["context"]["dependencies"]["weights"]).write_bytes(b"not safetensors")
    elif case == "too-many-parameters":
        settings["limits"]["max_parameters"] = 1
    elif case == "too-many-rows":
        settings["limits"]["max_rows"] = 1
    elif case == "remote-code":
        path = Path(local["context"]["dependencies"]["config"])
        config = json.loads(path.read_text())
        config["auto_map"] = {"AutoModel": "arbitrary.remote"}
        path.write_text(json.dumps(config))
        settings["model"]["config"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif case == "wrong-label":
        settings["labels"] = ["one", "two", "three"]
    elif case == "oversized-text":
        settings["max_length"] = 2
        row = local["inputs"]["train"]["rows"][0]
        row["text"] = "sun sky light warm"
        lineage = local["inputs"]["train"]["consumption"]
        lineage["sample_digests"][0] = identity({key: value for key, value in row.items() if key != "id"})
        lineage["digest"] = identity({key: value for key, value in lineage.items() if key != "digest"})
    else:
        local["context"]["device"] = "cuda:0"
    with pytest.raises((ValueError, RuntimeError)):
        do_train(local)


@pytest.mark.integration
@pytest.mark.parametrize("streaming,live", [(False, True), (True, False)])
def test_registered_data_native_zenml_train_evaluate_export(local, streaming, live):
    dependency("zenml")
    from modelbuilder.backend.common import write_json
    registry, pid, record = local["registry"], local["project"]["project_id"], local["record"]
    reference = registry.portable_reference(pid, record["dataset_id"], 1)
    dependency_paths, guards = registry.execution_sources(pid, record["dataset_id"], 1)
    reference_path = local["tmp"] / "dataset-reference.json"
    reference_path.write_text(json.dumps(reference))
    dependency_paths["dataset-reference"] = str(reference_path)
    dependency_paths.update(local["context"]["dependencies"])
    if streaming:
        streamed = stream_input(local)
        stream_reference = local["tmp"] / "stream-reference.json"
        stream_reference.write_text(json.dumps(streamed["stream"]["reference"]))
        dependency_paths["stream-reference"] = str(stream_reference)
    dependencies = [{"id": key, "kind": "model" if key in local["context"]["dependencies"] else "dataset",
                     "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                     "metadata": {"rights": local["rights"], **({"dataset_reference": json.loads(Path(path).read_text())}
                                  if key in {"dataset-reference", "stream-reference"} else {})}}
                    for key, path in dependency_paths.items() if not (streaming and key == "dataset-reference")]
    nodes, edges = [], []
    for name, split, target, port in [("training_data", "train", "train", "train"), ("evaluation_data", "dev", "evaluate", "data")]:
        nodes.append({"id": name, "kind": "dataset.version.import", "timeout": 120, "inputs": [], "outputs": [{"name": "data", "type": "dict"}],
            "parameters": {"reference_id": "stream-reference" if streaming else "dataset-reference", "split_id": split, "purpose": "training" if split == "train" else "evaluation"}})
        edges.append({"source_node": name, "source_port": "data", "target_node": target, "target_port": port})
    if live:
        edges.append({"source_node": "evaluation_data", "source_port": "data", "target_node": "train", "target_port": "validation"})
    parameter_sets = [local["parameters"], {"batch_size": 2, "metrics": ["accuracy", "cross_entropy"]},
                      {"parity_texts": ["sun sky"], "atol": 1e-7, "verification_timeout_seconds": 60}]
    for name, entry, parameters in zip(["train", "evaluate", "export"], local.get("catalog_entries", adapters.catalog_entries()), parameter_sets):
        component = {key: entry[key] for key in ("id", "version", "protocol", "role", "description", "parameter_schema", "capabilities")}
        node = {"id": name, "kind": "component.run", "component": component, "inputs": entry["inputs"], "outputs": entry["outputs"],
            "parameters": parameters, "timeout": 120, "retry": {"max_attempts": 1},
            "implementation_ref": {**local["context"]["source"]["files"][0], "module": "adapter", "entrypoint": name, "commit": local["context"]["source"]["commit"]}}
        validate_component(node)
        nodes.append(node)
    edges.extend({"source_node": "train", "source_port": "trained", "target_node": name, "target_port": "trained"} for name in ["evaluate", "export"])
    recipe = {"recipe_id": "native-trainer", "revision": 1, "source": local["context"]["source"], "nodes": nodes,
              "edges": edges, "external_dependencies": dependencies,
              "evaluation": {"live": {"enabled": live, "max_concurrency": 1, "timeout_seconds": 60}}}
    if local.get("execution"):
        recipe["execution"] = local["execution"]
    from tests.components.rights_test_support import authorize_test_recipe
    from tests.integration.native_fixture_support import synthetic_review
    source_review = synthetic_review(recipe, pid, local["source"])
    policies = {review["policy_id"]: review for review in (source_review, local["policy"])}
    authorization = authorize_test_recipe(local["tmp"], recipe, policies, project_id=pid)
    job = local["tmp"] / "job"
    write_json(job / "request.json", {**local.get("request_extensions", {}), **authorization, "recipe": recipe, "project_id": pid, "operation_id": "native-trainer", "run_name": "native-trainer",
        "source_root": str(local["source"]), "trusted_code_roots": [str(local["source"])], "dependency_paths": dependency_paths, "dataset_source_guards": guards})
    env = {**os.environ, "ZENML_CONFIG_PATH": str(local["tmp"] / "zenml"), "ZENML_LOCAL_STORES_PATH": str(local["tmp"] / "stores"),
           "ZENML_ANALYTICS_OPT_IN": "false", "ZENML_LOGGING_VERBOSITY": "ERROR", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "OMP_NUM_THREADS": "1"}
    result = subprocess.run([sys.executable, "-m", "modelbuilder.backend.worker", str(job)], env=env, capture_output=True, text=True, timeout=180)
    (local["tmp"] / "native.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, (result.stdout + result.stderr)[-16000:]
    assert (job / "native.json").is_file()
    checkpoints = list((job / "outputs/train/checkpoints").glob("*/checkpoint.manifest.json"))
    assert len(checkpoints) == 3
    manifest = read_export(job / "outputs/export")
    assert all(source in manifest["rights"]["sources"] for source in local["rights"]["sources"])
    assert not manifest["rights"]["unresolved"]
    assert manifest["verification"]["fresh_process"]
    inspect = local["tmp"] / "inspect_native.py"
    inspect.write_text("""import json,sys
from pathlib import Path
from zenml.client import Client
job=Path(sys.argv[1]); native=json.loads((job/'native.json').read_text())
run=Client().get_pipeline_run(native['backend_run_id'], include_full_metadata=True)
assert str(run.status)=='completed'
metrics=[versions[-1].load() for step in run.steps.values() for port,versions in step.outputs.items() if port=='metrics']
assert len(metrics)==1 and 'accuracy' in metrics[0]
(job/'verified-metrics.json').write_text(json.dumps(metrics[0]))
""")
    checked = subprocess.run([sys.executable, str(inspect), str(job)], env=env, capture_output=True, text=True, timeout=60)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    metrics = json.loads((job / "verified-metrics.json").read_text())
    assert metrics["evaluation_records"]["accuracy"]["split"]["sample_ids"] == ["4", "5"]
    if live:
        from modelbuilder.backend.live_evaluation import list_evaluations
        evaluations = list_evaluations(job)["evaluations"]
        assert evaluations and all(item["status"] == "completed" for item in evaluations), evaluations
    if streaming:
        descriptor = read_checkpoint(sorted(checkpoints)[-1].parent)
        assert descriptor["data"]["consumption"]["count"] == 6
        assert descriptor["data"]["iteration"] == "sequential_stream"
        assert metrics["evaluation_records"]["accuracy"]["dataset"] == descriptor["data"]["dataset"]


def stream_input(local, split="train"):
    from copy import deepcopy
    manifest = deepcopy(local["record"]["manifest"])
    manifest["access_mode"] = "streaming"
    manifest["name"] = "Streaming user weather"
    if "stream_record" not in local:
        local["stream_record"] = local["registry"].register(local["project"]["project_id"], manifest)
    registered = local["stream_record"]
    value = local["registry"].read_split(local["project"]["project_id"], registered["dataset_id"], 1, split,
        purpose="training" if split == "train" else "evaluation")
    return {**value, "rights": local["rights"]}


def test_streamed_trainer_observes_actual_batch_lineage_without_prefetch(local):
    from modelbuilder.dataset_streaming import StreamingConsumption, iter_stream_rows
    data = stream_input(local)
    assert "rows" not in data and data["split"]["sample_count"] == 4
    local["inputs"]["train"] = data
    local["parameters"]["max_steps"] = 1
    local["parameters"]["batch_size"] = 2
    local["parameters"]["gradient_accumulation_steps"] = 1
    trained = do_train(local)
    descriptor = read_checkpoint(trained)
    actual = descriptor["data"]["consumption"]
    expected = StreamingConsumption(data)
    iterator = iter_stream_rows(data)
    try:
        expected.add(next(iterator))
        expected.add(next(iterator))
    finally:
        iterator.close()
    assert actual == expected.snapshot()
    assert actual["count"] == 2  # DataLoader lookahead must not count untrained rows.
    assert str(local["tmp"]) not in json.dumps(descriptor)
    with pytest.raises(ValueError, match="Streaming evaluation"):
        adapters.evaluate(inputs={"trained": trained, "data": data}, parameters={"batch_size": 1, "metrics": ["accuracy"]},
            context={**local["context"], "output_dir": str(local["tmp"] / "unsupported-stream-evaluation")})
    evaluated = adapters.evaluate(inputs={"trained": trained, "data": stream_input(local, "dev")},
        parameters={"batch_size": 1, "metrics": ["accuracy"]},
        context={**local["context"], "output_dir": str(local["tmp"] / "bounded-stream-evaluation")})["metrics"]
    record = evaluated["evaluation_records"]["accuracy"]
    assert record["dataset"] == data["dataset"] and record["split"]["sample_ids"] == ["4", "5"]


def test_eager_actual_training_lineage_excludes_prefetch(local):
    local["parameters"]["max_steps"] = 1
    trained = do_train(local)
    assert read_checkpoint(trained)["data"]["consumption"]["count"] == 2
    assert read_checkpoint(trained)["execution"]["device"] == "cpu"


def test_explicit_placement_properties_do_not_allow_data_parallel(local):
    with adapters._device_environment("cpu"):
        args = adapters._training_arguments("cpu", output_dir=str(local["tmp"] / "args"), report_to=[])
        assert str(args.device) == "cpu" and args.n_gpu == 0
    assert adapters._same_device("cuda:1", "cuda:1")
    assert not adapters._same_device("cuda:0", "cuda:1")
    assert not adapters._same_device("cpu", "mps")
    assert adapters._same_device("mps:0", "mps")


@pytest.mark.integration
def test_required_accelerator_trainer_hardware(local):
    device = os.environ.get("MODELBUILDER_REQUIRE_TRAINER_DEVICE")
    if not device:
        pytest.skip("Set MODELBUILDER_REQUIRE_TRAINER_DEVICE=cuda:<ordinal> or mps on the real pinned target")
    assert device == "mps" or device.startswith("cuda:")
    trained = do_train(local, device=device)
    descriptor = read_checkpoint(trained)
    assert adapters._same_device(descriptor["execution"]["device"], device)
    assert descriptor["resume_modes"] == ["warm_start"]
    assert descriptor["step"] == local["parameters"]["max_steps"]
    exported = adapters.export(inputs={"trained": trained}, parameters={"parity_texts": ["sun sky"],
        "atol": 1e-7, "verification_timeout_seconds": 60},
        context={**local["context"], "output_dir": str(local["tmp"] / "accelerator-export")})["exported"]
    assert read_export(exported)["verification"]["fresh_process"]


def test_streaming_batch_resource_budget_is_enforced(local):
    local["inputs"]["train"] = stream_input(local)
    local["parameters"]["architecture"]["limits"]["max_tokenized_tokens"] = 8
    with pytest.raises(ValueError, match="lookahead window"):
        do_train(local)


def test_explicit_device_never_calls_trainer_automatic_setup(local, monkeypatch):
    from transformers import TrainingArguments
    def fail(self):
        pytest.fail("Automatic Trainer device setup could touch the wrong CUDA ordinal")
    monkeypatch.setattr(TrainingArguments, "_setup_devices", property(fail))
    with adapters._device_environment("cpu"):
        args = adapters._training_arguments("cpu", output_dir=str(local["tmp"] / "selected-only"), report_to=[])
        assert str(args.device) == "cpu"
        assert args.distributed_state.num_processes == 1
        assert args.n_gpu == 0
    local["parameters"]["max_steps"] = 1
    assert read_checkpoint(do_train(local))["step"] == 1


def test_streamed_heldout_limits_fail_before_source_resolution(local, monkeypatch):
    from modelbuilder import dataset_streaming
    data = stream_input(local, "dev")
    settings = local["parameters"]["architecture"]
    settings["limits"]["max_rows"] = 1
    monkeypatch.setattr(dataset_streaming, "_resolve_stream", lambda *a, **kw: pytest.fail("Oversized held-out split must fail before source reads"))
    with pytest.raises(ServiceError) as error:
        adapters._bounded_evaluation_data(data, settings)
    assert error.value.code == "dataset_limit_exceeded"


def test_warm_start_refuses_unmanifested_weight_file(local):
    local["parameters"]["max_steps"] = 1
    trained = do_train(local)
    descriptor = read_checkpoint(trained)
    descriptor["assets"] = [asset for asset in descriptor["assets"] if asset["path"] != "model.safetensors"]
    (trained / "checkpoint.manifest.json").write_text(json.dumps(descriptor))
    with pytest.raises(ValueError, match="hash-verified Transformers assets"):
        do_train(local, output_dir=str(local["tmp"] / "unsafe-resume"),
            resume={"path": str(trained), "descriptor": descriptor}, resume_mode="warm_start")
