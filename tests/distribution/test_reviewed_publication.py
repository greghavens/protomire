"""Managed exact publication over real Hub service, isolated fake provider only.

Collected when the Hub increment is integrated. No network, login or uploads.
"""
from copy import deepcopy
import hashlib
import threading
import time
from pathlib import Path
import sys
from uuid import uuid4

import pytest

pytest.importorskip('modelbuilder.hub', reason='The separate Hub increment is not integrated in this checkout')
from modelbuilder.hub import HubTransfers, TERMINAL
from modelbuilder.hub_provider import HubCancelled
from modelbuilder.models import Actor, ServiceError
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'distribution'))
import managed_core as core
from reviewed_requests import COMBINED_PUBLISH

FILES = {'README.md': b'---\nlicense: mit\n---\nAuthored fixture.', 'weights.bin': b'inert fixture bytes'}
USAGE = {'purpose': 'Offline managed review test', 'commercial': False, 'deployment': 'isolated fixture', 'conditions': ['No real upload']}


class Provider:
    def __init__(self):
        self.published = False
        self.calls = []
        self.block = False
        self.entered, self.release = threading.Event(), threading.Event()
    def validate_repository(self, repo_id, repo_type):
        assert repo_id == 'owner/model' and repo_type == 'model'
    def immutable_revision(self, revision):
        return isinstance(revision, str) and len(revision) == 40
    def capabilities(self):
        return {'id': 'fake', 'repo_types': ['model'], 'download': True, 'publish': True}
    def call(self, action, p, *, cancelled=lambda: False):
        self.calls.append((action, deepcopy(p)))
        if action == 'card.inspect':
            return {'license': 'mit'}
        if action == 'destination':
            return {'exists': True, 'repo_id': p['repo_id'], 'repo_type': p['repo_type'], 'commit': 'a'*40,
                    'private': True, 'gated': False, 'license': 'mit', 'access_state': 'available', 'files': []}
        if action == 'publish':
            assert p['parent_commit'] == 'a'*40
            for item in p['files']:
                assert hashlib.sha256((Path(p['snapshot_dir'])/item['path']).read_bytes()).hexdigest() == item['sha256']
            self.entered.set()
            if self.block:
                while not self.release.wait(.01):
                    if cancelled():
                        raise HubCancelled()
            if cancelled():
                raise HubCancelled()
            self.published = True
            return {'verified': True, 'commit': 'b'*40, 'url': 'https://example.invalid/no-real-upload'}
        raise AssertionError(action)


@pytest.fixture
def publication(tmp_path):
    app = core.make_application(tmp_path, tmp_path, {}, 'a'*64)
    service, queue, auth = app.state.service, app.state.review_queue, app.state.auth
    owner = auth.authenticate('Bearer '+'a'*64)
    # Explicit fixture publisher seeds a reviewed license; no user credential.
    publisher = Actor('fixture-publisher', {'read', 'modify', 'publish', 'execute'})
    auth._tokens['fixture-publisher-token'] = publisher
    project = service.invoke('project.create', {'model_name': 'Reviewed publication', 'slug': 'reviewed-publication', 'idempotency_key': 'create'}, owner)
    pid = project['project_id']
    source = tmp_path/'source'
    source.mkdir()
    for name, data in FILES.items():
        (source/name).write_bytes(data)
    provider = Provider()
    service.hub.close()
    service.hub = HubTransfers(service, providers={'fake': provider}, config={
        'accounts': {'publisher': {'keyring_service': 'unused', 'username': 'unused', 'project_ids': [pid]}},
        'publication_roots': {pid: {'output': str(source)}}})
    def invoke(operation, payload):
        return service.invoke(operation, {'project_id': pid, 'idempotency_key': str(uuid4()), **payload}, publisher)
    evidence = invoke('hub.license.local.inspect', {'source_alias': 'output', 'repo_type': 'model', 'files': list(FILES), 'max_bytes': 1024, 'declared_license': 'mit'})
    policy = invoke('hub.license.review', {'evidence_id': evidence['evidence_id'], 'evidence_digest': evidence['evidence_digest'],
        'license_document_paths': [doc['path'] for doc in evidence['evidence']['documents']], 'permitted_uses': ['download', 'publish'], 'intended_context': 'Authored fixture only', 'reviewed_usage_context': USAGE,
        'required_files': [], 'required_attributions': [], 'allowed_destination_licenses': ['mit'],
        'gated_terms_acknowledged': True, 'confirm_review': True})
    asset = invoke('hub.local.register', {'evidence_id': evidence['evidence_id'], 'license_policy_id': policy['policy_id']})
    preview = invoke('hub.publish.preview', {'usage_context': USAGE, 'provider': 'fake', 'repo_id': 'owner/model', 'repo_type': 'model',
        'account_alias': 'publisher', 'source_alias': 'output', 'files': list(FILES), 'max_bytes': 1024,
        'visibility': 'private', 'license': 'mit', 'source_asset_ids': [asset['asset_id']]})
    token = auth.issue_bridge('review-consumer', pid)
    consumer = auth.authenticate('Bearer '+token)
    request = queue.submit(consumer, pid, COMBINED_PUBLISH, {'project_id': pid, 'preview_id': preview['preview_id'], 'payload_digest': preview['payload_digest'], 'idempotency_key': 'publish-once'})
    yield app, service, queue, owner, consumer, request, provider
    provider.release.set()
    service.hub.close()
    service.store.close()


def finish(publication, result):
    _, service, queue, owner, _, request, _ = publication
    for _ in range(300):
        transfer = service.hub.status(result['result']['transfer_id'], request['project_id'])
        if transfer['state'] in TERMINAL:
            return queue.status(owner, request['id'])
        time.sleep(.01)
    pytest.fail('Offline fixture transfer did not finish')


def test_single_native_review_executes_checked_approve_start_once(publication):
    app, service, queue, owner, consumer, request, provider = publication
    frozen = request['disclosure']['preview']['payload']
    assert frozen['repo_id'] == 'owner/model' and frozen['visibility'] == 'private' and frozen['license'] == 'mit'
    assert frozen['parent_commit'] == 'a'*40 and all(item['sha256'] for item in frozen['files'])
    assert not provider.published
    result = queue.decide(owner, request['id'], request['review_digest'], 'approve')
    assert result['status'] == 'active'
    final = finish(publication, result)
    assert final['status'] == 'completed' and final['transfer']['state'] == 'succeeded'
    assert provider.published and len([a for a, _ in provider.calls if a == 'publish']) == 1
    assert not app.state.auth.authorize_operation('hub.publish.start', consumer, request['project_id'])
    with pytest.raises(ServiceError, match='already decided'):
        queue.decide(owner, request['id'], request['review_digest'], 'approve')
    audit = [item for item in service.store.history(request['project_id']) if item['operation'] in {'hub.publish.approve', 'hub.publish.start'}]
    assert audit and all(item['actor_id'] == consumer.id and item['request_id'] == request['id'] for item in audit)


def test_readonly_bearer_never_inherits_active_async_grant_and_revoke_reconciles(publication):
    app, service, queue, owner, consumer, request, provider = publication
    provider.block = True
    result = queue.decide(owner, request['id'], request['review_digest'], 'approve')
    assert provider.entered.wait(3)
    grant = queue.grants.active()[0]
    assert grant.actor.id == consumer.id and grant.actor is not consumer
    assert app.state.auth.authorize_operation('hub.publish.start', grant.actor, request['project_id'])
    assert not app.state.auth.authorize_operation('hub.publish.start', consumer, request['project_id'])
    queue.decide(owner, request['id'], request['review_digest'], 'revoke')
    final = finish(publication, result)
    assert final['status'] == 'revoked' and final['transfer']['state'] == 'needs_reconciliation'
    assert not provider.published and not queue.grants.active()


def test_changed_frozen_preview_denied_before_publication(publication):
    _, service, queue, owner, _, request, provider = publication
    preview = request['disclosure']['preview']
    altered = deepcopy(preview)
    altered['payload']['visibility'] = 'public'
    altered['revision'] += 1
    service.store.put('hub_preview', preview['preview_id'], request['project_id'], altered, preview['revision'])
    with pytest.raises(ServiceError, match='changed after review'):
        queue.decide(owner, request['id'], request['review_digest'], 'approve')
    assert not provider.published


def test_generated_approval_mismatch_never_starts_upload(publication, monkeypatch):
    _, service, queue, owner, _, request, provider = publication
    original = service.invoke
    def altered(operation, payload, actor):
        result = original(operation, payload, actor)
        return {**result, 'payload_digest': 'c'*64} if operation == 'hub.publish.approve' else result
    monkeypatch.setattr(service, 'invoke', altered)
    result = queue.decide(owner, request['id'], request['review_digest'], 'approve')
    assert result['status'] == 'failed' and result['error']['code'] == 'publication_approval_mismatch'
    assert not provider.published and not service.store.list('hub_transfer', request['project_id'])


def test_approval_only_operation_does_not_imply_upload(publication):
    _, service, queue, owner, consumer, combined, provider = publication
    payload = {**combined['payload'], 'confirm_publication': True, 'idempotency_key': 'approval-only'}
    request = queue.submit(consumer, combined['project_id'], 'hub.publish.approve', payload)
    result = queue.decide(owner, request['id'], request['review_digest'], 'approve')
    assert result['status'] == 'succeeded' and result['result']['consumed'] is False
    assert not provider.published and not service.store.list('hub_transfer', request['project_id'])


def test_license_policy_review_requires_native_approval_and_discloses_actual_terms(publication):
    _, service, queue, owner, consumer, combined, _ = publication
    previous = service.store.list('hub_license_policy', combined['project_id'])[0]
    from modelbuilder.hub_contract import POLICY_FIELDS
    payload = {key: previous[key] for key in POLICY_FIELDS if key != 'confirm_review'}
    payload.update(project_id=combined['project_id'], evidence_id=previous['evidence_id'],
                   evidence_digest=previous['license_evidence_digest'], confirm_review=True, idempotency_key='license-review')
    count = len(service.store.list('hub_license_policy', combined['project_id']))
    request = queue.submit(consumer, combined['project_id'], 'hub.license.review', payload)
    assert request['required_scopes'] == ['publish', 'read']
    evidence = request['disclosure']['license_evidence']
    assert evidence['evidence']['documents'] and evidence['evidence_digest'] == payload['evidence_digest']
    assert len(service.store.list('hub_license_policy', combined['project_id'])) == count
    result = queue.decide(owner, request['id'], request['review_digest'], 'approve')
    assert result['status'] == 'succeeded' and result['result']['reviewed_by'] == consumer.id
    assert len(service.store.list('hub_license_policy', combined['project_id'])) == count + 1
    assert consumer.scopes == frozenset({'read'})


def test_compound_input_uses_authoritative_publication_schema(publication):
    _, _, queue, _, consumer, request, provider = publication
    with pytest.raises(ServiceError) as failed:
        queue.submit(consumer, request['project_id'], COMBINED_PUBLISH, {**request['payload'], 'idempotency_key': {}})
    assert failed.value.code == 'validation_error'
    assert not provider.published
