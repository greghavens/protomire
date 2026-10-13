"""Real CPU checkpoint evaluations remain readable during controlled slow I/O.

The service backend is a local harness exposing actual LiveEvaluationCoordinator
records. The held hardware/source responses are fixtures, not live providers or
full native-ZenML admission acceptance; that boundary is tested separately.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import sys
import threading
import time
from uuid import uuid4

import pytest

pytest.importorskip("torch")
from modelbuilder import parquet_reader, research_content  # noqa: E402
from modelbuilder.backend import ZenMLBackend, fixtures, placement  # noqa: E402
from modelbuilder.backend.live_evaluation import LiveEvaluationCoordinator, list_evaluations  # noqa: E402
from modelbuilder.models import Actor  # noqa: E402
from modelbuilder.service import OperationService  # noqa: E402

from tests.live_evaluation.fixture_rights import reviewed_fixture  # noqa: E402
from modelbuilder.backend.fixture_artifacts import input_rights  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("held_preparation", ["none", "parquet", "placement"])
def test_live_evaluation_reads_and_writes_continue_with_compute_and_content_held(tmp_path, monkeypatch, held_preparation):
    hold_parquet = held_preparation == "parquet"
    hold_placement = held_preparation == "placement"
    if hold_parquet:
        if os.environ.get("MODELBUILDER_REQUIRE_PARQUET") == "1":
            import pyarrow as arrow
            import pyarrow.parquet as parquet
        else:
            arrow = pytest.importorskip("pyarrow")
            parquet = pytest.importorskip("pyarrow.parquet")
    actor = Actor("live-io-owner", {"read", "modify", "execute"})
    job = tmp_path / "actual-cpu-evaluations"
    data = fixtures.dataset({"fixture": "custom", "count": 80})
    data, evaluator_spec = reviewed_fixture(tmp_path, data)
    initial = fixtures.initialize(data, {"fixture": "custom", "width": 4,
        "_artifact_rights": input_rights(evaluator_spec, data)}, tmp_path / "initial")
    coordinator = LiveEvaluationCoordinator(job, "train", {"kind": "fixture", "data": data, "parameters": {}, "spec": evaluator_spec},
                                          max_concurrency=1, timeout_seconds=30)
    failures = []
    def train():
        try:
            fixtures.train(data, initial, {"steps": 16, "step_delay": 0.5, "checkpoint_interval_steps": 2,
                                           "_checkpoint_ready": coordinator.checkpoint_ready,
                                           "_artifact_rights": input_rights(evaluator_spec, data, [initial])}, tmp_path / "training")
        except BaseException as exc:
            failures.append(exc)
    trainer = threading.Thread(target=train)
    class LiveBackend(ZenMLBackend):
        def invoke(self, operation, payload, actor=None):
            if operation == "run.evaluations.list":
                return list_evaluations(job, limit=payload.get("limit"))
            if operation == "run.status":
                return {"operation_id": payload["operation_id"], "backend_run_id": "local-live-harness",
                        "status": "running" if trainer.is_alive() else "succeeded"}
            return super().invoke(operation, payload, actor)
    backend = LiveBackend(tmp_path / "backend", python_executable=sys.executable)
    service = OperationService(tmp_path / "service.sqlite3", backend)
    writer = OperationService(service.store.path)
    def call(operation, payload):
        return service.invoke(operation, payload, actor)
    project = call("project.create", {"slug": "concurrent-live", "model_name": "One live model", "idempotency_key": "project"})
    pid, operation_id = project["project_id"], str(uuid4())
    with service.store.transaction():
        service.store.put("operation", operation_id, pid, {"operation_id": operation_id, "project_id": pid,
            "backend_run_id": "local-live-harness", "status": "running", "revision": 1,
            "recipe_id": project["recipe_id"], "recipe_revision": 1, "recipe_digest": "controlled-harness"})
    evidence = call("evidence.add", {"project_id": pid, "title": "Independent synthetic paper",
                                     "source_identifier": "PMC123", "idempotency_key": "evidence"})
    compute_entered, content_entered, release = threading.Event(), threading.Event(), threading.Event()
    parquet_entered, placement_entered = threading.Event(), threading.Event()
    discovery_calls, placement_calls = [], []
    expected_revision = 1
    if hold_placement:
        # The actual CPU harness remains tied to its original revision. A second
        # revision plans a different device while those checkpoints are read.
        recipe = call("recipe.get", {"project_id": pid, "recipe_id": project["recipe_id"]})
        recipe["environment"]["device"] = "mps"
        recipe["environment"]["lock_file"] = {"path": "requirements/profiles/backend-darwin-arm64.lock",
                                               "sha256": placement.MPS_LOCK_SHA256}
        call("recipe.update", {"project_id": pid, "recipe_id": project["recipe_id"], "expected_revision": 1,
                               "recipe": recipe, "idempotency_key": "device-revision"})
        expected_revision = 2
        call("policy.update", {"project_id": pid, "expected_revision": 1,
             "changes": {"allowed_targets": ["local-cpu", "local-mps"]}, "idempotency_key": "mps-policy"})
        def held_probe(_backend, device, *, allocate=False):
            assert not service.store._lock._is_owned()
            assert allocate is False
            if placement._PREPARED_PROBES.get() is None:
                # Discovery inspects both accelerator runtimes before reaching
                # its own held hardware fixture, separate from the MPS plan.
                assert device in {"mps", "cuda:0"}
                discovery_calls.append((device, allocate))
                return {"device": device, "available": False, "reason": "Controlled discovery fixture"}
            assert device == "mps"
            placement_calls.append((device, allocate))
            placement_entered.set()
            assert release.wait(25), "Held placement fixture was not released"
            return {"device": device, "available": False, "reason": "Controlled unavailable MPS fixture"}
        monkeypatch.setattr(placement, "_probe", held_probe)
    if hold_parquet:
        from modelbuilder.dataset import DatasetRegistry
        root = tmp_path / "external-data"
        root.mkdir()
        path = root / "bounded.parquet"
        parquet.write_table(arrow.table({"id": ["one"], "text": ["local synthetic data"]}), path)
        manifest = {"name": "Local fixture", "benchmark": "local", "license": "CC0-1.0",
            "provenance": {"source": "synthetic fixture"}, "id_column": "id",
            "row_schema": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}}},
            "sources": [{"id": "data", "root_id": "local", "path": path.name, "format": "parquet",
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}],
            "splits": [{"id": "train", "role": "train", "source_ids": ["data"]}]}
        service._dataset_registry = DatasetRegistry(service.store, project_roots={pid: {"local": str(root)}})
        original_decode = parquet_reader.read_parquet
        def held_decode(*args, **kwargs):
            parquet_entered.set()
            assert not service.store.connection.in_transaction
            assert release.wait(25), "Held dataset fixture was not released"
            return original_decode(*args, **kwargs)
        monkeypatch.setattr(parquet_reader, "read_parquet", held_decode)
    def hardware():
        compute_entered.set()
        assert release.wait(25), "Held compute fixture was not released"
        return {"scope": "controlled-held-probe"}
    def article(_identifier):
        content_entered.set()
        assert release.wait(25), "Held content fixture was not released"
        return b'''<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta>
<article-id pub-id-type="pmc">PMC123</article-id><title-group><article-title>Independent synthetic paper</article-title></title-group>
<permissions><license xlink:href="https://creativecommons.org/licenses/by/4.0/"><license-p>Synthetic fixture.</license-p></license></permissions>
<abstract><p>Fixture only.</p></abstract></article-meta></front><body><sec><title>Methods</title><p>Read measured live evaluations.</p></sec></body></article>'''
    monkeypatch.setattr(backend.hardware_discovery, "snapshot", hardware)
    monkeypatch.setattr(research_content, "fetch_xml", article)
    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            compute = pool.submit(call, "execution.targets", {"project_id": pid})
            content = pool.submit(call, "research.text.fetch", {"project_id": pid, "evidence_id": evidence["evidence_id"],
                "expected_evidence_revision": 1, "provider": "europe_pmc", "source_identifier": "PMC123", "idempotency_key": "fetch"})
            dataset = (pool.submit(call, "dataset.register", {"project_id": pid, "manifest": manifest,
                       "idempotency_key": "held-dataset"}) if hold_parquet else None)
            planned = (pool.submit(call, "run.plan", {"project_id": pid, "recipe_id": project["recipe_id"],
                       "target": "local-mps", "trusted": True}) if hold_placement else None)
            try:
                assert compute_entered.wait(3), compute.exception() if compute.done() else "Discovery did not reach fixture"
                assert content_entered.wait(3)
                assert not hold_parquet or parquet_entered.wait(3)
                assert not hold_placement or placement_entered.wait(3)
                assert not service.store.connection.in_transaction
                trainer.start()
                observed = []
                deadline = time.monotonic() + 20
                while trainer.is_alive() and time.monotonic() < deadline:
                    result = pool.submit(call, "run.evaluations.list", {"project_id": pid, "operation_id": operation_id, "limit": 200}).result(timeout=1)
                    observed = [row for row in result["evaluations"] if row["status"] == "completed"]
                    if len(observed) >= 2:
                        break
                    time.sleep(0.05)
                assert len(observed) >= 2, observed
                assert trainer.is_alive() and not compute.done() and not content.done()
                assert dataset is None or not dataset.done()
                assert planned is None or not planned.done()
                assert all(row["metrics"]["validation_loss"] > 0 for row in observed)
                assert {row["step"] for row in observed}.issuperset({2, 4})
                assert pool.submit(call, "run.status", {"project_id": pid, "operation_id": operation_id}).result(timeout=1)["status"] == "running"
                written = pool.submit(writer.invoke, "evidence.add", {"project_id": pid, "title": "Concurrent local observation",
                    "source_identifier": "urn:concurrency-fixture", "idempotency_key": "concurrent-write"}, actor).result(timeout=1)
                assert written["title"] == "Concurrent local observation"
                assert not compute.done() and not content.done()
            finally:
                release.set()
            assert compute.result(timeout=3)["hardware"]["scope"] == "controlled-held-probe"
            assert content.result(timeout=3)["status"] == "succeeded"
            if planned is not None:
                result = planned.result(timeout=5)
                assert result["executable"] is False
                assert any("Controlled unavailable MPS fixture" in row["message"] for row in result["blocked_reasons"])
                assert discovery_calls == [("mps", False), ("cuda:0", False)]
                assert placement_calls == [("mps", False)]  # No allocation or commit-time retry.
            if dataset is not None:
                assert dataset.result(timeout=5)["row_count"] == 1
        trainer.join(timeout=25)
        assert not trainer.is_alive() and not failures
        coordinator.close()
        assert list_evaluations(job)["summary"]["completed"] == 8
        current = call("recipe.get", {"project_id": pid, "recipe_id": project["recipe_id"]})
        assert current["revision"] == expected_revision
        assert len(service.store.list("recipe", pid)) == 1
        assert len(service.store.list("operation", pid)) == 1  # Only the explicitly installed local harness.
        (tmp_path / "verified-read-responsiveness.json").write_text(json.dumps({"observed_steps_while_training": [row["step"] for row in observed],
            "compute_and_content_pending": True, "cross_connection_write_completed": True, "recipe_revision": expected_revision,
            "held_preparation": held_preparation}))
    finally:
        release.set()
        if trainer.ident:
            trainer.join(timeout=25)
        coordinator.close(cancel=bool(failures) or trainer.is_alive())
        service.store.close()
        writer.store.close()
