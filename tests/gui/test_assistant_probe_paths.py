"""No vendor process is run by these path/isolation tests."""
import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("assistant_probe", ROOT / "tests/gui/probe_assistant_mcp.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def installed(tmp_path, publisher, name, version, binary):
    directory = tmp_path / f"{publisher}.{name}-{version}"
    directory.mkdir()
    (directory / "package.json").write_text(json.dumps({"publisher": publisher, "name": name, "version": version}))
    path = directory / binary
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not executed")
    return path.resolve()


@pytest.mark.parametrize("native", ["linux-x86_64", "linux-aarch64", "darwin-x86_64", "darwin-aarch64"])
def test_codex_discovery_has_no_fixed_version_or_architecture(tmp_path, native):
    path = installed(tmp_path, "openai", "chatgpt", "99.1.0", f"bin/{native}/codex")
    assert probe.discover_binary(tmp_path, "openai.chatgpt", "codex", windows=False) == path


def test_windows_codex_and_claude_suffix(tmp_path):
    codex = installed(tmp_path, "openai", "chatgpt", "99.1.0", "bin/windows-aarch64/codex.exe")
    claude = installed(tmp_path, "Anthropic", "claude-code", "5.0.0", "resources/native-binary/claude.exe")
    assert probe.discover_binary(tmp_path, "openai.chatgpt", "codex", windows=True) == codex
    assert probe.discover_binary(tmp_path, "anthropic.claude-code", "claude", windows=True) == claude


def test_multiple_installations_require_explicit_selection(tmp_path):
    one = installed(tmp_path, "openai", "chatgpt", "1", "bin/native/codex")
    installed(tmp_path, "openai", "chatgpt", "2", "bin/native/codex")
    with pytest.raises(ValueError, match="found 2"):
        probe.discover_binary(tmp_path, "openai.chatgpt", "codex", windows=False)
    assert probe.executable(str(one), tmp_path, "openai.chatgpt", "codex") == one


def test_missing_binary_is_a_blocker(tmp_path):
    with pytest.raises(ValueError, match="found 0"):
        probe.discover_binary(tmp_path, "openai.chatgpt", "codex")
    with pytest.raises(ValueError, match="Missing"):
        probe.executable(str(tmp_path / "missing"), tmp_path, "openai.chatgpt", "codex")


@pytest.mark.parametrize("windows,layout", [(True, "Scripts/python.exe"), (False, "bin/python")])
def test_python_environment_layout(tmp_path, monkeypatch, windows, layout):
    monkeypatch.delenv("MODELBUILDER_GUI_PYTHON", raising=False)
    path = tmp_path / ".venv" / layout
    path.parent.mkdir(parents=True)
    path.touch()
    assert probe.default_python(tmp_path, windows=windows) == str(path)


def test_vendor_homes_and_tokens_are_isolated(tmp_path, monkeypatch):
    for name in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "MODELBUILDER_TOKENS_JSON"]:
        monkeypatch.setenv(name, "must-not-be-inherited")
    monkeypatch.setenv("HOME", "/real/home")
    env = probe.isolated_environment(tmp_path, "disposable", 54321)
    assert "must-not-be-inherited" not in env.values()
    assert env["MODELBUILDER_TOKEN"] == "disposable"
    assert env["MODELBUILDER_API_URL"] == "http://127.0.0.1:54321"
    for name in ["HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR"]:
        assert Path(env[name]).is_relative_to(tmp_path)


def test_pipe_reader_is_portable_and_reports_eof():
    messages = probe.stdout_queue(io.StringIO('one\ntwo\n'))
    assert [messages.get(timeout=1) for _ in range(3)] == ['one\n', 'two\n', None]
