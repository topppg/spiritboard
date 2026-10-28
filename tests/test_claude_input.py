"""Recorded Claude input requests keep their state across unrelated transcript activity."""

import json
from pathlib import Path

import pytest

from spiritboard.harnesses import claude
from spiritboard.sources import SourceProfile
from spiritboard.transcripts import READ_GAP


def row(kind: str, second: int, content: object, **fields: object) -> dict[str, object]:
    return {
        "sessionId": "synthetic-session",
        "type": kind,
        "timestamp": f"2026-10-20T10:00:{second:02d}Z",
        "message": {"content": content},
        **fields,
    }


def question(
    second: int = 2, tool_id: str = "question-one", name: str = "AskUserQuestion"
) -> dict[str, object]:
    return row(
        "assistant",
        second,
        [
            {
                "type": "tool_use",
                "id": tool_id,
                "name": name,
                "input": {"secret": "PRIVATE QUESTION"},
            }
        ],
    )


def result(tool_id: str, second: int = 3) -> dict[str, object]:
    return row(
        "user",
        second,
        [{"type": "tool_result", "tool_use_id": tool_id, "content": "PRIVATE ANSWER"}],
    )


def observe(rows: list[dict[str, object]], *, include_text: bool = True) -> dict[str, object]:
    profile = SourceProfile(
        "synthetic", "claude", "Synthetic", str(Path("/tmp/invented")), include_text
    )
    value = claude.parse(profile, rows, 0.0)
    assert value is not None
    return value


@pytest.mark.parametrize("name", ["AskUserQuestion", "ExitPlanMode"])
def test_recorded_interactive_tool_needs_user_in_every_interface(name: str) -> None:
    for interface in ("terminal", "cmux", "vscode", "claude-desktop", "custom"):
        value = question(name=name)
        value["entrypoint"] = interface
        assert observe([value])["state"] == "waiting_user"


def test_unrelated_results_and_assistant_activity_keep_question_pending() -> None:
    rows = [question(), result("other-tool")]
    assert observe(rows)["state"] == "waiting_user"
    rows.append(row("assistant", 4, [{"type": "text", "text": "Checking another branch"}]))
    assert observe(rows)["state"] == "waiting_user"
    rows.append(question(5, "other-tool", "Read"))
    assert observe(rows)["state"] == "waiting_user"


def test_only_matching_results_clear_each_outstanding_question() -> None:
    rows = [question(), question(3, "question-two"), result("question-one", 4)]
    assert observe(rows)["state"] == "waiting_user"
    rows.append(result("question-two", 5))
    assert observe(rows)["state"] == "working"


def test_one_result_message_can_answer_multiple_questions() -> None:
    rows = [question(), question(3, "question-two")]
    rows.append(
        row(
            "user",
            4,
            [
                {"type": "tool_result", "tool_use_id": "question-one"},
                {"type": "tool_result", "tool_use_id": "question-two"},
            ],
        )
    )
    assert observe(rows)["state"] == "working"


def test_genuine_new_prompt_clears_pending_questions() -> None:
    assert observe([question(), row("user", 3, "Try another approach")])["state"] == "working"
    assert observe([question(), row("user", 3, [{"type": "image"}])])["state"] == "working"


def test_repeated_question_after_answer_is_pending_again() -> None:
    assert observe([question(), result("question-one"), question(4)])["state"] == "waiting_user"


@pytest.mark.parametrize("flag", ["isMeta", "isCompactSummary"])
def test_injected_user_messages_do_not_answer_questions(flag: str) -> None:
    assert (
        observe([question(), row("user", 3, "Injected context", **{flag: True})])["state"]
        == "waiting_user"
    )


@pytest.mark.parametrize("stop", ["end_turn", "stop_sequence", "max_tokens"])
def test_end_of_turn_clears_pending_questions(stop: str) -> None:
    ended = row("assistant", 3, [])
    ended["message"] = {"content": [], "stop_reason": stop}
    assert observe([question(), ended])["state"] == "idle"


def test_turn_duration_clears_pending_questions() -> None:
    assert observe([question(), row("system", 3, [], subtype="turn_duration")])["state"] == "idle"


def test_read_gap_discards_header_wait_until_newer_evidence() -> None:
    rows = [question(), {"type": READ_GAP}]
    assert observe(rows)["state"] == "unknown"
    rows.append({"type": "custom-title", "customTitle": "Synthetic title"})
    assert observe(rows)["state"] == "unknown"
    rows.append(result("unrelated-tool", 5))
    assert observe(rows)["state"] == "working"
    rows.append(question(6, "new-question"))
    assert observe(rows)["state"] == "waiting_user"


def test_old_result_prompt_and_turn_end_cannot_clear_newer_question() -> None:
    for old in (
        result("question-one", 1),
        row("user", 1, "Older request"),
        row("system", 1, [], subtype="turn_duration"),
    ):
        assert observe([question(5), old])["state"] == "waiting_user"


def test_old_question_cannot_reopen_newer_resolved_state() -> None:
    assert observe([question(2), result("question-one", 5), question(3)])["state"] == "working"


def test_private_profiles_detect_waiting_without_exporting_questions_or_answers() -> None:
    rows = [question(), result("unrelated-tool")]
    shown = observe(rows)
    hidden = observe(rows, include_text=False)
    assert shown["state"] == hidden["state"] == "waiting_user"
    assert "PRIVATE QUESTION" not in json.dumps(shown)
    assert "PRIVATE ANSWER" not in json.dumps(hidden)


def test_sidechain_keeps_its_own_waiting_identity() -> None:
    child = {**question(), "isSidechain": True, "agentId": "synthetic-child"}
    value = observe([child])
    assert value["native_id"] == "synthetic-session:agent:synthetic-child"
    assert value["state"] == "waiting_user"


def test_missing_or_invalid_request_id_does_not_create_uncorrelated_wait() -> None:
    for tool_id in ("", "../invalid"):
        assert observe([question(tool_id=tool_id)])["state"] == "waiting_tool"
