"""Registered family policy survives native ZenML artifacts and worker serialization."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from modelbuilder.backend.common import write_json
from modelbuilder.backend.components import read_checkpoint, validate_component
from modelbuilder.backend.components import transformers_training as adapters
from modelbuilder.backend.components.transformers_family_exposure import verify_family_exposure_path
from modelbuilder.evaluation import validate_evaluation_record
from modelbuilder.family_evaluation import family_metric_id
from tests.components.test_transformers_training import local as registered_local

local = registered_local


def family_policy(ids, *, training):
    return {'schema_version': 'modelbuilder.family-policy.v1',
            'mapping': {'version': 'explicit-test-families-v1', 'field': 'id'},
            'families': [{'id': sample, 'min_rows': 1 if training else 0,
                          'min_exposure': 1 if training else 0, 'min_eval_support': 1}
                         for sample in ids]}


@pytest.mark.integration
@pytest.mark.parametrize("live", [False, True])
def test_registered_family_policy_native_train_and_family_evaluation(local, live):
    # This is a required native test: missing backend dependencies are failures.
    import zenml  # noqa: F401
    from tests.components.rights_test_support import authorize_test_recipe
    from tests.integration.native_fixture_support import synthetic_review

    train_policy = family_policy(['0', '1', '2', '3'], training=True)
    eval_policy = family_policy(['4', '5', 'missing-family'], training=False)
    manifest = deepcopy(local['record']['manifest'])
    manifest['name'] = 'Registered functionality-family fixture'
    manifest['family_policy'] = train_policy
    registry, pid = local['registry'], local['project']['project_id']
    record = registry.register(pid, manifest)
    reference = registry.portable_reference(pid, record['dataset_id'], 1)
    dependency_paths, guards = registry.execution_sources(pid, record['dataset_id'], 1)
    reference_path = local['tmp'] / 'family-dataset-reference.json'
    reference_path.write_text(json.dumps(reference))
    dependency_paths['dataset-reference'] = str(reference_path)
    dependency_paths.update(local['context']['dependencies'])
    dependencies = [
        {'id': key, 'kind': 'model' if key in local['context']['dependencies'] else 'dataset',
         'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
         'metadata': {'rights': local['rights'],
                      **({'dataset_reference': reference} if key == 'dataset-reference' else {})}}
        for key, path in dependency_paths.items()
    ]
    nodes, edges = [], []
    for name, split, target, port, purpose in [
        ('training_data', 'train', 'train', 'train', 'training'),
        ('evaluation_data', 'dev', 'evaluate', 'data', 'evaluation'),
    ]:
        nodes.append({'id': name, 'kind': 'dataset.version.import', 'timeout': 120, 'inputs': [],
                      'outputs': [{'name': 'data', 'type': 'dict'}],
                      'parameters': {'reference_id': 'dataset-reference', 'split_id': split, 'purpose': purpose}})
        edges.append({'source_node': name, 'source_port': 'data', 'target_node': target, 'target_port': port})
    if live:
        edges.append({'source_node': 'evaluation_data', 'source_port': 'data', 'target_node': 'train', 'target_port': 'validation'})
    parameter_sets = [local['parameters'], {'batch_size': 2, 'metrics': ['accuracy', 'cross_entropy'],
                                            'family_policy': eval_policy}]
    for name, entry, parameters in zip(['train', 'evaluate'], adapters.catalog_entries()[:2], parameter_sets):
        component = {key: entry[key] for key in
                     ('id', 'version', 'protocol', 'role', 'description', 'parameter_schema', 'capabilities')}
        node = {'id': name, 'kind': 'component.run', 'component': component, 'inputs': entry['inputs'],
                'outputs': entry['outputs'], 'parameters': parameters, 'timeout': 120,
                'retry': {'max_attempts': 1},
                'implementation_ref': {**local['context']['source']['files'][0], 'module': 'adapter',
                                       'entrypoint': name, 'commit': local['context']['source']['commit']}}
        validate_component(node)
        nodes.append(node)
    edges.append({'source_node': 'train', 'source_port': 'trained', 'target_node': 'evaluate', 'target_port': 'trained'})
    recipe = {'recipe_id': 'native-family-policy', 'revision': 1, 'source': local['context']['source'],
              'nodes': nodes, 'edges': edges, 'external_dependencies': dependencies,
              'evaluation': {'live': {'enabled': live, 'max_concurrency': 1, 'timeout_seconds': 60}}}
    review = synthetic_review(recipe, pid, local['source'])
    policies = {item['policy_id']: item for item in (review, local['policy'])}
    authorization = authorize_test_recipe(local['tmp'], recipe, policies, project_id=pid)
    job = local['tmp'] / 'family-job'
    write_json(job / 'request.json', {**authorization, 'recipe': recipe, 'project_id': pid,
               'operation_id': 'native-family-policy', 'run_name': 'native-family-policy',
               'source_root': str(local['source']), 'trusted_code_roots': [str(local['source'])],
               'dependency_paths': dependency_paths, 'dataset_source_guards': guards})
    env = {**os.environ, 'ZENML_CONFIG_PATH': str(local['tmp'] / 'zenml'),
           'ZENML_LOCAL_STORES_PATH': str(local['tmp'] / 'stores'), 'ZENML_ANALYTICS_OPT_IN': 'false',
           'ZENML_LOGGING_VERBOSITY': 'ERROR', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
           'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'}
    process = subprocess.run([sys.executable, '-m', 'modelbuilder.backend.worker', str(job)],
                             env=env, capture_output=True, text=True, timeout=180)
    (local['tmp'] / 'family-native.log').write_text(process.stdout + process.stderr)
    assert process.returncode == 0, (process.stdout + process.stderr)[-16000:]

    # Reload actual serialized outputs through the backend, in a fresh process.
    inspection = local['tmp'] / 'inspect_family_native.py'
    inspection.write_text('''import json,sys
from pathlib import Path
from zenml.client import Client
job=Path(sys.argv[1]); native=json.loads((job/'native.json').read_text())
run=Client().get_pipeline_run(native['backend_run_id'], include_full_metadata=True)
assert str(run.status)=='completed'
outputs={port: versions[-1].load() for step in run.steps.values() for port,versions in step.outputs.items()}
assert 'trained' in outputs and 'metrics' in outputs
(job/'backend-family-results.json').write_text(json.dumps({'trained':str(outputs['trained']), 'metrics':outputs['metrics']}))
''')
    inspected = subprocess.run([sys.executable, str(inspection), str(job)], env=env,
                               capture_output=True, text=True, timeout=60)
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    reloaded = json.loads((job / 'backend-family-results.json').read_text())
    selected = Path(reloaded['trained'])
    checkpoint = read_checkpoint(selected)
    receipt = verify_family_exposure_path(selected, train_policy)
    assert checkpoint['data']['family_policy'] == train_policy
    assert checkpoint['data']['final_dataset_family_coverage']['status'] == 'passed'
    assert receipt['assessment']['status'] == 'passed'
    assert sum(receipt['counts'].values()) == 6
    assert receipt['successful_updates'] == 3
    assert {sample for ids in receipt['unique_sample_ids'].values() for sample in ids} == {'0', '1', '2', '3'}
    early = job / 'outputs/train/checkpoints/000000000001'
    assert verify_family_exposure_path(early, train_policy)['assessment']['status'] == 'failed'
    metrics = reloaded['metrics']
    assert metrics['evaluation_records']['accuracy']['split']['sample_ids'] == ['4', '5']
    for family in ('4', '5'):
        for metric in ('accuracy', 'cross_entropy'):
            record = metrics['evaluation_records'][family_metric_id(family, metric)]
            assert not validate_evaluation_record(record)
            assert record['split']['sample_ids'] == [family]
            assert record['treatment']['checkpoint'] == metrics['evaluation_records']['accuracy']['treatment']['checkpoint']

    assert metrics['family_evaluation']['families']['missing-family']['status'] == 'unavailable'
    if live:
        from modelbuilder.backend.live_evaluation import list_evaluations, list_events
        evaluations = list_evaluations(job)['evaluations']
        assert len(evaluations) == 3 and all(item['status'] == 'completed' for item in evaluations), evaluations
        for item in evaluations:
            report = item['family_evaluation']
            assert report['families']['missing-family']['support'] == 0
            assert report['families']['missing-family']['status'] == 'unavailable'
            assert report['families']['4']['status'] == 'available'
        completed = [event for event in list_events(job)['events'] if event['kind'] == 'evaluation.completed']
        assert len(completed) == 3
        assert all(event['family_evaluation']['families']['missing-family']['status'] == 'unavailable' for event in completed)
