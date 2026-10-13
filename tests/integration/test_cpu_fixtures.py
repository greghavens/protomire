"""Real CPU computations. Run in .venv-backend; no model calls are mocked."""
import builtins
import copy
import hashlib
import io
import json
import os
import shutil
from pathlib import Path

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


def deny_file_reads(monkeypatch, paths):
    forbidden = {path.resolve() for path in paths}
    for module in (builtins, io):
        original = module.open

        def guarded(file, *args, _open=original, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)) and Path(os.fsdecode(file)).resolve() in forbidden:
                raise AssertionError(f"Unpinned tokenizer file was opened: {file}")
            return _open(file, *args, **kwargs)

        monkeypatch.setattr(module, "open", guarded)


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
    assert data["sha256"] == "d677dcdcd6f2a73f8c4e4387fa7c2dafdaafc2deb124e275cf3a0f8d65d3ef6e"
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


@pytest.fixture
def tiny_pinned_tokenizer(tmp_path, monkeypatch):
    """Synthetic hash-checked assets exercise closure without fetching a model."""
    directory = tmp_path / "model"
    directory.mkdir()
    files = {"vocab.txt": b"[PAD]\n[UNK]\n[CLS]\n[SEP]\n[MASK]\nhello\nworld\n",
             "config.json": b"{}", "pytorch_model.bin": b"not used by the tokenizer"}
    for name, content in files.items():
        (directory / name).write_bytes(content)
    monkeypatch.setattr(fixtures, "MODEL_FILES", {name: hashlib.sha256(content).hexdigest()
                                                for name, content in files.items()})
    monkeypatch.setenv("MODELBUILDER_PRETRAINED_PATH", str(directory))
    return directory


@pytest.mark.parametrize("sidecar", ["tokenizer_config.json", "added_tokens.json", "special_tokens_map.json",
                                     "tokenizer.json"])
def test_pretrained_tokenizer_ignores_unpinned_sidecars(tiny_pinned_tokenizer, tmp_path, sidecar, monkeypatch):
    directory = tiny_pinned_tokenizer
    outside = tmp_path / "outside-vocab.txt"
    outside.write_bytes((directory / "vocab.txt").read_bytes().replace(b"hello", b"benign"))
    payloads = {"tokenizer_config.json": {"vocab_file": str(outside), "unk_token": "world",
                                        "do_basic_tokenize": False},
                "added_tokens.json": {"benign-added-token": 7},
                "special_tokens_map.json": {"cls_token": "[MASK]", "unk_token": "world"}}
    # Invalid tokenizer.json also must not be parsed just because it is nearby.
    (directory / sidecar).write_text(json.dumps(payloads[sidecar]) if sidecar in payloads else "invalid json")
    deny_file_reads(monkeypatch, [directory / sidecar, outside])
    tokenizer = fixtures._pretrained_tokenizer()
    assert tokenizer.encode("Hello world") == [2, 5, 6, 3]
    assert len(tokenizer) == 7 and tokenizer.unk_token == "[UNK]"
    assert "benign" not in tokenizer.get_vocab() and "benign-added-token" not in tokenizer.get_vocab()
    tokenizer.save_pretrained(tmp_path / "export")
    assert (tmp_path / "export/vocab.txt").read_bytes() == (directory / "vocab.txt").read_bytes()
    assert str(outside) not in (tmp_path / "export/tokenizer_config.json").read_text()


def test_pretrained_tokenizer_rechecks_vocabulary_snapshot(tiny_pinned_tokenizer, monkeypatch):
    original = fixtures.model_directory

    def replace_after_directory_check():
        directory = original()
        (directory / "vocab.txt").write_text("replaced after directory validation\n")
        return directory

    monkeypatch.setattr(fixtures, "model_directory", replace_after_directory_check)
    with pytest.raises(ValueError, match="hash mismatch: vocab.txt"):
        fixtures._pretrained_tokenizer()


def test_pretrained_dataset_and_export_close_tokenizer_assets(tmp_path, monkeypatch):
    from transformers import BertTokenizer

    directory = tmp_path / "model"
    directory.mkdir()
    for name in fixtures.MODEL_FILES:
        shutil.copyfile(fixtures.model_directory() / name, directory / name)
    monkeypatch.setenv("MODELBUILDER_PRETRAINED_PATH", str(directory))
    data = fixtures.dataset({"fixture": "pretrained"})
    initial = fixtures.initialize(data, {"fixture": "pretrained", "seed": 701}, tmp_path / "initial")
    checkpoint = fixtures.train(data, initial, {"steps": 1}, tmp_path / "trained")
    baseline = fixtures.export_model(data, checkpoint, {}, tmp_path / "baseline")
    # The explicit constructor must preserve the clean legacy tokenizer assets.
    legacy = tmp_path / "legacy-tokenizer"
    BertTokenizer.from_pretrained(str(directory), local_files_only=True).save_pretrained(legacy)
    for path in legacy.iterdir():
        assert (baseline / path.name).read_bytes() == path.read_bytes(), path.name

    outside = tmp_path / "outside-vocab.txt"
    vocabulary = (directory / "vocab.txt").read_text().splitlines()
    vocabulary[vocabulary.index("wonderful")] = "benign-unused-token"
    outside.write_text("\n".join(vocabulary) + "\n")
    (directory / "tokenizer_config.json").write_text(json.dumps({"vocab_file": str(outside)}))
    (directory / "added_tokens.json").write_text(json.dumps({"benign-added-token": 30522}))
    (directory / "special_tokens_map.json").write_text(json.dumps({"cls_token": "[MASK]"}))
    deny_file_reads(monkeypatch, [outside, *(directory / name for name in (
        "tokenizer_config.json", "added_tokens.json", "special_tokens_map.json"))])
    assert fixtures.dataset({"fixture": "pretrained"}) == data
    exported = fixtures.export_model(data, checkpoint, {}, tmp_path / "with-sidecars")
    assert {path.name: path.read_bytes() for path in exported.iterdir()} == {
        path.name: path.read_bytes() for path in baseline.iterdir()}


def test_builtin_rights_manifests_preserve_exact_selected_bytes_and_parent_union(tmp_path):
    from modelbuilder.backend.components import read_checkpoint, read_export
    from modelbuilder.backend.fixture_artifacts import input_rights, selected_identity
    from modelbuilder.hub_rights import validate_artifact_rights
    from modelbuilder.models import ServiceError
    from tests.components.rights_test_support import USAGE_CONTEXT, local_review

    first, first_rights = local_review(policy_id="dataset-origin")
    second, second_rights = local_review(policy_id="model-origin", commit="2" * 40)
    data = fixtures.dataset({"fixture": "custom"})
    original_digest = data["sha256"]
    data["rights"] = first_rights
    assert fixtures.verify_data(data) == original_digest
    spec = {"node_id": "initial", "rights_extensions": {"artifact_rights": second_rights}}
    initial_rights = input_rights(spec, data)
    initial = fixtures.initialize(data, {"fixture": "custom", "_artifact_rights": initial_rights}, tmp_path / "initial")
    training_rights = input_rights({**spec, "node_id": "train"}, data, [initial])
    trained = fixtures.train(data, initial, {"steps": 3, "_artifact_rights": training_rights}, tmp_path / "trained")
    manifest = read_checkpoint(trained)
    assert manifest["rights"] == training_rights
    assert {source["license_policy_id"] for source in manifest["rights"]["sources"]} == {"dataset-origin", "model-origin"}
    assert manifest["rights"]["parent_rights_digests"] and not manifest["rights"]["unresolved"]
    assert manifest["resume_modes"] == ["exact_state", "warm_start"]
    for kind, filename in (("last", "checkpoint.pt"), ("best", "best.pt")):
        assert selected_identity(manifest, kind) == hashlib.sha256((trained / filename).read_bytes()).hexdigest()
    policies = {policy["policy_id"]: policy for policy in (first, second)}
    assert validate_artifact_rights(manifest["rights"], "train", policies, USAGE_CONTEXT)["policy_ids"]
    exported = read_export(fixtures.export_model(data, trained, {}, tmp_path / "export"))
    assert exported["rights"] == manifest["rights"]
    assert exported["selected_checkpoint"] == selected_identity(manifest, "best")
    unreviewed = fixtures.initialize(fixtures.dataset({}), {"fixture": "custom"}, tmp_path / "unreviewed")
    missing = read_checkpoint(unreviewed)["rights"]
    assert missing["unresolved"]
    with pytest.raises(ServiceError) as failed:
        validate_artifact_rights(missing, "train", policies, USAGE_CONTEXT)
    assert failed.value.code == "license_rights_unreviewed"
    with pytest.raises(ValueError, match="exact verified asset"):
        selected_identity({**manifest, "selections": {"best": manifest["selections"]["last"]}}, "best")
    (trained / "best.pt").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity mismatch"):
        read_checkpoint(trained)
