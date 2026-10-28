"""Native callback stdin is reduced to metadata before any disk write."""

import contextlib
import io
import json
from pathlib import Path

from spiritboard.cli import main
from spiritboard.input_events import InputSignals


def test_real_cli_receiver_persists_only_metadata_and_resolves_matching_question(
    tmp_path: Path,
) -> None:
    data = tmp_path / "board"
    arguments = [
        "input-event",
        "--source",
        "claude",
        "--profile-id",
        "work",
        "--data-dir",
        str(data),
    ]
    payload = {
        "session_id": "session-111",
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_use_id": "question-one",
        "tool_input": {
            "questions": [
                {"question": "PRIVATE question", "options": [{"label": "PRIVATE answer"}]}
            ]
        },
    }
    with (
        contextlib.redirect_stdout(io.StringIO()) as output,
        contextlib.redirect_stderr(io.StringIO()),
    ):
        from unittest.mock import patch

        with patch("sys.stdin", io.StringIO(json.dumps(payload))):
            assert main(arguments) == 0
        assert output.getvalue() == ""
        signals = InputSignals(data / "inputs.sqlite3")
        assert signals.snapshot()["work:session-111"].state == "waiting_user"
        payload["hook_event_name"] = "PostToolUse"
        with patch("sys.stdin", io.StringIO(json.dumps(payload))):
            assert main(arguments) == 0
        assert signals.snapshot()["work:session-111"].pending == 0
        signals.close()
    assert all(b"PRIVATE" not in path.read_bytes() for path in data.iterdir() if path.is_file())


def test_callback_failures_never_emit_hook_decisions_or_private_errors(tmp_path: Path) -> None:
    from unittest.mock import patch

    for raw in (
        "{broken PRIVATE",
        "[]",
        "x" * 1_048_577,
        json.dumps({"session_id": "--invalid", "hook_event_name": "request", "request_id": "one"}),
    ):
        with (
            patch("sys.stdin", io.StringIO(raw)),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            assert (
                main(
                    [
                        "input-event",
                        "--source",
                        "claude",
                        "--profile-id",
                        "work",
                        "--data-dir",
                        str(tmp_path / "board"),
                    ]
                )
                == 0
            )
            assert output.getvalue() == ""


def test_generic_contract_supports_hosts_independent_of_their_native_adapter(
    tmp_path: Path,
) -> None:
    from unittest.mock import patch

    data = tmp_path / "board"
    for source in ("claude", "codex", "opencode", "pi", "dsh", "grokbot"):
        with patch(
            "sys.stdin",
            io.StringIO(
                json.dumps({"session_id": "session-111", "request_id": "one", "kind": "approval"})
            ),
        ):
            assert (
                main(
                    [
                        "input-event",
                        "--source",
                        source,
                        "--profile-id",
                        source,
                        "--event",
                        "request",
                        "--data-dir",
                        str(data),
                    ]
                )
                == 0
            )
    signals = InputSignals(data / "inputs.sqlite3")
    try:
        assert len(signals.snapshot()) == 6
        assert all(row.state == "waiting_user" for row in signals.snapshot().values())
    finally:
        signals.close()
