"""Real Python HTTP → JavaScript serialization → Python import transport tests."""
from __future__ import annotations

import json
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest

from modelbuilder.api import create_app
from modelbuilder.models import Actor, MAX_BUNDLE_JSON_BYTES, ServiceError
from modelbuilder.recipe import default_recipe, parse_bundle_json
from modelbuilder.service import OperationService

OWNER = Actor("transport-owner", {"read", "modify"})


@pytest.fixture
def context(tmp_path):
    service = OperationService(tmp_path / "transport.sqlite3")
    body = default_recipe("placeholder-project", "placeholder-recipe", "Numeric integrity")
    body["evaluation"] = {"coefficient": 0.0, "whole_float": 1.0, "negative_zero": -0.0,
                          "large_integer": 9007199254740993, "unicode_label": "λ"}
    project = service.invoke("project.create", {"slug": "transport", "model_name": "Transport model",
                             "recipe": body, "idempotency_key": "project"}, OWNER)
    identity = {"project_id": project["project_id"], "recipe_id": project["recipe_id"]}
    yield service, identity
    service.store.close()


def invoke(service, operation, payload):
    return service.invoke(operation, payload, OWNER)


def javascript(source, body):
    node = shutil.which("node")
    if not node:
        pytest.skip("Actual JavaScript roundtrip requires Node.js")
    return subprocess.check_output([node, "-e", source], input=body, text=True)


def test_http_raw_bundle_survives_actual_javascript_roundtrip(context):
    service, identity = context
    client = TestClient(create_app(service, {"transport-test-token": OWNER}))
    headers = {"Authorization": "Bearer transport-test-token"}
    exported = client.post("/v1/operations/recipe.export", json={**identity, "format": "json"}, headers=headers)
    assert exported.status_code == 200
    raw = exported.json()["bundle_json"]
    assert '"coefficient":0.0' in raw
    assert '"large_integer":9007199254740993' in raw
    # The UI parses only the response envelope, then carries the opaque JSON string.
    request = javascript("""
        let text=''; process.stdin.setEncoding('utf8');
        process.stdin.on('data',x=>text+=x);
        process.stdin.on('end',()=>{
          const envelope=JSON.parse(text);
          process.stdout.write(JSON.stringify({project_id:JSON.parse(envelope.bundle_json).recipe.project_id,
            bundle_json:envelope.bundle_json,expected_revision:1,idempotency_key:'raw-import'}));
        });
    """, exported.text)
    assert json.loads(request)["bundle_json"] == raw
    response = client.post("/v1/operations/recipe.import", content=request,
                           headers={**headers, "Content-Type": "application/json"})
    assert response.status_code == 200, response.text
    imported = response.json()
    assert imported["recipe_id"] == identity["recipe_id"]
    assert imported["revision"] == 2
    assert type(imported["evaluation"]["coefficient"]) is float
    assert imported["evaluation"]["large_integer"] == 9007199254740993
    assert imported["import_report"]["execution_allowed"] is False


def test_object_bundle_stays_compatible_but_lossy_js_object_still_rejected(context):
    service, identity = context
    bundle = invoke(service, "recipe.export", identity)
    assert set(bundle) == {"bundle_version", "recipe", "digest", "files", "bundle_digest"}
    lossy = javascript("let s='';process.stdin.on('data',x=>s+=x);process.stdin.on('end',()=>process.stdout.write(JSON.stringify(JSON.parse(s))));", json.dumps(bundle))
    assert json.loads(lossy)["recipe"]["evaluation"]["coefficient"] == 0
    with pytest.raises(ServiceError) as error:
        invoke(service, "recipe.import", {"project_id": identity["project_id"], "bundle": json.loads(lossy),
               "expected_revision": 1, "idempotency_key": "lossy"})
    assert error.value.code == "recipe_hash_mismatch"
    imported = invoke(service, "recipe.import", {"project_id": identity["project_id"], "bundle": bundle,
                      "expected_revision": 1, "idempotency_key": "python-object"})
    assert imported["revision"] == 2


@pytest.mark.parametrize("variant", ["both", "neither", "tamper", "duplicate", "nan", "path", "depth"])
def test_raw_bundle_rejects_ambiguous_or_modified_inputs_without_mutating(context, variant):
    service, identity = context
    current = invoke(service, "recipe.get", identity)
    exported = invoke(service, "recipe.export", {**identity, "format": "json"})
    raw = exported["bundle_json"]
    payload = {"project_id": identity["project_id"], "expected_revision": 1, "idempotency_key": variant}
    if variant == "both":
        payload.update(bundle=parse_bundle_json(raw), bundle_json=raw)
    elif variant == "neither":
        pass
    elif variant == "tamper":
        payload["bundle_json"] = raw.replace('"coefficient":0.0', '"coefficient":0.5')
    elif variant == "duplicate":
        payload["bundle_json"] = raw.replace('"bundle_version":"0"', '"bundle_version":"0","bundle_version":"0"')
    elif variant == "nan":
        payload["bundle_json"] = raw.replace('"coefficient":0.0', '"coefficient":NaN')
    elif variant == "path":
        payload["bundle_json"] = "/etc/passwd"
    elif variant == "depth":
        payload["bundle_json"] = '{"x":' * 80 + '0' + '}' * 80
    with pytest.raises(ServiceError):
        invoke(service, "recipe.import", payload)
    assert invoke(service, "recipe.get", identity) == current


def test_raw_bundle_byte_limit_not_only_character_count(context, monkeypatch):
    service, identity = context
    monkeypatch.setattr("modelbuilder.service.MAX_BUNDLE_JSON_BYTES", 64)
    with pytest.raises(ServiceError) as error:
        invoke(service, "recipe.import", {"project_id": identity["project_id"], "expected_revision": 1,
               "bundle_json": '"' + "λ" * 40 + '"', "idempotency_key": "utf8-size"})
    assert error.value.code == "bundle_limit"
    assert invoke(service, "recipe.get", identity)["revision"] == 1
    with pytest.raises(ServiceError) as error:
        invoke(service, "recipe.export", {**identity, "format": "json"})
    assert error.value.status_code == 413


def test_declared_raw_bundle_limit_is_enforced(context):
    service, identity = context
    with pytest.raises(ServiceError) as error:
        invoke(service, "recipe.import", {"project_id": identity["project_id"], "expected_revision": 1,
               "bundle_json": " " * (MAX_BUNDLE_JSON_BYTES + 1), "idempotency_key": "oversized"})
    assert error.value.code == "validation_error"
    assert invoke(service, "recipe.get", identity)["revision"] == 1
