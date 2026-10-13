"""Public-API owner review for self-authored CPU fixtures, never user policy setup."""
import json
from pathlib import Path
from uuid import uuid4

from modelbuilder.models import Actor, OPERATIONS
from modelbuilder.service import OperationService


def review_synthetic_snapshot(prepared, *, evidence_alias="snapshot"):
    identity = prepared["preparation"]
    pid, root = identity["project_id"], Path(identity["source_root"])
    if (root / ".git").exists() or not (root / "TERMS.txt").read_text().startswith("Synthetic fixture terms:"):
        raise ValueError("This regression helper accepts only Git-free, explicitly self-authored fixture terms")
    service = OperationService(db_path=Path(identity["data_dir"]) / "application.sqlite3", source_root=root,
        source_roots={"snapshot": {"path": str(root), "kind": "imported_snapshot", "trusted": True, "project_ids": [pid]}},
        hub_config={"publication_roots": {pid: {evidence_alias: str(root)}}}, hub_providers={})
    actor = Actor("synthetic-fixture-owner", frozenset({"read", "modify", "execute", "publish"}))
    transcript = []
    def call(operation, **payload):
        result = service.invoke(operation, {"project_id": pid, **payload,
            **({"idempotency_key": str(uuid4())} if OPERATIONS[operation].mutating else {})}, actor)
        transcript.append({"operation": operation, "result": result})
        return result
    try:
        source = call("project.source.list")["sources"][0]
        assert source["source_kind"] == "imported_snapshot" and source["git_provenance_verified"] is False
        project = call("project.get")
        project = call("project.source.bind", source_alias="snapshot", source_repository_id=source["repository_id"],
                       trusted=True, expected_revision=project["revision"])
        recipe = call("recipe.get", recipe_id=identity["recipe_id"])
        evidence = call("hub.license.local.inspect", source_alias=evidence_alias, repo_type="source",
            source_repository_id=source["repository_id"], source_commit=recipe["source"]["commit"],
            recipe_id=recipe["recipe_id"], expected_revision=recipe["revision"], expected_recipe_digest=recipe["digest"],
            expected_project_revision=project["revision"], files=sorted({item["path"] for item in recipe["source"]["files"]} | {"TERMS.txt"}),
            evidence_document_paths=["TERMS.txt"], max_bytes=8 * 1024 * 1024, declared_license="unknown")
        context = {"purpose": "Self-authored deterministic CPU regression", "commercial": False,
                   "deployment": "isolated offline fixture", "conditions": ["No user or third-party legal clearance"]}
        policy = call("hub.license.review", evidence_id=evidence["evidence_id"], evidence_digest=evidence["evidence_digest"],
            license_document_paths=["TERMS.txt"], permitted_uses=["train", "evaluate", "export"], intended_context="Self-authored fixture tests only",
            required_files=[], required_attributions=[], allowed_destination_licenses=[], gated_terms_acknowledged=False,
            reviewed_usage_context=context, confirm_review=True)
        saved = call("hub.source.policy.bind", recipe_id=recipe["recipe_id"], expected_revision=recipe["revision"],
            expected_project_revision=project["revision"], license_policy_id=policy["policy_id"], intended_use="train", usage_context=context)
        return {"recipe": saved["recipe"], "policy": policy, "transcript": transcript,
                "synthetic_fixture_only": True, "public_api_only": True, "git_free_snapshot": True}
    finally:
        service.hub.close()
        service.store.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prepared", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--confirm-synthetic-fixture", action="store_true", required=True)
    parser.add_argument("--evidence-alias", default="review-files")
    args = parser.parse_args()
    args.output.write_text(json.dumps(review_synthetic_snapshot(json.loads(args.prepared.read_text()), evidence_alias=args.evidence_alias), indent=2) + "\n")
