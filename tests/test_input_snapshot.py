"""Identical input behavior for all native harnesses without terminal metadata."""

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from spiritboard.harnesses import HARNESSES
from spiritboard.input_events import InputEvent, InputSignals
from spiritboard.server import Collector
from spiritboard.store import Store


@pytest.fixture
def lifecycle_collector() -> Iterator[tuple[Collector, Store, InputSignals]]:
    store = Store()
    signals = InputSignals()
    server = Collector(store, "synthetic-collector-token-only", input_signals=signals)
    try:
        yield server, store, signals
    finally:
        server.server_close()
        signals.close()
        store.close()


@pytest.mark.parametrize("kind", list(HARNESSES))
@pytest.mark.parametrize(
    ("previous", "action", "expected"),
    [
        ("idle", "resume", "working"),
        ("working", "idle", "idle"),
        ("waiting_tool", "idle", "idle"),
    ],
)
def test_newer_native_lifecycle_updates_state_without_rewriting_transcript(
    lifecycle_collector: tuple[Collector, Store, InputSignals],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    previous: str,
    action: str,
    expected: str,
) -> None:
    server, store, signals = lifecycle_collector
    current = time.time()
    transcript_at = datetime.fromtimestamp(current - 120, UTC).isoformat()
    event_at = datetime.fromtimestamp(current, UTC).isoformat()
    store.observe(
        {
            "run_id": "work:session-111",
            "session_id": "work:observed",
            "source": kind,
            "account": "synthetic",
            "name": "Invented",
            "state": previous,
            "last_seen": transcript_at,
        }
    )
    signals.record(InputEvent("work", "session-111", action, "tool:one", occurred_at=event_at))
    run = server.snapshot()["runs"][0]
    assert run["state"] == expected
    assert run["health"] == "observed"
    assert run["state_observed_at"] == event_at
    assert run["last_seen"] == transcript_at
    assert "Native lifecycle event" in run["evidence"]
    persisted = store.snapshot()["runs"][0]
    assert persisted["state"] == previous
    assert "state_observed_at" not in persisted
    historical = server.snapshot(current + 1)["runs"][0]
    assert historical["state"] == previous
    assert "state_observed_at" not in historical
    monkeypatch.setattr("spiritboard.server.time.time", lambda: current + 61)
    aged = server.snapshot()["runs"][0]
    assert aged["state"] == expected
    assert aged["health"] == "stale"
    assert aged["state_observed_at"] == event_at


@pytest.mark.parametrize("action", ["resume", "idle", "resolve"])
def test_older_native_lifecycle_cannot_replace_newer_transcript(
    lifecycle_collector: tuple[Collector, Store, InputSignals],
    action: str,
) -> None:
    server, store, signals = lifecycle_collector
    current = time.time()
    store.observe(
        {
            "run_id": "work:session-111",
            "session_id": "work:observed",
            "source": "claude",
            "account": "synthetic",
            "name": "Invented",
            "state": "waiting_tool",
            "last_seen": datetime.fromtimestamp(current, UTC).isoformat(),
        }
    )
    signals.record(
        InputEvent(
            "work",
            "session-111",
            action,
            "tool:one",
            occurred_at=datetime.fromtimestamp(current - 1, UTC).isoformat(),
        )
    )
    run = server.snapshot()["runs"][0]
    assert run["state"] == "waiting_tool"
    assert "state_observed_at" not in run


def test_unrelated_tool_completion_does_not_clear_transcript_question(
    lifecycle_collector: tuple[Collector, Store, InputSignals],
) -> None:
    server, store, signals = lifecycle_collector
    store.observe(
        {
            "run_id": "work:session-111",
            "session_id": "work:observed",
            "source": "claude",
            "account": "synthetic",
            "name": "Invented",
            "state": "waiting_user",
            "last_seen": "2026-01-01T00:00:00Z",
            "input_request_ids": ["tool:question"],
        }
    )
    signals.record(InputEvent("work", "session-111", "resolve", "tool:unrelated"))
    assert server.snapshot()["runs"][0]["state"] == "waiting_user"


@pytest.mark.parametrize("state", ["idle", "working", "waiting_tool"])
def test_generic_resolution_does_not_supply_execution_state(
    lifecycle_collector: tuple[Collector, Store, InputSignals],
    state: str,
) -> None:
    server, store, signals = lifecycle_collector
    store.observe(
        {
            "run_id": "work:session-111",
            "session_id": "work:observed",
            "source": "codex",
            "account": "synthetic",
            "name": "Invented",
            "state": state,
            "last_seen": "2026-01-01T00:00:00Z",
        }
    )
    signals.record(InputEvent("work", "session-111", "resolve", "status:question"))
    run = server.snapshot()["runs"][0]
    assert run["state"] == state
    assert run["health"] == "stale"
    assert "state_observed_at" not in run


@pytest.mark.parametrize("action", ["resume", "idle"])
def test_lifecycle_state_survives_later_generic_resolution(
    lifecycle_collector: tuple[Collector, Store, InputSignals],
    action: str,
) -> None:
    server, store, signals = lifecycle_collector
    current = time.time()
    event_at = datetime.fromtimestamp(current - 1, UTC).isoformat()
    store.observe(
        {
            "run_id": "work:session-111",
            "session_id": "work:observed",
            "source": "codex",
            "account": "synthetic",
            "name": "Invented",
            "state": "waiting_tool",
            "last_seen": "2026-01-01T00:00:00Z",
        }
    )
    signals.record(InputEvent("work", "session-111", action, occurred_at=event_at))
    signals.record(InputEvent("work", "session-111", "resolve", "status:question"))
    run = server.snapshot()["runs"][0]
    assert run["state"] == ("working" if action == "resume" else "idle")
    assert run["state_observed_at"] == event_at


@pytest.mark.parametrize("kind", ["pi", "opencode", "claude", "codex"])
@pytest.mark.parametrize("action", ["resume", "idle"])
def test_explicit_lifecycle_overrides_only_heuristic_closed_ownership(
    lifecycle_collector: tuple[Collector, Store, InputSignals],
    kind: str,
    action: str,
) -> None:
    server, store, signals = lifecycle_collector
    server.monitor = SimpleNamespace(
        public_state=lambda: {
            "profiles": [{"id": "work", "kind": kind, "label": "Synthetic"}],
            "source_errors": {},
            "source_stats": [],
            "automatic_discovery": False,
            "live": {"work": frozenset()},
        }
    )
    store.observe(
        {
            "run_id": "work:session-111",
            "session_id": "work:observed",
            "source": kind,
            "account": "synthetic",
            "name": "Invented",
            "state": "working",
            "last_seen": "2026-01-01T00:00:00Z",
        }
    )
    signals.record(InputEvent("work", "session-111", action))
    run = server.snapshot()["runs"][0]
    assert run["live"] is (False if HARNESSES[kind].liveness_exact else None)


def test_pending_input_wins_over_lifecycle_and_unrelated_resolution(
    lifecycle_collector: tuple[Collector, Store, InputSignals],
) -> None:
    server, store, signals = lifecycle_collector
    store.observe(
        {
            "run_id": "work:session-111",
            "session_id": "work:observed",
            "source": "claude",
            "account": "synthetic",
            "name": "Invented",
            "state": "working",
            "last_seen": "2026-01-01T00:00:00Z",
        }
    )
    signals.record(InputEvent("work", "session-111", "resume"))
    signals.record(InputEvent("work", "session-111", "request", "question"))
    signals.record(InputEvent("work", "session-111", "resolve", "unrelated"))
    run = server.snapshot()["runs"][0]
    assert run["state"] == "waiting_user"
    assert run["input_status"]["pending"] == 1


def test_answer_without_new_execution_evidence_retains_explicit_idle(
    lifecycle_collector: tuple[Collector, Store, InputSignals],
) -> None:
    server, store, signals = lifecycle_collector
    store.observe(
        {
            "run_id": "work:session-111",
            "session_id": "work:observed",
            "source": "claude",
            "account": "synthetic",
            "name": "Invented",
            "state": "waiting_user",
            "last_seen": "2026-01-01T00:00:00Z",
            "input_request_ids": ["tool:question"],
        }
    )
    for action, request, stamp in [
        ("idle", "", "2026-01-01T00:00:01Z"),
        ("request", "tool:question", "2026-01-01T00:00:02Z"),
        ("resolve", "tool:question", "2026-01-01T00:00:03Z"),
    ]:
        signals.record(InputEvent("work", "session-111", action, request, occurred_at=stamp))
    run = server.snapshot()["runs"][0]
    assert run["state"] == "idle"
    assert run["state_observed_at"] == "2026-01-01T00:00:01+00:00"
    assert run["health"] == "stale"


@pytest.mark.parametrize("recorded_question", [False, True])
@pytest.mark.parametrize("completion", ["tool:unrelated", "tool:question"])
def test_explicit_idle_survives_late_completion_after_clearing_transcript_question(
    lifecycle_collector: tuple[Collector, Store, InputSignals],
    recorded_question: bool,
    completion: str,
) -> None:
    server, store, signals = lifecycle_collector
    current = time.time()
    event_at = datetime.fromtimestamp(current - 1, UTC).isoformat()
    store.observe(
        {
            "run_id": "work:session-111",
            "session_id": "work:observed",
            "source": "claude",
            "account": "synthetic",
            "name": "Invented",
            "state": "waiting_user",
            "last_seen": "2026-01-01T00:00:00Z",
            "input_request_ids": ["tool:question"],
        }
    )
    if recorded_question:
        signals.record(
            InputEvent(
                "work",
                "session-111",
                "request",
                "tool:question",
                occurred_at=datetime.fromtimestamp(current - 2, UTC).isoformat(),
            )
        )
    signals.record(InputEvent("work", "session-111", "idle", occurred_at=event_at))
    signals.record(InputEvent("work", "session-111", "resolve", completion))
    run = server.snapshot()["runs"][0]
    assert run["state"] == "idle"
    assert run["health"] == "observed"
    assert run["state_observed_at"] == event_at


@pytest.mark.parametrize(
    ("runtime", "flags", "async_question", "expected"),
    [
        ("idle", [], False, "idle"),
        ("idle", [], True, "waiting_user"),
        ("active", ["waitingOnUserInput"], False, "waiting_user"),
        ("active", [], False, "working"),
    ],
)
def test_codex_runtime_status_updates_execution_without_resolving_async_input(
    lifecycle_collector: tuple[Collector, Store, InputSignals],
    runtime: str,
    flags: list[str],
    async_question: bool,
    expected: str,
) -> None:
    from spiritboard.input_codex import events

    server, store, signals = lifecycle_collector
    current = time.time()
    stamp = datetime.fromtimestamp(current, UTC).isoformat()
    store.observe(
        {
            "run_id": "work:session-111",
            "session_id": "work:observed",
            "source": "codex",
            "account": "synthetic",
            "name": "Invented",
            "state": "waiting_tool",
            "last_seen": "2026-01-01T00:00:00Z",
        }
    )
    signals.record(
        InputEvent(
            "work",
            "session-111",
            "resume",
            occurred_at=datetime.fromtimestamp(current - 2, UTC).isoformat(),
        )
    )
    if async_question:
        for event in events(
            "work",
            "item/tool/requestUserInput",
            {
                "id": 42,
                "params": {
                    "threadId": "session-111",
                    "itemId": "async-question",
                    "timestamp": datetime.fromtimestamp(current - 1, UTC).isoformat(),
                },
            },
        ):
            signals.record(event)
    for event in events(
        "work",
        "thread/status/changed",
        {
            "params": {
                "threadId": "session-111",
                "timestamp": stamp,
                "status": {"type": runtime, "activeFlags": flags},
            },
        },
    ):
        signals.record(event)
    run = server.snapshot()["runs"][0]
    assert run["state"] == expected
    if expected != "waiting_user":
        assert run["state_observed_at"] == stamp
        assert run["health"] == "observed"
    else:
        assert run["input_status"]["pending"] == 1


@pytest.mark.parametrize("kind", list(HARNESSES))
def test_unanswered_native_input_overrides_stale_transcript_in_every_harness(kind: str) -> None:
    store = Store()
    signals = InputSignals()
    profile = {"id": "work", "kind": kind, "label": "Synthetic"}
    monitor = SimpleNamespace(
        public_state=lambda: {
            "profiles": [profile],
            "source_errors": {},
            "source_stats": [],
            "automatic_discovery": False,
            "live": {"work": None},
        }
    )
    row = {
        "run_id": "work:session-111",
        "session_id": "work:observed",
        "native_id": "session-111",
        "source": kind,
        "account": "invented",
        "name": "Invented question",
        "state": "working",
        "last_seen": "2026-01-01T00:00:00Z",
    }
    store.observe(row)
    server = Collector(
        store, "synthetic-collector-token-only", monitor=monitor, input_signals=signals
    )
    try:
        signals.record(InputEvent("work", "session-111", "request", "question-one"))
        snapshot = server.snapshot()
        current = snapshot["runs"][0]
        assert (current["state"], current["health"], current["live"]) == (
            "waiting_user",
            "stale",
            None,
        )
        assert current["last_seen"] == row["last_seen"]
        assert current["input_status"]["pending"] == 1
        assert snapshot["profiles"][0]["input_detection"] == "events observed"
        assert store.snapshot()["runs"][0]["state"] == "working"
        assert server.snapshot(time.time())["runs"][0]["state"] == "working"
        assert "input_status" not in server.snapshot(time.time())["runs"][0]
        signals.record(InputEvent("work", "session-111", "resolve", "question-one"))
        assert server.snapshot()["runs"][0]["state"] == "working"
        store.observe({**row, "state": "completed"})
        signals.record(InputEvent("work", "session-111", "request", "question-two"))
        assert server.snapshot()["runs"][0]["state"] == "completed"
    finally:
        server.server_close()
        signals.close()
        store.close()


def test_one_subagent_answer_does_not_clear_another_agents_request() -> None:
    signals = InputSignals()
    try:
        signals.record(InputEvent("work", "session-111:agent:one", "request", "same"))
        signals.record(InputEvent("work", "session-111:agent:two", "request", "same"))
        signals.record(InputEvent("work", "session-111:agent:one", "resolve", "same"))
        assert signals.snapshot()["work:session-111:agent:two"].state == "waiting_user"
    finally:
        signals.close()


@pytest.mark.parametrize("kind", ["pi", "opencode"])
def test_explicit_prompt_is_stronger_than_heuristic_process_ownership(kind: str) -> None:
    signals = InputSignals()
    store = Store()
    observed = {
        "profiles": [{"id": "work", "kind": kind, "label": "Synthetic"}],
        "source_errors": {},
        "source_stats": [],
        "automatic_discovery": False,
        "live": {"work": frozenset()},
    }
    monitor = SimpleNamespace(public_state=lambda: observed)
    store.observe(
        {
            "run_id": "work:session-111",
            "session_id": "work:observed",
            "source": kind,
            "account": "synthetic",
            "name": "Invented",
            "state": "waiting_tool",
            "last_seen": "2026-01-01T00:00:00Z",
        }
    )
    server = Collector(
        store, "synthetic-collector-token-only", monitor=monitor, input_signals=signals
    )
    try:
        signals.record(InputEvent("work", "session-111", "request", "one"))
        run = server.snapshot()["runs"][0]
        assert run["state"] == "waiting_user"
        assert run["live"] is None
    finally:
        server.server_close()
        store.close()
        signals.close()


def test_actual_answer_clears_stale_native_question_without_clearing_unrelated_input() -> None:
    from spiritboard.input_codex import events

    store = Store()
    signals = InputSignals()
    row = {
        "run_id": "work:session-111",
        "session_id": "work:observed",
        "source": "codex",
        "account": "synthetic",
        "name": "Invented",
        "state": "waiting_user",
        "last_seen": "2026-01-01T00:00:00Z",
        "input_request_ids": ["tool:question-one"],
    }
    store.observe(row)
    server = Collector(store, "synthetic-collector-token-only", input_signals=signals)
    try:
        signals.record(InputEvent("work", "session-111", "resolve", "tool:unrelated"))
        assert server.snapshot()["runs"][0]["state"] == "waiting_user"
        request = {"id": 42, "params": {"threadId": "session-111", "itemId": "question-one"}}
        resolution = {"params": {"threadId": "session-111", "requestId": 42}}
        for name, raw in (
            ("item/tool/requestUserInput", request),
            ("serverRequest/resolved", resolution),
        ):
            for event in events("work", name, raw):
                signals.record(event)
        assert server.snapshot()["runs"][0]["state"] == "working"
        assert store.snapshot()["runs"][0]["state"] == "waiting_user"
        store.observe({**row, "input_request_ids": ["tool:question-new"]})
        assert server.snapshot()["runs"][0]["state"] == "waiting_user"
        signals.record(InputEvent("work", "session-111", "resume"))
        assert server.snapshot()["runs"][0]["state"] == "working"
        signals.record(InputEvent("work", "session-111", "close"))
        assert server.snapshot()["runs"][0]["live"] is False
    finally:
        server.server_close()
        signals.close()
        store.close()
