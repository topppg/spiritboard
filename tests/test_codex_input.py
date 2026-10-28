"""Recorded Codex questions keep their waiting state independently of the interface."""

from spiritboard.harnesses.codex import parse
from spiritboard.sources import SourceProfile
from spiritboard.transcripts import READ_GAP

PROFILE = SourceProfile("synthetic", "codex", "Synthetic", "/synthetic/codex", False)
HEADER = {
    "type": "session_meta",
    "payload": {"id": "synthetic-session", "cwd": "/synthetic/project"},
}


def item(kind: str, **fields: object) -> dict[str, object]:
    return {"type": "response_item", "payload": {"type": kind, **fields}}


def event(kind: str, **fields: object) -> dict[str, object]:
    return {"type": "event_msg", "payload": {"type": kind, **fields}}


def state(*rows: dict[str, object]) -> str:
    observed = parse(PROFILE, [HEADER, *rows], 0)
    assert observed is not None
    return str(observed["state"])


def question(call_id: str = "question-one", *, asynchronous: bool = False) -> dict[str, object]:
    name = "request_user_input_async" if asynchronous else "request_user_input"
    return item("function_call", name=name, call_id=call_id, arguments="{}")


def test_recorded_question_needs_user_even_when_text_is_disabled() -> None:
    assert state(question()) == "waiting_user"


def test_question_remains_pending_across_unrelated_activity() -> None:
    assert (
        state(
            question(),
            item("function_call", name="exec_command", call_id="other"),
            item("function_call_output", call_id="other", output="done"),
            item("message", role="assistant", content=[]),
        )
        == "waiting_user"
    )


def test_matching_blocking_answer_clears_only_its_question() -> None:
    answer = item("function_call_output", call_id="question-one", output='{"answers":{}}')
    assert state(question(), answer) == "working"
    assert state(question(), question("question-two"), answer) == "waiting_user"


def test_async_acknowledgement_does_not_resolve_question() -> None:
    assert (
        state(
            question(asynchronous=True),
            item("function_call_output", call_id="question-one", output="Question displayed"),
        )
        == "waiting_user"
    )


def test_async_question_can_outlive_finished_turn() -> None:
    assert state(question(asynchronous=True), event("task_complete")) == "waiting_user"
    assert state(question(asynchronous=True), event("turn_aborted")) == "idle"


def test_real_user_message_and_turn_end_clear_pending_question() -> None:
    assert state(question(), event("user_message", message="A synthetic answer")) == "working"
    assert state(question(), item("message", role="user", content=[])) == "working"
    for name in ("task_complete", "turn_complete", "turn_aborted"):
        assert state(question(), event(name)) == "idle"


def test_scan_gap_drops_questions_from_unread_history() -> None:
    assert state(question(), {"type": READ_GAP}, item("reasoning")) == "working"
    assert state(question(), {"type": READ_GAP}) == "unknown"


def test_older_question_cannot_replace_newer_answered_turn() -> None:
    first = event("user_message", message="Synthetic new turn")
    first["timestamp"] = "2026-10-28T10:00:02Z"
    older = question()
    older["timestamp"] = "2026-10-28T10:00:01Z"
    assert state(first, older) == "working"


def test_only_explicit_named_calls_establish_pending_input() -> None:
    assert state(item("function_call", name="exec", arguments="request_user_input({})")) == (
        "waiting_tool"
    )
    assert state(item("function_call", name="request_user_input", arguments="{}")) == (
        "waiting_tool"
    )
    assert state(item("function_call", name={}, call_id="malformed")) == "waiting_tool"
