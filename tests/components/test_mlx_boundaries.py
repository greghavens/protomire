"""Native isolated-worker regressions; standalone results are not pipeline acceptance."""

import hashlib
import json
import os
import subprocess
import sys
import time

import pytest
from modelbuilder.backend.components import identity, read_checkpoint, read_export
from modelbuilder.backend.components import mlx, mlx_bridge
from mlx_fixture import create_model

pytestmark = pytest.mark.integration


@pytest.fixture
def configured(tmp_path, monkeypatch):
    if os.environ.get("MODELBUILDER_REQUIRE_MLX") != "1":
        pytest.skip("Required-hardware isolated MLX worker gate")
    python = os.environ["MODELBUILDER_MLX_PYTHON"]
    assert mlx.probe(python, "mlx-metal", allocate=True)["available"]
    model = create_model(python, tmp_path / "base", shards=True)
    assets = [
        {"path": str(path.relative_to(model)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in sorted(model.iterdir())
        if path.is_file()
    ]
    entries = [{**asset, "dependency_id": "asset-" + str(i)} for i, asset in enumerate(assets)]
    manifest = tmp_path / "model.json"
    manifest.write_text(json.dumps({"files": entries}))
    paths = {
        "model": str(manifest),
        **{entry["dependency_id"]: str(model / entry["path"]) for entry in entries},
    }
    params = {
        "device": "mlx-metal",
        "mode": "full",
        "model_dependency": "model",
        "assets": assets,
        "updates": 1,
        "accumulation": 1,
        "batch_size": 1,
        "max_length": 32,
        "seed": 7,
        "learning_rate": 0.01,
        "num_layers": 1,
        "lora": {"rank": 2, "scale": 2.0, "dropout": 0.0},
        "limits": {"max_parameters": 100000, "max_asset_bytes": 1000000, "max_rows": 10},
    }
    data = {
        "identity": "standalone-token-fixture",
        "samples": [{"id": "a", "tokens": [2, 4, 5, 3], "prompt_offset": 0}],
    }
    ctx = {
        "identities": {
            key: key + "-fixture"
            for key in ["architecture", "source", "dependencies", "training", "environment", "data"]
        },
        "dependencies": paths,
        "output_dir": str(tmp_path / "train"),
    }
    monkeypatch.setenv("MODELBUILDER_MLX_PYTHON", python)
    return {"tmp": tmp_path, "python": python, "params": params, "data": data, "ctx": ctx, "model": model}


def test_hostile_python_environment_cannot_shadow_worker(configured, monkeypatch):
    local = configured
    evil = local["tmp"] / "hostile"
    evil.mkdir()
    marker = evil / "executed"
    (evil / "mlx.py").write_text(
        "from pathlib import Path\nPath(" + repr(str(marker)) + ').touch()\nraise RuntimeError("shadow")\n'
    )
    monkeypatch.setenv("PYTHONPATH", str(evil))
    monkeypatch.setenv("PYTHONHOME", str(evil))
    trained = mlx_bridge.train(
        inputs={"train": local["data"]}, parameters=local["params"], context=local["ctx"]
    )["trained"]
    assert read_checkpoint(trained)["execution"]["device"] == "mlx-metal"
    assert not marker.exists()


def test_full_checkpoint_is_self_contained_and_sharded_warm_start(configured):
    local = configured
    trained = mlx_bridge.train(
        inputs={"train": local["data"]}, parameters=local["params"], context=local["ctx"]
    )["trained"]
    # Force tiny shards with the official writer, then pin the resulting authored fixture.
    code = r"""import json,sys
from pathlib import Path
from functools import partial
import mlx.core as mx
from mlx_lm import load,utils
root=Path(sys.argv[1]);model,_=load(str(root),trust_remote_code=False);mx.eval(model.parameters())
for file in root.glob('*.safetensors'):file.unlink()
(root/'model.safetensors.index.json').unlink()
utils.make_shards=partial(utils.make_shards,max_file_size_gb=0);utils.save_model(root,model,donate_model=False)
import hashlib
record=json.loads((root/'checkpoint.manifest.json').read_text())
record['assets']=[{'path':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(root.iterdir()) if p.is_file() and p.name!='checkpoint.manifest.json']
(root/'checkpoint.manifest.json').write_text(json.dumps(record))
"""
    subprocess.run(
        [local["python"], "-I", "-c", code, str(trained)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert len(list(trained.glob("model-*.safetensors"))) > 1
    context = {
        **local["ctx"],
        "output_dir": str(local["tmp"] / "warm"),
        "resume": {"path": str(trained)},
        "resume_mode": "warm_start",
        "identities": {
            **local["ctx"]["identities"],
            "training": "changed-updates",
            "data": "new-data",
            "environment": "new-worker-device",
        },
    }
    warm = mlx_bridge.train(
        inputs={"train": local["data"]}, parameters={**local["params"], "updates": 2}, context=context
    )["trained"]
    assert read_checkpoint(warm)["step"] == 2
    # Full checkpoints must evaluate/export after the original base disappears.
    moved = local["tmp"] / "moved-base"
    local["model"].rename(moved)
    measured = mlx_bridge.evaluate(
        inputs={"trained": trained, "data": local["data"]},
        parameters={"device": "mlx-cpu", "max_length": 32},
        context={**local["ctx"], "output_dir": str(local["tmp"] / "evaluate")},
    )["metrics"]
    assert measured["sample_count"] == 1
    exported = mlx_bridge.export(
        inputs={"trained": trained},
        parameters={"device": "mlx-metal", "parity_tokens": [[2, 4, 5]], "atol": 1e-6},
        context={**local["ctx"], "output_dir": str(local["tmp"] / "export")},
    )["exported"]
    manifest = read_export(exported)
    assert (
        manifest["selected_checkpoint"] == identity(read_checkpoint(trained))
        and manifest["verification"]["fresh_process"]
    )


def test_stalled_reader_large_payload_respects_deadline(configured, monkeypatch):
    local = configured
    wrapper = local["tmp"] / "stalled-python"
    wrapper.write_text("#!" + sys.executable + "\nimport time\ntime.sleep(120)\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("MODELBUILDER_MLX_PYTHON", str(wrapper))
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="deadline"):
        mlx_bridge.evaluate(
            inputs={"data": {"padding": "x" * (1024 * 1024)}},
            parameters={"device": "mlx-metal"},
            context={**local["ctx"], "output_dir": str(local["tmp"] / "stalled"), "timeout_seconds": 0.2},
        )
    assert time.monotonic() - started < 5


def test_architecture_and_asset_budget_precede_model_loading(configured):
    local = configured
    context = {**local["ctx"], "output_dir": str(local["tmp"] / "budget")}
    with pytest.raises(ValueError, match="resource limit|byte budget|exceeds"):
        mlx_bridge.train(
            inputs={"train": local["data"]},
            parameters={**local["params"], "limits": {**local["params"]["limits"], "max_asset_bytes": 10}},
            context=context,
        )
    context = {**local["ctx"], "output_dir": str(local["tmp"] / "architecture")}
    with pytest.raises(ValueError, match="architecture budget exceeded before allocation"):
        mlx_bridge.train(
            inputs={"train": local["data"]},
            parameters={**local["params"], "limits": {**local["params"]["limits"], "max_parameters": 10}},
            context=context,
        )
