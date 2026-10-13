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


PACKAGE_DATA = ("src/modelbuilder/backend/components/mlx-darwin-arm64.lock",)


def source_inventory(root: Path) -> list[str]:
    """Explicit application sources; no caches, credentials, datasets or weights."""
    return sorted([str(path.relative_to(root)) for path in (root / "src/modelbuilder").rglob("*.py")]
                  + [str(path.relative_to(root)) for path in (root / "requirements/profiles").glob("*.lock")]
                  + [path for path in PACKAGE_DATA if (root / path).is_file()]
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


def profile_requirements(root: Path, recipe: dict, requirements: list[str], kind: str,
                         target: str | None, backend_variant: str) -> tuple[list[str], dict | None]:
    """Resolve direct dependency versions from an explicitly selected source lock.

    Legacy recipe replay keeps the recipe's exact environment. Native packaging
    selects a target explicitly; neither the host nor a Linux recipe silently
    chooses wheel metadata. Profile files remain inside the verified inventory.
    """
    from modelbuilder.recipe import validate_pinned_requirements
    requirements = validate_pinned_requirements(requirements)
    if target is None:
        if backend_variant != "default":
            raise ValueError("A backend variant requires an explicit target")
        return requirements, None
    if not re.fullmatch(r"[a-z0-9]+-[a-z0-9]+", target):
        raise ValueError("Invalid wheel dependency target")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", backend_variant):
        raise ValueError("Invalid backend dependency variant")
    if kind != "backend" and backend_variant != "default":
        raise ValueError("Backend variants apply only to backend wheels")
    component = kind + ("-" + backend_variant if backend_variant != "default" else "")
    profile = f"requirements/profiles/{component}-{target}.lock"
    entries = {item["path"]: item["sha256"] for item in recipe["source"]["files"]}
    if profile not in entries:
        raise ValueError("Selected target dependency profile is not in the verified source inventory")
    data = (root / profile).read_bytes()
    if sha256(data) != entries[profile]:
        raise ValueError("Selected dependency profile changed after source verification")
    pins = []
    for line in data.decode().splitlines():
        line = line.strip().removesuffix("\\").strip()
        if not line or line.startswith("#") or re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", line):
            continue
        pins.append(line)
    pins = validate_pinned_requirements(pins)

    def key(name):
        return re.sub(r"[-_.]+", "-", name.split("[", 1)[0]).lower()

    versions = {key(pin.split("==", 1)[0]): pin.split("==", 1)[1] for pin in pins}
    resolved = []
    for requirement in requirements:
        name, _ = requirement.split("==", 1)
        if key(name) not in versions:
            raise ValueError(f"Selected dependency profile does not pin {name}")
        # Keep declared extras and direct dependency names; use the reviewed
        # target's exact version, including CPU/CUDA/local-version suffixes.
        resolved.append(name + "==" + versions[key(name)])
    return resolved, {"target": target, "backend_variant": backend_variant,
                      "lock_file": {"path": profile, "sha256": entries[profile]},
                      "requirements": resolved}


def build_wheel(root: Path, destination: Path, kind: str, *, target: str | None = None,
                backend_variant: str = "default") -> dict:
    """Build a conventional wheel from verified text, without running build hooks.

    Backend and service versions conflict upstream, so separate distributions go
    into separate environments. Both use exactly the same pinned source bytes.
    """
    if kind not in {"service", "backend"}:
        raise ValueError("Unknown wheel component")
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
        if path in PACKAGE_DATA:
            files[path.removeprefix("src/")] = (root / path).read_bytes()
        elif path.startswith("src/modelbuilder/") and path.endswith(".py"):
            if (kind == "service" or path in {
                    "src/modelbuilder/__init__.py", "src/modelbuilder/recipe.py",
                    "src/modelbuilder/evaluation.py", "src/modelbuilder/tracking.py",
                    "src/modelbuilder/dataset.py", "src/modelbuilder/dataset_streaming.py",
                    "src/modelbuilder/platform_file_access.py",
                    "src/modelbuilder/parquet_reader.py", "src/modelbuilder/parquet_decode.py",
                    "src/modelbuilder/parquet_stream.py", "src/modelbuilder/parquet_stream_decode.py",
                    "src/modelbuilder/export_packages.py", "src/modelbuilder/execution.py",
                    "src/modelbuilder/conditions.py", "src/modelbuilder/iterations.py",
                    "src/modelbuilder/compute_discovery.py",
                    "src/modelbuilder/models.py", "src/modelbuilder/store.py", "src/modelbuilder/hub_rights.py",
                    "src/modelbuilder/hub_contract.py", "src/modelbuilder/research_context_contract.py",
                    "src/modelbuilder/scientific.py", "src/modelbuilder/scientific_operations.py",
                    "src/modelbuilder/rules.py", "src/modelbuilder/rule_operations.py",
                    "src/modelbuilder/smoke.py", "src/modelbuilder/artifact_rights.py",
                    "src/modelbuilder/generated_artifacts.py",
                    "src/modelbuilder/research_contract.py", "src/modelbuilder/recipe_registry_contract.py",
                    "src/modelbuilder/source_workspace.py", "src/modelbuilder/source_pinning_contract.py"}
                    or path.startswith(("src/modelbuilder/backend/", "src/modelbuilder/remote/"))):
                files[path.removeprefix("src/")] = (root / path).read_bytes()
    if ("modelbuilder/backend/components/mlx_bridge.py" in files
            and "modelbuilder/backend/components/mlx-darwin-arm64.lock" not in files):
        raise ValueError("MLX runtime source requires its inventoried adjacent worker lock")
    if not files or "modelbuilder/backend/adapter.py" not in files:
        raise ValueError("Bundle lacks application source needed by replay")
    requirements = project["dependencies"] if kind == "service" else recipe["environment"]["requirements"]
    requirements, profile = profile_requirements(root, recipe, requirements, kind, target, backend_variant)
    if profile:
        files[f"{dist_info}/modelbuilder-profile.json"] = (json.dumps(profile, indent=2, sort_keys=True) + "\n").encode()
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
            "source_commit": recipe["source"]["commit"], "build_hooks_executed": False, "files": len(files),
            **({"dependency_profile": profile} if profile else {})}


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
        for path in sorted([*package.rglob("*.py"),
                            *(package / path.removeprefix("src/modelbuilder/")
                              for path in PACKAGE_DATA if (package / path.removeprefix("src/modelbuilder/")).is_file())])}
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


PREPARATION_SCHEMA = "modelbuilder.replay-preparation/v1"


def _error_record(error):
    return {"type": type(error).__name__, "code": getattr(error, "code", "replay_error"),
            "message": str(error), "details": getattr(error, "details", {})}


def _replay_result(output, state, action):
    """All replay stages, including pre-submit failures, leave structured evidence."""
    try:
        return action()
    except Exception as error:
        details = getattr(error, "details", {})
        if state.get("submit_attempted") and isinstance(details, dict):
            state.update({key: details[key] for key in ("operation_id", "dispatch_state") if key in details})
        write_json(output, {"status": "FAIL", "stage": state.get("stage", "prepare"),
                            "submitted": state.get("submitted", False), "submit_attempted": state.get("submit_attempted", False),
                            "error": _error_record(error),
                            "transcript": state.get("transcript", []),
                            **{key: state[key] for key in ("preparation", "plan", "operation_id", "dispatch_state", "submission") if key in state},
                            **state.get("failure", {})})
        raise
    finally:
        if state.get("service") is not None:
            state["service"].store.close()


def _session(source_root, data_dir, backend_python, pretrained_path, state, *, trusted=False):
    from modelbuilder.backend import ZenMLBackend
    from modelbuilder.models import Actor
    from modelbuilder.service import OperationService
    backend = ZenMLBackend(data_dir, python_executable=str(backend_python.absolute()), source_root=source_root,
                           pretrained_path=str(pretrained_path.resolve()) if pretrained_path else None)
    service = OperationService(db_path=data_dir / "application.sqlite3", backend=backend, source_root=source_root)
    state["service"] = service
    actor = Actor(id="portable-replay-operator", scopes=frozenset({"read", "modify", "execute"}))
    transcript = state.setdefault("transcript", [])

    def call(operation: str, payload: dict) -> dict:
        try:
            result = service.invoke(operation, payload, actor)
        except Exception as error:
            transcript.append({"operation": operation, "error": _error_record(error)})
            raise
        transcript.append({"operation": operation, "result": result})
        return result

    return {"source_root": source_root, "data_dir": data_dir, "backend_python": backend_python,
            "pretrained_path": pretrained_path, "service": service, "backend": backend, "call": call,
            "trusted": trusted, "state": state, "transcript": transcript}


def _prepare_session(source_root, data_dir, backend_python, pretrained_path, state, *, trusted):
    from modelbuilder.models import new_id
    from modelbuilder.recipe import export_bundle
    if data_dir.exists():
        raise ValueError("Replay preparation requires a new metadata/artifact directory")
    report = verify_source(source_root)
    data_dir.mkdir(parents=True)
    session = _session(source_root, data_dir, backend_python, pretrained_path, state, trusted=trusted)
    call = session["call"]
    project = call("project.create", {"slug": "portable-cpu", "model_name": report["recipe"]["name"],
                                      "idempotency_key": "portable:project"})
    pid = project["project_id"]
    current = call("recipe.get", {"project_id": pid, "recipe_id": project["recipe_id"]})
    raw_bundle = json.dumps(export_bundle(report["recipe"], source_root=source_root), allow_nan=False)
    imported = call("recipe.import", {"project_id": pid, "bundle_json": raw_bundle,
                                      "expected_revision": current["revision"], "idempotency_key": "portable:import"})
    if imported["recipe_id"] != project["recipe_id"] or imported["revision"] != current["revision"] + 1:
        raise AssertionError("Import did not advance the single owned recipe identity")
    if imported["import_report"]["execution_allowed"] is not False:
        raise AssertionError("Import unexpectedly authorized execution")
    original_revision = call("recipe.get", {"project_id": pid, "recipe_id": project["recipe_id"],
                                             "revision": current["revision"]})
    recipes = call("recipe.list", {"project_id": pid})
    if original_revision != current or [item["recipe_id"] for item in recipes["recipes"]] != [project["recipe_id"]]:
        raise AssertionError("Import changed historical recipe content or created another recipe identity")
    execution_recipe = call("recipe.get", {"project_id": pid, "recipe_id": project["recipe_id"]})
    preparation = {"schema_version": PREPARATION_SCHEMA, "preparation_id": new_id(), "revision": 1,
                   "project_id": pid, "recipe_id": project["recipe_id"],
                   "source_root": str(source_root.resolve()), "data_dir": str(data_dir.resolve()),
                   "backend_python": str(backend_python.absolute()),
                   "pretrained_path": str(pretrained_path.resolve()) if pretrained_path else None,
                   "transport_recipe_digest": report["digest"], "source_commit": report["recipe"]["source"]["commit"],
                   "source_kind": "git" if (source_root / ".git").exists() else "imported_snapshot",
                   "initial_recipe": current, "imported_recipe": execution_recipe}
    # This records only replay identity/configuration. It is never license authority.
    session["service"].store.put("replay_preparation", preparation["preparation_id"], pid, preparation)
    state["preparation"] = preparation
    session.update(report=report, project=project, pid=pid, current=current, imported=imported,
                   original_revision=original_revision, recipes=recipes, execution_recipe=execution_recipe,
                   preparation=preparation)
    return session


def _recipe_without_review(recipe):
    from copy import deepcopy
    result = deepcopy(recipe)
    for key in ("digest", "revision", "created_at", "updated_at"):
        result.pop(key, None)
    extensions = result.get("extensions", {})
    for key in ("artifact_rights", "usage_context"):
        extensions.pop(key, None)
    if not extensions:
        result.pop("extensions", None)
    return result


def _load_session(prepared_path, state, *, trusted):
    from modelbuilder.models import ServiceError
    from modelbuilder.store import SQLiteStore
    prepared = json.loads(prepared_path.read_text())["preparation"]
    if prepared.get("schema_version") != PREPARATION_SCHEMA:
        raise ValueError("Unsupported replay preparation")
    data_dir = Path(prepared["data_dir"])
    database = data_dir / "application.sqlite3"
    if not database.is_file():
        raise ValueError("Receiving project policy store is unavailable")
    store = SQLiteStore(database)
    try:
        authoritative = store.get("replay_preparation", prepared["preparation_id"])
        if authoritative != prepared:
            raise ValueError("Preparation differs from the receiving project's recorded identity")
    finally:
        store.close()
    state["preparation"] = prepared
    source_root = Path(prepared["source_root"])
    report = verify_source(source_root)
    if report["digest"] != prepared["transport_recipe_digest"]:
        raise ValueError("Transported recipe changed after replay preparation")
    session = _session(source_root, data_dir, Path(prepared["backend_python"]),
                       Path(prepared["pretrained_path"]) if prepared["pretrained_path"] else None, state, trusted=trusted)
    call, pid = session["call"], prepared["project_id"]
    project = call("project.get", {"project_id": pid})
    current = call("recipe.get", {"project_id": pid, "recipe_id": prepared["recipe_id"]})
    if project["recipe_id"] != prepared["recipe_id"] or _recipe_without_review(current) != _recipe_without_review(prepared["imported_recipe"]):
        raise ServiceError("replay_recipe_changed", "Prepared source, graph or execution settings changed; prepare a new replay", status_code=409)
    binding = project.get("source_binding")
    if binding:
        # The operator's explicit trust applies only to this recorded root. The
        # source resolver rechecks the exact explicitly bound source kind/identity.
        from modelbuilder.source_workspace import SourceWorkspaces
        session["service"].source_workspaces = SourceWorkspaces({binding["alias"]: {
            "path": str(source_root), "kind": binding.get("source_kind", "git"),
            "trusted": trusted, "project_ids": [pid]}})
        # Restore local evidence aliases only from receiving-store authority for
        # this exact already-bound root. Native source and Hub folder aliases may
        # differ. Neither bundle JSON nor the receipt can supply policy authority.
        hub = session["service"].hub
        aliases = {binding["alias"]: str(source_root)}
        for policy in hub.policy_snapshot(pid).values():
            origin = policy.get("source_origin", {})
            if (origin.get("source_kind") == "imported_snapshot"
                    and origin.get("repository_id") == binding["repository_id"]):
                evidence = hub._get("hub_license_evidence", policy["evidence_id"], pid)
                if evidence["evidence"].get("source_origin") == origin:
                    aliases[evidence["source_alias"]] = str(source_root)
        hub.publication_roots = {pid: aliases}
    original = call("recipe.get", {"project_id": pid, "recipe_id": prepared["recipe_id"],
                                   "revision": prepared["initial_recipe"]["revision"]})
    recipes = call("recipe.list", {"project_id": pid})
    session.update(report=report, project=project, pid=pid, current=prepared["initial_recipe"],
                   imported=prepared["imported_recipe"], original_revision=original, recipes=recipes,
                   execution_recipe=current, preparation=prepared)
    return session


def _plan_session(session):
    state = session["state"]
    state["stage"] = "plan"
    recipe = session["execution_recipe"]
    from modelbuilder.models import ServiceError
    try:
        plan = session["call"]("run.plan", {"project_id": session["pid"], "recipe_id": recipe["recipe_id"],
                                           "trusted": session["trusted"]})
    except ServiceError as error:
        plan = {"executable": False, "blocked_reasons": [_error_record(error)]}
    latest = session["call"]("recipe.get", {"project_id": session["pid"], "recipe_id": recipe["recipe_id"]})
    if (latest["revision"], latest["digest"]) != (recipe["revision"], recipe["digest"]):
        plan = {"executable": False, "blocked_reasons": [{"code": "replay_recipe_changed",
                "message": "Recipe changed while planning; inspect the current revision again", "details": {}}]}
    state["plan"] = plan
    return {"status": "PREPARED" if plan.get("executable") is True else "BLOCKED", "execution_allowed": False,
            "submitted": False, "preparation": session["preparation"], "plan": plan,
            "source_review_setup": {"source_alias": "portable-snapshot",
                "source_roots": {"portable-snapshot": {"path": str(session["source_root"]),
                    "kind": session["preparation"].get("source_kind", "git"), "trusted": False,
                    "project_ids": [session["pid"]]}},
                "hub": {"publication_roots": {session["pid"]: {"portable-snapshot": str(session["source_root"])}}},
                "authority": "Operator configuration guidance only; binding, trust and rights require separate explicit decisions"},
            "execution_revision": recipe["revision"], "execution_digest": recipe["digest"],
            "transcript": session["transcript"]}


def prepare_recipe(source_root, data_dir, backend_python, output, *, trusted=False, pretrained_path=None):
    state = {"stage": "prepare"}
    def action():
        result = _plan_session(_prepare_session(source_root, data_dir, backend_python, pretrained_path, state, trusted=trusted))
        write_json(output, result)
        return result
    return _replay_result(output, state, action)


def plan_prepared(prepared_path, output, *, trusted=False):
    state = {"stage": "load_preparation"}
    def action():
        result = _plan_session(_load_session(prepared_path, state, trusted=trusted))
        write_json(output, result)
        return result
    return _replay_result(output, state, action)


def _require_plan(session):
    from modelbuilder.models import ServiceError
    result = _plan_session(session)
    if result["plan"].get("executable") is not True:
        reasons = result["plan"].get("blocked_reasons", [])
        first = reasons[0] if reasons else {}
        raise ServiceError(first.get("code", "replay_blocked"), first.get("message", "Replay preflight is blocked"),
                           {"blocked_reasons": reasons}, 409)


def run_recipe(source_root: Path, data_dir: Path, backend_python: Path, output: Path,
               *, trusted: bool = False, pretrained_path: Path | None = None, timeout: float = 600) -> dict:
    state = {"stage": "prepare"}
    def action():
        if not trusted:
            raise ValueError("Execution requires --trust-repository-source after operator review; import is inert")
        session = _prepare_session(source_root, data_dir, backend_python, pretrained_path, state, trusted=trusted)
        _require_plan(session)
        return _execute_session(session, output, timeout)
    return _replay_result(output, state, action)


def run_prepared(prepared_path, output, *, expected_revision, expected_digest, trusted=False, timeout=600):
    state = {"stage": "load_preparation"}
    def action():
        from modelbuilder.models import ServiceError
        if not trusted:
            raise ValueError("Execution requires --trust-repository-source after operator review; preparation is inert")
        session = _load_session(prepared_path, state, trusted=trusted)
        recipe = session["execution_recipe"]
        if recipe["revision"] != expected_revision or recipe["digest"] != expected_digest:
            raise ServiceError("replay_recipe_changed", "Reviewed recipe revision or digest changed; plan again", status_code=409)
        _require_plan(session)
        return _execute_session(session, output, timeout)
    return _replay_result(output, state, action)


def _execute_session(session, output, timeout):
    source_root, data_dir, backend_python, pretrained_path = (session[key] for key in
        ("source_root", "data_dir", "backend_python", "pretrained_path"))
    report, project, pid, current, imported, original_revision, recipes, execution_recipe = (session[key] for key in
        ("report", "project", "pid", "current", "imported", "original_revision", "recipes", "execution_recipe"))
    call, backend, transcript, state = (session[key] for key in ("call", "backend", "transcript", "state"))
    key = "portable:run:" + session["preparation"]["preparation_id"] + ":" + str(execution_recipe["revision"]) + ":" + execution_recipe["digest"]
    prior = session["service"].store.connection.execute(
        "SELECT result FROM idempotency WHERE actor_id=? AND key=?", ("portable-replay-operator", key)).fetchone()
    submission = {"idempotency_key": key, "existing_operation_recorded_before_submit": prior is not None,
                  "retry_semantics": "same_preparation_revision_digest_reconciles_existing_operation"}
    state.update(stage="submit", submit_attempted=True, submitted=None, submission=submission)
    submitted = call("run.submit", {"project_id": pid, "recipe_id": imported["recipe_id"],
                                    "expected_revision": execution_recipe["revision"], "trusted": True,
                                    "idempotency_key": key})
    state.update(stage="poll", submitted=True, operation_id=submitted["operation_id"])
    deadline = time.monotonic() + timeout
    payload = {"project_id": pid, "operation_id": submitted["operation_id"]}
    while True:
        status = call("run.status", payload)
        if status["status"] in {"succeeded", "failed", "cancelled"} and not status.get("worker_alive"):
            break
        if time.monotonic() > deadline:
            try:
                cancelled = call("run.cancel", {**payload, "idempotency_key": "portable:timeout-cancel:" + submitted["operation_id"]})
            except Exception as error:
                state["failure"] = {"status": "FAIL", "reason": "timeout", "transcript": transcript,
                                    "cancellation_error": {"type": type(error).__name__, "message": str(error)},
                                    "termination_confirmed": False}
                raise TimeoutError("Replay timed out; cancellation failed, so worker termination is unconfirmed") from error
            state["failure"] = {"status": "FAIL", "reason": "timeout", "cancellation": cancelled, "transcript": transcript}
            raise TimeoutError("Replay timed out; cancellation result recorded")
        time.sleep(1)
    if status["status"] != "succeeded":
        state["failure"] = {"status": "FAIL", "run": status, "transcript": transcript,
                            "logs": call("run.logs", payload)}
        raise RuntimeError("Replay failed; see result and native worker logs")
    state["stage"] = "verify_results"
    recipe_identity = {"project_id": pid, "project_recipe_id": project["recipe_id"],
                       "imported_recipe_id": imported["recipe_id"], "listed_recipe_ids": [r["recipe_id"] for r in recipes["recipes"]],
                       "initial_revision": current["revision"], "imported_revision": imported["revision"],
                       "original_revision_unchanged": original_revision == current,
                       "run_recipe_id": status.get("recipe_id"), "run_recipe_revision": status.get("recipe_revision"),
                       "run_recipe_digest": status.get("recipe_digest"), "native_executed_recipe_digest": status.get("executed_recipe_digest"),
                       "imported_recipe_digest": imported["digest"],
                       "execution_revision": execution_recipe["revision"], "execution_digest": execution_recipe["digest"]}
    if not valid_recipe_identity(recipe_identity):
        raise AssertionError("Run provenance does not identify the single owned recipe and its exact imported revision")
    # Keep the probe read-only: -I ignores PYTHONDONTWRITEBYTECODE from _env().
    completed = subprocess.run([str(backend_python.absolute()), "-I", "-B", str(source_root / "scripts/replay_bundle.py"),
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
              "transport_recipe_digest": report["digest"], "executed_recipe_digest": execution_recipe["digest"],
              "source_root": str(source_root.resolve()), "data_dir": str(data_dir.resolve()),
              "import_execution_allowed": False, "import_transport": "bundle_json", "service_identity": service_identity,
              "backend_identity": backend_identity, "run": status, "output_sha256": hashes,
              "installed_source_verified": True, "external_resource_sha256": external_hashes,
              "recipe_identity": recipe_identity, "preparation": session["preparation"], "submission": submission,
              "transcript": transcript, "unresolved_dependencies_at_import": report["unresolved_dependencies"]}
    write_json(output, result)
    return result


def valid_recipe_identity(identity: dict) -> bool:
    recipe_id = identity.get("project_recipe_id")
    return bool(recipe_id and identity.get("imported_recipe_id") == recipe_id
                and identity.get("listed_recipe_ids") == [recipe_id]
                and identity.get("run_recipe_id") == recipe_id
                and identity.get("original_revision_unchanged") is True
                and type(identity.get("initial_revision")) is int
                and identity.get("imported_revision") == identity["initial_revision"] + 1
                and identity.get("run_recipe_revision") == identity.get("execution_revision", identity["imported_revision"])
                and identity.get("run_recipe_revision", 0) >= identity["imported_revision"]
                and identity.get("imported_recipe_digest")
                and identity.get("run_recipe_digest") == identity.get("execution_digest", identity["imported_recipe_digest"])
                and identity.get("native_executed_recipe_digest") == identity.get("execution_digest", identity["imported_recipe_digest"]))


def comparable_metrics(record: dict) -> dict:
    """Keep all measurement fields, separating only native store identifiers."""
    metrics = dict(record["run"]["metrics"])
    catalog = metrics.get("evaluation_catalog")
    if isinstance(catalog, list) and all(isinstance(row, dict) for row in catalog):
        metrics["evaluation_catalog"] = [
            {key: value for key, value in row.items()
             if key not in {"artifact_id", "backend_run_id", "model_version_ref"}}
            for row in catalog]
    return metrics


def valid_evaluation_catalog(record: dict) -> bool:
    """Require catalog references to resolve inside this run's native evidence."""
    run = record["run"]
    if "evaluation_catalog" not in run["metrics"]:
        return True  # Historical evidence predating the catalog remains readable.
    catalog = run["metrics"]["evaluation_catalog"]
    if not isinstance(catalog, list) or not isinstance(run.get("evaluations"), list) or len(catalog) != len(run["evaluations"]):
        return False
    seen = set()
    for row in catalog:
        if not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key]
                                           for key in ("artifact_id", "backend_run_id",
                                                       "node_id", "output_port", "metric", "evaluation_digest")):
            return False
        identity = (row["node_id"], row["output_port"], row["metric"], row["evaluation_digest"])
        if identity in seen or row["backend_run_id"] != run.get("backend_run_id"):
            return False
        seen.add(identity)
        artifacts = [item for item in run.get("artifacts", [])
                     if item.get("artifact_id") == row["artifact_id"]
                     and item.get("backend_run_id") == row["backend_run_id"]
                     and item.get("node_id") == row["node_id"]
                     and item.get("port") == row["output_port"]]
        evaluations = [item for item in run.get("evaluations", [])
                       if all(item.get(key) == value for key, value in row.items())]
        if len(artifacts) != 1 or len(evaluations) != 1:
            return False
        version = row.get("model_version_ref")
        verification = row.get("checkpoint_verification")
        if version is None:
            # Status may truthfully defer checkpoint attribution to comparison.
            # Native evaluation-artifact ownership above must still be proven.
            if ("model_version_ref" not in row or not isinstance(verification, dict)
                    or verification.get("verified") is not False):
                return False
            continue
        if not isinstance(version, str) or not version or (isinstance(verification, dict) and verification.get("verified") is False):
            return False
        bindings = evaluations[0].get("checkpoint_bindings", [])
        if not isinstance(bindings, list) or not all(isinstance(item, dict) for item in bindings):
            return False
        owners = ({item.get("producer_model_version_ref") for item in bindings}
                  if bindings else {run.get("model_version_ref")})
        if owners != {version}:
            return False
    return True


def comparable_evaluations(record: dict) -> list:
    # Keep complete records and checkpoint bindings. Only these top-level native
    # IDs may differ in fresh independent stores; unknown future fields stay strict.
    return [{key: value for key, value in row.items()
             if key not in {"artifact_id", "backend_run_id", "model_version_ref"}}
            for row in record["run"].get("evaluations", [])]


def independent_catalog_references(origin: dict, replay: dict) -> bool:
    for field in ("artifact_id", "model_version_ref"):
        left = {row[field] for row in origin["run"]["metrics"].get("evaluation_catalog", []) if row[field] is not None}
        right = {row[field] for row in replay["run"]["metrics"].get("evaluation_catalog", []) if row[field] is not None}
        if left & right:
            return False
    return True


def compare_results(origin: dict, replay: dict) -> dict:
    """Fail closed on same-environment, shared stores, dependency or output drift."""
    checks = {"both_succeeded": origin["status"] == replay["status"] == "PASS",
              "source_commit_equal": origin["source_commit"] == replay["source_commit"],
              "transport_recipe_digest_equal": origin["transport_recipe_digest"] == replay["transport_recipe_digest"],
              "metrics_equal": comparable_metrics(origin) == comparable_metrics(replay),
              "dataset_sha256_equal": origin["run"]["metrics"]["dataset_sha256"] == replay["run"]["metrics"]["dataset_sha256"],
              "output_hashes_equal": bool(origin["output_sha256"]) and origin["output_sha256"] == replay["output_sha256"],
              "external_resource_hashes_equal": origin.get("external_resource_sha256", {}) == replay.get("external_resource_sha256", {}),
              "independent_data_dirs": origin["data_dir"] != replay["data_dir"],
              "independent_run_ids": origin["run"]["backend_run_id"] != replay["run"]["backend_run_id"],
              "independent_source_roots": origin["source_root"] != replay["source_root"]}
    for role, record in (("origin", origin), ("replay", replay)):
        checks[role + "_single_owned_recipe_identity"] = valid_recipe_identity(record.get("recipe_identity", {}))
        checks[role + "_evaluation_catalog_owned"] = valid_evaluation_catalog(record)
    catalogs_valid = checks["origin_evaluation_catalog_owned"] and checks["replay_evaluation_catalog_owned"]
    checks["evaluation_records_equal"] = catalogs_valid and comparable_evaluations(origin) == comparable_evaluations(replay)
    checks["independent_catalog_references"] = catalogs_valid and independent_catalog_references(origin, replay)
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
    wheel.add_argument("--target", help="Explicit native target; resolve metadata from its inventoried hash-lock")
    wheel.add_argument("--backend-variant", default="default", help="Reviewed backend profile variant, e.g. cuda-cu128")
    run = commands.add_parser("run")
    run.add_argument("source_root", type=Path)
    run.add_argument("--data-dir", type=Path, required=True)
    run.add_argument("--backend-python", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--pretrained-path", type=Path)
    run.add_argument("--trust-repository-source", action="store_true")
    prepare = commands.add_parser("prepare", help="Import and plan; never submit or approve rights")
    for target in (prepare,):
        target.add_argument("source_root", type=Path)
        target.add_argument("--data-dir", type=Path, required=True)
        target.add_argument("--backend-python", type=Path, required=True)
        target.add_argument("--output", type=Path, required=True)
        target.add_argument("--pretrained-path", type=Path)
        target.add_argument("--trust-repository-source", action="store_true")
    for name in ("plan", "run-prepared"):
        target = commands.add_parser(name)
        target.add_argument("prepared", type=Path)
        target.add_argument("--output", type=Path, required=True)
        target.add_argument("--trust-repository-source", action="store_true")
        if name == "run-prepared":
            target.add_argument("--expected-revision", type=int, required=True)
            target.add_argument("--expected-digest", required=True)
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
        result = build_wheel(args.source_root.resolve(), args.destination, args.kind,
                             target=args.target, backend_variant=args.backend_variant)
    elif args.command == "run":
        result = run_recipe(args.source_root.resolve(), args.data_dir.resolve(), args.backend_python,
                            args.output, trusted=args.trust_repository_source, pretrained_path=args.pretrained_path)
        result = {key: result[key] for key in ("status", "source_commit", "transport_recipe_digest", "data_dir")}
    elif args.command == "prepare":
        result = prepare_recipe(args.source_root.resolve(), args.data_dir.resolve(), args.backend_python,
                                args.output, trusted=args.trust_repository_source, pretrained_path=args.pretrained_path)
    elif args.command == "plan":
        result = plan_prepared(args.prepared, args.output, trusted=args.trust_repository_source)
    elif args.command == "run-prepared":
        result = run_prepared(args.prepared, args.output, expected_revision=args.expected_revision,
                              expected_digest=args.expected_digest, trusted=args.trust_repository_source)
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
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "FAIL", "error": _error_record(error)}, indent=2), file=sys.stderr)
        raise SystemExit(1) from None
