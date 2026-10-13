"""Native operator settings; fake lookup names, no real credential-store access."""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "distribution"))
from managed_hub import HubSettings  # noqa: E402
from modelbuilder.models import Actor, ServiceError  # noqa: E402
from modelbuilder.service import OperationService  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    service = OperationService(db_path=tmp_path / "state.sqlite3")
    owner = Actor(id="desktop-owner", scopes=frozenset({"read", "modify", "execute"}))
    project = service.invoke("project.create", {"slug": "managed-hub", "model_name": "Managed Hub", "idempotency_key": "create"}, owner)["project_id"]
    hub = HubSettings(service, sys.executable, tmp_path)
    yield hub, owner, project
    service.hub.close()
    service.store.close()


def proposed(hub, owner, project, folder=None, **changes):
    payload = {"project_id": project, "expected_revision": hub.status(owner, project)["revision"],
               "account": {"keyring_service": "fixture-keyring", "username": "fixture-user"}, "folder_path": str(folder) if folder else None, "folder_selected": folder is not None, "max_bytes": 16*1024**3}
    return hub.preview(owner, {**payload, **changes})


def test_owner_only_settings_and_project_scoped_accounts(settings, tmp_path):
    hub, owner, project = settings
    assistant = Actor(id="desktop-assistant:fixture", scopes=frozenset({"read"}), project_ids=frozenset({project}))
    with pytest.raises(ServiceError, match="native desktop owner"):
        hub.status(assistant, project)
    folder = tmp_path / "selected"
    folder.mkdir()
    review = proposed(hub, owner, project, folder)
    assert hub.status(owner, project)["revision"] == 0  # preview/cancel writes nothing
    result = hub.apply(owner, review)
    assert result["credential_status"] == "not_checked"
    alias = result["account"]["alias"]
    assert hub.service.hub.accounts[alias]["project_ids"] == [project]
    with pytest.raises(ServiceError, match="unavailable to this project"):
        hub.service.hub._provider({"provider": "huggingface", "project_id": "other-project", "account_alias": alias})
    with pytest.raises(ServiceError, match="settings changed"):
        hub.apply(owner, review)


def test_folder_identity_rejects_symlink_swap_and_restart_drift(settings, tmp_path):
    hub, owner, project = settings
    folder = tmp_path / "selected"
    folder.mkdir()
    link = tmp_path / "link"
    link.symlink_to(folder)
    with pytest.raises(ServiceError, match="canonical"):
        proposed(hub, owner, project, link)
    review = proposed(hub, owner, project, folder)
    folder.rename(tmp_path / "original")
    folder.mkdir()
    with pytest.raises(ServiceError, match="changed during review"):
        hub.apply(owner, review)
    hub.apply(owner, proposed(hub, owner, project, folder))
    alias = next(iter(hub.service.hub.publication_roots[project]))
    assert hub.service.hub._source(project, alias) == folder
    folder.rename(tmp_path / "second-original")
    folder.mkdir()
    # Reconstruct settings from the same durable store, retaining prior inode.
    restarted = HubSettings(hub.service, sys.executable, tmp_path)
    with pytest.raises(ServiceError, match="identity changed"):
        restarted.service.hub._source(project, alias)


def test_account_change_revokes_previews_approvals_and_requests_active_cancellation(settings):
    hub, owner, project = settings
    initial = hub.apply(owner, proposed(hub, owner, project))
    old_alias = initial["account"]["alias"]
    captured_provider = hub.service.hub.providers["huggingface"]
    for kind, identity, body in [("hub_preview", "preview", {"preview_id": "preview", "discarded": False}),
                                 ("hub_approval", "approval", {"approval_id": "approval", "consumed": False}),
                                 ("hub_transfer", "transfer", {"transfer_id": "transfer", "state": "running", "cancel_requested": False})]:
        hub.store.put(kind, identity, project, {**body, "project_id": project, "revision": 1})
    result = hub.apply(owner, proposed(hub, owner, project, account={"keyring_service": "fixture-keyring", "username": "another-fixture"}))
    assert result["account"]["alias"] != old_alias
    assert old_alias not in hub.service.hub.accounts
    assert captured_provider.accounts[old_alias]["username"] == "fixture-user"
    assert hub.store.get("hub_preview", "preview")["discarded"]
    assert hub.store.get("hub_approval", "approval")["consumed"]
    assert hub.store.get("hub_transfer", "transfer")["cancel_requested"]
    assert result["cancellation_requested"] == ["transfer"]
    assert "cannot undo" in result["notice"]


def test_settings_reject_changed_digest_expiry_raw_tokens_and_extra_fields(settings):
    hub, owner, project = settings
    for account in [{"token": "fake-only"}, {"keyring_service": "hf_fake_token", "username": "fixture"}]:
        with pytest.raises(ServiceError):
            proposed(hub, owner, project, account=account)
    review = proposed(hub, owner, project)
    review["plan"]["expires_at"] = 0
    with pytest.raises(ServiceError, match="changed"):
        hub.apply(owner, review)
    from managed_hub import digest
    review["review_digest"] = digest(review["plan"])
    with pytest.raises(ServiceError, match="expired"):
        hub.apply(owner, review)
    assert hub.status(owner, project)["revision"] == 0


def test_real_managed_http_native_settings_route_denies_readonly_bridge(tmp_path):
    import managed_core as core
    runtime, state = tmp_path / "runtime", tmp_path / "state"
    runtime.mkdir()
    (runtime / "hub-python").write_text("inert worker; never launched by settings")
    manifest = {"schema_version": 1, "hub_python": "hub-python", "files": {"hub-python": core.hashlib.sha256((runtime / "hub-python").read_bytes()).hexdigest()}}
    (runtime / "runtime.json").write_text(json.dumps(manifest))
    process = subprocess.Popen([sys.executable, "-I", str(ROOT / "distribution/managed_core.py"), "lease", "--runtime-root", str(runtime), "--state-dir", str(state)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    connection = None
    try:
        assert select.select([process.stdout], [], [], 20)[0]
        line = process.stdout.readline()
        assert line, process.stderr.read()
        connection = json.loads(line)
        session = {**connection, "port": int(connection["url"].rsplit(":", 1)[1])}
        project = core.rpc(session, "/v1/operations/project.create", {"slug": "native-hub", "model_name": "Native Hub", "idempotency_key": "create"})["project_id"]
        native = core.rpc(session, "/_desktop/hub/settings/preview", {"project_id": project, "expected_revision": 0, "account": {"keyring_service": "fixture", "username": "fake-user"}, "folder_path": None, "folder_selected": False, "max_bytes": 16*1024**3})
        core.rpc(session, "/_desktop/lease", {"id": "f" * 48})
        bridge = core.rpc(session, "/_desktop/bridge", {"project_id": project, "id": "f" * 48})
        with pytest.raises(urllib.error.HTTPError) as denied:
            core.rpc({**session, "token": bridge["token"]}, "/_desktop/hub/settings/apply", native)
        assert denied.value.code == 403
        result = core.rpc(session, "/_desktop/hub/settings/apply", native)
        assert result["revision"] == 1
        providers = core.rpc(session, "/v1/operations/hub.providers", {"project_id": project})
        assert providers["accounts"] == [result["account"]["alias"]]
        assert providers["providers"]
    finally:
        process.stdin.close()
        process.wait(timeout=8)
        if connection and (active := core.discover(state)):
            os.kill(active["pid"], signal.SIGTERM)


def test_account_edit_cannot_silently_retrust_a_replaced_folder(settings, tmp_path):
    hub, owner, project = settings
    folder = tmp_path / 'publication'
    folder.mkdir()
    hub.apply(owner, proposed(hub, owner, project, folder))
    folder.rename(tmp_path / 'previous-publication')
    folder.mkdir()
    with pytest.raises(ServiceError, match='native folder picker'):
        proposed(hub, owner, project, folder, folder_selected=False)
    assert hub.apply(owner, proposed(hub, owner, project, folder))["revision"] == 2


def test_native_project_budget_default_persistence_bounds_and_no_cross_project_expansion(settings, tmp_path):
    hub, owner, project = settings
    from managed_hub import byte_budget
    from modelbuilder.hub import DEFAULT_MAX_BYTES, MAX_BYTES
    assert DEFAULT_MAX_BYTES == 16*1024**3 and MAX_BYTES == 2**50
    assert hub.status(owner, project)['max_bytes'] == DEFAULT_MAX_BYTES
    for invalid in [True, False, 0, -1, 1.5, '64', MAX_BYTES+1]:
        with pytest.raises(ServiceError, match='integer'):
            byte_budget(invalid)
    result = hub.apply(owner, proposed(hub, owner, project, max_bytes=256*1024**3))
    assert result['max_bytes'] == hub.service.hub.byte_budget(project) == 256*1024**3
    assert hub.service.hub.byte_budget('another-project') == DEFAULT_MAX_BYTES
    restarted = HubSettings(hub.service, sys.executable, tmp_path)
    assert restarted.service.hub.byte_budget(project) == 256*1024**3
    assert restarted.status(owner, project)['max_bytes'] == 256*1024**3


def test_budget_change_reuses_stale_preview_invalidation(settings):
    hub, owner, project = settings
    hub.store.put('hub_preview','budget-preview',project,{'preview_id':'budget-preview','project_id':project,'revision':1,'discarded':False})
    hub.store.put('hub_approval','budget-approval',project,{'approval_id':'budget-approval','project_id':project,'revision':1,'consumed':False})
    review = proposed(hub, owner, project, max_bytes=1024**4)
    assert hub.service.hub.byte_budget(project) == 16*1024**3  # preview/cancel changes no cap
    result = hub.apply(owner, review)
    assert result['revoked_previews'] == ['budget-preview']
    assert hub.store.get('hub_approval','budget-approval')['consumed']
    with pytest.raises(ServiceError, match='settings changed'):
        hub.apply(owner, review)


def test_pre_budget_settings_keep_conservative_default_after_restart(settings, tmp_path):
    hub, owner, project = settings
    hub.store.put('desktop_hub_settings', project, project, {'project_id':project,'revision':1,'account':None,'folder':None})
    restored = HubSettings(hub.service, sys.executable, tmp_path)
    assert restored.status(owner, project)['max_bytes'] == 16*1024**3
    assert restored.service.hub.byte_budget(project) == 16*1024**3
    assert restored.apply(owner, proposed(restored, owner, project, max_bytes=64*1024**3))['revision'] == 2
