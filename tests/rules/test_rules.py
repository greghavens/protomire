from copy import deepcopy
from collections import Counter
import json

import pytest
from modelbuilder.evaluation import digest
from modelbuilder.rules import evaluate_policy, sample_rows, validate_policy
from modelbuilder.backend.components.rule_gate import gate
from modelbuilder.dataset import DatasetRegistry, validate_manifest
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService


def policy(**changes):
    rule = {
        "id": "curve",
        "kind": "convergence/v1",
        "metric": "validation_error",
        "direction": "minimize",
        "criterion": "plateau",
        "threshold": 0.01,
        "window": 3,
        "insufficient_window": "block",
        "min_epochs": 2,
        "max_epochs": 8,
        "max_steps": 1000,
        "max_runtime_seconds": 100,
        "max_cost": 0,
        "resource_modes": {"steps": "active", "runtime_seconds": "active", "cost": "not_applicable"},
        "cost_unit": "local-free",
        "step_unit": "synthetic_work_units",
        "next_epoch_steps": 10,
        "next_epoch_seconds": 1,
        "next_epoch_cost": 0,
    }
    rule.update(changes)
    return {"schema_version": "modelbuilder.rules/v1", "id": "selected", "version": "1", "rules": [rule]}


def evidence(values=(0.8, 0.65, 0.54)):
    subject = {"project_id": "p", "run_id": "run", "config_digest": "a" * 64, "data_digest": "b" * 64}
    return {
        "schema_version": "modelbuilder.rule-evidence/v1",
        "subject": subject,
        "source": "synthetic-heldout-evaluation/v1",
        "verification": "verified",
        "facts": {
            "curve": {
                "status": "completed",
                "resource_units": {
                    "epochs": "complete_dataset_pass",
                    "steps": "synthetic_work_units",
                    "runtime_seconds": "seconds",
                    "cost": "local-free",
                },
                "completed_epochs": len(values),
                "steps": len(values) * 10,
                "runtime_seconds": len(values),
                "cost": 0,
                "checkpoint_digest": "c" * 64,
                "expected_checkpoint_digest": "c" * 64,
                "continuation": {"mode": "exact_state", "supported": True},
                "observations": [
                    {
                        "epoch": i,
                        "metric": "validation_error",
                        "value": value,
                        "checkpoint_digest": "c" * 64,
                        "subject_digest": digest(subject),
                    }
                    for i, value in enumerate(values, 1)
                ],
            }
        },
    }


def test_decisions_are_deterministic_and_bounds_are_not_convergence():
    p, e = policy(), evidence()
    result = evaluate_policy(p, e)
    assert result == evaluate_policy(deepcopy(p), deepcopy(e))
    assert result["outcome"] == "continue"
    assert result["rules"][0]["selected_options"] == p["rules"][0]
    stopped = evaluate_policy(policy(max_epochs=3), e)
    assert stopped["outcome"] == "stop" and stopped["reasons"] == ["resource_bound:max_epochs"]
    assert stopped["rules"][0]["observed"]["criterion_met"] is False
    assert evaluate_policy(p, evidence((0.5, 0.495, 0.49)))["reasons"] == ["criterion_met"]
    assert evaluate_policy(p, evidence((0.5, 0.6, 0.7)))["outcome"] == "continue"
    assert evaluate_policy(policy(criterion="target", threshold=0.6), e)["outcome"] == "stop"
    assert evaluate_policy(policy(insufficient_window="collect_within_bounds"), evidence((0.8,)))[
        "reasons"
    ] == ["collecting_window"]
    assert evaluate_policy(
        policy(insufficient_window="collect_within_bounds", min_epochs=1, max_epochs=1), evidence((0.8,))
    )["reasons"] == ["resource_bound:max_epochs"]


@pytest.mark.parametrize(
    "change",
    ["missing", "short", "failed", "checkpoint", "mixed_run", "gap", "bool", "capability", "unverified"],
)
def test_missing_conflicting_failed_evidence_never_continues(change):
    e = evidence()
    facts = e["facts"]["curve"]
    if change == "missing":
        del facts["cost"]
    if change == "short":
        facts["observations"] = facts["observations"][-1:]
    if change == "failed":
        facts["status"] = "failed"
    if change == "checkpoint":
        facts["checkpoint_digest"] = "d" * 64
    if change == "mixed_run":
        facts["observations"][0]["subject_digest"] = "d" * 64
    if change == "gap":
        facts["observations"][1]["epoch"] = 3
    if change == "bool":
        facts["observations"][0]["value"] = True
    if change == "capability":
        facts["continuation"]["mode"] = "warm_start"
    if change == "unverified":
        e["verification"] = "unknown"
    assert evaluate_policy(policy(), e)["outcome"] == "blocked"
    with pytest.raises(ValueError, match="rule_blocked"):
        gate(inputs={"evidence": e}, parameters={"policy": policy()}, context={})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_evidence_is_rejected(value):
    e = evidence()
    e["facts"]["curve"]["observations"][0]["value"] = value
    with pytest.raises(ValueError):
        evaluate_policy(policy(), e)


def test_conflicting_rules_and_cancellation_are_explicit(tmp_path):
    p, e = policy(), evidence()
    second = {**p["rules"][0], "id": "other", "criterion": "target", "threshold": 1}
    p["rules"].append(second)
    e["facts"]["other"] = deepcopy(e["facts"]["curve"])
    result = evaluate_policy(p, e)
    assert result["outcome"] == "blocked" and result["reasons"] == ["conflicting_decisions"]
    e["facts"]["curve"]["status"] = "cancelled"
    e["facts"].pop("other")
    assert evaluate_policy(policy(), e)["outcome"] == "cancelled"
    cancel = tmp_path / "cancel"
    cancel.touch()
    with pytest.raises(RuntimeError, match="rule_cancelled"):
        gate(
            inputs={"evidence": evidence()},
            parameters={"policy": policy()},
            context={"cancel_file": str(cancel)},
        )


def sample_spec(**changes):
    return {
        "kind": "rows.sample",
        "version": "1",
        "split_ids": ["train"],
        "fields": ["category"],
        "fraction": 0.1,
        "seed": "seed-1",
        "allocation": "proportional",
        "min_per_stratum": 1,
        "max_share_error": 0.01,
        "required_strata": [],
        **changes,
    }


def rows():
    return [
        {"sample_id": str(i), "split_id": "train", "row": {"id": str(i), "category": "A" if i < 600 else "B"}}
        for i in range(1000)
    ]


def test_percentage_preserves_proportions_and_order_and_selected_identity():
    source = rows()
    selected, report = sample_rows(source, sample_spec())
    assert Counter(x["row"]["category"] for x in selected) == {"A": 60, "B": 40}
    assert selected == [x for x in source if x["sample_id"] in report["selected_sample_ids"]]
    assert sample_rows(source[::-1], sample_spec())[1] == report
    assert sample_rows(source, sample_spec())[1] == report
    equal, _ = sample_rows(source, sample_spec(allocation="equal"))
    assert Counter(x["row"]["category"] for x in equal) == {"A": 50, "B": 50}
    assert len(sample_rows(source, sample_spec(fraction=1))[0]) == 1000
    with pytest.raises(ValueError, match="minimum"):
        sample_rows(source, sample_spec(fraction=0.001))


def test_real_registry_sampling_and_streaming_fail_closed(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text("".join(json.dumps(x["row"]) + "\n" for x in rows()))
    import hashlib

    manifest = {
        "name": "sample",
        "benchmark": "synthetic",
        "license": "MIT",
        "provenance": {"source": "synthetic"},
        "sources": [
            {
                "id": "source",
                "root_id": "local",
                "path": path.name,
                "format": "jsonl",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        ],
        "id_column": "id",
        "row_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "category": {"type": "string"}},
            "required": ["id", "category"],
            "additionalProperties": False,
        },
        "splits": [{"id": "train", "role": "train", "source_ids": ["source"]}],
        "transformations": [sample_spec()],
    }
    registry = DatasetRegistry(None, project_roots={"p": {"local": tmp_path}})
    _, selected, _, report = registry._materialize("p", manifest)
    assert Counter(x["row"]["category"] for x in selected) == {"A": 60, "B": 40}
    assert report[0]["input_rows"] == 1000 and report[0]["output_rows"] == 100
    with pytest.raises(ServiceError) as error:
        validate_manifest({**manifest, "access_mode": "streaming"})
    assert error.value.code == "dataset_sampling_streaming_unsupported"


def test_shared_operations_immutable_permissions_project_and_retry(tmp_path):
    service = OperationService(tmp_path / "rules.sqlite")
    owner = Actor("owner", frozenset({"read", "modify"}))
    project = service.invoke(
        "project.create", {"slug": "rules", "model_name": "Rules", "idempotency_key": "project"}, owner
    )
    pid = project["project_id"]
    request = {"project_id": pid, "policy": policy(), "idempotency_key": "policy"}
    row = service.invoke("rules.policy.register", request, owner)
    assert service.invoke("rules.policy.register", request, owner) == row
    e = evidence()
    e["subject"]["project_id"] = pid
    for item in e["facts"]["curve"]["observations"]:
        item["subject_digest"] = digest(e["subject"])
    result = service.invoke(
        "rules.evaluate", {"project_id": pid, "policy_digest": row["policy_digest"], "evidence": e}, owner
    )
    assert result["outcome"] == "continue"
    with pytest.raises(ServiceError) as error:
        service.invoke("rules.policy.register", request, Actor("reader", frozenset({"read"})))
    assert error.value.code == "forbidden"
    with pytest.raises(ServiceError) as error:
        service.invoke(
            "rules.evaluate",
            {"project_id": pid, "policy_digest": row["policy_digest"], "evidence": evidence()},
            owner,
        )
    assert error.value.code == "rule_evidence_project_mismatch"
    with pytest.raises(ValueError):
        validate_policy(policy(min_epochs=9))
    service.store.close()


@pytest.mark.asyncio
async def test_http_mcp_gateway_and_catalog_use_one_evaluator(tmp_path):
    import httpx
    from modelbuilder.api import create_app
    from modelbuilder.mcp_server import OperationGateway, OperationError, catalog_tools
    from modelbuilder.models import OPERATIONS

    service = OperationService(tmp_path / "http.sqlite")
    owner = Actor("owner", frozenset({"read", "modify"}))
    app = create_app(
        service=service, tokens={"owner-token": owner, "read-token": Actor("owner", frozenset({"read"}))}
    )
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1",
        transport=httpx.ASGITransport(app=app),
        headers={"Authorization": "Bearer owner-token"},
    ) as client:
        gateway = OperationGateway(client)
        project = await gateway.invoke(
            "project.create", {"slug": "http-rules", "model_name": "Rules", "idempotency_key": "p"}
        )
        pid = project["project_id"]
        request = {"project_id": pid, "policy": policy(), "idempotency_key": "rules"}
        row = await gateway.invoke("rules.policy.register", request)
        assert row == (await client.post("/v1/operations/rules.policy.register", json=request)).json()
        e = evidence()
        e["subject"]["project_id"] = pid
        for item in e["facts"]["curve"]["observations"]:
            item["subject_digest"] = digest(e["subject"])
        query = {"project_id": pid, "policy_digest": row["policy_digest"], "evidence": e}
        assert (
            await gateway.invoke("rules.evaluate", query)
            == (await client.post("/v1/operations/rules.evaluate", json=query)).json()
        )
        tools, aliases = catalog_tools(await gateway.catalog())
        assert aliases["rules_evaluate"] == "rules.evaluate"
        tool = next(x for x in tools if x.name == "rules_evaluate")
        assert tool.inputSchema == OPERATIONS["rules.evaluate"].input_schema
        client.headers["Authorization"] = "Bearer read-token"
        with pytest.raises(OperationError) as error:
            await gateway.invoke("rules.policy.register", request)
        assert error.value.status == 403
    service.store.close()
