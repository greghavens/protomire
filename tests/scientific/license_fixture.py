"""Explicit, test-local evidence and review. Never used for external artifacts."""
from modelbuilder.evaluation import digest

NOTICE = "Independent synthetic test fixture weights and generated data. Fixture author grants use solely for Modelbuilder automated tests."
USAGE = {"purpose": "Modelbuilder automated acceptance test", "commercial": False, "deployment": "isolated local test", "conditions": ["No external model or dataset"]}
SOURCE = {"provider": "local", "repo_type": "model", "repo_id": "independent-scientific-fixture", "commit": digest(NOTICE)}
RIGHTS = {"schema_version": "modelbuilder.artifact-rights/v1", "sources": [{**SOURCE, "license_evidence_digest": digest(NOTICE), "license_policy_id": "fixture-review"}], "unresolved": [], "parent_rights_digests": []}


def policy(project_id):
    result = {"schema_version": "modelbuilder.license-policy/v1", "project_id": project_id, "policy_id": "fixture-review", "revision": 1,
              "status": "reviewed", "reviewed_by": "automated-test-fixture-author", "reviewed_at": "2026-01-01T00:00:00Z", "source": SOURCE,
              "license_evidence_digest": digest(NOTICE), "permitted_uses": ["train", "evaluate", "promote", "export"],
              "reviewed_usage_context": USAGE, "unresolved_obligations": [], "required_files": [], "required_attributions": []}
    result["review_digest"] = digest(result)
    return result
