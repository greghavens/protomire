"""Actual multi-output component and Python nodes through the shared service."""
from pathlib import Path

from modelbuilder.backend import ZenMLBackend
from modelbuilder.service import OperationService
import test_project_source_execution as fixture

pytestmark = fixture.pytestmark


def test_native_component_and_python_multiple_outputs(tmp_path, monkeypatch):
    source_text = fixture.HARNESS.replace("return {\"trained\": target}", "return {\"trained\": target, \"metrics\": descriptor['metrics']}")
    source_text += '''\ndef split(trained, metrics):\n    assert (trained / 'checkpoint.manifest.json').is_file()\n    return metrics['weight'], 120\n'''
    monkeypatch.setattr(fixture, 'HARNESS', source_text)
    source, recipe = fixture.source_recipe(tmp_path, 'multi-output', 3)
    train = recipe['nodes'][0]
    train['outputs'].append({'name': 'metrics', 'type': 'dict'})
    recipe['nodes'].append({'id': 'split', 'kind': 'code.python',
        'implementation_ref': {**train['implementation_ref'], 'entrypoint': 'split'},
        'inputs': [{'name': 'trained', 'type': 'Path'}, {'name': 'metrics', 'type': 'dict'}],
        'outputs': [{'name': 'weight', 'type': 'float'}, {'name': 'updates', 'type': 'int'}], 'parameters': {}})
    recipe['nodes'][-1].update({key: train[key] for key in ('environment_ref', 'resource_request', 'cache_policy', 'timeout', 'retry', 'provenance')})
    recipe['nodes'][-1]['provenance'] = {'intended_use': 'evaluate'}
    recipe['edges'] = [{'source_node': 'train', 'source_port': port, 'target_node': 'split', 'target_port': port}
                       for port in ('trained', 'metrics')]
    backend = ZenMLBackend(tmp_path / 'state', python_executable=str(fixture.PYTHON), source_root=source,
                           trusted_code_roots=[str(source)])
    service = OperationService(tmp_path / 'service.sqlite', backend=backend, source_root=source)
    def call(name, **payload):
        return service.invoke(name, payload, fixture.OWNER)
    try:
        project = call('project.create', slug='multiple', model_name='Multiple outputs', recipe=recipe, idempotency_key='project')
        fixture.register_source_review(service, project, source, recipe)
        saved = call('recipe.get', project_id=project['project_id'], recipe_id=project['recipe_id'])
        submitted = call('run.submit', project_id=project['project_id'], recipe_id=project['recipe_id'],
                         expected_revision=1, trusted=True, idempotency_key='run')
        run = fixture.wait(service, project, submitted)
        assert abs(run['metrics']['weight'] - 3) < 1e-9
        assert run['metrics']['loss'] < 1e-12
        artifacts = call('run.artifacts', project_id=project['project_id'], operation_id=submitted['operation_id'])['artifacts']
        assert {(item['node_id'], item['port']) for item in artifacts} == {('train', 'trained'), ('train', 'metrics'), ('split', 'weight'), ('split', 'updates')}
        assert all(item['artifact_id'] for item in artifacts)
        assert call('recipe.get', project_id=project['project_id'], recipe_id=project['recipe_id'], revision=1) == saved
        assert (Path(backend.root) / 'jobs' / submitted['operation_id'] / 'request.json').is_file()
    finally:
        service.store.close()
