"""Verify output ABI against the pinned native SDK without importing user code."""
import pytest

pytest.importorskip('zenml')
from modelbuilder.backend.components.runtime import compile_component, compile_python


@pytest.mark.parametrize('kind', ['component.run', 'code.python'])
@pytest.mark.parametrize('count', [1, 2])
def test_native_shim_declares_each_output_port(tmp_path, kind, count):
    outputs = [{'name': 'checkpoint', 'type': 'Path'}, {'name': 'metrics', 'type': 'dict'}][:count]
    node = {'id': 'multiple_' + kind.replace('.', '_'), 'kind': kind, 'inputs': [], 'outputs': outputs, 'parameters': {},
            'implementation_ref': {'path': 'absent_user_code.py', 'module': 'absent_user_code', 'entrypoint': 'run',
                                   'commit': 'a' * 40, 'sha256': 'b' * 64},
            'component': {'id': 'multi-output', 'version': '1', 'protocol': 'modelbuilder.component/v1', 'role': 'utility',
                          'parameter_schema': {'type': 'object'}, 'capabilities': {'frameworks': [], 'resume_modes': [],
                          'export_formats': [], 'checkpoint_events': False}}}
    compiled = (compile_component if kind == 'component.run' else compile_python)(node, tmp_path)
    assert list(compiled.entrypoint_definition.outputs) == [port['name'] for port in outputs]
