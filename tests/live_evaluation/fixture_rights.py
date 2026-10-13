"""Explicit authored source review for direct CPU fixture/coordinator tests."""
import hashlib
from pathlib import Path
import subprocess

from modelbuilder.backend.fixture_artifacts import declared_rights
from tests.components.rights_test_support import authorize_test_recipe
from tests.integration.native_fixture_support import pinned_builtin_source, synthetic_review


def reviewed_fixture(directory, data):
    source = pinned_builtin_source(directory)
    path = source / "src/modelbuilder/backend/fixtures.py"
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    node = {"id": "evaluate", "kind": "model.evaluate", "inputs": [{"name": "data", "type": "dict"},
            {"name": "checkpoint", "type": "Path"}], "parameters": {}, "outputs": [], "cache_policy": "disabled"}
    recipe = {"source": {"commit": commit, "files": [{"path": str(path.relative_to(source)),
               "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}, "nodes": [node], "resources": []}
    policy = synthetic_review(recipe, "direct-cpu-fixture", source)
    spec = {"node_id": node["id"], "component_node": node, "source": recipe["source"], "dependencies": [],
            **authorize_test_recipe(Path(directory), recipe, {policy["policy_id"]: policy}, project_id=policy["project_id"])}
    return {**data, "rights": declared_rights(spec)}, spec
