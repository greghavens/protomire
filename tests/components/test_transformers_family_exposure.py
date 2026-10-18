"""Offline native optimizer-bound family receipts, separate from dataset presence."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from modelbuilder.backend.components import read_checkpoint
from modelbuilder.backend.components import transformers_training as base
from modelbuilder.backend.components import transformers_epochs as epochs
from modelbuilder.backend.components.transformers_family_exposure import Exposure, verify_family_exposure, verify_family_exposure_path
from test_transformers_training import local as registered_local
from test_transformers_epochs import epoch_local as registered_epoch_local, run_child

local = registered_local
epoch_local = registered_epoch_local


def policy(minimum=1):
    return {'schema_version': 'modelbuilder.family-policy.v1',
            'mapping': {'version': 'user-taxonomy-v1', 'field': 'text'},
            'families': [{'id': name, 'min_rows': 1, 'min_exposure': minimum, 'min_eval_support': 1}
                         for name in ['sun warm', 'rain cold', 'sky light', 'cloud dark']]}


def test_pending_skipped_nonfinite_and_causal_targets():
    torch = pytest.importorskip('torch')
    ledger = Exposure(policy())
    row = ledger.row({'text': 'sun warm'}, 'a')
    ledger.stage([row], torch.tensor([[-100, -100]]), 'causal-lm', torch.tensor(0.))
    ledger.commit()
    assert ledger.snapshot()['counts']['sun warm'] == 0
    # A first-position-only target has no shifted causal prediction target.
    ledger.stage([row], torch.tensor([[4, -100]]), 'causal-lm', torch.tensor(0.))
    ledger.commit()
    assert ledger.snapshot()['counts']['sun warm'] == 0
    ledger.stage([row], torch.tensor([[4, 5]]), 'causal-lm', torch.tensor(1.))
    with pytest.raises(ValueError, match='boundary'):
        ledger.snapshot()
    ledger.commit(skipped=True)
    assert ledger.snapshot()['counts']['sun warm'] == 0
    ledger.stage([row], torch.tensor([0]), 'sequence-classification', torch.tensor(float('nan')))
    ledger.commit()
    ledger.stage([row], torch.tensor([0]), 'sequence-classification', torch.tensor(1.))
    ledger.commit(finite_gradients=False)
    assert ledger.snapshot()['counts']['sun warm'] == 0
    for _ in range(2):
        ledger.stage([row], torch.tensor([0]), 'sequence-classification', torch.tensor(1.))
    ledger.commit()
    receipt = ledger.snapshot()
    assert receipt['counts']['sun warm'] == 2
    assert receipt['unique_sample_ids']['sun warm'] == ['a']
    restored = Exposure(policy())
    restored.restore(receipt)
    assert restored.snapshot() == receipt
    changed = deepcopy(receipt)
    changed['assessment']['status'] = 'passed'
    with pytest.raises(ValueError, match='assessment'):
        restored.restore(changed)


@pytest.mark.parametrize('accumulation', [1, 2])
def test_actual_v1_checkpoint_prefix_and_accumulation(local, accumulation):
    train = {**local['inputs']['train'], 'family_policy': policy()}
    parameters = {**local['parameters'], 'batch_size': 1, 'gradient_accumulation_steps': accumulation,
                  'max_steps': 2}
    result = base.train(inputs={'train': train}, parameters=parameters, context=local['context'])
    first = read_checkpoint(Path(local['context']['output_dir']) / 'checkpoints' / '000000000001')
    first_receipt = verify_family_exposure(first, policy())
    assert sum(first_receipt['counts'].values()) == accumulation
    assert first_receipt['assessment']['status'] == 'failed'
    last = read_checkpoint(result['trained'])
    receipt = verify_family_exposure(last, policy())
    assert sum(receipt['counts'].values()) == 2 * accumulation
    assert receipt['successful_updates'] == 2
    assert verify_family_exposure_path(result['trained'], policy()) == receipt
    assert receipt['assessment']['status'] == ('passed' if accumulation == 2 else 'failed')
    assert first['data']['family_exposure'] == first_receipt
    assert json.loads((result['trained'] / 'family-exposure.json').read_text()) == receipt


@pytest.mark.parametrize('minimum,expected_epochs,complete', [(2, 2, True), (10, 4, False)])
def test_actual_v2_convergence_waits_for_exposure_but_budget_stops(epoch_local, minimum, expected_epochs, complete):
    epoch_local['inputs']['train']['family_policy'] = policy(minimum)
    epoch_local['parameters']['policy']['rules'][0]['threshold'] = 10
    result = epochs.train(inputs=epoch_local['inputs'], parameters=epoch_local['parameters'], context=epoch_local['context'])
    checkpoint = read_checkpoint(result['trained'])
    state = json.loads((result['trained'] / 'epoch-state.json').read_text())
    assert state['completed_epochs'] == expected_epochs
    assert state['family_eligibility']['coverage_complete'] is complete
    assert state['family_exposure'] == verify_family_exposure(checkpoint, policy(minimum))
    assert verify_family_exposure_path(result['trained'], policy(minimum)) == state['family_exposure']
    assert state['family_exposure']['counts'] == {key: expected_epochs for key in state['family_exposure']['counts']}
    earlier = read_checkpoint(Path(epoch_local['context']['output_dir']) / 'checkpoints' / '000000000002')
    assert earlier['data']['family_exposure']['assessment']['status'] == 'failed'


def test_actual_v2_fresh_process_resume_keeps_bound_exposure(epoch_local):
    epoch_local['inputs']['train']['family_policy'] = policy(3)
    epoch_local['parameters']['policy']['rules'][0]['threshold'] = 10
    interrupted = run_child(epoch_local, 'first', cancel_after_epoch=1)
    assert interrupted['status'] == 'cancelled'
    previous = epoch_local['tmp'] / 'first/checkpoints/000000000002'
    result = run_child(epoch_local, 'resumed', resume=previous)
    checkpoint = read_checkpoint(Path(result['path']))
    state = json.loads((Path(result['path']) / 'epoch-state.json').read_text())
    assert state['completed_epochs'] == 3
    assert verify_family_exposure(checkpoint, policy(3))['counts'] == {x['id']: 3 for x in policy()['families']}


def test_actual_stream_prefix_does_not_credit_prefetch(local):
    from test_transformers_training import stream_input
    local['record']['manifest']['family_policy'] = policy()
    data = stream_input(local)
    result = base.train(inputs={'train': data}, parameters={**local['parameters'], 'max_steps': 1,
                        'batch_size': 1}, context=local['context'])
    receipt = verify_family_exposure(read_checkpoint(result['trained']), policy())
    assert receipt['counts'] == {'sun warm': 1, 'rain cold': 0, 'sky light': 0, 'cloud dark': 0}
    assert receipt['assessment']['status'] == 'failed'


def test_actual_skipped_update_discards_pending_rows(local, monkeypatch):
    from accelerate.optimizer import AcceleratedOptimizer
    original = AcceleratedOptimizer.step
    calls = 0

    def skip_first(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        self._is_overflow = calls == 1
        if calls > 1:
            return original(self, *args, **kwargs)

    monkeypatch.setattr(AcceleratedOptimizer, 'step', skip_first)
    result = base.train(inputs={'train': {**local['inputs']['train'], 'family_policy': policy()}},
                        parameters={**local['parameters'], 'max_steps': 2, 'batch_size': 1}, context=local['context'])
    first = read_checkpoint(Path(local['context']['output_dir']) / 'checkpoints/000000000001')
    assert sum(first['data']['family_exposure']['counts'].values()) == 0
    receipt = verify_family_exposure(read_checkpoint(result['trained']), policy())
    assert sum(receipt['counts'].values()) == 1
    assert receipt['successful_updates'] == 1


def test_actual_causal_masked_row_never_counts(local, monkeypatch):
    import hashlib
    import torch
    from transformers import GPT2Config, GPT2LMHeadModel
    from safetensors.torch import save_model
    assets = local['tmp'] / 'assets'
    torch.manual_seed(13)
    model = GPT2LMHeadModel(GPT2Config(vocab_size=10, n_embd=8, n_layer=1, n_head=2,
        n_positions=16, n_ctx=16, pad_token_id=1, bos_token_id=None, eos_token_id=None,
        resid_pdrop=0, embd_pdrop=0, attn_pdrop=0))
    model.config.to_json_file(assets / 'config.json', use_diff=False)
    save_model(model, str(assets / 'weights.safetensors'))
    parameters = deepcopy(local['parameters'])
    for key in ('config', 'weights'):
        parameters['architecture']['model'][key]['sha256'] = hashlib.sha256(Path(local['context']['dependencies'][key]).read_bytes()).hexdigest()
    parameters['architecture'].update(task='causal-lm', labels=[])
    parameters.update(max_steps=1, batch_size=4)
    original = base._features

    def mask(rows, tokenizer, settings):
        features = original(rows, tokenizer, settings)
        for row, feature in zip(rows, features):
            if row['text'] == 'sun warm':
                feature['labels'] = [-100] * len(feature['labels'])
        return features

    monkeypatch.setattr(base, '_features', mask)
    result = base.train(inputs={'train': {**local['inputs']['train'], 'family_policy': policy()}},
                        parameters=parameters, context=local['context'])
    receipt = verify_family_exposure(read_checkpoint(result['trained']), policy())
    assert receipt['counts'] == {'sun warm': 0, 'rain cold': 1, 'sky light': 1, 'cloud dark': 1}
    assert receipt['assessment']['status'] == 'failed'


def test_family_eligibility_preserves_cancel_custom_stop_and_budget():
    from modelbuilder.backend.components.transformers_family_exposure import eligibility
    receipt = Exposure(policy()).snapshot()
    convergence = {'outcome': 'stop', 'rules': [{'kind': 'convergence/v1', 'outcome': 'stop', 'reasons': ['criterion_met']}]}
    assert eligibility(convergence, {'outcome': 'continue'}, receipt)['outcome'] == 'continue'
    assert eligibility(convergence, {'outcome': 'stop'}, receipt)['outcome'] == 'stop'
    custom = {'outcome': 'stop', 'rules': [{'kind': 'method/v1', 'outcome': 'stop', 'reasons': ['criterion_met']}]}
    assert eligibility(custom, {'outcome': 'continue'}, receipt)['outcome'] == 'stop'
    for outcome in ('cancelled', 'blocked'):
        assert eligibility({'outcome': outcome}, {'outcome': 'stop'}, receipt)['outcome'] == outcome


def test_eager_policy_receipt_cannot_be_stripped_or_inflated(local):
    from modelbuilder.dataset import final_family_coverage
    from modelbuilder.backend.components import identity
    data = deepcopy(local['inputs']['train'])
    report = final_family_coverage(policy(), data['split']['id'], data['rows'])
    data['family_policy'] = policy()
    data['final_dataset_family_coverage'] = deepcopy(report)
    data['consumption']['final_dataset_family_coverage'] = deepcopy(report)
    data['consumption']['digest'] = identity({key: value for key, value in data['consumption'].items() if key != 'digest'})
    settings = local['parameters']['architecture']
    assert base._split(data, {'train'}, settings)[1]['final_dataset_family_coverage'] == report
    stripped = deepcopy(data)
    del stripped['family_policy']
    with pytest.raises(ValueError, match='matching family policy'):
        base._split(stripped, {'train'}, settings)
    inflated = deepcopy(data)
    inflated['final_dataset_family_coverage']['counts']['sun warm'] = 10
    with pytest.raises(ValueError, match='measured eager rows'):
        base._split(inflated, {'train'}, settings)
    missing = deepcopy(data)
    del missing['final_dataset_family_coverage']
    with pytest.raises(ValueError, match='envelope'):
        base._split(missing, {'train'}, settings)


@pytest.mark.parametrize('mode', ['amp', 'distributed', 'noncpu', 'nonfloat32'])
def test_unreliable_exposure_runtime_fails_closed(mode):
    from types import SimpleNamespace
    from modelbuilder.backend.components.transformers_family_exposure import validate_runtime
    torch = pytest.importorskip('torch')
    model = torch.nn.Linear(2, 2)
    args = SimpleNamespace(device='cpu', fp16=False, bf16=False)
    accelerator = SimpleNamespace(num_processes=1, mixed_precision='no')
    if mode == 'amp':
        accelerator.mixed_precision = 'bf16'
    elif mode == 'distributed':
        accelerator.num_processes = 2
    elif mode == 'noncpu':
        args.device = 'cuda:0'
    else:
        model.double()
    with pytest.raises(ValueError, match='single-process CPU float32'):
        validate_runtime(SimpleNamespace(args=args, accelerator=accelerator), model)
