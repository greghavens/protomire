"""Explicit source-pinned local synthetic fixture reviews, never pretrained approval."""
from copy import deepcopy
from pathlib import Path
import subprocess

from tests.components.rights_test_support import USAGE_CONTEXT, local_source_review, write_reviewed_policies


def pinned_builtin_source(directory):
    import modelbuilder.backend
    source = Path(directory) / "fixture-source"
    path = source / "src/modelbuilder/backend/fixtures.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(Path(modelbuilder.backend.__file__).with_name("fixtures.py").read_bytes())
    lock = Path(__file__).resolve().parents[2] / "requirements/backend.lock"
    target = source / "requirements/backend.lock"
    target.parent.mkdir(parents=True)
    target.write_bytes(lock.read_bytes())
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture acceptance", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "Exact builtin test source"], check=True)
    return source


def synthetic_review(recipe, project_id, source):
    if any(node.get("parameters", {}).get("fixture") == "pretrained" for node in recipe["nodes"]):
        raise ValueError("Cached pretrained fixture has unresolved upstream license review; local synthetic review cannot authorize it")
    policy, rights = local_source_review(source, recipe["source"], project_id,
                                        policy_id="test-local-origin-" + project_id)
    recipe.setdefault("extensions", {}).update(usage_context=deepcopy(USAGE_CONTEXT), artifact_rights=rights)
    return policy


def authorized_backend(backend, request):
    # Public pretrained dependencies must reach the real missing-review gate;
    # never apply this synthetic-origin authority to cached third-party weights.
    if any(node.get("parameters", {}).get("fixture") == "pretrained" for node in request["recipe"]["nodes"]):
        return backend
    # Pure utilities with no asset production remain outside this local-origin review.
    if not any(node["kind"].startswith("model.") for node in request["recipe"]["nodes"]):
        return backend
    policy = synthetic_review(request["recipe"], request["project_id"], backend.repository)
    policies = {policy["policy_id"]: policy}
    store = write_reviewed_policies(backend.root / "test-reviews.sqlite3", policies)
    return backend.with_execution_rights(project_id=request["project_id"], policies=policies, policy_store_path=store)


def register_project_review(service, policy, project_id):
    """Bind this explicit fixture review to its newly allocated service project."""
    from modelbuilder.hub_rights import digest
    policy = {**deepcopy(policy), "project_id": project_id}
    policy.pop("review_digest", None)
    policy["review_digest"] = digest(policy)
    service.store.put("hub_license_policy", policy["policy_id"], project_id, policy)
    return policy
