"""Installed lifecycle hooks outlive disposable source checkouts and environments."""

import importlib
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

import spiritboard
from spiritboard.input_events import InputSignals
from spiritboard.input_install import hook_command, install
from spiritboard.sources import SourceProfile


def _command(root: Path, kind: str) -> str:
    settings = root / ("settings.json" if kind == "claude" else "hooks.json")
    return json.loads(settings.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def _payload(kind: str) -> str:
    return json.dumps(
        {
            "session_id": "synthetic-session",
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion" if kind == "claude" else "request_user_input",
            "tool_use_id": "synthetic-question",
            "tool_input": {"questions": []},
        }
    )


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_installed_hook_survives_deleted_checkout_and_venv(tmp_path: Path, kind: str) -> None:
    checkout = tmp_path / "disposable checkout"
    package = checkout / "src" / "spiritboard"
    shutil.copytree(
        Path(spiritboard.__file__).parent, package, ignore=shutil.ignore_patterns("__pycache__")
    )
    environment = checkout / ".venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    agent = tmp_path / "synthetic-agent"
    agent.mkdir()
    stable = tmp_path / "stable home"
    data = tmp_path / "board data ' and $literal"
    script = """
import importlib
import sys
from pathlib import Path
from spiritboard.input_install import install
from spiritboard.sources import SourceProfile
runtime = importlib.import_module('spiritboard.input_runtime')
runtime.default_data_dir = lambda: Path(sys.argv[4])
install([SourceProfile('synthetic', sys.argv[1], 'Synthetic', sys.argv[2])], Path(sys.argv[3]))
"""
    installer_environment = {**os.environ, "PYTHONPATH": str(checkout / "src")}
    installer_environment.pop("PYTHONHOME", None)
    installed = subprocess.run(
        [str(interpreter), "-c", script, kind, str(agent), str(data), str(stable)],
        env=installer_environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert installed.stdout == installed.stderr == ""
    command = _command(agent, kind)
    shutil.rmtree(checkout)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "spiritboard.py").write_text("raise RuntimeError('untrusted working directory')")
    result = subprocess.run(
        command,
        shell=True,
        input=_payload(kind),
        capture_output=True,
        text=True,
        cwd=unrelated,
        env={**os.environ, "PYTHONPATH": str(unrelated), "PYTHONHOME": str(unrelated)},
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    signals = InputSignals(data / "inputs.sqlite3")
    try:
        assert signals.snapshot()["synthetic:synthetic-session"].state == "waiting_user"
    finally:
        signals.close()


@pytest.mark.parametrize("missing", ["runtime", "interpreter", "corrupt archive"])
def test_missing_hook_runtime_or_interpreter_is_silent_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    runtime = importlib.import_module("spiritboard.input_runtime")
    stable = tmp_path / "stable"
    monkeypatch.setattr(runtime, "default_data_dir", lambda: stable)
    agent = tmp_path / "agent"
    agent.mkdir()
    data = tmp_path / "data"
    install([SourceProfile("synthetic", "claude", "Synthetic", str(agent))], data)
    command = _command(agent, "claude")
    if missing == "runtime":
        shutil.rmtree(stable)
    elif missing == "interpreter":
        executable = runtime.prepare()[0]
        assert hook_command([executable]) in command
        command = command.replace(
            hook_command([executable]), hook_command([str(tmp_path / "missing")]), 1
        )
    else:
        archive = next(stable.rglob("*.pyz"))
        archive.write_bytes(b"invalid archive")
    result = subprocess.run(
        command,
        shell=True,
        input=_payload("claude"),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    signals = InputSignals(data / "inputs.sqlite3")
    try:
        assert not signals.snapshot()
    finally:
        signals.close()
    install([SourceProfile("synthetic", "claude", "Synthetic", str(agent))], data, remove=True)
    assert "hooks" not in json.loads((agent / "settings.json").read_text())


def test_frozen_receiver_survives_removing_the_installing_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spiritboard import input_runtime

    backend = tmp_path / "synthetic-app" / "backend"
    backend.parent.mkdir()
    backend.write_bytes(b"synthetic standalone backend")
    monkeypatch.setattr(input_runtime, "default_data_dir", lambda: tmp_path / "stable")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(backend))
    command = input_runtime.prepare()
    assert command == input_runtime.prepare()
    assert command[1:] == ["input-event"]
    receiver = Path(command[0])
    assert receiver.read_bytes() == backend.read_bytes()
    shutil.rmtree(backend.parent)
    assert receiver.read_bytes() == b"synthetic standalone backend"
    if os.name == "posix":
        assert receiver.stat().st_mode & 0o777 == 0o700


def test_runtime_migration_and_removal_preserve_unrelated_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = importlib.import_module("spiritboard.input_runtime")
    monkeypatch.setattr(runtime, "default_data_dir", lambda: tmp_path / "stable")
    agent = tmp_path / "agent"
    agent.mkdir()
    path = agent / "settings.json"
    unrelated = {"type": "command", "command": "synthetic-unrelated-hook"}
    legacy = (
        "'/missing checkout/.venv/bin/python' -m spiritboard input-event "
        "--source claude --profile-id synthetic --data-dir '/synthetic board'"
    )
    original = {
        "env": {"SYNTHETIC": "preserved"},
        "hooks": {"Stop": [{"hooks": [unrelated, {"type": "command", "command": legacy}]}]},
    }
    path.write_text(json.dumps(original))
    profile = SourceProfile("synthetic", "claude", "Synthetic", str(agent))
    install([profile], tmp_path / "data")
    first = path.read_text()
    assert legacy not in first
    assert "/missing checkout/" not in first
    assert json.loads(first)["hooks"]["Stop"][0]["hooks"] == [unrelated]
    install([profile], tmp_path / "data")
    assert path.read_text() == first
    install([profile], tmp_path / "data", remove=True)
    assert json.loads(path.read_text()) == {
        "env": original["env"],
        "hooks": {"Stop": [{"hooks": [unrelated]}]},
    }
