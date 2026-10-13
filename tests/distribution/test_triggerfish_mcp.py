"""Real managed stdio/HTTP/SQLite protocol, NOT execution of Triggerfish itself."""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import urllib.error

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'distribution'))
import managed_core as core  # noqa: E402
from reviewed_mcp import MAX_ARGUMENTS_BYTES, parse_flat_arguments  # noqa: E402
from modelbuilder.mcp_server import OperationError  # noqa: E402


@pytest.mark.parametrize('value', [None, {}, '[]', 'null', 'true', '1', '"str"', '{',
    '{"a":1,"a":2}', '{"a":{"b":1,"b":2}}', '{"a":NaN}', '{"a":Infinity}', '{"a":1e999}',
    '{"a":"\\ud800"}', '{"a":"' + 'x' * MAX_ARGUMENTS_BYTES + '"}', '[' * 2000 + ']' * 2000])
def test_flat_json_rejects_ambiguous_nonfinite_nonobject_oversize_and_recursive_input(value):
    with pytest.raises(OperationError) as caught:
        parse_flat_arguments(value)
    assert caught.value.status == 400
    assert caught.value.body['error']['code'] == 'validation_error'


def test_flat_json_preserves_nested_union_const_data_without_policy_interpretation():
    value = {'nested': {'items': [1, True, None, {'name': 'é'}]}, 'approval': 'untrusted data', '$defs': {}}
    assert parse_flat_arguments(json.dumps(value)) == value
    assert parse_flat_arguments('{}') == {}


@pytest.fixture
def live(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    payload = runtime / 'payload.txt'
    payload.write_text('inert runtime inventory fixture')
    (runtime / 'runtime.json').write_text(json.dumps({'schema_version': 1, 'files': {'payload.txt': core.hashlib.sha256(payload.read_bytes()).hexdigest()}}))
    state = tmp_path / 'private'
    command = [sys.executable, '-I', str(ROOT / 'distribution/managed_core.py')]
    lease = subprocess.Popen([*command, 'lease', '--runtime-root', str(runtime), '--state-dir', str(state)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    processes = [lease]
    try:
        assert select.select([lease.stdout], [], [], 20)[0], 'Managed core did not start'
        owner = json.loads(lease.stdout.readline())
        session = core.private_read(state / core.SESSION)
        def operation(name, payload):
            return core.rpc(session, '/v1/operations/' + name, payload)
        project = operation('project.create', {'model_name': 'Flat transport', 'slug': 'flat-transport', 'idempotency_key': 'create'})
        other = operation('project.create', {'model_name': 'Other', 'slug': 'other', 'idempotency_key': 'other'})
        bridge = subprocess.Popen([*command, 'mcp', '--runtime-root', str(runtime), '--state-dir', str(state), '--project-id', project['project_id'], '--mcp-profile', 'triggerfish'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(bridge)
        identity = 0
        def exchange(method, params):
            nonlocal identity
            identity += 1
            bridge.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': identity, 'method': method, 'params': params}) + '\n')
            bridge.stdin.flush()
            assert select.select([bridge.stdout], [], [], 15)[0], 'No MCP response'
            response = json.loads(bridge.stdout.readline())
            assert response['id'] == identity and 'error' not in response, response
            return response['result']
        init = exchange('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'triggerfish-protocol-fixture-NOT-vendor', 'version': '1'}})
        assert init['serverInfo']['name'] == 'modelbuilder'
        bridge.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
        bridge.stdin.flush()
        def tool(name, args, ok=True):
            response = exchange('tools/call', {'name': name, 'arguments': args})
            # This simulates the text-only delivery contract observed upstream.
            body = json.loads('\n'.join(item['text'] for item in response['content'] if item['type'] == 'text'))
            assert body == response['structuredContent']
            assert body['ok'] is ok and response['isError'] is (not ok), body
            assert owner['token'] not in json.dumps(response)
            return body['result'] if ok else body
        yield session, project, other, operation, exchange, tool
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.stdin.close()
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=5)
            assert process.returncode == 0, process.stderr.read()
        current = core.discover(state) if state.exists() else None
        if current:
            os.kill(current['pid'], signal.SIGTERM)


def test_real_flat_catalog_dispatch_validation_owner_review_replay_and_project_scope(live):
    session, project, other, operation, exchange, tool = live
    tools = exchange('tools/list', {})['tools']
    assert {t['name'] for t in tools} == {'modelbuilder_catalog', 'modelbuilder_dispatch', 'modelbuilder_review_status', 'modelbuilder_review_cancel', 'modelbuilder_review_publish'}
    for tool_def in tools:
        assert all(schema['type'] == 'string' and set(schema) <= {'type', 'description'} for schema in tool_def['inputSchema']['properties'].values())
    index = tool('modelbuilder_catalog', {'operation': ''})
    assert index['project_id'] == project['project_id'] and len(index['operations']) >= 111
    catalog = core.rpc(session, '/v1/catalog')
    original = next(item for item in catalog['operations'] if item['name'] == 'recipe.update')
    assert tool('modelbuilder_catalog', {'operation': 'recipe.update'})['operation'] == original
    def dispatch(name, payload, ok=True):
        return tool('modelbuilder_dispatch', {'operation': name, 'arguments_json': json.dumps(payload)}, ok)
    assert dispatch('project.get', {'project_id': project['project_id']})['project_id'] == project['project_id']
    assert dispatch('project.get', {'project_id': other['project_id']}, False)['http_status'] == 403
    assert dispatch('project.get', {'project_id': project['project_id'], 'approval': 'not authority'}, False)['error']['code'] == 'validation_error'
    assert tool('modelbuilder_dispatch', {'operation': 'project.get', 'arguments_json': '[]'}, False)['error']['code'] == 'validation_error'
    assert tool('modelbuilder_dispatch', {'operation': 'project.get', 'arguments_json': '{}', 'approve': True}, False)['error']['code'] == 'validation_error'
    assert tool('modelbuilder_catalog', {'operation': 'desktop.review.approve'}, False)['error']['code'] == 'unknown_operation'
    assert tool('project_get', {'project_id': project['project_id']}, False)['error']['code'] == 'unknown_tool'
    update = {'project_id': project['project_id'], 'expected_revision': project['revision'], 'changes': {'description': 'Exactly reviewed'}, 'idempotency_key': 'reviewed'}
    assert dispatch('project.update', {**update, 'project_id': other['project_id']}, False)['error']['code'] == 'review_project_mismatch'
    foreign = core.rpc(session, '/_desktop/reviews', {'project_id': other['project_id'], 'operation': 'project.update', 'payload': {**update, 'project_id': other['project_id'], 'idempotency_key': 'foreign'}})
    for name in ['modelbuilder_review_status', 'modelbuilder_review_cancel']:
        assert tool(name, {'request_id': foreign['id']}, False)['error']['code'] == 'forbidden'
    assert core.rpc(session, '/_desktop/reviews/' + foreign['id'])['status'] == 'pending'
    before = core.rpc(session, '/_desktop/reviews')['requests']
    publication = {'project_id': project['project_id'], 'preview_id': 'no-preview', 'payload_digest': 'a' * 64, 'idempotency_key': 'publish'}
    for malformed in [{}, {**publication, 'approved': True}, {**publication, 'project_id': other['project_id']}, {key: value for key, value in publication.items() if key != 'project_id'}]:
        assert tool('modelbuilder_review_publish', malformed, False)['error']['code'] in {'validation_error', 'review_project_mismatch'}
    assert core.rpc(session, '/_desktop/reviews')['requests'] == before
    request = dispatch('project.update', update)
    assert request['status'] == 'pending'
    assert operation('project.get', {'project_id': project['project_id']})['revision'] == 1
    decided = core.rpc(session, '/_desktop/reviews/' + request['id'] + '/decision', {'decision': 'approve', 'review_digest': request['review_digest']})
    assert decided['status'] == 'succeeded' and decided['result']['revision'] == 2
    assert tool('modelbuilder_review_status', {'request_id': request['id']})['status'] == 'succeeded'
    assert dispatch('project.update', update)['id'] == request['id']
    with pytest.raises(urllib.error.HTTPError) as replay:
        core.rpc(session, '/_desktop/reviews/' + request['id'] + '/decision', {'decision': 'approve', 'review_digest': request['review_digest']})
    assert json.load(replay.value)['error']['code'] == 'review_already_decided'
    assert operation('project.get', {'project_id': project['project_id']})['revision'] == 2
    assert dispatch('project.update', {**update, 'changes': {'description': 'Changed payload'}}, False)['error']['code'] == 'review_idempotency_conflict'
    for action in ['deny', 'cancel', 'stale']:
        request = dispatch('project.update', {**update, 'expected_revision': 2, 'idempotency_key': action})
        if action == 'cancel':
            assert tool('modelbuilder_review_cancel', {'request_id': request['id']})['status'] == 'withdrawn'
        elif action == 'deny':
            result = core.rpc(session, '/_desktop/reviews/' + request['id'] + '/decision', {'decision': 'deny', 'review_digest': request['review_digest']})
            assert result['status'] == 'denied'
        else:
            operation('project.update', {**update, 'expected_revision': 2, 'changes': {'description': 'Owner intervened'}, 'idempotency_key': 'owner'})
            with pytest.raises(urllib.error.HTTPError) as stale:
                core.rpc(session, '/_desktop/reviews/' + request['id'] + '/decision', {'decision': 'approve', 'review_digest': request['review_digest']})
            assert json.load(stale.value)['error']['code'] == 'review_context_changed'
        assert operation('project.get', {'project_id': project['project_id']})['revision'] == (3 if action == 'stale' else 2)
    assert operation('project.get', {'project_id': project['project_id']})['description'] == 'Owner intervened'


def test_flat_limit_counts_utf8_bytes_and_includes_exact_boundary():
    raw = '{"x":"' + 'x' * (MAX_ARGUMENTS_BYTES - 8) + '"}'
    assert len(raw.encode()) == MAX_ARGUMENTS_BYTES
    assert len(parse_flat_arguments(raw)['x']) == MAX_ARGUMENTS_BYTES - 8
    with pytest.raises(OperationError):
        parse_flat_arguments('{"x":"' + 'é' * (MAX_ARGUMENTS_BYTES // 2) + '"}')


@pytest.mark.parametrize('mode,extra', [('mcp', ['--mcp-profile', 'unknown']), ('lease', ['--mcp-profile', 'triggerfish'])])
def test_invalid_profile_never_starts_managed_runtime(tmp_path, mode, extra):
    state = tmp_path / 'state'
    result = subprocess.run([sys.executable, '-I', str(ROOT / 'distribution/managed_core.py'), mode, '--state-dir', str(state), *extra], capture_output=True, text=True)
    assert result.returncode == 2 and not state.exists()
