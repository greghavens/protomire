from modelbuilder.backend.components import transformers_training as adapters
from modelbuilder.family_evaluation import family_metric_id
from modelbuilder.evaluation import validate_evaluation_record
from tests.components.test_transformers_training import local as registered_local, do_train

local = registered_local


def test_actual_family_classification_support_and_missing_slice(local):
    trained = do_train(local)
    policy = {'schema_version': 'modelbuilder.family-policy.v1', 'mapping': {'version': '1', 'field': 'id'},
              'families': [{'id': key, 'min_rows': 0, 'min_exposure': 0, 'min_eval_support': 1}
                           for key in ['4', '5', 'missing']]}
    result = adapters.evaluate(inputs={'trained': trained, 'data': local['inputs']['data']},
        parameters={'batch_size': 2, 'metrics': ['accuracy', 'cross_entropy'], 'family_policy': policy},
        context={'output_dir': str(local['tmp'] / 'family-eval')})['metrics']
    assert result['family_evaluation']['families']['missing']['status'] == 'unavailable'
    for family in ('4', '5'):
        for metric in ('accuracy', 'cross_entropy'):
            record = result['evaluation_records'][family_metric_id(family, metric)]
            assert not validate_evaluation_record(record)
            assert record['split']['sample_ids'] == [family]
            assert len(record['predictions']) == 1
    assert family_metric_id('missing', 'accuracy') not in result['evaluation_records']


def test_native_checkpoint_exact_family_exposure_gate(local):
    import pytest
    from copy import deepcopy
    from modelbuilder.backend.rule_attestations import family_exposure_facts
    from modelbuilder.backend.components.rule_gate import gate
    policy = {'schema_version': 'modelbuilder.family-policy.v1', 'mapping': {'version': '1', 'field': 'id'},
              'families': [{'id': key, 'min_rows': 0, 'min_exposure': 1, 'min_eval_support': 1}
                           for key in ['0', '1', '2', '3']]}
    local['inputs']['train']['family_policy'] = policy
    trained = do_train(local)
    parameters = {'policy': {'schema_version': 'modelbuilder.rules/v1', 'id': 'coverage', 'version': '1',
                            'rules': [{'id': 'coverage', 'kind': 'family_exposure/v1', 'policy': policy}]}}
    evidence = {'schema_version': 'modelbuilder.rule-evidence/v1',
                'subject': {'project_id': 'p', 'run_id': 'r', 'config_digest': 'a' * 64, 'data_digest': 'b' * 64},
                'source': 'native-test', 'verification': 'verified',
                'facts': {'coverage': family_exposure_facts(trained, policy)}}
    assert gate(inputs={'target': trained, 'evidence': evidence}, parameters=parameters, context={})['allowed']
    with pytest.raises(ValueError, match='native_target_required'):
        gate(inputs={'evidence': evidence}, parameters=parameters, context={})
    # Discover the adapter's exact immutable checkpoint directory spelling.
    earlier = min((local['tmp'] / 'trained' / 'checkpoints').glob('*/checkpoint.manifest.json'), key=lambda path: __import__('json').loads(path.read_text())['step']).parent
    with pytest.raises(ValueError, match='target_mismatch'):
        gate(inputs={'target': earlier, 'evidence': evidence}, parameters=parameters, context={})
    early_evidence = deepcopy(evidence)
    early_evidence['facts']['coverage'] = family_exposure_facts(earlier, policy)
    with pytest.raises(ValueError, match='rule_blocked'):
        gate(inputs={'target': earlier, 'evidence': early_evidence}, parameters=parameters, context={})
    with pytest.raises(ValueError, match='unavailable'):
        family_exposure_facts(trained, policy, 'best')
    forged = deepcopy(evidence)
    forged['facts']['coverage']['counts']['0'] += 100
    with pytest.raises(ValueError, match='target_mismatch'):
        gate(inputs={'target': trained, 'evidence': forged}, parameters=parameters, context={})


def test_actual_causal_family_metrics_are_token_weighted(local):
    import hashlib
    import math
    from pathlib import Path
    from safetensors.torch import save_model
    from transformers import GPT2Config, GPT2LMHeadModel
    from modelbuilder.backend.components import identity
    model = GPT2LMHeadModel(GPT2Config(vocab_size=10, n_embd=8, n_layer=1, n_head=2,
        n_positions=16, n_ctx=16, pad_token_id=1, bos_token_id=None, eos_token_id=None,
        resid_pdrop=0, embd_pdrop=0, attn_pdrop=0))
    assets = local['tmp'] / 'assets'
    model.config.to_json_file(assets / 'config.json', use_diff=False)
    save_model(model, str(assets / 'weights.safetensors'))
    architecture = local['parameters']['architecture']
    for key in ('config', 'weights'):
        architecture['model'][key]['sha256'] = hashlib.sha256(Path(local['context']['dependencies'][key]).read_bytes()).hexdigest()
    architecture.update(task='causal-lm', labels=[])
    trained = do_train(local)
    data = local['inputs']['data']
    data['rows'][1]['text'] = 'rain cloud dark'
    for index, row in enumerate(data['rows']):
        row['family'] = 'words'
        data['consumption']['sample_digests'][index] = identity({k: v for k, v in row.items() if k != 'id'})
    data['consumption']['digest'] = identity({k: v for k, v in data['consumption'].items() if k != 'digest'})
    policy = {'schema_version': 'modelbuilder.family-policy.v1', 'mapping': {'version': '1', 'field': 'family'},
              'families': [{'id': 'words', 'min_rows': 0, 'min_exposure': 0, 'min_eval_support': 2}]}
    metrics = adapters.evaluate(inputs={'trained': trained, 'data': data},
        parameters={'batch_size': 2, 'metrics': ['cross_entropy', 'perplexity'], 'family_policy': policy},
        context={'output_dir': str(local['tmp'] / 'causal-family-eval')})['metrics']
    record = metrics['evaluation_records'][family_metric_id('words', 'cross_entropy')]
    contributions = record['predictions']
    assert sorted(p['token_count'] for p in contributions) == [1, 2]
    expected = sum(p['negative_log_likelihood_sum'] for p in contributions) / 3
    assert math.isclose(record['value'], expected)
    assert math.isclose(metrics[family_metric_id('words', 'perplexity')], math.exp(expected))
    assert not validate_evaluation_record(record)
