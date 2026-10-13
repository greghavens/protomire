#!/usr/bin/env python3
"""Frozen, offline Linux CPU installed-wheel acceptance (test tooling only).

Uses replay_bundle's existing public operations, not a second production runner.
Run with -B. No existing directory or evidence file is reused or overwritten.
See INSTALLED_ACCEPTANCE.md for scope, driver contract, and commands.
"""
import argparse
import base64
import configparser
import csv
import datetime
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import unquote, urlparse
import zipfile


ROLES = ("origin", "replay")
KINDS = ("service", "backend")
CONTEXT_SCHEMA = "modelbuilder.installed-acceptance-context/v1"
RESULT_SCHEMA = "modelbuilder.installed-acceptance-driver-result/v1"
TERMS = ("Synthetic fixture terms: Explicit test-owned deterministic fixture inputs and generated outputs may be "
         "trained, evaluated and exported in isolated offline regression tests only. This is not user or third-party "
         "legal approval. No clearance is granted for third-party runtime or dependency redistribution, public pretrained "
         "assets, upstream data, or external publication. Copied runtime and lockfile bytes retain their declarations.\n")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree_snapshot(root):
    """Hash all files, including bytecode; record symlinks rather than skip them."""
    root = Path(root)
    require(root.is_dir(), f"Missing inventory root: {root}")
    result = {}
    for path in sorted(root.rglob("*")):
        name = str(path.relative_to(root))
        if path.is_symlink():
            if path.is_dir():
                require(path.resolve().is_relative_to(root.resolve()), f"External directory symlink: {path}")
                result[name] = {"kind": "directory_symlink", "target": os.readlink(path)}
                continue  # Ordinary lib64 -> lib; the target is inventoried once.
            require(path.is_file(), f"Dangling or special symlink: {path}")
            result[name] = {"kind": "symlink", "target": os.readlink(path), "sha256": sha(path)}
        elif path.is_file():
            result[name] = {"kind": "file", "sha256": sha(path), "mode": stat.S_IMODE(path.stat().st_mode)}
        else:
            require(path.is_dir(), f"Special file in frozen tree: {path}")
    return result


def site_path(venv):
    path = Path(venv) / "lib/python3.12/site-packages"
    require(path.is_dir(), f"Missing Python 3.12 site-packages: {path}")
    return path


def installed_snapshot(work, *, strict=True):
    result = {}
    for role in ROLES:
        for kind in KINDS:
            key = f"{role}-{kind}"
            try:
                result[key] = tree_snapshot(Path(work) / key)
            except (OSError, ValueError) as error:
                if strict:
                    raise
                result[key] = {"inventory_error": str(error)}
    return result


def root_bytecode(root):
    return {str(p.relative_to(root)): sha(p) for name in ("src", "scripts", "tests/portability")
            for p in sorted((root / name).rglob("*.pyc"))}


def freeze_source(root, replay):
    """Derive the current declared commit; reject dirty/incomplete runtime pins."""
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args])
    inventory = replay.source_inventory(root)
    require(len(inventory) == len(set(inventory)), "Duplicate runtime inventory path")
    require(all(not Path(n).is_absolute() and ".." not in Path(n).parts for n in inventory), "Unsafe source inventory path")
    files, commit, recipes = None, None, {}
    for fixture in ("custom-pytorch", "pretrained-small"):
        path = root / "examples" / fixture / "recipe.json"
        recipe = json.loads(path.read_text())
        actual = {item["path"]: item["sha256"] for item in recipe["source"]["files"]}
        require(len(actual) == len(recipe["source"]["files"]), "Duplicate source declaration")
        require(set(actual) == set(inventory), "Shipping runtime inventory is incomplete")
        if files is None:
            files, commit = actual, recipe["source"]["commit"]
        require(actual == files and recipe["source"]["commit"] == commit, "Shipping pins disagree")
        for name, digest in actual.items():
            path2 = root / name
            require(not path2.is_symlink() and path2.resolve().is_relative_to(root.resolve())
                    and not any(p.is_symlink() for p in path2.parents if p != root and p.is_relative_to(root)),
                    f"Symlinked runtime source: {name}")
            require(sha(path2) == digest and path2.read_bytes() == git("show", f"{commit}:{name}"),
                    f"Runtime differs from declared commit: {name}")
        recipes[fixture] = sha(path)
    tracked = git("ls-files", "-z").decode().split("\0")
    return {"checkout_commit": git("rev-parse", "HEAD").decode().strip(), "runtime_commit": commit,
            "runtime_files": files, "recipes": recipes,
            "tracked_files": {name: sha(root / name) for name in tracked if name and (root / name).is_file()},
            "harness_files": {str(p.relative_to(root)): sha(p) for p in sorted((root / "tests/portability").glob("*.py"))}}


def audit_installed_record(venv, site, payload, record_name):
    """Validate installer-updated RECORD, with a narrow generated-file allowlist."""
    record_path = site / record_name
    require(record_path.is_file() and not record_path.is_symlink(), "Missing or indirect installed RECORD")
    rows = list(csv.reader(io.StringIO(record_path.read_text())))
    require(all(len(row) == 3 for row in rows), "Malformed installed RECORD")
    names = [row[0] for row in rows]
    require(len(names) == len(set(names)), "Duplicate installed RECORD row")
    dist_info = Path(record_name).parent
    generated = {str(dist_info / name) for name in ("INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json")}
    wrappers = set()
    entry_points = str(dist_info / "entry_points.txt")
    if entry_points in payload:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string((site / entry_points).read_text())
        for section in ("console_scripts", "gui_scripts"):
            if parser.has_section(section):
                for name in parser[section]:
                    require(re.fullmatch(r"[A-Za-z0-9_.-]+", name) is not None, "Unsafe wheel entry-point name")
                    wrappers.add(os.path.relpath(venv / "bin" / name, site))
    required = set(payload) | wrappers | {str(dist_info / name) for name in ("INSTALLER", "direct_url.json")}
    allowed = set(payload) | wrappers | generated
    require(required <= set(names) <= allowed, "Installed RECORD has missing or unapproved members")
    metadata_files = {str(dist_info / name) for name in tree_snapshot(site / dist_info)}
    require(metadata_files == {name for name in names if name.startswith(str(dist_info) + "/")},
            "Installed metadata member set differs from RECORD")
    for name, digest, size in rows:
        path = site / name
        require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(venv),
                f"Missing or indirect installed RECORD member: {name}")
        if name == record_name:
            require(digest == size == "", "Installed RECORD must leave its own digest empty")
        else:
            expected = "sha256=" + base64.urlsafe_b64encode(bytes.fromhex(sha(path))).rstrip(b"=").decode()
            require(digest == expected and size == str(path.stat().st_size), f"Invalid installed RECORD entry: {name}")
    return {"installed_record_rows": len(rows), "generated_metadata": sorted(set(names) & generated),
            "entry_point_wrappers": sorted(wrappers)}


def audit_wheel_installation(venv, receipt, frozen_files, identity=None, forbidden_roots=()):
    """Require the whole wheel payload, not a nonempty subset of source hashes."""
    venv, wheel = Path(venv).absolute(), Path(receipt["wheel"])
    site = site_path(venv)
    require(sha(wheel) == receipt["sha256"] and receipt["build_hooks_executed"] is False, "Wheel receipt mismatch")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "Duplicate wheel members")
        require(all(not Path(n).is_absolute() and ".." not in Path(n).parts for n in names), "Unsafe wheel path")
        payload = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in names if not name.endswith("/")}
        records = [name for name in payload if name.endswith(".dist-info/RECORD")]
        require(len(records) == 1, "Missing/ambiguous wheel RECORD")
        rows = list(csv.reader(io.StringIO(archive.read(records[0]).decode())))
        require(all(len(row) == 3 for row in rows) and len(rows) == len(payload)
                and {row[0] for row in rows} == set(payload), "Incomplete or duplicate wheel RECORD")
        for name, digest, size in rows:
            if name == records[0]:
                require(digest == size == "", "Wheel RECORD must leave its own digest empty")
            else:
                raw = archive.read(name)
                expected = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
                require(digest == expected and size == str(len(raw)), f"Invalid wheel RECORD entry: {name}")
    package = {name: digest for name, digest in payload.items() if name.startswith("modelbuilder/")}
    require(package and "modelbuilder/__init__.py" in package, "Missing application wheel payload")
    actual = tree_snapshot(site / "modelbuilder")
    require(set(actual) == {name.removeprefix("modelbuilder/") for name in package}, "Installed application member set differs")
    for name, digest in payload.items():
        if name.endswith(".dist-info/RECORD"):
            continue  # Validated separately below against its installed row set.
        path = site / name
        require(path.is_file() and not path.is_symlink() and sha(path) == digest, f"Installed wheel member differs: {name}")
    installed_record = audit_installed_record(venv, site, payload, records[0])
    expected_sources = {"src/" + name: digest for name, digest in package.items()}
    require(all(frozen_files.get(name) == digest for name, digest in expected_sources.items()), "Wheel differs from frozen source")
    metadata = [name for name in payload if name.endswith(".dist-info/METADATA")]
    require(len(metadata) == 1, "Ambiguous wheel distribution")
    direct = json.loads((site / Path(metadata[0]).parent / "direct_url.json").read_text())
    url = urlparse(direct["url"])
    require(url.scheme == "file" and not url.netloc and Path(unquote(url.path)).resolve() == wheel.resolve(), "Installation is not from this wheel")
    require(not direct.get("dir_info") and isinstance(direct.get("archive_info"), dict), "Missing noneditable wheel archive identity")
    recorded = direct["archive_info"].get("hashes", {}).get("sha256")
    require(recorded in (None, receipt["sha256"]), "Different installation archive hash")
    require(not list(site.glob("__editable__*")) and not list(site.glob("*.egg-link")), "Editable installation hook")
    pths = {str(p): p.read_text() for p in sorted(site.glob("*.pth"))}
    for text in pths.values():
        require("__editable__" not in text and not any(str(Path(p).absolute()) in text for p in forbidden_roots), "Contaminated .pth")
        for line in text.splitlines():
            if line.strip() and not line.startswith(("#", "import ", "import\t")):
                require((site / line).resolve().is_relative_to(venv), "External .pth search path")
    if identity is not None:
        required = ({"modelbuilder", "modelbuilder.recipe", "modelbuilder.service", "pydantic", "jsonschema"}
                    if receipt["kind"] == "service" else
                    {"modelbuilder", "modelbuilder.backend.adapter", "modelbuilder.backend.fixtures", "zenml", "torch", "numpy"})
        require(Path(identity["prefix"]).absolute() == venv, "Wrong environment prefix")
        require(Path(identity["executable"]).absolute() == venv / "bin/python", "Wrong environment interpreter")
        require(set(identity["modules"]) == required, "Missing module identity evidence")
        require(all(value and Path(value).is_file() and Path(value).resolve().is_relative_to(site)
                    for value in identity["modules"].values()), "Module imported outside own environment")
        require(identity.get("pythonpath") in (None, ""), "PYTHONPATH leaked into installed execution")
        require(all(value and Path(value).is_absolute() for value in identity["sys_path"]), "Relative/current directory on sys.path")
        require(all(not any(Path(value).resolve().is_relative_to(Path(p).resolve()) for p in forbidden_roots)
                    for value in identity["sys_path"] if value), "Source/other environment on sys.path")
        require(all(Path(value).resolve().is_relative_to(venv) for value in identity["sys_path"] if "site-packages" in value), "Foreign site-packages")
        require(identity["pth_files"] == pths, "Incomplete .pth identity evidence")
        require(identity["installed_source_sha256"] == expected_sources, "Incomplete installed source identity")
    return {"wheel_sha256": receipt["sha256"], "wheel_members": len(payload), "application_members": len(package),
            "exact_installed_members": True, "own_environment_identity_checked": identity is not None, **installed_record}


def audit_dependency_closure(venv, locks, wheel):
    def canonical(name):
        return re.sub(r"[-_.]+", "-", name).lower()
    expected = {}
    for lock in locks:
        for line in Path(lock).read_text().splitlines():
            line = line.strip().removesuffix("\\").strip()
            if not line or line.startswith(("#", "--hash=")):
                continue
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line)
            require(match is not None, f"Unsupported lock entry: {line}")
            name, version = canonical(match[1]), match[2]
            require(name not in expected or expected[name] == version, "Overlay conflicts with dependency lock")
            expected[name] = version
    with zipfile.ZipFile(wheel["wheel"]) as archive:
        from email.parser import Parser
        metadata = Parser().parsestr(archive.read(next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))).decode())
    expected[canonical(metadata["Name"])] = metadata["Version"]
    actual = {}
    for distribution in importlib.metadata.distributions(path=[str(site_path(venv))]):
        name = canonical(distribution.metadata["Name"])
        require(name not in actual, "Duplicate installed distribution")
        actual[name] = distribution.version
    require(actual == expected, "Installed dependency versions differ from selected hash locks")
    return actual


def audit_driver_result(context, context_path, result):
    require(result.get("schema") == RESULT_SCHEMA and result.get("status") == "PASS", "Driver did not report PASS")
    for key in ("driver_id", "role", "runtime_commit"):
        require(result.get(key) == context[key], f"Driver context mismatch: {key}")
    require(result.get("context_sha256") == sha(context_path), "Driver context digest mismatch")
    assertions = result.get("assertions")
    require(isinstance(assertions, dict) and assertions and all(type(v) is bool and v for v in assertions.values()), "Missing/pending driver assertions")
    require(result.get("worker_termination_confirmed") is True, "Driver workers not confirmed terminated")
    evidence = result.get("evidence")
    require(isinstance(evidence, list) and evidence, "Driver evidence is absent")
    seen = set()
    for item in evidence:
        path = Path(item["path"])
        require(path.is_absolute() and path.is_file() and not path.is_symlink(), "Missing or indirect driver evidence")
        require(path.resolve().is_relative_to(Path(context["driver_work"]).resolve()), "Evidence outside driver-owned work")
        require(str(path) not in seen and sha(path) == item["sha256"], "Duplicate or altered driver evidence")
        seen.add(str(path))
    return {"status": "REPORTED_UNVERIFIED", "driver_reported_status": "PASS", "driver_id": context["driver_id"],
            "assertions": assertions, "evidence": evidence,
            "scope": "Generic hook records assertions, not scientific acceptance; workload-specific review is still required"}


def cleanup_owned_jobs(context, *, timeout=120):
    """Bounded cleanup through existing installed service APIs; never raw PID kills."""
    from modelbuilder.backend import ZenMLBackend
    from modelbuilder.models import Actor
    from modelbuilder.service import OperationService
    actor = Actor("installed-acceptance-cleanup", frozenset({"read", "modify", "execute"}))
    owner = Path(context["work"]).resolve()
    records, errors = [], []
    deadline = time.monotonic() + timeout
    def stopped(status):
        return (status.get("status") in {"succeeded", "failed", "cancelled"}
                and all(status.get(name) is False for name in ("worker_alive", "supervisor_alive", "process_group_alive")))
    data_dirs = set()
    for search in context["search_roots"]:
        search = Path(search)
        require(search.resolve().is_relative_to(owner) and not search.is_symlink(), "Cleanup root escapes work")
        if search.exists():
            for jobs in search.rglob("backend/jobs"):
                require(jobs.resolve().is_relative_to(owner) and not jobs.is_symlink(), "Cleanup jobs escape work")
                data_dirs.add(jobs.parent.parent)
    for data in sorted(data_dirs):
        service = None
        try:
            require((data / "application.sqlite3").is_file(), "Native jobs lack their service database")
            backend = ZenMLBackend(data, python_executable=context["backend_python"], source_root=context["source_root"])
            service = OperationService(db_path=data / "application.sqlite3", backend=backend, source_root=context["source_root"])
            for job in sorted((data / "backend/jobs").iterdir()):
                try:
                    require(job.is_dir() and not job.is_symlink() and job.resolve().is_relative_to(owner), "Unsafe job directory")
                    request = json.loads((job / "request.json").read_text())
                    require(request["operation_id"] == job.name and Path(request["job_dir"]).resolve() == job.resolve(), "Job request ownership differs")
                    payload = {"project_id": request["project_id"], "operation_id": job.name}
                    status = service.invoke("run.status", payload, actor)
                    cancelled = None
                    if status.get("worker_alive") or status["status"] not in {"succeeded", "failed", "cancelled"}:
                        cancelled = service.invoke("run.cancel", {**payload, "idempotency_key": "acceptance-cleanup:" + job.name}, actor)
                    while not stopped(status):
                        require(time.monotonic() < deadline, "Owned native termination timeout")
                        time.sleep(0.1)
                        status = service.invoke("run.status", payload, actor)
                    state = json.loads((job / "state.json").read_text())
                    require(state.get("termination", {}).get("confirmed") is True and stopped(status), "Termination not confirmed")
                    records.append({"data_dir": str(data), "job": str(job), "request": request, "state": state,
                                    "status": status, "cancellation": cancelled})
                except Exception as error:
                    errors.append({"job": str(job), "error": str(error)})
        except Exception as error:
            errors.append({"data_dir": str(data), "error": str(error)})
        finally:
            if service is not None:
                service.hub.close()
                service.store.close()
    return {"status": "FAIL" if errors else "PASS", "jobs": records, "errors": errors,
            "scope": "Existing service cancellation/status on jobs confined to this acceptance work directory"}


class Acceptance:
    def __init__(self, args):
        self.args, self.root, self.work = args, args.root.resolve(), args.work.resolve()
        require(not self.work.exists() and not self.work.is_symlink(), "A new work directory is required")
        require(not self.work.is_relative_to(self.root), "Work must be outside the checkout")
        self.evidence, self.logs = self.work / "evidence", self.work / "logs"
        self.evidence.mkdir(parents=True)
        self.logs.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
        self.env.update(HOME=str(self.work / "home"), UV_CACHE_DIR=str(args.cache_dir.resolve()), UV_LINK_MODE="copy",
                        UV_OFFLINE="1", PIP_NO_INDEX="1", PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="1",
                        OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                        TOKENIZERS_PARALLELISM="false", ZENML_ANALYTICS_OPT_IN="false", WANDB_MODE="disabled")
        self.replay = load_module("acceptance_replay", self.root / "scripts/replay_bundle.py")
        self.audit = load_module("acceptance_rights_audit", Path(__file__).with_name("reproduce_snapshot_review.py"))

    def save(self, name, value):
        with (self.evidence / name).open("x") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")

    def read(self, name):
        return json.loads((self.evidence / name).read_text())

    def command(self, name, argv, allowed=(0,), timeout=None):
        started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        argv = [str(arg) for arg in argv]
        code, error = None, None
        try:
            with (self.logs / (name + ".log")).open("x") as log:
                code = subprocess.run(argv, cwd=self.work, env=self.env, stdout=log, stderr=subprocess.STDOUT,
                                      timeout=timeout or self.args.command_timeout).returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = str(exc)
        self.save(name + "-command.json", {"command": argv, "cwd": str(self.work), "returncode": code,
            "error": error, "started_at": started, "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "log_sha256": sha(self.logs / (name + ".log"))})
        print(name, code, flush=True)
        require(error is None and code in allowed, f"Command failed: {name}; inspect retained evidence/log")

    def stage(self):
        self.command("python-profile", [self.args.python, "-I", "-B", "-c",
                     "import json,platform,sys; print(json.dumps([list(sys.version_info[:2]),platform.system(),platform.machine()]))"])
        require(json.loads((self.logs / "python-profile.log").read_text()) == [[3, 12], "Linux", "x86_64"], "Selected Python has wrong target profile")
        self.frozen = freeze_source(self.root, self.replay)
        self.save("frozen-source-before.json", self.frozen)
        self.bytecode_before = root_bytecode(self.root)
        self.save("root-bytecode-before.json", self.bytecode_before)
        author = self.work / "author-source"
        author.mkdir()
        for name in self.frozen["runtime_files"]:
            target = author / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / name, target)
        (author / "TERMS.txt").write_text(TERMS)
        for name, command in (("init", ["init", "-q"]), ("add", ["add", "."]),
                              ("commit", ["-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid",
                                          "commit", "-qm", "Self-authored fixture terms with frozen runtime bytes"])):
            self.command("git-" + name, ["git", "-C", author, *command])
        synthetic = subprocess.check_output(["git", "-C", str(author), "rev-parse", "HEAD"], text=True).strip()
        # Coordinator-only import; child processes never inherit this path.
        sys.path.insert(0, str(self.root / "src"))
        from modelbuilder.recipe import export_bundle, import_bundle, pin_source_files
        require(Path(sys.modules["modelbuilder.recipe"].__file__).resolve() == self.root / "src/modelbuilder/recipe.py", "Coordinator imported wrong checkout")
        recipe = json.loads((self.root / "examples/custom-pytorch/recipe.json").read_text())
        recipe = pin_source_files(recipe, author, [*self.frozen["runtime_files"], "TERMS.txt"], synthetic)
        archive = self.work / "synthetic.recipe.tar"
        bundle = export_bundle(recipe, archive, source_root=author)
        self.save("export.json", {"runtime_commit": self.frozen["runtime_commit"], "synthetic_author_commit": synthetic,
            "runtime_source_files": len(self.frozen["runtime_files"]), "bundle_digest": bundle["bundle_digest"],
            "archive_sha256": sha(archive), "terms": TERMS, "synthetic_fixture_only": True})
        self.sources_before = {}
        for role in ROLES:
            source = self.work / f"{role}-source"
            import_bundle(archive, source)
            require(not (source / ".git").exists(), "Imported source is not Git-free")
            self.sources_before[role] = tree_snapshot(source)
        self.save("sources-before.json", self.sources_before)
        helper = self.root / "tests/portability/snapshot_review_fixture.py"
        shutil.copyfile(helper, self.work / helper.name)
        shutil.copyfile(Path(__file__), self.work / "installed-acceptance-tool.py")
        if self.args.driver:
            require(self.args.driver.is_file() and self.args.driver_id, "Driver file and explicit driver id required")
            shutil.copyfile(self.args.driver, self.work / "acceptance-driver.py")
        self.tools_before = {p.name: sha(p) for p in self.work.glob("*.py")}
        self.save("test-tools-before.json", self.tools_before)

    def install(self):
        self.wheels = {}
        for role in ROLES:
            source = self.work / f"{role}-source"
            for kind in KINDS:
                key = f"{role}-{kind}"
                venv = self.work / key
                python = venv / "bin/python"
                self.command(key + "-venv", [self.args.uv, "venv", "--offline", "--python", self.args.python, venv])
                lock = source / f"requirements/profiles/{kind}-linux-x64.lock"
                extra = [] if kind == "service" else ["--extra-index-url", "https://download.pytorch.org/whl/cpu",
                                                       "--index-strategy", "unsafe-best-match"]
                self.command(key + "-sync", [self.args.uv, "pip", "sync", "--offline", "--require-hashes", "--only-binary", ":all:",
                                             "--python", python, lock, *extra])
                if kind == "backend":
                    for index, overlay in enumerate(self.args.backend_overlay):
                        require(overlay in self.frozen["runtime_files"] and overlay.startswith("requirements/profiles/")
                                and overlay.endswith(".lock"), "Overlay is outside pinned profiles")
                        self.command(key + f"-overlay-{index}", [self.args.uv, "pip", "install", "--offline", "--require-hashes",
                            "--only-binary", ":all:", "--no-deps", "--python", python, "-r", source / overlay])
                wheel = self.replay.build_wheel(source, self.work / f"{role}-wheels", kind)
                self.wheels[key] = wheel
                self.save(key + "-wheel.json", wheel)
                self.command(key + "-install", [self.args.uv, "pip", "install", "--offline", "--no-deps", "--python", python, wheel["wheel"]])
                self.command(key + "-check", [self.args.uv, "pip", "check", "--offline", "--python", python])
                audit_wheel_installation(venv, wheel, self.frozen["runtime_files"], forbidden_roots=self.forbidden(role, kind))
                locks = [lock, *(source / name for name in self.args.backend_overlay if kind == "backend")]
                self.save(key + "-dependencies.json", audit_dependency_closure(venv, locks, wheel))
        for kind in KINDS:
            require(self.wheels[f"origin-{kind}"]["sha256"] == self.wheels[f"replay-{kind}"]["sha256"], "Independent wheel builds differ")

    def forbidden(self, role, kind):
        return [self.root, self.work / "author-source", *(self.work / f"{r}-source" for r in ROLES),
                *(self.work / f"{r}-{k}" for r in ROLES for k in KINDS if (r, k) != (role, kind))]

    def execute_custom(self):
        for role in ROLES:
            source = self.work / f"{role}-source"
            base = [self.work / f"{role}-service/bin/python", "-I", "-B", source / "scripts/replay_bundle.py"]
            prepared = self.evidence / f"{role}-prepared.json"
            self.command(role + "-prepare", [*base, "prepare", source, "--data-dir", self.work / f"{role}-data", "--backend-python",
                         self.work / f"{role}-backend/bin/python", "--trust-repository-source", "--output", prepared])
            initial = self.read(role + "-prepared.json")
            require(initial["status"] == "BLOCKED" and initial["submitted"] is False, "Unreviewed fixture unexpectedly admitted")
            self.command(role + "-review", [base[0], "-I", "-B", self.work / "snapshot_review_fixture.py", prepared,
                         self.evidence / f"{role}-review.json", "--confirm-synthetic-fixture"])
            self.command(role + "-plan", [*base, "plan", prepared, "--trust-repository-source", "--output", self.evidence / f"{role}-plan.json"])
            plan = self.read(role + "-plan.json")
            require(plan["status"] == "PREPARED", "Reviewed fixture plan is not prepared")
            for stage in ("run", "retry"):
                self.command(role + "-" + stage, [*base, "run-prepared", prepared, "--expected-revision", plan["execution_revision"],
                    "--expected-digest", plan["execution_digest"], "--trust-repository-source", "--output", self.evidence / f"{role}-{stage}.json"])
        self.command("strict-compare", [self.work / "origin-service/bin/python", "-I", "-B", self.work / "origin-source/scripts/replay_bundle.py",
                     "compare", self.evidence / "origin-run.json", self.evidence / "replay-run.json", "--output", self.evidence / "strict-comparison.json"], allowed=(1,))

    def execute_driver(self):
        if not self.args.driver:
            return {"status": "NOT_RUN", "reason": "No explicit installed-workload driver supplied"}
        results = {}
        for role in ROLES:
            directory = self.work / f"{role}-driver"
            directory.mkdir()
            context = {"schema": CONTEXT_SCHEMA, "role": role, "driver_id": self.args.driver_id,
                "runtime_commit": self.frozen["runtime_commit"], "source_root": str(self.work / f"{role}-source"),
                "service_python": str(self.work / f"{role}-service/bin/python"), "backend_python": str(self.work / f"{role}-backend/bin/python"),
                "driver_work": str(directory), "timeout_seconds": max(1, self.args.command_timeout - 120),
                "driver_sha256": sha(self.work / "acceptance-driver.py"), "context_scope": "Independent installed service workload; no synthetic rights inheritance"}
            self.save(role + "-driver-context.json", context)
            context_path, output = self.evidence / f"{role}-driver-context.json", directory / "result.json"
            self.command(role + "-driver", [context["service_python"], "-I", "-B", self.work / "acceptance-driver.py",
                         "--acceptance-context", context_path, "--output", output])
            result = json.loads(output.read_text())
            results[role] = audit_driver_result(context, context_path, result)
            self.save(role + "-driver-result.json", result)
        return {"status": "REPORTED_UNVERIFIED", "runs": results,
                "scope": "Generic execution hook only; requires separate workload-specific scientific acceptance review"}

    def verify_custom(self):
        runs = [self.read(role + "-run.json") for role in ROLES]
        comparison = self.read("strict-comparison.json")
        require(comparison == self.replay.compare_results(*runs), "Strict comparator receipt changed")
        require(comparison["status"] == "FAIL" and [k for k, v in comparison["checks"].items() if not v] == ["output_hashes_equal"], "Unexpected strict comparison failure")
        rights = self.audit.audit_review_provenance(*runs)
        self.save("review-provenance-audit.json", rights)
        require(rights["status"] == "PASS", "Manifest drift is not verified rights-only")
        require(sum(value == runs[1]["output_sha256"].get(name) for name, value in runs[0]["output_sha256"].items()) == 7, "Scientific outputs changed")
        installations = {}
        for role, run in zip(ROLES, runs):
            require(run["status"] == "PASS" and not run["run"]["worker_alive"], "Native custom run did not terminate successfully")
            data = self.work / f"{role}-data"
            jobs = list((data / "backend/jobs").iterdir())
            require(len(jobs) == 1 and jobs[0].name == run["run"]["operation_id"], "Retry created an additional native job")
            job = jobs[0]
            require({name: item["sha256"] for name, item in tree_snapshot(job / "outputs").items()} == run["output_sha256"], "Original outputs changed")
            state = json.loads((job / "state.json").read_text())
            self.save(role + "-native-state.json", state)
            self.save(role + "-native-request.json", json.loads((job / "request.json").read_text()))
            require(state.get("worker_reaped") is True and state.get("process_group_terminated") is True
                    and state.get("termination", {}).get("confirmed") is True and state.get("supervisor_state") == "finished"
                    and state.get("worker_exit_code") == 0 and state.get("cancelled") is False, "Native termination is unconfirmed")
            retry = self.read(role + "-retry.json")
            require(retry["status"] == "PASS" and retry["run"]["backend_run_id"] == run["run"]["backend_run_id"]
                    and retry["run"]["operation_id"] == run["run"]["operation_id"] and retry["output_sha256"] == run["output_sha256"]
                    and retry["submission"]["existing_operation_recorded_before_submit"] is True
                    and run["submission"]["existing_operation_recorded_before_submit"] is False, "Idempotent retry evidence differs")
            with sqlite3.connect(f"file:{data / 'application.sqlite3'}?mode=ro", uri=True) as connection:
                rows = connection.execute("SELECT kind, body FROM resources").fetchall()
            preserved = {}
            for kind, body in rows:
                if "license" in kind or "source" in kind or kind in {"operation", "project", "recipe", "replay_preparation"}:
                    preserved.setdefault(kind, []).append(json.loads(body))
            review = self.read(role + "-review.json")
            self.save(role + "-preserved-rights-records.json", preserved)
            require(preserved.get("hub_license_policy") == [review["policy"]] and len(preserved.get("operation", [])) == 1, "Review/operation storage changed")
            require(review["synthetic_fixture_only"] and review["public_api_only"] and review["git_free_snapshot"], "Missing public review evidence")
            require([r["operation"] for r in review["transcript"]] == ["project.source.list", "project.get", "project.source.bind",
                    "recipe.get", "hub.license.local.inspect", "hub.license.review", "hub.source.policy.bind"], "Public review operation sequence differs")
            for kind in KINDS:
                key = f"{role}-{kind}"
                installations[key] = audit_wheel_installation(self.work / key, self.wheels[key], self.frozen["runtime_files"],
                                                            run[kind + "_identity"], self.forbidden(role, kind))
        self.save("installed-wheel-audit.json", installations)
        policies = [self.read(role + "-review.json")["policy"] for role in ROLES]
        require(policies[0]["policy_id"] != policies[1]["policy_id"] and policies[0]["reviewed_at"] != policies[1]["reviewed_at"], "Review identities were reused")

    def capture_after(self):
        errors = []
        for role in ROLES:
            try:
                context = {"work": str(self.work), "source_root": str(self.work / f"{role}-source"),
                           "backend_python": str(self.work / f"{role}-backend/bin/python"),
                           "search_roots": [str(self.work / f"{role}-data"), str(self.work / f"{role}-driver")]}
                self.save(role + "-cleanup-context.json", context)
                output = self.evidence / f"{role}-cleanup.json"
                self.command(role + "-cleanup", [self.work / f"{role}-service/bin/python", "-I", "-B", self.work / "installed-acceptance-tool.py",
                    "--cleanup-context", self.evidence / f"{role}-cleanup-context.json", "--cleanup-output", output], allowed=(0, 1), timeout=150)
                require(self.read(role + "-cleanup.json")["status"] == "PASS", "Owned native cleanup unconfirmed")
            except Exception as error:
                errors.append({"stage": role + "-cleanup", "error": str(error)})
        captures = {"installed-after.json": lambda: installed_snapshot(self.work, strict=False),
                    "root-bytecode-after.json": lambda: root_bytecode(self.root),
                    "frozen-source-after.json": lambda: freeze_source(self.root, self.replay),
                    "runtime-bytes-after.json": lambda: {name: sha(self.root / name) if (self.root / name).is_file() else None
                                                        for name in self.frozen["runtime_files"]}}
        for role in ROLES:
            captures[role + "-source-after.json"] = lambda role=role: tree_snapshot(self.work / f"{role}-source")
        for name, capture in captures.items():
            try:
                self.save(name, capture())
            except Exception as error:
                errors.append({"stage": name, "error": str(error)})
        self.save("after-capture-errors.json", errors)
        return errors

    def run(self):
        try:
            self.stage()
            self.install()
            before = installed_snapshot(self.work)
            self.save("installed-before.json", before)
            try:
                self.execute_custom()
                driver = self.execute_driver()
                self.verify_custom()
            finally:
                # Each after-image is independent; preserve the original failure.
                capture_errors = self.capture_after()
            require(not capture_errors, "Cleanup/integrity capture failed; inspect after-capture-errors.json")
            require(before == self.read("installed-after.json"), "Installed files or bytecode mutated; original inventories retained")
            require({role: self.read(role + "-source-after.json") for role in ROLES} == self.sources_before, "Imported source changed")
            require(self.read("root-bytecode-after.json") == self.bytecode_before, "Checkout bytecode changed")
            require(self.read("frozen-source-after.json") == self.frozen, "Frozen checkout changed")
            require({p.name: sha(p) for p in self.work.glob("*.py")} == self.tools_before, "Test helper/driver changed")
            self.save("summary.json", {"status": "PASS_SYNTHETIC_INSTALLED_REPLAY", "runtime_commit": self.frozen["runtime_commit"],
                "checkout_commit": self.frozen["checkout_commit"], "runtime_source_files": len(self.frozen["runtime_files"]),
                "strict_comparison_status": "FAIL", "strict_failed_checks": ["output_hashes_equal"],
                "rights_only_structural_audit": "PASS", "fresh_noneditable_environments": 4,
                "all_installed_files_and_bytecode_unchanged": True, "source_and_checkout_bytecode_unchanged": True,
                "both_sides_exact_wheel_and_import_audit": True, "custom_pytorch_scientific_outputs_equal": 7,
                "additional_driver": driver, "scope": "Same-host Linux CPU synthetic custom-PyTorch; no GUI/installer/GPU/provider/pretrained rights claim"})
        except Exception as error:
            self.save("failure.json", {"status": "FAIL", "type": type(error).__name__, "message": str(error),
                "termination_note": "Inspect native states; failed/timed-out driver termination is not assumed"})
            raise
        finally:
            self.save("receipt-sha256.json", {str(p.relative_to(self.work)): sha(p) for p in
                sorted([*self.evidence.glob("*.json"), *self.logs.glob("*.log"), *self.work.glob("*.py")]) if p.is_file()})


def main():
    if "--cleanup-context" in sys.argv:
        parser = argparse.ArgumentParser(description="Owned acceptance-job cleanup via installed service APIs")
        parser.add_argument("--cleanup-context", type=Path, required=True)
        parser.add_argument("--cleanup-output", type=Path, required=True)
        args = parser.parse_args()
        result = cleanup_owned_jobs(json.loads(args.cleanup_context.read_text()))
        with args.cleanup_output.open("x") as stream:
            json.dump(result, stream, indent=2, sort_keys=True)
        raise SystemExit(0 if result["status"] == "PASS" else 1)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--backend-overlay", action="append", default=[])
    parser.add_argument("--driver", type=Path)
    parser.add_argument("--driver-id")
    parser.add_argument("--command-timeout", type=int, default=1800)
    args = parser.parse_args()
    require(sys.dont_write_bytecode, "Invoke this harness with python -B")
    require(sys.version_info[:2] == (3, 12) and platform.system() == "Linux" and platform.machine() == "x86_64", "Only Python 3.12 Linux x86-64 CPU acceptance is supported")
    require(args.command_timeout > 120, "Command timeout must leave native cleanup time")
    require(bool(args.driver) == bool(args.driver_id), "Supply both --driver and --driver-id")
    args.cache_dir = args.cache_dir or args.root / ".cache/uv"
    args.backend_overlay = ["requirements/profiles/trainer.lock", *args.backend_overlay]
    require(len(args.backend_overlay) == len(set(args.backend_overlay)), "Duplicate overlay")
    Acceptance(args).run()


if __name__ == "__main__":
    main()
