"""Public operation adapter. Service interpreter never imports ZenML or Torch.

The journal holds process ownership/correlation only. Every run/artifact/model
read is freshly reconciled against the native ZenML store.
"""
from __future__ import annotations
import copy
import fcntl
import hashlib
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

from .common import alive, now, process_group_alive, recipe_provenance, read_json, write_json

BUILTINS = {"dataset.import", "dataset.synthetic", "dataset.validate", "model.initialize", "model.train", "model.evaluate", "model.checkpoint", "model.export", "model.compare"}


class BackendError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


class ZenMLBackend:
    def __init__(self, data_dir: str | Path, python_executable: str | None = None,
                 trusted_code_roots: list[str] | None = None, dependency_paths: dict[str, str] | None = None,
                 source_root: str | Path | None = None, pretrained_path: str | Path | None = None):
        self.root = Path(data_dir).resolve() / "backend"
        self.root.mkdir(parents=True, exist_ok=True)
        package_root = Path(__file__).resolve().parents[2]
        default_source = package_root.parent if package_root.name == "src" else Path.cwd()
        repository = Path(source_root or os.environ.get("MODELBUILDER_SOURCE_ROOT", default_source)).resolve()
        self.python = python_executable or os.environ.get("MODELBUILDER_BACKEND_PYTHON", str(repository / ".venv-backend/bin/python"))
        self.repository = repository
        self.package_root = package_root
        self.pretrained_path = Path(pretrained_path or repository / ".cache/models/bert-tiny").resolve()
        self.trusted_code_roots = [str(Path(x).resolve()) for x in (trusted_code_roots or [])]
        self.dependency_paths = {key: str(Path(value).resolve()) for key, value in (dependency_paths or {}).items()}

    def _env(self) -> dict:
        allowed = {"PATH", "LANG", "LC_ALL", "TZ", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
                   "https_proxy", "http_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"}
        env = {key: value for key, value in os.environ.items() if key in allowed}
        env["HOME"] = str(self.root / "home")
        Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
        env.update(PYTHONPATH=str(self.package_root),
                   MODELBUILDER_PRETRAINED_PATH=str(self.pretrained_path),
                   ZENML_CONFIG_PATH=str(self.root / "zenml-config"), ZENML_LOCAL_STORES_PATH=str(self.root / "zenml-stores"),
                   ZENML_ANALYTICS_OPT_IN="false", ZENML_ENABLE_REPO_INIT_WARNINGS="false", ZENML_LOGGING_VERBOSITY="ERROR",
                   ZENML_DEBUG="false", TOKENIZERS_PARALLELISM="false", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                   OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", PYTHONUNBUFFERED="1")
        if self.package_root.name != "src":
            env.pop("PYTHONPATH", None)
        return env

    def _rpc(self, operation: str, payload: dict) -> dict:
        if not Path(self.python).is_file():
            raise BackendError("capability_unavailable", "Pinned ZenML backend environment is not installed; run scripts/bootstrap.sh --backend")
        request_id = str(uuid.uuid4())
        path = self.root / "requests" / f"{request_id}.json"
        result = path.with_suffix(".result.json")
        write_json(path, {"operation": operation, "payload": payload, "root": str(self.root), "result": str(result)})
        with path.with_suffix(".log").open("w") as log:
            try:
                completed = subprocess.run([self.python, "-m", "modelbuilder.backend.sdk", str(path)], env=self._env(),
                                           cwd=self.repository, stdout=log, stderr=subprocess.STDOUT, timeout=90)
            except subprocess.TimeoutExpired as error:
                raise BackendError("backend_timeout", "ZenML read did not complete", {"request_id": request_id}) from error
        response = read_json(result)
        if isinstance(response.get("error"), dict):
            error = response["error"]
            raise BackendError(error.get("code", "backend_error"), error["message"], error.get("details"))
        if completed.returncode or not response:
            raise BackendError("backend_error", "Pinned ZenML subprocess failed", {"request_id": request_id, "exit_code": completed.returncode})
        return response

    def _job(self, payload: dict) -> tuple[Path, dict]:
        operation_id = payload.get("operation_id")
        if not operation_id and payload.get("backend_run_id"):
            for request in (self.root / "jobs").glob("*/request.json"):
                if read_json(request.parent / "native.json").get("backend_run_id") == payload["backend_run_id"]:
                    operation_id = request.parent.name
                    break
        try:
            operation_id = str(uuid.UUID(str(operation_id)))
        except ValueError as error:
            raise BackendError("not_found", "Known operation_id or backend_run_id is required") from error
        path = self.root / "jobs" / operation_id
        request = read_json(path / "request.json")
        if not request or request["project_id"] != payload.get("project_id"):
            raise BackendError("not_found", "Operation does not belong to this project")
        return path, request

    def _validate_execution(self, payload: dict) -> dict:
        if payload.get("target", "local-cpu") != "local-cpu":
            raise BackendError("capability_unavailable", "Remote execution and spend are disabled")
        if payload.get("trusted") is not True:
            raise BackendError("untrusted_execution_blocked", "Explicit trust consent is required for repository-owned fixtures; untrusted code has no verified isolation boundary")
        recipe = payload.get("recipe", {})
        if payload.get("project_recipe_id") and recipe.get("recipe_id") != payload["project_recipe_id"]:
            raise BackendError("conflict", "Execution must use the project's single canonical recipe identity")
        if payload.get("project_id") and recipe.get("project_id") and recipe["project_id"] != payload["project_id"]:
            raise BackendError("conflict", "Recipe and execution project identities differ")
        if recipe.get("digest") and recipe["digest"] != recipe_provenance(recipe)["recipe_digest"]:
            raise BackendError("dependency_mismatch", "Declared recipe digest differs from executed immutable revision")
        if any(node.get("kind") == "code.python" for node in recipe.get("nodes", [])) and not self.trusted_code_roots:
            raise BackendError("untrusted_execution_blocked", "Arbitrary Python requires an operator-configured trusted source root")
        source = recipe.get("source", {})
        commit = source.get("commit", "")
        if not commit or set(commit) == {"0"}:
            raise BackendError("dependency_mismatch", "Execution requires a real immutable source commit")
        declared = source.get("files", [])
        if not declared:
            raise BackendError("dependency_mismatch", "Execution requires SHA256-pinned source files")
        for reference in declared:
            path = (self.repository / reference["path"]).resolve()
            if not path.is_relative_to(self.repository) or not path.is_file():
                raise BackendError("dependency_mismatch", "Declared source file is unavailable")
            if hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
                raise BackendError("dependency_mismatch", "Declared source file has changed", {"path": reference["path"]})
        fixture_ref = next((r for r in declared if r["path"] == "src/modelbuilder/backend/fixtures.py"), None)
        if any(n.get("kind") in BUILTINS for n in recipe.get("nodes", [])):
            actual_fixture = Path(__file__).with_name("fixtures.py")
            if not fixture_ref or hashlib.sha256(actual_fixture.read_bytes()).hexdigest() != fixture_ref["sha256"]:
                raise BackendError("dependency_mismatch", "Installed builtin fixture differs from declared pinned workload source")
        if (self.repository / ".git").exists():
            probe = subprocess.run(["git", "cat-file", "-e", commit + "^{commit}"], cwd=self.repository, capture_output=True)
            if probe.returncode:
                raise BackendError("dependency_mismatch", "Declared source commit is unavailable locally")
            for reference in declared:
                blob = subprocess.run(["git", "show", f"{commit}:{reference['path']}"], cwd=self.repository, capture_output=True)
                if blob.returncode or hashlib.sha256(blob.stdout).hexdigest() != reference["sha256"]:
                    raise BackendError("dependency_mismatch", "Source bytes do not match the declared Git commit", {"path": reference["path"]})
        for dependency in recipe.get("resources", []) + recipe.get("external_dependencies", []):
            identifier = dependency["id"]
            path = self.dependency_paths.get(identifier)
            if path:
                if not Path(path).is_file() or hashlib.sha256(Path(path).read_bytes()).hexdigest() != dependency.get("sha256"):
                    raise BackendError("dependency_mismatch", "Configured dependency bytes do not match manifest", {"id": identifier})
            elif dependency.get("revision") == "6f75de8b60a9f8a2fdf7b69cbd86d9e64bcb3837":
                pinned = {"config.json": "cf3179684f7c360dd6185962d480e7d426ceb8fe49f5db7b64bdc15dba0b8798",
                          "vocab.txt": "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3",
                          "pytorch_model.bin": "dab2c2bddcfb48ea430ef63fd76d46d67d704487844d967256a50dd7d7fd0a66"}
                name = dependency.get("url", "").rsplit("/", 1)[-1]
                expected_url = f"https://huggingface.co/prajjwal1/bert-tiny/resolve/{dependency['revision']}/{name}"
                if name not in pinned or dependency["url"] != expected_url or dependency.get("sha256") != pinned[name]:
                    raise BackendError("dependency_mismatch", "Unrecognized pretrained dependency identity")
                path = self.pretrained_path / name
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != dependency["sha256"]:
                    raise BackendError("dependency_mismatch", "Pinned pretrained dependency is absent or changed")
            else:
                raise BackendError("dependency_unavailable", "External dependency must be explicitly resolved and verified before execution", {"id": identifier})
        nodes = recipe.get("nodes", [])
        if not nodes:
            raise BackendError("validation_error", "Execution requires a nonempty recipe DAG")
        for node in nodes:
            kind, reference = node["kind"], node.get("implementation_ref")
            if node.get("resource_request", {}).get("gpu", 0):
                raise BackendError("capability_unavailable", "local-cpu cannot satisfy a GPU resource request")
            parameters = node.get("parameters", {})
            if "steps" in parameters and (type(parameters["steps"]) is not int or not 1 <= parameters["steps"] <= 100000):
                raise BackendError("validation_error", "Fixture steps must be an integer between 1 and 100000")
            kl = parameters.get("kl")
            if kind == "model.train" and kl is not None:
                initial_edges = [edge for edge in recipe.get("edges", []) if edge["target_node"] == node["id"] and edge["target_port"] == "initial"]
                if (len(initial_edges) != 1 or kl.get("reference_model_ref") != initial_edges[0]["source_node"]
                        or kl.get("applies_to") != "logits"):
                    raise BackendError("capability_unavailable", "Fixture KL supports logits anchored to the actual connected initial model only")
            if kind == "dataset.import" and parameters.get("resource_id") not in self.dependency_paths:
                raise BackendError("dependency_unavailable", "Dataset import requires an explicitly resolved content-addressed JSON dependency")
            if kind == "code.python":
                if not isinstance(reference, dict) or not self.trusted_code_roots:
                    raise BackendError("untrusted_execution_blocked", "Arbitrary Python requires an operator-configured trusted source root; untrusted isolation is unavailable")
                path = (self.repository / reference.get("path", "")).resolve()
                if not any(path.is_relative_to(Path(root)) for root in self.trusted_code_roots):
                    raise BackendError("untrusted_execution_blocked", "Python source lies outside explicitly trusted roots")
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != reference.get("sha256"):
                    raise BackendError("dependency_mismatch", "Trusted Python source SHA256 does not match")
            elif kind not in BUILTINS or reference != f"builtin:{kind}":
                raise BackendError("capability_unavailable", f"Executable node implementation is unsupported: {kind}")
        return recipe

    def invoke(self, operation: str, payload: dict, actor=None) -> dict:
        if operation == "execution.targets":
            return {"targets": [{"id": "local-cpu", "available": Path(self.python).is_file(), "backend": "zenml", "version": "0.97.0",
                                 "cancel": "supervised_process_termination", "exact_resume": True, "untrusted_isolation": False}],
                    "remote_enabled": False}
        if operation == "run.plan":
            recipe = payload.get("recipe", {})
            warnings = []
            if not recipe.get("source", {}).get("files") or set(recipe.get("source", {}).get("commit", "0")) == {"0"}:
                warnings.append("Source dependencies are unresolved; execution will fail closed until pinned")
            if any(n.get("kind") == "code.python" for n in recipe.get("nodes", [])) and not self.trusted_code_roots:
                warnings.append("Untrusted Python isolation is unavailable")
            if any(n.get("resource_request", {}).get("gpu", 0) for n in recipe.get("nodes", [])):
                warnings.append("Requested GPU is unavailable on local-cpu")
            return {"project_id": payload["project_id"], "target": "local-cpu", "backend": "zenml", "backend_version": "0.97.0",
                    "node_ids": [node["id"] for node in recipe.get("nodes", [])], "executable": not warnings, "warnings": warnings,
                    "dependency_verification": "required_at_submission",
                    "isolation": "trusted_local_process", "estimated_cost": {"currency": "USD", "amount": 0, "scope": "local provider charge only"}}
        if operation == "run.submit":
            return self.submit(payload)
        if operation == "run.resume":
            return self.resume(payload)
        if operation in {"run.status", "run.logs", "run.artifacts", "run.cancel"}:
            job, request = self._job(payload)
            if operation == "run.logs":
                content = (job / "worker.log").read_text(errors="replace") if (job / "worker.log").exists() else ""
                cursor = int(payload.get("cursor") or 0)
                return {"operation_id": job.name, "logs": content[cursor:cursor + 100000], "cursor": str(min(len(content), cursor + 100000))}
            if operation == "run.cancel":
                return self.cancel(job, request)
            return self._rpc(operation, {**payload, "job_dir": str(job)})
        if operation == "run.compare":
            run_ids = payload.get("backend_run_ids", payload.get("run_ids", []))
            operation_ids = payload.get("operation_ids", [])
            jobs = [self._job({"project_id": payload["project_id"], "backend_run_id": x})[0] for x in run_ids]
            jobs += [self._job({"project_id": payload["project_id"], "operation_id": x})[0] for x in operation_ids]
            return self._rpc(operation, {**payload, "job_dirs": [str(p) for p in jobs]})
        if operation == "model.checkpoint.get" and (payload.get("operation_id") or payload.get("backend_run_id")):
            job, _ = self._job(payload)
            return self._rpc(operation, {**payload, "job_dir": str(job)})
        if operation.startswith("model."):
            return self._rpc(operation, payload)
        raise BackendError("capability_unavailable", f"Backend operation is unavailable: {operation}")

    def submit(self, payload: dict) -> dict:
        self._validate_execution(payload)
        if not Path(self.python).is_file():
            raise BackendError("capability_unavailable", "Backend interpreter is not installed")
        with (self.root / "initialize.lock").open("a") as initialization:
            fcntl.flock(initialization.fileno(), fcntl.LOCK_EX)
            self._rpc("backend.validate", {"environment": payload["recipe"].get("environment", {})})
        operation_id = str(uuid.UUID(payload.get("operation_id") or str(uuid.uuid4())))
        job = self.root / "jobs" / operation_id
        job.mkdir(parents=True, exist_ok=True)
        with (job / "launch.lock").open("a") as lease:
            try:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return self._rpc("run.status", {**payload, "job_dir": str(job)})
            if (job / "request.json").exists():
                request = read_json(job / "request.json")
                if (request["project_id"] != payload["project_id"] or request.get("idempotency_key") != payload.get("idempotency_key")
                        or request["recipe"] != payload["recipe"]):
                    raise BackendError("conflict", "Operation identity reused with conflicting request")
                state = read_json(job / "state.json")
                if (read_json(job / "native.json") or state.get("worker_pid") or
                        alive(state.get("supervisor_pid"), state.get("supervisor_identity"))):
                    return self._rpc("run.status", {**payload, "job_dir": str(job)})
                # No process owns the inherited launch lease and no worker/native
                # identity exists: the previous service died before a real launch.
            else:
                request = {**payload, "operation_id": operation_id, "root": str(self.root), "job_dir": str(job),
                           "run_name": f"modelbuilder-{operation_id}", "dependency_paths": self.dependency_paths, "source_root": str(self.repository),
                           "submitted_at": now(), "trusted_code_roots": self.trusted_code_roots,
                           "executed_recipe_digest": recipe_provenance(payload["recipe"])["recipe_digest"]}
                write_json(job / "request.json", request)
            write_json(job / "state.json", {"supervisor_state": "starting", "submitted_at": request["submitted_at"]})
            try:
                with (job / "supervisor.log").open("a") as log:
                    process = subprocess.Popen([self.python, "-m", "modelbuilder.backend.supervisor", str(job), str(lease.fileno())],
                                               cwd=self.repository, env=self._env(), stdout=log, stderr=subprocess.STDOUT,
                                               start_new_session=True, stdin=subprocess.DEVNULL, pass_fds=(lease.fileno(),))
            except OSError as error:
                write_json(job / "state.json", {"supervisor_state": "submission_failed", "submission_error": str(error),
                                                 "failed_at": now(), "retryable": True})
                raise BackendError("submission_failed", "Local supervisor could not start; same-key retry is safe") from error
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline and not (job / "native.json").exists():
            time.sleep(0.02)
        native = read_json(job / "native.json")
        return {"operation_id": operation_id, "backend_run_id": native.get("backend_run_id"), "status": "submitted",
                "backend": "zenml", "backend_version": "0.97.0", "project_id": payload["project_id"],
                "submitted_at": request["submitted_at"], "last_reconciled_at": now(), "supervisor_pid": process.pid,
                **recipe_provenance(request["recipe"])}

    def cancel(self, job: Path, request: dict) -> dict:
        status = self._rpc("run.status", {"project_id": request["project_id"], "job_dir": str(job)})
        if status["status"] in {"succeeded", "failed", "cancelled"}:
            return {**status, "cancellation_acknowledged": status["status"] == "cancelled"}
        write_json(job / "cancel.json", {"requested_at": now()})
        state = read_json(job / "state.json")
        if (not alive(state.get("supervisor_pid"), state.get("supervisor_identity"))
                and alive(state.get("worker_pid"), state.get("worker_identity"))):
            # Recover control only when the original worker birth identity still
            # proves ownership. A stale numeric PID is never authority to kill.
            group = state["worker_pid"]
            try:
                os.killpg(group, signal.SIGTERM)
            except ProcessLookupError:
                pass
            grace = time.monotonic() + 2
            while process_group_alive(group) and time.monotonic() < grace:
                time.sleep(0.05)
            if process_group_alive(group):
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            state = read_json(job / "state.json")
            if state.get("worker_reaped") or (state.get("worker_pid") and not process_group_alive(state["worker_pid"])):
                break
            time.sleep(0.1)
        result = self._rpc("run.status", {"project_id": request["project_id"], "job_dir": str(job)})
        return {**result, "cancellation_acknowledged": result["status"] == "cancelled" and result.get("worker_alive") is False}

    def resume(self, payload: dict) -> dict:
        job, parent = self._job({**payload, "operation_id": payload.get("parent_operation_id")})
        parent_status = self._rpc("run.status", {"project_id": payload["project_id"], "job_dir": str(job)})
        if (parent_status.get("status") not in {"succeeded", "failed", "cancelled"}
                or parent_status.get("worker_alive") is not False):
            raise BackendError("conflict", "Checkpoint continuation requires the parent worker to stop and cancellation to reconcile")
        checkpoint = self._rpc("model.checkpoint.get", {"project_id": payload["project_id"], "job_dir": str(job),
                                                       "checkpoint_kind": payload.get("checkpoint_kind", "last")})
        requested_checkpoint = payload.get("checkpoint_ref") or payload.get("checkpoint_id")
        if requested_checkpoint and requested_checkpoint != checkpoint["artifact_id"]:
            raise BackendError("dependency_mismatch", "Requested checkpoint identity differs from resolved parent checkpoint")
        if payload.get("mode", payload.get("resume_mode", "exact_state")) not in {"exact_state", "warm_start"}:
            raise BackendError("validation_error", "Resume must explicitly use exact_state or warm_start")
        request = {**parent, **payload, "operation_id": payload.get("operation_id") or str(uuid.uuid4()),
                   "recipe": copy.deepcopy(payload.get("recipe") or parent["recipe"]), "trusted": payload.get("trusted", False),
                   "parent_run_id": checkpoint["backend_run_id"], "parent_checkpoint_ref": checkpoint["artifact_id"],
                   "resume_path": checkpoint["path"], "resume_mode": payload.get("mode", payload.get("resume_mode", "exact_state")),
                   "checkpoint_kind": payload.get("checkpoint_kind", "last")}
        parent_identity = recipe_provenance(parent["recipe"])
        current_identity = recipe_provenance(request["recipe"])
        canonical_id = payload.get("project_recipe_id") or parent_identity["recipe_id"]
        if current_identity["recipe_id"] != canonical_id:
            raise BackendError("conflict", "Continuation must preserve the project's single recipe identity")
        if parent_identity["recipe_id"] != canonical_id:
            if payload.get("legacy_parent_recipe_id") != parent_identity["recipe_id"]:
                raise BackendError("conflict", "A legacy parent recipe needs an explicit canonical migration mapping")
        elif current_identity["recipe_digest"] != parent_identity["recipe_digest"]:
            old_revision, new_revision = parent_identity["recipe_revision"], current_identity["recipe_revision"]
            if type(old_revision) is not int or type(new_revision) is not int or new_revision <= old_revision:
                raise BackendError("conflict", "Changed continuation settings require a new immutable revision of the same recipe")
        for node in request["recipe"]["nodes"]:
            if node["kind"] == "model.train":
                if "steps" not in payload:
                    raise BackendError("validation_error", "Resume requires final total steps")
                if node["parameters"]["steps"] != int(payload["steps"]):
                    raise BackendError("validation_error", "Resume requires a canonical recipe revision with the requested final steps")
        request.update(parent_recipe_id=parent_identity["recipe_id"], parent_recipe_revision=parent_identity["recipe_revision"],
                       parent_recipe_digest=parent_identity["recipe_digest"], executed_recipe_digest=current_identity["recipe_digest"])
        return self.submit(request)
