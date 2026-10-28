"""Private, interface-independent lifecycle signals; never stores prompts or tool payloads."""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .launcher import LaunchError, _private_directory, _regular_descriptor
from .model import now, text, timestamp

ACTIONS = {"request", "resolve", "resume", "idle", "close"}


@dataclass(frozen=True)
class InputEvent:
    profile_id: str
    native_id: str
    action: str
    request_id: str = ""
    kind: str = "question"
    occurred_at: str = ""
    alias_id: str = ""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", self.profile_id):
            raise ValueError("invalid input profile")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}", self.native_id):
            raise ValueError("invalid native input identity")
        if self.action not in ACTIONS or self.kind not in {"question", "approval"}:
            raise ValueError("invalid input event")
        text(self.request_id, 256, empty=self.action not in {"request", "resolve"})
        text(self.alias_id, 256, empty=True)
        stamp = timestamp(self.occurred_at or now())
        object.__setattr__(self, "occurred_at", datetime.fromtimestamp(stamp, UTC).isoformat())


@dataclass(frozen=True)
class InputStatus:
    state: str
    occurred_at: str
    pending: int
    resolved_ids: frozenset[str] = frozenset()
    cleared_at: float = 0.0


class InputSignals:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            destination = Path(path)
            try:
                _private_directory(destination.parent)
                os.close(_regular_descriptor(destination, create=True))
            except LaunchError:
                raise OSError("Input status storage is unavailable") from None
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            str(path), timeout=1, check_same_thread=False, isolation_level=None
        )
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS input_runs(
                profile TEXT NOT NULL, native TEXT NOT NULL, reset_at REAL NOT NULL,
                state TEXT NOT NULL, state_at REAL NOT NULL, PRIMARY KEY(profile,native));
            CREATE TABLE IF NOT EXISTS input_requests(
                profile TEXT NOT NULL, native TEXT NOT NULL, request TEXT NOT NULL,
                kind TEXT NOT NULL, asked_at REAL, resolved_at REAL,
                PRIMARY KEY(profile,native,request));
            CREATE TABLE IF NOT EXISTS input_integrations(
                profile TEXT PRIMARY KEY, source TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS input_aliases(
                profile TEXT NOT NULL, native TEXT NOT NULL, alias TEXT NOT NULL,
                request TEXT NOT NULL, PRIMARY KEY(profile,native,alias));
            CREATE INDEX IF NOT EXISTS pending_input ON input_requests(profile,native,asked_at)
                WHERE resolved_at IS NULL;
        """)

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def configure(self, profile_id: str, source: str, *, enabled: bool = True) -> None:
        with self.lock:
            if enabled:
                self.db.execute(
                    "INSERT OR REPLACE INTO input_integrations VALUES(?,?)", (profile_id, source)
                )
            else:
                self.db.execute("DELETE FROM input_integrations WHERE profile=?", (profile_id,))
                self.db.execute("DELETE FROM input_requests WHERE profile=?", (profile_id,))
                self.db.execute("DELETE FROM input_runs WHERE profile=?", (profile_id,))
                self.db.execute("DELETE FROM input_aliases WHERE profile=?", (profile_id,))

    def coverage(self) -> dict[str, str]:
        with self.lock:
            configured = {
                row[0]: "configured"
                for row in self.db.execute("SELECT profile FROM input_integrations")
            }
            configured.update(
                {
                    row[0]: "events observed"
                    for row in self.db.execute("SELECT DISTINCT profile FROM input_runs")
                }
            )
            return configured

    def record(self, event: InputEvent) -> None:
        stamp = timestamp(event.occurred_at)
        state = (
            "closed" if event.action == "close" else "idle" if event.action == "idle" else "working"
        )
        reset = stamp if event.action in {"resume", "idle", "close"} else 0.0
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                request_id = event.alias_id or event.request_id
                known = self.db.execute(
                    "SELECT request FROM input_aliases WHERE profile=? AND native=? AND alias=?",
                    (event.profile_id, event.native_id, request_id),
                ).fetchone()
                if known:
                    request_id = known[0]
                if event.alias_id and event.request_id != request_id:
                    self.db.execute(
                        "INSERT OR REPLACE INTO input_aliases VALUES(?,?,?,?)",
                        (event.profile_id, event.native_id, event.request_id, request_id),
                    )
                    earlier = self.db.execute(
                        """
                        SELECT asked_at,resolved_at FROM input_requests
                        WHERE profile=? AND native=? AND request IN (?,?)
                        """,
                        (event.profile_id, event.native_id, event.request_id, request_id),
                    ).fetchall()
                    if earlier:
                        asked = max((row[0] for row in earlier if row[0] is not None), default=None)
                        resolved = max(
                            (row[1] for row in earlier if row[1] is not None), default=None
                        )
                        if asked is not None and resolved is not None and resolved < asked:
                            resolved = None
                        self.db.execute(
                            "INSERT OR REPLACE INTO input_requests VALUES(?,?,?,?,?,?)",
                            (
                                event.profile_id,
                                event.native_id,
                                request_id,
                                event.kind,
                                asked,
                                resolved,
                            ),
                        )
                        self.db.execute(
                            "DELETE FROM input_requests WHERE profile=? AND native=? AND request=?",
                            (event.profile_id, event.native_id, event.request_id),
                        )
                self.db.execute(
                    """
                    INSERT INTO input_runs VALUES(?,?,?,?,?)
                    ON CONFLICT(profile,native) DO UPDATE SET
                        reset_at=MAX(reset_at,excluded.reset_at),
                        state=CASE WHEN excluded.state_at>=state_at
                            THEN excluded.state ELSE state END,
                        state_at=MAX(state_at,excluded.state_at)
                    """,
                    (event.profile_id, event.native_id, reset, state, stamp),
                )
                if event.action == "request":
                    self.db.execute(
                        """
                        INSERT INTO input_requests VALUES(?,?,?,?,?,NULL)
                        ON CONFLICT(profile,native,request) DO UPDATE SET
                            kind=excluded.kind,
                            asked_at=MAX(COALESCE(asked_at,0),excluded.asked_at),
                            resolved_at=CASE WHEN excluded.asked_at>resolved_at
                                THEN NULL ELSE resolved_at END
                        """,
                        (event.profile_id, event.native_id, request_id, event.kind, stamp),
                    )
                elif event.action == "resolve":
                    self.db.execute(
                        """
                        INSERT INTO input_requests VALUES(?,?,?,?,NULL,?)
                        ON CONFLICT(profile,native,request) DO UPDATE SET
                            resolved_at=CASE WHEN asked_at>excluded.resolved_at THEN resolved_at
                                ELSE MAX(COALESCE(resolved_at,0),excluded.resolved_at) END
                        """,
                        (event.profile_id, event.native_id, request_id, event.kind, stamp),
                    )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def snapshot(self) -> dict[str, InputStatus]:
        with self.lock:
            rows = self.db.execute("""
                SELECT r.profile,r.native,r.state,r.state_at,
                    COUNT(q.request),MAX(q.asked_at),r.reset_at
                FROM input_runs r LEFT JOIN input_requests q
                    ON r.profile=q.profile AND r.native=q.native
                    AND q.asked_at>r.reset_at AND q.resolved_at IS NULL
                GROUP BY r.profile,r.native
                """).fetchall()
            resolved: dict[str, set[str]] = {}
            for profile, native, request in self.db.execute("""
                SELECT q.profile,q.native,q.request FROM input_requests q JOIN input_runs r
                ON q.profile=r.profile AND q.native=r.native
                WHERE q.resolved_at IS NOT NULL OR q.asked_at<=r.reset_at
                """):
                resolved.setdefault(f"{profile}:{native}", set()).add(request)
        return {
            f"{profile}:{native}": InputStatus(
                "waiting_user" if pending else state,
                datetime.fromtimestamp(max(state_at, asked or 0), UTC).isoformat(),
                pending,
                frozenset(resolved.get(f"{profile}:{native}", ())),
                reset_at,
            )
            for profile, native, state, state_at, pending, asked, reset_at in rows
        }

    def prune(
        self, *, before: str | None = None, native_requests: dict[str, list[str]] | None = None
    ) -> None:
        """Retain one day of resolved ordering metadata; unresolved questions never expire."""
        cutoff = timestamp(before) if before else time.time() - 86400
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS protected_input("
                    "run TEXT, request TEXT, PRIMARY KEY(run,request))"
                )
                self.db.execute("DELETE FROM protected_input")
                self.db.executemany(
                    "INSERT OR IGNORE INTO protected_input VALUES(?,?)",
                    [
                        (run, request)
                        for run, requests in (native_requests or {}).items()
                        for request in requests
                    ],
                )
                self.db.execute(
                    """
                    DELETE FROM input_requests AS q WHERE resolved_at<? AND NOT EXISTS(
                        SELECT 1 FROM protected_input p
                        WHERE p.run=q.profile||':'||q.native AND p.request=q.request)
                    """,
                    (cutoff,),
                )
                self.db.execute(
                    """
                    DELETE FROM input_requests WHERE EXISTS(
                        SELECT 1 FROM input_runs r WHERE r.profile=input_requests.profile
                        AND r.native=input_requests.native AND r.reset_at<?
                        AND input_requests.asked_at<=r.reset_at) AND NOT EXISTS(
                            SELECT 1 FROM protected_input p
                            WHERE p.run=input_requests.profile||':'||input_requests.native
                            AND p.request=input_requests.request)
                    """,
                    (cutoff,),
                )
                self.db.execute("""
                    DELETE FROM input_aliases WHERE NOT EXISTS(
                        SELECT 1 FROM input_requests q WHERE q.profile=input_aliases.profile
                        AND q.native=input_aliases.native AND q.request=input_aliases.request)
                    """)
                self.db.execute(
                    """
                    DELETE FROM input_runs WHERE state_at<? AND NOT EXISTS(
                        SELECT 1 FROM input_requests q WHERE q.profile=input_runs.profile
                        AND q.native=input_runs.native AND q.asked_at>input_runs.reset_at
                        AND q.resolved_at IS NULL) AND NOT EXISTS(
                            SELECT 1 FROM protected_input p
                            WHERE p.run=input_runs.profile||':'||input_runs.native)
                    """,
                    (cutoff,),
                )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
