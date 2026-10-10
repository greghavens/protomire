"""Public operation adapter. Service interpreter never imports ZenML or Torch.

The journal holds process ownership/correlation only. Every run/artifact/model
read is freshly reconciled against the native ZenML store.
"""
from __future__ import annotations
import copy
import json
import os
import signal
import subprocess
import shutil
import sys

try:
    import fcntl
except ImportError:  # Native Windows can still import and run the service.
    fcntl = None
import time
import uuid
from pathlib import Path

from .common import alive, now, process_group_alive, recipe_provenance, read_json, write_json, runtime_identity

from .admission import BackendError, capabilities, environment_profiles, validate_execution


class ZenMLBackend:
    def __init__(self, data_dir: str | Path, python_executable: str | None = None,
                 trusted_code_roots: list[str] | None = None, dependency_paths: dict[str, str] | None = None,
                 source_root: str | Path | None = None, pretrained_path: str | Path | None = None):
        self.root = Path(data_dir).resolve() / "backend"
        self.root.mkdir(parents=True, exist_ok=True)
        package_root = Path(__file__).resolve().parents[2]
        default_source = package_root.parent if package_root.name == "src" else Path.cwd()
        repository = Path(source_root or os.environ.get("MODELBUILDER_SOURCE_ROOT", default_source)).resolve()
        default_python = repository / ".venv-backend" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        configured = python_executable or os.environ.get("MODELBUILDER_BACKEND_PYTHON") or str(default_python)
        self.python = shutil.which(configured) or str(Path(configured).expanduser().resolve())
        self.repository = repository
        self.package_root = package_root
        self.pretrained_path = Path(pretrained_path or repository / ".cache/models/bert-tiny").resolve()
        self.trusted_code_roots = [str(Path(x).resolve()) for x in (trusted_code_roots or [])]
        self.dependency_paths = {key: str(Path(value).resolve()) for key, value in (dependency_paths or {}).items()}

    def with_source_workspace(self, source_root, *, trusted, dependency_paths):
        """Request-local view, sharing native store/job ownership but never mutable routing."""
        backend = copy.copy(self)
        backend.repository = Path(source_root).resolve()
        backend.trusted_code_roots = [str(backend.repository)] if trusted else []
        backend.dependency_paths = dict(dependency_paths)
        return backend

    def with_dataset_dependencies(self, paths, guards):
        """Isolated per-request approved dataset locations; never change operator state."""
        backend = copy.copy(self)
        backend.dependency_paths = dict(self.dependency_paths)
        for identifier, path in paths.items():
            if identifier in backend.dependency_paths and backend.dependency_paths[identifier] != path:
                raise BackendError("dataset_binding_conflict", "Operator dependency conflicts with a registered dataset binding",
                                   {"dependency_id": identifier})
            backend.dependency_paths[identifier] = path
        backend.dataset_source_guards = copy.deepcopy(guards)
        return backend

    def _env(self) -> dict:
        allowed = {"PATH", "LANG", "LC_ALL", "TZ", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
                   "https_proxy", "http_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"}
        env = {key: value for key, value in os.environ.items() if key in allowed}
        env["HOME"] = str(self.root / "home")
        Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
        env.update(PYTHONPATH=str(self.package_root), PYTHONSAFEPATH="1",
                   MODELBUILDER_PRETRAINED_PATH=str(self.pretrained_path),
                   ZENML_CONFIG_PATH=str(self.root / "zenml-config"), ZENML_LOCAL_STORES_PATH=str(self.root / "zenml-stores"),
                   ZENML_ANALYTICS_OPT_IN="false", ZENML_ENABLE_REPO_INIT_WARNINGS="false", ZENML_LOGGING_VERBOSITY="ERROR",
                   ZENML_DEBUG="false", TOKENIZERS_PARALLELISM="false", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                   OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", PYTHONUNBUFFERED="1")
        if self.package_root.name != "src":
            env.pop("PYTHONPATH", None)
        return env

    def _capability_reason(self) -> str | None:
        if sys.platform == "win32":
            return "The pinned ZenML local backend does not support native Windows; use a separately configured supported backend host. The IDE and service remain available."
        if fcntl is None or sys.platform not in {"linux", "darwin"}:
            return "The pinned local backend requires supported POSIX process supervision"
        if runtime_identity() is None:
            return "Local backend cannot establish the operating system boot/process namespace identity; safe supervision is unavailable"
        if not Path(self.python).is_file():
            return "Backend interpreter is not installed; configure MODELBUILDER_BACKEND_PYTHON or run scripts/bootstrap.py --backend"
        return None

    def _require_capability(self) -> None:
        reason = self._capability_reason()
        if reason:
            raise BackendError("capability_unavailable", reason)

    def _rpc(self, operation: str, payload: dict) -> dict:
        self._require_capability()
        if not Path(self.python).is_file():
            raise BackendError("capability_unavailable", "Pinned ZenML backend environment is not installed; run scripts/bootstrap.sh --backend")
        request_id = str(uuid.uuid4())
        path = self.root / "requests" / f"{request_id}.json"
        result = path.with_suffix(".result.json")
        write_json(path, {"operation": operation, "payload": payload, "root": str(self.root), "result": str(result)})
        with path.with_suffix(".log").open("w") as log:
            try:
                completed = subprocess.run([self.python, "-P", "-m", "modelbuilder.backend.sdk", str(path)], env=self._env(),
                                           cwd=self.root, stdout=log, stderr=subprocess.STDOUT, timeout=90)
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
        recipe = validate_execution(self, payload)
        from ..execution import selection
        payload["execution"] = selection(payload["recipe"], payload.get("execution"))
        if payload["execution"]["input_bindings"]:
            jobs = sorted({str(self._job({"project_id": payload["project_id"], "backend_run_id": binding["backend_run_id"]})[0])
                           for binding in payload["execution"]["input_bindings"]})
            # A read-only subprocess avoids creating request/job records and never
            # imports producer code or deserializes an artifact during planning.
            if not (self.root / "zenml-config").exists():
                raise BackendError("artifact_binding_denied", "No owned native artifact store exists")
            code = """import json,sys
from modelbuilder.backend.native_inputs import resolve_bindings
body=json.load(sys.stdin)
try:
 result=resolve_bindings(body['payload'],body['jobs'])
except Exception as error:
 result={'error':{'code':getattr(error,'code','artifact_binding_denied'),'message':str(error),'details':getattr(error,'details',{})}}
print(json.dumps(result))"""
            completed = subprocess.run([self.python, "-P", "-c", code], input=json.dumps({"payload": payload, "jobs": jobs}),
                text=True, capture_output=True, cwd=self.root, env=self._env(), timeout=90)
            try:
                result = json.loads(completed.stdout.strip().splitlines()[-1])
            except (IndexError, ValueError) as error:
                raise BackendError("artifact_binding_denied", "Native artifact ownership inspection failed") from error
            if completed.returncode or "error" in result:
                error = result.get("error", {})
                raise BackendError(error.get("code", "artifact_binding_denied"), error.get("message", "Native input inspection failed"), error.get("details"))
            payload["resolved_input_bindings"] = result["input_bindings"]
            payload["input_binding_jobs"] = jobs
        else:
            payload["resolved_input_bindings"], payload["input_binding_jobs"] = [], []
        return recipe

    def invoke(self, operation: str, payload: dict, actor=None) -> dict:
        if operation == "execution.targets":
            return {"targets": [{"id": "local-cpu", "available": self._capability_reason() is None, "unavailable_reason": self._capability_reason(), "backend": "zenml", "version": "0.97.0",
                                 "cancel": "supervised_process_termination", "exact_resume": True, "untrusted_isolation": False}],
                    "remote_enabled": False, "capabilities": capabilities(self),
                    "environment_profiles": environment_profiles(self)}
        if operation == "run.plan":
            recipe = payload.get("recipe", {})
            blocked = []
            try:
                self._validate_execution(payload)
            except BackendError as error:
                blocked.append({"code": error.code, "message": error.message, "details": error.details})
            from ..tracking import disclosure, tracking_readiness
            return {"project_id": payload["project_id"], "target": payload.get("target", "local-cpu"), "backend": "zenml", "backend_version": "0.97.0",
                    "node_ids": payload.get("execution", {}).get("node_ids", [node["id"] for node in recipe.get("nodes", [])]),
                    "execution": {**payload.get("execution", {}), "input_bindings": payload.get("resolved_input_bindings", [])}, "executable": not blocked,
                    "blocked_reasons": blocked, "warnings": [item["message"] for item in blocked],
                    "dependency_verification": "verified_at_plan_and_rechecked_at_submission" if not blocked else "blocked",
                    "capabilities": capabilities(self), "tracking": disclosure(payload.get("tracking") or {"mode": "disabled"}),
                    "tracking_readiness": tracking_readiness(payload.get("tracking"), os.environ.get("MODELBUILDER_TRACKING_PYTHON", self.python)),
                    "isolation": "trusted_local_process", "estimated_cost": {"currency": "USD", "amount": 0, "scope": "local provider charge only"}}
        if operation == "run.submit":
            return self.submit(payload)
        if operation == "run.resume":
            return self.resume(payload)
        if operation in {"run.status", "run.logs", "run.artifacts", "run.cancel", "run.evaluations.list", "run.events.list"}:
            job, request = self._job(payload)
            if operation == "run.evaluations.list":
                from .live_evaluation import list_evaluations
                return list_evaluations(job)
            if operation == "run.events.list":
                from .live_evaluation import list_events
                return list_events(job, after=payload.get("after", 0), limit=payload.get("limit", 200))
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
        if operation == "model.export.read":
            from ..export_packages import read_package
            self._job(payload)  # Independent project/run ownership check on every chunk.
            return read_package(self.root / "export-packages", payload)
        if operation.startswith("model."):
            return self._rpc(operation, payload)
        raise BackendError("capability_unavailable", f"Backend operation is unavailable: {operation}")

    def _check_submission_identity(self, request: dict, payload: dict) -> None:
        """A lost service transaction cannot relabel an already submitted native job."""
        from ..execution import selection
        if (not request or request.get("project_id") != payload.get("project_id")
                or request.get("idempotency_key") != payload.get("idempotency_key")
                or request.get("recipe") != payload.get("recipe")
                or selection(request["recipe"], request.get("execution")) != selection(payload["recipe"], payload.get("execution"))
                or request.get("resolved_input_bindings", []) != payload.get("resolved_input_bindings", [])
                or request.get("source_root") != str(self.repository)
                or request.get("dependency_paths", {}) != self.dependency_paths
                or request.get("dataset_source_guards", {}) != getattr(self, "dataset_source_guards", {})
                or request.get("source_binding") != payload.get("source_binding")
                or request.get("source_workspace") != payload.get("source_workspace")):
            raise BackendError("conflict", "Operation identity reused with a conflicting request or source workspace; inspect the original run")

    def submit(self, payload: dict) -> dict:
        self._validate_execution(payload)
        if not Path(self.python).is_file():
            raise BackendError("capability_unavailable", "Backend interpreter is not installed")
        with (self.root / "initialize.lock").open("a") as initialization:
            fcntl.flock(initialization.fileno(), fcntl.LOCK_EX)
            self._rpc("backend.validate", {"environment": payload["recipe"].get("environment", {})})
        supervisor_env = self._env()
        if os.environ.get("MODELBUILDER_TRACKING_PYTHON"):
            supervisor_env["MODELBUILDER_TRACKING_PYTHON"] = os.environ["MODELBUILDER_TRACKING_PYTHON"]
        if payload.get("tracking", {}).get("mode") == "online" and payload.get("tracking_online_consent") is True:
            if os.environ.get("MODELBUILDER_WANDB_API_KEY"):
                supervisor_env["MODELBUILDER_WANDB_API_KEY"] = os.environ["MODELBUILDER_WANDB_API_KEY"]
        operation_id = str(uuid.UUID(payload.get("operation_id") or str(uuid.uuid4())))
        job = self.root / "jobs" / operation_id
        job.mkdir(parents=True, exist_ok=True)
        with (job / "launch.lock").open("a") as lease:
            try:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._check_submission_identity(read_json(job / "request.json"), payload)
                return self._rpc("run.status", {**payload, "job_dir": str(job)})
            if (job / "request.json").exists():
                request = read_json(job / "request.json")
                self._check_submission_identity(request, payload)
                state = read_json(job / "state.json")
                if (read_json(job / "native.json") or state.get("worker_pid") or
                        alive(state.get("supervisor_pid"), state.get("supervisor_identity"))):
                    return self._rpc("run.status", {**payload, "job_dir": str(job)})
                # No process owns the inherited launch lease and no worker/native
                # identity exists: the previous service died before a real launch.
            else:
                request = {**payload, "operation_id": operation_id, "root": str(self.root), "job_dir": str(job),
                           "run_name": f"modelbuilder-{operation_id}", "dependency_paths": self.dependency_paths,
                           "dataset_source_guards": getattr(self, "dataset_source_guards", {}), "source_root": str(self.repository),
                           "submitted_at": now(), "trusted_code_roots": self.trusted_code_roots,
                           "executed_recipe_digest": recipe_provenance(payload["recipe"])["recipe_digest"]}
                write_json(job / "request.json", request)
            write_json(job / "state.json", {"supervisor_state": "starting", "submitted_at": request["submitted_at"]})
            try:
                with (job / "supervisor.log").open("a") as log:
                    process = subprocess.Popen([self.python, "-P", "-m", "modelbuilder.backend.supervisor", str(job), str(lease.fileno())],
                                               cwd=self.root, env=supervisor_env, stdout=log, stderr=subprocess.STDOUT,
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
                **recipe_provenance(request["recipe"]),
                "execution": {**(request.get("execution") or {"node_ids": [node["id"] for node in request["recipe"].get("nodes", [])]}), "input_bindings": request.get("resolved_input_bindings", [])},
                "executed_node_ids": []}

    def cancel(self, job: Path, request: dict) -> dict:
        self._require_capability()
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
        # A checkpoint cut is part of continuation identity. Never expand a
        # policy-only run into its unselected reference/calibration ancestors.
        # Resolve the original public pins again; cached host paths are not trust.
        from ..execution import continuation_selection, selection
        parent_execution = selection(parent["recipe"], parent.get("execution"))
        requested_execution = (selection(request["recipe"], payload["execution"]) if "execution" in payload else
            continuation_selection(parent["recipe"], request["recipe"], parent.get("execution"), request["resume_mode"]))
        if request["resume_mode"] == "exact_state" and requested_execution != parent_execution:
            raise BackendError("dependency_mismatch", "Exact continuation must retain its selected nodes and reference/input bindings; submit a new stage for changed inputs")
        request["execution"] = requested_execution
        for key in ("resolved_input_bindings", "input_binding_jobs"):
            request.pop(key, None)
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
        for node in request["recipe"].get("nodes", []):
            if node["id"] in request["execution"]["node_ids"] and node["kind"] == "model.train":
                if "steps" not in payload:
                    raise BackendError("validation_error", "Resume requires final total steps")
                if node["parameters"]["steps"] != int(payload["steps"]):
                    raise BackendError("validation_error", "Resume requires a canonical recipe revision with the requested final steps")
        request.update(parent_recipe_id=parent_identity["recipe_id"], parent_recipe_revision=parent_identity["recipe_revision"],
                       parent_recipe_digest=parent_identity["recipe_digest"], executed_recipe_digest=current_identity["recipe_digest"])
        return self.submit(request)
