"""Unsupported pip resolution directives cannot yield a verified environment."""
import hashlib
import importlib.metadata
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from modelbuilder.backend.admission import BackendError, verify_environment


def verify_lock(tmp_path, content):
    path = tmp_path / "environment.lock"
    path.write_text(content)
    backend = SimpleNamespace(repository=tmp_path, python=sys.executable)
    recipe = {"environment": {"python": "3.12", "lock_file": {
        "path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}}
    return verify_environment(backend, recipe)


@pytest.mark.parametrize("directive", [
    "-r required.txt", "--requirement=required.txt", "-c constraints.txt",
    "--constraint constraints.txt", "-e ./source", "--editable=./source",
    "--unknown-option", "--hash=sha256:not-a-digest",
    "--hash=sha256:" + "a" * 64 + " -r required.txt",
    "--index-url https://unreviewed.example.invalid/simple",
])
def test_lock_rejects_unsupported_resolution_directives(tmp_path, directive):
    (tmp_path / "required.txt").write_text("definitely-absent-review-package==9.9.9\n")
    with pytest.raises(BackendError, match="unsupported dependency directive") as failure:
        verify_lock(tmp_path, directive + "\n")
    assert failure.value.code == "dependency_mismatch"


def test_lock_accepts_pinned_distribution_hash_continuations_and_target_markers(tmp_path):
    version = importlib.metadata.version("pydantic")
    result = verify_lock(tmp_path, "# Official generated-lock syntax\n"
        f"pydantic=={version} \\\n    --hash=sha256:{'a' * 64} \\\n    --hash=sha256:{'b' * 64}\n"
        'definitely-absent-review-package==9.9.9; sys_platform == "never"\n')
    assert result["verified"] is True
    assert result["packages"] == {"pydantic": version}


def test_all_shipped_lock_option_lines_are_supported_hash_continuations():
    root = Path(__file__).resolve().parents[2]
    locks = list((root / "requirements").glob("*.lock")) + list((root / "requirements/profiles").glob("*.lock"))
    assert locks
    import re
    for path in locks:
        for line in path.read_text().splitlines():
            entry = line.strip().removesuffix("\\").strip()
            if entry.startswith("-"):
                assert re.fullmatch(r"--hash=sha256:[0-9a-fA-F]{64}", entry), (path, entry)


def test_sdk_submission_probe_matches_admission_target_markers(tmp_path):
    """Exercise the real backend branch without creating a ZenML client/store."""
    import json
    import os
    import subprocess

    root = Path(__file__).resolve().parents[2]
    python = root / ".venv-backend/bin/python"
    if not python.is_file():
        pytest.skip("Pinned backend interpreter is not installed")
    script = '''
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from modelbuilder.backend import sdk
from modelbuilder.backend.admission import BackendError
sdk.Client = lambda: SimpleNamespace(active_stack=SimpleNamespace(id="read-only-test-stack"))
environment = {"python": "3.12", "requirements": [
    'definitely-absent-review-package==9.9.9; sys_platform == "never"']}
result = sdk.invoke("backend.validate", {"environment": environment}, Path.cwd())
assert result["verified"] and result["packages"] == {}
environment["requirements"] = [f'pydantic==0.0.0; sys_platform == "{sys.platform}"']
try:
    sdk.invoke("backend.validate", {"environment": environment}, Path.cwd())
except BackendError as error:
    assert error.code == "dependency_mismatch", error.code
else:
    raise AssertionError("Active mismatched requirement was accepted")
print(json.dumps({"false_marker_skipped": True, "active_mismatch_rejected": True}))
'''
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"}}
    env.update(HOME=str(tmp_path), PYTHONPATH=str(root / "src"),
               ZENML_CONFIG_PATH=str(tmp_path / "zenml-config"), ZENML_ANALYTICS_OPT_IN="false")
    completed = subprocess.run([str(python), "-c", script], cwd=tmp_path, env=env,
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "false_marker_skipped": True, "active_mismatch_rejected": True}


def test_admission_rejects_trusted_subtree_when_runtime_requires_repository_trust(tmp_path):
    from modelbuilder.backend.admission import validate_execution

    backend = SimpleNamespace(repository=tmp_path, trusted_code_roots=[str(tmp_path / "adapters")],
                              _require_capability=lambda: None)
    payload = {"trusted": True, "recipe": {"nodes": [{"id": "adapter", "kind": "code.python"}]}}
    with pytest.raises(BackendError, match="source repository root") as failure:
        validate_execution(backend, payload)
    assert failure.value.code == "untrusted_execution_blocked"
