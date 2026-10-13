"""Replacing a dataset importer must not collide with its orphaned old rights."""
from copy import deepcopy

from modelbuilder.dataset_binding import _retire_replaced_dependencies


def recipe():
    return {"nodes": [{"id": "replace", "kind": "dataset.version.import", "parameters": {"reference_id": "old-ref"}}],
            "resources": [{"id": "user", "metadata": {}}],
            "external_dependencies": [
                {"id": "old-ref", "metadata": {"dataset_reference": {"dependencies": [{"id": "same-source-sha"}]}}},
                {"id": "same-source-sha", "metadata": {"dataset_source": True, "rights": {"policy": "old"}}}]}


def test_replacement_retires_orphaned_reference_and_same_sha_source_before_insertion():
    value = recipe()
    _retire_replaced_dependencies(value, "replace")
    assert value["external_dependencies"] == []
    assert value["resources"] == [{"id": "user", "metadata": {}}]


def test_another_importer_keeps_shared_source_so_new_policy_still_conflicts():
    value = recipe()
    value["nodes"].append({"id": "shared", "kind": "dataset.version.import", "parameters": {"reference_id": "old-ref"}})
    expected = deepcopy(value)
    _retire_replaced_dependencies(value, "replace")
    assert value == expected


def test_generic_node_reference_keeps_source_and_user_dependencies_are_never_retired():
    value = recipe()
    value["nodes"].append({"id": "generic", "parameters": {"nested": ["same-source-sha"]}})
    _retire_replaced_dependencies(value, "replace")
    assert [item["id"] for item in value["external_dependencies"]] == ["same-source-sha"]
    assert [item["id"] for item in value["resources"]] == ["user"]
