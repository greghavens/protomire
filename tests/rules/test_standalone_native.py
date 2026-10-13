"""Real native target-bound gate permits and denies standalone package/promotion."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import time

import pytest
from modelbuilder.backend import ZenMLBackend
from modelbuilder.backend.components.rule_gate import catalog_entries
from modelbuilder.evaluation import digest
from modelbuilder.models import Actor, ServiceError
from modelbuilder.service import OperationService
from tests.integration.native_fixture_support import synthetic_review, register_project_review
from test_native_rules import PYTHON

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PYTHON.is_file(), reason="Pinned backend required"),
]
EXTRA = """
from zenml import get_step_context
from modelbuilder.backend.components.rule_gate import gate as rule_gate
from modelbuilder.backend.components import read_export
import shutil

def export(*, inputs, parameters, context):
    checkpoint = read_checkpoint(inputs['checkpoint'])
    root = Path(context['output_dir'])
    shutil.copyfile(inputs['checkpoint']/'weights.safetensors', root/'weights.safetensors')
    (root/'loader.py').write_text('def predict(): return 1.0\\n')
    assets = [{'path': name, 'sha256': hashlib.sha256((root/name).read_bytes()).hexdigest()} for name in ('weights.safetensors','loader.py')]
    manifest = {'schema_version':'modelbuilder.export/v1','format':'safetensors',
        'selected_checkpoint': identity(checkpoint),'loader':{'path':'loader.py','entrypoint':'predict'},
        'dependencies': context['identities']['dependencies'],'source':context['identities']['source'],
        'signature':{'input':'none','output':'scalar'},'verification':{'passed':True,'atol':0},
        'rights':checkpoint['rights'],'assets':assets}
    (root/'export.manifest.json').write_text(json.dumps(manifest))
    return {'exported':root}

def evidence(*, inputs, parameters, context):
    target = inputs['target']
    if (target/'checkpoint.manifest.json').exists(): read_checkpoint(target)
    else: read_export(target)
    ctx=get_step_context()
    subject={'project_id':ctx.pipeline_run.config.extra['application_project_id'], 'run_id':str(ctx.pipeline_run.id),
             'config_digest':ctx.pipeline_run.config.extra['recipe_digest'],'data_digest':'d'*64}
    return {'evidence':{'schema_version':'modelbuilder.rule-evidence/v1','subject':subject,
        'source':'actual-native-target-descriptor/v1','verification':'verified',
        'facts':{'integrity':{'actual':{'ready':True}}}}}
"""


def setup(tmp_path, *, builtin=False):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "harness.py"
    path.write_text((ROOT / "tests/scientific/tiny_harness.py").read_text() + EXTRA)
    source_files = [{'path': 'harness.py', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]
    if builtin:
        for relative in ('src/modelbuilder/backend/fixtures.py', 'requirements/backend.lock'):
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / relative).read_bytes())
            source_files.append({'path': relative, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
    for args in (
        ["init", "-q"],
        ["add", "."],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "Synthetic target attestation",
        ],
    ):
        subprocess.run(["git", "-C", str(source), *args], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    ref = {"module": "target_harness", "path": "harness.py", "sha256": sha, "commit": commit}
    policy = {
        "schema_version": "modelbuilder.rules/v1",
        "id": "target-integrity",
        "version": "1",
        "rules": [{"id": "integrity", "kind": "identity/v1", "expected": {"ready": True}}],
    }

    def node(name, role, inputs, outputs, parameters, entrypoint):
        return {
            "id": name,
            "kind": "component.run",
            "implementation_ref": {**ref, "entrypoint": entrypoint},
            "inputs": inputs,
            "outputs": outputs,
            "parameters": parameters,
            "timeout": 90,
            "retry": {"max_attempts": 1, "backoff_seconds": 0},
            "resource_request": {"gpu": 0},
            "environment_ref": "default",
            "cache_policy": "disabled",
            "provenance": {
                "intended_use": "export"
                if "export" in name
                else "train"
                if role == "training"
                else "evaluate"
            },
            "component": {
                "id": "independent." + role,
                "version": "1",
                "protocol": "modelbuilder.component/v1",
                "role": role,
                "parameter_schema": {"type": "object"},
                "capabilities": {
                    "frameworks": ["pytorch"],
                    "resume_modes": [],
                    "export_formats": ["safetensors"] if role == "export" else [],
                },
            },
        }

    def pathport(name):
        return [{"name": name, "type": "Path"}]

    nodes = [
        node(
            "train",
            "training",
            [],
            pathport("checkpoint"),
            {"steps": 2, "target": 2.0, "architecture": {"task": "tiny"}},
            "train",
        ),
        node(
            "evaluate",
            "evaluation",
            pathport("checkpoint"),
            [{"name": "metrics", "type": "dict"}],
            {},
            "evaluate",
        ),
        node("export", "export", pathport("checkpoint"), pathport("exported"), {}, "export"),
    ]
    edges = [
        {"source_node": "train", "source_port": "checkpoint", "target_node": n, "target_port": "checkpoint"}
        for n in ("evaluate", "export")
    ]
    entry = catalog_entries()[0]
    targets = [("checkpoint", "train", "checkpoint"), ("last", "train", "checkpoint")] if builtin else [("checkpoint", "train", "checkpoint"), ("export", "export", "exported")]
    if builtin:
        built = json.loads((ROOT / 'examples/custom-pytorch/recipe.json').read_text())
        nodes = [node for node in built['nodes'] if node['id'] != 'export']
        edges = [edge for edge in built['edges'] if edge['target_node'] != 'export']
        next(node for node in nodes if node['id'] == 'train')['parameters']['steps'] = 3
        # Built-in checkpoint outputs materialize as native Paths. Declare that
        # published type explicitly for the common component guard connection.
        next(node for node in nodes if node['id'] == 'train')['outputs'][0]['type'] = 'Path'
        next(node for node in nodes if node['id'] == 'evaluate')['inputs'][0]['type'] = 'Path'
    for suffix, producer, port in targets:
        evidence = node(
            "evidence_" + suffix,
            "utility",
            pathport("target"),
            [{"name": "evidence", "type": "dict"}],
            {},
            "evidence",
        )
        guard = node(
            "guard_" + suffix,
            "utility",
            [{"name": "evidence", "type": "dict"}, {"name": "target", "type": "Path"}],
            entry["outputs"],
            {"policy": policy},
            "rule_gate",
        )
        if builtin:
            guard['parameters']['target_checkpoint_kind'] = 'best' if suffix == 'checkpoint' else 'last'
        guard["component"] = {
            key: entry[key]
            for key in (
                "id",
                "version",
                "protocol",
                "role",
                "parameter_schema",
                "capabilities",
                "description",
            )
        }
        nodes.extend([evidence, guard])
        edges.extend(
            [
                {"source_node": producer, "source_port": port, "target_node": target, "target_port": "target"}
                for target in (evidence["id"], guard["id"])
            ]
        )
        edges.append(
            {
                "source_node": evidence["id"],
                "source_port": "evidence",
                "target_node": guard["id"],
                "target_port": "evidence",
            }
        )
    recipe = {
        "schema_version": "0",
        "project_id": "pending",
        "recipe_id": "target",
        "revision": 1,
        "name": "Native target guards",
        "source": {
            "repository": "urn:sha256:" + sha,
            "commit": commit,
            "files": source_files,
        },
        "nodes": nodes,
        "edges": edges,
        "resources": [],
        "external_dependencies": [],
        "outputs": [],
        "environment": {"python": "3.12", "requirements": ["zenml==0.97.0"]},
        "evaluation": {},
    }
    if builtin:
        recipe['evaluation'] = built['evaluation']
    review = synthetic_review(recipe, "pending", source)
    backend = ZenMLBackend(
        tmp_path / "state",
        python_executable=str(PYTHON),
        source_root=source,
        trusted_code_roots=[str(source)],
    )
    service = OperationService(tmp_path / "service.sqlite", backend=backend, source_root=source)
    actor = Actor("owner", {"read", "modify", "execute"})
    project = service.invoke(
        "project.create",
        {"slug": "targets", "model_name": "Targets", "recipe": recipe, "idempotency_key": "project"},
        actor,
    )
    register_project_review(service, review, project["project_id"])
    pid = project["project_id"]
    row = service.invoke(
        "rules.policy.register", {"project_id": pid, "policy": policy, "idempotency_key": "policy"}, actor
    )
    project = service.invoke(
        "rules.policy.activate",
        {
            "project_id": pid,
            "policy_digest": row["policy_digest"],
            "expected_revision": project["revision"],
            "operations": ["model.export.package", "model.version.promote"],
            "node_roles": ["training"],
            "idempotency_key": "activate",
        },
        actor,
    )
    request = {"project_id": pid, "recipe_id": project["recipe_id"]}
    submitted = service.invoke(
        "run.submit", {**request, "expected_revision": 1, "trusted": True, "idempotency_key": "run"}, actor
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        result = service.invoke(
            "run.status", {"project_id": pid, "operation_id": submitted["operation_id"]}, actor
        )
        if result["status"] in {"succeeded", "failed", "cancelled"} and not result["worker_alive"]:
            break
        time.sleep(0.2)
    assert result["status"] == "succeeded", result
    return service, actor, project, result, request


def test_actual_target_guard_allows_export_and_promotion_and_denies_substitution(tmp_path):
    service, actor, project, run, request = setup(tmp_path)
    pid = project["project_id"]
    artifacts = service.invoke(
        "run.artifacts",
        {"project_id": pid, "backend_run_id": run["backend_run_id"], "include_binding_descriptors": True},
        actor,
    )["artifacts"]

    def selector(node):
        item = next(x for x in artifacts if x["node_id"] == node and x["port"] == "result")
        return {
            "backend_run_id": run["backend_run_id"],
            "node_id": node,
            "artifact_id": item["artifact_id"],
            "content_sha256": item["binding_descriptor"]["content_sha256"],
        }

    exported = next(x for x in artifacts if x["node_id"] == "export")
    identity = {
        "project_id": pid,
        "backend_run_id": run["backend_run_id"],
        "artifact_id": exported["artifact_id"],
    }
    descriptor = service.invoke("model.export.inspect", identity, actor)
    payload = {
        **identity,
        "expected_export_digest": descriptor["export_digest"],
        "rule_gate": selector("guard_export"),
    }
    packaged = service.invoke("model.export.package", payload, actor)
    assert packaged["sha256"]
    missing = {key: value for key, value in payload.items() if key != "rule_gate"}
    with pytest.raises(ServiceError) as error:
        service.invoke("model.export.package", missing, actor)
    assert error.value.code == "active_rule_decision_required"
    with pytest.raises(ServiceError) as error:
        service.invoke("model.export.package", payload, Actor("outsider", {"read"}, {"other-project"}))
    assert error.value.code == "forbidden"
    with pytest.raises(ServiceError):
        service.invoke("model.export.package", {**payload, "rule_gate": selector("guard_checkpoint")}, actor)
    hypothesis = service.invoke(
        "hypothesis.create",
        {
            "project_id": pid,
            "proposed_change": "Synthetic fixture",
            "expected_outcome": "Measured loss",
            "acceptance_metric": {"name": "loss"},
            "idempotency_key": "hypothesis",
        },
        actor,
    )
    decision = service.invoke(
        "decision.record",
        {
            **request,
            "recipe_revision": 1,
            "hypothesis_id": hypothesis["hypothesis_id"],
            "run_ids": [run["backend_run_id"]],
            "selection_rule": {"metric": "loss", "direction": "minimize"},
            "outcome": "accepted",
            "rationale": "Actual synthetic heldout metric",
            "idempotency_key": "decision",
        },
        actor,
    )
    promotion = {
        "project_id": pid,
        "model_version_ref": run["model_version_ref"],
        "decision_id": decision["decision_id"],
        "expected_revision": project["revision"],
        "idempotency_key": "promote",
        "rule_gate": selector("guard_checkpoint"),
    }
    with pytest.raises(ServiceError) as error:
        service.invoke("model.version.promote", promotion, Actor("owner", {"read"}))
    assert error.value.code == "forbidden"
    with pytest.raises(ServiceError):
        service.invoke(
            "model.version.promote",
            {**promotion, "rule_gate": selector("guard_export"), "idempotency_key": "wrong-target"},
            actor,
        )
    promoted = service.invoke("model.version.promote", promotion, actor)
    assert promoted["model_version_ref"] == run["model_version_ref"]
    stale = copy.deepcopy(payload)
    stale["rule_gate"]["content_sha256"] = "0" * 64
    with pytest.raises(ServiceError):
        service.invoke("model.export.package", stale, actor)
    current = service.invoke("project.get", {"project_id": pid}, actor)
    service.invoke(
        "rules.policy.activate",
        {
            "project_id": pid,
            "policy_digest": digest(next(x for x in service.store.list("rule_policy", pid))["policy"]),
            "expected_revision": current["revision"],
            "operations": ["model.export.package", "model.version.promote"],
            "node_roles": ["training"],
            "idempotency_key": "reactivate",
        },
        actor,
    )
    with pytest.raises(ServiceError):
        service.invoke("model.export.package", payload, actor)
    service.store.close()


def test_actual_builtin_selected_checkpoint_promotion_binds_manifest_and_selection(tmp_path):
    service, actor, project, run, request = setup(tmp_path, builtin=True)
    pid = project['project_id']
    artifacts = service.invoke('run.artifacts', {'project_id': pid, 'backend_run_id': run['backend_run_id'], 'include_binding_descriptors': True}, actor)['artifacts']
    def selector(node):
        item = next(x for x in artifacts if x['node_id'] == node and x['port'] == 'result')
        return {'backend_run_id': run['backend_run_id'], 'node_id': node, 'artifact_id': item['artifact_id'], 'content_sha256': item['binding_descriptor']['content_sha256']}
    # Inspect actual gate results through the supported artifact read path.
    from modelbuilder.backend.common import read_json
    job = next(path for path in service.backend.root.joinpath('jobs').glob('*') if read_json(path/'native.json').get('backend_run_id') == run['backend_run_id'])
    # Native handles are backend-only, so use its pinned interpreter for reads.
    probe = tmp_path / 'inspect_targets.py'
    probe.write_text("import json,sys; from pathlib import Path; from modelbuilder.backend.sdk import native_run; _,r=native_run(Path(sys.argv[1])); print(json.dumps({n:r.steps[n].outputs['result'][0].load()['native_execution']['target'] for n in ('guard_checkpoint','guard_last')}))")
    output = subprocess.check_output([str(PYTHON), str(probe), str(job)], text=True, env=service.backend._env(), cwd=service.backend.root)
    targets = json.loads(output.strip().splitlines()[-1])
    assert targets['guard_checkpoint']['descriptor_digest'] == targets['guard_last']['descriptor_digest']
    assert targets['guard_checkpoint']['checkpoint_selection']['kind'] == 'best'
    assert targets['guard_last']['checkpoint_selection']['kind'] == 'last'
    assert targets['guard_checkpoint']['checkpoint_digest'] == targets['guard_checkpoint']['checkpoint_selection']['sha256']
    hypothesis = service.invoke('hypothesis.create', {'project_id': pid, 'proposed_change': 'Synthetic builtin', 'expected_outcome': 'Measured validation', 'acceptance_metric': {'name': 'validation_loss'}, 'idempotency_key': 'hypothesis'}, actor)
    decision = service.invoke('decision.record', {**request, 'recipe_revision': 1, 'hypothesis_id': hypothesis['hypothesis_id'], 'run_ids': [run['backend_run_id']], 'selection_rule': {'metric': 'validation_loss', 'direction': 'minimize'}, 'outcome': 'accepted', 'rationale': 'Synthetic built-in validation', 'idempotency_key': 'decision'}, actor)
    promotion = {'project_id': pid, 'model_version_ref': run['model_version_ref'], 'decision_id': decision['decision_id'], 'expected_revision': project['revision'], 'idempotency_key': 'promote', 'rule_gate': selector('guard_checkpoint')}
    with pytest.raises(ServiceError):
        service.invoke('model.version.promote', {**promotion, 'idempotency_key': 'wrong-selection', 'rule_gate': selector('guard_last')}, actor)
    promoted = service.invoke('model.version.promote', promotion, actor)
    assert promoted['model_version_ref'] == run['model_version_ref']
    service.store.close()
