"""Shared project instructions retain exact block ownership across clients."""
from itertools import permutations
import json

import pytest

from tests.assistant_setup.test_project_setup import command as registered_command
import assistant_project as project
import assistant_setup as setup

command = registered_command
CLIENTS = ('codex', 'claude', 'opencode', 'kilocode')
OWNER = b'Owner instructions stay byte-exact.\r\n'


def configure(root, command, client):
    preview, files = project.preview_project(client, root, 'project-1', command=command)
    result = project.apply_project(preview, files, preview['approval'])
    assert result['credentials_stored'] is False
    return preview, files


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('order', list(permutations(CLIENTS)))
def test_every_setup_order_is_idempotent_and_preserves_other_owned_blocks(tmp_path, command, order):
    root = tmp_path/'project'
    root.mkdir()
    for name in ('AGENTS.md', 'CLAUDE.md'):
        (root/name).write_bytes(OWNER)
    approved = {}
    for client in order:
        approved[client] = configure(root, command, client)
        for active, (preview, files) in approved.items():
            assert project.apply_project(preview, files, preview['approval'])['status'] == 'unchanged'
            current, same = project.preview_project(active, root, 'project-1', command=command)
            assert current['plan']['change'] == 'unchanged'
            assert project.apply_project(current, same, current['approval'])['status'] == 'unchanged'
            text = (root/('CLAUDE.md' if active == 'claude' else 'AGENTS.md')).read_bytes()
            assert text.startswith(OWNER) and text.count(project.instruction_block(active)) == 1
    # User text remains theirs even after managed setup. It is never rewritten.
    (root/'AGENTS.md').write_bytes((root/'AGENTS.md').read_bytes()+b'New owner note.\r\n')
    for client in reversed(order):
        preview, changes = project.preview_rollback(client, root)
        project.rollback_project(preview, changes, preview['approval'])
        approved.pop(client)
        for active, (original, files) in approved.items():
            assert project.apply_project(original, files, original['approval'])['status'] == 'unchanged'
            assert project.preview_project(active, root, 'project-1', command=command)[0]['plan']['change'] == 'unchanged'
    assert (root/'AGENTS.md').read_bytes() == OWNER+b'New owner note.\r\n'
    assert (root/'CLAUDE.md').read_bytes() == OWNER


@pytest.mark.parametrize('order', list(permutations(CLIENTS)))
def test_every_disconnect_order_removes_only_its_client_and_cleans_managed_empty_file(tmp_path, command, order):
    root = tmp_path/'project'
    root.mkdir()
    for client in CLIENTS:
        configure(root, command, client)
    active = set(CLIENTS)
    for client in order:
        preview, changes = project.preview_rollback(client, root)
        project.rollback_project(preview, changes, preview['approval'])
        active.remove(client)
        for peer in active:
            receipt, _ = project.load_receipt(root, peer)
            assert receipt['state'] == 'applied'
            assert project.config_entry(peer, (root/project.CONFIG_PATHS[peer]).read_bytes()) == receipt['entry']
            assert project.preview_project(peer, root, 'project-1', command=command)[0]['plan']['change'] == 'unchanged'
        again, same = project.preview_rollback(client, root)
        assert project.rollback_project(again, same, again['approval'])['status'] == 'unchanged'
    assert not (root/'AGENTS.md').exists()
    assert not (root/'CLAUDE.md').exists()


@pytest.mark.parametrize('raw', [b'<!-- modelbuilder:unknown -->', project.instruction_block('codex'),
                               project.instruction_block('opencode'), b'<!-- modelbuilder:codex:v1:end -->'])
def test_foreign_or_unreceipted_markers_still_block_without_overwriting(tmp_path, command, raw):
    (tmp_path/'AGENTS.md').write_bytes(OWNER+raw)
    before = snapshot(tmp_path)
    with pytest.raises(setup.SetupError) as error:
        project.preview_project('kilocode', tmp_path, 'project-1', command=command)
    assert error.value.code == 'instruction_conflict'
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('change', ['missing_receipt', 'applying', 'wrong_project', 'wrong_template',
                                  'edited_block', 'duplicate_block', 'untracked_foreign'])
def test_invalid_peer_ownership_is_never_adopted(tmp_path, command, change):
    configure(tmp_path, command, 'codex')
    path = tmp_path/project.receipt_name('codex')
    receipt = json.loads(path.read_text())
    instructions = tmp_path/'AGENTS.md'
    if change == 'missing_receipt':
        path.unlink()
    elif change in {'applying', 'wrong_project', 'wrong_template'}:
        receipt[{'applying':'state','wrong_project':'project_id','wrong_template':'template_version'}[change]] = {
            'applying':'applying','wrong_project':'another-project','wrong_template':'999'}[change]
        path.write_text(json.dumps(receipt))
    else:
        raw = instructions.read_bytes()
        instructions.write_bytes(raw.replace(b'This integration grants no permissions.', b'Edited owned instructions.')
                                 if change == 'edited_block' else raw+(project.instruction_block('codex')
                                 if change == 'duplicate_block' else b'<!-- modelbuilder:foreign -->'))
    before = snapshot(tmp_path)
    with pytest.raises(setup.SetupError):
        project.preview_project('opencode', tmp_path, 'project-1', command=command)
    assert snapshot(tmp_path) == before


def test_owned_edits_conflict_but_unrelated_owner_text_survives(tmp_path, command):
    configure(tmp_path, command, 'codex')
    configure(tmp_path, command, 'opencode')
    instructions = tmp_path/'AGENTS.md'
    instructions.write_bytes(instructions.read_bytes()+OWNER)
    assert project.preview_project('codex', tmp_path, 'project-1', command=command)[0]['plan']['change'] == 'unchanged'
    instructions.write_bytes(instructions.read_bytes().replace(b'.agents/skills/modelbuilder-project', b'.agents/edited'))
    before = snapshot(tmp_path)
    for action in (lambda:project.preview_project('codex', tmp_path, 'project-1', command=command),
                   lambda:project.preview_rollback('codex', tmp_path),
                   lambda:project.preview_project('kilocode', tmp_path, 'project-1', command=command)):
        with pytest.raises(setup.SetupError):
            action()
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('target', ['instructions', 'peer_receipt'])
def test_shared_ownership_changes_after_review_reject_apply(tmp_path, command, target):
    configure(tmp_path, command, 'codex')
    preview, files = project.preview_project('opencode', tmp_path, 'project-1', command=command)
    changed = tmp_path/('AGENTS.md' if target == 'instructions' else project.receipt_name('codex'))
    changed.write_bytes(changed.read_bytes()+b'\n')
    before = snapshot(tmp_path)
    with pytest.raises(setup.SetupError) as error:
        project.apply_project(preview, files, preview['approval'])
    assert error.value.code == 'config_changed'
    assert snapshot(tmp_path) == before


def test_stale_disconnect_review_does_not_remove_a_later_client(tmp_path, command):
    configure(tmp_path, command, 'codex')
    preview, changes = project.preview_rollback('codex', tmp_path)
    configure(tmp_path, command, 'opencode')
    before = snapshot(tmp_path)
    with pytest.raises(setup.SetupError) as error:
        project.rollback_project(preview, changes, preview['approval'])
    assert error.value.code == 'config_changed'
    assert snapshot(tmp_path) == before
    preview, changes = project.preview_rollback('codex', tmp_path)
    project.rollback_project(preview, changes, preview['approval'])
    assert project.instruction_block('opencode') in (tmp_path/'AGENTS.md').read_bytes()


def test_failed_second_setup_restores_shared_text_and_original_peer_receipt(tmp_path, command, monkeypatch):
    (tmp_path/'AGENTS.md').write_bytes(OWNER)
    configure(tmp_path, command, 'codex')
    before = snapshot(tmp_path)
    preview, files = project.preview_project('opencode', tmp_path, 'project-1', command=command)
    original = project._atomic
    failed = False
    def fail_once(path, data):
        nonlocal failed
        if path.name == 'SKILL.md' and not failed:
            failed = True
            raise OSError('injected write failure')
        return original(path, data)
    monkeypatch.setattr(project, '_atomic', fail_once)
    with pytest.raises(OSError):
        project.apply_project(preview, files, preview['approval'])
    assert snapshot(tmp_path) == before
    assert project.preview_project('codex', tmp_path, 'project-1', command=command)[0]['plan']['change'] == 'unchanged'


def test_legacy_receipt_can_share_and_disconnect_without_leaving_an_empty_managed_file(tmp_path, command):
    configure(tmp_path, command, 'codex')
    path = tmp_path/project.receipt_name('codex')
    receipt = json.loads(path.read_text())
    for item in receipt['files']:
        item.pop('managed_file_created', None)
    path.write_text(json.dumps(receipt))
    configure(tmp_path, command, 'opencode')
    for client in ('codex', 'opencode'):
        preview, changes = project.preview_rollback(client, tmp_path)
        project.rollback_project(preview, changes, preview['approval'])
    assert not (tmp_path/'AGENTS.md').exists()


def test_disconnect_does_not_accept_unreceipted_foreign_markers(tmp_path, command):
    configure(tmp_path, command, 'codex')
    path = tmp_path/'AGENTS.md'
    path.write_bytes(path.read_bytes()+project.instruction_block('opencode'))
    before = snapshot(tmp_path)
    with pytest.raises(setup.SetupError) as error:
        project.preview_rollback('codex', tmp_path)
    assert error.value.code == 'instruction_conflict'
    assert snapshot(tmp_path) == before


def test_failed_disconnect_restores_other_clients_and_user_text(tmp_path, command, monkeypatch):
    (tmp_path/'AGENTS.md').write_bytes(OWNER)
    configure(tmp_path, command, 'codex')
    configure(tmp_path, command, 'kilocode')
    before = snapshot(tmp_path)
    preview, changes = project.preview_rollback('codex', tmp_path)
    original = project._atomic
    failed = False
    def fail_once(path, data):
        nonlocal failed
        if path.name == 'SKILL.md' and not failed:
            failed = True
            raise OSError('injected disconnect failure')
        return original(path, data)
    monkeypatch.setattr(project, '_atomic', fail_once)
    with pytest.raises(OSError):
        project.rollback_project(preview, changes, preview['approval'])
    assert snapshot(tmp_path) == before


def test_unchanged_apply_still_checks_reviewed_peer_receipt(tmp_path, command):
    configure(tmp_path, command, 'codex')
    configure(tmp_path, command, 'opencode')
    preview, files = project.preview_project('codex', tmp_path, 'project-1', command=command)
    peer = tmp_path/project.receipt_name('opencode')
    peer.write_bytes(peer.read_bytes()+b'\n')
    before = snapshot(tmp_path)
    with pytest.raises(setup.SetupError) as error:
        project.apply_project(preview, files, preview['approval'])
    assert error.value.code == 'config_changed'
    assert snapshot(tmp_path) == before
