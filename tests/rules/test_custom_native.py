"""Independent project method loaded only by the real trusted native runtime."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest
from modelbuilder.backend.components import identity
from test_custom_methods import declaration, custom_policy
from test_native_rules import setup, execute, PYTHON

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PYTHON.is_file(), reason="Pinned backend required"),
]
METHOD = """
def range_rule(*, facts, parameters, subject):
    values = facts['values']
    spread = max(values) - min(values)
    return {'outcome': 'passed' if spread <= parameters['width'] else 'blocked',
            'reasons': ['range_within_width' if spread <= parameters['width'] else 'range_exceeds_width'],
            'observed': {'spread': spread}}
"""


def custom_setup(tmp_path, mutation=None):
    backend, request = setup(tmp_path)
    recipe = request["recipe"]
    recipe.pop("bounded_iterations")
    source = Path(recipe["source"]["repository"])
    path = source / "harness.py"
    content = path.read_text().replace(
        "'facts': {'curve': facts}", "'facts': {'spread': {'values': [1., 1.02, .99]}}"
    )
    if mutation == "provenance":
        content = content.replace(
            "from modelbuilder.backend.components.rule_gate import gate",
            "from modelbuilder.backend.components.rule_gate import gate as platform_gate\n"
            "def gate(*, inputs, parameters, context):\n"
            "    result = platform_gate(inputs=inputs, parameters=parameters, context=context)\n"
            "    context['source']['commit'] = '0'*40\n"
            "    context['environment']['modelbuilder_runtime_source'] = '0'*64\n"
            "    return result\n",
        )
    if mutation == "receipt":
        content = content.replace(
            "from modelbuilder.backend.components.rule_gate import gate",
            "from modelbuilder.backend.components.rule_gate import gate as platform_gate\n"
            "def gate(*, inputs, parameters, context):\n"
            "    result = platform_gate(inputs=inputs, parameters=parameters, context=context)\n"
            "    result['allowed'] = 1\n"
            "    return result\n",
        )
    if mutation == "options":
        content = content.replace(
            "from modelbuilder.backend.components.rule_gate import gate",
            "from modelbuilder.backend.components.rule_gate import gate as platform_gate\n"
            "def gate(*, inputs, parameters, context):\n"
            "    result = platform_gate(inputs=inputs, parameters=parameters, context=context)\n"
            "    result['result']['rules'][0]['selected_options']['parameters']['width'] = False\n"
            "    return result\n",
        )
        content = content.replace("[1., 1.02, .99]", "[1., 1., 1.]")
    path.write_text(content + METHOD)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "Independent custom predicate",
        ],
        check=True,
    )
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    recipe["source"].update(commit=commit, files=[{"path": "harness.py", "sha256": sha}])
    for node in recipe["nodes"]:
        node["implementation_ref"].update(commit=commit, sha256=sha)
    guard = next(x for x in recipe["nodes"] if x["id"] == "guard")
    method = declaration({**guard["implementation_ref"], "entrypoint": "range_rule"})
    guard["parameters"]["policy"] = custom_policy(method)
    if mutation == "options":
        guard["parameters"]["policy"]["rules"][0]["parameters"]["width"] = 0
    action = next(x for x in recipe["nodes"] if x["id"] == "finish")
    action["required_guards"] = ["guard"]
    action["condition"] = {"source_node": "guard", "source_port": "allowed", "equals": True}
    return backend, request


def load_reports(backend, tmp_path, result):
    script = tmp_path / "read_custom_native.py"
    script.write_text(
        "import json,sys\nfrom zenml.client import Client\nr=Client().get_pipeline_run(sys.argv[1])\nprint(json.dumps([a.load() for s in r.steps.values() for a in s.outputs.get('result',[])]))\n"
    )
    response = subprocess.run(
        [str(PYTHON), str(script), result["backend_run_id"]],
        env=backend._env(),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(response.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("mutation", [None, "provenance"])
def test_actual_custom_method_execution_and_platform_snapshot_attestation(tmp_path, mutation):
    backend, request = custom_setup(tmp_path, mutation)
    source_digest = identity(copy.deepcopy(request["recipe"]["source"]))
    result = execute(backend, request)
    assert result["status"] == "succeeded", result
    reports = load_reports(backend, tmp_path, result)
    assert len(reports) == 1 and reports[0]["outcome"] == "passed"
    native = reports[0]["native_execution"]
    method = next(x for x in request["recipe"]["nodes"] if x["id"] == "guard")["parameters"]["policy"][
        "rules"
    ][0]["method"]
    assert native["methods"] == [method]
    assert native["source_digest"] == source_digest
    assert native["runtime_source_digest"] != "0" * 64
    assert native["producer"]["backend_run_id"] == result["backend_run_id"]
    assert native["inputs"] and (tmp_path / "epochs").read_text().splitlines() == ["1"]


@pytest.mark.parametrize("mutation", ["receipt", "options"])
def test_actual_native_canonical_boolean_forgery_fails(tmp_path, mutation):
    backend, request = custom_setup(tmp_path, mutation)
    assert execute(backend, request)["status"] == "failed"


def test_method_source_change_is_rejected_before_native_execution(tmp_path):
    backend, request = custom_setup(tmp_path)
    source = Path(request["recipe"]["source"]["repository"]) / "harness.py"
    source.write_text(source.read_text() + "\n# unapproved source drift\n")
    plan = backend.invoke("run.plan", request)
    assert plan["executable"] is False
    assert not (tmp_path / "epochs").exists()
