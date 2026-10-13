"""Host-independent MLX contracts. No Metal, MLX wheels, or native training required."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, ValidationError

from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.admission import BackendError, validate_recipe_settings
from modelbuilder.backend.components import mlx, mlx_bridge, mlx_components
from modelbuilder.backend.operator_config import load_operator_config
from modelbuilder.backend import placement
from modelbuilder.recipe import default_recipe

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import bootstrap  # noqa: E402


def test_mlx_profile_is_exact_hash_locked_and_separate_from_torch():
    profile = ROOT / "requirements/profiles/mlx-darwin-arm64.lock"
    data = profile.read_bytes()
    assert hashlib.sha256(data).hexdigest() == mlx.PROFILE_SHA256
    assert data == Path(mlx.__file__).with_name(profile.name).read_bytes()
    pins = dict(re.findall(r"^([\w.-]+)==([^\s]+)", data.decode(), flags=re.M))
    assert len(pins) == 34
    assert {name: pins[name] for name in mlx_components.PINS} == mlx_components.PINS
    assert "torch" not in pins and "zenml" not in pins
    for entry in re.split(r"(?m)(?=^[\w.-]+==)", data.decode())[1:]:
        assert re.search(r"--hash=sha256:[0-9a-f]{64}", entry)
    for target in ["linux-x64", "linux-arm64", "darwin-arm64"]:
        for component in ["service", "backend"]:
            assert not re.search(
                r"^mlx(?:[-=])", (ROOT / f"requirements/profiles/{component}-{target}.lock").read_text(), re.M
            )


def test_mlx_bootstrap_plan_does_not_install_and_needs_backend(monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "run", lambda *a, **k: pytest.fail("No installation allowed"))
    monkeypatch.setattr(bootstrap, "detect_target", lambda: "linux-x64")
    with pytest.raises(ValueError, match="requires --backend"):
        bootstrap.main(["--mlx", "--plan"])
    assert bootstrap.main(["--mlx", "--backend", "--plan"]) == 0
    assert json.loads(capsys.readouterr().out)["mlx_available"] is False
    with pytest.raises(ValueError, match="Apple Silicon"):
        bootstrap.main(["--mlx", "--backend", "--skip-model-download", "--skip-frontend"])


def test_mlx_bootstrap_rejects_shared_environment_before_install(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "run", lambda *a, **k: pytest.fail("No installation allowed"))
    with pytest.raises(ValueError, match="separate environment"):
        bootstrap.main(["--mlx", "--backend", "--mlx-venv", str(tmp_path), "--backend-venv", str(tmp_path)])


def test_operator_mlx_interpreter_preserves_venv_symlink(tmp_path):
    executable = tmp_path / "mlx/bin/python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    config = tmp_path / "operator.json"
    config.write_text(json.dumps({"mlx_python_executable": str(executable)}))
    assert load_operator_config(config)["mlx_python_executable"] == str(executable)
    backend = ZenMLBackend(tmp_path / "state", mlx_python_executable=str(executable))
    assert backend.mlx_python == str(executable)
    assert backend._env()["MODELBUILDER_MLX_PYTHON"] == str(executable)


@pytest.mark.parametrize("device", [None, "cpu", "mps"])
def test_real_plan_rejects_mlx_component_without_explicit_mlx_placement(tmp_path, monkeypatch, device):
    monkeypatch.delenv("MODELBUILDER_MLX_PYTHON", raising=False)
    backend = ZenMLBackend(
        tmp_path, python_executable=sys.executable, source_root=ROOT, trusted_code_roots=[str(ROOT)]
    )
    entry = mlx.catalog_entries()[0]
    recipe = default_recipe("mlx-project", "mlx-recipe", "Unconfigured MLX")
    recipe["nodes"] = [
        {"id": "train", "kind": "component.run", "inputs": [], "outputs": [], "component": entry}
    ]
    recipe["edges"] = []
    if device is not None:
        recipe["environment"]["device"] = device
    if device == "mps":
        # Explicit mps itself has its own earlier immutable-profile guard.
        expected = "MPS requires"
    else:
        expected = "MLX components require explicit"
    result = backend.invoke(
        "run.plan",
        {
            "project_id": "mlx-project",
            "recipe": recipe,
            "target": "local-" + (device or "cpu"),
            "trusted": True,
        },
    )
    assert not result["executable"]
    assert expected in result["blocked_reasons"][0]["message"], result
    assert not (backend.root / "jobs").exists()


@pytest.mark.parametrize("device", ["mlx-cpu", "mlx-metal"])
def test_mlx_placement_rejects_missing_changed_profile_and_interpreter(tmp_path, monkeypatch, device):
    monkeypatch.delenv("MODELBUILDER_MLX_PYTHON", raising=False)
    backend = ZenMLBackend(tmp_path / "state", python_executable=sys.executable, source_root=ROOT)
    payload = {"recipe": {"environment": {"device": device}, "nodes": []}, "target": "local-" + device}
    with pytest.raises(BackendError, match="reviewed separate"):
        placement.validate_placement(backend, payload)
    profile = {"path": "requirements/profiles/mlx-darwin-arm64.lock", "sha256": mlx.PROFILE_SHA256}
    payload["recipe"]["environment"]["mlx_profile"] = profile
    with pytest.raises(BackendError, match="interpreter is unavailable"):
        placement.validate_placement(backend, payload)
    payload["recipe"]["environment"]["mlx_profile"] = {**profile, "path": "../outside.lock"}
    with pytest.raises(BackendError, match="missing or changed"):
        placement.validate_placement(backend, payload)
    payload["recipe"]["environment"]["mlx_profile"] = profile
    payload["resume_mode"] = "exact_state"
    with pytest.raises(BackendError, match="exact-state"):
        placement.validate_placement(backend, payload)


def test_mlx_native_negative_probe_does_not_import_mlx_in_service():
    if sys.platform == "darwin":
        pytest.skip("Negative availability observation uses the non-Mac service interpreter")
    before = set(sys.modules)
    result = mlx.probe(sys.executable, "mlx-metal", allocate=True)
    assert result["available"] is False
    assert not any(name == "mlx" or name.startswith("mlx.") for name in set(sys.modules) - before)


@pytest.mark.parametrize(
    "response",
    [
        [],
        None,
        {},
        {"available": True, "device": "mlx-cpu"},
        {"available": True, "device": "mlx-metal", "allocation_verified": True},
    ],
)
def test_mlx_probe_rejects_malformed_or_unverified_allocation(monkeypatch, response):
    def probe(command, **kwargs):
        assert command[1:3] == ["-I", "-B"]
        assert not any(name.startswith("PYTHON") for name in kwargs["env"])
        return SimpleNamespace(returncode=0, stdout=json.dumps(response))

    monkeypatch.setattr(mlx.subprocess, "run", probe)
    monkeypatch.setenv("PYTHONHOME", "/untrusted")
    monkeypatch.setenv("PYTHONPATH", "/untrusted")
    assert mlx.probe(sys.executable, "mlx-cpu", allocate=True)["available"] is False


@pytest.mark.parametrize("device", ["mlx-cpu", "mlx-metal"])
def test_mlx_keeps_sequential_default_and_rejects_parallel(device):
    assert validate_recipe_settings({"environment": {"device": device}}) == 1
    with pytest.raises(BackendError, match="Linux CPU"):
        validate_recipe_settings({"environment": {"device": device}, "execution": {"max_parallel_stages": 2}})
    if sys.platform == "linux":
        assert validate_recipe_settings({"execution": {"max_parallel_stages": 2}}) == 2


def test_mlx_architecture_identity_excludes_data_optimizer_budget_and_placement():
    parameters = {
        "assets": [{"path": "config.json", "sha256": "a" * 64}],
        "mode": "lora",
        "num_layers": 1,
        "lora": {"rank": 2},
        "updates": 2,
        "device": "mlx-metal",
    }
    other = {**parameters, "updates": 8, "device": "mlx-cpu", "learning_rate": 0.2, "model_dependency": "new"}
    assert mlx.architecture_identity(parameters) == mlx.architecture_identity(other)
    assert mlx.architecture_identity(parameters) != mlx.architecture_identity({**other, "mode": "full"})


def test_mlx_advertised_asset_budget_rejects_before_staging(tmp_path):
    schema = mlx.LIMITS
    assert schema["properties"]["max_asset_bytes"]["maximum"] == mlx_bridge.MAX_ASSET_BYTES
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(
            {"max_asset_bytes": mlx_bridge.MAX_ASSET_BYTES + 1, "max_parameters": 10, "max_rows": 10}
        )
    with pytest.raises(ValueError, match="512 MiB"):
        mlx_bridge.stage_model("model", [], {}, tmp_path, mlx_bridge.MAX_ASSET_BYTES + 1)
    assert list(tmp_path.iterdir()) == []


def parity_checkpoint(tmp_path, vocab=32000):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"vocab_size": vocab}))
    (tmp_path / "checkpoint.manifest.json").write_text(
        json.dumps(
            {
                "mode": "full",
                "resume_modes": ["warm_start"],
                "assets": [
                    {"path": "config.json", "sha256": hashlib.sha256(config.read_bytes()).hexdigest()}
                ],
            }
        )
    )
    return {"trained": tmp_path}


def test_export_parity_output_is_bounded_before_native_import_or_model_loading(tmp_path, monkeypatch):
    inputs = parity_checkpoint(tmp_path)
    monkeypatch.setattr(
        mlx_components, "loaded_checkpoint", lambda *a, **k: pytest.fail("Must not load model")
    )
    with pytest.raises(ValueError, match="before model loading"):
        mlx_components.export(
            inputs=inputs, parameters={"parity_tokens": [[0] * 4096 for _ in range(32)]}, context={}
        )
    config, tokens = mlx_components.parity_config(inputs, {"parity_tokens": [[1, 2, 3]]}, {})
    assert config["vocab_size"] == 32000 and tokens == [[1, 2, 3]]


def test_export_parity_result_is_size_bounded_before_json_decode(tmp_path):
    result = tmp_path / "parity.json"
    with result.open("wb") as stream:
        stream.truncate(mlx_components.MAX_PARITY_RESULT_BYTES + 1)
    with pytest.raises(ValueError, match="32 MiB"):
        mlx_components.read_parity_result(result)


def test_stalled_worker_request_is_deadline_bounded_without_mlx(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("POSIX worker supervision contract")
    wrapper = tmp_path / "stalled-python"
    wrapper.write_text("#!" + sys.executable + "\nimport time\ntime.sleep(120)\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("MODELBUILDER_MLX_PYTHON", str(wrapper))
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="deadline"):
        mlx_bridge.evaluate(
            inputs={"data": {"padding": "x" * (1024 * 1024)}},
            parameters={"device": "mlx-cpu"},
            context={"output_dir": str(tmp_path / "output"), "timeout_seconds": 0.2},
        )
    assert time.monotonic() - started < 5


def test_evaluation_evidence_preserves_live_cpu_device_and_full_worker_profile(monkeypatch):
    import modelbuilder.backend.components as components

    lineage = {
        "dataset": {"id": "dataset", "revision": "1", "content_digest": "data-hash", "benchmark": "held-out"},
        "split": {"id": "test", "role": "test", "sample_ids": ["held-out"]},
    }
    execution = {
        "framework": "mlx",
        "device": "mlx-cpu",
        "profile_sha256": mlx.PROFILE_SHA256,
        "packages": {"mlx": "0.32.3", "numpy": "2.4.4"},
        "versions": {"mlx": "0.32.3"},
    }
    monkeypatch.setattr(mlx, "registered_tokens", lambda *a, **k: {"lineage": lineage})
    monkeypatch.setattr(components, "read_checkpoint", lambda path: {"identities": {"model": "model"}})

    def evaluate(**kwargs):
        assert kwargs["parameters"]["device"] == "mlx-cpu"
        return {
            "metrics": {
                "cross_entropy": 0.5,
                "execution": deepcopy(execution),
                "predictions": [{"item_id": "held-out", "negative_log_likelihood_sum": 1, "token_count": 2}],
            }
        }

    monkeypatch.setattr(mlx.bridge, "evaluate", evaluate)
    result = mlx.evaluate(
        inputs={"trained": "inert", "data": {}},
        parameters={"max_rows": 10, "max_length": 8},
        context={"device": "cpu", "source": {"commit": "test"}},
    )
    record = result["metrics"]["evaluation_records"]["cross_entropy"]
    assert record["provenance"]["execution"] == execution
    assert record["provenance"]["packages"] == execution["packages"]


@pytest.mark.parametrize(
    "key",
    [
        "tokenizer_file",
        "vocab_file",
        "merges_file",
        "spm_file",
        "gguf_file",
        "fast_tokenizer_files",
        "vocab_files_names",
    ],
)
def test_tokenizer_file_overrides_fail_before_native_model_loading(tmp_path, monkeypatch, key):
    config = tmp_path / "tokenizer_config.json"
    config.write_text(json.dumps({key: "/unmanifested/tokenizer.json"}))
    assets = mlx_components.manifest_files(tmp_path)
    monkeypatch.setattr(
        mlx_components, "runtime", lambda *a, **k: pytest.fail("Must reject before native import")
    )
    with pytest.raises(ValueError, match="Tokenizer file overrides"):
        mlx_components.load_local(tmp_path, assets, "mlx-cpu")


def test_standard_verified_tokenizer_metadata_and_shard_index_are_allowed(tmp_path):
    (tmp_path / "tokenizer_config.json").write_text(
        json.dumps(
            {
                "backend": "tokenizers",
                "bos_token": "<bos>",
                "eos_token": "<eos>",
                "model_max_length": 128,
                "tokenizer_class": "TokenizersBackend",
                "pad_token": "<pad>",
                "unk_token": "<unk>",
                "tokenizer_file": None,
            }
        )
    )
    (tmp_path / "model-00001.safetensors").write_bytes(b"inert fixture")
    index = tmp_path / "model.safetensors.index.json"
    index.write_text(json.dumps({"weight_map": {"weight": "model-00001.safetensors"}}))
    mlx_components.validate_model_references(tmp_path)
    index.write_text(json.dumps({"weight_map": {"weight": "../outside.safetensors"}}))
    with pytest.raises(ValueError, match="escapes verified"):
        mlx_components.validate_model_references(tmp_path)


def test_exported_standalone_loader_rejects_tokenizer_override_without_optional_runtime(tmp_path):
    import subprocess

    loader = tmp_path / "loader.py"
    loader.write_text(mlx_components.export_loader_source())
    (tmp_path / "config.json").write_text(json.dumps({"vocab_size": 16}))
    (tmp_path / "tokenizer_config.json").write_text(
        json.dumps({"tokenizer_file": "/unmanifested/tokens.json"})
    )
    (tmp_path / "parity-input.json").write_text(json.dumps({"tokens": [[2, 3]], "device": "mlx-cpu"}))
    result = subprocess.run(
        [sys.executable, "-I", str(loader), str(tmp_path)], text=True, capture_output=True, timeout=5
    )
    assert result.returncode != 0 and "Tokenizer file overrides" in result.stderr
    assert not (tmp_path / "parity-actual.json").exists()


@pytest.mark.parametrize(
    "tokens,offset,count", [([2, 3], 1, 1), ([2, 4, 5, 6, 3], 4, 1), ([2, 4, 5, 6, 3], 0, 4)]
)
def test_single_supervised_target_is_valid_and_counted(tokens, offset, count):
    rows = mlx_components.dataset(
        {"samples": [{"id": "sample", "tokens": tokens, "prompt_offset": offset}]}, 16, 32
    )
    assert rows == [(tokens, offset)]
    assert len(rows[0][0]) - max(1, rows[0][1]) == count
    # Official loss's inclusive next-token positions exclude prompt and padding.
    assert sum(position >= offset for position in range(1, len(tokens))) == count


def test_source_pin_refreshes_declared_mlx_profile_hash(tmp_path):
    import subprocess
    from modelbuilder.recipe import pin_source_files

    lock = tmp_path / "mlx.lock"
    lock.write_text("# inert lock fixture\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "mlx.lock"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "Inert source fixture",
        ],
        check=True,
    )
    commit = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    recipe = default_recipe("project", "recipe")
    recipe["environment"]["mlx_profile"] = {"path": "mlx.lock", "sha256": "0" * 64}
    pinned = pin_source_files(recipe, tmp_path, ["mlx.lock"], commit)
    assert pinned["environment"]["mlx_profile"]["sha256"] == hashlib.sha256(lock.read_bytes()).hexdigest()


@pytest.mark.parametrize("prompt", [[16], [2**63], [True], [-1]])
def test_generation_rejects_invalid_token_before_native_import(tmp_path, monkeypatch, prompt):
    inputs = parity_checkpoint(tmp_path, vocab=16)
    monkeypatch.setattr(
        mlx_components, "loaded_checkpoint", lambda *a, **k: pytest.fail("Must not load model")
    )
    with pytest.raises(ValueError, match="within the verified model vocabulary"):
        mlx_components.generate(inputs=inputs, parameters={"prompt": prompt, "max_tokens": 1}, context={})


def test_native_deadline_fixture_preserves_probe_and_protocol_routing_without_mlx(tmp_path):
    import importlib.util
    import subprocess

    path = ROOT / "tests/integration/test_mlx_service.py"
    spec = importlib.util.spec_from_file_location("mlx_deadline_fixture_contract", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    wrapper = fixture.deadline_worker(sys.executable, tmp_path / "python-wrapper", tmp_path / "marker")
    assert mlx.probe(str(wrapper), "mlx-metal")["available"] is False
    result = subprocess.run(
        [str(wrapper), "-I", str(Path(mlx.__file__).with_name("mlx_worker.py"))],
        input=json.dumps({"protocol": "invalid"}),
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 1 and json.loads(result.stdout)["message"] == "Unsupported MLX IPC request"


def test_native_deadline_fixture_generates_a_valid_synchronized_export_pause(tmp_path):
    import importlib.util
    import subprocess

    path = ROOT / "tests/integration/test_mlx_service.py"
    spec = importlib.util.spec_from_file_location("mlx_deadline_codegen_contract", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    marker = tmp_path / "marker.json"
    wrapper = fixture.deadline_worker(sys.executable, tmp_path / "python-wrapper", marker)
    worker = tmp_path / "mlx_worker.py"
    worker.write_text(
        "import importlib.util,json\n"
        + "spec=importlib.util.spec_from_file_location('fixture_components',"
        + repr(mlx_components.__file__)
        + ")\n"
        + "components=importlib.util.module_from_spec(spec);spec.loader.exec_module(components)\n"
        + "def emit(value): print(json.dumps(value))\n"
        + "def main():\n    code=components.export_loader_source()\n    compile(code,'loader.py','exec')\n    emit({'code':code})\n    return 0\n"
    )
    result = subprocess.run([str(wrapper), "-I", str(worker)], text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    loader = json.loads(result.stdout)["code"]
    assert str(marker) in loader and "native_logits_completed" in loader and "time.sleep(600)" in loader
    assert loader.index("result=json.dumps(predict(") < loader.index("time.sleep(600)")


def test_mlx_rights_union_is_inert_and_preserves_all_actual_parents(tmp_path):
    from modelbuilder.backend.components import identity
    from modelbuilder.backend.components.mlx_bridge import training_rights
    from tests.components.rights_test_support import local_review
    first, base = local_review(policy_id="local-base")
    second, data = local_review(policy_id="local-data", commit="2" * 40)
    _, parent_rights = local_review(policy_id="local-resume", commit="3" * 40)
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    asset = checkpoint / "weights.safetensors"
    asset.write_bytes(b"test-authored inert checkpoint asset")
    descriptor = {"schema_version": "modelbuilder.checkpoint/v1", "purpose": "last", "step": 1,
                  "identities": {key: key for key in ("architecture", "source", "dependencies", "training", "environment")},
                  "resume_modes": ["warm_start"], "assets": [{"path": asset.name, "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()}],
                  "rights": parent_rights}
    (checkpoint / "checkpoint.manifest.json").write_text(json.dumps(descriptor))
    result = training_rights({"train": {"rights": data}}, {"declared_artifact_rights": base, "resume": {"path": str(checkpoint)}})
    assert {entry["license_policy_id"] for entry in result["sources"]} == {"local-base", "local-data", "local-resume"}
    assert {identity(base), identity(data), identity(parent_rights)} <= set(result["parent_rights_digests"])
    assert not result["unresolved"]
    missing = training_rights({"train": {}}, {})
    assert len(missing["unresolved"]) == 2 and not missing["sources"]
    asset.write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity mismatch"):
        training_rights({"train": {"rights": data}}, {"declared_artifact_rights": base, "resume": {"path": str(checkpoint)}})
