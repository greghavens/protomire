"""Native Path materialization survives isolated source lifetime and cwd."""
import hashlib
import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration


def test_native_snapshot_and_relative_paths_match_sequential(tmp_path):
    spec = importlib.util.spec_from_file_location('native_path_harness', ROOT / 'tests/concurrency/test_native_parallel.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend, payload = module.setup(tmp_path, roots=2, torch=False)
    source = backend.repository
    implementation = source / 'paths.py'
    implementation.write_text('''from pathlib import Path
import os
import tempfile
TEMPORARY = tempfile.TemporaryDirectory()

def temporary_file() -> Path:
    path = Path(TEMPORARY.name) / "temporary.txt"
    path.write_text("module-owned temporary content")
    return path

def source_file() -> Path:
    return Path(__file__)

def source_directory() -> Path:
    return Path(__file__).parent

def relative_file(destination: str) -> Path:
    Path(destination).mkdir(parents=True, exist_ok=True)
    os.chdir(destination)
    Path('result.txt').write_text('relative output bytes')
    return Path('result.txt')

def consume(file: Path, directory: Path, relative: Path, temporary: Path) -> dict:
    assert file.read_bytes() == (directory / 'paths.py').read_bytes()
    return {'sha256': __import__('hashlib').sha256(file.read_bytes()).hexdigest(),
            'relative': relative.read_text(), 'temporary': temporary.read_text()}
''')
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'Pinned path return fixture'], check=True)
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    reference = {'path': 'paths.py', 'sha256': hashlib.sha256(implementation.read_bytes()).hexdigest()}
    payload['recipe']['source'].update(commit=commit, files=[reference])
    def node(name, inputs, outputs, parameters=None):
        return {'id': name, 'kind': 'code.python', 'implementation_ref': {**reference, 'module': 'native_paths', 'entrypoint': name, 'commit': commit},
            'inputs': [{'name': key, 'type': value} for key, value in inputs.items()],
            'outputs': [{'name': key, 'type': value} for key, value in outputs.items()],
            'parameters': parameters or {}, 'environment_ref': 'default', 'resource_request': {'gpu': 0},
            'timeout': 30, 'cache_policy': 'disabled', 'retry': {'max_attempts': 1}, 'provenance': {}}
    payload['recipe']['nodes'] = [node('source_file', {}, {'path': 'Path'}), node('source_directory', {}, {'path': 'Path'}),
        node('relative_file', {}, {'path': 'Path'}, {'destination': str(tmp_path / 'relative')}),
        node('temporary_file', {}, {'path': 'Path'}),
        node('consume', {'file': 'Path', 'directory': 'Path', 'relative': 'Path', 'temporary': 'Path'}, {'data': 'dict'})]
    payload['recipe']['edges'] = [{'source_node': name, 'source_port': 'path', 'target_node': 'consume', 'target_port': port}
        for name, port in [('source_file', 'file'), ('source_directory', 'directory'), ('relative_file', 'relative'), ('temporary_file', 'temporary')]]
    values = []
    for bound in (1, 2):
        payload['recipe']['execution'] = {'max_parallel_stages': bound}
        result, request = module.execute(backend, payload)
        assert result['status'] == 'succeeded', result
        values.append(module.data_value(result, 'consume'))
        artifact = module.data_artifact(result, 'source_file', 'path')
        assert (Path(artifact['uri']) / 'file_data').read_bytes() == implementation.read_bytes()
        if bound == 2:
            job = backend.root / 'jobs' / request['operation_id']
            paths = list((job / 'processes').glob('*/returned-paths/*/paths.py'))
            assert paths and all(path.read_bytes() == implementation.read_bytes() for path in paths)
    assert values == [{'sha256': reference['sha256'], 'relative': 'relative output bytes', 'temporary': 'module-owned temporary content'}] * 2
