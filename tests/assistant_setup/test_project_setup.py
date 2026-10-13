"""Real temporary project files; never invoke a vendor, sign in or install."""
import json
from pathlib import Path
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "distribution"))
import assistant_project as project  # noqa: E402
import assistant_setup as setup  # noqa: E402


@pytest.fixture
def command(tmp_path):
    entrypoint = tmp_path / "managed_core.py"
    entrypoint.write_text("raise RuntimeError('must not run')\n")
    return {"command": sys.executable, "args": ["-I", str(entrypoint), "mcp", "--runtime-root", str(tmp_path),
                                              "--state-dir", str(tmp_path / "private-state"), "--project-id", "project-1"]}


def preview(tmp_path, command, client="codex"):
    return project.preview_project(client, tmp_path, "project-1", command=command)


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_project_setup_preserves_user_data_receipts_no_secrets_and_rollback(tmp_path, command, monkeypatch, client):
    monkeypatch.setenv("MODELBUILDER_TOKEN", "environment-secret-sentinel")
    config = tmp_path / (".codex/config.toml" if client == "codex" else ".mcp.json")
    config.parent.mkdir(exist_ok=True)
    raw = (b'model = "owner-model"\n[mcp_servers.other]\ncommand="other"\n[mcp_servers.other.env]\nTOKEN="existing-secret-sentinel"\n'
           if client == "codex" else b'{"mcpServers":{"other":{"command":"other","env":{"TOKEN":"existing-secret-sentinel"}}}}')
    config.write_bytes(raw)
    instructions = tmp_path / ("AGENTS.md" if client == "codex" else "CLAUDE.md")
    instructions.write_bytes(b"Owner instructions remain exact.\r\n")
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    report, files = preview(tmp_path, command, client)
    assert {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert report["plan"]["scope"] == "project"
    assert report["plan"]["capability"] == "project_read_only"
    assert "secret-sentinel" not in json.dumps(report)
    result = project.apply_project(report, files, report["approval"])
    receipt = Path(result["receipt"]).read_text()
    assert "secret-sentinel" not in receipt and "MODELBUILDER_TOKEN" not in receipt
    assert instructions.read_bytes().startswith(before[str(instructions.relative_to(tmp_path))])
    assert "existing-secret-sentinel" in config.read_text()
    assert "env_vars" not in json.dumps(report["plan"]["entry"])
    assert project.apply_project(report, files, report["approval"])["status"] == "unchanged"
    repeat, same = preview(tmp_path, command, client)
    assert repeat["plan"]["change"] == "unchanged"
    assert project.apply_project(repeat, same, repeat["approval"])["status"] == "unchanged"
    # Unrelated subsequent edits survive rollback, while managed edits fail closed.
    instructions.write_bytes(instructions.read_bytes() + b"New owner instruction.\n")
    if client == "codex":
        config.write_bytes(config.read_bytes() + b'\n[mcp_servers.another]\ncommand="new"\n')
    else:
        data = json.loads(config.read_text())
        data["new_setting"] = True
        config.write_text(json.dumps(data))
    rollback, changes = project.preview_rollback(client, tmp_path)
    assert "secret-sentinel" not in json.dumps(rollback)
    project.rollback_project(rollback, changes, rollback["approval"])
    assert instructions.read_bytes() == b"Owner instructions remain exact.\r\nNew owner instruction.\n"
    assert "existing-secret-sentinel" in config.read_text()
    data = tomllib.loads(config.read_text()) if client == "codex" else json.loads(config.read_text())
    assert "modelbuilder" not in data["mcp_servers" if client == "codex" else "mcpServers"]
    assert not (tmp_path / (".agents" if client == "codex" else ".claude") / "skills/modelbuilder-project/SKILL.md").exists()
    rb, fs = project.preview_rollback(client, tmp_path)
    assert project.rollback_project(rb, fs, rb["approval"])["status"] == "unchanged"


@pytest.mark.parametrize("relative,content,code", [
    (".codex/config.toml", '[mcp_servers.modelbuilder]\ncommand="owner"\n', "config_conflict"),
    (".agents/skills/modelbuilder-project/SKILL.md", "Owner skill", "skill_conflict"),
    ("AGENTS.md", "<!-- modelbuilder:unknown -->", "instruction_conflict"),
])
def test_conflicts_never_overwrite(tmp_path, command, relative, content, code):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    with pytest.raises(setup.SetupError) as error:
        preview(tmp_path, command)
    assert error.value.code == code and path.read_text() == content


def test_expected_hash_guard_and_approval(tmp_path, command):
    report, files = preview(tmp_path, command)
    with pytest.raises(setup.SetupError):
        project.apply_project(report, files, "0" * 64)
    (tmp_path / "AGENTS.md").write_text("new user content")
    with pytest.raises(setup.SetupError) as error:
        project.apply_project(report, files, report["approval"])
    assert error.value.code == "config_changed"
    assert not (tmp_path / ".codex/config.toml").exists()
    assert (tmp_path / "AGENTS.md").read_text() == "new user content"


def test_partial_failure_restores_original_files_without_secret_backups(tmp_path, command, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("owner")
    report, files = preview(tmp_path, command)
    original = project._atomic
    failed = False
    def fail_once(path, data):
        nonlocal failed
        if path.name == "SKILL.md" and not failed:
            failed = True
            raise OSError("injected temporary write failure")
        return original(path, data)
    monkeypatch.setattr(project, "_atomic", fail_once)
    with pytest.raises(OSError):
        project.apply_project(report, files, report["approval"])
    assert (tmp_path / "AGENTS.md").read_text() == "owner"
    assert not (tmp_path / ".codex/config.toml").exists()
    assert not (tmp_path / project.receipt_name("codex")).exists()
    assert not list(tmp_path.rglob("*.lock"))


def test_rollback_refuses_modified_owned_skill_and_overrides_untouched(tmp_path, command):
    override = tmp_path / "AGENTS.override.md"
    override.write_text("Owner override")
    report, files = preview(tmp_path, command)
    assert report["plan"]["warnings"]
    project.apply_project(report, files, report["approval"])
    skill = tmp_path / ".agents/skills/modelbuilder-project/SKILL.md"
    skill.write_text(skill.read_text() + "Owner edits")
    with pytest.raises(setup.SetupError) as error:
        project.preview_rollback("codex", tmp_path)
    assert error.value.code == "managed_file_changed" and override.read_text() == "Owner override"
    assert skill.read_text().endswith("Owner edits")


def test_symlinks_and_unsupported_harness_cannot_fake_setup(tmp_path, command):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".agents").symlink_to(outside, target_is_directory=True)
    with pytest.raises(setup.SetupError):
        preview(tmp_path, command)
    with pytest.raises(setup.SetupError) as error:
        preview(tmp_path, command, "triggerfish")
    assert error.value.code == "capability_unavailable" and list(outside.iterdir()) == []


def test_managed_command_rejects_tokens_or_wrong_project(tmp_path, command):
    for changed in ({**command, "env": {"TOKEN": "never"}}, {**command, "args": [*command["args"][:-1], "another"]}):
        with pytest.raises(setup.SetupError) as error:
            preview(tmp_path, changed)
        assert error.value.code == "invalid_managed_command"


def test_catalog_snapshot_matches_live_operation_contract():
    from modelbuilder.models import operation_catalog
    snapshot = json.loads((ROOT / "distribution/assistant_templates/v1/operation-catalog.json").read_text())
    assert snapshot["operations"] == operation_catalog()
    assert len(snapshot["operations"]) >= 111


def test_cli_project_setup_repeat_and_errors_return_safe_json(tmp_path, command):
    import subprocess
    args = [sys.executable, str(ROOT / "distribution/assistant_setup.py")]
    common = ["--client", "codex", "--workspace", str(tmp_path), "--project-id", "project-1",
              "--mcp-command-json", json.dumps(command)]
    result = subprocess.run([*args, "preview-project", *common], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    apply = [*args, "apply-project", *common, "--approval", plan["approval"]]
    for status in ("configured", "unchanged"):
        output = subprocess.run(apply, capture_output=True, text=True)
        assert output.returncode == 0, output.stderr
        assert json.loads(output.stdout)["status"] == status
    blocked = subprocess.run([*args, "preview-project", "--client", "triggerfish", "--workspace", str(tmp_path),
                              "--project-id", "project-1"], capture_output=True, text=True)
    assert blocked.returncode == 2 and blocked.stderr == ""
    assert json.loads(blocked.stdout)["error"]["code"] == "capability_unavailable"


def test_codex_rollback_preserves_comments_outside_owned_stanza_and_blocks_inside(tmp_path, command):
    report, files = preview(tmp_path, command)
    project.apply_project(report, files, report["approval"])
    path = tmp_path / ".codex/config.toml"
    path.write_text(path.read_text() + "# Owner comment after the managed stanza\n")
    rb, changes = project.preview_rollback("codex", tmp_path)
    project.rollback_project(rb, changes, rb["approval"])
    assert "Owner comment" in path.read_text()
    report, files = preview(tmp_path, command)
    project.apply_project(report, files, report["approval"])
    path.write_text(path.read_text().replace('[mcp_servers.modelbuilder]\n', '[mcp_servers.modelbuilder]\n# Edited owner comment\n'))
    with pytest.raises(setup.SetupError) as error:
        project.preview_rollback("codex", tmp_path)
    assert error.value.code == "managed_file_changed"
    assert "Edited owner comment" in path.read_text()
