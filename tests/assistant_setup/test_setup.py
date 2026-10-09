"""Isolated config files and mocked installation boundaries; no vendor downloads/login."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("assistant_setup", ROOT / "distribution/assistant_setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.fixture
def python():
    return sys.executable


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_preview_does_not_mutate_and_contains_reference_only(tmp_path, python, client, monkeypatch):
    monkeypatch.setenv("MODELBUILDER_TOKEN", "must-never-appear")
    path = tmp_path / ("config.toml" if client == "codex" else ".mcp.json")
    preview = setup.preview_config(client, python, "http://127.0.0.1:8765", path)
    assert not path.exists()
    assert not list(tmp_path.iterdir())
    assert "must-never-appear" not in json.dumps(preview)
    assert preview["approval"] == setup.digest(preview["plan"])
    entry = preview["plan"]["entry"]
    assert entry["args"] == ["-I", "-m", "modelbuilder.mcp_server"]
    assert entry["command"] == str(Path(python).absolute())
    if client == "codex":
        assert entry["env_vars"] == ["MODELBUILDER_TOKEN"]
        assert "MODELBUILDER_TOKEN" not in entry["env"]
    else:
        assert entry["env"]["MODELBUILDER_TOKEN"] == "${MODELBUILDER_TOKEN}"


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_configuration_is_atomic_preserves_others_and_backs_up(tmp_path, python, client):
    path = tmp_path / ("config.toml" if client == "codex" else ".mcp.json")
    raw = (b'# User comments\nmodel = "personal-model"\n[mcp_servers.other]\ncommand = "other"\n'
           if client == "codex" else b'{"unrelated":{"a":1},"mcpServers":{"other":{"command":"other"}}}\n')
    path.write_bytes(raw)
    preview = setup.preview_config(client, python, "https://service.example.test", path)
    result = setup.configure(preview["plan"], preview["approval"])
    assert result["status"] == "configured"
    backup = Path(result["backup"])
    assert backup.read_bytes() == raw
    if client == "codex":
        parsed = tomllib.loads(path.read_text())
        assert parsed["mcp_servers"]["other"] == {"command": "other"}
        assert parsed["model"] == "personal-model"
        assert path.read_bytes().startswith(raw.rstrip(b"\n"))
    else:
        parsed = json.loads(path.read_text())
        assert parsed["mcpServers"]["other"] == {"command": "other"}
        assert parsed["unrelated"] == {"a": 1}
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert backup.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.lock"))
    assert not list(tmp_path.glob(".modelbuilder-config-*"))
    repeat = setup.preview_config(client, python, "https://service.example.test", path)
    assert repeat["plan"]["change"] == "unchanged"
    assert setup.configure(repeat["plan"], repeat["approval"])["backup"] is None
    assert len(list(tmp_path.glob("*.modelbuilder-backup-*"))) == 1


@pytest.mark.parametrize("raw", [
    b'[mcp_servers.modelbuilder]\ncommand = "old"\n[mcp_servers.modelbuilder.env]\nOLD = "x"\n'
    b'[mcp_servers.other]\ncommand = "keep"\n',
    b'[mcp_servers."modelbuilder"] # target\ncommand = "old"\n[mcp_servers.other]\ncommand = "keep"\n',
    b'[mcp_servers.other]\ncommand = "keep"\n[mcp_servers.modelbuilder]\ncommand = "old"\n',
])
def test_codex_replaces_only_named_entry(tmp_path, python, raw):
    path = tmp_path / "config.toml"
    path.write_bytes(raw)
    before = tomllib.loads(raw.decode())
    preview = setup.preview_config("codex", python, "http://localhost:8765", path)
    assert preview["plan"]["change"] == "replace"
    setup.configure(preview["plan"], preview["approval"])
    after = tomllib.loads(path.read_text())
    assert setup.without_server(after, "mcp_servers") == setup.without_server(before, "mcp_servers")
    assert after["mcp_servers"]["modelbuilder"] == preview["plan"]["entry"]


@pytest.mark.parametrize("raw", [
    b'mcp_servers = {modelbuilder = {command = "old"}, other = {command = "keep"}}',
    b'[mcp_servers]\nmodelbuilder = { command = "old" }\n',
    b'text = """\n[mcp_servers.modelbuilder]\nfake = 1\n"""\n[mcp_servers.other]\ncommand = "keep"\n',
    b'[mcp_servers.modelbuilder]\ncommand = "old"\n[[people]]\nname = "keep"\n',
])
def test_unhandled_toml_is_refused_without_overwriting(tmp_path, python, raw):
    path = tmp_path / "config.toml"
    path.write_bytes(raw)
    with pytest.raises(setup.SetupError, match="safely edited"):
        setup.preview_config("codex", python, "http://localhost:8765", path)
    assert path.read_bytes() == raw
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize("client,raw", [("claude", b'{"mcpServers":{},"mcpServers":{}}'),
                                        ("claude", b'[]'), ("codex", b'[broken'),
                                        ("claude", b'{"mcpServers":[]}')])
def test_malformed_configuration_never_changes(tmp_path, python, client, raw):
    path = tmp_path / "config"
    path.write_bytes(raw)
    with pytest.raises(setup.SetupError):
        setup.preview_config(client, python, "http://localhost:8765", path)
    assert path.read_bytes() == raw


def test_approval_and_stale_configuration_fail_closed(tmp_path, python):
    path = tmp_path / "config.toml"
    preview = setup.preview_config("codex", python, "http://localhost:8765", path)
    with pytest.raises(setup.SetupError, match="approval digest"):
        setup.configure(preview["plan"], "bad")
    assert not path.exists()
    path.write_text("model = 'changed'\n")
    with pytest.raises(setup.SetupError, match="changed since preview"):
        setup.configure(preview["plan"], preview["approval"])
    assert path.read_text() == "model = 'changed'\n"


def test_approval_is_bound_to_url_python_and_target(tmp_path, python):
    preview = setup.preview_config("codex", python, "http://localhost:8765", tmp_path / "config.toml")
    preview["plan"]["service_url"] = "https://other.example.test"
    with pytest.raises(setup.SetupError):
        setup.configure(preview["plan"], preview["approval"])


def test_busy_configuration_is_not_replaced(tmp_path, python):
    path = tmp_path / "config.toml"
    preview = setup.preview_config("codex", python, "http://localhost:8765", path)
    lock = path.with_name(path.name + ".modelbuilder.lock")
    lock.write_text("already locked")
    with pytest.raises(setup.SetupError, match="lock"):
        setup.configure(preview["plan"], preview["approval"])
    assert lock.read_text() == "already locked"
    assert not path.exists()


def test_symlink_and_hardlink_config_are_refused(tmp_path, python):
    target = tmp_path / "original.toml"
    target.write_text("model = 'mine'\n")
    link = tmp_path / "linked.toml"
    link.symlink_to(target)
    with pytest.raises(setup.SetupError, match="symbolic"):
        setup.preview_config("codex", python, "http://localhost:8765", link)
    hardlink = tmp_path / "hard.toml"
    os.link(target, hardlink)
    with pytest.raises(setup.SetupError, match="single-link"):
        setup.preview_config("codex", python, "http://localhost:8765", hardlink)


@pytest.mark.parametrize("url", ["http://remote.example.test", "https://user:secret@example.test",
                                  "https://service.test?token=secret", "https://service.test/#secret",
                                  "http://localhost:bad", "http://localhost\n/injection",
                                  "https://${MODELBUILDER_TOKEN}.example.test"])
def test_unsafe_service_urls_rejected_without_secret_echo(url):
    with pytest.raises(setup.SetupError) as error:
        setup.validate_url(url)
    assert url not in str(error.value)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("url", ["http://localhost:8765", "http://127.0.0.1:1", "http://[::1]:8765",
                                  "https://service.example.test/prefix"])
def test_safe_urls(url):
    assert setup.validate_url(url) == url


def test_profile_paths_follow_client_conventions(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom-codex"))
    assert setup.config_path("codex", None, None) == tmp_path / "custom-codex/config.toml"
    assert setup.config_path("claude", None, tmp_path) == tmp_path / ".mcp.json"
    with pytest.raises(setup.SetupError):
        setup.config_path("claude", None, None)


def fake_install_preview(tmp_path, monkeypatch, target="linux-x64"):
    cli = tmp_path / "modelbuilder-ide.py"
    cli.write_text("# not executed\n")
    monkeypatch.setattr(setup, "host_target", lambda: target)
    base = "https://open-vsx.org/api/openai/chatgpt/" + target + "/26.5908.31748"
    metadata = {"namespace": "openai", "name": "chatgpt", "version": "26.5908.31748",
                "verified": True, "targetPlatform": target,
                "files": {"download": base + "/file/openai.chatgpt.vsix", "sha256": base + "/file/hash.sha256"}}
    monkeypatch.setattr(setup, "registry_read", lambda url, limit: b"a" * 64 if url.endswith("sha256")
                        else json.dumps(metadata).encode())
    preview = setup.preview_install("codex", None, cli, None, None)
    return preview, metadata


def test_install_preview_exact_identity_source_hash_terms_and_no_profile_guess(tmp_path, monkeypatch):
    preview, _ = fake_install_preview(tmp_path, monkeypatch)
    plan = preview["plan"]
    assert plan["vendor"] == "OpenAI"
    assert plan["extension_id"] == "openai.chatgpt"
    assert plan["version"] == "26.5908.31748"
    assert plan["sha256"] == "a" * 64
    assert plan["terms"] == ["https://openai.com/policies/row-terms-of-use/"]
    assert plan["extensions_dir"] is None and plan["user_data_dir"] is None
    assert plan["cli_python"] == sys.executable
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize("field,bad", [("namespace", "impostor"), ("name", "fake"),
                                      ("version", "latest"), ("targetPlatform", "win32-x64"),
                                      ("verified", False)])
def test_wrong_registry_identity_is_refused(tmp_path, monkeypatch, field, bad):
    preview, metadata = fake_install_preview(tmp_path, monkeypatch)
    metadata[field] = bad
    with pytest.raises(setup.SetupError):
        setup.preview_install("codex", None, Path(preview["plan"]["ide_cli"]), None, None)


@pytest.mark.parametrize("url", ["https://marketplace.visualstudio.com/api/test", "http://open-vsx.org/api/a",
                                  "https://open-vsx.org.evil.test/api/a", "https://user:pass@open-vsx.org/api/a",
                                  "https://open-vsx.org/api/a?token=secret", "https://open-vsx.org/api/a#x"])
def test_marketplace_and_untrusted_registry_urls_refused(url):
    with pytest.raises(setup.SetupError):
        setup.registry_url(url)


def test_artifact_absence_is_channel_gap(monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            raise setup.urllib.error.HTTPError("https://open-vsx.org/api/test", 404, "absent", {}, None)
    monkeypatch.setattr(setup.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(setup.SetupError) as error:
        setup.registry_read("https://open-vsx.org/api/test", 100)
    assert error.value.code == "artifact_unavailable"
    assert "channel gap" in str(error.value)


def vsix(tmp_path, target="linux-x64", identity="openai.chatgpt", version="26.5908.31748"):
    path = tmp_path / "mock.vsix"
    publisher, name = identity.split(".")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("extension/package.json", json.dumps({"publisher": publisher, "name": name, "version": version}))
        archive.writestr("extension.vsixmanifest", f'<PackageManifest><Metadata><Identity TargetPlatform="{target}"/></Metadata></PackageManifest>')
    return path


def test_archive_verifies_bytes_and_embedded_identity(tmp_path):
    path = vsix(tmp_path)
    plan = {"sha256": setup.digest(path.read_bytes()), "extension_id": "openai.chatgpt",
            "version": "26.5908.31748", "target": "linux-x64"}
    setup.verify_archive(path, plan)
    for key, value in [("sha256", "b" * 64), ("extension_id", "Anthropic.claude-code"),
                       ("version", "999"), ("target", "win32-x64")]:
        with pytest.raises(setup.SetupError):
            setup.verify_archive(path, {**plan, key: value})


def test_install_is_separate_approval_download_then_cli_and_verification(tmp_path, monkeypatch):
    preview, _ = fake_install_preview(tmp_path, monkeypatch)
    path = vsix(tmp_path)
    plan = preview["plan"]
    plan["sha256"] = setup.digest(path.read_bytes())
    payload = path.read_bytes()
    calls = []
    monkeypatch.setenv("MODELBUILDER_TOKEN", "service-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "vendor-secret")
    monkeypatch.setattr(setup, "registry_read", lambda *args: payload)
    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "--install-extension" in command:
            assert Path(command[command.index("--install-extension") + 1]).read_bytes() == payload
            return SimpleNamespace(returncode=0, stdout=b"")
        return SimpleNamespace(returncode=0, stdout=b"openai.chatgpt@26.5908.31748\n")
    monkeypatch.setattr(setup.subprocess, "run", run)
    with pytest.raises(setup.SetupError):
        setup.install(plan, "incorrect")
    assert not calls
    result = setup.install(plan, setup.digest(plan))
    assert result["status"] == "installed"
    assert len(calls) == 2
    command, options = calls[0]
    assert command[:2] == [sys.executable, str(tmp_path / "modelbuilder-ide.py")]
    assert "--do-not-include-pack-dependencies" in command
    assert "--extensions-dir" not in command and "--user-data-dir" not in command
    assert "MODELBUILDER_TOKEN" not in options["env"] and "OPENAI_API_KEY" not in options["env"]
    assert not Path(command[command.index("--install-extension") + 1]).exists()


def test_install_requires_exact_post_install_version(tmp_path, monkeypatch):
    preview, _ = fake_install_preview(tmp_path, monkeypatch)
    plan = preview["plan"]
    path = vsix(tmp_path)
    plan["sha256"] = setup.digest(path.read_bytes())
    monkeypatch.setattr(setup, "registry_read", lambda *args: path.read_bytes())
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=b"openai.chatgpt@wrong-version\n"))
    with pytest.raises(setup.SetupError, match="unverified"):
        setup.install(plan, setup.digest(plan))


def test_shell_wrappers_are_not_implicitly_executed(tmp_path, monkeypatch):
    cli = tmp_path / "modelbuilder-ide.cmd"
    cli.touch()
    monkeypatch.setattr(setup, "host_target", lambda: "win32-x64")
    with pytest.raises(setup.SetupError, match="shell wrapper"):
        setup.preview_install("codex", None, cli, None, None)


def test_discovery_uses_manifests_and_never_guesses_version(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda *args: None)
    folder = tmp_path / "an-arbitrary-extension-folder"
    folder.mkdir()
    (folder / "package.json").write_text(json.dumps({"publisher": "openai", "name": "chatgpt", "version": "900"}))
    binary = folder / "bin" / "native" / ("codex.exe" if os.name == "nt" else "codex")
    binary.parent.mkdir(parents=True)
    binary.touch()
    info = setup.discover_client("codex", tmp_path)
    assert info["path"] == str(binary)
    assert info["source"] == "installed_extension"
    second = folder / "bin" / "second" / binary.name
    second.parent.mkdir()
    second.touch()
    assert setup.discover_client("codex", tmp_path)["status"] == "ambiguous"
    assert setup.discover_client("claude", tmp_path)["status"] == "absent"


def test_doctor_missing_token_does_not_probe_or_authenticate(tmp_path, python, monkeypatch):
    monkeypatch.delenv("MODELBUILDER_TOKEN", raising=False)
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(setup, "native_probe", lambda *args: pytest.fail("Must not start MCP without a token"))
    result = setup.doctor("codex", python, "http://127.0.0.1:8765", tmp_path)
    assert result["credential"] == "missing_environment_variable"
    assert result["native_mcp"]["status"] == "not_run"


def test_doctor_never_leaks_probe_exception_or_token(tmp_path, python, monkeypatch):
    monkeypatch.setenv("MODELBUILDER_TOKEN", "never-echo-this")
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    def fail(*args):
        raise setup.SetupError("failed", "never-echo-this")
    monkeypatch.setattr(setup, "native_probe", fail)
    result = setup.doctor("codex", python, "http://localhost:8765", tmp_path)
    assert "never-echo-this" not in json.dumps(result)
    assert result["native_mcp"]["status"] == "failed"


def test_cli_stdout_is_machine_readable_and_missing_token_is_honest(tmp_path):
    env = {**os.environ}
    env.pop("MODELBUILDER_TOKEN", None)
    result = subprocess.run([sys.executable, str(ROOT / "distribution/assistant_setup.py"), "doctor", "--client", "claude",
                             "--python", sys.executable, "--service-url", "http://localhost:8765"],
                            env=env, text=True, capture_output=True, check=True)
    parsed = json.loads(result.stdout)
    assert parsed["status"] == "blocked"
    assert parsed["native_mcp"]["status"] == "not_run"
    assert parsed["authenticated_agent_acceptance"] == "UNVERIFIED"


def test_preview_refuses_python_without_installed_mcp(tmp_path, python, monkeypatch):
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout=b"", stderr=b"private diagnostic"))
    with pytest.raises(setup.SetupError, match="installed native MCP"):
        setup.preview_config("codex", python, "http://localhost:8765", tmp_path / "config.toml")
    assert not list(tmp_path.iterdir())


def test_atomic_replace_failure_leaves_original_intact_and_backup_private(tmp_path, python, monkeypatch):
    path = tmp_path / "config.toml"
    raw = b'model = "keep"\n'
    path.write_bytes(raw)
    preview = setup.preview_config("codex", python, "http://localhost:8765", path)
    def fail(*args):
        raise OSError("simulated replace failure")
    monkeypatch.setattr(setup.os, "replace", fail)
    with pytest.raises(OSError):
        setup.configure(preview["plan"], preview["approval"])
    assert path.read_bytes() == raw
    assert not list(tmp_path.glob(".modelbuilder-config-*"))
    assert not list(tmp_path.glob("*.lock"))
    assert next(tmp_path.glob("*.modelbuilder-backup-*")).read_bytes() == raw


def test_doctor_native_success_does_not_claim_absent_client_is_ready(tmp_path, python, monkeypatch):
    monkeypatch.setenv("MODELBUILDER_TOKEN", "synthetic-test-only")
    monkeypatch.setattr(setup.shutil, "which", lambda *args: None)
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(setup, "native_probe", lambda *args: {"status": "ready", "tool_count": 61})
    result = setup.doctor("claude", python, "http://localhost:8765", tmp_path, probe_client=True)
    assert result["status"] == "native_ready_client_absent"
    assert result["vendor_mcp"]["status"] == "not_run"
    assert result["authenticated_agent_acceptance"] == "UNVERIFIED"


def test_codex_vendor_probe_isolated_and_read_only(tmp_path, python, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/must-not-read-auth")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-forward-vendor-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-forward-vendor-key")
    calls = []
    class FakeProcess:
        def __init__(self, command, env, cwd):
            calls.append((command, env, cwd))
            assert env["CODEX_HOME"] != "/must-not-read-auth"
            assert "OPENAI_API_KEY" not in env and "ANTHROPIC_API_KEY" not in env
            config = Path(env["CODEX_HOME"]) / "config.toml"
            assert "synthetic-test-only" not in config.read_text()
            assert tomllib.loads(config.read_text())["mcp_servers"]["modelbuilder"]["env_vars"] == ["MODELBUILDER_TOKEN"]
        def request(self, method, params):
            assert method in {"initialize", "mcpServerStatus/list"}
            return {"data": [{"name": "modelbuilder", "tools": {"one": {}}}]}
        def notify(self, method):
            assert method == "initialized"
        def close(self):
            pass
    monkeypatch.setattr(setup, "JsonProcess", FakeProcess)
    result = setup.vendor_probe("codex", "codex-reviewed", python, "http://localhost:8765", "synthetic-test-only")
    assert result["status"] == "ready" and result["tool_calls"] == 0 and result["model_prompts"] == 0
    assert not Path(calls[0][1]["CODEX_HOME"]).exists()


def test_claude_vendor_probe_only_mcp_list_and_never_serializes_output(tmp_path, python, monkeypatch):
    def run(command, **kwargs):
        assert command == ["claude-reviewed", "mcp", "list"]
        config = json.loads((kwargs["cwd"] / ".mcp.json").read_text())
        assert config["mcpServers"]["modelbuilder"]["env"]["MODELBUILDER_TOKEN"] == "${MODELBUILDER_TOKEN}"
        assert kwargs["env"]["MODELBUILDER_TOKEN"] == "synthetic-test-only"
        return SimpleNamespace(returncode=0, stdout=b"modelbuilder: synthetic-test-only - Connected\n")
    monkeypatch.setattr(setup.subprocess, "run", run)
    result = setup.vendor_probe("claude", "claude-reviewed", python, "http://localhost:8765", "synthetic-test-only")
    assert result["status"] == "ready"
    assert "synthetic-test-only" not in json.dumps(result)
