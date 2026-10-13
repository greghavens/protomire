"""Explicit local-authored review evidence for tests; never a production fallback."""
from copy import deepcopy

from modelbuilder.hub_rights import digest, source_reference

USAGE_CONTEXT = {"purpose": "Offline synthetic fixture acceptance", "commercial": False,
                 "deployment": "local test only", "conditions": ["No upstream data or pretrained weights"]}


def local_review(project_id="test-project", *, commit="1" * 40, policy_id="test-local-origin",
                 source_files=None, repo_id="urn:modelbuilder:test-authored-fixture", repo_type="model", source_origin=None):
    source = {"provider": "local", "repo_type": repo_type, "repo_id": repo_id, "commit": commit}
    source_files = source_files if source_files is not None else [{"path": "weights.bin", "sha256": "a" * 64, "size_bytes": 1}]
    evidence = digest({"source": source, "source_files": source_files, "origin": "Test-authored synthetic fixtures; no ownership claim inferred",
                       **({"source_origin": source_origin} if source_origin is not None else {})})
    policy = {"schema_version": "modelbuilder.license-policy/v1", "policy_id": policy_id,
              "project_id": project_id, "revision": 1, "status": "reviewed", "source": source,
              "license_evidence_digest": evidence,
              "source_files": source_files, "reviewed_by": "test-owner", "reviewed_at": "2026-10-13T00:00:00Z",
              "permitted_uses": ["train", "evaluate", "generate", "export", "promote"],
              "reviewed_usage_context": USAGE_CONTEXT, "unresolved_obligations": []}
    if source_origin is not None:
        policy["source_origin"] = deepcopy(source_origin)
    policy["review_digest"] = digest(policy)
    envelope = {"schema_version": "modelbuilder.artifact-rights/v1",
                "sources": [source_reference(source, evidence, policy_id)], "unresolved": [], "parent_rights_digests": []}
    return policy, envelope


def local_source_review(source_root, source, project_id="test-project", *, policy_id="test-local-origin", source_alias="test-source"):
    """Review only this test's exact authored and committed source closure."""
    import hashlib
    from pathlib import Path
    import subprocess
    from modelbuilder.source_workspace import SourceWorkspaces
    root = Path(source_root).resolve()
    repository = SourceWorkspaces({source_alias: {"path": str(root), "trusted": True}}).inspect(source_alias)
    files = []
    for item in source["files"]:
        path = (root / item["path"]).resolve()
        assert path.is_relative_to(root) and not (root / item["path"]).is_symlink()
        content = path.read_bytes()
        committed = subprocess.check_output(["git", "-C", str(root), "show", f"{source['commit']}:{item['path']}"])
        assert hashlib.sha256(content).hexdigest() == item["sha256"]
        assert committed == content
        files.append({**item, "size_bytes": len(content)})
    origin = {"schema_version": "modelbuilder.source-origin/v1", "repository_id": repository["repository_id"],
              "source_alias": source_alias, "commit": source["commit"], "files": deepcopy(source["files"])}
    return local_review(project_id, commit=source["commit"], policy_id=policy_id, source_files=files,
                        repo_id="urn:modelbuilder:test-source:" + repository["repository_id"], repo_type="source", source_origin=origin)


def write_reviewed_policies(path, policies):
    """Register explicit test-owned reviews; never silently replace an earlier review."""
    from pathlib import Path
    from modelbuilder.store import SQLiteStore
    path = Path(path).resolve()
    store = SQLiteStore(path)
    try:
        existing = {item["policy_id"]: item for item in store.list("hub_license_policy")}
        for policy in policies.values():
            if policy["policy_id"] in existing:
                assert existing[policy["policy_id"]] == policy, "Fixture review changed without a new policy revision"
            else:
                store.put("hub_license_policy", policy["policy_id"], policy["project_id"], policy)
    finally:
        store.close()
    return str(path)


def authorize_test_recipe(directory, recipe, policies, *, project_id="test-project"):
    """Service-like durable authority for explicitly reviewed local test fixtures."""
    from pathlib import Path
    from types import SimpleNamespace
    from modelbuilder.backend.execution_rights import bind_authority, prepare_authorization
    path = write_reviewed_policies(Path(directory) / "test-review.sqlite3", policies)
    backend = SimpleNamespace(_execution_rights_authority=bind_authority(project_id, policies, path))
    authorization = prepare_authorization(backend, {"project_id": project_id}, recipe, durable=True)
    return {"project_id": project_id, "rights_authorization": authorization,
            "rights_extensions": authorization["receipt"]["declarations"]["extensions"]}


def review_local_dataset(service, version, root, actor, *, intended_use="train"):
    """Explicit local synthetic-data review and real public per-source policy binding."""
    import hashlib
    from pathlib import Path
    project_id = version["project_id"]
    files = []
    for source in version["manifest"]["sources"]:
        content = (Path(root) / source["path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == source["sha256"]
        files.append({"path": source["path"], "sha256": source["sha256"], "size_bytes": len(content)})
    policy_id = "test-dataset-" + digest({"project_id": project_id, "dataset_id": version["dataset_id"],
                                         "revision": version["revision"]})[:24]
    policy, _ = local_review(project_id, commit=digest(files), policy_id=policy_id,
        repo_id="urn:modelbuilder:test:dataset:" + version["dataset_id"], repo_type="dataset", source_files=files)
    policy["reviewed_by"] = actor.id
    policy["review_digest"] = digest({key: value for key, value in policy.items() if key != "review_digest"})
    write_reviewed_policies(service.store.path, {policy_id: policy})
    return service.invoke("hub.dataset.policy.bind", {"project_id": project_id, "dataset_id": version["dataset_id"],
        "dataset_revision": version["revision"], "source_policies": {source["id"]: policy_id for source in version["manifest"]["sources"]},
        "intended_use": intended_use, "usage_context": USAGE_CONTEXT, "idempotency_key": policy_id}, actor)
