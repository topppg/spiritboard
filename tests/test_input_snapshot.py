"""Identical input behavior for all native harnesses without terminal metadata."""

import time
from types import SimpleNamespace

import pytest

from spiritboard.harnesses import HARNESSES
from spiritboard.input_events import InputEvent, InputSignals
from spiritboard.server import Collector
from spiritboard.store import Store


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
