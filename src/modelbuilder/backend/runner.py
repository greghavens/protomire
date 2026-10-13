"""Pinned, operator-selected interpreters for the existing trusted component ABI.

This is an environment/adapter contract, not a framework, installer, scheduler,
container sandbox, or claim that an upstream package works on every device.
Planning inspects distribution metadata only. Source imports occur exclusively
inside the existing supervised native attempt after trust/closure checks.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys

from jsonschema import Draft202012Validator

PROTOCOL = "modelbuilder.runner/v1"
ENVIRONMENT_PROTOCOL = "modelbuilder.runner-environment/v1"
MAX_PROFILE_BYTES = 1024 * 1024
NAME = {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"}
VERSION = {"type": "string", "pattern": r"^[0-9][A-Za-z0-9.!+_-]{0,127}$"}


def _object(properties, required=None):
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": list(properties) if required is None else required}


RUNNER_SCHEMA = _object({
    "protocol": {"const": PROTOCOL},
    "stack": {"type": "array", "minItems": 1, "maxItems": 32, "items": _object({
        "id": NAME, "category": {"enum": ["framework", "training-library", "fine-tuning-library",
                                            "launcher", "distributed-strategy"]},
        "distribution": NAME, "version": VERSION})},
    "launcher": _object({"kind": {"enum": ["python", "accelerate", "torchrun", "deepspeed"]},
                         "processes": {"type": "integer", "minimum": 1, "maximum": 1024}}),
    "contracts": _object({"model": {"const": "source-owned-native"},
                           "dataset": {"const": "native-component-inputs"},
                           "checkpoint": {"const": "modelbuilder.checkpoint/v1"},
                           "evaluation": {"const": "modelbuilder.evaluation/v1"}}),
})
ENVIRONMENT_SCHEMA = _object({
    "schema_version": {"const": ENVIRONMENT_PROTOCOL},
    "python": {"type": "string", "pattern": r"^3\.12\.[0-9]{1,3}$"},
    "implementation": {"const": "CPython"},
    "executable_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
    "platform": {"enum": ["linux", "darwin", "win32"]},
    "machine": NAME,
    "packages": {"type": "object", "minProperties": 3, "maxProperties": 4096,
                 "propertyNames": {"type": "string", "pattern": r"^[a-z0-9]+(?:-[a-z0-9]+)*$"},
                 "additionalProperties": VERSION},
})


def normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def environment_snapshot() -> dict:
    """Read versions; never import a framework, resolve dependencies or install."""
    packages = {}
    for distribution in importlib.metadata.distributions():
        raw = distribution.metadata.get("Name")
        if not raw:
            raise ValueError("Runner distribution metadata is missing its name")
        name = normalized_name(raw)
        if name in packages:
            raise ValueError("Runner contains ambiguous duplicate distribution metadata: " + name)
        packages[name] = distribution.version
    with open(sys.executable, "rb") as stream:
        executable_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"schema_version": ENVIRONMENT_PROTOCOL, "executable_sha256": executable_sha256, "python": platform.python_version(),
            "implementation": platform.python_implementation(), "platform": sys.platform,
            "machine": platform.machine(), "packages": dict(sorted(packages.items()))}


def validate_environment(profile: dict) -> dict:
    Draft202012Validator(ENVIRONMENT_SCHEMA).validate(profile)
    required = {"jsonschema", "psutil", "pydantic"}
    if not required <= profile["packages"].keys():
        raise ValueError("Runner environment must include pinned jsonschema, psutil and pydantic ABI dependencies")
    return profile


def validate_runner(node: dict) -> dict | None:
    descriptor = node.get("component", {}).get("runner")
    if descriptor is None:
        return None
    Draft202012Validator(RUNNER_SCHEMA).validate(descriptor)
    if node.get("environment_ref", "default") == "default":
        raise ValueError("Runner requires an explicit operator-registered environment_ref")
    stack = descriptor["stack"]
    if len({item["id"] for item in stack}) != len(stack):
        raise ValueError("Runner stack IDs must be unique")
    pins = {}
    for item in stack:
        name = normalized_name(item["distribution"])
        if name in pins and pins[name] != item["version"]:
            raise ValueError("Runner stack contains conflicting distribution versions")
        pins[name] = item["version"]
    component = node["component"]
    if "exact_state" in component["capabilities"]["resume_modes"]:
        raise ValueError("Runner v1 exact-state continuation is unverified; declare warm_start or no resume")
    if component["role"] == "training" and not any(p["type"] == "Path" for p in node.get("outputs", [])):
        raise ValueError("Runner training must return a native checkpoint Path")
    if component["role"] == "evaluation" and not any(p["type"] in {"dict", "EvaluationArtifact"}
                                                   for p in node.get("outputs", [])):
        raise ValueError("Runner evaluation must return immutable evaluation records")
    return descriptor


# Entirely platform-owned program. Does not load Modelbuilder or project modules.
# The code deliberately uses only Python's standard library so missing packages
# can be reported without importing or installing anything in that environment.
METADATA_PROBE = r'''
import hashlib, importlib.metadata, json, platform, re, sys
packages={}
for distribution in importlib.metadata.distributions():
    raw=distribution.metadata.get("Name")
    if not raw: raise ValueError("Unnamed distribution metadata")
    name=re.sub(r"[-_.]+", "-", raw).lower()
    if name in packages: raise ValueError("Duplicate distribution metadata: " + name)
    packages[name]=distribution.version
with open(sys.executable,"rb") as stream: executable_sha256=hashlib.file_digest(stream,"sha256").hexdigest()
print(json.dumps({"schema_version":"modelbuilder.runner-environment/v1", "executable_sha256":executable_sha256, "python":platform.python_version(),
 "implementation":platform.python_implementation(), "platform":sys.platform, "machine":platform.machine(),
 "packages":dict(sorted(packages.items()))},sort_keys=True))
'''


def inspect_environment(python: str) -> dict:
    """Operator interpreter only; isolated read-only metadata inspection."""
    safe = {key: value for key, value in os.environ.items()
            if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL"}}
    try:
        result = subprocess.run([python, "-I", "-B", "-c", METADATA_PROBE], env=safe,
                                stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30)
        if result.returncode or len(result.stdout.encode()) > MAX_PROFILE_BYTES:
            raise ValueError("Runner metadata inspection failed or exceeded its bound")
        return validate_environment(json.loads(result.stdout))
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        raise ValueError("Runner metadata inspection failed") from error


def load_environment(path: str, expected_sha256: str) -> dict:
    from .dependency_files import dependency_sha256
    source = Path(path)
    if source.stat().st_size > MAX_PROFILE_BYTES or dependency_sha256(path) != expected_sha256:
        raise ValueError("Runner environment profile is oversized or changed")
    raw = source.read_bytes()
    if len(raw) > MAX_PROFILE_BYTES or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("Runner environment profile changed while reading")
    return validate_environment(json.loads(raw))


def verify_snapshot(expected: dict, actual: dict) -> None:
    validate_environment(expected)
    validate_environment(actual)
    if expected != actual:
        changed = sorted(key for key in expected if expected[key] != actual.get(key))
        raise ValueError("Runner environment differs from its exact pinned profile: " + ", ".join(changed))


def validate_selection(node: dict, profile: dict, device: str, *, live_evaluation=False) -> None:
    descriptor = validate_runner(node)
    if descriptor is None:
        raise ValueError("Non-default environments require a source-pinned runner component")
    # This boundary is intentionally narrower than upstream support. Existing
    # reviewed Transformers/MLX accelerator routes remain unchanged.
    if profile["platform"] != "linux" or device != "cpu":
        raise ValueError("Runner v1 process lifecycle is verified only on Linux CPU")
    if descriptor["launcher"] != {"kind": "python", "processes": 1}:
        raise ValueError("Runner v1 supports one Python process; distributed launchers are unverified")
    if live_evaluation:
        raise ValueError("Cross-environment live evaluation is unverified; use native downstream evaluation")
    for item in descriptor["stack"]:
        if profile["packages"].get(normalized_name(item["distribution"])) != item["version"]:
            raise ValueError("Runner stack is absent or differs from the selected profile: " + item["id"])
        if item["category"] == "distributed-strategy":
            raise ValueError("Distributed strategies require separate lifecycle/hardware acceptance")


def resolve_environments(backend, recipe: dict, *, inspect=True) -> dict:
    """Build job-private routing from operator paths, never recipe executables."""
    routes = {}
    profiles = {}
    declarations = {item["id"]: item for item in recipe.get("resources", []) if item.get("kind") == "environment"}
    configured = getattr(backend, "runner_environments", {})
    for node in recipe.get("nodes", []):
        alias = node.get("environment_ref", "default")
        descriptor = node.get("component", {}).get("runner")
        if alias == "default" and descriptor is None:
            continue
        validate_runner(node)
        if alias not in configured or alias not in declarations:
            raise ValueError("Runner environment is not both operator-registered and recipe-declared: " + alias)
        reference = declarations[alias]
        registration = configured[alias]
        if reference.get("sha256") != registration["profile_sha256"]:
            raise ValueError("Recipe runner profile differs from operator-approved receipt: " + alias)
        path = backend.dependency_paths.get(alias)
        if not path or not re.fullmatch(r"[0-9a-f]{64}", reference.get("sha256", "")):
            raise ValueError("Runner environment needs an operator-bound SHA256 profile: " + alias)
        if alias not in profiles:
            profile = load_environment(path, reference["sha256"])
            if inspect:
                verify_snapshot(profile, inspect_environment(registration["python_executable"]))
            profiles[alias] = profile
        profile = profiles[alias]
        validate_selection(node, profile, recipe.get("environment", {}).get("device", "cpu"),
                           live_evaluation=recipe.get("evaluation", {}).get("live", {}).get("enabled", False))
        routes[node["id"]] = {"environment_ref": alias, "python": registration["python_executable"],
                              "profile": profile, "profile_sha256": reference["sha256"],
                              "profile_path": path}
    return routes


def verify_runtime(spec: dict) -> dict:
    """Recheck before source import; planning is never an execution guarantee."""
    route = spec.get("runner_environment")
    if not isinstance(route, dict):
        raise ValueError("Runner component has no operator-resolved execution environment")
    profile = load_environment(route["profile_path"], route["profile_sha256"])
    if profile != route["profile"]:
        raise ValueError("Runner profile differs from its prepared native attempt")
    verify_snapshot(profile, environment_snapshot())
    validate_selection(spec["component_node"], profile, spec.get("placement", {}).get("device", "cpu"),
                       live_evaluation=bool(spec.get("live_evaluation")))
    return {"protocol": PROTOCOL, "environment_ref": route["environment_ref"],
            "profile_sha256": route["profile_sha256"], "launcher": {"kind": "python", "processes": 1},
            "verification": "exact_distribution_metadata", "framework_smoke": "not_implied"}


def command(python: str, module: str, *arguments: str) -> list[str]:
    """Fixed platform body in the chosen environment; no shell or recipe CLI."""
    if module != "modelbuilder.backend.process_execution":
        raise ValueError("Runner commands may launch only the platform native-attempt body")
    bootstrap = ("import importlib,sys; sys.path.insert(0, sys.argv.pop(1)); "
                 "importlib.import_module(sys.argv.pop(1)).main()")
    return [python, "-I", "-B", "-c", bootstrap, str(Path(__file__).resolve().parents[2]),
            module, *map(str, arguments)]


def validate_evaluation_outputs(results: dict, node: dict) -> None:
    if node["component"]["role"] != "evaluation":
        return
    from ..evaluation import validate_evaluation_record
    records = []
    for value in results.values():
        if not isinstance(value, dict) or "evaluation_records" not in value:
            continue
        collection = value["evaluation_records"]
        if not isinstance(collection, dict) or not collection:
            raise ValueError("Runner evaluation requires valid immutable modelbuilder.evaluation/v1 records")
        for name, record in collection.items():
            if (validate_evaluation_record(record) or record["metric"]["name"] != name
                    or isinstance(value.get(name), bool) or not isinstance(value.get(name), (int, float))
                    or value[name] != record["value"]):
                raise ValueError("Runner evaluation metric differs from its immutable evidence")
            records.append(record)
    if not records:
        raise ValueError("Runner evaluation requires valid immutable modelbuilder.evaluation/v1 records")


def main():
    """Explicit operator metadata capture, never dependency installation."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, help="Existing operator-approved interpreter")
    parser.add_argument("--output", type=Path, required=True, help="New environment receipt JSON path")
    arguments = parser.parse_args()
    receipt = inspect_environment(os.path.abspath(os.path.expanduser(arguments.python)))
    raw = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode()
    with arguments.output.open("xb") as stream:
        stream.write(raw)
    print(json.dumps({"path": str(arguments.output), "sha256": hashlib.sha256(raw).hexdigest(),
                      "verification": "metadata_only", "training_executed": False}))


if __name__ == "__main__":
    main()
