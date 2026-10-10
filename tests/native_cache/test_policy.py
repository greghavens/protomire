"""Fail-closed bounded cache intent and inert artifact checks."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from modelbuilder.backend.admission import BackendError, validate_node_settings
from modelbuilder.backend.native_cache import cache_identity, supports_cache, value_digest, _read_native_json, record_outputs, output_provenance


def node(**changes):
    return {"id": "prepare", "kind": "code.python", "inputs": [],
            "outputs": [{"name": "data", "type": "dict"}], "parameters": {},
            "cache_policy": "enabled", **changes}


def test_only_explicit_json_value_functions_are_cacheable():
    validate_node_settings(node())
    assert supports_cache(node(kind="component.run", component={"role": "dataset"}))
    assert supports_cache(node(kind="component.run", component={"role": "utility"}))
    for value in [node(kind="model.train"), node(kind="model.evaluate"), node(kind="dataset.version.import"),
                  node(kind="component.run", component={"role": "training"}),
                  node(kind="component.run", component={"role": "evaluation"}),
                  node(kind="component.run", component={"role": "export"}),
                  node(outputs=[{"name": "checkpoint", "type": "Path"}]),
                  node(inputs=[{"name": "checkpoint", "type": "Path"}]), node(outputs=[])]:
        with pytest.raises(BackendError, match="Caching supports"):
            validate_node_settings(value)
        value["cache_policy"] = "disabled"
        validate_node_settings(value)
    with pytest.raises(BackendError, match="Unknown native cache"):
        validate_node_settings(node(cache_policy="sometimes"))


def test_every_immutable_identity_affects_cache_but_job_paths_do_not():
    request = {"project_id": "project", "recipe": {"recipe_id": "recipe", "revision": 1,
        "project_id": "project", "source": {"commit": "a" * 40}, "nodes": [node()], "environment": {"python": "3.12"}}}
    selection = {"node_ids": ["prepare"], "input_bindings": []}
    environment = {"python": "3.12.14", "packages": {"zenml": "0.97.0"}, "modelbuilder_runtime_source": "runtime"}
    initial = cache_identity(request, node(), selection, environment)
    operational = {**request, "job_dir": "/other", "operation_id": "other", "run_name": "other", "source_root": "/relocated"}
    assert cache_identity(operational, node(), selection, environment) == initial
    for key, value in [("project_id", "other"), ("parent_run_id", "parent"), ("resume_mode", "warm_start"),
                       ("parent_checkpoint_ref", "checkpoint"), ("checkpoint_kind", "best")]:
        assert cache_identity({**request, key: value}, node(), selection, environment) != initial
    for key, value in [("revision", 2), ("recipe_id", "other"), ("source", {"commit": "b" * 40}),
                       ("resources", [{"sha256": "d" * 64}]), ("environment", {"python": "3.13"})]:
        changed = copy.deepcopy(request)
        changed["recipe"][key] = value
        assert cache_identity(changed, node(), selection, environment) != initial
    assert cache_identity(request, node(parameters={"n": 2}), selection, environment) != initial
    assert cache_identity(request, node(), {**selection, "input_bindings": [{"artifact_id": "changed", "content_sha256": "f" * 64}]}, environment) != initial
    assert cache_identity(request, node(), selection, {**environment, "modelbuilder_runtime_source": "changed"}) != initial
    assert cache_identity(request, node(), selection, {**environment, "packages": {"zenml": "0.98.0"}}) != initial


@pytest.mark.parametrize("value", [{1: "bad"}, {"bad": Path("a")}, [float("inf")], [float("nan")], {"bad": (1, 2)}, {"bad": object()}])
def test_cache_values_are_plain_finite_json(value):
    with pytest.raises((ValueError, TypeError)):
        value_digest(value)


def test_bounded_json_and_canonical_order(monkeypatch):
    assert value_digest({"a": 1, "b": [True, None]}) == value_digest({"b": [True, None], "a": 1})
    data = 1
    for _ in range(34):
        data = [data]
    with pytest.raises(ValueError, match="complexity"):
        value_digest(data)
    monkeypatch.setattr("modelbuilder.backend.native_cache.MAX_VALUE_BYTES", 8)
    with pytest.raises(ValueError, match="byte limit"):
        value_digest("longer than eight")


def test_secure_json_read_rejects_symlinks_escape_special_and_oversize(tmp_path, monkeypatch):
    root = tmp_path / "backend"
    artifact = root / "zenml-stores" / "artifacts" / "id"
    artifact.mkdir(parents=True)
    path = artifact / "data.json"
    path.write_text(json.dumps({"a": 1}))
    assert _read_native_json(str(artifact), root) == {"a": 1}
    with pytest.raises(ValueError, match="outside"):
        _read_native_json(str(tmp_path), root)
    monkeypatch.setattr("modelbuilder.backend.native_cache.MAX_VALUE_BYTES", 2)
    with pytest.raises(ValueError, match="bounded"):
        _read_native_json(str(artifact), root)
    monkeypatch.setattr("modelbuilder.backend.native_cache.MAX_VALUE_BYTES", 1024)
    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    path.symlink_to(outside)
    with pytest.raises(OSError):
        _read_native_json(str(artifact), root)
    path.unlink()
    path.mkdir()
    with pytest.raises(ValueError, match="bounded"):
        _read_native_json(str(artifact), root)


def test_disabled_output_recording_preserves_legacy_value():
    value = Path("legacy")
    assert record_outputs({}, value) is value


def test_non_cached_provenance_does_not_import_native_sdk():
    assert output_provenance(NS(id="step", status=NS(value="completed")), NS(id="run")) == {
        "reused": False, "original_step_run_id": None,
        "producer_backend_run_id": "run", "producer_step_run_id": "step"}


def test_cached_candidate_checks_are_read_only_and_fail_closed(tmp_path, monkeypatch):
    import sys
    from modelbuilder.backend import native_cache
    from modelbuilder.backend.native_cache import verify_cached_outputs
    request = {"project_id": "project", "root": str(tmp_path / "backend"), "source_root": str(tmp_path),
        "trusted_code_roots": [str(tmp_path)], "recipe": {"recipe_id": "recipe", "revision": 1,
        "project_id": "project", "source": {"commit": "a" * 40}, "nodes": [node()], "edges": []}}
    execution = {"node_ids": ["prepare"], "input_bindings": []}
    expected = cache_identity(request, node(), execution, {})
    store = Path(request["root"]) / "zenml-stores" / "artifact"
    store.mkdir(parents=True)
    (store / "data.json").write_text('{"a":1}')
    artifact = NS(id="artifact", uri=str(store), producer_step_run_id="step", project=NS(id="native-project"),
        data_type=NS(import_path="builtins.dict"),
        materializer=NS(import_path="zenml.materializers.built_in_materializer.BuiltInContainerMaterializer"))
    candidate = NS(id="step", name="prepare", pipeline_run_id="original-run", project=NS(id="native-project"),
        outputs={"data": [artifact]}, run_metadata={"modelbuilder_cache_identity": expected,
            "modelbuilder_cache_outputs": {"data": value_digest({"a": 1})}})
    page = NS(items=[candidate], total=1)
    run = NS(config=NS(extra={"application_project_id": "project"}))
    calls = []
    class Client:
        active_project = NS(id="native-project")
        def list_run_steps(self, **kwargs):
            calls.append(kwargs)
            return page
        def get_pipeline_run(self, *args, **kwargs):
            return run
    monkeypatch.setitem(sys.modules, "zenml.client", NS(Client=Client))
    monkeypatch.setenv("MODELBUILDER_JOB_DIR", str(tmp_path / "job"))
    monkeypatch.setattr(native_cache, "read_json", lambda _: request)
    monkeypatch.setattr("modelbuilder.backend.adapter.ZenMLBackend", lambda *a, **kw: NS())
    admitted = []
    monkeypatch.setattr("modelbuilder.backend.admission.validate_execution", lambda backend, body: admitted.append(body))
    monkeypatch.setattr("modelbuilder.backend.components.runtime.runtime_environment", lambda: {})
    assert verify_cached_outputs(expected, "prepare") == expected
    assert admitted == [request]
    assert calls[0]["project"] == "native-project" and calls[0]["status"] == "completed"
    assert calls[0]["size"] == native_cache.MAX_CACHE_CANDIDATES
    page.total = native_cache.MAX_CACHE_CANDIDATES + 1
    with pytest.raises(ValueError, match="candidate bound"):
        verify_cached_outputs(expected, "prepare")
    page.total = 1
    artifact.producer_step_run_id = "other"
    with pytest.raises(ValueError, match="ownership"):
        verify_cached_outputs(expected, "prepare")
    artifact.producer_step_run_id = "step"
    run.config.extra["application_project_id"] = "other"
    with pytest.raises(ValueError, match="application project"):
        verify_cached_outputs(expected, "prepare")
    run.config.extra["application_project_id"] = "project"
    candidate.project.id = "other"
    with pytest.raises(ValueError, match="project differs"):
        verify_cached_outputs(expected, "prepare")
    candidate.project.id = "native-project"
    candidate.outputs = {"data": [artifact, artifact]}
    with pytest.raises(ValueError, match="exactly one"):
        verify_cached_outputs(expected, "prepare")
    candidate.outputs = {"data": [artifact]}
    artifact.materializer.import_path = "project.evil.Loader"
    with pytest.raises(BackendError, match="materializer"):
        verify_cached_outputs(expected, "prepare")
    artifact.materializer.import_path = "zenml.materializers.built_in_materializer.BuiltInContainerMaterializer"
    (store / "data.json").write_text('{"a":2}')
    with pytest.raises(ValueError, match="recorded SHA256"):
        verify_cached_outputs(expected, "prepare")
    monkeypatch.setattr("modelbuilder.backend.components.runtime.runtime_environment", lambda: {"changed": True})
    with pytest.raises(ValueError, match="identity changed"):
        verify_cached_outputs(expected, "prepare")


def test_dynamic_cache_ignores_only_parameters_covered_by_identity(tmp_path, monkeypatch):
    import sys
    from modelbuilder.backend.native_cache import configure_cache
    monkeypatch.setitem(sys.modules, "zenml.config", NS(CachePolicy=lambda **kwargs: kwargs))
    function = NS(with_options=lambda **kwargs: kwargs)
    declaration = node(inputs=[{"name": "data", "type": "dict"}], parameters={"threshold": .5, "data": {}})
    result = configure_cache(function, declaration, tmp_path, "f" * 64)
    policy = result["cache_policy"]
    assert policy["ignored_inputs"] == ["spec", "threshold"]
    assert policy["include_artifact_ids"] is True and policy["include_artifact_values"] is False
    assert policy["include_step_code"] is True
    assert result["enable_cache"] is True
    # Parameters and condition decisions are in the full immutable identity.
    request = {"project_id": "project", "recipe": {"nodes": [declaration]}}
    initial = cache_identity(request, declaration, {}, {})
    changed = copy.deepcopy(declaration)
    changed["parameters"]["threshold"] = .6
    assert cache_identity(request, changed, {}, {}) != initial
    changed = copy.deepcopy(declaration)
    changed["condition"] = {"source_node": "gate", "source_port": "bool", "equals": True}
    assert cache_identity(request, changed, {}, {}) != initial


def test_execution_and_condition_source_belong_to_runtime_closure(tmp_path):
    from modelbuilder.backend.components.runtime import runtime_source_identity
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/worker.py").write_text("worker")
    (tmp_path / "conditions.py").write_text("gate")
    (tmp_path / "execution.py").write_text("selection")
    initial = runtime_source_identity(tmp_path)
    (tmp_path / "conditions.py").write_text("changed gate")
    changed = runtime_source_identity(tmp_path)
    assert changed != initial
    (tmp_path / "execution.py").write_text("changed selection")
    assert runtime_source_identity(tmp_path) != changed
