"""Canonical stage bounds travel with the recipe, never with a run override."""
import copy
import json
from pathlib import Path

import pytest

from modelbuilder.execution import MAX_PARALLEL_STAGES, max_parallel_stages, selection, selected_recipe
from modelbuilder.models import ServiceError
from modelbuilder.recipe import (
    RECIPE_SCHEMA, RecipeValidationError, build_manifest, default_recipe, import_bundle,
    normalize_recipe, recipe_digest,
)


def recipe():
    return default_recipe("project", "recipe", "Canonical stage settings")


def test_omitted_settings_keep_sequential_behavior_and_canonical_bytes():
    body = recipe()
    before = copy.deepcopy(body)
    assert max_parallel_stages(body) == 1
    assert "execution" not in normalize_recipe(body)
    assert body == before
    assert recipe_digest(body) == recipe_digest(normalize_recipe(body))
    assert max_parallel_stages({**body, "execution": {}}) == 1


@pytest.mark.parametrize("limit", [1, 2, 8])
def test_bounds_are_immutable_recipe_content_and_bundle_roundtrip(limit):
    body = recipe()
    original_digest = recipe_digest(body)
    body["execution"] = {"max_parallel_stages": limit}
    normalized = normalize_recipe(body)
    assert max_parallel_stages(normalized) == limit
    assert recipe_digest(body) != original_digest
    report = import_bundle(build_manifest(body))
    assert report["recipe"]["execution"] == body["execution"]
    assert report["execution_allowed"] is False
    assert report["recipe"]["recipe_id"] == body["recipe_id"]
    assert selected_recipe(body, selection(body))["execution"] == body["execution"]


@pytest.mark.parametrize("value", [0, -1, 9, 1.0, 1.5, True, False, "2", None, [], {}])
def test_bad_bounds_never_normalize_or_reach_workers(value):
    body = {**recipe(), "execution": {"max_parallel_stages": value}}
    with pytest.raises(RecipeValidationError) as failure:
        normalize_recipe(body)
    assert failure.value.code == "invalid_recipe"
    with pytest.raises(ValueError):
        max_parallel_stages(body)


@pytest.mark.parametrize("settings", [None, [], 1, "parallel", {"max_concurrency": 2}, {"max_parallel_stages": 2, "node_ids": []}])
def test_unknown_settings_and_second_execution_configuration_are_rejected(settings):
    body = {**recipe(), "execution": settings}
    with pytest.raises(RecipeValidationError):
        normalize_recipe(body)
    with pytest.raises(ValueError):
        max_parallel_stages(body)


def test_run_selection_cannot_override_canonical_stage_bound():
    body = recipe()
    body["execution"] = {"max_parallel_stages": 2}
    with pytest.raises(ServiceError) as failure:
        selection(body, {"node_ids": ["stage"], "input_bindings": [], "max_parallel_stages": 8})
    assert failure.value.code == "invalid_execution_selection"
    assert body["execution"]["max_parallel_stages"] == 2


def test_service_and_native_json_editor_share_stage_settings_schema():
    assert RECIPE_SCHEMA["properties"]["execution"]["properties"]["max_parallel_stages"]["maximum"] == MAX_PARALLEL_STAGES
    for relative in ("schemas/recipe.v0.schema.json", "packages/ide_feature/schemas/recipe.v0.schema.json"):
        assert json.loads(Path(relative).read_text()) == RECIPE_SCHEMA
