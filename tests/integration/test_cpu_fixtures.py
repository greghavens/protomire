"""Real CPU computations. Run in .venv-backend; no model calls are mocked."""
import copy

import pytest
torch = pytest.importorskip("torch", reason="Real CPU fixture suite requires .venv-backend")

from modelbuilder.backend import fixtures  # noqa: E402

pytestmark = pytest.mark.integration


def equal_state(a, b):
    if isinstance(a, torch.Tensor):
        return isinstance(b, torch.Tensor) and torch.equal(a, b)
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(equal_state(a[k], b[k]) for k in a)
    if isinstance(a, (tuple, list)):
        return len(a) == len(b) and all(equal_state(x, y) for x, y in zip(a, b))
    return a == b


def test_dataset_consumed_hash_and_tamper(tmp_path):
    data = fixtures.dataset({"fixture": "custom"})
    assert fixtures.verify_data(data) == data["sha256"]
    altered = copy.deepcopy(data)
    altered["x"][0][0] += 0.1
    with pytest.raises(ValueError, match="hash mismatch"):
        fixtures.initialize(altered, {"fixture": "custom"}, tmp_path)


def test_exact_model_optimizer_scheduler_rng_and_cursor(tmp_path):
    data = fixtures.dataset({"fixture": "custom"})
    initial = fixtures.initialize(data, {"fixture": "custom"}, tmp_path / "initial")
    params = {"steps": 12, "batch_size": 50}
    full = fixtures.train(data, initial, params, tmp_path / "full")
    partial = fixtures.train(data, initial, {**params, "steps": 5}, tmp_path / "partial")
    resumed = fixtures.train(data, initial, params, tmp_path / "resumed", resume=partial)
    a, b = fixtures.load_checkpoint(full), fixtures.load_checkpoint(resumed)
    for field in ("model", "optimizer", "scheduler", "torch_rng", "python_rng", "order", "cursor", "epoch", "reference", "history"):
        assert equal_state(a[field], b[field]), field
    assert b["start_step"] == 5
    assert b["resume_mode"] == "exact_state"
    assert b["cursor"] != 0


def test_resume_rejects_changed_training_contract(tmp_path):
    data = fixtures.dataset({})
    initial = fixtures.initialize(data, {"fixture": "custom"}, tmp_path / "initial")
    partial = fixtures.train(data, initial, {"steps": 2}, tmp_path / "partial")
    with pytest.raises(ValueError, match="training contract differs"):
        fixtures.train(data, initial, {"steps": 3, "learning_rate": 0.005}, tmp_path / "bad", resume=partial)
    warm = fixtures.train(data, initial, {"steps": 3, "learning_rate": 0.005, "resume_mode": "warm_start"}, tmp_path / "warm", resume=partial)
    assert fixtures.load_checkpoint(warm)["start_step"] == 0


def test_architecture_kl_and_negative_outcome_are_real(tmp_path):
    data = fixtures.dataset({})
    initial = fixtures.initialize(data, {"fixture": "custom"}, tmp_path / "initial")
    baseline = fixtures.train(data, initial, {"steps": 30, "batch_size": 60}, tmp_path / "baseline")
    kl = {"enabled": True, "coefficient": 5.0, "direction": "reference_to_model", "reference_model_ref": "initial", "frozen": True}
    regularized = fixtures.train(data, initial, {"steps": 30, "batch_size": 60, "kl": kl}, tmp_path / "kl")
    wide_initial = fixtures.initialize(data, {"fixture": "custom", "width": 20}, tmp_path / "wide_initial")
    wide = fixtures.train(data, wide_initial, {"steps": 30, "width": 20}, tmp_path / "wide")
    b = fixtures.evaluate(data, baseline, {}, tmp_path / "baseline_metrics")
    k = fixtures.evaluate(data, regularized, {}, tmp_path / "kl_metrics")
    assert k["validation_loss"] > b["validation_loss"]  # Deliberately retained negative outcome.
    assert k["kl"]["coefficient"] == 5.0
    assert fixtures.load_checkpoint(wide)["model"]["net.0.weight"].shape[0] == 20
    assert not equal_state(fixtures.load_checkpoint(baseline)["model"], fixtures.load_checkpoint(regularized)["model"])


def test_cancellation_preserves_complete_atomic_checkpoint(tmp_path):
    data = fixtures.dataset({})
    initial = fixtures.initialize(data, {"fixture": "custom"}, tmp_path / "initial")
    cancel = tmp_path / "cancel.json"
    cancel.write_text("{}")
    with pytest.raises(InterruptedError):
        fixtures.train(data, initial, {"steps": 20, "_cancel_file": str(cancel)}, tmp_path / "cancelled")
    state = fixtures.load_checkpoint(tmp_path / "cancelled")
    assert state["step"] == 1 and "scheduler" in state and "python_rng" in state
    assert not list((tmp_path / "cancelled").glob("*.partial"))


def test_real_pinned_pretrained_kl_and_export_parity(tmp_path):
    fixtures.model_directory()  # Missing or mismatching immutable dependency is a hard failure.
    data = fixtures.dataset({"fixture": "pretrained"})
    assert data["vocabulary_size"] == 30522 and len(data["tokens"]["input_ids"]) == 48
    initial = fixtures.initialize(data, {"fixture": "pretrained", "seed": 701}, tmp_path / "initial")
    baseline = fixtures.train(data, initial, {"steps": 8}, tmp_path / "baseline")
    kl = {"enabled": True, "coefficient": 0.2, "direction": "model_to_reference", "reference_model_ref": "initial", "frozen": True}
    regularized = fixtures.train(data, initial, {"steps": 8, "kl": kl}, tmp_path / "kl")
    assert not equal_state(fixtures.load_checkpoint(baseline)["model"], fixtures.load_checkpoint(regularized)["model"])
    result = fixtures.evaluate(data, baseline, {}, tmp_path / "evaluation")
    assert result["revision"] == fixtures.MODEL_REVISION
    exported = fixtures.export_model(data, baseline, {}, tmp_path / "export")
    assert '"max_abs_difference": 0.0' in (exported / "parity.json").read_text()


def test_pretrained_exact_continuation(tmp_path):
    data = fixtures.dataset({"fixture": "pretrained"})
    initial = fixtures.initialize(data, {"fixture": "pretrained", "seed": 701}, tmp_path / "initial")
    full = fixtures.train(data, initial, {"steps": 4, "batch_size": 12}, tmp_path / "full")
    partial = fixtures.train(data, initial, {"steps": 2, "batch_size": 12}, tmp_path / "partial")
    resumed = fixtures.train(data, initial, {"steps": 4, "batch_size": 12}, tmp_path / "resumed", resume=partial)
    a, b = fixtures.load_checkpoint(full), fixtures.load_checkpoint(resumed)
    for field in ("model", "optimizer", "scheduler", "torch_rng", "python_rng", "order", "cursor", "epoch"):
        assert equal_state(a[field], b[field]), field


def test_model_initialization_is_explicit_and_dataset_compatible(tmp_path):
    custom_data = fixtures.dataset({"fixture": "custom"})
    with pytest.raises(ValueError, match="explicitly select"):
        fixtures.initialize(custom_data, {}, tmp_path / "unspecified")
    with pytest.raises(ValueError, match="incompatible"):
        fixtures.initialize(custom_data, {"fixture": "pretrained"}, tmp_path / "mismatch")
    with pytest.raises(ValueError, match="explicitly select"):
        fixtures.initialize(custom_data, {"fixture": "arbitrary-model"}, tmp_path / "unsupported")
    chosen = fixtures.initialize(custom_data, {"fixture": "custom", "width": 20}, tmp_path / "chosen")
    saved = fixtures.load_checkpoint(chosen)
    assert saved["initialization"]["fixture"] == "custom"
    assert saved["initialization"]["width"] == 20
    assert saved["model"]["net.0.weight"].shape[0] == 20
    pretrained_data = fixtures.dataset({"fixture": "pretrained"})
    with pytest.raises(ValueError, match="incompatible"):
        fixtures.initialize(pretrained_data, {"fixture": "custom"}, tmp_path / "reverse-mismatch")
    with pytest.raises(ValueError, match="architecture is pinned"):
        fixtures.initialize(pretrained_data, {"fixture": "pretrained", "width": 20}, tmp_path / "false-width")
