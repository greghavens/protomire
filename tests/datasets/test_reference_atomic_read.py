"""Regression: parse exactly the bounded reference bytes that were hashed."""
import hashlib
import io
import json
from pathlib import Path

import pytest
pytest.importorskip("zenml")
pytest.importorskip("torch")

from modelbuilder import dataset  # noqa: E402
from modelbuilder.backend import process_execution, worker  # noqa: E402
from tests.components.rights_test_support import USAGE_CONTEXT, authorize_test_recipe, local_review  # noqa: E402

LIMIT = 16 * 1024 * 1024


def setup_reference(tmp_path, monkeypatch, content):
    reference = tmp_path / "reference.json"
    reference.write_bytes(content)
    parameters = {"reference_id": "reference", "split_id": "train", "purpose": "training"}
    node = {"id": "import", "kind": "dataset.version.import", "parameters": parameters}
    digest = hashlib.sha256(content).hexdigest()
    policy, rights = local_review("project", repo_type="dataset", source_files=[
        {"path": reference.name, "sha256": digest, "size_bytes": len(content)}])
    recipe = {"nodes": [node], "resources": [{"id": "reference", "kind": "dataset", "sha256": digest,
                                              "metadata": {"rights": rights}}],
              "extensions": {"usage_context": USAGE_CONTEXT}}
    spec = {"node_id": node["id"], "component_node": node, "parameters": parameters,
            "dependency_paths": {"reference": str(reference)},
            "dependencies": recipe["resources"],
            "recipe_provenance": {"application_project_id": "project"},
            **authorize_test_recipe(tmp_path, recipe, {policy["policy_id"]: policy}, project_id="project")}
    # This direct entrypoint has no native metadata context; retain the real
    # rights check and verify its receipt at the logging boundary.
    def metadata(value):
        assert value["rights_admission"]["policy_revisions"][policy["policy_id"]]["revision"] == 1
    monkeypatch.setattr(process_execution, "metadata", metadata)
    monkeypatch.setattr(worker, "context", lambda value: (value["parameters"], tmp_path))
    return reference, spec


def test_reference_parses_same_hashed_bytes_without_second_read(tmp_path, monkeypatch):
    content = b'{"version": "original"}'
    reference, spec = setup_reference(tmp_path, monkeypatch, content)
    original_open = Path.open
    reads, opens, observed = [], [], []

    class MutatingStream(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            result = super().read(size)
            # Replace the on-disk reference between hashing and parsing. The
            # loader must still consume the original, hash-verified bytes.
            with original_open(reference, "wb") as target:
                target.write(b'{"version": "replaced"}')
            return result

    def guarded_open(path, *args, **kwargs):
        if path == reference:
            opens.append(args)
            assert len(opens) == 1, "Reference must not be opened a second time"
            assert args == ("rb",)
            return MutatingStream(content)
        return original_open(path, *args, **kwargs)

    def load(value, paths, split, **kwargs):
        observed.append(value)
        return {"rows": [], "checked": True}

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(dataset, "load_portable_split", load)
    result = worker.import_dataset_version_step.entrypoint(spec)
    assert result == {"rows": [], "checked": True, "rights": result["rights"]}
    assert result["rights"]["sources"] == spec["dependencies"][0]["metadata"]["rights"]["sources"]
    assert result["rights"]["unresolved"] == []
    assert observed == [json.loads(content)]
    assert reads == [LIMIT + 1] and len(opens) == 1


def test_reference_oversize_rejected_before_parse_or_portable_load(tmp_path, monkeypatch):
    content = b" " * (LIMIT + 1)
    reference, spec = setup_reference(tmp_path, monkeypatch, content)
    reads = []
    original_open = Path.open

    class BoundedStream(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            assert size == LIMIT + 1
            return super().read(size)

    def guarded_open(path, *args, **kwargs):
        return BoundedStream(content) if path == reference else original_open(path, *args, **kwargs)

    def unexpected_load(*args, **kwargs):
        raise AssertionError("Oversized reference must not reach the dataset loader")

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(dataset, "load_portable_split", unexpected_load)
    with pytest.raises(ValueError, match="oversized"):
        worker.import_dataset_version_step.entrypoint(spec)
    assert reads == [LIMIT + 1]
