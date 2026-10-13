"""Standalone installed streaming sampling/Trainer v1 acceptance, not a runner.

Only the existing installed-acceptance hook may execute this driver. The supplied
service interpreter owns public operations; the backend interpreter owns authored
assets and tensor inspection. No exact-state streaming continuation is claimed.
"""
from __future__ import annotations

import argparse
from bisect import insort
from copy import deepcopy
import csv
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from uuid import uuid4

DRIVER_ID = "streaming-sampling-service-v1"
CONTEXT_SCHEMA = "modelbuilder.installed-acceptance-context/v1"
RESULT_SCHEMA = "modelbuilder.installed-acceptance-driver-result/v1"
TRAIN_ROWS = 120_000
SELECTED_ROWS = 12
TERMS = """Synthetic fixture terms:
These exact locally randomized tiny model assets, generated weather rows and
adapter are authored solely for this isolated offline acceptance. The author
permits local copying, training, evaluation and export of these exact fixture
bytes. No user data, upstream datasets, pretrained assets, external publication
or third-party dependency redistribution is approved by these fixture terms.
"""
USAGE = {"purpose": "Self-authored installed streaming acceptance", "commercial": False,
         "deployment": "isolated offline fixture", "conditions": ["No upstream data or pretrained weights"]}
WRAPPER = '''from pathlib import Path
import time
from modelbuilder.backend.components.transformers_training import train as production_train, evaluate, export

def train(*, inputs, parameters, context):
    job = Path(context['job_dir'])
    pause = (job.parents[2] / 'pause-control').read_text() == 'pause'
    original = context.get('checkpoint_ready')
    def ready(**event):
        if original:
            original(**event)
        if pause and event['step'] == 1:
            (job / 'boundary-waiting').write_text('completed native step 1')
            deadline = time.monotonic() + 60
            while not Path(context['cancel_file']).exists() and time.monotonic() < deadline:
                time.sleep(.05)
            if not Path(context['cancel_file']).exists():
                raise RuntimeError('Test checkpoint synchronization expired')
            raise InterruptedError('Public cancellation observed after native step 1 checkpoint')
    return production_train(inputs=inputs, parameters=parameters,
                            context={**context, 'checkpoint_ready': ready})
'''

def require(condition, message):
    if not condition:
        raise AssertionError(message)


def sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def dump(path, value):
    path = Path(path)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def inventory(root):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        require(not path.is_symlink(), "Fixture inventory contains a symlink")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = {"sha256": sha(path), "size": path.stat().st_size}
    return result


def installed_identity(expected_python):
    import modelbuilder
    require(sys.flags.isolated == 1 and sys.dont_write_bytecode, "Installed calls require -I -B")
    require(os.path.abspath(sys.executable) == os.path.abspath(expected_python), "Wrong supplied interpreter")
    prefix = Path(sys.prefix).resolve()
    require(prefix != Path(sys.base_prefix).resolve(), "An independent installed environment is required")
    require(prefix == Path(expected_python).parent.parent.resolve(), "Interpreter does not own the selected environment")
    require(Path(modelbuilder.__file__).resolve().is_relative_to(prefix), "Modelbuilder is outside this environment")
    require(not os.environ.get("PYTHONPATH"), "PYTHONPATH is forbidden")
    require(all(path and Path(path).is_absolute() for path in sys.path), "Relative import path")
    return {"executable": sys.executable, "prefix": sys.prefix, "module": modelbuilder.__file__, "sys_path": sys.path}


def check_distinct_environments(service, backend):
    require(Path(service["prefix"]).resolve() != Path(backend["prefix"]).resolve(), "Service/backend environments alias")


def check_helper_identity(identity, service_identity, backend_python):
    require(Path(identity["prefix"]).resolve() == Path(backend_python).parent.parent.resolve(), "Wrong backend prefix")
    check_distinct_environments(service_identity, identity)
    require(os.path.abspath(identity["executable"]) == os.path.abspath(backend_python), "Wrong backend interpreter")
    require(Path(identity["module"]).resolve().is_relative_to(Path(identity["prefix"]).resolve()), "Backend module outside environment")


def validate_context(context, context_path, output, *, script=None):
    require(context.get("schema") == CONTEXT_SCHEMA and context.get("driver_id") == DRIVER_ID, "Wrong driver context")
    require(context.get("role") in {"origin", "replay"}, "Unknown installed role")
    require(isinstance(context.get("runtime_commit"), str) and len(context["runtime_commit"]) == 40, "Missing runtime commit")
    require(type(context.get("timeout_seconds")) is int and context["timeout_seconds"] >= 360, "Insufficient driver budget")
    work = Path(context["driver_work"])
    require(work.is_absolute() and work.is_dir() and not work.is_symlink(), "Fresh absolute driver work required")
    require(not any(work.iterdir()), "Driver work is not empty; never reuse earlier acceptance")
    require(Path(output).absolute() == work / "result.json" and not Path(output).exists(), "Wrong/new output path")
    require(not work.resolve().is_relative_to(Path(context["source_root"]).resolve()), "Work overlaps immutable source")
    require(Path(context_path).is_file(), "Missing original context")
    require(sha(script or __file__) == context["driver_sha256"], "Driver bytes differ from selected hash")
    for key in ("service_python", "backend_python"):
        require(Path(context[key]).is_absolute() and Path(context[key]).is_file(), "Missing supplied interpreter")
    require(context["service_python"] != context["backend_python"], "Service/backend roles are not independent")
    return work


def asset_helper(request):
    """Backend only: author all assets locally, no model/download lookup."""
    identity = installed_identity(request["backend_python"])
    import torch
    from transformers import BertConfig, BertForSequenceClassification
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from safetensors.torch import save_model
    torch.set_num_threads(1)
    directory = Path(request["assets"])
    require(directory.is_dir() and not any(directory.iterdir()), "Assets must be new")
    vocab = ["[UNK]", "[PAD]", "sun", "rain", "warm", "cold", "sky", "cloud", "dark", "light"]
    tokenizer = Tokenizer(WordLevel({word: i for i, word in enumerate(vocab)}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.save(str(directory / "tokenizer.json"))
    torch.manual_seed(10)
    config = BertConfig(vocab_size=10, hidden_size=8, num_hidden_layers=1, num_attention_heads=2,
                        intermediate_size=12, max_position_embeddings=16, num_labels=2, pad_token_id=1,
                        hidden_dropout_prob=.25, attention_probs_dropout_prob=.2)
    model = BertForSequenceClassification(config)
    config.to_json_file(directory / "config.json", use_diff=False)
    save_model(model, str(directory / "weights.safetensors"))
    return {"status": "PASS", "identity": identity, "dropout": [.25, .2], "files": inventory(directory)}


def stopped(row):
    return row.get("status") in {"succeeded", "failed", "cancelled"} and all(
        row.get(name) is False for name in ("worker_alive", "supervisor_alive", "process_group_alive"))


def canonical(value):
    """Selection/content digest contract, independent of production sampler."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def row_for(index, *, heldout=False):
    label = index % 2 if heldout else int(index % 4 == 3)
    return {"id": ("dev-" if heldout else "row-é-") + str(index),
            "text": ("sun sky" if label == 0 else "rain cloud") if heldout else ("sun warm" if label == 0 else "rain cold"),
            "label": label, "position": TRAIN_ROWS + index if heldout else index}


def sample_policy(allocation, count=TRAIN_ROWS):
    require(allocation in {"proportional", "equal"} and count >= 24 and count % 4 == 0, "Unsupported oracle fixture")
    fraction = SELECTED_ROWS / count
    require(Fraction(str(fraction)) * count == SELECTED_ROWS, "Fixture fraction must produce an exact integer")
    return {"kind": "rows.sample", "version": "1", "split_ids": ["train"], "fields": ["label"],
            "fraction": fraction, "seed": "installed-雪-seed", "allocation": allocation, "min_per_stratum": 1,
            "required_strata": [{"label": 0}, {"label": 1}], "max_share_error": 0}


def independent_oracle(count=TRAIN_ROWS):
    """Known exact 75/25 quotas and bounded top-rank lists, never eager input."""
    candidates = {0: [], 1: []}
    seed = sample_policy("proportional", count)["seed"]
    for number in range(count):
        row = row_for(number)
        rank = canonical({"seed": seed, "sample_id": row["id"]})
        target = candidates[row["label"]]
        insort(target, (rank, row["id"], number))
        if len(target) > SELECTED_ROWS:
            target.pop()
    result = {}
    for mode, quotas in (("proportional", {0: 9, 1: 3}), ("equal", {0: 6, 1: 6})):
        numbers = sorted(item[2] for label, quota in quotas.items() for item in candidates[label][:quota])
        selected = []
        for number in numbers:
            row = row_for(number)
            selected.append({"sample_id": row["id"], "source_id": "train-source", "source_row": number + 1,
                "source_content_digest": canonical(row), "content_digest": canonical({k: v for k, v in row.items() if k != "id"}),
                "split_id": "train"})
        result[mode] = {"selected": selected, "selected_ids": [item["sample_id"] for item in selected],
                        "selection_digest": canonical(sorted(item["sample_id"] for item in selected)),
                        "before": {0: count * 3 // 4, 1: count // 4}, "after": quotas}
    return result


def write_data(root, count=TRAIN_ROWS):
    """Two actual encodings, one train source over the eager row ceiling each."""
    root.mkdir()
    manifests = {}
    for fmt in ("jsonl", "csv"):
        directory = root / fmt
        directory.mkdir()
        (directory / "TERMS.txt").write_text(TERMS)
        sources = []
        for name, size, heldout in (("train", count, False), ("dev", 2, True)):
            path = directory / (name + "." + fmt)
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=["id", "text", "label", "position"]) if fmt == "csv" else None
                if writer:
                    writer.writeheader()
                for i in range(size):
                    row = row_for(i, heldout=heldout)
                    if writer:
                        writer.writerow(row)
                    else:
                        stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            sources.append({"id": name + "-source", "root_id": fmt, "path": path.name, "format": fmt, "sha256": sha(path)})
        manifests[fmt] = {"name": "Owned streaming " + fmt, "benchmark": "authored-streaming-v1", "license": "unknown",
            "provenance": {"source": "Generated test-owned weather rows"}, "access_mode": "streaming", "sources": sources,
            "id_column": "id", "row_schema": {"type": "object", "additionalProperties": False,
                "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "label": {"type": "integer"},
                               "position": {"type": "integer"}}, "required": ["id", "text", "label", "position"]},
            "splits": [{"id": "train", "role": "train", "source_ids": ["train-source"]},
                       {"id": "dev", "role": "validation", "sample_ids": ["dev-1", "dev-0"]}]}
    return manifests


def lineage_digest(identities):
    """Six-field ordered UTF-8 chain, independently reproduced from the contract."""
    chain = hashlib.sha256()
    for item in identities:
        raw = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        chain.update(len(raw).to_bytes(8, "big"))
        chain.update(raw)
    return chain.hexdigest()


def expected_consumption(data, identities):
    """Exact actual-prefix consumption, distinct from all-selected membership."""
    result = {"schema": "modelbuilder.dataset-consumption/v2", "dataset": data["dataset"], "split": data["split"],
              "version_id": data["version_id"], "stream_identity": data["stream"]["identity"], "purpose": "training",
              "count": len(identities), "ordered_lineage_digest": lineage_digest(identities),
              "digest_algorithm": "sha256-length-prefixed-canonical-json/v1"}
    return {**result, "digest": canonical(result)}


def check_selection(record, page, oracle, spec):
    require(record["lineage"]["source_row_count"] == sum(oracle["before"].values()) + 2, "Full source coverage missing")
    require(record["row_count"] == SELECTED_ROWS + 2, "Wrong selected/full dataset count")
    require(page["sample_ids"] == oracle["selected_ids"], "Selected source order differs from independent oracle")
    require(page["rows"] == [row_for(item["source_row"] - 1) for item in oracle["selected"]], "Selected row content differs")
    require(page["split"]["lineage_digest"] == lineage_digest(oracle["selected"]), "Full selected source/content lineage differs")
    report = record["transformations"][0]
    require(report["selection_digest"] == oracle["selection_digest"] and report["policy_digest"] == canonical(spec), "Selection/policy digest differs")
    require(report["selected_count"] == SELECTED_ROWS and "selected_sample_ids" not in report, "Unbounded/missing selection report")
    strata = {row["stratum_values"]["label"]: row for row in report["representation"][0]["strata"]}
    require(set(strata) == {0, 1}, "Required strata missing")
    for label in (0, 1):
        require(strata[label]["before"] == oracle["before"][label] and strata[label]["after"] == oracle["after"][label], "Wrong constructive quota")
    require(report["source_counts"] == [{"source_id": "train-source", "before": sum(oracle["before"].values()), "after": SELECTED_ROWS}], "Unexpected source cap/balancing")


def check_checkpoint(descriptor, expected, steps):
    require(descriptor["step"] == steps, "Wrong actual optimizer step count")
    require(descriptor["framework"]["adapter"] == "modelbuilder.transformers.train/v1", "Wrong training adapter")
    require(descriptor["execution"]["device"] == "cpu" and descriptor["execution"]["dtype"] == "float32", "Wrong placement")
    require(descriptor["data"]["iteration"] == "sequential_stream", "Training materialized or shuffled source")
    require(descriptor["data"]["consumption"] == expected, "Actual batch lineage differs, includes prefetch, or loses identity")
    require(descriptor["state"] == ["model"] and descriptor["resume_modes"] == ["warm_start"], "Unsupported exact continuation claim")


def state_helper(request):
    identity = installed_identity(request["backend_python"])
    from safetensors.torch import load_file
    import torch
    root = Path(request["driver_work"]).resolve()
    for key in ("initial", "trained"):
        path = Path(request[key])
        require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root), "Tensor inspection outside owned work")
        require(sha(path) == request[key + "_sha256"], "Tensor input identity changed")
    initial, trained = (load_file(request[key], device="cpu") for key in ("initial", "trained"))
    require(initial.keys() == trained.keys(), "Model tensor keys changed")
    require(all(initial[key].shape == trained[key].shape and trained[key].dtype == torch.float32
                and torch.isfinite(trained[key]).all() for key in initial), "Invalid trained model tensors")
    require(any(not torch.equal(initial[key], trained[key]) for key in initial), "Training left authored weights unchanged")
    return {"status": "PASS", "identity": identity, "assertions": {"native_weights_changed_and_finite": True}}


def parameters(resources):
    lookup = {row["metadata"]["path"]: row for row in resources}
    refs = {key: {"dependency_id": lookup[name]["id"], "sha256": lookup[name]["sha256"]}
            for key, name in (("config", "config.json"), ("weights", "weights.safetensors"), ("tokenizer", "tokenizer.json"))}
    return {"architecture": {"task": "sequence-classification", "model": refs, "text_field": "text", "label_field": "label",
            "labels": ["clear", "wet"], "max_length": 8, "truncate": False, "pad_token_id": 1,
            "limits": {"max_parameters": 100000, "max_asset_bytes": 1048576, "max_rows": 100, "max_tokenized_tokens": 1000}},
            "seed": 123, "max_steps": 2, "batch_size": 2, "gradient_accumulation_steps": 1, "learning_rate": .005,
            "weight_decay": .01, "warmup_steps": 0, "lr_scheduler_type": "constant", "max_grad_norm": 1., "checkpoint_interval_steps": 1}


class Driver:
    def __init__(self, context, context_path, output):
        self.context, self.context_path, self.output = context, Path(context_path), Path(output)
        self.work = validate_context(context, context_path, output)
        self.deadline = time.monotonic() + context["timeout_seconds"] - 120
        self.service = self.backend = self.actor = self.project = None
        self.transcript = self.work / "operations.jsonl"
        self.env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
        self.env.update(HOME=str(self.work / "home"), TMPDIR=str(self.work / "tmp"), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                        OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", TOKENIZERS_PARALLELISM="false",
                        ZENML_ANALYTICS_OPT_IN="false", WANDB_MODE="disabled", PYTHONDONTWRITEBYTECODE="1")
        for name in ("HOME", "TMPDIR"):
            Path(self.env[name]).mkdir()
        # This controller is itself an isolated short-lived harness subprocess.
        os.environ.clear()
        os.environ.update(self.env)
        tempfile.tempdir = None
        self.service_identity = installed_identity(context["service_python"])

    def event(self, row):
        with self.transcript.open("a") as stream:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def call(self, name, payload=None, *, actor=None):
        from modelbuilder.models import OPERATIONS
        body = {**({"project_id": self.project["project_id"]} if self.project else {}), **(payload or {})}
        if OPERATIONS[name].mutating:
            body.setdefault("idempotency_key", str(uuid4()))
        self.event({"operation": name, "phase": "dispatch", "payload": body, "actor": (actor or self.actor).id})
        try:
            result = self.service.invoke(name, body, actor or self.actor)
        except BaseException as error:
            self.event({"operation": name, "phase": "error", "error": str(error), "code": getattr(error, "code", None)})
            raise
        self.event({"operation": name, "phase": "result", "result": result})
        return result

    def command(self, name, args, *, timeout=90):
        budget = min(timeout, self.deadline - time.monotonic())
        require(budget > 0, "Driver execution deadline exceeded")
        with (self.work / (name + ".log")).open("x") as log:
            result = subprocess.run([str(value) for value in args], cwd=self.work, env=self.env,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=budget)
        require(result.returncode == 0, "Helper failed; inspect " + name + ".log")

    def helper(self, name, payload):
        label = name + ("-" + payload["case"] if "case" in payload else "")
        request, output = self.work / (label + "-request.json"), self.work / (label + "-result.json")
        dump(request, {**payload, "backend_python": self.context["backend_python"]})
        self.command(label, [self.context["backend_python"], "-I", "-B", Path(__file__).absolute(),
                            "--backend-mode", name, "--request", request, "--output", output])
        result = read(output)
        require(result["status"] == "PASS", "Backend helper did not pass")
        check_helper_identity(result["identity"], self.service_identity, self.context["backend_python"])
        return result

    def review(self, alias, kind, files, **extra):
        evidence = self.call("hub.license.local.inspect", {"source_alias": alias, "repo_type": kind,
            "files": sorted({*files, "TERMS.txt"}), "evidence_document_paths": ["TERMS.txt"],
            "max_bytes": 64 * 1024 * 1024, "declared_license": "unknown", **extra})
        require(any(row["path"] == "TERMS.txt" and row["text"] == TERMS for row in evidence["evidence"]["documents"]), "Terms differ")
        policy = self.call("hub.license.review", {"evidence_id": evidence["evidence_id"], "evidence_digest": evidence["evidence_digest"],
            "license_document_paths": ["TERMS.txt"], "permitted_uses": ["download", "train", "evaluate", "export"],
            "intended_context": "Exact self-authored fixture bytes only", "required_files": [], "required_attributions": [],
            "allowed_destination_licenses": [], "gated_terms_acknowledged": False,
            "reviewed_usage_context": USAGE, "confirm_review": True})
        return evidence, policy

    def wait(self, operation_id, expected="succeeded"):
        until = min(self.deadline, time.monotonic() + 360)
        while time.monotonic() < until:
            row = self.call("run.status", {"operation_id": operation_id})
            if stopped(row):
                require(row["status"] == expected, "Unexpected native terminal status: " + str(row))
                return row
            time.sleep(.2)
        raise AssertionError("Native service operation exceeded bounded deadline")

    def checkpoint(self, row):
        result = self.call("model.checkpoint.get", {"operation_id": row["operation_id"], "checkpoint_kind": "last"})
        require(Path(result["path"]).resolve().is_relative_to(self.work.resolve()), "Checkpoint escaped owned work")
        return result

    def cleanup(self):
        """Every owned job, including submits interrupted before a result, is reconciled."""
        errors, records = [], []
        deadline = time.monotonic() + 110
        if self.backend is None or self.service is None or self.project is None:
            return {"status": "PASS", "jobs": records, "errors": errors}
        for job in sorted((self.backend.root / "jobs").glob("*")):
            try:
                require(job.is_dir() and not job.is_symlink() and job.resolve().is_relative_to(self.work.resolve()), "Job outside driver")
                request = read(job / "request.json")
                require(request["operation_id"] == job.name and Path(request["job_dir"]).resolve() == job.resolve(), "Job owner differs")
                require(request["project_id"] == self.project["project_id"], "Foreign project job")
                row = self.call("run.status", {"operation_id": job.name})
                if not stopped(row):
                    self.call("run.cancel", {"operation_id": job.name, "idempotency_key": "driver-cleanup-" + job.name})
                while not stopped(row):
                    require(time.monotonic() < deadline, "Owned native cleanup timeout")
                    time.sleep(.1)
                    row = self.call("run.status", {"operation_id": job.name})
                state = read(job / "state.json")
                require(state.get("termination", {}).get("confirmed") is True, "Termination unconfirmed")
                records.append({"job": str(job), "status": row, "request": request, "state": state})
            except BaseException as error:
                errors.append({"job": str(job), "error": str(error)})
        return {"status": "FAIL" if errors else "PASS", "jobs": records, "errors": errors}

    def run(self):
        failure = None
        assertions = None
        try:
            self.prepare()
            assertions = self.execute()
            require(inventory(self.source) == self.source_before, "Committed test source changed")
            require(inventory(self.work / "assets") == self.assets_before, "Authored model assets changed")
            require(inventory(self.work / "data") == self.data_before, "Registered dataset bytes changed")
            require("torch" not in sys.modules and "transformers" not in sys.modules, "Service imported training stack")
        except BaseException as error:
            failure = {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
        finally:
            try:
                cleanup = self.cleanup()
            except BaseException as error:
                cleanup = {"status": "FAIL", "error": str(error)}
            if self.service is not None:
                for name, resource in (("hub", self.service.hub), ("store", self.service.store)):
                    try:
                        resource.close()
                    except BaseException as error:
                        cleanup["status"] = "FAIL"
                        cleanup.setdefault("errors", []).append({"resource": name, "error": str(error)})
            dump(self.work / "cleanup.json", cleanup)
        if failure is not None or cleanup["status"] != "PASS":
            dump(self.work / "failure.json", {"failure": failure, "cleanup": cleanup})
            raise AssertionError("Installed streaming acceptance failed; original failure and cleanup retained")
        assertions.update(fixture_source_and_assets_immutable=True, service_backend_interpreters_isolated=True, owned_native_cleanup_confirmed=True)
        # Seal durable file inventories as evidence; raw originals remain in work.
        dump(self.work / "final-file-inventory.json", inventory(self.work))
        evidence = [{"path": str(path), "sha256": sha(path)} for path in sorted(self.work.glob("*.json"))]
        evidence.append({"path": str(self.transcript), "sha256": sha(self.transcript)})
        result = {"schema": RESULT_SCHEMA, "status": "PASS", "driver_id": DRIVER_ID, "role": self.context["role"],
                  "runtime_commit": self.context["runtime_commit"], "context_sha256": sha(self.context_path),
                  "assertions": assertions, "worker_termination_confirmed": True, "evidence": evidence}
        dump(self.output, result)
        return result

    def open_service(self):
        from modelbuilder.service import OperationService
        if self.service is not None:
            self.service.hub.close()
            self.service.store.close()
        self.service = OperationService(**self.service_config)

    def preview(self, record, split="train"):
        return self.call("dataset.preview", {"dataset_id": record["dataset_id"], "revision": record["revision"],
                         "split_id": split, "purpose": "training" if split == "train" else "evaluation", "limit": 100})

    def prepare(self):
        from modelbuilder.backend import ZenMLBackend
        from modelbuilder.models import Actor
        from modelbuilder.service import OperationService
        from modelbuilder.dataset import MAX_ROWS
        require(TRAIN_ROWS > MAX_ROWS, "Fixture no longer crosses the real eager row limit")
        self.source, assets = self.work / "source", self.work / "assets"
        self.source.mkdir()
        assets.mkdir()
        (self.source / "adapter.py").write_text(WRAPPER)
        (self.source / "TERMS.txt").write_text(TERMS)
        self.manifests = write_data(self.work / "data")
        self.oracle = independent_oracle()
        dump(self.work / "independent-oracle.json", self.oracle)
        self.helper("assets", {"assets": str(assets)})
        (assets / "TERMS.txt").write_text(TERMS)
        for name, args in (("init", ["init", "-q"]), ("add", ["add", "."]), ("commit", ["-c", "user.name=Synthetic Fixture",
                "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Immutable installed streaming fixture"])):
            self.command("source-" + name, ["git", "-C", self.source, *args])
        commit = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], env=self.env, text=True).strip()
        self.source_identity = {"repository": "urn:sha256:" + sha(self.source / "adapter.py"), "commit": commit,
            "files": [{"path": name, "sha256": sha(self.source / name)} for name in ("adapter.py", "TERMS.txt")]}
        self.source_before = inventory(self.source)
        self.assets_before, self.data_before = inventory(assets), inventory(self.work / "data")
        self.data_dir = self.work / "state"
        self.data_dir.mkdir()
        (self.data_dir / "pause-control").write_text("continue")
        self.backend = ZenMLBackend(self.data_dir, python_executable=self.context["backend_python"], source_root=self.source,
                                    trusted_code_roots=[str(self.source)])
        self.actor = Actor("streaming-fixture-owner", frozenset({"read", "modify", "execute", "publish"}))
        self.service_config = {"db_path": self.data_dir / "application.sqlite3", "backend": self.backend, "source_root": self.source,
                              "source_roots": {"owned": {"path": str(self.source), "trusted": True}}, "hub_providers": {}}
        self.service = OperationService(**self.service_config)
        repository = next(row for row in self.call("project.source.list")["sources"] if row["alias"] == "owned")
        self.project = self.call("project.create", {"slug": "streaming-installed", "model_name": "Owned streaming Trainer",
            "source_alias": "owned", "source_repository_id": repository["repository_id"], "trusted": True})
        pid = self.project["project_id"]
        roots = {fmt: str(self.work / "data" / fmt) for fmt in self.manifests}
        self.service_config.update(dataset_roots={pid: roots}, hub_config={"publication_roots": {pid: {
            **roots, "source": str(self.source), "assets": str(assets)}}})
        self.open_service()
        self.recipe = self.call("recipe.get", {"recipe_id": self.project["recipe_id"]})
        evidence, model_policy = self.review("assets", "model", ["config.json", "weights.safetensors", "tokenizer.json"])
        asset = self.call("hub.local.register", {"evidence_id": evidence["evidence_id"], "license_policy_id": model_policy["policy_id"]})
        self.recipe = self.call("hub.asset.bind", {"asset_id": asset["asset_id"], "expected_revision": self.recipe["revision"], "usage_context": USAGE})
        self.resources = deepcopy(self.recipe["resources"])
        self.parameters = parameters(asset["resources"])
        self.model_policy, self.repository = model_policy, repository
        self.records, self.data_policies, self.preparation = {}, {}, []
        for fmt, manifest in self.manifests.items():
            _, policy = self.review(fmt, "dataset", [item["path"] for item in manifest["sources"]])
            self.data_policies[fmt] = policy
            negative = self.call("dataset.validate", {"manifest": {**manifest, "access_mode": "eager"}})
            require(negative["valid"] is False and any(row["code"] == "dataset_limit_exceeded" for row in negative["diagnostics"]),
                    "Large fixture unexpectedly passed real eager admission")
            for mode in ("proportional", "equal"):
                spec = sample_policy(mode)
                record = self.call("dataset.register", {"manifest": {**manifest, "transformations": [spec]}})
                key = fmt + "-" + mode
                page = self.preview(record)
                check_selection(record, page, self.oracle[mode], spec)
                require(self.preview(record, "dev")["sample_ids"] == ["dev-1", "dev-0"], "Held-out explicit order changed")
                pinned = deepcopy(record)
                self.open_service()
                restarted = self.call("dataset.get", {"dataset_id": record["dataset_id"], "revision": record["revision"]})
                require(restarted == pinned and self.preview(record) == page, "Restart redirected pinned dataset/selection")
                self.records[key] = record
                self.preparation.append({"case": key, "eager_rejection": negative, "record": record, "page": page,
                                         "restart_exact": True, "oracle": self.oracle[mode]})
        dump(self.work / "preparation-evidence.json", self.preparation)
        self.dataset_negatives()
        # Build each execution recipe only after real source-policy review of its
        # exact committed adapter. Dataset binding performs immutable rights pinning.
        self.source_policy = None
        dump(self.work / "fixture-provenance.json", {"source": self.source_identity, "source_inventory": self.source_before,
             "assets": self.assets_before, "data": self.data_before, "runtime_commit": self.context["runtime_commit"],
             "service_identity": self.service_identity, "parameters": self.parameters,
             "scope": "Trainer v1 streaming prefix consumption; no exact-state continuation"})

    def dataset_negatives(self):
        from modelbuilder.dataset import DatasetRegistry, load_portable_split
        from modelbuilder.dataset_streaming import StreamingIndex
        from modelbuilder.models import ServiceError
        record = self.records["jsonl-proportional"]
        registry, pid = self.service._dataset_registry, self.project["project_id"]
        manifest = record["manifest"]
        # Original source and index are never changed. Only isolated owned copies.
        owned = self.work / "negative-copies"
        owned.mkdir()
        for source in manifest["sources"]:
            shutil.copyfile(self.work / "data/jsonl" / source["path"], owned / source["path"])
        changed = owned / manifest["sources"][0]["path"]
        with changed.open("ab") as stream:
            stream.write(b"\n")
        copy_reader = DatasetRegistry(None, project_roots={pid: {"jsonl": owned}})
        checks = []
        jobs_before = sorted(path.name for path in (self.backend.root / "jobs").glob("*"))
        def reject(name, action, code, **identity):
            try:
                action()
            except ServiceError as error:
                require(error.code == code, "Unexpected negative gate: " + error.code)
                checks.append({"case": name, "code": error.code, "diagnostic": str(error), **identity})
            else:
                raise AssertionError("Negative gate admitted: " + name)
            require(sorted(path.name for path in (self.backend.root / "jobs").glob("*")) == jobs_before,
                    "Preparation-only negative created a native job")
        reject("changed_source_copy", lambda: copy_reader._materialize(pid, manifest), "dataset_checksum_mismatch",
               target=str(changed), expected_sha256=manifest["sources"][0]["sha256"], actual_sha256=sha(changed))
        limited = DatasetRegistry(self.service.store, project_roots={pid: {"jsonl": self.work / "data/jsonl"}},
                                  streaming_limits={"max_sampling_strata": 1})
        reject("restart_strata_limit", lambda: limited._materialize(pid, manifest), "dataset_limit_exceeded",
               dataset_id=record["dataset_id"], version_id=record["version_id"], max_sampling_strata=1)
        _, index, _, _ = registry._materialize(pid, manifest)
        damaged = owned / "tampered-index.sqlite3"
        shutil.copyfile(index.path, damaged)
        with damaged.open("r+b") as stream:
            stream.seek(100)
            original = stream.read(1)
            stream.seek(100)
            stream.write(bytes([original[0] ^ 1]))
        unsafe = StreamingIndex(registry, pid, manifest, damaged, deepcopy(index.summary), index.sha256,
                                peak_index_bytes=index.peak_index_bytes)
        def connect():
            with unsafe.connect():
                pass
        reject("changed_index_copy", connect, "dataset_identity_mismatch", target=str(damaged),
               expected_sha256=index.sha256, actual_sha256=sha(damaged))
        cancel = owned / "preexisting-cancel.json"
        cancel.write_text("cancel before portable preparation")
        reference = registry.portable_reference(pid, record["dataset_id"], record["revision"])
        paths, guards = registry.execution_sources(pid, record["dataset_id"], record["revision"])
        reject("preexisting_portable_cancel", lambda: load_portable_split(reference, paths, "train", purpose="training",
               source_guards=guards, cancel_file=str(cancel)), "dataset_cancelled", cancel_file=str(cancel), version_id=record["version_id"])
        require(inventory(self.work / "data") == self.data_before, "Negative checks changed original source files")
        dump(self.work / "dataset-negative-evidence.json", {"checks": checks, "native_jobs_unchanged": True, "native_jobs": jobs_before,
             "scope": "Owned tampered copies, stricter restarted cache and preexisting cancellation; not in-flight ingestion/ranking cancellation"})

    def configure_case(self, key):
        from modelbuilder.backend.components import transformers_training
        fmt, mode = key.split("-")
        record = self.records[key]
        policy = self.data_policies[fmt]
        bound = self.call("hub.dataset.policy.bind", {"dataset_id": record["dataset_id"], "dataset_revision": record["revision"],
            "source_policies": {item["id"]: policy["policy_id"] for item in record["manifest"]["sources"]},
            "intended_use": "train", "usage_context": USAGE})
        record = self.call("dataset.get", {"dataset_id": bound["dataset_id"], "revision": bound["dataset_revision"]})
        check_selection(record, self.preview(record), self.oracle[mode], sample_policy(mode))
        current = self.call("recipe.get", {"recipe_id": self.project["recipe_id"]})
        entries = transformers_training.catalog_entries()
        values = [self.parameters, {"batch_size": 2, "metrics": ["accuracy", "cross_entropy"]},
                  {"parity_texts": ["sun sky", "rain cloud"], "atol": 1e-7, "verification_timeout_seconds": 60}]
        nodes = []
        for name, entry, params in zip(("train", "evaluate", "export"), entries, values):
            nodes.append({"id": name, "kind": "component.run", "component": {k: entry[k] for k in
                ("id", "version", "protocol", "role", "description", "parameter_schema", "capabilities")},
                "inputs": entry["inputs"], "outputs": entry["outputs"], "parameters": deepcopy(params), "cache_policy": "disabled",
                "timeout": 240, "retry": {"max_attempts": 1, "backoff_seconds": 0}, "resource_request": {"gpu": 0},
                "environment_ref": "default", "provenance": {}, "implementation_ref": {**self.source_identity["files"][0],
                    "module": "installed_streaming_adapter", "entrypoint": name, "commit": self.source_identity["commit"]}})
        current.update(source=deepcopy(self.source_identity), nodes=nodes, edges=[
            {"source_node": "train", "source_port": "trained", "target_node": name, "target_port": "trained"}
            for name in ("evaluate", "export")], resources=deepcopy(self.resources), external_dependencies=[],
            environment={"python": "3.12", "requirements": ["zenml==0.97.0"]}, evaluation={"live": {"enabled": False}})
        self.recipe = self.call("recipe.update", {"recipe_id": current["recipe_id"], "recipe": current, "expected_revision": current["revision"]})
        if self.source_policy is None:
            _, self.source_policy = self.review("source", "source", ["adapter.py"], source_repository_id=self.repository["repository_id"],
                                               source_commit=self.source_identity["commit"])
            self.recipe = self.call("hub.source.policy.bind", {"recipe_id": self.recipe["recipe_id"], "expected_revision": self.recipe["revision"],
                "expected_project_revision": self.project["revision"], "license_policy_id": self.source_policy["policy_id"],
                "intended_use": "train", "usage_context": USAGE})["recipe"]
        # The immutable adapter is unchanged. Preserve its genuine first review
        # in recipe extensions rather than appending a second source parent.
        for split, purpose, node, target, port in (("train", "training", "training_data", "train", "train"),
                                               ("dev", "evaluation", "evaluation_data", "evaluate", "data")):
            self.recipe = self.call("dataset.bind", {"recipe_id": self.recipe["recipe_id"], "expected_revision": self.recipe["revision"],
                "dataset_id": record["dataset_id"], "dataset_revision": record["revision"], "split_id": split, "purpose": purpose,
                "node_id": node, "target_node": target, "target_port": port})["recipe"]
        require(len(self.recipe["nodes"]) == 5 and len(self.recipe["edges"]) == 4, "Expected existing five-node streaming DAG")
        self.policies = {row["policy_id"] for row in (self.model_policy, policy, self.source_policy)}
        require(len(self.policies) == 3, "Independent model/data/source review identities required")
        data = self.service._dataset_registry.read_split(self.project["project_id"], record["dataset_id"], record["revision"],
                                                        "train", purpose="training")
        dump(self.work / (key + "-recipe.json"), self.recipe)
        return record, data, mode

    def submit(self, key):
        payload = {"recipe_id": self.recipe["recipe_id"], "expected_revision": self.recipe["revision"],
                   "trusted": True, "idempotency_key": key}
        plan = self.call("run.plan", {"recipe_id": self.recipe["recipe_id"], "trusted": True})
        require(plan["executable"] is True, "Streaming native plan blocked: " + json.dumps(plan))
        result = self.call("run.submit", payload)
        require(self.call("run.submit", payload)["operation_id"] == result["operation_id"], "Retry created a second native job")
        return result

    def execute(self):
        from modelbuilder.backend.components import read_checkpoint, read_export
        from modelbuilder.evaluation import validate_evaluation_record
        outcomes = []
        for key in ("jsonl-proportional", "csv-equal"):
            record, data, mode = self.configure_case(key)
            expected = expected_consumption(data, self.oracle[mode]["selected"][:4])
            submitted = self.submit(key)
            row = self.wait(submitted["operation_id"])
            selected = self.checkpoint(row)
            descriptor = read_checkpoint(Path(selected["path"]))
            check_checkpoint(descriptor, expected, 2)
            self.helper("state", {"driver_work": str(self.work), "initial": str(self.work / "assets/weights.safetensors"),
                "initial_sha256": self.assets_before["weights.safetensors"]["sha256"],
                "trained": str(Path(selected["path"]) / "model.safetensors"),
                "trained_sha256": sha(Path(selected["path"]) / "model.safetensors"), "case": key})
            for name in ("accuracy", "cross_entropy"):
                metric = row["metrics"]["evaluation_records"][name]
                require(not validate_evaluation_record(metric), "Invalid native evaluation record")
                require(metric["split"]["sample_ids"] == ["dev-1", "dev-0"], "Wrong actual held-out evaluation order")
                require(metric["dataset"] == data["dataset"] and metric["treatment"]["checkpoint"] == selected["sha256"], "Evaluation identity mismatch")
            directory = self.backend.root / "jobs" / row["operation_id"] / "outputs/export"
            exported = read_export(directory)
            require(exported["selected_checkpoint"] == selected["sha256"], "Export selected a different checkpoint")
            require(exported["verification"]["fresh_process"] is True and exported["verification"]["passed"] is True,
                    "Fresh export prediction parity absent")
            require({item["license_policy_id"] for item in exported["rights"]["sources"]} == self.policies
                    and not exported["rights"]["unresolved"], "Export lacks actual public review provenance")
            require(sha(directory / "model.safetensors") == sha(Path(selected["path"]) / "model.safetensors"), "Exported weights changed")
            outcomes.append({"case": key, "record": record, "status": row, "checkpoint": selected, "descriptor": descriptor,
                             "expected_consumption": expected, "export": exported, "policy_ids": sorted(self.policies)})
        # Last configured CSV/equal recipe is paused only after step1 publication.
        (self.data_dir / "pause-control").write_text("pause")
        cancelled = self.submit("csv-equal-cancel")
        job = self.backend.root / "jobs" / cancelled["operation_id"]
        until = min(self.deadline, time.monotonic() + 240)
        while not (job / "boundary-waiting").is_file() and time.monotonic() < until:
            status = self.call("run.status", {"operation_id": cancelled["operation_id"]})
            require(not stopped(status), "Native cancellation run ended before checkpoint synchronization")
            time.sleep(.1)
        require((job / "boundary-waiting").is_file(), "Missing step1 cancellation synchronization")
        self.call("run.cancel", {"operation_id": cancelled["operation_id"], "idempotency_key": "cancel-step1"})
        stopped_run = self.wait(cancelled["operation_id"], "cancelled")
        selected = self.checkpoint(stopped_run)
        descriptor = read_checkpoint(Path(selected["path"]))
        check_checkpoint(descriptor, expected_consumption(data, self.oracle[mode]["selected"][:2]), 1)
        (self.data_dir / "pause-control").write_text("continue")
        assertions = {"csv_jsonl_beyond_eager_limits": True, "proportional_equal_required_strata_oracle": True,
            "unicode_membership_digest_and_order": True, "immutable_restart_selection": True,
            "owned_source_index_tamper_rejected": True, "cache_strata_limit_and_preexisting_cancel_enforced": True,
            "public_installed_five_node_streaming_dag": True, "actual_consumed_prefix_excludes_prefetch": True,
            "native_weights_changed_finite": True, "selected_heldout_evaluation_and_export": True,
            "three_actual_public_rights_reviews_per_run": True, "public_step1_cancel_with_retained_prefix2": True,
            "model_only_warm_start_scope_no_exact_state_claim": True}
        dump(self.work / "scientific-receipt.json", {"schema": "modelbuilder.installed-streaming-scientific/v1", "status": "PASS",
            "scope": "Trainer v1 CPU/float32 streaming: 12 selected, 4 consumed; cancelled checkpoint 2 consumed. No exact continuation.",
            "assertions": assertions, "positive_runs": outcomes, "cancelled_run": stopped_run, "cancelled_checkpoint": descriptor,
            "remaining_coverage": ["No Parquet installed acceptance", "No Trainer streaming exact continuation or warm-start execution",
                "No in-flight ingestion/ranking cancellation claim", "No cross-role checkpoint byte equality claim"]})
        return assertions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acceptance-context", type=Path)
    parser.add_argument("--backend-mode", choices=("assets", "state"))
    parser.add_argument("--request", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(sys.flags.isolated == 1 and sys.dont_write_bytecode, "Invoke only with -I -B")
    if args.backend_mode:
        require(args.request is not None and args.acceptance_context is None, "Invalid backend helper arguments")
        operation = asset_helper if args.backend_mode == "assets" else state_helper
        dump(args.output, operation(read(args.request)))
    else:
        require(args.acceptance_context is not None and args.request is None, "Missing harness context")
        Driver(read(args.acceptance_context), args.acceptance_context, args.output).run()


if __name__ == "__main__":
    main()
