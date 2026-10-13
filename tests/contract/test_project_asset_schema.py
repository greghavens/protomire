"""The scaffold preserves the canonical Hub provenance schema, including extensions."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import copy

from jsonschema import Draft202012Validator
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = spec_from_file_location("project_schema_export", ROOT / "scripts/export_contracts.py")
module = module_from_spec(spec)
spec.loader.exec_module(module)


def test_hf_specialization_retains_license_text_scope_hash_and_custom_extensions():
    canonical = {"type": "object", "additionalProperties": False, "required": ["provider", "license_provenance"],
                 "properties": {"provider": {"type": "string", "maxLength": 80},
                     "license_provenance": {"type": "object", "additionalProperties": False,
                         "required": ["text", "source", "sha256", "scope"], "properties": {
                             "text": {"type": "string", "minLength": 1},
                             "source": {"type": "string", "minLength": 1},
                             "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                             "scope": {"enum": ["weights", "code", "dataset"]}}},
                     "future_provenance_extension": {"type": "object", "properties": {"verified": {"type": "boolean"}}}}}
    before = copy.deepcopy(canonical)
    scoped = module.project_asset_schema(canonical)
    assert canonical == before
    assert scoped["properties"]["license_provenance"] == canonical["properties"]["license_provenance"]
    assert scoped["properties"]["future_provenance_extension"] == canonical["properties"]["future_provenance_extension"]
    validate = Draft202012Validator(scoped).validate
    payload = {"provider": "huggingface", "license_provenance": {"text": "actual license text", "source": "LICENSE",
                                                                "sha256": "a" * 64, "scope": "weights"}}
    validate(payload)
    with pytest.raises(Exception):
        validate({**payload, "provider": "unregistered-other"})
    with pytest.raises(Exception):
        validate({"provider": "huggingface", "license_provenance": {"license": "MIT"}})


def test_missing_canonical_provider_contract_fails_closed():
    with pytest.raises(ValueError):
        module.project_asset_schema({"type": "object"})
