"""Exact-request authorization against the real operation service, offline only."""
import json
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'distribution'))
import managed_core as core
from reviewed_requests import ReviewQueue, ReviewGrants, Grant, digest
from modelbuilder.models import Actor, ServiceError


@pytest.fixture
def harness(tmp_path):
    app = core.make_application(tmp_path, tmp_path, {}, 'a' * 64)
    owner = app.state.auth.authenticate('Bearer ' + 'a' * 64)
    service = app.state.service
    project = service.invoke('project.create', {'model_name': 'Review fixture', 'slug': 'review-fixture', 'idempotency_key': 'create'}, owner)
    token = app.state.auth.issue_bridge('consumer', project['project_id'])
    consumer = app.state.auth.authenticate('Bearer ' + token)
    queue = app.state.review_queue
    yield app, service, queue, owner, consumer, project
    service.store.close()


def update(project, key='update'):
    return {'project_id': project['project_id'], 'expected_revision': project['revision'],
            'changes': {'description': 'Reviewed exactly'}, 'idempotency_key': key}


def submit(harness, key='update'):
    _, _, queue, _, consumer, project = harness
    return queue.submit(consumer, project['project_id'], 'project.update', update(project, key))


def error(code, fn):
    with pytest.raises(ServiceError) as caught:
        fn()
    assert caught.value.code == code


def test_exact_request_approval_mutates_once_with_distinct_audit(harness):
    app, service, queue, owner, consumer, project = harness
    request = submit(harness)
    assert service.store.get('project', project['project_id'])['revision'] == 1
    assert request['status'] == 'pending' and request['payload']['request_id'] == request['id']
    error('forbidden', lambda: service.invoke('project.update', update(project), consumer))
    error('forbidden', lambda: queue.decide(consumer, request['id'], request['review_digest'], 'approve'))
    result = queue.decide(owner, request['id'], request['review_digest'], 'approve')
    assert result['status'] == 'succeeded' and result['result']['revision'] == 2
    assert result['executed_actor'] == consumer.id and result['approved_by'] == owner.id
    error('review_already_decided', lambda: queue.decide(owner, request['id'], request['review_digest'], 'approve'))
    assert queue.submit(consumer, project['project_id'], 'project.update', update(project))['id'] == request['id']
    audits = service.store.history(project['project_id'])
    write = next(row for row in audits if row['operation'] == 'project.update' and row['outcome'] == 'succeeded')
    approved = next(row for row in audits if row['operation'] == 'desktop.review' and row['outcome'] == 'approved')
    assert write['request_id'] == request['id'] == approved['request_id']
    assert write['actor_id'] == consumer.id and approved['actor_id'] == owner.id
    assert approved['details']['requested_by'] == consumer.id
    assert 'a' * 64 not in json.dumps(result)
    assert not queue.grants.active()
    error('forbidden', lambda: service.invoke('project.update', update({**project, 'revision': 2}, 'later'), consumer))


@pytest.mark.parametrize('decision,expected', [('deny', 'denied'), ('revoke', 'revoked'), ('cancel', 'withdrawn')])
def test_deny_revoke_cancel_never_mutate(harness, decision, expected):
    _, service, queue, owner, consumer, project = harness
    request = submit(harness)
    result = queue.cancel(consumer, request['id']) if decision == 'cancel' else queue.decide(owner, request['id'], request['review_digest'], decision)
    assert result['status'] == expected
    assert service.store.get('project', project['project_id'])['revision'] == 1
    error('review_already_decided', lambda: queue.decide(owner, request['id'], request['review_digest'], 'approve'))


def test_payload_digest_idempotency_and_project_cannot_change(harness):
    _, service, queue, owner, consumer, project = harness
    request = submit(harness)
    error('review_changed', lambda: queue.decide(owner, request['id'], 'b' * 64, 'approve'))
    error('review_idempotency_conflict', lambda: queue.submit(consumer, project['project_id'], 'project.update', {**update(project), 'changes': {'description': 'Changed'}}))
    other = service.invoke('project.create', {'model_name': 'Other', 'slug': 'other', 'idempotency_key': 'other'}, owner)
    error('forbidden', lambda: queue.submit(consumer, other['project_id'], 'project.update', update(other)))
    error('review_project_mismatch', lambda: queue.submit(consumer, project['project_id'], 'project.update', update(other)))
    other_actor = Actor(id=consumer.id, scopes=frozenset({'read'}), project_ids=frozenset({other['project_id']}))
    error('forbidden', lambda: queue.status(other_actor, request['id']))
    assert service.store.get('project', project['project_id'])['revision'] == 1


def test_expiry_stale_project_and_restart_do_not_execute(harness):
    _, service, queue, owner, consumer, project = harness
    request = submit(harness)
    queue.clock = lambda: request['expires_at'] + 1
    error('review_expired', lambda: queue.decide(owner, request['id'], request['review_digest'], 'approve'))
    assert queue.status(consumer, request['id'])['status'] == 'expired'
    queue.clock = lambda: request['created_at']
    stale = submit(harness, 'stale')
    service.invoke('project.update', {**update(project, 'owner-update'), 'changes': {'description': 'Owner changed it'}}, owner)
    error('review_context_changed', lambda: queue.decide(owner, stale['id'], stale['review_digest'], 'approve'))
    restarted = ReviewQueue(service, ReviewGrants())
    assert restarted.status(owner, stale['id'])['status'] == 'revoked'
    assert service.store.get('project', project['project_id'])['description'] == 'Owner changed it'


def test_simultaneous_native_approvals_dispatch_at_most_once(harness):
    _, service, queue, owner, _, project = harness
    request = submit(harness)
    def decide():
        try:
            return queue.decide(owner, request['id'], request['review_digest'], 'approve')['status']
        except ServiceError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: decide(), range(2)))
    assert sorted(results) == ['review_already_decided', 'succeeded']
    assert service.store.get('project', project['project_id'])['revision'] == 2


def test_actor_identity_does_not_elevate_same_id_readonly_bearer(harness):
    app, service, queue, _, consumer, project = harness
    payload = update(project)
    approved = Actor(id=consumer.id, scopes=frozenset({'modify', 'read'}), project_ids=consumer.project_ids)
    grant = Grant(approved, 'exact', project['project_id'], {'project.update': digest(payload)}, queue.clock()+30, queue.snapshot(project['project_id']))
    queue.grants.add(grant)
    assert app.state.auth.authorize_operation('project.update', approved, project['project_id'])
    assert not app.state.auth.authorize_operation('project.update', consumer, project['project_id'])
    error('review_payload_changed', lambda: service.invoke('project.update', {**payload, 'changes': {'description': 'injected'}}, approved))
    service.invoke('project.update', payload, approved)
    grant.dispatched = True
    error('review_replayed', lambda: service.invoke('project.update', payload, approved))
    queue.grants.remove(grant)
    error('review_grant_revoked', lambda: service.invoke('project.update', payload, approved))


def test_revocation_between_native_decision_and_dispatch_fails_closed(harness, monkeypatch):
    _, service, queue, owner, _, project = harness
    request = submit(harness)
    entered, released = threading.Event(), threading.Event()
    original = service.invoke
    def blocked(operation, payload, actor):
        entered.set()
        assert released.wait(3)
        return original(operation, payload, actor)
    monkeypatch.setattr(service, 'invoke', blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(queue.decide, owner, request['id'], request['review_digest'], 'approve')
        assert entered.wait(3)
        revoked = queue.decide(owner, request['id'], request['review_digest'], 'revoke')
        assert revoked['status'] == 'revoking'
        released.set()
        result = pending.result(3)
    assert result['status'] == 'failed' and result['error']['code'] == 'review_grant_revoked'
    assert service.store.get('project', project['project_id'])['revision'] == 1


def test_dispatch_rechecks_stale_project_inside_transaction(harness, monkeypatch):
    _, service, queue, owner, _, project = harness
    request = submit(harness)
    original = service._dispatch
    def changed(operation, payload, actor, **kwargs):
        changed_project = service.store.get('project', project['project_id'])
        changed_project['revision'] += 1
        # Simulate an intervening writer before the subclass admission check.
        service.store.connection.execute("UPDATE resources SET body=? WHERE kind='project' AND id=?", (json.dumps(changed_project), project['project_id']))
        return original(operation, payload, actor, **kwargs)
    monkeypatch.setattr(service, '_dispatch', changed)
    result = queue.decide(owner, request['id'], request['review_digest'], 'approve')
    assert result['status'] == 'failed' and result['error']['code'] == 'review_context_changed'
    assert service.store.get('project', project['project_id'])['revision'] == 1


def test_async_capability_expiry_revoke_and_terminal_cleanup(harness):
    app, service, queue, owner, consumer, project = harness
    request = submit(harness)
    approved = Actor(id=consumer.id, scopes=frozenset({'modify', 'read'}), project_ids=consumer.project_ids)
    grant = Grant(approved, request['id'], project['project_id'], {'project.update': request['payload_digest']}, queue.clock()+30, request['snapshot'], dispatched=True, transfer_id='transfer')
    queue.grants.add(grant)
    service.store.put('hub_transfer', 'transfer', project['project_id'], {'state': 'running'})
    with service.store.transaction():
        queue._save({**request, 'status': 'active'})
    assert queue.has_active_transfers()
    assert app.state.auth.authorize_operation('project.update', approved, project['project_id'])
    queue.decide(owner, request['id'], request['review_digest'], 'revoke')
    assert not app.state.auth.authorize_operation('project.update', approved, project['project_id'])
    assert not app.state.auth.authorize_operation('project.update', consumer, project['project_id'])
    service.store.put('hub_transfer', 'transfer', project['project_id'], {'state': 'needs_reconciliation', 'revision': 2}, expected_revision=1)
    assert not queue.has_active_transfers()
    assert queue.status(owner, request['id'])['status'] == 'revoked'
    assert queue.status(owner, request['id'])['transfer']['state'] == 'needs_reconciliation'


def test_standing_schedule_is_not_authorized_by_one_shot_review(harness):
    _, _, queue, _, consumer, project = harness
    error('standing_review_required', lambda: queue.submit(consumer, project['project_id'], 'research.watch.schedule_enable', {}))


@pytest.mark.parametrize('revoke', [True, False])
def test_execution_request_reauthenticates_before_native_launch(harness, revoke):
    from copy import deepcopy
    from modelbuilder.recipe import default_recipe
    _, service, queue, owner, _, _ = harness
    class InertBackend:
        def __init__(self):
            self.entered, self.release = threading.Event(), threading.Event()
            self.launches = []
        def prepare_execution(self, operation, payload):
            self.entered.set()
            assert self.release.wait(3)
            return operation, deepcopy(payload)
        def launch_prepared(self, handle, *, authorize):
            authorize()
            self.launches.append(handle)
            return {'backend_run_id': 'inert-native', 'status': 'submitted'}
    backend = InertBackend()
    service.backend = backend
    recipe = default_recipe('template', 'template', 'review-admission')
    recipe['nodes'] = [{'id': 'train', 'kind': 'model.train', 'implementation_ref': 'builtin:model.train',
        'inputs': [], 'outputs': [], 'parameters': {'steps': 4, 'learning_rate': .1}, 'environment_ref': 'default',
        'resource_request': {}, 'retry': {'max_attempts': 1, 'backoff_seconds': 0}, 'timeout': 30,
        'cache_policy': 'disabled', 'provenance': {}}]
    project = service.invoke('project.create', {'slug': 'execution', 'model_name': 'Execution', 'recipe': recipe, 'idempotency_key': 'execution'}, owner)
    consumer = Actor('desktop-assistant:execution', {'read'}, {project['project_id']})
    payload = {'project_id': project['project_id'], 'recipe_id': project['recipe_id'], 'expected_revision': 1, 'trusted': True, 'idempotency_key': 'one-admission'}
    request = queue.submit(consumer, project['project_id'], 'run.submit', payload)
    assert not backend.launches
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(queue.decide, owner, request['id'], request['review_digest'], 'approve')
        assert backend.entered.wait(3)
        if revoke:
            queue.decide(owner, request['id'], request['review_digest'], 'revoke')
        backend.release.set()
        result = pending.result(3)
    assert len(backend.launches) == (0 if revoke else 1)
    assert result['status'] in ({'failed', 'revoked'} if revoke else {'succeeded'})
    # Authorization does not silently cancel an already submitted native job;
    # the existing dispatch receipt records that this one never reached launch.
    operations = service.store.list('operation', project['project_id'])
    assert operations
    assert all((item['backend_run_id'] is None) == revoke for item in operations)


def test_proposal_review_shows_real_staged_change_and_rejects_replacement(harness):
    _, service, queue, owner, consumer, project = harness
    proposal = {'project_id': project['project_id'], 'proposal_id': 'proposal-fixture', 'revision': 1,
                'diff': {'before': 'old content', 'after': 'exact new content'}, 'status': 'pending'}
    service.store.put('research_proposal', 'proposal-fixture', project['project_id'], proposal)
    payload = {'project_id': project['project_id'], 'proposal_id': 'proposal-fixture', 'expected_revision': 1,
               'outcome': 'approved', 'rationale': 'Review exactly this change', 'idempotency_key': 'review-proposal'}
    request = queue.submit(consumer, project['project_id'], 'research.proposal.review', payload)
    assert request['disclosure']['proposal']['diff'] == proposal['diff']
    service.store.put('research_proposal', 'proposal-fixture', project['project_id'], {**proposal, 'revision': 2, 'diff': {}}, expected_revision=1)
    error('review_material_changed', lambda: queue.decide(owner, request['id'], request['review_digest'], 'approve'))


def test_async_authority_expiry_stops_callbacks_without_extending_readonly_actor(harness):
    app, service, queue, owner, consumer, project = harness
    request = submit(harness)
    actor = Actor(consumer.id, {'read', 'modify'}, consumer.project_ids)
    expires = queue.clock() + 10
    grant = Grant(actor, request['id'], project['project_id'], {'project.update': request['payload_digest']}, expires, request['snapshot'], dispatched=True, transfer_id='expires')
    queue.grants.add(grant)
    service.store.put('hub_transfer', 'expires', project['project_id'], {'state': 'running'})
    with service.store.transaction():
        queue._save({**request, 'status': 'active'})
    assert app.state.auth.authorize_operation('project.update', actor, project['project_id'])
    queue.clock = queue.grants.clock = lambda: expires + 1
    assert not app.state.auth.authorize_operation('project.update', actor, project['project_id'])
    assert queue.status(owner, request['id'])['status'] == 'revoking'
    assert not app.state.auth.authorize_operation('project.update', consumer, project['project_id'])
    service.store.put('hub_transfer', 'expires', project['project_id'], {'state': 'cancelled', 'revision': 2}, expected_revision=1)
    assert queue.status(owner, request['id'])['status'] == 'revoked'
    assert not queue.has_active_transfers()


@pytest.mark.parametrize('operation', ['research.watch.refresh', 'research.watch.refresh_due'])
@pytest.mark.parametrize('revoke', [False, True])
def test_manual_research_request_context_never_elevates_scheduled_or_readonly_actor(harness, monkeypatch, operation, revoke):
    app, service, queue, owner, consumer, project = harness
    watch = service.invoke('research.watch.create', {'project_id': project['project_id'], 'name': 'Bounded query', 'query': 'fixture only', 'idempotency_key': 'watch'}, owner)
    payload = {'project_id': project['project_id'], 'idempotency_key': 'refresh'}
    if operation.endswith('refresh'):
        payload.update(watch_id=watch['watch_id'], expected_revision=watch['revision'])
    else:
        payload.update(max_watches=1)
    request = queue.submit(consumer, project['project_id'], operation, payload)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def fetch(query, limit):
        calls.append((query, limit))
        entered.set()
        assert release.wait(3)
        return []
    monkeypatch.setattr('modelbuilder.literature.search_openalex', fetch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(queue.decide, owner, request['id'], request['review_digest'], 'approve')
        assert entered.wait(3)
        # This thread has no request-local Actor capability. Matching actor ID
        # cannot grant the scheduler or another HTTP bearer modify authority.
        assert not app.state.auth.authorize_research(consumer.id, project['project_id'])
        if revoke:
            queue.decide(owner, request['id'], request['review_digest'], 'revoke')
        release.set()
        result = pending.result(3)
    assert calls == [('fixture only', watch['limit'])]
    assert result['status'] == ('revoked' if revoke else 'succeeded')
    refreshes = service.store.list('research_refresh', project['project_id'])
    assert len(refreshes) == 1 and refreshes[0]['status'] == ('discarded' if revoke else 'succeeded')
    assert not app.state.auth.authorize_research(consumer.id, project['project_id'])


@pytest.mark.parametrize('operation', ['research.text.fetch', 'research.document.import'])
@pytest.mark.parametrize('revoke', [False, True])
def test_content_import_exact_capability_reauthenticates_before_retaining_text(harness, monkeypatch, operation, revoke):
    from modelbuilder.research_content import ContentRetrieval
    from modelbuilder.research_documents import DocumentImport
    app, service, queue, owner, consumer, project = harness
    evidence = service.invoke('evidence.add', {'project_id': project['project_id'], 'title': 'Authored fixture', 'source_identifier': 'PMC123', 'idempotency_key': 'evidence'}, owner)
    payload = {'project_id': project['project_id'], 'evidence_id': evidence['evidence_id'], 'expected_evidence_revision': evidence['revision'], 'idempotency_key': 'content'}
    if operation.endswith('fetch'):
        payload.update(provider='europe_pmc', source_identifier='PMC123')
        decoder = ContentRetrieval
    else:
        payload.update(filename='inert-fixture.pdf', media_type='application/pdf', content_base64='QQ==', expected_sha256='a'*64,
                       license='Authored fixture permission', rights_basis='self_authored', rights_confirmed=True)
        decoder = DocumentImport
    request = queue.submit(consumer, project['project_id'], operation, payload)
    assert request['disclosure']['evidence']['title'] == 'Authored fixture'
    entered, release = threading.Event(), threading.Event()
    def prepare(self, payload, evidence):
        entered.set()
        assert release.wait(3)
        return {'text': 'Inert decoded fixture', 'license': 'Authored fixture permission', 'rights_basis': 'self_authored',
                'provenance': {'raw_sha256': 'a'*64, 'extractor': 'test-only-no-external-call', 'tools': {}, 'extraction_options': {}}}
    monkeypatch.setattr(decoder, 'prepare', prepare)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(queue.decide, owner, request['id'], request['review_digest'], 'approve')
        assert entered.wait(3)
        assert not app.state.auth.authorize_research(consumer.id, project['project_id'])
        if revoke:
            queue.decide(owner, request['id'], request['review_digest'], 'revoke')
        release.set()
        result = pending.result(3)
    assert result['status'] == ('failed' if revoke else 'succeeded')
    assert len(service.store.list('research_text', project['project_id'])) == (0 if revoke else 1)
    assert not app.state.auth.authorize_research(consumer.id, project['project_id'])
