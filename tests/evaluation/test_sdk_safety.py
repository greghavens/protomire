"""Both native comparison and promotion must use the same evidence predicate."""
from types import SimpleNamespace
import pytest

pytest.importorskip("zenml")
from modelbuilder.backend import sdk  # noqa: E402
from modelbuilder.backend.adapter import BackendError  # noqa: E402
from modelbuilder.evaluation import digest  # noqa: E402
from test_identity import evidence, run  # noqa: E402


def test_native_compare_returns_unknown_without_winner(monkeypatch, tmp_path):
    monkeypatch.setattr(sdk, "status", lambda job, **kwargs: {"status":"succeeded", "backend_run_id":job.name, "metrics":{"rmse":.01}})
    result = sdk.invoke("run.compare", {"job_dirs":["old-a","old-b"], "metric":"rmse", "direction":"minimize"}, tmp_path)
    assert not result["compatible"] and result["selected_run_id"] is None


def test_promotion_refuses_changed_sample_identity_and_preserves_version(monkeypatch, tmp_path):
    project = SimpleNamespace(id="project")
    version = SimpleNamespace(id="b", model=SimpleNamespace(id="model"))
    a, b = evidence(), evidence(.08, "b")
    b["split"]["sample_ids"] = ["new-x", "new-y"]
    b["digest"] = digest({k:v for k,v in b.items() if k != "digest"})
    evaluations = {"a":run(a)["metrics"], "b":run(b)["metrics"]}
    candidates = {rid:SimpleNamespace(id=rid, project=project,status=sdk.ExecutionStatus.COMPLETED,model_version=SimpleNamespace(id=rid)) for rid in evaluations}
    mutations = []
    client = SimpleNamespace(get_pipeline_run=lambda rid, **kwargs:candidates[rid], update_model_version=lambda *args,**kwargs:mutations.append(args))
    monkeypatch.setattr(sdk,"project_client",lambda _: (client,project))
    monkeypatch.setattr(sdk,"find_version",lambda *args:version)
    monkeypatch.setattr(sdk,"metrics_for",lambda candidate:evaluations[candidate.id])
    monkeypatch.setattr(sdk,"log_metadata",lambda *args,**kwargs:mutations.append(args))
    with pytest.raises(BackendError) as error:
        sdk.promotion_comparison(client, project, version,
            {"project_id":"project", "metric":"rmse", "direction":"minimize", "candidate_run_ids":["a","b"]}, tmp_path)
    assert error.value.code == "incompatible_evaluation"
    assert error.value.details["selected_run_id"] is None
    assert not mutations
