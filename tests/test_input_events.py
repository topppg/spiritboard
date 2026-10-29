"""Synthetic native lifecycle events, independent of terminals and transcript writes."""

import sqlite3
from pathlib import Path

import pytest

from spiritboard.input_events import InputEvent, InputSignals


def test_existing_aggregate_status_does_not_backfill_explicit_lifecycle(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "inputs.sqlite3"
    with sqlite3.connect(path) as database:
        database.execute("""
            CREATE TABLE input_runs(profile TEXT NOT NULL,native TEXT NOT NULL,
                reset_at REAL NOT NULL,state TEXT NOT NULL,state_at REAL NOT NULL,
                PRIMARY KEY(profile,native))
        """)
        database.execute("INSERT INTO input_runs VALUES('work','session-111',123,'working',124)")
    path.chmod(0o600)
    signals = InputSignals(path)
    try:
        status = signals.snapshot()["work:session-111"]
        assert status.lifecycle_state == ""
        assert status.lifecycle_at == ""
    finally:
        signals.close()


@pytest.mark.parametrize("action", ["resume", "idle", "close", "active", "inactive"])
def test_explicit_lifecycle_persists_separately_from_request_resolution(
    tmp_path: Path,
    action: str,
) -> None:
    path = tmp_path / "private" / "inputs.sqlite3"
    signals = InputSignals(path)
    stamp = "2026-01-01T00:00:02+00:00"
    signals.record(InputEvent("work", "session-111", action, occurred_at=stamp))
    signals.record(InputEvent("work", "session-111", "resolve", "same-time", occurred_at=stamp))
    signals.record(
        InputEvent(
            "work",
            "session-111",
            "resolve",
            "later",
            occurred_at="2026-01-01T00:00:03Z",
        )
    )
    signals.record(
        InputEvent(
            "work",
            "session-111",
            "resume",
            occurred_at="2026-01-01T00:00:01Z",
        )
    )
    signals.close()
    signals = InputSignals(path)
    try:
        status = signals.snapshot()["work:session-111"]
        assert status.state == "working"
        assert (
            status.lifecycle_state
            == {
                "resume": "working",
                "idle": "idle",
                "close": "closed",
                "active": "working",
                "inactive": "idle",
            }[action]
        )
        assert status.lifecycle_at == stamp
        signals.prune(before="2026-01-03T00:00:00Z")
        assert signals.db.execute("SELECT COUNT(*) FROM input_lifecycle").fetchone()[0] == 0
    finally:
        signals.close()


def test_resolution_has_no_lifecycle_and_disabling_profile_removes_lifecycle() -> None:
    signals = InputSignals()
    try:
        signals.record(InputEvent("work", "session-111", "resolve", "one"))
        assert signals.snapshot()["work:session-111"].lifecycle_state == ""
        signals.record(InputEvent("work", "session-111", "idle"))
        signals.configure("work", "synthetic", enabled=False)
        assert signals.db.execute("SELECT COUNT(*) FROM input_lifecycle").fetchone()[0] == 0
    finally:
        signals.close()


def test_pending_questions_survive_time_other_requests_and_restarts(tmp_path: Path) -> None:
    path = tmp_path / "private" / "inputs.sqlite3"
    events = InputSignals(path)
    events.record(
        InputEvent("work", "session-111", "request", "one", occurred_at="2026-01-01T00:00:00Z")
    )
    events.record(
        InputEvent(
            "work",
            "session-111",
            "request",
            "two",
            kind="approval",
            occurred_at="2026-01-01T00:00:01Z",
        )
    )
    events.record(
        InputEvent("work", "session-111", "resolve", "two", occurred_at="2026-01-01T00:00:02Z")
    )
    events.close()
    events = InputSignals(path)
    try:
        state = events.snapshot()["work:session-111"]
        assert state.state == "waiting_user"
        assert state.pending == 1
        events.record(
            InputEvent("work", "session-111", "resolve", "one", occurred_at="2026-01-01T00:00:03Z")
        )
        assert events.snapshot()["work:session-111"].state == "working"
    finally:
        events.close()


def test_new_turn_clears_old_requests_but_late_events_do_not_reopen_them() -> None:
    events = InputSignals()
    try:
        events.record(
            InputEvent("work", "session-111", "request", "one", occurred_at="2026-01-01T00:00:00Z")
        )
        events.record(
            InputEvent("work", "session-111", "resume", occurred_at="2026-01-01T00:00:02Z")
        )
        events.record(
            InputEvent("work", "session-111", "request", "old", occurred_at="2026-01-01T00:00:01Z")
        )
        assert events.snapshot()["work:session-111"].state == "working"
        events.record(
            InputEvent("work", "session-111", "resolve", "new", occurred_at="2026-01-01T00:00:04Z")
        )
        events.record(
            InputEvent("work", "session-111", "request", "new", occurred_at="2026-01-01T00:00:03Z")
        )
        assert events.snapshot()["work:session-111"].pending == 0
    finally:
        events.close()


def test_profiles_and_subagents_are_independent_and_end_clears_only_its_session() -> None:
    events = InputSignals()
    try:
        for profile, native in (
            ("work", "session-111"),
            ("personal", "session-111"),
            ("work", "session-111:agent:child"),
        ):
            events.record(InputEvent(profile, native, "request", "same"))
        events.record(InputEvent("work", "session-111", "idle"))
        states = events.snapshot()
        assert states["work:session-111"].state == "idle"
        assert states["personal:session-111"].state == "waiting_user"
        assert states["work:session-111:agent:child"].state == "waiting_user"
    finally:
        events.close()


def test_signal_contract_rejects_unknown_fields_states_and_unsafe_storage(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        InputEvent("bad:profile", "native", "request", "one")
    with pytest.raises(ValueError):
        InputEvent("profile", "native", "guessed")
    with pytest.raises(ValueError):
        InputEvent("profile", "native", "request")
    outside = tmp_path / "outside"
    outside.write_text("PRIVATE")
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    (directory / "inputs.sqlite3").symlink_to(outside)
    with pytest.raises(OSError):
        InputSignals(directory / "inputs.sqlite3")
    assert outside.read_text() == "PRIVATE"


def test_repeated_permission_can_reopen_but_old_completion_history_is_bounded() -> None:
    signals = InputSignals()
    try:
        signals.record(
            InputEvent(
                "work",
                "session-111",
                "request",
                "permission-hash",
                occurred_at="2026-01-01T00:00:00Z",
            )
        )
        signals.record(
            InputEvent(
                "work",
                "session-111",
                "resolve",
                "permission-hash",
                occurred_at="2026-01-01T00:00:01Z",
            )
        )
        signals.record(
            InputEvent(
                "work",
                "session-111",
                "request",
                "permission-hash",
                occurred_at="2026-01-01T00:00:02Z",
            )
        )
        assert signals.snapshot()["work:session-111"].pending == 1
        signals.record(
            InputEvent(
                "work", "session-111", "resolve", "unrelated", occurred_at="2026-01-01T00:00:03Z"
            )
        )
        signals.prune(before="2026-01-03T00:00:00Z")
        assert signals.snapshot()["work:session-111"].pending == 1
        assert signals.db.execute("SELECT COUNT(*) FROM input_requests").fetchone()[0] == 1
        signals.record(
            InputEvent("work", "session-111", "idle", occurred_at="2026-01-04T00:00:00Z")
        )
        signals.prune(before="2026-01-05T00:00:00Z")
        assert not signals.snapshot()
    finally:
        signals.close()


def test_answer_is_retained_while_native_transcript_still_lists_that_question() -> None:
    signals = InputSignals()
    try:
        signals.record(
            InputEvent(
                "work", "session-111", "request", "tool:one", occurred_at="2026-01-01T00:00:00Z"
            )
        )
        signals.record(
            InputEvent(
                "work", "session-111", "resolve", "tool:one", occurred_at="2026-01-01T00:00:01Z"
            )
        )
        signals.prune(
            before="2026-02-01T00:00:00Z", native_requests={"work:session-111": ["tool:one"]}
        )
        assert "tool:one" in signals.snapshot()["work:session-111"].resolved_ids
        signals.prune(before="2026-02-01T00:00:00Z")
        assert not signals.snapshot()
    finally:
        signals.close()
