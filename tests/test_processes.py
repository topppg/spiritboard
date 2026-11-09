"""System tool discovery must work in a login service's restricted PATH."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from spiritboard import processes
from spiritboard.harnesses import codex
from spiritboard.processes import Process


def test_lsof_falls_back_to_executable_mac_system_tool() -> None:
    with (
        patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}),
        patch("sys.platform", "darwin"),
        patch("shutil.which", return_value=None),
        patch("os.path.isfile", return_value=True),
        patch("os.access", return_value=True) as executable,
    ):
        assert processes.lsof() == "/usr/sbin/lsof"
        executable.assert_called_once_with("/usr/sbin/lsof", os.X_OK)


def test_lsof_prefers_tool_selected_by_path() -> None:
    with (
        patch("shutil.which", return_value="/invented/bin/lsof"),
        patch("os.access", side_effect=AssertionError("PATH already selected a tool")),
    ):
        assert processes.lsof() == "/invented/bin/lsof"


@pytest.mark.parametrize(
    ("platform", "is_file", "executable"),
    [("linux", True, True), ("darwin", False, True), ("darwin", True, False)],
)
def test_lsof_unavailable_is_unknown(platform: str, is_file: bool, executable: bool) -> None:
    with (
        patch("sys.platform", platform),
        patch("shutil.which", return_value=None),
        patch("os.path.isfile", return_value=is_file),
        patch("os.access", return_value=executable),
    ):
        assert processes.lsof() is None


def test_codex_liveness_uses_system_lsof_with_restricted_path(tmp_path: Path) -> None:
    session = "019a0000-aaaa-7bbb-8ccc-dddddddddddd"
    locks = tmp_path / "thread-writer-locks"
    locks.mkdir()
    lock = locks / f"{session}.lock"
    lock.touch()
    with (
        patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}),
        patch("sys.platform", "darwin"),
        patch("shutil.which", return_value=None),
        patch("os.path.isfile", return_value=True),
        patch("os.access", return_value=True),
        patch.object(processes, "run", return_value=(0, f"p42\nccodex\nn{lock}\n")) as run,
    ):
        assert codex.live_sessions(tmp_path) == frozenset({session})
        run.assert_called_once_with(["/usr/sbin/lsof", "-F", "pcn", "--", str(lock)])


def test_codex_rollout_owner_uses_system_lsof_with_restricted_path(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.touch()
    owner = Process(42, 1, "ttys003", "Tue Oct 20 10:00:00 2026", "/invented/bin/codex")
    with (
        patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}),
        patch("sys.platform", "darwin"),
        patch("shutil.which", return_value=None),
        patch("os.path.isfile", return_value=True),
        patch("os.access", return_value=True),
        patch.object(processes, "run", return_value=(0, "42\n")) as run,
        patch.object(processes, "process", return_value=owner),
    ):
        assert codex.rollout_pid(tmp_path, rollout) == (True, 42)
        run.assert_called_once_with(["/usr/sbin/lsof", "-t", "--", str(rollout)])
