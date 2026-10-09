"""Approved source routing never imports code, accepts paths, or changes global state."""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from modelbuilder.api import create_app
from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.operator_config import load_operator_config
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService

OWNER = Actor("owner", {"read", "modify", "execute"})


def repository(path, marker):
    path.mkdir()
    (path / "model.py").write_text(f"def run(*, inputs, parameters, context):\n    return {{'value': {marker}}}\n")
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Independent model"], check=True)
    return path


def recipe(root):
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    digest = hashlib.sha256((root / "model.py").read_bytes()).hexdigest()
    return {"schema_version": "0", "recipe_id": str(uuid.uuid4()), "project_id": str(uuid.uuid4()), "revision": 1,
        "name": "Independent model", "source": {"repository": "https://example.invalid/not-a-source-fallback", "commit": commit,
        "files": [{"path": "model.py", "sha256": digest}]}, "nodes": [{"id": "model", "kind": "component.run",
        "implementation_ref": {"module": "model", "entrypoint": "run", "path": "model.py", "commit": commit, "sha256": digest},
        "inputs": [], "outputs": [{"name": "value", "type": "int"}], "parameters": {}, "timeout": 10,
        "environment_ref": "default", "resource_request": {"gpu": 0}, "cache_policy": "disabled", "provenance": {},
        "retry": {"max_attempts": 1, "backoff_seconds": 0}, "component": {"id": "independent.model", "version": "1", "protocol": "modelbuilder.component/v1",
        "role": "utility", "parameter_schema": {"type": "object"}, "capabilities": {"resume_modes": [], "export_formats": [], "frameworks": []}}}],
        "edges": [], "resources": [], "external_dependencies": [], "environment": {"python": "3.12", "requirements": []},
        "evaluation": {}, "outputs": [], "licenses": [{"scope": "source", "license": "MIT", "redistribution_allowed": True}]}


@pytest.fixture
def setup(tmp_path):
    roots = {alias: {"path": str(repository(tmp_path / alias, i)), "trusted": True} for i, alias in enumerate(("first", "second"))}
    backend = ZenMLBackend(tmp_path / "state", python_executable=sys.executable)
    service = OperationService(tmp_path / "service.db", backend=backend, source_roots=roots)
    yield service, roots
    service.store.close()


def create(service, alias, **extra):
    item = next(s for s in service.invoke("project.source.list", {}, OWNER)["sources"] if s["alias"] == alias)
    return service.invoke("project.create", {"slug": alias + str(uuid.uuid4())[:8], "model_name": alias, "source_alias": alias,
        "source_repository_id": item["repository_id"], "trusted": True, "idempotency_key": str(uuid.uuid4()), **extra}, OWNER)


def bind(service, project, alias, **extra):
    item = service.source_workspaces.inspect(alias, project["project_id"])
    return service.invoke("project.source.bind", {"project_id": project["project_id"], "expected_revision": project["revision"],
        "source_alias": alias, "source_repository_id": item["repository_id"], "trusted": True, "idempotency_key": str(uuid.uuid4()), **extra}, OWNER)


def test_binding_and_status_are_inert_revisioned_and_durable(setup):
    service, roots = setup
    p = create(service, "first", recipe=recipe(Path(roots["first"]["path"])))
    result = service.invoke("project.source.status", {"project_id": p["project_id"]}, OWNER)
    assert result["pinned_source_matches"] and result["workspace_path"] == roots["first"]["path"]
    assert not (service.backend.root / "jobs").exists()
    changed = bind(service, p, "second")
    assert changed["recipe_id"] == p["recipe_id"] and changed["revision"] == 2
    assert changed["source_binding"]["repository_id"] != p["source_binding"]["repository_id"]
    with pytest.raises(ServiceError) as error:
        bind(service, p, "first")
    assert error.value.code == "revision_conflict"
    reopened = OperationService(service.store, backend=service.backend, source_roots=roots)
    assert reopened.invoke("project.get", {"project_id": p["project_id"]}, OWNER) == changed
    assert service.store.resource_versions("project", p["project_id"])[0]["source_binding"] == p["source_binding"]


def test_paths_urls_unknown_alias_and_untrusted_bind_are_rejected(setup):
    service, _ = setup
    p = create(service, "first")
    for alias in ("../first", "/tmp", "https://example.invalid/code", "absent"):
        with pytest.raises(ServiceError) as error:
            service.invoke("project.source.bind", {"project_id": p["project_id"], "expected_revision": 1,
                "source_alias": alias, "source_repository_id": p["source_binding"]["repository_id"], "trusted": True,
                "idempotency_key": str(uuid.uuid4())}, OWNER)
        assert error.value.code in {"source_not_allowed", "validation_error"}
    with pytest.raises(ServiceError) as error:
        bind(service, p, "second", trusted=False)
    assert error.value.code == "source_trust_required"
    with pytest.raises(ServiceError) as error:
        bind(service, p, "second", path="/tmp")
    assert error.value.code == "validation_error"
    with pytest.raises(ServiceError) as error:
        bind(service, p, "second", source_repository_id=p["source_binding"]["repository_id"])
    assert error.value.code == "source_identity_conflict"


def test_allowlist_and_actor_scope_reject_cross_project_access(setup):
    service, _ = setup
    first, second = create(service, "first"), create(service, "second")
    restricted = Actor("scoped", {"read", "modify", "execute"}, {first["project_id"]})
    for operation, payload in [("project.source.list", {}), ("project.source.list", {"project_id": second["project_id"]}),
                               ("project.source.status", {"project_id": second["project_id"]})]:
        with pytest.raises(ServiceError) as error:
            service.invoke(operation, payload, restricted)
        assert error.value.code == "forbidden"
    service.source_workspaces.aliases["second"]["project_ids"] = [second["project_id"]]
    assert [s["alias"] for s in service.invoke("project.source.list", {"project_id": first["project_id"]}, restricted)["sources"]] == ["first"]
    with pytest.raises(ServiceError) as error:
        bind(service, first, "second")
    assert error.value.code == "source_not_allowed"


def test_alias_repin_and_missing_binding_fail_closed_without_fallback(setup):
    service, roots = setup
    p = create(service, "first", recipe=recipe(Path(roots["first"]["path"])))
    service.source_workspaces.aliases["first"]["path"] = roots["second"]["path"]
    status = service.invoke("project.source.status", {"project_id": p["project_id"]}, OWNER)
    assert status["diagnostics"][0]["code"] == "source_binding_changed"
    plan = service.invoke("run.plan", {"project_id": p["project_id"], "recipe_id": p["recipe_id"], "trusted": True}, OWNER)
    assert not plan["executable"] and plan["blocked_reasons"][0]["code"] == "source_binding_changed"
    unbound = service.invoke("project.create", {"slug": "unbound", "model_name": "Unbound", "idempotency_key": "unbound"}, OWNER)
    plan = service.invoke("run.plan", {"project_id": unbound["project_id"], "recipe_id": unbound["recipe_id"], "trusted": True}, OWNER)
    assert not plan["executable"] and plan["blocked_reasons"][0]["code"] == "source_binding_required"
    assert not (service.backend.root / "jobs").exists()


def test_untrusted_operator_root_and_changed_or_escaping_source_are_blocked(setup, tmp_path):
    service, roots = setup
    source = Path(roots["first"]["path"])
    p = create(service, "first", recipe=recipe(source))
    payload = {"project_id": p["project_id"], "recipe_id": p["recipe_id"], "trusted": True}
    assert service.invoke("run.plan", payload, OWNER)["executable"]
    service.source_workspaces.aliases["first"]["trusted"] = False
    assert service.invoke("run.plan", payload, OWNER)["blocked_reasons"][0]["code"] == "untrusted_execution_blocked"
    service.source_workspaces.aliases["first"]["trusted"] = True
    original = (source / "model.py").read_bytes()
    (source / "model.py").write_text("raise RuntimeError('must never be imported')")
    assert service.invoke("run.plan", payload, OWNER)["blocked_reasons"][0]["code"] == "dependency_mismatch"
    (source / "model.py").unlink()
    outside = tmp_path / "outside.py"
    outside.write_bytes(original)
    (source / "model.py").symlink_to(outside)
    assert service.invoke("run.plan", payload, OWNER)["blocked_reasons"][0]["code"] == "dependency_mismatch"


def test_request_local_views_cannot_race_or_leak_dependency_routes(setup):
    service, roots = setup
    projects = [create(service, alias) for alias in roots]
    baseline = (service.backend.repository, copy.deepcopy(service.backend.dependency_paths), service.backend.trusted_code_roots[:])
    for alias in roots:
        service.source_workspaces.aliases[alias]["dependency_paths"] = {"same-id": str(Path(roots[alias]["path"]) / "data.json")}
    def route(project):
        payload = {"project_id": project["project_id"]}
        view = service._source_backend("run.submit", payload)
        alias = project["source_binding"]["alias"]
        assert view.root == service.backend.root
        assert str(view.repository) == roots[alias]["path"]
        assert view.dependency_paths["same-id"] == str(view.repository / "data.json")
        return payload["source_binding"]
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(route, projects * 12))
    assert len(results) == 24
    assert baseline == (service.backend.repository, service.backend.dependency_paths, service.backend.trusted_code_roots)


def test_exact_resume_uses_parent_source_but_warm_start_uses_current(setup):
    service, _ = setup
    p = create(service, "first")
    operation_id = str(uuid.uuid4())
    service.store.put("operation", operation_id, p["project_id"], {"operation_id": operation_id, "project_id": p["project_id"],
        "source_binding": p["source_binding"], "revision": 1})
    changed = bind(service, p, "second")
    payload = {"project_id": p["project_id"], "parent_operation_id": operation_id, "mode": "exact_state"}
    assert service._source_backend("run.resume", payload).repository.name == "first"
    assert payload["source_binding"] == p["source_binding"]
    payload = {**payload, "mode": "warm_start"}
    assert service._source_backend("run.resume", payload).repository.name == "second"
    assert payload["source_binding"] == changed["source_binding"]
    service.source_workspaces.aliases.pop("first")
    assert service._source_backend("run.status", payload) is service.backend
    with pytest.raises(ServiceError) as error:
        service._source_backend("run.resume", {**payload, "mode": "exact_state"})
    assert error.value.code == "source_not_allowed"


def test_operator_config_factory_and_transport_expose_same_contract(tmp_path, monkeypatch):
    config = tmp_path / "operator.json"
    config.write_text(json.dumps({"source_roots": {"model": {"path": "model", "trusted": True,
        "dependency_paths": {"data": "data.json"}, "project_ids": ["project"]}}}))
    parsed = load_operator_config(config)
    assert parsed["source_roots"]["model"]["path"] == str(tmp_path / "model")
    assert parsed["source_roots"]["model"]["dependency_paths"] == {"data": str(tmp_path / "data.json")}
    monkeypatch.setenv("MODELBUILDER_OPERATOR_CONFIG", str(config))
    monkeypatch.setenv("MODELBUILDER_DB_PATH", str(tmp_path / "state/service.db"))
    app = create_app(tokens={})
    assert app.state.service.require_source_binding
    for name in ("project.source.bind", "project.source.list", "project.source.status"):
        assert "/v1/operations/" + name in app.openapi()["paths"]
    app.state.service.store.close()


def test_project_capabilities_follow_bound_source_without_global_mutation(setup):
    service, _ = setup
    p = create(service, "first")
    capabilities = service.invoke("execution.targets", {"project_id": p["project_id"]}, OWNER)
    assert capabilities["capabilities"]["arbitrary_python"]["configured"]
    assert capabilities["source_workspace"]["alias"] == "first"
    assert not service.backend.trusted_code_roots
    service.source_workspaces.aliases.pop("first")
    unavailable = service.invoke("execution.targets", {"project_id": p["project_id"]}, OWNER)
    assert not unavailable["targets"][0]["available"]
    assert unavailable["source_diagnostics"][0]["code"] == "source_not_allowed"


def test_export_and_dependency_inspection_use_bound_checkout(setup):
    service, roots = setup
    p = create(service, "first", recipe=recipe(Path(roots["first"]["path"])))
    payload = {"project_id": p["project_id"], "recipe_id": p["recipe_id"]}
    bundle = service.invoke("recipe.export", payload, OWNER)
    assert bundle
    resolved = service.invoke("recipe.resolve_dependencies", payload, OWNER)
    assert any(item["id"] == "model.py" for item in resolved["resolved"])
    bind(service, p, "second")
    with pytest.raises(ServiceError):
        service.invoke("recipe.export", payload, OWNER)
    resolved = service.invoke("recipe.resolve_dependencies", payload, OWNER)
    assert any(item["id"] == "model.py" for item in resolved["unresolved"])


def test_source_dependencies_are_isolated_and_hash_checked(setup):
    service, roots = setup
    projects = []
    for alias in roots:
        path = Path(roots[alias]["path"]) / "data.json"
        path.write_text(json.dumps({"model": alias}))
        service.source_workspaces.aliases[alias]["dependency_paths"] = {"data": str(path)}
        body = recipe(path.parent)
        body["resources"] = [{"id": "data", "kind": "dataset", "url": "https://example.invalid/no-fetch",
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "license": "MIT"}]
        project = create(service, alias, recipe=body)
        projects.append(project)
        plan = service.invoke("run.plan", {"project_id": project["project_id"], "recipe_id": project["recipe_id"], "trusted": True}, OWNER)
        assert plan["executable"], plan
    service.source_workspaces.aliases["first"]["dependency_paths"] = service.source_workspaces.aliases["second"]["dependency_paths"]
    first = projects[0]
    plan = service.invoke("run.plan", {"project_id": first["project_id"], "recipe_id": first["recipe_id"], "trusted": True}, OWNER)
    assert not plan["executable"] and plan["blocked_reasons"][0]["code"] == "dependency_mismatch"


def test_historical_rpc_uses_safe_startup_without_original_source_directory(tmp_path, monkeypatch):
    import shutil
    from types import SimpleNamespace
    from modelbuilder.backend.common import read_json, write_json
    original = repository(tmp_path / "original", 1)
    backend = ZenMLBackend(tmp_path / "state", python_executable=sys.executable, source_root=original)
    shutil.rmtree(original)
    calls = []
    def run(command, **kwargs):
        assert command[1:3] == ["-P", "-m"]
        assert kwargs["cwd"] == backend.root
        assert kwargs["env"]["PYTHONSAFEPATH"] == "1"
        request = read_json(Path(command[-1]))
        write_json(Path(request["result"]), {"status": "succeeded"})
        calls.append(command)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("modelbuilder.backend.adapter.subprocess.run", run)
    assert backend._rpc("run.status", {"project_id": "project"})["status"] == "succeeded"
    assert len(calls) == 1


@pytest.mark.parametrize("active_lease", [False, True])
def test_lost_submit_reply_cannot_relabel_job_after_project_rebind(setup, monkeypatch, active_lease):
    from modelbuilder.backend.adapter import fcntl
    from modelbuilder.backend.common import write_json
    if fcntl is None:
        pytest.skip("Native POSIX launch leases required")
    service, roots = setup
    project = create(service, "first", recipe=recipe(Path(roots["first"]["path"])))
    current = service.invoke("recipe.get", {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}, OWNER)
    operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "modelbuilder:owner:run.submit:lost-reply"))
    payload = {"project_id": project["project_id"], "recipe": current, "operation_id": operation_id, "idempotency_key": "lost-reply"}
    accepted_backend = service._source_backend("run.submit", payload)
    job = service.backend.root / "jobs" / operation_id
    write_json(job / "request.json", {**payload, "source_root": str(accepted_backend.repository),
                                      "dependency_paths": accepted_backend.dependency_paths})
    write_json(job / "native.json", {"backend_run_id": "accepted-native-run"})
    # Emulate a surviving native job whose enclosing service transaction rolled back.
    bind(service, project, "second")
    monkeypatch.setattr(service.backend, "_validate_execution", lambda payload: None)
    def rpc(operation, payload):
        assert operation == "backend.validate", "Conflicting retry must never read/relabel the accepted run"
        return {}
    monkeypatch.setattr(service.backend, "_rpc", rpc)
    with (job / "launch.lock").open("a") as lease:
        if active_lease:
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ServiceError) as error:
            service.invoke("run.submit", {"project_id": project["project_id"], "recipe_id": project["recipe_id"],
                "expected_revision": 1, "trusted": True, "idempotency_key": "lost-reply"}, OWNER)
    assert error.value.code == "conflict"
    with pytest.raises(ServiceError) as missing:
        service.store.get("operation", operation_id)
    assert missing.value.code == "not_found"


def test_in_root_source_symlink_is_blocked_before_runtime(setup):
    service, roots = setup
    source = Path(roots["first"]["path"])
    project = create(service, "first", recipe=recipe(source))
    original = (source / "model.py").read_bytes()
    (source / "copy.py").write_bytes(original)
    (source / "model.py").unlink()
    (source / "model.py").symlink_to(source / "copy.py")
    status = service.invoke("project.source.status", {"project_id": project["project_id"]}, OWNER)
    assert not status["pinned_source_matches"]
    plan = service.invoke("run.plan", {"project_id": project["project_id"], "recipe_id": project["recipe_id"], "trusted": True}, OWNER)
    assert not plan["executable"] and plan["blocked_reasons"][0]["code"] == "dependency_mismatch"
