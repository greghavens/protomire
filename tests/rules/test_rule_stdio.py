"""Independent official MCP client and separate HTTP process; no execution backend."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import httpx
import pytest
from modelbuilder.evaluation import digest
from test_rules import policy, evidence


@pytest.mark.integration
@pytest.mark.asyncio
async def test_official_stdio_policy_http_parity_restart_and_denial(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    spec = spec_from_file_location('rule_mcp_harness', root / 'scripts/mcp_smoke.py')
    harness = module_from_spec(spec)
    spec.loader.exec_module(harness)
    monkeypatch.setenv('MODELBUILDER_BACKEND', 'none')
    process = harness.ServiceProcess(tmp_path, owner_scopes=['read', 'modify'])
    process.start()
    try:
        async with httpx.AsyncClient(base_url=process.base_url,
                                    headers={'Authorization': 'Bearer ' + process.owner}, trust_env=False) as http:
            async with process.mcp() as session:
                await session.initialize()
                project = await session.call_tool('project_create', {'slug': 'rules-stdio', 'model_name': 'Rules', 'idempotency_key': 'p'})
                assert not project.isError
                pid = project.structuredContent['project_id']
                request = {'project_id': pid, 'policy': policy(), 'idempotency_key': 'register'}
                registered = await session.call_tool('rules_policy_register', request)
                assert not registered.isError
                row = registered.structuredContent
                assert row == (await http.post('/v1/operations/rules.policy.register', json=request)).json()
                e = evidence()
                e['subject']['project_id'] = pid
                for item in e['facts']['curve']['observations']:
                    item['subject_digest'] = digest(e['subject'])
                query = {'project_id': pid, 'policy_digest': row['policy_digest'], 'evidence': e}
                result = await session.call_tool('rules_evaluate', query)
                assert result.structuredContent['outcome'] == 'continue'
                assert result.structuredContent == (await http.post('/v1/operations/rules.evaluate', json=query)).json()
                conflict = await session.call_tool('rules_policy_register', {**request,
                    'policy': policy(threshold=.1), 'idempotency_key': 'changed-options'})
                assert conflict.isError and conflict.structuredContent['http_status'] == 409
                process.restart()
                assert (await session.call_tool('rules_policy_register', request)).structuredContent == row
                assert (await session.call_tool('rules_evaluate', query)).structuredContent == result.structuredContent
            async with process.mcp(readonly=True) as reader:
                await reader.initialize()
                denied = await reader.call_tool('rules_policy_register', request)
                assert denied.isError and denied.structuredContent['http_status'] == 403
    finally:
        process.stop()
