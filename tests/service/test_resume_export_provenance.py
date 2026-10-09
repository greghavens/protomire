"""Service regressions for resume selection and authored-checkout provenance."""
from copy import deepcopy
import hashlib
from pathlib import Path
import subprocess

import pytest

from modelbuilder.models import Actor, ServiceError
from modelbuilder import recipe as recipe_module
from modelbuilder.recipe import default_recipe, export_bundle, import_bundle
from modelbuilder.service import OperationService

OWNER = Actor("reviewer", {"read", "modify", "execute", "publish", "spend"})


class NonexecutingBackend:
    def __init__(self):
        self.calls = []

    def invoke(self, operation, payload, actor):
        self.calls.append((operation, deepcopy(payload)))
        return {"operation_id": payload.get("operation_id"), "project_id": payload["project_id"],
                "backend_run_id": "native-" + payload["operation_id"], "status": "submitted", "worker_alive": False}


def project(service, recipe, key="model"):
    return service.invoke("project.create", {"slug": key, "model_name": key, "recipe": recipe,
                                           "idempotency_key": key}, OWNER)


def training_recipe():
    recipe = default_recipe("template", "template", "training")
    recipe["nodes"] = [{"id": "train", "kind": "model.train", "implementation_ref": "builtin:model.train",
        "inputs": [], "outputs": [], "parameters": {"steps": 4, "learning_rate": 0.1}, "environment_ref": "default",
        "resource_request": {}, "retry": {"max_attempts": 1, "backoff_seconds": 0}, "timeout": 30,
        "cache_policy": "disabled", "provenance": {}}]
    return recipe


@pytest.mark.parametrize("mode,expected_rate", [("warm_start", 0.01), ("exact_state", 0.1)])
def test_resume_preserves_selected_recipe_parameters_and_parent_history(tmp_path, mode, expected_rate):
    backend = NonexecutingBackend()
    service = OperationService(tmp_path / "service.db", backend=backend)
    try:
        p = project(service, training_recipe())
        parent = service.invoke("run.submit", {"project_id": p["project_id"], "recipe_id": p["recipe_id"],
            "expected_revision": 1, "trusted": True, "idempotency_key": "parent"}, OWNER)
        edited = deepcopy(service._current_recipe(p["project_id"]))
        edited["nodes"][0]["parameters"]["learning_rate"] = 0.01
        edited = service.invoke("recipe.update", {"project_id": p["project_id"], "recipe_id": p["recipe_id"],
            "expected_revision": 1, "recipe": edited, "idempotency_key": "edit"}, OWNER)
        resumed = service.invoke("run.resume", {"project_id": p["project_id"], "operation_id": parent["operation_id"],
            "mode": mode, "expected_recipe_revision": 2, "steps": 6, "trusted": True,
            "idempotency_key": "warm-start"}, OWNER)
        assert resumed["recipe_id"] == p["recipe_id"]
        assert service._current_recipe(p["project_id"])["nodes"][0]["parameters"]["learning_rate"] == expected_rate
        assert service._current_recipe(p["project_id"])["nodes"][0]["parameters"]["steps"] == 6
        assert service._get("recipe", p["recipe_id"], p["project_id"], 1)["nodes"][0]["parameters"] == {"steps": 4, "learning_rate": 0.1}
        assert service._get("recipe", p["recipe_id"], p["project_id"], 2) == edited
        assert backend.calls[-1][1]["recipe"]["nodes"][0]["parameters"]["learning_rate"] == expected_rate
    finally:
        service.store.close()


@pytest.mark.parametrize("trainers", [0, 2])
def test_builtin_warm_start_rejects_ambiguous_current_trainer_without_mutation(tmp_path, trainers):
    backend = NonexecutingBackend()
    service = OperationService(tmp_path / "service.db", backend=backend)
    try:
        p = project(service, training_recipe())
        parent = service.invoke("run.submit", {"project_id": p["project_id"], "recipe_id": p["recipe_id"],
            "expected_revision": 1, "trusted": True, "idempotency_key": "parent"}, OWNER)
        edited = deepcopy(service._current_recipe(p["project_id"]))
        edited["nodes"] = [dict(deepcopy(edited["nodes"][0]), id=f"train-{index}") for index in range(trainers)]
        edited = service.invoke("recipe.update", {"project_id": p["project_id"], "recipe_id": p["recipe_id"],
            "expected_revision": 1, "recipe": edited, "idempotency_key": "edit"}, OWNER)
        with pytest.raises(ServiceError) as error:
            service.invoke("run.resume", {"project_id": p["project_id"], "operation_id": parent["operation_id"],
                "mode": "warm_start", "expected_recipe_revision": 2, "steps": 6, "trusted": True,
                "idempotency_key": "warm-start"}, OWNER)
        assert error.value.code == "capability_unavailable"
        assert service._current_recipe(p["project_id"]) == edited
        assert len(backend.calls) == 1
    finally:
        service.store.close()


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


def checkout(tmp_path, layout="root"):
    repository = tmp_path / "repository"
    repository.mkdir()
    nested = "nested" in layout
    source = repository / "sources" if nested else repository
    source.mkdir(exist_ok=True)
    (source / "model.py").write_text("value = 'committed'\n")
    git(repository, "init", "-q")
    git(repository, "add", ".")
    git(repository, "-c", "user.name=Review", "-c", "user.email=review@example.invalid", "commit", "-qm", "Fixture")
    commit = git(repository, "rev-parse", "HEAD")
    if "worktree" in layout:
        worktree = tmp_path / "worktree"
        git(repository, "worktree", "add", "--detach", str(worktree), commit)
        assert (worktree / ".git").is_file()
        source = worktree / "sources" if nested else worktree
    return source, commit


def source_recipe(root, commit):
    recipe = default_recipe("template", "template", "source")
    recipe["source"] = {"repository": "https://example.invalid/model", "commit": commit,
        "files": [{"path": "model.py", "sha256": hashlib.sha256((root / "model.py").read_bytes()).hexdigest()}]}
    return recipe


def export(service, p, format="object"):
    return service.invoke("recipe.export", {"project_id": p["project_id"], "recipe_id": p["recipe_id"],
        "format": format}, OWNER)


@pytest.mark.parametrize("layout", ["root", "nested", "worktree", "worktree-nested"])
@pytest.mark.parametrize("dirty", [False, True])
def test_authored_export_binds_packaged_bytes_to_git_commit(tmp_path, layout, dirty):
    root, commit = checkout(tmp_path, layout)
    if dirty:
        (root / "model.py").write_text("value = 'uncommitted'\n")
    service = OperationService(tmp_path / "service.db", source_root=root)
    try:
        p = project(service, source_recipe(root, commit))
        before = service._current_recipe(p["project_id"])
        if dirty:
            for format in ("object", "json"):
                with pytest.raises(ServiceError) as error:
                    export(service, p, format)
                assert error.value.code == "dirty_source"
        else:
            bundle = export(service, p)
            assert bundle["recipe"]["source"]["commit"] == commit
            assert import_bundle(bundle)["execution_allowed"] is False
        assert service._current_recipe(p["project_id"]) == before
    finally:
        service.store.close()


@pytest.mark.parametrize("marker", ["file", "directory", "dangling-symlink"])
def test_malformed_nearest_git_marker_does_not_fall_back_to_inert_or_ancestor(tmp_path, marker):
    root, commit = checkout(tmp_path, "nested")
    metadata = root / ".git"
    if marker == "file":
        metadata.write_text("not a Git directory reference\n")
    elif marker == "directory":
        metadata.mkdir()
    else:
        metadata.symlink_to(root / "absent-git-dir")
    service = OperationService(tmp_path / "service.db", source_root=root)
    try:
        p = project(service, source_recipe(root, commit))
        with pytest.raises(ServiceError) as error:
            export(service, p)
        assert error.value.code == "source_revision_unavailable"
    finally:
        service.store.close()


@pytest.mark.parametrize("revision", ["missing", "tree"])
def test_git_export_requires_available_commit_object(tmp_path, revision):
    root, commit = checkout(tmp_path)
    commit = "a" * 40 if revision == "missing" else git(root, "rev-parse", "HEAD^{tree}")
    service = OperationService(tmp_path / "service.db", source_root=root)
    try:
        p = project(service, source_recipe(root, commit))
        with pytest.raises(ServiceError) as error:
            export(service, p)
        assert error.value.code == "source_revision_unavailable"
    finally:
        service.store.close()


def test_git_export_rejects_untracked_source_with_matching_manifest_hash(tmp_path):
    root, commit = checkout(tmp_path)
    (root / "new.py").write_text("value = 'untracked'\n")
    recipe = source_recipe(root, commit)
    recipe["source"]["files"].append({"path": "new.py", "sha256": hashlib.sha256((root / "new.py").read_bytes()).hexdigest()})
    service = OperationService(tmp_path / "service.db", source_root=root)
    try:
        p = project(service, recipe)
        with pytest.raises(ServiceError) as error:
            export(service, p)
        assert error.value.code == "source_revision_unavailable"
    finally:
        service.store.close()


def test_git_provenance_checks_packaged_bytes_not_a_second_working_tree_read(tmp_path, monkeypatch):
    root, commit = checkout(tmp_path)
    committed = (root / "model.py").read_bytes()
    (root / "model.py").write_text("value = 'uncommitted'\n")
    original_export = recipe_module.export_bundle

    def export_then_restore(*args, **kwargs):
        bundle = original_export(*args, **kwargs)
        (root / "model.py").write_bytes(committed)
        return bundle

    monkeypatch.setattr(recipe_module, "export_bundle", export_then_restore)
    service = OperationService(tmp_path / "service.db", source_root=root)
    try:
        p = project(service, source_recipe(root, commit))
        with pytest.raises(ServiceError) as error:
            export(service, p)
        assert error.value.code == "dirty_source"
    finally:
        service.store.close()


def test_git_replacements_and_inherited_git_environment_cannot_spoof_revision(tmp_path, monkeypatch):
    root, commit = checkout(tmp_path)
    (root / "model.py").write_text("value = 'different'\n")
    git(root, "add", ".")
    git(root, "-c", "user.name=Review", "-c", "user.email=review@example.invalid", "commit", "-qm", "Changed")
    git(root, "replace", commit, git(root, "rev-parse", "HEAD"))
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "0")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-the-source"))
    service = OperationService(tmp_path / "service.db", source_root=root)
    try:
        p = project(service, source_recipe(root, commit))
        with pytest.raises(ServiceError) as error:
            export(service, p)
        assert error.value.code == "dirty_source"
    finally:
        service.store.close()


def test_git_free_imported_closure_reexports_with_manifest_integrity(tmp_path, monkeypatch):
    # Isolate this fixture from unrelated ancestor repositories (including sandbox
    # .git sentinels). The fixture and every other checkout-detection test stay real.
    for method in ("exists", "is_symlink"):
        original = getattr(Path, method)
        monkeypatch.setattr(Path, method, lambda path, original=original:
                            False if path.name == ".git" and not path.is_relative_to(tmp_path) else original(path))
    source = tmp_path / "original-snapshot"
    source.mkdir()
    (source / "model.py").write_text("value = 'immutable'\n")
    bundle = export_bundle(source_recipe(source, "a" * 40), source_root=source)
    extracted = tmp_path / "extracted-snapshot"
    imported = import_bundle(bundle, extracted)
    assert not (extracted / ".git").exists()

    def no_git(*args, **kwargs):
        raise AssertionError("Inert snapshot export must not invoke Git")

    monkeypatch.setattr(subprocess, "run", no_git)
    assert export_bundle(imported["recipe"], source_root=extracted) == bundle
    service = OperationService(tmp_path / "service.db", source_root=extracted)
    try:
        p = project(service, default_recipe("template", "template"))
        service.invoke("recipe.import", {"project_id": p["project_id"], "expected_revision": 1,
            "bundle": bundle, "idempotency_key": "import"}, OWNER)
        reexported = export(service, p)
        assert reexported["files"] == bundle["files"]
        assert reexported["recipe"]["source"] == bundle["recipe"]["source"]
        assert import_bundle(reexported)["execution_allowed"] is False
        (extracted / "model.py").write_text("value = 'tampered'\n")
        with pytest.raises(ServiceError) as error:
            export(service, p)
        assert error.value.code == "source_hash_mismatch"
    finally:
        service.store.close()


def test_core_bundle_export_remains_git_free_even_in_authored_checkout(tmp_path, monkeypatch):
    root, commit = checkout(tmp_path)
    (root / "model.py").write_text("value = 'uncommitted'\n")

    def no_git(*args, **kwargs):
        raise AssertionError("Core bundle API only verifies manifest integrity")

    monkeypatch.setattr(subprocess, "run", no_git)
    bundle = export_bundle(source_recipe(root, commit), source_root=root)
    assert import_bundle(bundle)["execution_allowed"] is False
