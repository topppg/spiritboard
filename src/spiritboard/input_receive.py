"""Best-effort local hook receiver; stdin and exceptions never become output."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TextIO

from . import input_claude, input_codex, input_extensions
from .input_events import ACTIONS, InputEvent, InputSignals

MAX_INPUT = 1_048_576


def receive(
    source: str, profile_id: str, event_name: str | None, directory: Path, stream: TextIO
) -> int:
    signals = None
    try:
        raw = stream.read(MAX_INPUT + 1)
        if len(raw.encode("utf-8")) > MAX_INPUT:
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        event_name = event_name or payload.get("hook_event_name") or payload.get("method")
        if not isinstance(event_name, str):
            return 0
        if event_name in ACTIONS:
            native, request, kind = (
                payload.get("session_id"),
                payload.get("request_id", ""),
                payload.get("kind", "question"),
            )
            stamp = payload.get("occurred_at", "")
            alias = payload.get("alias_id", "")
            if not all(isinstance(value, str) for value in (native, request, kind, stamp, alias)):
                return 0
            events = [InputEvent(profile_id, native, event_name, request, kind, stamp, alias)]
        elif source == "claude":
            events = input_claude.events(profile_id, event_name, payload)
        elif source == "codex":
            events = input_codex.events(profile_id, event_name, payload)
        elif source in {"opencode", "pi"}:
            events = input_extensions.events(profile_id, event_name, payload)
        else:
            return 0
        if events:
            signals = InputSignals(directory / "inputs.sqlite3")
            for event in events:
                signals.record(event)
    except (OSError, ValueError, TypeError, RecursionError, sqlite3.Error):
        pass
    finally:
        if signals:
            signals.close()
    return 0
