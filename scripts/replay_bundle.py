#!/usr/bin/env python3
"""Inert recipe transport, ordinary wheels, and explicitly trusted CPU replay.

No export/import/wheel command installs dependencies or executes bundled Python.
Only `run --trust-repository-source` starts work. It is for operator-reviewed,
repository-owned fixtures; this command is not an untrusted-code sandbox.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import tomllib
import zipfile


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def source_inventory(root: Path) -> list[str]:
    """Explicit application sources; no caches, credentials, datasets or weights."""
    return sorted([str(path.relative_to(root)) for path in (root / "src/modelbuilder").rglob("*.py")]
                  + ["pyproject.toml", "requirements/service.lock", "requirements/backend.lock",
                     "scripts/replay_bundle.py"])


def export_recipe(recipe_path: Path, root: Path, commit: str, output: Path) -> dict:
    from modelbuilder.recipe import export_bundle, pin_source_files
    recipe = json.loads(recipe_path.read_text())
    recipe = pin_source_files(recipe, root, source_inventory(root), commit)
    bundle = export_bundle(recipe, output, source_root=root)
    return {"digest": bundle["digest"], "bundle_digest": bundle["bundle_digest"],
            "source_commit": commit, "source_files": len(bundle["files"]),
            "archive_sha256": sha256(output.read_bytes()), "execution_allowed": False}


def verify_source(root: Path) -> dict:
    from modelbuilder.recipe import export_bundle, import_bundle
    recipe = json.loads((root / "recipe.json").read_text())
    # Export rereads every inventoried file, then import validates the transport.
    return import_bundle(export_bundle(recipe, source_root=root))


def build_wheel(root: Path, destination: Path, kind: str) -> dict:
    """Build a conventional wheel from verified text, without running build hooks.

    Backend and service versions conflict upstream, so separate distributions go
    into separate environments. Both use exactly the same pinned source bytes.
    """
    report = verify_source(root)
    recipe = report["recipe"]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    name = "modelbuilder" if kind == "service" else "modelbuilder_runtime"
    version = project["version"]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version):
        raise ValueError("Replay wheel requires an ordinary numeric version")
    dist_info = f"{name}-{version}.dist-info"
    files = {}
    for item in recipe["source"]["files"]:
        path = item["path"]
        if path.startswith("src/modelbuilder/") and path.endswith(".py"):
            if kind == "service" or path == "src/modelbuilder/__init__.py" or path.startswith("src/modelbuilder/backend/"):
                files[path.removeprefix("src/")] = (root / path).read_bytes()
    if not files or "modelbuilder/backend/adapter.py" not in files:
        raise ValueError("Bundle lacks application source needed by replay")
    requirements = project["dependencies"] if kind == "service" else recipe["environment"]["requirements"]
    from modelbuilder.recipe import validate_pinned_requirements
    requirements = validate_pinned_requirements(requirements)
    metadata = ["Metadata-Version: 2.1", f"Name: {name.replace('_', '-')}", f"Version: {version}",
                "Summary: ModelBuilder verified portable source snapshot", "Requires-Python: >=3.12,<3.13"]
    metadata += ["Requires-Dist: " + requirement for requirement in requirements]
    files[f"{dist_info}/METADATA"] = ("\n".join(metadata) + "\n\n").encode()
    files[f"{dist_info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: modelbuilder-inert-replay\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    if kind == "service":
        files[f"{dist_info}/entry_points.txt"] = (
            "[console_scripts]\nmodelbuilder-service = modelbuilder.api:main\n"
            "modelbuilder-mcp = modelbuilder.mcp_server:main\n").encode()
    rows = []
    for path, data in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rows.append((path, "sha256=" + digest, str(len(data))))
    rows.append((f"{dist_info}/RECORD", "", ""))
    record = io.StringIO(newline="")
    csv.writer(record, lineterminator="\n").writerows(rows)
    files[f"{dist_info}/RECORD"] = record.getvalue().encode()
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
        for path, data in sorted(files.items()):
            member = zipfile.ZipInfo(path, (1980, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = 0o644 << 16
            wheel.writestr(member, data)
    return {"wheel": str(output.resolve()), "sha256": sha256(output.read_bytes()), "kind": kind,
            "source_commit": recipe["source"]["commit"], "build_hooks_executed": False, "files": len(files)}


def runtime_identity(backend: bool = False) -> dict:
    modules = ["modelbuilder", "modelbuilder.backend.adapter", "modelbuilder.backend.fixtures", "zenml", "torch", "numpy"]
    if not backend:
        modules = ["modelbuilder", "modelbuilder.recipe", "modelbuilder.service", "pydantic", "jsonschema"]
    locations = {}
    for name in modules:
        spec = importlib.util.find_spec(name)
        locations[name] = str(Path(spec.origin).resolve()) if spec and spec.origin else None
    pths = {}
    for entry in sys.path:
        if entry and Path(entry).name == "site-packages":
            for path in Path(entry).glob("*.pth"):
                pths[str(path)] = path.read_text()
    result = {"python": platform.python_version(), "executable": sys.executable, "prefix": sys.prefix,
              "base_prefix": sys.base_prefix, "sys_path": list(sys.path), "modules": locations,
              "pythonpath": os.environ.get("PYTHONPATH"), "pth_files": pths,
              "distributions": {dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions()}}
    package = Path(locations["modelbuilder"]).parent
    result["installed_source_sha256"] = {
        "src/modelbuilder/" + str(path.relative_to(package)): sha256(path.read_bytes())
        for path in sorted(package.rglob("*.py"))}
    if backend:
        from zenml.client import Client
        client = Client()
        stack = client.active_stack
        result["store"] = {"stack_id": str(stack.id), "artifact_store_id": str(stack.artifact_store.id),
                           "artifact_store_path": str(stack.artifact_store.path),
                           "metadata_url": str(client.zen_store.config.url),
                           "zenml_config_path": os.environ.get("ZENML_CONFIG_PATH"),
                           "zenml_local_stores_path": os.environ.get("ZENML_LOCAL_STORES_PATH")}
    return result


def run_recipe(source_root: Path, data_dir: Path, backend_python: Path, output: Path,
               *, trusted: bool = False, pretrained_path: Path | None = None, timeout: float = 600) -> dict:
    if not trusted:
        raise ValueError("Execution requires --trust-repository-source after operator review; import is inert")
    if data_dir.exists():
        raise ValueError("Replay requires a new metadata/artifact directory")
    from modelbuilder.backend import ZenMLBackend
    from modelbuilder.models import Actor
    from modelbuilder.recipe import export_bundle
    from modelbuilder.service import OperationService
    report = verify_source(source_root)
    data_dir.mkdir(parents=True)
    backend = ZenMLBackend(data_dir, python_executable=str(backend_python.absolute()), source_root=source_root,
                           pretrained_path=str(pretrained_path.resolve()) if pretrained_path else None)
    service = OperationService(db_path=data_dir / "application.sqlite3", backend=backend, source_root=source_root)
    actor = Actor(id="portable-replay-operator", scopes=frozenset({"read", "modify", "execute"}))
    transcript = []

    def call(operation: str, payload: dict) -> dict:
        result = service.invoke(operation, payload, actor)
        transcript.append({"operation": operation, "result": result})
        return result

    project = call("project.create", {"slug": "portable-cpu", "model_name": report["recipe"]["name"],
                                      "idempotency_key": "portable:project"})
    pid = project["project_id"]
    current = call("recipe.get", {"project_id": pid, "recipe_id": project["recipe_id"]})
    imported = call("recipe.import", {"project_id": pid, "bundle": export_bundle(report["recipe"], source_root=source_root),
                                      "expected_revision": current["revision"],
                                      "idempotency_key": "portable:import"})
    if imported["recipe_id"] != project["recipe_id"]:
        raise AssertionError("Import created an independent recipe instead of updating the model project recipe")
    if imported["import_report"]["execution_allowed"] is not False:
        raise AssertionError("Import unexpectedly authorized execution")
    submitted = call("run.submit", {"project_id": pid, "recipe_id": imported["recipe_id"],
                                    "expected_revision": imported["revision"], "trusted": True,
                                    "idempotency_key": "portable:run"})
    deadline = time.monotonic() + timeout
    payload = {"project_id": pid, "operation_id": submitted["operation_id"]}
    while True:
        status = call("run.status", payload)
        if status["status"] in {"succeeded", "failed", "cancelled"} and not status.get("worker_alive"):
            break
        if time.monotonic() > deadline:
            try:
                cancelled = call("run.cancel", {**payload, "idempotency_key": "portable:timeout-cancel"})
            except Exception as error:
                write_json(output, {"status": "FAIL", "reason": "timeout", "transcript": transcript,
                                    "cancellation_error": {"type": type(error).__name__, "message": str(error)},
                                    "termination_confirmed": False})
                raise TimeoutError("Replay timed out; cancellation failed, so worker termination is unconfirmed") from error
            write_json(output, {"status": "FAIL", "reason": "timeout", "cancellation": cancelled, "transcript": transcript})
            raise TimeoutError("Replay timed out; cancellation result recorded")
        time.sleep(1)
    if status["status"] != "succeeded":
        write_json(output, {"status": "FAIL", "run": status, "transcript": transcript,
                            "logs": call("run.logs", payload)})
        raise RuntimeError("Replay failed; see result and native worker logs")
    # Probe the same interpreter/environment used by SDK and native workers.
    completed = subprocess.run([str(backend_python.absolute()), "-I", str(source_root / "scripts/replay_bundle.py"),
                                "identity", "--backend"], cwd=source_root, env=backend._env(),
                               text=True, capture_output=True, check=True, timeout=90)
    backend_identity = json.loads(completed.stdout)
    service_identity = runtime_identity()
    expected_sources = {item["path"]: item["sha256"] for item in report["recipe"]["source"]["files"]}
    for identity in (service_identity, backend_identity):
        actual_sources = identity["installed_source_sha256"]
        if not actual_sources or any(expected_sources.get(name) != digest for name, digest in actual_sources.items()):
            raise AssertionError("Installed application source differs from the exported snapshot")
    external_hashes = {}
    if pretrained_path:
        for resource in report["recipe"]["resources"]:
            path = pretrained_path / resource["url"].rsplit("/", 1)[-1]
            digest = sha256(path.read_bytes())
            if digest != resource.get("sha256"):
                raise AssertionError("External resource differs from immutable recipe reference")
            external_hashes[resource["id"]] = digest
    job = backend.root / "jobs" / submitted["operation_id"]
    hashes = {str(path.relative_to(job / "outputs")): sha256(path.read_bytes())
              for path in sorted((job / "outputs").rglob("*")) if path.is_file()}
    result = {"status": "PASS", "source_commit": report["recipe"]["source"]["commit"],
              "transport_recipe_digest": report["digest"], "executed_recipe_digest": imported["digest"],
              "source_root": str(source_root.resolve()), "data_dir": str(data_dir.resolve()),
              "import_execution_allowed": False, "service_identity": service_identity,
              "backend_identity": backend_identity, "run": status, "output_sha256": hashes,
              "installed_source_verified": True, "external_resource_sha256": external_hashes,
              "transcript": transcript, "unresolved_dependencies_at_import": report["unresolved_dependencies"]}
    write_json(output, result)
    return result


def compare_results(origin: dict, replay: dict) -> dict:
    """Fail closed on same-environment, shared stores, dependency or output drift."""
    checks = {"both_succeeded": origin["status"] == replay["status"] == "PASS",
              "source_commit_equal": origin["source_commit"] == replay["source_commit"],
              "transport_recipe_digest_equal": origin["transport_recipe_digest"] == replay["transport_recipe_digest"],
              "metrics_equal": origin["run"]["metrics"] == replay["run"]["metrics"],
              "dataset_sha256_equal": origin["run"]["metrics"]["dataset_sha256"] == replay["run"]["metrics"]["dataset_sha256"],
              "output_hashes_equal": bool(origin["output_sha256"]) and origin["output_sha256"] == replay["output_sha256"],
              "external_resource_hashes_equal": origin.get("external_resource_sha256", {}) == replay.get("external_resource_sha256", {}),
              "independent_data_dirs": origin["data_dir"] != replay["data_dir"],
              "independent_run_ids": origin["run"]["backend_run_id"] != replay["run"]["backend_run_id"],
              "independent_source_roots": origin["source_root"] != replay["source_root"]}
    for role in ("service_identity", "backend_identity"):
        left, right = origin[role], replay[role]
        checks[role + "_independent_prefix"] = left["prefix"] != right["prefix"]
        checks[role + "_same_python"] = left["python"] == right["python"]
        checks[role + "_same_distributions"] = left["distributions"] == right["distributions"]
        checks[role + "_no_pythonpath"] = not right["pythonpath"]
        checks[role + "_own_modules"] = all(path and Path(path).is_relative_to(right["prefix"])
                                                  for path in right["modules"].values())
        checks[role + "_no_origin_imports"] = not any(
            entry and (Path(entry).is_relative_to(left["prefix"]) or Path(entry).is_relative_to(origin["source_root"]))
            for entry in right["sys_path"])
        checks[role + "_no_shared_pth"] = not any(
            left["prefix"] in content or origin["source_root"] in content
            for content in right["pth_files"].values())
    left_store, right_store = origin["backend_identity"]["store"], replay["backend_identity"]["store"]
    for name in ("stack_id", "artifact_store_id", "artifact_store_path", "metadata_url", "zenml_config_path"):
        checks["independent_" + name] = left_store[name] != right_store[name]
    return {"status": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
            "source_commit": origin["source_commit"], "recipe_digest": origin["transport_recipe_digest"],
            "dataset_sha256": origin["run"]["metrics"]["dataset_sha256"],
            "origin_backend_run_id": origin["run"]["backend_run_id"], "replay_backend_run_id": replay["run"]["backend_run_id"],
            "limitations": ["Same CPU host and operating system; no cross-platform or GPU claim",
                            "Trusted local source only; no untrusted-code sandbox or redistribution clearance"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("recipe", type=Path)
    export.add_argument("--root", type=Path, required=True)
    export.add_argument("--commit", required=True)
    export.add_argument("--output", type=Path, required=True)
    unpack = commands.add_parser("import")
    unpack.add_argument("bundle", type=Path)
    unpack.add_argument("--destination", type=Path, required=True)
    wheel = commands.add_parser("wheel")
    wheel.add_argument("source_root", type=Path)
    wheel.add_argument("--kind", choices=["service", "backend"], required=True)
    wheel.add_argument("--destination", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("source_root", type=Path)
    run.add_argument("--data-dir", type=Path, required=True)
    run.add_argument("--backend-python", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--pretrained-path", type=Path)
    run.add_argument("--trust-repository-source", action="store_true")
    identity = commands.add_parser("identity")
    identity.add_argument("--backend", action="store_true")
    compare = commands.add_parser("compare")
    compare.add_argument("origin", type=Path)
    compare.add_argument("replay", type=Path)
    compare.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "export":
        result = export_recipe(args.recipe, args.root.resolve(), args.commit, args.output)
    elif args.command == "import":
        from modelbuilder.recipe import import_bundle
        result = import_bundle(args.bundle, args.destination)
    elif args.command == "wheel":
        result = build_wheel(args.source_root.resolve(), args.destination, args.kind)
    elif args.command == "run":
        result = run_recipe(args.source_root.resolve(), args.data_dir.resolve(), args.backend_python,
                            args.output, trusted=args.trust_repository_source, pretrained_path=args.pretrained_path)
        result = {key: result[key] for key in ("status", "source_commit", "transport_recipe_digest", "data_dir")}
    elif args.command == "identity":
        result = runtime_identity(args.backend)
    else:
        result = compare_results(json.loads(args.origin.read_text()), json.loads(args.replay.read_text()))
        write_json(args.output, result)
        if result["status"] != "PASS":
            print(json.dumps(result, indent=2))
            raise SystemExit(1)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
