"""Controlled placement preparation races; no native jobs or tensor operations.

The backend double retains the production placement admission/cache boundary,
while every low-level device probe is replaced with an explicitly mocked result.
These tests establish service locking/authorization behavior, not GPU acceptance.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
import shutil
import sys
import subprocess
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from modelbuilder.api import create_app
from modelbuilder.auth import TokenAuth
from modelbuilder.backend import placement
from modelbuilder.backend.adapter import ZenMLBackend
from modelbuilder.backend.admission import BackendError
from modelbuilder.models import Actor, ServiceError
from modelbuilder.recipe import default_recipe
from modelbuilder.service import OperationService


OWNER = Actor("placement-owner", {"read", "modify", "execute"})


def mock_result(device, allocate):
    return {"available": True, "device": device, "platform": "darwin", "machine": "arm64",
            "torch": "2.9.0", "allocation_verified": allocate, "mocked_hardware": True}


class PlacementBackend(ZenMLBackend):
    """Use real placement admission but never enter a native backend executor."""

    def __init__(self, root, python, source):
        super().__init__(root, python_executable=str(python), source_root=source,
                         trusted_code_roots=[str(source)])
        self.calls = []
        self.executed = []
        self.store = None

    def prepare_execution(self, operation, payload):
        return operation, deepcopy(payload), self.invoke(operation, payload)

    def launch_prepared(self, prepared, authorize=None):
        if authorize:
            authorize()
        operation, payload, result = prepared
        self.executed.append((operation, payload, result["placement"]))
        return result

    def invoke(self, operation, payload, actor=None, **kwargs):
        self.calls.append((operation, deepcopy(payload)))
        if operation == "run.evaluations.list":
            return {"evaluations": [], "operation_id": payload["operation_id"]}
        assert operation in {"run.plan", "run.submit", "run.resume"}
        assert not self.store.connection.in_transaction
        # Full admission now consumes request-local probe results outside the writer.
        value = placement.validate_placement(self, payload)
        if operation == "run.plan":
            return {"executable": True, "blocked_reasons": [], "placement": value}
        value = placement.validate_placement(self, payload, allocate=True)
        return {"operation_id": payload["operation_id"], "backend_run_id": "mock-" + payload["operation_id"],
                "status": "submitted", "placement": value, "execution": payload.get("execution")}


@pytest.fixture(autouse=True)
def never_run_a_real_device_probe(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Every low-level device probe must be explicitly mocked")
    monkeypatch.setattr(placement, "_probe", forbidden)


@pytest.fixture
def environment(tmp_path):
    services = []

    def create(device="cpu", *, bound=False):
        base = tmp_path / str(len(services))
        source = base / "source"
        source.mkdir(parents=True)
        (source / "marker.py").write_text("MARKER = 'inert source fixture'\n")
        interpreter = base / "interpreter" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("This is an inert interpreter identity fixture.\n")
        backend = PlacementBackend(base / "state", interpreter, source)
        roots = {"source": {"path": str(source), "trusted": True}} if bound else None
        service = OperationService(base / "service.sqlite3", backend=backend, source_roots=roots)
        services.append(service)
        backend.store = service.store
        recipe = default_recipe("unassigned-project", "unassigned-recipe", "Controlled placement")
        recipe["nodes"] = [{"id": "train", "kind": "model.train", "implementation_ref": "builtin:model.train",
                            "inputs": [], "outputs": [{"name": "checkpoint", "type": "Path"}],
                            "parameters": {"steps": 3}, "environment_ref": "default", "resource_request": {"gpu": 0},
                            "retry": {"max_attempts": 1, "backoff_seconds": 0}, "timeout": 30,
                            "cache_policy": "disabled", "provenance": {}}]
        recipe["environment"]["device"] = device
        if device == "mps":
            recipe["environment"]["lock_file"] = {"path": "backend.lock", "sha256": placement.MPS_LOCK_SHA256}
        payload = {"slug": "placement", "model_name": "Placement fixture", "recipe": recipe,
                   "idempotency_key": "project"}
        if bound:
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "add", "marker.py"], check=True)
            subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture", "-c",
                            "user.email=fixture@example.invalid", "commit", "-qm", "Inert fixture"], check=True)
            identity = service.source_workspaces.inspect("source")
            payload.update(source_alias="source", source_repository_id=identity["repository_id"], trusted=True)
        project = service.invoke("project.create", payload, OWNER)
        pid = project["project_id"]
        service.invoke("policy.update", {"project_id": pid, "expected_revision": 1,
                       "changes": {"allowed_targets": ["local-cpu", "local-mps"]},
                       "idempotency_key": "policy"}, OWNER)
        recipe = service._current_recipe(pid)
        with service.store.transaction():
            service.store.put("operation", "parent", pid, {"operation_id": "parent", "project_id": pid,
                "backend_run_id": "mock-parent", "recipe_id": recipe["recipe_id"], "recipe_revision": 1,
                "recipe_digest": recipe["digest"], "revision": 1, "status": "succeeded",
                "execution": {"node_ids": ["train"], "input_bindings": []},
                "source_binding": deepcopy(project.get("source_binding"))})
        return SimpleNamespace(service=service, backend=backend, project=project, source=source,
                               interpreter=interpreter, device=device)

    yield create
    for service in services:
        service.store.close()


def request(env, operation, key="held"):
    pid = env.project["project_id"]
    if operation == "run.resume":
        return {"project_id": pid, "operation_id": "parent", "expected_recipe_revision": 1,
                "mode": "warm_start", "steps": 5, "trusted": True, "idempotency_key": key}
    payload = {"project_id": pid, "recipe_id": env.project["recipe_id"],
               "target": placement.target_for_device(env.device), "trusted": True}
    if operation == "run.submit":
        payload.update(expected_revision=1, idempotency_key=key)
    return payload


class HeldProbe:
    def __init__(self, service, *, allocate):
        self.service, self.allocate = service, allocate
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = []

    def __call__(self, backend, device, *, allocate=False):
        assert not self.service.store.connection.in_transaction
        self.calls.append((device, allocate))
        if allocate == self.allocate:
            self.entered.set()
            assert self.release.wait(10), "Test failed to release the controlled probe"
        return mock_result(device, allocate)


def assert_no_request_write(env, key="held"):
    # Admission now durably reserves the key before slow work, without native launch.
    assert not env.backend.executed
    for row in env.service.store.list("operation", env.project["project_id"]):
        if row["operation_id"] != "parent":
            assert row["backend_run_id"] is None
            assert row["dispatch_state"] in {"preparing", "failed", "cancelled"}
    assert env.service.store.connection.execute("SELECT COUNT(*) FROM idempotency WHERE key=?", (key,)).fetchone()[0] <= 1


@pytest.mark.parametrize("operation,device", [
    ("run.plan", "mps"), ("run.submit", "cpu"), ("run.submit", "mps"),
    ("run.resume", "cpu"), ("run.resume", "mps"),
])
def test_held_probe_releases_live_reads_and_independent_sqlite_writer(environment, monkeypatch, operation, device):
    env = environment(device)
    service, pid = env.service, env.project["project_id"]
    held = HeldProbe(service, allocate=operation != "run.plan")
    monkeypatch.setattr(placement, "_probe", held)
    writer = OperationService(service.store.path)
    payload = request(env, operation)
    original_recipe = service._current_recipe(pid)
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            future = pool.submit(service.invoke, operation, payload, OWNER)
            try:
                assert held.entered.wait(3)
                assert not service.store.connection.in_transaction
                poll = pool.submit(service.invoke, "run.evaluations.list", {"project_id": pid, "operation_id": "parent"}, OWNER)
                assert poll.result(timeout=1)["evaluations"] == []
                write = pool.submit(writer.invoke, "evidence.add", {"project_id": pid, "title": "Independent writer",
                                    "source_identifier": "doi:10.1234/inert", "idempotency_key": "writer"}, OWNER)
                assert write.result(timeout=1)["title"] == "Independent writer"
                assert not future.done()
            finally:
                held.release.set()
            result = future.result(timeout=3)
        expected = [(device, False)] if operation == "run.plan" else (
            [(device, False), (device, True)] if device == "mps" else [(device, True)])
        assert held.calls == expected  # Commit never launches a second probe.
        assert service.store.get("recipe", env.project["recipe_id"], 1) == original_recipe
        if operation == "run.plan":
            assert result["executable"] and not result["placement"]["allocation_verified"]
            assert not env.backend.executed
            assert service._current_recipe(pid) == original_recipe
        else:
            assert result["placement"]["allocation_verified"]
            assert len(env.backend.executed) == 1
            assert service.invoke(operation, payload, OWNER) == result
            assert held.calls == expected and len(env.backend.executed) == 1
    finally:
        held.release.set()
        writer.store.close()


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
@pytest.mark.parametrize("refusal", ["trust", "policy", "scope", "project_access"])
def test_ineligible_request_never_allocates(environment, monkeypatch, operation, refusal):
    env = environment("mps")
    payload = request(env, operation)
    actor = OWNER
    if refusal == "trust":
        payload["trusted"] = False
    elif refusal == "policy":
        env.service.invoke("policy.update", {"project_id": env.project["project_id"], "expected_revision": 2,
            "changes": {"allowed_targets": []}, "idempotency_key": "disable"}, OWNER)
    elif refusal == "scope":
        actor = Actor(OWNER.id, {"read"})
    else:
        actor = Actor(OWNER.id, OWNER.scopes, project_ids={"different-project"})
    calls = []
    def checked(backend, device, *, allocate=False):
        calls.append(allocate)
        assert not allocate, "A refused execution request must not allocate tensors"
        return mock_result(device, allocate)
    monkeypatch.setattr(placement, "_probe", checked)
    with pytest.raises(ServiceError) as error:
        env.service.invoke(operation, payload, actor)
    assert error.value.code in {"forbidden", "untrusted_execution_blocked", "remote_disabled", "target_disabled"}
    assert not calls
    assert_no_request_write(env)


@pytest.mark.parametrize("device", ["cpu", "mps"])
def test_plan_never_uses_allocation_permission(environment, monkeypatch, device):
    env = environment(device)
    calls = []
    def checked(backend, selected, *, allocate=False):
        calls.append((selected, allocate))
        assert allocate is False
        assert not env.service.store.connection.in_transaction
        return mock_result(selected, allocate)
    monkeypatch.setattr(placement, "_probe", checked)
    assert env.service.invoke("run.plan", request(env, "run.plan"), OWNER)["executable"]
    assert calls == ([("mps", False)] if device == "mps" else [])
    assert not env.backend.executed


@pytest.mark.parametrize("change", ["project", "policy", "recipe", "source_trust", "source_dependencies",
    "source_access", "source_path", "dataset_roots", "interpreter_path", "interpreter_bytes", "venv_config"])
def test_held_preparation_rejects_changed_admission_inputs(environment, monkeypatch, change):
    env = environment("cpu", bound=True)
    service, pid = env.service, env.project["project_id"]
    held = HeldProbe(service, allocate=True)
    monkeypatch.setattr(placement, "_probe", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.invoke, "run.submit", request(env, "run.submit"), OWNER)
        try:
            assert held.entered.wait(3)
            if change == "project":
                with service.store.transaction():
                    old = service.store.get("project", pid)
                    service.store.put("project", pid, pid, {**old, "model_name": "Changed", "revision": old["revision"] + 1}, old["revision"])
            elif change == "policy":
                service.invoke("policy.update", {"project_id": pid, "expected_revision": 2,
                    "changes": {"max_local_runtime_seconds": 10}, "idempotency_key": "changed-policy"}, OWNER)
            elif change == "recipe":
                with service.store.transaction():
                    old = service._current_recipe(pid)
                    service._save_recipe({**old, "name": "Changed"}, pid, old["recipe_id"], 2, 1)
            elif change == "source_trust":
                service.source_workspaces.aliases["source"]["trusted"] = False
            elif change == "source_dependencies":
                service.source_workspaces.aliases["source"]["dependency_paths"]["new"] = str(env.source / "new.bin")
            elif change == "source_access":
                service.source_workspaces.aliases["source"]["project_ids"] = ["different-project"]
            elif change == "source_path":
                service.source_workspaces.aliases["source"]["path"] = str(env.source.parent / "replacement")
            elif change == "dataset_roots":
                service._dataset_registry._roots[pid] = {"new": str(env.source)}
            elif change == "interpreter_path":
                env.backend.python = str(env.interpreter.with_name("other-python"))
            elif change == "interpreter_bytes":
                env.interpreter.write_text("Changed inert interpreter, never executed.\n")
            else:
                (env.interpreter.parent.parent / "pyvenv.cfg").write_text("home = changed\n")
        finally:
            held.release.set()
        with pytest.raises(ServiceError) as error:
            future.result(timeout=3)
    assert error.value.code == ("revision_conflict" if change == "recipe" else "run_preparation_changed")
    assert not env.backend.calls  # Refuse before even invoking backend dispatch.
    assert held.calls == [("cpu", True)]
    assert_no_request_write(env)


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
@pytest.mark.parametrize("change", ["remove", "replace", "downgrade", "project_access"])
def test_http_original_grant_is_rechecked_after_held_probe(environment, monkeypatch, operation, change):
    env = environment("cpu")
    auth = TokenAuth({"first": OWNER, "second": OWNER})
    app = create_app(env.service, auth)
    held = HeldProbe(env.service, allocate=True)
    monkeypatch.setattr(placement, "_probe", held)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, "/v1/operations/" + operation, json=request(env, operation),
                             headers={"Authorization": "Bearer first"})
        try:
            assert held.entered.wait(3)
            if change == "remove":
                auth._tokens.pop("first")
            else:
                auth._tokens["first"] = Actor(OWNER.id, {"read"} if change == "downgrade" else OWNER.scopes,
                    project_ids={"other-project"} if change == "project_access" else None)
            assert auth.authenticate("Bearer second").id == OWNER.id
        finally:
            held.release.set()
        response = future.result(timeout=3)
    assert response.status_code == 403 and response.json()["error"]["code"] == "forbidden", response.text
    assert not env.backend.calls
    assert held.calls == [("cpu", True)]
    assert env.service._current_recipe(env.project["project_id"])["revision"] == 1
    assert_no_request_write(env)


@pytest.mark.parametrize("operation", ["run.submit", "run.resume"])
@pytest.mark.parametrize("change", ["grant", "policy"])
def test_revocation_during_discovery_prevents_later_allocating_probe(environment, monkeypatch, change, operation):
    env = environment("mps")
    auth = TokenAuth({"first": OWNER, "second": OWNER})
    app = create_app(env.service, auth)
    held = HeldProbe(env.service, allocate=False)
    monkeypatch.setattr(placement, "_probe", held)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, "/v1/operations/" + operation, json=request(env, operation),
                             headers={"Authorization": "Bearer first"})
        try:
            assert held.entered.wait(3)
            if change == "grant":
                auth._tokens.pop("first")
            else:
                env.service.invoke("policy.update", {"project_id": env.project["project_id"], "expected_revision": 2,
                    "changes": {"allowed_targets": []}, "idempotency_key": "revocation"},
                    auth.authenticate("Bearer second"))
        finally:
            held.release.set()
        response = future.result(timeout=3)
    assert response.json()["error"]["code"] == ("forbidden" if change == "grant" else "run_preparation_changed")
    assert held.calls == [("mps", False)]
    assert not env.backend.calls
    assert_no_request_write(env)


def test_probe_cache_is_request_local_copied_and_allocation_specific(environment, monkeypatch):
    env = environment("mps")
    calls = []
    def checked(backend, device, *, allocate=False):
        calls.append((device, allocate))
        return {**mock_result(device, allocate), "details": {"marker": "unchanged"}}
    monkeypatch.setattr(placement, "_probe", checked)
    entries = {}
    with placement.probe_session(entries, compute=True):
        first = placement.probe(env.backend, "mps")
        first["details"]["marker"] = "caller mutation"
        assert placement.probe(env.backend, "mps")["details"]["marker"] == "unchanged"
    with placement.probe_session(entries, compute=False):
        assert placement.probe(env.backend, "mps")["available"]
        with pytest.raises(BackendError) as missing:
            placement.probe(env.backend, "mps", allocate=True)
        assert missing.value.code == "placement_preparation_changed"
    with placement.probe_session({}, compute=False):
        with pytest.raises(BackendError) as fresh:
            placement.probe(env.backend, "mps")
        assert fresh.value.code == "placement_preparation_changed"
    # Leaving the context cannot leak an earlier request's cache.
    assert placement.probe(env.backend, "mps")["available"]
    assert calls == [("mps", False), ("mps", False)]


@pytest.mark.parametrize("change", ["interpreter", "environment", "source_settings"])
def test_stale_probe_key_fails_closed_without_io(environment, monkeypatch, change):
    env = environment()
    calls = []
    def checked(backend, device, *, allocate=False):
        calls.append((device, allocate))
        return mock_result(device, allocate)
    monkeypatch.setattr(placement, "_probe", checked)
    entries = {}
    with placement.probe_session(entries, compute=True):
        placement.probe(env.backend, "cpu", allocate=True)
    if change == "interpreter":
        env.interpreter.write_text("Changed interpreter identity.\n")
    elif change == "environment":
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "changed-fixture-value")
    else:
        env.backend.dependency_paths["new"] = "new-path"
    with placement.probe_session(entries, compute=False):
        with pytest.raises(BackendError) as error:
            placement.probe(env.backend, "cpu", allocate=True)
        assert error.value.code == "placement_preparation_changed"
    assert calls == [("cpu", True)]


def test_simultaneous_probe_sessions_do_not_share_prepared_results(environment, monkeypatch):
    env = environment()
    barrier = threading.Barrier(2)
    calls = []
    def checked(backend, device, *, allocate=False):
        calls.append(threading.get_ident())
        barrier.wait(timeout=3)
        return {**mock_result(device, allocate), "request_thread": threading.get_ident()}
    monkeypatch.setattr(placement, "_probe", checked)
    def one_request():
        with placement.probe_session({}, compute=True):
            first = placement.probe(env.backend, "cpu", allocate=True)
            second = placement.probe(env.backend, "cpu", allocate=True)
            assert first == second
            return first["request_thread"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(one_request) for _ in range(2)]
        identifiers = [future.result(timeout=5) for future in futures]
    assert len(set(identifiers)) == 2 and sorted(calls) == sorted(identifiers)


def bind_small_jsonl(env):
    from modelbuilder.dataset import DatasetRegistry
    service, pid = env.service, env.project["project_id"]
    root = env.source.parent / "data"
    root.mkdir()
    path = root / "examples.jsonl"
    raw = b'{"id":"one","text":"inert bounded sample"}\n'
    path.write_bytes(raw)
    roots = {pid: {"data": str(root)}}
    service.dataset_roots = deepcopy(roots)
    service._dataset_registry = DatasetRegistry(service.store, project_roots=roots)
    manifest = {"name": "Inert fixture", "benchmark": "placement-test", "license": "CC0-1.0",
        "provenance": {"source": "locally authored test data"}, "id_column": "id",
        "sources": [{"id": "examples", "root_id": "data", "path": path.name, "format": "jsonl",
                     "sha256": hashlib.sha256(raw).hexdigest()}],
        "row_schema": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
                       "required": ["id", "text"], "additionalProperties": False},
        "splits": [{"id": "train", "role": "train", "sample_ids": ["one"]}]}
    record = service.invoke("dataset.register", {"project_id": pid, "manifest": manifest,
                            "idempotency_key": "data"}, OWNER)
    service.invoke("dataset.bind", {"project_id": pid, "dataset_id": record["dataset_id"],
        "dataset_revision": 1, "recipe_id": env.project["recipe_id"], "expected_revision": 1,
        "split_id": "train", "purpose": "training", "node_id": "bound", "idempotency_key": "bind"}, OWNER)
    return root, path, raw, record


@pytest.mark.parametrize("change", ["bytes", "root_identity", "none"])
def test_held_placement_preserves_pinned_dataset_bytes_and_root_guards(environment, monkeypatch, change):
    env = environment()
    root, path, raw, record = bind_small_jsonl(env)
    service, pid = env.service, env.project["project_id"]
    original_recipe = service._current_recipe(pid)
    held = HeldProbe(service, allocate=True)
    monkeypatch.setattr(placement, "_probe", held)
    payload = {**request(env, "run.submit"), "expected_revision": 2}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.invoke, "run.submit", payload, OWNER)
        try:
            assert held.entered.wait(3)
            if change == "bytes":
                path.write_bytes(raw.replace(b"inert", b"altered"))
            elif change == "root_identity":
                root.rename(root.with_name("retired-data"))
                root.mkdir()
                path.write_bytes(raw)  # Matching bytes cannot revive a replaced root grant.
        finally:
            held.release.set()
        if change == "none":
            result = future.result(timeout=3)
            assert result["status"] == "submitted"
            assert len(env.backend.executed) == 1
            submitted = env.backend.executed[0][1]
            assert {node["id"] for node in submitted["recipe"]["nodes"]} == {"train", "bound"}
        else:
            with pytest.raises(ServiceError) as error:
                future.result(timeout=3)
            assert error.value.code == ("dataset_checksum_mismatch" if change == "bytes" else "dataset_root_changed")
            assert not env.backend.calls
            assert_no_request_write(env)
    assert held.calls == [("cpu", True)]
    assert service._current_recipe(pid) == original_recipe
    assert service._dataset_registry.get(pid, record["dataset_id"], 1) == record
    assert not env.backend.dependency_paths
    assert str(root) not in json.dumps(original_recipe)


def test_missing_prepared_commit_cache_refuses_without_second_probe(environment, monkeypatch):
    from modelbuilder import dataset_preparation
    env = environment()
    calls = []
    def checked(backend, device, *, allocate=False):
        assert not env.service.store.connection.in_transaction
        calls.append((device, allocate))
        return mock_result(device, allocate)
    monkeypatch.setattr(placement, "_probe", checked)
    original = dataset_preparation.prepare
    def lose_prepared_probe(*args, **kwargs):
        state, datasets, previous, _ = original(*args, **kwargs)
        return state, datasets, previous, {}
    monkeypatch.setattr(dataset_preparation, "prepare", lose_prepared_probe)
    with pytest.raises(ServiceError) as error:
        env.service.invoke("run.submit", request(env, "run.submit"), OWNER)
    assert error.value.code == "placement_preparation_changed"
    assert calls == [("cpu", True)]
    assert_no_request_write(env)


def test_unavailable_prepared_probe_is_replayed_without_retry_under_writer(environment, monkeypatch):
    env = environment()
    calls = []
    def unavailable(backend, device, *, allocate=False):
        assert not env.service.store.connection.in_transaction
        calls.append((device, allocate))
        return {"available": False, "device": device, "reason": "Explicitly mocked unavailable device"}
    monkeypatch.setattr(placement, "_probe", unavailable)
    with pytest.raises(ServiceError) as error:
        env.service.invoke("run.submit", request(env, "run.submit"), OWNER)
    assert error.value.code == "capability_unavailable"
    assert calls == [("cpu", True)]
    assert_no_request_write(env)


@pytest.mark.parametrize("change", ["bytes", "root_identity"])
def test_metadata_probe_dataset_change_refuses_before_allocation(environment, monkeypatch, change):
    env = environment("mps")
    root, path, raw, _ = bind_small_jsonl(env)
    calls = []
    def checked(backend, device, *, allocate=False):
        assert not env.service.store._lock._is_owned()
        calls.append((device, allocate))
        assert allocate is False, "Changed dataset must refuse before allocation"
        if change == "bytes":
            path.write_bytes(raw.replace(b"inert", b"altered"))
        else:
            root.rename(root.with_name("retired-data"))
            root.mkdir()
            path.write_bytes(raw)
        return mock_result(device, allocate)
    monkeypatch.setattr(placement, "_probe", checked)
    with pytest.raises(ServiceError) as error:
        env.service.invoke("run.submit", {**request(env, "run.submit"), "expected_revision": 2}, OWNER)
    assert error.value.code == ("dataset_checksum_mismatch" if change == "bytes" else "dataset_root_changed")
    assert calls == [("mps", False)]
    assert_no_request_write(env)


def test_metadata_probe_checkout_change_refuses_before_allocation(environment, monkeypatch):
    env = environment("mps", bound=True)
    # Resolve a genuinely different approved checkout from the base backend.
    base = env.source.parent / "base-repository"
    base.mkdir()
    env.backend.repository = base
    calls = []
    def checked(backend, device, *, allocate=False):
        assert not env.service.store._lock._is_owned()
        calls.append((device, allocate))
        assert allocate is False, "Changed checkout must refuse before allocation"
        original = env.source / ".git"
        retired = env.source.parent / "retired-git"
        original.rename(retired)
        shutil.copytree(retired, original)
        return mock_result(device, allocate)
    monkeypatch.setattr(placement, "_probe", checked)
    with pytest.raises(ServiceError) as error:
        env.service.invoke("run.submit", request(env, "run.submit"), OWNER)
    assert error.value.code == "source_binding_changed"
    assert calls == [("mps", False)]
    assert_no_request_write(env)


@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory", "oversized"])
def test_unsafe_venv_configuration_refuses_without_blocking_writer(tmp_path, kind):
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO fixture requires POSIX")
    interpreter = tmp_path / "venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("inert identity fixture")
    config = interpreter.parent.parent / "pyvenv.cfg"
    if kind == "fifo":
        os.mkfifo(config)
    elif kind == "symlink":
        target = tmp_path / "ordinary.cfg"
        target.write_text("home = fixture")
        config.symlink_to(target)
    elif kind == "directory":
        config.mkdir()
    else:
        config.write_bytes(b"x" * 16385)
    # A subprocess deadline makes a regression's blocking FIFO open safely fail.
    program = """
import sys
from types import SimpleNamespace
from modelbuilder.backend.placement import backend_identity, validate_placement
from modelbuilder.backend.admission import BackendError
from modelbuilder.store import SQLiteStore
backend = SimpleNamespace(python=sys.argv[1])
store = SQLiteStore(sys.argv[2])
with store.transaction():
    assert backend_identity(backend)['venv_config']['invalid']
    try:
        validate_placement(backend, {'recipe': {'nodes': []}})
    except BackendError as error:
        assert error.code == 'capability_unavailable'
    else:
        raise AssertionError('Unsafe configuration was admitted')
store.close()
print('refused')
"""
    result = subprocess.run([sys.executable, "-c", program, str(interpreter), str(tmp_path / "store.sqlite")],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == "refused", result.stderr


def test_regular_venv_configuration_digest_and_replacement_are_fenced(environment, monkeypatch):
    env = environment()
    config = env.interpreter.parent.parent / "pyvenv.cfg"
    raw = b"home = /approved/python\ninclude-system-site-packages = false\n"
    config.write_bytes(raw)
    first = placement.backend_identity(env.backend)
    assert first["venv_config"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert placement.validate_placement(env.backend, {"recipe": {"nodes": []}})["device"] == "cpu"
    config.rename(config.with_suffix(".old"))
    config.write_bytes(raw)
    assert placement.backend_identity(env.backend) != first
    monkeypatch.delattr(placement.os, "O_NOFOLLOW")
    with pytest.raises(BackendError, match="no-follow"):
        placement.validate_placement(env.backend, {"recipe": {"nodes": []}})


def test_venv_descriptor_replacement_during_read_refuses(environment, monkeypatch):
    env = environment()
    config = env.interpreter.parent.parent / "pyvenv.cfg"
    config.write_text("home = original")
    original_read = placement.os.read
    def replaced(descriptor, length):
        raw = original_read(descriptor, length)
        config.rename(config.with_suffix(".old"))
        config.write_bytes(raw)
        return raw
    monkeypatch.setattr(placement.os, "read", replaced)
    assert placement.backend_identity(env.backend)["venv_config"]["invalid"]
