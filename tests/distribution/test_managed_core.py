"""Isolated real HTTP/stdio lifecycle probes; not native installer acceptance."""
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("managed_core", ROOT / "distribution/managed_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "payload.txt").write_text("isolated runtime test fixture")
    (root / "runtime.json").write_text(json.dumps({"schema_version": 1, "files": {
        "payload.txt": core.hashlib.sha256((root / "payload.txt").read_bytes()).hexdigest()}}))
    return root


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "private-app-state"
    yield path
    session = core.discover(path) if path.exists() else None
    if session:
        # Kill only the fixture instance whose authenticated identity was verified.
        os.kill(session["pid"], signal.SIGTERM)


def lease(runtime, state):
    process = subprocess.Popen([sys.executable, "-I", str(ROOT / "distribution/managed_core.py"),
                                "lease", "--runtime-root", str(runtime), "--state-dir", str(state)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert select.select([process.stdout], [], [], 20)[0], "Core did not report ready"
    line = process.stdout.readline()
    assert line, process.stderr.read()
    return process, json.loads(line)


def call(connection, operation, payload):
    request = urllib.request.Request(connection["url"] + "/v1/operations/" + operation,
                                     data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + connection["token"],
                                     "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def close(process):
    process.stdin.close()
    process.wait(timeout=6)
    assert process.returncode == 0, process.stderr.read()


def until_stopped(state):
    deadline = time.monotonic() + 14
    while time.monotonic() < deadline and (state / core.SESSION).exists():
        time.sleep(0.1)
    assert not (state / core.SESSION).exists()


def test_private_state_rejects_shared_directory_and_symlink(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    with pytest.raises(core.CoreError, match="0700"):
        core.private_directory(shared)
    redirected = tmp_path / "redirected"
    redirected.symlink_to(shared)
    with pytest.raises(core.CoreError, match="symbolic"):
        core.private_directory(redirected)


def test_runtime_inventory_rejects_tampering_and_escape(runtime, tmp_path):
    manifest = core.runtime_manifest(runtime)
    core.verify_runtime(runtime, manifest)
    (runtime / "payload.txt").write_text("modified")
    with pytest.raises(core.CoreError, match="integrity"):
        core.verify_runtime(runtime, manifest)
    (tmp_path / "external").write_text("outside")
    with pytest.raises(core.CoreError, match="redirected"):
        core.relative_file(runtime, "../external")


def test_no_external_environment_or_untrusted_port():
    for port in ["8765", 0, -1, 65536, True]:
        with pytest.raises(core.CoreError, match="port"):
            core.rpc({"port": port, "token": "a" * 64}, "/health")
    assert not any(key.startswith(("MODELBUILDER", "PYTHON", "GIT_")) for key in core.service_environment())


def test_real_service_two_consumers_persistence_rotation_and_shutdown(runtime, state):
    first, owner = lease(runtime, state)
    second, same = lease(runtime, state)
    assert owner == same
    metadata = core.private_read(state / core.SESSION)
    assert os.stat(state).st_mode & 0o777 == 0o700
    assert os.stat(state / core.SESSION).st_mode & 0o777 == 0o600
    with pytest.raises(urllib.error.HTTPError) as unauthorized:
        urllib.request.urlopen(owner["url"] + "/v1/catalog")
    assert unauthorized.value.code == 401
    project = call(owner, "project.create", {"model_name": "Persisted model", "slug": "persisted-model", "idempotency_key": "create-one"})
    close(first)
    assert call(same, "project.get", {"project_id": project["project_id"]})["project_id"] == project["project_id"]
    assert core.discover(state)["pid"] == metadata["pid"]
    close(second)
    until_stopped(state)
    third, restarted = lease(runtime, state)
    assert restarted["token"] != owner["token"]
    assert restarted["instance"] != owner["instance"]
    assert call(restarted, "project.get", {"project_id": project["project_id"]})["model_name"] == "Persisted model"
    close(third)
    until_stopped(state)
    assert not (state / core.SESSION).exists()
    assert (state / "modelbuilder.sqlite3").exists()


def test_bridge_credentials_are_readonly_project_scoped_and_revoked(runtime, state):
    process, owner = lease(runtime, state)
    project = call(owner, "project.create", {"model_name": "Allowed", "slug": "allowed", "idempotency_key": "one"})
    other = call(owner, "project.create", {"model_name": "Private", "slug": "private", "idempotency_key": "two"})
    session = core.private_read(state / core.SESSION)
    bridge = core.Lease(session)
    grant = core.rpc(session, "/_desktop/bridge", {"id": bridge.identity, "project_id": project["project_id"]})
    assert grant["scopes"] == ["read"]
    assert grant["project_ids"] == [project["project_id"]]
    assert grant["token"] != owner["token"]
    consumer = {**owner, "token": grant["token"]}
    assert call(consumer, "project.get", {"project_id": project["project_id"]})["model_name"] == "Allowed"
    for operation, payload in [("project.get", {"project_id": other["project_id"]}),
                               ("project.create", {"model_name": "Denied", "slug": "denied", "idempotency_key": "denied"})]:
        with pytest.raises(urllib.error.HTTPError) as denied:
            call(consumer, operation, payload)
        assert denied.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as management:
        core.rpc({**session, "token": grant["token"]}, "/_desktop/session")
    assert management.value.code == 403
    assert grant["token"] not in (state / core.SESSION).read_text()
    bridge.close()
    with pytest.raises(urllib.error.HTTPError) as revoked:
        call(consumer, "project.get", {"project_id": project["project_id"]})
    assert revoked.value.code == 401
    close(process)


def test_stdio_mcp_is_per_consumer_and_exits_on_eof(runtime, state):
    process, owner = lease(runtime, state)
    project = call(owner, "project.create", {"model_name": "MCP", "slug": "mcp", "idempotency_key": "mcp"})
    mcp = subprocess.Popen([sys.executable, "-I", str(ROOT / "distribution/managed_core.py"), "mcp",
                            "--runtime-root", str(runtime), "--state-dir", str(state), "--project-id", project["project_id"]],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    def exchange(message):
        mcp.stdin.write(json.dumps(message) + "\n")
        mcp.stdin.flush()
        assert select.select([mcp.stdout], [], [], 10)[0]
        return json.loads(mcp.stdout.readline())
    response = exchange({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "isolated-fixture", "version": "1"}}})
    assert response["result"]["serverInfo"]["name"] == "modelbuilder"
    mcp.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    mcp.stdin.flush()
    tools = exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert any(tool["name"] == "project_get" for tool in tools["result"]["tools"])
    queued = exchange({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "project_create", "arguments": {"model_name": "Denied", "slug": "denied", "idempotency_key": "blocked"}}})
    review = queued["result"]["structuredContent"]
    assert not queued["result"]["isError"] and review["status"] == "pending"
    assert len(call(owner, "project.list", {})["projects"]) == 1
    session = core.private_read(state / core.SESSION)
    decision = core.rpc(session, "/_desktop/reviews/" + review["id"] + "/decision", {"review_digest": review["review_digest"], "decision": "approve"})
    assert decision["status"] == "succeeded"
    result = exchange({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
        "name": "modelbuilder_review_status", "arguments": {"request_id": review["id"]}}})
    assert result["result"]["structuredContent"]["status"] == "succeeded"
    assert len(call(owner, "project.list", {})["projects"]) == 2
    assert owner["token"] not in json.dumps(result)
    second = exchange({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
        "name": "project_create", "arguments": {"model_name": "Withdrawn", "slug": "withdrawn", "idempotency_key": "withdrawn"}}})
    withdrawn = exchange({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {
        "name": "modelbuilder_review_cancel", "arguments": {"request_id": second["result"]["structuredContent"]["id"]}}})
    assert withdrawn["result"]["structuredContent"]["status"] == "withdrawn"
    assert len(call(owner, "project.list", {})["projects"]) == 2
    close(mcp)
    assert core.discover(state) is not None
    close(process)


def test_simultaneous_first_windows_acquire_exactly_one_core(runtime, state):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        consumers = list(pool.map(lambda _: lease(runtime, state), range(4)))
    try:
        assert len({connection["instance"] for _, connection in consumers}) == 1
        assert len({connection["token"] for _, connection in consumers}) == 1
    finally:
        for process, _ in consumers:
            close(process)


def test_mcp_refuses_unscoped_assistant_access_before_starting_service(runtime, state):
    result = subprocess.run([sys.executable, "-I", str(ROOT / "distribution/managed_core.py"), "mcp",
                             "--runtime-root", str(runtime), "--state-dir", str(state)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "requires --project-id" in result.stderr
    assert not state.exists()


def test_running_core_cannot_be_silently_reused_by_different_runtime(runtime, state):
    process, _ = lease(runtime, state)
    manifest = json.loads((runtime / "runtime.json").read_text())
    manifest["version"] = "different-runtime"
    (runtime / "runtime.json").write_text(json.dumps(manifest))
    try:
        with pytest.raises(core.CoreError, match="Another application runtime"):
            core.acquire(runtime, state)
    finally:
        close(process)


def test_native_source_registration_project_isolation_restart_and_replacement(runtime, state, tmp_path):
    import shutil
    checkout = tmp_path / "selected-source"
    subprocess.run(["git", "clone", "--local", "--no-hardlinks", "--no-checkout", str(ROOT), str(checkout)],
                   check=True, capture_output=True)
    process, owner = lease(runtime, state)
    first = call(owner, "project.create", {"model_name": "First", "slug": "source-first", "idempotency_key": "source-first"})
    second = call(owner, "project.create", {"model_name": "Second", "slug": "source-second", "idempotency_key": "source-second"})
    session = core.private_read(state / core.SESSION)
    payload = {"folder_path": str(checkout), "project_id": first["project_id"], "trust_consent": True}
    with pytest.raises(urllib.error.HTTPError) as cancelled:
        core.rpc(session, "/_desktop/source", {**payload, "trust_consent": False})
    assert cancelled.value.code == 403 and not (state / "sources.json").exists()
    symlink = tmp_path / "redirected-source"
    symlink.symlink_to(checkout, target_is_directory=True)
    with pytest.raises(urllib.error.HTTPError) as redirected:
        core.rpc(session, "/_desktop/source", {**payload, "folder_path": str(symlink)})
    assert redirected.value.code == 400 and not (state / "sources.json").exists()
    receipt = core.rpc(session, "/_desktop/source", payload)
    assert receipt["registered"] and receipt["operator_trusted"]
    assert receipt["workspace_path"] == str(checkout) and receipt["project_id"] == first["project_id"]
    binding = {"project_id": first["project_id"], "source_alias": receipt["alias"],
               "source_repository_id": receipt["repository_id"], "trusted": True,
               "expected_revision": first["revision"], "idempotency_key": "bind-first"}
    call(owner, "project.source.bind", binding)
    with pytest.raises(urllib.error.HTTPError) as other_project:
        call(owner, "project.source.bind", {**binding, "project_id": second["project_id"], "idempotency_key": "bind-denied"})
    assert other_project.value.code == 403
    bridge = core.Lease(session)
    grant = core.rpc(session, "/_desktop/bridge", {"id": bridge.identity, "project_id": first["project_id"]})
    with pytest.raises(urllib.error.HTTPError) as assistant:
        core.rpc({**session, "token": grant["token"]}, "/_desktop/source", payload)
    assert assistant.value.code == 403
    stored = (state / "sources.json").read_text()
    assert owner["token"] not in stored and grant["token"] not in stored
    assert os.stat(state / "sources.json").st_mode & 0o777 == 0o600
    bridge.close()
    close(process)
    until_stopped(state)
    process, owner = lease(runtime, state)
    entries = call(owner, "project.source.list", {"project_id": first["project_id"]})["sources"]
    assert entries[0]["repository_id"] == receipt["repository_id"] and entries[0]["operator_trusted"]
    # Replacing Git's identity never inherits the earlier project trust/binding.
    shutil.move(checkout / ".git", checkout / ".old-git")
    shutil.copytree(checkout / ".old-git", checkout / ".git")
    assert call(owner, "project.source.list", {"project_id": first["project_id"]})["sources"][0]["operator_trusted"] is False
    status = call(owner, "project.source.status", {"project_id": first["project_id"]})
    assert not status["available"] and status["diagnostics"][0]["code"] == "source_binding_changed"
    close(process)
    until_stopped(state)
    process, owner = lease(runtime, state)
    entries = call(owner, "project.source.list", {"project_id": first["project_id"]})["sources"]
    assert entries[0]["operator_trusted"] is False
    assert receipt["repository_id"] != entries[0]["repository_id"]
    close(process)


def test_optional_mlx_is_disabled_without_import_on_older_macos():
    manifest = {"mlx_python": "mlx-python/python", "mlx_minimum_os": "26.0"}
    assert core.optional_mlx_available(manifest, "Darwin", "26.0")
    assert not core.optional_mlx_available(manifest, "Darwin", "15.7")
    assert not core.optional_mlx_available(manifest, "Linux", "26.0")
    assert not core.optional_mlx_available({"mlx_python": "anything"}, "Darwin", "26.0")


def test_real_http_review_denial_cancel_changed_payload_and_stale_context(runtime, state):
    process, owner = lease(runtime, state)
    bridge = None
    try:
        project = call(owner, 'project.create', {'model_name': 'HTTP review', 'slug': 'http-review', 'idempotency_key': 'review-create'})
        session = core.private_read(state / core.SESSION)
        bridge = core.Lease(session)
        token = core.rpc(session, '/_desktop/bridge', {'id': bridge.identity, 'project_id': project['project_id']})['token']
        consumer = {**session, 'token': token}
        payload = {'project_id': project['project_id'], 'expected_revision': project['revision'], 'changes': {'description': 'approved'}, 'idempotency_key': 'review-update'}
        envelope = {'project_id': project['project_id'], 'operation': 'project.update', 'payload': payload}
        for path, body in [('/_assistant/reviews', {**envelope, 'human_approved': True}), ('/_desktop/reviews', envelope)]:
            with pytest.raises(urllib.error.HTTPError) as denied:
                core.rpc(consumer, path, body)
            assert denied.value.code in {400, 403}
        pending = core.rpc(consumer, '/_assistant/reviews', envelope)
        route = '/_desktop/reviews/' + pending['id'] + '/decision'
        for actor, body, expected in [
            (consumer, {'review_digest': pending['review_digest'], 'decision': 'approve'}, 403),
            (session, {'review_digest': 'b'*64, 'decision': 'approve'}, 409),
            (session, {'review_digest': pending['review_digest'], 'decision': 'approve', 'payload': {}}, 400),
        ]:
            with pytest.raises(urllib.error.HTTPError) as denied:
                core.rpc(actor, route, body)
            assert denied.value.code == expected
        assert call(owner, 'project.get', {'project_id': project['project_id']})['revision'] == 1
        denied = core.rpc(session, route, {'review_digest': pending['review_digest'], 'decision': 'deny'})
        assert denied['status'] == 'denied'
        assert core.rpc(session, '/_desktop/reviews/' + pending['id'])['status'] == 'denied'
        with pytest.raises(urllib.error.HTTPError) as replay:
            core.rpc(session, route, {'review_digest': pending['review_digest'], 'decision': 'approve'})
        assert replay.value.code == 409
        withdrawn = core.rpc(consumer, '/_assistant/reviews', {**envelope, 'payload': {**payload, 'idempotency_key': 'withdraw'}})
        assert core.rpc(consumer, '/_assistant/reviews/' + withdrawn['id'] + '/cancel', {})['status'] == 'withdrawn'
        stale = core.rpc(consumer, '/_assistant/reviews', {**envelope, 'payload': {**payload, 'idempotency_key': 'stale'}})
        call(owner, 'project.update', {**payload, 'idempotency_key': 'owner-change', 'changes': {'description': 'owner'}})
        with pytest.raises(urllib.error.HTTPError) as changed:
            core.rpc(session, '/_desktop/reviews/' + stale['id'] + '/decision', {'review_digest': stale['review_digest'], 'decision': 'approve'})
        assert changed.value.code == 409
        visible = core.rpc(consumer, '/_assistant/reviews/' + stale['id'])
        assert token not in json.dumps(visible) and owner['token'] not in json.dumps(visible)
        assert call(owner, 'project.get', {'project_id': project['project_id']})['description'] == 'owner'
    finally:
        if bridge:
            bridge.close()
        close(process)


def test_native_imported_snapshot_registration_is_explicit_and_restart_safe(runtime, state, tmp_path):
    import hashlib
    import shutil
    from modelbuilder.recipe import default_recipe, export_bundle, import_bundle
    author = tmp_path / "author"
    author.mkdir()
    (author / "source.py").write_text("raise RuntimeError('must never execute during registration')\n")
    recipe = default_recipe("transport", "transport-recipe")
    recipe["source"].update(commit="a" * 40, files=[{"path": "source.py", "sha256": hashlib.sha256((author / "source.py").read_bytes()).hexdigest()}])
    snapshot = tmp_path / "imported-snapshot"
    report = import_bundle(export_bundle(recipe, source_root=author), snapshot)
    process, owner = lease(runtime, state)
    try:
        project = call(owner, "project.create", {"model_name": "Imported", "slug": "imported", "idempotency_key": "create"})
        session = core.private_read(state / core.SESSION)
        payload = {"folder_path": str(snapshot), "project_id": project["project_id"], "trust_consent": True}
        with pytest.raises(urllib.error.HTTPError):
            core.rpc(session, "/_desktop/source", payload)  # No implicit Git-free fallback.
        with pytest.raises(urllib.error.HTTPError):
            core.rpc(session, "/_desktop/source", {**payload, "source_kind": "automatic"})
        assert not (state / "sources.json").exists()
        receipt = core.rpc(session, "/_desktop/source", {**payload, "source_kind": "imported_snapshot"})
        assert receipt["source_kind"] == "imported_snapshot" and receipt["git_provenance_verified"] is False
        assert receipt["snapshot_digest"] == report["bundle_digest"] and receipt["operator_trusted"]
        assert call(owner, "hub.license.policy.list", {"project_id": project["project_id"]})["policies"] == []
        call(owner, "project.source.bind", {"project_id": project["project_id"], "source_alias": receipt["alias"],
            "source_repository_id": receipt["repository_id"], "trusted": True, "expected_revision": 1, "idempotency_key": "bind"})
    finally:
        close(process)
    until_stopped(state)
    process, owner = lease(runtime, state)
    try:
        sources = call(owner, "project.source.list", {"project_id": project["project_id"]})["sources"]
        assert sources[0]["source_kind"] == "imported_snapshot" and sources[0]["operator_trusted"]
        moved = snapshot.with_name("original-snapshot")
        snapshot.rename(moved)
        shutil.copytree(moved, snapshot)
        sources = call(owner, "project.source.list", {"project_id": project["project_id"]})["sources"]
        assert sources[0]["operator_trusted"] is False
        status = call(owner, "project.source.status", {"project_id": project["project_id"]})
        assert not status["available"] and status["source_kind"] == "imported_snapshot"
    finally:
        close(process)
