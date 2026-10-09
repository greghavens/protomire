import copy
import hashlib
import json
from pathlib import Path

import pytest

from modelbuilder.recipe import (
    RECIPE_SCHEMA, RecipeValidationError, build_manifest, canonical_json, default_recipe,
    export_bundle, import_bundle, normalize_recipe, recipe_digest, resolve_dependencies,
    validate_manifest, validate_pinned_requirements, validate_recipe, validation_report,
)


def node(node_id, kind="code.python", inputs=None, outputs=None, parameters=None):
    return {"id": node_id, "kind": kind,
            "implementation_ref": {"module": "fixture.training", "entrypoint": "train", "commit": "a" * 40}
            if kind == "code.python" else f"builtin:{kind}",
            "inputs": inputs or [], "outputs": outputs or [], "parameters": parameters or {},
            "environment_ref": "default", "resource_request": {"cpu": 1},
            "retry": {"max_attempts": 1, "backoff_seconds": 0}, "timeout": 300,
            "cache_policy": "disabled", "provenance": {"author": "fixture"}}


@pytest.fixture
def recipe():
    result = default_recipe("project-one", "recipe-one", "custom Python roundtrip")
    result["source"] = {"repository": "https://example.org/research/model.git", "commit": "a" * 40, "files": []}
    result["licenses"] = [{"scope": "source", "license": "MIT", "redistribution_allowed": True}]
    result["nodes"] = [node("data", "dataset.synthetic", outputs=[{"name": "data", "type": "dataset"}]),
                       node("train", inputs=[{"name": "data", "type": "dataset", "required": True}],
                            outputs=[{"name": "checkpoint", "type": "checkpoint"}], parameters={"custom": {"layers": [16, 8]}})]
    result["edges"] = [{"source_node": "data", "source_port": "data", "target_node": "train", "target_port": "data"}]
    return result


def rejected(recipe, code):
    with pytest.raises(RecipeValidationError) as error:
        validate_recipe(recipe)
    assert error.value.code == code


def test_schema_file_matches_runtime():
    assert json.loads(Path("schemas/recipe.v0.schema.json").read_text()) == RECIPE_SCHEMA


def test_default_recipe_is_explicit_draft():
    recipe = default_recipe("p", "r")
    assert validate_recipe(recipe)["revision"] == 1
    report = resolve_dependencies(recipe)
    assert report["ready"] is False
    assert {item["reason"] for item in report["unresolved"]} == {"source_not_packaged", "license_review_required"}


def test_roundtrip_keeps_arbitrary_python_and_params(recipe):
    before = copy.deepcopy(recipe)
    normalized = normalize_recipe(recipe)
    assert recipe == before
    assert normalized["nodes"][1]["implementation_ref"] == before["nodes"][1]["implementation_ref"]
    assert normalized["nodes"][1]["parameters"] == {"custom": {"layers": [16, 8]}}
    assert json.loads(canonical_json(normalized)) == normalized
    assert validation_report(recipe)["valid"] is True


def test_digest_ignores_key_and_graph_order_and_derived_digest(recipe):
    alternate = json.loads(json.dumps(recipe, sort_keys=True))
    alternate["nodes"].reverse()
    alternate["digest"] = "old-derived-value"
    assert recipe_digest(recipe) == recipe_digest(alternate)
    alternate["nodes"][0]["parameters"]["new_hyperparameter"] = 3
    assert recipe_digest(recipe) != recipe_digest(alternate)


def test_schema_version_rejected(recipe):
    recipe["schema_version"] = "1"
    rejected(recipe, "unsupported_schema_version")
    assert validation_report(recipe)["valid"] is False


@pytest.mark.parametrize("key", ["source", "environment", "evaluation", "outputs", "external_dependencies", "resources"])
def test_required_manifest_fields(recipe, key):
    recipe.pop(key)
    rejected(recipe, "invalid_recipe")


def test_unknown_node_is_explicitly_unsupported(recipe):
    recipe["nodes"][0]["kind"] = "control.loop"
    rejected(recipe, "invalid_recipe")


def test_type_mismatch_rejected(recipe):
    recipe["nodes"][1]["inputs"][0]["type"] = "checkpoint"
    rejected(recipe, "port_type_mismatch")


def test_cycle_rejected(recipe):
    recipe["nodes"][0]["inputs"] = [{"name": "checkpoint", "type": "checkpoint"}]
    recipe["edges"].append({"source_node": "train", "source_port": "checkpoint", "target_node": "data", "target_port": "checkpoint"})
    rejected(recipe, "graph_cycle")


def test_unknown_port_rejected(recipe):
    recipe["edges"][0]["source_port"] = "missing"
    rejected(recipe, "unknown_port")


def test_required_input_is_not_silently_ignored(recipe):
    recipe["edges"] = []
    rejected(recipe, "unconnected_input")


def test_duplicate_input_and_nodes_rejected(recipe):
    recipe["nodes"].append(copy.deepcopy(recipe["nodes"][0]))
    rejected(recipe, "duplicate_node")
    recipe["nodes"].pop()
    recipe["edges"].append(copy.deepcopy(recipe["edges"][0]))
    rejected(recipe, "duplicate_edge")


def test_parameter_schema_is_executable_validation(recipe):
    recipe["nodes"][1]["parameter_schema"] = {"type": "object", "required": ["width"], "properties": {"width": {"type": "integer", "minimum": 2}}}
    rejected(recipe, "invalid_parameters")
    recipe["nodes"][1]["parameters"]["width"] = 3
    assert validate_recipe(recipe)


def test_kl_semantics_are_explicit(recipe):
    recipe["nodes"].append(node("initial", "model.initialize"))
    train = recipe["nodes"][1]
    train["parameters"]["kl"] = {"enabled": True, "coefficient": 0.2, "direction": "reference_to_model",
                                     "reference_model_ref": "initial", "frozen": True, "applies_to": "logits"}
    assert validate_recipe(recipe)
    train["parameters"]["kl"]["direction"] = "forward"
    rejected(recipe, "invalid_kl")
    train["parameters"]["kl"]["direction"] = "model_to_reference"
    train["parameters"]["kl"]["frozen"] = False
    rejected(recipe, "invalid_kl")
    train["parameters"]["kl"]["frozen"] = True
    train["parameters"]["kl"]["enabled"] = False
    rejected(recipe, "invalid_kl")
    train["parameters"]["kl"]["coefficient"] = 0
    assert validate_recipe(recipe)


@pytest.mark.parametrize("requirement", ["torch", "torch>=2", "torch==2.*", "-r evil.txt", "./evil.whl", "-e .", "pkg @ https://example.org/pkg.tar", "pkg==1; python_version>='3'"])
def test_only_exact_non_executable_dependency_pins(requirement):
    with pytest.raises(RecipeValidationError):
        validate_pinned_requirements([requirement])


def test_installed_dependency_versions_are_actually_checked(recipe):
    recipe["environment"]["requirements"] = ["uninstalled-modelbuilder-test-package==0.0.1"]
    report = resolve_dependencies(recipe, installed=True)
    assert any(item["reason"] == "package_missing" for item in report["unresolved"])
    assert report["network_accessed"] is False


def test_private_refs_remain_explicitly_unresolved(recipe):
    recipe["resources"] = [{"id": "private-data", "kind": "dataset", "url": "https://example.org/private/data.json",
                            "sha256": "c" * 64, "license": "NOASSERTION", "private": True}]
    report = import_bundle(build_manifest(recipe))
    assert report["execution_allowed"] is False
    assert report["ready"] is False
    assert any(item["reason"] == "private_reference" for item in report["unresolved_dependencies"])


def test_source_is_packaged_verified_and_imported_without_execution(recipe, tmp_path):
    content = b"raise RuntimeError('import must never execute this module')\n"
    source = tmp_path / "source"
    (source / "fixture").mkdir(parents=True)
    (source / "fixture/training.py").write_bytes(content)
    file_ref = {"path": "fixture/training.py", "sha256": hashlib.sha256(content).hexdigest()}
    recipe["source"]["files"] = [file_ref]
    recipe["nodes"][1]["implementation_ref"].update(file_ref)
    tar_path = tmp_path / "portable.tar"
    transport = export_bundle(recipe, tar_path, source)
    assert validate_manifest(transport)["recipe"] == normalize_recipe(recipe)
    first_bytes = tar_path.read_bytes()
    export_bundle(recipe, tar_path, source)
    assert tar_path.read_bytes() == first_bytes
    destination = tmp_path / "imported"
    report = import_bundle(tar_path, destination)
    assert report["ready"] is True
    assert report["trust_required"] is True
    assert report["execution_allowed"] is False
    assert (destination / "fixture/training.py").read_bytes() == content
    assert resolve_dependencies(recipe, destination)["ready"] is True
    with pytest.raises(RecipeValidationError, match="new directory"):
        import_bundle(tar_path, destination)


def test_source_hash_is_not_silently_rewritten(recipe, tmp_path):
    (tmp_path / "training.py").write_text("print('hello')")
    recipe["source"]["files"] = [{"path": "training.py", "sha256": "f" * 64}]
    with pytest.raises(RecipeValidationError) as error:
        export_bundle(recipe, source_root=tmp_path)
    assert error.value.code == "source_hash_mismatch"


def test_pin_source_files_proves_git_revision_and_rejects_dirty_source(tmp_path):
    import subprocess
    from modelbuilder.recipe import pin_source_files
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], stderr=subprocess.DEVNULL).decode().strip()
    git("init")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (tmp_path / "training.py").write_text("def train():\n    return 42\n")
    git("add", "training.py")
    git("commit", "-m", "Test fixture")
    commit = git("rev-parse", "HEAD")
    body = default_recipe("p", "r")
    pinned = pin_source_files(body, tmp_path, ["training.py"], commit)
    assert pinned["source"]["commit"] == commit
    assert pinned["source"]["files"][0]["sha256"] == hashlib.sha256((tmp_path / "training.py").read_bytes()).hexdigest()
    (tmp_path / "training.py").write_text("def train():\n    return 43\n")
    with pytest.raises(RecipeValidationError) as error:
        pin_source_files(body, tmp_path, ["training.py"], commit)
    assert error.value.code == "dirty_source"


def test_custom_python_tuple_output_order_is_preserved(recipe):
    recipe["nodes"][1]["outputs"] = [{"name": "z_checkpoint", "type": "checkpoint"}, {"name": "a_metrics", "type": "metrics"}]
    normalized = normalize_recipe(recipe)
    assert normalized["nodes"][1]["outputs"] == recipe["nodes"][1]["outputs"]
    reordered = copy.deepcopy(recipe)
    reordered["nodes"][1]["outputs"].reverse()
    assert recipe_digest(recipe) != recipe_digest(reordered)


@pytest.mark.parametrize("field,value", [("direction", []), ("reference_model_ref", {}), ("coefficient", True)])
def test_malformed_kl_fields_return_typed_errors(recipe, field, value):
    recipe["nodes"].append(node("initial", "model.initialize"))
    kl = {"enabled": True, "coefficient": 0.2, "direction": "reference_to_model", "reference_model_ref": "initial", "frozen": True, "applies_to": "logits"}
    kl[field] = value
    recipe["nodes"][1]["parameters"]["kl"] = kl
    rejected(recipe, "invalid_kl")


def test_dataset_is_not_a_kl_model_reference(recipe):
    recipe["resources"] = [{"id": "dataset", "kind": "dataset", "url": "https://example.org/data", "sha256": "c" * 64, "license": "MIT"}]
    recipe["nodes"][1]["parameters"]["kl"] = {"enabled": True, "coefficient": 0.2, "direction": "reference_to_model", "reference_model_ref": "dataset", "frozen": True, "applies_to": "logits"}
    rejected(recipe, "invalid_kl")


def test_unpaired_surrogates_return_typed_error(recipe):
    recipe["name"] = "invalid \ud800"
    rejected(recipe, "invalid_json")


def test_malformed_url_returns_typed_error(recipe):
    recipe["source"]["repository"] = "https://[not-valid-ipv6/repo"
    rejected(recipe, "unsafe_url")
