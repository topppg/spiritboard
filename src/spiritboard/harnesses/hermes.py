"""Hermes Agent's canonical SQLite transcripts and optional local ownership registry."""

from __future__ import annotations

import json
import math
import os
import shlex
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .. import processes
from ..transcripts import (
    TEXT_HIDDEN,
    Session,
    native_id,
    ns,
    observation,
    parts,
    redact,
    reply_text,
    request_text,
    safe,
    small_json,
    valid_cwd,
)
from .harness import SESSION_ID, Goal, Harness, shell_resume

if TYPE_CHECKING:
    from ..sources import SourceProfile

DATABASE = "state.db"
MAX_PROFILES = 100
MAX_MESSAGES = 200
RECENT_MESSAGES = 40
MAX_TEXT = 16_000
COLUMNS = {
    "sessions": {
        "id",
        "source",
        "parent_session_id",
        "title",
        "started_at",
        "ended_at",
        "cwd",
        "last_activity_at",
        "archived",
    },
    "messages": {
        "id",
        "session_id",
        "role",
        "content",
        "tool_calls",
        "timestamp",
        "finish_reason",
        "active",
    },
}


def home_roots(home: Path) -> list[tuple[str, Path]]:
    root = home / ".hermes"
    found = [("Hermes", root)]
    profiles = root / "profiles"
    if profiles.is_symlink():
        return found
    try:
        with os.scandir(profiles) as entries:
            names = sorted(e.name for e in entries if e.is_dir(follow_symlinks=False))
    except OSError:
        return found
    found.extend((f"Hermes · {name}", profiles / name) for name in names[:MAX_PROFILES])
    return found


def database(root: Path) -> Path:
    return root if root.name == DATABASE and not root.is_dir() else root / DATABASE


def is_root(root: Path) -> bool:
    try:
        return stat.S_ISREG(database(root).lstat().st_mode)
    except OSError:
        return False


def _guard(action: int, table: str | None, column: str | None, *_: str | None) -> int:
    if action == sqlite3.SQLITE_READ and column not in COLUMNS.get(table, set()):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


@contextmanager
def connect(root: Path) -> Iterator[sqlite3.Connection]:
    """Read canonical tables only; never initialize, migrate, repair or checkpoint."""
    path = Path(os.path.abspath(database(root)))
    if not stat.S_ISREG(path.lstat().st_mode):
        raise OSError("Hermes database is not a regular file")
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error as error:
        raise OSError("Hermes database is unreadable") from error
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.set_authorizer(_guard)
        yield conn
    except sqlite3.Error as error:
        raise OSError("Hermes database query failed") from error
    finally:
        conn.close()


def supported(conn: sqlite3.Connection) -> bool:
    return all(
        needed <= {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for table, needed in COLUMNS.items()
    )


def _iso(value: object) -> str | None:
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _signature(path: Path) -> int:
    # WAL writes can change titles, active generations or existing content without a new message.
    stamps = []
    for file in (path, path.with_name(path.name + "-wal")):
        try:
            info = file.lstat()
            stamps.append((info.st_ino, info.st_size, info.st_mtime_ns))
        except FileNotFoundError:
            stamps.append((0, 0, 0))
    return hash(tuple(stamps))


def inventory(
    root: Path, limit: int, max_age_days: float | None = None, archived: bool = True
) -> tuple[list[Session], dict]:
    db = database(root)
    diagnostics: dict = {
        "files_seen": 0,
        "selected_files": 0,
        "truncated": False,
        "scan_limit": limit,
        "discovery_limited": False,
        "transcript_trees": [DATABASE],
        "tree_count": 1,
        "max_age_days": max_age_days,
        "aged_out": 0,
        "archived_skipped": 0,
        "unsupported_schema": False,
    }
    cutoff = time.time() - max_age_days * 86400 if max_age_days else 0
    where = "1" if archived else "coalesce(archived, 0) = 0"
    updated = "coalesce(last_activity_at, started_at)"
    # Capture before the read: a concurrent write will invalidate the next scan's cache.
    signature = _signature(db)
    with connect(root) as conn:
        if not supported(conn):
            diagnostics["unsupported_schema"] = True
            return [], diagnostics
        total, aged = conn.execute(
            f"SELECT COUNT(id), SUM({updated} < ?) FROM sessions WHERE {where}", (cutoff,)
        ).fetchone()
        if not archived:
            diagnostics["archived_skipped"] = conn.execute(
                "SELECT COUNT(id) FROM sessions WHERE coalesce(archived, 0) != 0"
            ).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, {updated} FROM sessions WHERE {where} AND {updated} >= ? "
            f"ORDER BY {updated} DESC, id DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    sessions = [
        Session(sid, db, signature, int(stamp * 1e9), stamp)
        for sid, stamp in rows
        if native_id(sid) and _iso(stamp)
    ]
    available = total - (aged or 0)
    diagnostics.update(
        files_seen=available,
        selected_files=len(sessions),
        truncated=available > len(sessions),
        aged_out=int(aged or 0) if cutoff else 0,
    )
    return sessions, diagnostics


def _session(conn: sqlite3.Connection, native: str) -> tuple | None:
    return conn.execute(
        "SELECT id, source, parent_session_id, substr(title, 1, 300), started_at, ended_at, "
        "cwd, last_activity_at, archived FROM sessions WHERE id = ?",
        (native,),
    ).fetchone()


def _content(raw: object) -> object:
    if not isinstance(raw, bytes):
        return ""
    value = raw.decode("utf-8", errors="replace")
    if value.startswith("\x00json:"):
        try:
            decoded = json.loads(value[6:])
        except (ValueError, RecursionError):
            return ""
        return " ".join(parts(decoded))
    return value


def _messages(
    conn: sqlite3.Connection, native: str, *, text: bool = True, users: bool = False
) -> list[tuple]:
    # BLOB substr preserves Hermes's NUL-prefixed structured content. Tool I/O stays unselected.
    content = (
        f"CASE WHEN role IN ('user', 'assistant') "
        f"THEN substr(CAST(content AS BLOB), 1, {MAX_TEXT}) END"
        if text
        else "NULL"
    )
    where = "AND role = 'user'" if users else ""
    return conn.execute(
        f"SELECT role, {content}, timestamp, finish_reason, "
        "CASE WHEN json_valid(tool_calls) THEN json_array_length(tool_calls) > 0 ELSE 0 END, "
        "length(CAST(content AS BLOB)) > 0 "
        f"FROM messages WHERE session_id = ? AND active = 1 {where} "
        "ORDER BY id DESC LIMIT ?",
        (native, MAX_MESSAGES if users else RECENT_MESSAGES),
    ).fetchall()


def observe(profile: SourceProfile, session: Session) -> dict | None:
    with connect(session.path) as conn:
        row = _session(conn, session.key)
        if row is None or not native_id(row[0]):
            return None
        messages = _messages(conn, session.key, text=profile.include_text)
    sid, source, parent, title, started, ended, directory, updated, archived = row
    cwd = valid_cwd(directory)
    out = observation(
        profile, "Hermes", sid, safe(Path(cwd).name, 80) if cwd else "", _iso(started)
    )
    out["last_seen"] = max(
        (
            stamp
            for value in (started, updated, ended, *(m[2] for m in messages))
            if (stamp := _iso(value))
        ),
        default=out["started_at"],
    )
    if archived:
        out["archived"] = True
    if source == "subagent" and native_id(parent) and parent != sid:
        out["parent_run_id"] = ns(profile, parent)
    request = reply = ""
    if profile.include_text:
        out["name"] = safe(title, 120) or out["name"]
        for role, content, *_ in messages:
            if role == "user":
                request = request_text(_content(content))
                if request:
                    break
            elif role == "assistant" and not reply:
                reply = reply_text(_content(content))
        out["task"] = request or TEXT_HIDDEN
    last = next((m for m in messages if m[0] in {"user", "assistant", "tool"}), None)
    if ended is not None:
        state, activity, evidence = "idle", reply or "Session ended · may resume", "session ended"
    elif last is None:
        return out
    elif last[0] == "user":
        state, activity, evidence = "working", "User turn recorded", "user message"
    elif last[0] == "tool":
        state, activity, evidence = "working", reply or "Tool result recorded", "tool result"
    elif last[4]:
        state, activity, evidence = (
            "waiting_tool",
            reply or "Tool call recorded",
            "assistant tool call",
        )
    elif last[3] or last[5]:
        state, activity, evidence = (
            "idle",
            reply or "Turn ended · session may resume",
            "assistant reply",
        )
    else:
        state, activity, evidence = "working", "Model activity recorded", "assistant activity"
    out.update(
        state=state,
        activity=activity,
        evidence=f"Hermes database: {evidence}; inferred session state",
    )
    return out


def latest_request(session: Session) -> str:
    try:
        with connect(session.path) as conn:
            messages = _messages(conn, session.key, users=True)
        return next((found for m in messages if (found := request_text(_content(m[1])))), "")
    except OSError:
        return ""


def goal(session: Session) -> Goal:
    with connect(session.path) as conn:
        row = _session(conn, session.key)
        messages = _messages(conn, session.key)
    return Goal(
        redact(row[3], 120) if row else "",
        "",
        "",
        tuple(
            (role, _content(content), _iso(stamp) or "")
            for role, content, stamp, *_ in reversed(messages)
            if role in {"user", "assistant"}
        ),
    )


def _owners(root: Path) -> dict[str, set[int]] | None:
    folder = database(root).parent
    runtime = folder / "runtime"
    if runtime.is_symlink():
        return None
    registry = small_json(runtime / "active_sessions.json")
    entries = registry.get("entries") if registry else None
    if not isinstance(entries, list) or len(entries) > 5000:
        return None
    owners: dict[str, set[int]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not native_id(entry.get("session_id")):
            return None
        pid, start = entry.get("pid"), entry.get("process_start_time")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        if not isinstance(start, int | float) or isinstance(start, bool) or not 0 < start < 1e12:
            return None
        try:
            process = processes.process(pid)
            if process is None:
                continue
            actual = (
                datetime.strptime(process.start, "%a %b %d %H:%M:%S %Y")
                .replace(tzinfo=UTC)
                .timestamp()
            )
        except (OSError, ValueError, OverflowError):
            return None
        if math.floor(actual) == math.floor(start):
            owners.setdefault(entry["session_id"], set()).add(pid)
    return owners


def live_sessions(root: Path) -> frozenset[str] | None:
    owners = _owners(root)
    return frozenset(owners) if owners is not None else None


def session_pid(root: Path, native: str) -> tuple[bool, int | None]:
    owners = _owners(root)
    if owners is None:
        return False, None
    pids = owners.get(native, set())
    if len(pids) > 1:
        return False, None
    return True, next(iter(pids), None)


def resume(root: Path, native: str) -> str | None:
    if not SESSION_ID.fullmatch(native):
        return None
    with connect(root) as conn:
        if not supported(conn):
            return None
        row = _session(conn, native)
    if row is None or row[1] == "subagent":
        return None
    home = database(root).parent
    # Hermes trusts named homes; root homes need an explicit flag to override its sticky profile.
    profile = "" if home.parent.name == "profiles" else " --profile default"
    return shell_resume(
        "HERMES_HOME",
        home,
        f"hermes{profile} --resume {shlex.quote(native)}",
        valid_cwd(row[6]),
    )


HARNESS = Harness(
    kind="hermes",
    name="Hermes",
    env="HERMES_HOME",
    home_roots=home_roots,
    home_label=lambda name: None,
    is_root=is_root,
    inventory=inventory,
    observe=observe,
    latest_request=latest_request,
    titles=None,
    live_sessions=live_sessions,
    owner=lambda native: native,
    session_pid=session_pid,
    resume=resume,
    goal=goal,
)
