"""Independent tiny CPU workloads: real predictions, no fixture data or weights."""
import pytest

torch = pytest.importorskip("torch")
from modelbuilder.evaluation import digest, make_evaluation_record, compare_records  # noqa: E402


def test_independent_forecast_experiments_and_changed_dev_guard(tmp_path):
    torch.set_num_threads(1)
    torch.manual_seed(31)
    series = torch.randn(48, 8, 3)
    targets = series[:, -1, 0] * .7 - series[:, -2, 1] * .3
    content = digest({"series":series.tolist(), "targets":targets.tolist()})
    runs = []
    for width in [3, 6]:
        model = torch.nn.Sequential(torch.nn.Flatten(), torch.nn.Linear(24, width), torch.nn.Tanh(), torch.nn.Linear(width, 1))
        optimizer = torch.optim.Adam(model.parameters(), lr=.02)
        before = model[1].weight.detach().clone()
        for _ in range(20):
            loss = torch.nn.functional.mse_loss(model(series[:32]).squeeze(-1), targets[:32])
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        assert not torch.equal(before, model[1].weight)
        artifact = tmp_path / f"forecast-{width}.pt"
        torch.save(model.state_dict(), artifact)
        with torch.no_grad():
            predictions = model(series[32:]).squeeze(-1)
            rmse = float(torch.nn.functional.mse_loss(predictions, targets[32:]).sqrt())
        e = make_evaluation_record(
            dataset={"id":"independent-sequences", "revision":content, "content_digest":content, "benchmark":"forecast-synthetic-v1"},
            split={"id":"dev-32-48", "role":"validation", "sample_ids":[str(i) for i in range(32,48)]},
            protocol={"implementation":"independent.forecast.evaluate", "version":"1", "configuration":{"horizon":1}, "preprocessing":{}, "seed":31},
            metric={"name":"rmse", "implementation":"torch.nn.functional.mse_loss.sqrt", "version":torch.__version__, "parameters":{}, "aggregation":"root_mean_square", "direction":"minimize"},
            treatment={"model":{"width":width}, "checkpoint":digest({k:v.tolist() for k,v in model.state_dict().items()}), "calibration":None, "reference":None},
            value=rmse, predictions=[{"item_id":str(i+32), "prediction":float(v), "target":float(targets[i+32])} for i,v in enumerate(predictions)])
        runs.append({"backend_run_id":str(width), "model_version_ref":str(width), "status":"succeeded", "metrics":{"rmse":rmse,"evaluation_record":e}})
    result = compare_records(runs, "rmse", "minimize")
    assert result["compatible"] and result["selected_run_id"] in {"3","6"}
    original = runs[1]["metrics"]["evaluation_record"]
    changed = {k:v for k,v in original.items() if k not in {"schema","digest","predictions"}}
    changed["split"] = {"id":"dev-new", "role":"validation", "sample_ids":[str(i) for i in range(16)]}
    runs[1]["metrics"]["evaluation_record"] = make_evaluation_record(**changed)
    rejected = compare_records(runs, "rmse", "minimize")
    assert not rejected["compatible"] and rejected["selected_run_id"] is None
    assert any(d["field"] == "split" for d in rejected["diagnostics"])


def test_real_temperature_fit_intervention_and_leakage_rejection():
    from modelbuilder.backend.calibration import fit_temperature, apply_temperature
    torch.manual_seed(14)
    logits = torch.randn(32, 3) * 4
    labels = torch.randint(0, 3, (32,))
    fit_ids, eval_ids = [f"fit-{i}" for i in range(16)], [f"eval-{i}" for i in range(16)]
    dataset = {"id":"calibration-synthetic", "revision":"1", "content_digest":digest(logits.tolist()), "benchmark":"three-class"}
    artifact = fit_temperature(logits[:16], labels[:16], model_identity="model-1", dataset_identity=dataset,
                               fit_sample_ids=fit_ids, evaluation_sample_ids=eval_ids)
    assert artifact["fit_nll_after"] <= artifact["fit_nll_before"]
    calibrated = apply_temperature(logits[16:], artifact, model_identity="model-1", sample_ids=eval_ids)
    runs = []
    for name, predictions, calibration in [("raw", logits[16:], None), ("calibrated", calibrated, artifact["digest"])]:
        per_item = torch.nn.functional.cross_entropy(predictions, labels[16:], reduction="none")
        value = float(per_item.mean())
        record = make_evaluation_record(dataset=dataset, split={"id":"eval", "role":"test", "sample_ids":eval_ids},
            protocol={"implementation":"classification-eval", "version":"1", "configuration":{}, "preprocessing":{}, "seed":14},
            metric={"name":"nll", "implementation":"torch.nn.functional.cross_entropy", "version":torch.__version__, "parameters":{}, "aggregation":"mean", "direction":"minimize"},
            treatment={"model":"model-1", "checkpoint":"model-1", "calibration":calibration, "reference":None},
            value=value, predictions=[{"item_id":i,"value":float(v)} for i,v in zip(eval_ids,per_item)])
        runs.append({"backend_run_id":name,"status":"succeeded","metrics":{"nll":value,"evaluation_record":record}})
    assert not compare_records(runs,"nll","minimize")["compatible"]
    result = compare_records(runs,"nll","minimize",{"allowed_variations":["calibration"],"paired":True})
    assert result["compatible"] and result["paired"][1]["count"] == 16
    with pytest.raises(ValueError, match="leakage"):
        fit_temperature(logits[:16],labels[:16], model_identity="model-1", dataset_identity=dataset, fit_sample_ids=fit_ids,evaluation_sample_ids=fit_ids)
    with pytest.raises(ValueError, match="another model"):
        apply_temperature(logits[16:],artifact,model_identity="model-2",sample_ids=eval_ids)
    artifact["temperature"] += 1
    with pytest.raises(ValueError, match="identity mismatch"):
        apply_temperature(logits[16:],artifact,model_identity="model-1",sample_ids=eval_ids)


def test_fresh_fixture_provenance_uses_actual_metric_split(tmp_path):
    from modelbuilder.backend import fixtures
    from modelbuilder.evaluation import fixture_evaluation_records, validate_evaluation_record
    data = fixtures.dataset({"count":40})
    initial = fixtures.initialize(data, {"fixture":"custom"}, tmp_path / "initial")
    checkpoint = fixtures.train(data, initial, {"steps":2}, tmp_path / "train")
    metrics = fixtures.evaluate(data, checkpoint, {}, tmp_path / "evaluate")
    records = fixture_evaluation_records(data, checkpoint, {}, metrics)
    assert not validate_evaluation_record(records["validation_loss"])
    assert records["validation_loss"]["split"]["sample_ids"] == [str(i) for i in data["validation_ids"]]
    assert records["accuracy"]["split"]["sample_ids"] == [str(i) for i in data["heldout_ids"]]
    assert records["accuracy"]["treatment"]["model"]["training_reference_digest"]
    assert "best_validation_loss" not in records
