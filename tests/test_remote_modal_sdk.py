"""OFFLINE pinned-SDK construction tests, not live remote/provider acceptance.

The real ZenML Modal orchestrator builds the entrypoint and sandbox request.
Provider/native-store call boundaries are intercepted and all sockets forbidden.
"""
from __future__ import annotations

import os
import socket
from types import SimpleNamespace
from uuid import UUID

import pytest

if os.environ.get("MODELBUILDER_REQUIRE_MODAL_SDK") == "1":
    import modal
    import zenml
else:
    modal = pytest.importorskip("modal", reason="Optional Modal SDK construction profile is not installed")
    zenml = pytest.importorskip("zenml", reason="Separate native ZenML runtime is required")

from zenml.config import ResourceSettings
from zenml.config.step_configurations import Step, StepConfiguration
from zenml.enums import StackComponentType
from zenml.integrations.modal.flavors import ModalOrchestratorConfig
from zenml.integrations.modal.orchestrators.modal_orchestrator import ModalOrchestrator

from modelbuilder.models import ServiceError
from modelbuilder.remote.modal_runtime import ModalRuntime, NativeContext
from test_remote_modal import BINDING, NOW, RUN, approval, config


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("SDK request-construction tests must not open network sockets")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture
def native_context(monkeypatch):
    native = ModalOrchestrator(name="modal", id=UUID(BINDING.zenml_orchestrator_id),
        config=ModalOrchestratorConfig(modal_environment="staging", timeout=120, synchronous=False),
        flavor="modal", type=StackComponentType.ORCHESTRATOR, user=None, created=NOW, updated=NOW)
    step_config = StepConfiguration.model_construct(settings={}, resource_settings=ResourceSettings(cpu_count=1))
    step = Step.model_construct(config=step_config)
    snapshot = SimpleNamespace(id=UUID(int=5), schedule=None, is_dynamic=False,
        project=SimpleNamespace(id=UUID(int=4)), step_configurations={"train": step},
        pipeline_configuration=SimpleNamespace(settings={}, resource_settings=ResourceSettings(cpu_count=1)),
        model_dump=lambda **kw: {"id": str(UUID(int=5)), "source": "immutable-fixture"})
    stack = SimpleNamespace(id=UUID(BINDING.zenml_stack_id), orchestrator=native,
        artifact_store=SimpleNamespace(id=UUID(int=6), config=SimpleNamespace(is_local=False)),
        container_registry=SimpleNamespace(config=SimpleNamespace(is_local=False), credentials=None))
    run = SimpleNamespace(id=RUN, snapshot=snapshot, stack=stack, project=snapshot.project)
    image = "registry.example/test-image@sha256:" + "a" * 64
    monkeypatch.setattr(native, "get_image", lambda **kw: image)
    return NativeContext(snapshot, run, stack, {"ZENML_SERVER_URL": "https://native.example.invalid"})


def prepare(runtime, context):
    return runtime.prepare(context, project_id="p", submit_key="1" * 64, recipe_digest="2" * 64,
        source_digest="3" * 64, environment_lock_digest="4" * 64, max_runtime_seconds=120, created_at=NOW)


def test_pinned_sdk_and_native_orchestrator_build_actual_request(native_context, monkeypatch):
    explicit_client = object()  # Never a credential; RPC boundaries are intercepted.
    runtime = ModalRuntime(config(allow_native_submission=True, allow_provider_reads=True), client=explicit_client)
    assert all(item["compatible"] for item in runtime.dependencies().values())
    plan = prepare(runtime, native_context)
    native = native_context.stack.orchestrator
    captured = {}
    monkeypatch.setattr(native, "get_modal_client", lambda: explicit_client)
    monkeypatch.setattr(native, "_inject_modal_credentials", lambda env: None)
    monkeypatch.setattr(runtime, "_workspace", lambda: SimpleNamespace(name="approved-workspace"))
    monkeypatch.setattr("zenml.integrations.modal.orchestrators.modal_orchestrator.Client", lambda:
        SimpleNamespace(zen_store=SimpleNamespace(update_run=lambda **kw: captured.setdefault("update_run", kw))))
    def lookup(name, **kwargs):
        captured["lookup"] = {"name": name, **kwargs}
        return SimpleNamespace(app_id="ap-native")
    def create(*command, **kwargs):
        captured["create"] = {"command": command, **kwargs}
        return SimpleNamespace(object_id="sb-native-controller")
    monkeypatch.setattr(modal.App, "lookup", lookup)
    monkeypatch.setattr(modal.Sandbox, "create", create)
    monkeypatch.setattr(zenml, "log_metadata", lambda values, **kw: captured.setdefault("metadata", (values, kw)))
    result = runtime._native_submission_bridge(plan, native_context, approval(plan))
    assert result.provider_job_id == "sb-native-controller"
    assert result.backend_run_id == str(RUN)
    assert captured["lookup"]["name"] == f"zenml-{RUN}"
    assert captured["lookup"]["environment_name"] == "staging"
    request = captured["create"]
    assert request["timeout"] == 120 and request["cpu"] == 1.0
    assert request["client"] is explicit_client
    assert isinstance(request["image"], modal.Image)
    assert request["env"]["ZENML_MODAL_RUN_ID"] == str(RUN)
    assert request["env"]["ZENML_MODAL_APP_NAME"] == f"zenml-{RUN}"
    assert str(plan.snapshot_id) in request["command"]
    assert str(RUN) in request["command"]
    assert "zenml.integrations.modal.orchestrators.modal_orchestrator_entrypoint.ModalOrchestratorEntrypointConfiguration" in request["command"]
    # This native limitation is recorded honestly rather than patched around.
    assert "name" not in request and "idempotency_key" not in request
    assert captured["metadata"][1]["run_id_name_or_prefix"] == RUN


def test_native_plan_rejects_mutable_images_settings_and_changed_snapshot(native_context, monkeypatch):
    runtime = ModalRuntime(config(allow_native_submission=True))
    p = prepare(runtime, native_context)
    native_context.environment["ZENML_SERVER_URL"] = "https://changed.example.invalid"
    with pytest.raises(ServiceError) as caught:
        runtime._native_submission_bridge(p, native_context, approval(p))
    assert caught.value.code == "immutable_plan_changed"
    monkeypatch.setattr(native_context.stack.orchestrator, "get_image", lambda **kw: "image:latest")
    with pytest.raises(ValueError, match="OCI SHA256"):
        prepare(runtime, native_context)


def test_native_plan_rejects_schedules_wrong_stack_and_sandbox_deadline(native_context):
    runtime = ModalRuntime(config())
    native_context.snapshot.schedule = object()
    with pytest.raises(ServiceError) as caught:
        prepare(runtime, native_context)
    assert caught.value.code == "unsupported_remote_plan"
    native_context.snapshot.schedule = None
    native_context.stack.orchestrator._config = ModalOrchestratorConfig(modal_environment="staging", timeout=121, synchronous=False)
    with pytest.raises(ServiceError) as caught:
        prepare(runtime, native_context)
    assert caught.value.code == "unsupported_remote_plan"
