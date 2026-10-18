"""Generated harness configs cold-start the real MCP/core with no IDE lease."""
import json
from pathlib import Path
import select
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'distribution'))
import assistant_project as project_setup  # noqa: E402
from tests.distribution.test_managed_core import runtime as registered_runtime, state as registered_state, lease, call, close, until_stopped  # noqa: E402

runtime = registered_runtime
state = registered_state


@pytest.mark.parametrize('client', ['claude','codex','opencode','kilocode'])
def test_generated_config_starts_real_mcp_without_ide(runtime, state, tmp_path, client):
    ide, owner = lease(runtime, state)
    project = call(owner, 'project.create', {'model_name':'Existing model','slug':'existing','idempotency_key':'existing'})
    close(ide)
    until_stopped(state)
    workspace = tmp_path/'model-repo'
    workspace.mkdir()
    command = {'command':sys.executable,'args':['-I','-B',str(ROOT/'distribution/managed_core.py'),'mcp',
               '--runtime-root',str(runtime),'--state-dir',str(state),'--project-id',project['project_id']]}
    preview, files = project_setup.preview_project(client, workspace, project['project_id'], command=command)
    project_setup.apply_project(preview, files, preview['approval'])
    entry = preview['plan']['entry']
    vector = entry['command'] if client in {'opencode','kilocode'} else [entry['command'],*entry['args']]
    mcp = subprocess.Popen(vector,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    def exchange(message):
        mcp.stdin.write(json.dumps(message)+'\n')
        mcp.stdin.flush()
        assert select.select([mcp.stdout],[],[],20)[0], 'MCP cold start did not respond'
        return json.loads(mcp.stdout.readline())
    try:
        initialized = exchange({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'configuration-transport-test','version':'1'}}})
        assert initialized['result']['serverInfo']['name'] == 'modelbuilder'
        mcp.stdin.write(json.dumps({'jsonrpc':'2.0','method':'notifications/initialized'})+'\n')
        mcp.stdin.flush()
        result = exchange({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'project_get','arguments':{'project_id':project['project_id']}}})
        assert not result['result']['isError']
        assert result['result']['structuredContent']['project_id'] == project['project_id']
        assert owner['token'] not in json.dumps(result)
    finally:
        close(mcp)
    until_stopped(state)
