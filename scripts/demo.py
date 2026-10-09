#!/usr/bin/env python3
"""Create a model project and optionally run its pinned recipe through the service.

Supply MODELBUILDER_TOKEN. No credentials, remote targets or resources are created.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--run", action="store_true", help="Explicitly execute trusted repository CPU fixture")
    parser.add_argument("--fixture", choices=["custom-pytorch", "pretrained-small"], default="custom-pytorch")
    parser.add_argument("--import-bundle", type=Path, help="Open an existing portable bundle as this model's recipe")
    args = parser.parse_args()
    if args.run and args.import_bundle:
        parser.error("Imported bundles remain untrusted; review their dependencies before explicitly running them")
    token = os.environ.get("MODELBUILDER_TOKEN")
    if not token:
        parser.error("MODELBUILDER_TOKEN must identify an authorized service actor")
    from modelbuilder.mcp_server import Configuration
    url = Configuration(api_url=args.url.rstrip("/"), token=token).api_url
    key = "demo-" + uuid4().hex[:12]
    with httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, trust_env=False, timeout=100,
                      follow_redirects=False) as client:
        def call(operation, payload):
            response = client.post("/v1/operations/" + operation, json=payload)
            if response.status_code >= 400:
                raise RuntimeError(json.dumps(response.json(), indent=2))
            return response.json()
        project = call("project.create", {"slug": key, "model_name": args.fixture + " demo", "idempotency_key": key + ":project"})
        pid = project["project_id"]
        draft = call("recipe.get", {"project_id": pid, "recipe_id": project["recipe_id"]})
        if args.import_bundle:
            recipe = call("recipe.import", {"project_id": pid,
                "bundle": json.loads(args.import_bundle.read_text()), "expected_revision": draft["revision"],
                "idempotency_key": key + ":import"})
        else:
            manifest = json.loads((ROOT / "examples" / args.fixture / "recipe.json").read_text())
            recipe = call("recipe.create", {"project_id": pid, "recipe": manifest,
                "expected_revision": draft["revision"], "idempotency_key": key + ":recipe"})
        assert recipe["recipe_id"] == draft["recipe_id"] == project["recipe_id"]
        assert recipe["revision"] == draft["revision"] + 1
        evidence = call("evidence.add", {"project_id": pid,
            "source_url": "https://arxiv.org/abs/1706.04599", "source_identifier": "arxiv:1706.04599",
            "title": "On Calibration of Modern Neural Networks",
            "authors": ["Chuan Guo", "Geoff Pleiss", "Yu Sun", "Kilian Q. Weinberger"],
            "citation_metadata": {"claim_verified": False, "purpose": "Calibration motivates evaluation; no paper result is assumed reproduced."},
            "idempotency_key": key + ":evidence"})
        hypothesis = call("hypothesis.create", {"project_id": pid, "evidence_ids": [evidence["evidence_id"]],
            "proposed_change": "Compare the baseline with an explicitly configured KL variant.",
            "expected_outcome": "Measure validation loss and retain improvements or regressions.",
            "acceptance_metric": {"name": "validation_loss", "direction": "minimize"},
            "scope": "Tiny CPU execution fixture, not scientific generalization evidence.", "idempotency_key": key + ":hypothesis"})
        result = {"project": project, "recipe_id": recipe["recipe_id"], "recipe_digest": recipe["digest"],
                  "hypothesis_id": hypothesis["hypothesis_id"], "recipe_revision": recipe["revision"],
                  "recipe_flow": "import" if args.import_bundle else "build", "executed": False}
        if args.run:
            submitted = call("run.submit", {"project_id": pid, "recipe_id": recipe["recipe_id"],
                "expected_revision": recipe["revision"], "trusted": True, "idempotency_key": key + ":run"})
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                status = call("run.status", {"project_id": pid, "operation_id": submitted["operation_id"]})
                if status["status"] in {"succeeded", "failed", "cancelled"}:
                    result.update(executed=True, run=status)
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Run has not reached a terminal state; inspect/cancel operation " + submitted["operation_id"])
            if status["status"] != "succeeded":
                raise RuntimeError(json.dumps(status, indent=2))
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
