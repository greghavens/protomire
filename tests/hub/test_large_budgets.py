# ruff: noqa: F811
"""Large metadata inventories only: no large allocation, provider I/O or weights."""
from io import BytesIO
from types import SimpleNamespace
import subprocess
import sys

import pytest

from modelbuilder.hub import DEFAULT_MAX_BYTES, MAX_BYTES, HubTransfers
from modelbuilder.hub_contract import BUDGET
from modelbuilder.hub_sdk_worker import BoundedUploadFile, MAX_METADATA, WorkerError, response_allowance
from modelbuilder.hub_storage import OVERHEAD, reserve_space
from modelbuilder.models import ServiceError
from test_transfers import context, invoke, plan, start, finish, preview  # noqa: F401

GiB = 1024**3


def large_inventory(context, size=75 * GiB):
    provider = context[1]
    original = provider.call

    def call(action, payload, **kwargs):
        result = original(action, payload, **kwargs)
        if action in {"inspect", "license.inspect"}:
            for item in result["files"]:
                if item["path"] == "weights.bin":
                    item["size_bytes"] = size
        return result
    provider.call = call
    return provider


def test_default_stays_conservative_and_explicit_operator_cap_allows_75_gib_plan(context):
    provider = large_inventory(context)
    assert context[0].hub.byte_budget(context[2]) == DEFAULT_MAX_BYTES == 16 * GiB
    with pytest.raises(ServiceError, match="budget"):
        plan(context, max_bytes=100 * GiB)
    context[0].hub.max_bytes = 128 * GiB
    result = plan(context, max_bytes=100 * GiB)
    assert result["payload"]["total_bytes"] > 75 * GiB
    assert result["payload"]["max_bytes"] == 100 * GiB
    assert invoke(context, "hub.providers")["max_bytes"] == 128 * GiB
    assert BUDGET["maximum"] == MAX_BYTES < 2**53
    assert not any(action == "download" for action, _ in provider.calls)
    with pytest.raises(ServiceError, match="digest"):
        invoke(context, "hub.download.start", {"plan_id": result["plan_id"], "plan_digest": "f" * 64, "accept_license": True})


@pytest.mark.parametrize("value", [0, -1, True, 2.5, float("inf"), MAX_BYTES + 1])
def test_operator_and_internal_budgets_reject_non_exact_or_unbounded_values(context, value):
    with pytest.raises(ValueError, match="integer"):
        HubTransfers(context[0], config={"root": str(context[0].hub.root), "max_bytes": value})
    with pytest.raises(ServiceError):
        context[0].hub._bound([{"size_bytes": 1}], value, context[2])


def test_project_budget_hook_and_current_start_limit_are_independent(context, monkeypatch):
    hub = context[0].hub
    provider = large_inventory(context)
    caps = {context[2]: 100 * GiB, "other": DEFAULT_MAX_BYTES}
    monkeypatch.setattr(hub, "byte_budget", lambda project: caps[project])
    planned = plan(context, max_bytes=100 * GiB)
    assert hub._bound([{"size_bytes": 75 * GiB}], 100 * GiB, context[2]) == 75 * GiB
    with pytest.raises(ServiceError):
        hub._bound([{"size_bytes": 75 * GiB}], 100 * GiB, "other")
    caps[context[2]] = DEFAULT_MAX_BYTES
    with pytest.raises(ServiceError, match="budget"):
        start(context, planned)
    assert not any(action == "download" for action, _ in provider.calls)


def test_insufficient_disk_stops_large_transfer_before_any_provider_bytes(context, monkeypatch):
    provider = large_inventory(context)
    context[0].hub.max_bytes = 100 * GiB
    planned = plan(context, max_bytes=100 * GiB)
    monkeypatch.setattr("modelbuilder.hub_storage.shutil.disk_usage", lambda root: SimpleNamespace(free=120 * GiB))
    result = finish(context, start(context, planned))
    assert result["state"] == "failed" and result["error"]["code"] == "hub_disk_budget"
    assert not any(action == "download" for action, _ in provider.calls)
    assert not list((context[0].hub.root / "space-reservations").glob("*.reserve"))
    assert not invoke(context, "hub.asset.list")["assets"]


def test_huge_or_non_integer_metadata_never_reaches_native_json_totals(context):
    provider = large_inventory(context, MAX_BYTES + 1)
    with pytest.raises(ServiceError):
        invoke(context, "hub.inspect", {"provider": "fake", "repo_id": "owner/model", "repo_type": "model"})
    assert not any(action == "download" for action, _ in provider.calls)


def test_space_reservations_bound_concurrency_release_and_reclaim_stale(tmp_path, monkeypatch):
    monkeypatch.setattr("modelbuilder.hub_storage.shutil.disk_usage", lambda root: SimpleNamespace(free=200 * GiB))
    with reserve_space(tmp_path, 150 * GiB):
        with pytest.raises(ServiceError, match="reserved"):
            with reserve_space(tmp_path, 60 * GiB):
                pytest.fail("second large copy must not overcommit disk")
    with pytest.raises(RuntimeError):
        with reserve_space(tmp_path, 190 * GiB):
            raise RuntimeError("cancelled")
    assert not list((tmp_path / "space-reservations").glob("*.reserve"))
    stale = tmp_path / "space-reservations" / "dead.reserve"
    stale.write_text('{"bytes": 123456}')
    with reserve_space(tmp_path, 190 * GiB):
        assert not stale.exists()


def test_reservation_is_fenced_across_processes(tmp_path, monkeypatch):
    monkeypatch.setattr("modelbuilder.hub_storage.shutil.disk_usage", lambda root: SimpleNamespace(free=200 * GiB))
    with reserve_space(tmp_path, 150 * GiB):
        code = """from types import SimpleNamespace
from pathlib import Path
from modelbuilder import hub_storage
from modelbuilder.models import ServiceError
hub_storage.shutil.disk_usage=lambda root: SimpleNamespace(free=200*1024**3)
try:
    with hub_storage.reserve_space(Path(__import__('sys').argv[1]),60*1024**3):
        raise AssertionError('disk reservation ignored')
except ServiceError as error:
    assert error.code=='hub_disk_budget'
"""
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_publication_disk_check_precedes_snapshot_and_cleans_failed_preview(context, monkeypatch):
    # Build the tiny reviewed local asset before simulating exhausted destination disk.
    preview(context)
    asset_id = invoke(context, "hub.asset.list")["assets"][0]["asset_id"]
    monkeypatch.setattr("modelbuilder.hub_storage.shutil.disk_usage", lambda root: SimpleNamespace(free=OVERHEAD - 1))
    before = set((context[0].hub.root / "publications").iterdir())
    with pytest.raises(ServiceError, match="space"):
        preview(context, source_asset_ids=[asset_id])
    assert set((context[0].hub.root / "publications").iterdir()) == before


def test_metadata_and_error_responses_never_inherit_large_artifact_allowance():
    file = SimpleNamespace(method="GET", url="https://huggingface.co/owner/repo/resolve/revision/data.bin")
    api = SimpleNamespace(method="GET", url="https://huggingface.co/api/models/owner/repo")
    assert response_allowance(file, True, 200, 75 * GiB) == 75 * GiB
    for request, streamed, status in [(file, False, 200), (file, True, 500), (api, True, 200)]:
        assert response_allowance(request, streamed, status, 75 * GiB) == MAX_METADATA


def test_large_regular_upload_cannot_buffer_entire_file_into_base64_json():
    class ReadSpy(BytesIO):
        calls = 0
        def read(self, size=-1):
            self.calls += 1
            return super().read(size)
    handle = ReadSpy(b"tiny local bytes")
    wrapped = BoundedUploadFile(handle, 75 * GiB)
    with pytest.raises(WorkerError, match="") as error:
        wrapped.read()
    assert error.value.code == "hub_upload_memory_budget" and handle.calls == 0
    with pytest.raises(WorkerError):
        wrapped.read(75 * GiB)
    assert handle.calls == 0
    assert wrapped.read(4) == b"tiny"


def test_snapshot_growth_after_disk_reservation_is_rejected_before_copy(context, monkeypatch):
    from contextlib import contextmanager
    from modelbuilder import hub_storage
    preview(context)
    asset_id = invoke(context, "hub.asset.list")["assets"][0]["asset_id"]
    before = set((context[0].hub.root / "publications").iterdir())
    original = hub_storage.reserve_space

    @contextmanager
    def grow(root, required):
        with original(root, required):
            (context[3] / "weights.bin").write_bytes(b"changed after admission" * 100)
            yield
    monkeypatch.setattr(hub_storage, "reserve_space", grow)
    with pytest.raises((ServiceError, ValueError)):
        preview(context, source_asset_ids=[asset_id])
    assert set((context[0].hub.root / "publications").iterdir()) == before
    assert not list((context[0].hub.root / "space-reservations").glob("*.reserve"))
