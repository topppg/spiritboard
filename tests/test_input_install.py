"""Hook installation preserves unrelated synthetic agent configuration."""

import json
import subprocess
from pathlib import Path

import pytest

from spiritboard.input_events import InputSignals
from spiritboard.input_install import install
from spiritboard.sources import SourceProfile


def test_hook_install_is_idempotent_and_removal_preserves_other_hooks(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    root.mkdir()
    path = root / "settings.json"
    original = {
        "env": {"INVENTED": "private"},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing-command"}]}]},
    }
    path.write_text(json.dumps(original))
    profile = SourceProfile("claude-test", "claude", "Synthetic", str(root))
    data = tmp_path / "board"
    install([profile], data)
    first = path.read_text()
    install([profile], data)
    assert path.read_text() == first
    value = json.loads(first)
    assert value["env"] == original["env"]
    assert "AskUserQuestion" in json.dumps(value["hooks"])
    install([profile], data, remove=True)
    assert json.loads(path.read_text()) == original
    assert list(root.glob("settings.json.spiritboard-backup-*"))


def test_codex_install_uses_adjacent_hooks_file_and_requires_agent_trust(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    profile = SourceProfile("codex-test", "codex", "Synthetic", str(root))
    result = install([profile], tmp_path / "board")
    assert result[0]["status"] == "review hooks in codex"
    assert "PermissionRequest" in json.loads((root / "hooks.json").read_text())["hooks"]
    assert not (root / "config.toml").exists()


def test_install_refuses_malformed_or_linked_native_configuration(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    root.mkdir()
    path = root / "settings.json"
    profile = SourceProfile("cl", "claude", "Synthetic", str(root))
    path.write_text("{broken")
    with pytest.raises(ValueError):
        install([profile], tmp_path / "board")
    assert path.read_text() == "{broken"
    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    path.symlink_to(outside)
    with pytest.raises(ValueError):
        install([profile], tmp_path / "board")
    assert outside.read_text() == "{}"


def test_extensions_use_agent_configuration_instead_of_terminal_metadata(tmp_path: Path) -> None:
    pi = tmp_path / "pi"
    (pi / "sessions").mkdir(parents=True)
    config = tmp_path / "opencode-config"
    profiles = [
        SourceProfile("pi-test", "pi", "Pi", str(pi)),
        SourceProfile("oc-test", "opencode", "OpenCode", str(tmp_path / "opencode.db")),
    ]
    install(profiles, tmp_path / "board", opencode_config=config)
    assert (pi / "extensions" / "spiritboard-input.js").is_file()
    assert (config / "plugins" / "spiritboard-input.js").is_file()
    assert not (tmp_path / "extensions").exists()
    install(profiles, tmp_path / "board", remove=True, opencode_config=config)
    assert not (pi / "extensions" / "spiritboard-input.js").exists()
    assert not (config / "plugins" / "spiritboard-input.js").exists()


def test_custom_opencode_database_requires_its_own_config_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    profile = SourceProfile("custom", "opencode", "Custom", str(tmp_path / "other.db"))
    with pytest.raises(ValueError):
        install([profile], tmp_path / "board")
    assert not (tmp_path / "config" / "opencode" / "plugins").exists()


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_installed_hook_runs_from_an_unrelated_directory_with_shell_characters(
    tmp_path: Path,
    kind: str,
) -> None:
    root = tmp_path / "agent"
    root.mkdir()
    directory = tmp_path / "board space ' and $literal"
    profile = SourceProfile("synthetic", kind, "Synthetic", str(root))
    install([profile], directory)
    config = root / ("settings.json" if kind == "claude" else "hooks.json")
    command = json.loads(config.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    payload = {
        "session_id": "session-111",
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion" if kind == "claude" else "request_user_input",
        "tool_use_id": "question-one",
        "tool_input": {"questions": []},
    }
    result = subprocess.run(
        command,
        shell=True,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    signals = InputSignals(directory / "inputs.sqlite3")
    try:
        assert signals.snapshot()["synthetic:session-111"].state == "waiting_user"
    finally:
        signals.close()


def test_windows_hook_paths_do_not_expand_command_shell_metacharacters() -> None:
    from spiritboard.input_install import hook_command

    for character in "%&|<>^!\r\n":
        with pytest.raises(ValueError):
            hook_command(
                ["spiritboard", "--data-dir", "C:/board" + character + "literal"], windows=True
            )
    assert '"C:/board space"' in hook_command(
        ["spiritboard", "--data-dir", "C:/board space"], windows=True
    )
