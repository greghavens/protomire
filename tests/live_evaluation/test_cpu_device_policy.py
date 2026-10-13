"""Actual child process policy and context; no accelerator allocation involved."""
import hashlib
from pathlib import Path
from modelbuilder.backend.live_evaluation import LiveEvaluationCoordinator, list_evaluations
from test_live_evaluation import checkpoint, evaluator, authorize_evaluator


def test_live_child_explicitly_uses_cpu_environment_and_context(tmp_path, monkeypatch):
    configured = evaluator(tmp_path)
    source = tmp_path / 'source/harness.py'
    source.write_text(source.read_text().replace('    value = state["value"]',
        '    import os\n'
        '    assert context["device"] == "cpu"\n'
        '    assert context["placement"]["policy"] == "live_evaluation_cpu_only"\n'
        '    assert os.environ["MODELBUILDER_EXECUTION_DEVICE"] == "cpu"\n'
        '    assert os.environ["CUDA_VISIBLE_DEVICES"] == ""\n'
        '    assert os.environ["HIP_VISIBLE_DEVICES"] == ""\n'
        '    assert os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] == "0"\n'
        '    value = state["value"]'))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    configured['spec']['component_node']['implementation_ref']['sha256'] = digest
    configured['spec']['source']['files'] = [{'path': 'harness.py', 'sha256': digest}]
    authorize_evaluator(tmp_path, configured)
    monkeypatch.setenv('MODELBUILDER_EXECUTION_DEVICE', 'cpu')
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '4')
    monkeypatch.setenv('HIP_VISIBLE_DEVICES', '2')
    monkeypatch.setenv('PYTORCH_ENABLE_MPS_FALLBACK', '1')
    job = tmp_path / 'job'
    coordinator = LiveEvaluationCoordinator(job, 'train', configured, max_concurrency=1, timeout_seconds=10)
    try:
        coordinator.checkpoint_ready(Path(checkpoint(tmp_path)), step=1)
    finally:
        coordinator.close()
    records = list_evaluations(job)['evaluations']
    assert len(records) == 1 and records[0]['status'] == 'completed', records


def test_intermediate_accelerator_snapshot_rejects_exact_resume_claim_without_hardware(tmp_path):
    import json
    import pytest
    from modelbuilder.backend.live_evaluation import _snapshot
    source=checkpoint(tmp_path)
    manifest=source/'checkpoint.manifest.json'
    descriptor=json.loads(manifest.read_text())
    descriptor['resume_modes']=['exact_state','warm_start']
    descriptor['state']=['model','optimizer','scheduler','rng','data_cursor']
    manifest.write_text(json.dumps(descriptor))
    job=tmp_path/'job'
    with pytest.raises(ValueError,match='cannot advertise unverified exact_state'):
        _snapshot(job,'train',source,1,'last','component',device='mps')
    assert not (job/'live_evaluation/checkpoints').exists()
    descriptor['resume_modes']=['warm_start']
    manifest.write_text(json.dumps(descriptor))
    published=_snapshot(job,'train',source,1,'last','component',device='mps')
    assert published['step']==1
