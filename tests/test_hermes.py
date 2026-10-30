"""Hermes observations use synthetic SQLite data and never touch a real agent home."""

import json
import os
import shlex
import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from spiritboard import focus
from spiritboard.harnesses import HARNESSES, hermes
from spiritboard.processes import Process
from spiritboard.sources import SourceProfile, discover_profiles, scan_profile
from spiritboard.store import Store
from spiritboard.summaries import _goal, _goal_text
from spiritboard.transcripts import TEXT_HIDDEN, Session

NOW = time.time()
ALPHA = "20261030_100000_alpha123"
BETA = "20261030_100100_bravo123"
CHILD = "20261030_100200_child123"
SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, title TEXT,
    parent_session_id TEXT, started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
    cwd TEXT, git_branch TEXT, git_repo_root TEXT, last_activity_at REAL,
    archived INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0, profile_name TEXT,
    model_config TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
    tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL,
    finish_reason TEXT, observed INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
    compacted INTEGER DEFAULT 0, display_kind TEXT, _compressed_summary TEXT
);
CREATE TABLE auth_tokens (token TEXT);
INSERT INTO auth_tokens VALUES ('PRIVATE credential');
"""


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        self.conn = sqlite3.connect(root / "state.db")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def session(
        self,
        sid: str,
        *,
        title: str = "Invented alpha task",
        source: str = "cli",
        parent: str | None = None,
        at: float = NOW - 60,
        archived: bool = False,
        ended: float | None = None,
        cwd: str = "/invented/alpha",
    ) -> None:
        self.conn.execute(
            "INSERT INTO sessions (id, source, title, parent_session_id, started_at, "
            "last_activity_at, archived, ended_at, cwd, model_config) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, source, title, parent, at - 60, at, archived, ended, cwd, "PRIVATE config"),
        )

    def message(
        self,
        sid: str,
        role: str,
        content: str,
        *,
        at: float = NOW - 30,
        tool_calls: str | None = None,
        active: bool = True,
    ) -> None:
        self.conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, tool_calls, active, "
            "finish_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, role, content, at, tool_calls, active, "tool_calls" if tool_calls else "stop"),
        )

    def commit(self) -> None:
        self.conn.commit()

    def profile(self, text: bool = True) -> SourceProfile:
        return SourceProfile("hermes-test", "hermes", "Hermes", str(self.root), text)

    def scan(self, text: bool = True) -> dict[str, dict]:
        return {row["native_id"]: row for row in scan_profile(self.profile(text))}

    def inventory(self) -> dict[str, Session]:
        return {row.key: row for row in hermes.inventory(self.root, 50)[0]}


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Fixture]:
    fixture = Fixture(tmp_path / "hermes")
    fixture.session(ALPHA)
    fixture.message(ALPHA, "user", "First invented request", at=NOW - 50)
    fixture.message(ALPHA, "assistant", "First invented reply", at=NOW - 40)
    fixture.message(ALPHA, "user", "Latest invented request", at=NOW - 30)
    fixture.message(ALPHA, "assistant", "Latest invented reply", at=NOW - 20)
    fixture.commit()
    yield fixture
    fixture.conn.close()


def test_registry_scan_store_and_summary_context(database: Fixture) -> None:
    assert HARNESSES["hermes"] is hermes.HARNESS
    run = database.scan()[ALPHA]
    assert run["name"] == "Invented alpha task"
    assert run["task"] == "Latest invented request"
    assert run["activity"] == "Latest invented reply"
    assert run["state"] == "idle"
    assert run["project"] == "alpha"
    assert run["source"] == "hermes"
    assert run["run_id"] == f"hermes-test:{ALPHA}"
    store = Store()
    try:
        store.observe(run)
        saved = store.snapshot()["runs"][0]
        assert saved["source"] == "hermes"
        assert saved["task"] == "Latest invented request"
    finally:
        store.close()
    session = database.inventory()[ALPHA]
    context = hermes.goal(session)
    assert context.title == "Invented alpha task"
    assert [(role, content) for role, content, _ in context.messages] == [
        ("user", "First invented request"),
        ("assistant", "First invented reply"),
        ("user", "Latest invented request"),
        ("assistant", "Latest invented reply"),
    ]
    assert hermes.latest_request(session) == "Latest invented request"
    assert "user: Latest invented request" in _goal_text(_goal(context))


def test_latest_message_state_and_compaction_lineage(database: Fixture) -> None:
    database.session(BETA, parent=ALPHA)
    database.message(BETA, "user", "Next invented request")
    database.session(CHILD, source="subagent", parent=BETA)
    database.message(
        CHILD,
        "assistant",
        "Checking invented files",
        tool_calls=json.dumps([{"function": {"name": "terminal", "arguments": "PRIVATE args"}}]),
    )
    database.commit()
    runs = database.scan()
    assert runs[BETA]["parent_run_id"] is None
    assert runs[BETA]["state"] == "working"
    assert runs[CHILD]["parent_run_id"] == f"hermes-test:{BETA}"
    assert runs[CHILD]["state"] == "waiting_tool"
    database.message(CHILD, "tool", "PRIVATE result")
    database.commit()
    assert database.scan()[CHILD]["state"] == "working"
    database.conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (NOW, CHILD))
    database.commit()
    assert database.scan()[CHILD]["state"] == "idle"


def test_order_uses_message_id_and_ignores_inactive_branch(database: Fixture) -> None:
    database.message(ALPHA, "user", "Current request despite clock regression", at=NOW - 300)
    database.message(ALPHA, "assistant", "Current reply despite clock regression", at=NOW - 400)
    database.message(ALPHA, "user", "PRIVATE abandoned request", active=False)
    database.message(ALPHA, "assistant", "PRIVATE abandoned reply", active=False)
    database.commit()
    run = database.scan()[ALPHA]
    assert run["task"] == "Current request despite clock regression"
    assert run["activity"] == "Current reply despite clock regression"
    session = database.inventory()[ALPHA]
    assert hermes.latest_request(session) == "Current request despite clock regression"
    assert "PRIVATE" not in json.dumps(hermes.goal(session).messages)


def test_privacy_filters_structured_content_and_tool_reasoning(database: Fixture) -> None:
    content = "\x00json:" + json.dumps(
        [
            {"type": "text", "text": "Visible structured reply"},
            {"type": "thinking", "thinking": "PRIVATE reasoning"},
            {"type": "image_url", "image_url": {"url": "PRIVATE image"}},
        ]
    )
    database.message(ALPHA, "system", "PRIVATE system")
    database.message(ALPHA, "tool", "PRIVATE tool output")
    database.message(ALPHA, "assistant", content)
    database.commit()
    run = database.scan()[ALPHA]
    assert run["activity"] == "Visible structured reply"
    exposed = json.dumps(run) + json.dumps(hermes.goal(database.inventory()[ALPHA]).messages)
    assert "PRIVATE" not in exposed
    assert "\\u0000json:" not in exposed


def test_text_off_hides_titles_requests_and_replies(database: Fixture) -> None:
    run = database.scan(text=False)[ALPHA]
    assert run["name"] == f"Hermes {ALPHA[:8]}"
    assert run["task"] == TEXT_HIDDEN
    assert run["state"] == "idle"
    assert "invented" not in json.dumps(run).lower()


@pytest.mark.parametrize(
    "content, expected", [("Invented complete reply", "idle"), ("", "working")]
)
def test_text_off_preserves_state_when_finish_reason_is_missing(
    database: Fixture, content: str, expected: str
) -> None:
    database.message(ALPHA, "assistant", content)
    database.conn.execute("UPDATE messages SET finish_reason = NULL")
    database.commit()
    assert database.scan()[ALPHA]["state"] == expected
    hidden = database.scan(text=False)[ALPHA]
    assert hidden["state"] == expected
    assert "invented" not in json.dumps(hidden).lower()


def test_new_request_does_not_show_the_previous_turns_reply(database: Fixture) -> None:
    database.message(ALPHA, "user", "Another invented task")
    database.commit()
    run = database.scan()[ALPHA]
    assert run["state"] == "working"
    assert run["task"] == "Another invented task"
    assert run["activity"] != "Latest invented reply"


def test_inventory_bounds_age_and_archived_sessions(database: Fixture) -> None:
    database.session(BETA, at=NOW - 10)
    database.session(CHILD, at=NOW - 20, archived=True)
    database.session("20250101_100000_ancient1", at=NOW - 30 * 86400)
    database.commit()
    sessions, coverage = hermes.inventory(database.root, 1, 7, False)
    assert [session.key for session in sessions] == [BETA]
    assert coverage["truncated"]
    assert coverage["aged_out"] == 1
    assert coverage["archived_skipped"] == 1
    assert coverage["transcript_trees"] == ["state.db"]
    assert database.scan()[CHILD]["archived"]


def test_cache_invalidates_for_title_and_message_edits(database: Fixture) -> None:
    assert database.scan()[ALPHA]["name"] == "Invented alpha task"
    database.conn.execute("UPDATE sessions SET title = ? WHERE id = ?", ("Renamed task", ALPHA))
    database.commit()
    assert database.scan()[ALPHA]["name"] == "Renamed task"
    database.conn.execute(
        "UPDATE messages SET content = ? WHERE id = (SELECT MAX(id) FROM messages)",
        ("Edited latest reply",),
    )
    database.commit()
    assert database.scan()[ALPHA]["activity"] == "Edited latest reply"


def test_database_opens_read_only_and_never_selects_configuration(database: Fixture) -> None:
    statements: list[str] = []
    uris: list[str] = []
    connect = sqlite3.connect

    def traced(database_uri: str, **options: object) -> sqlite3.Connection:
        uris.append(database_uri)
        conn = connect(database_uri, **options)
        conn.set_trace_callback(statements.append)
        return conn

    with patch.object(hermes.sqlite3, "connect", traced):
        database.scan()
        session = database.inventory()[ALPHA]
        hermes.goal(session)
        hermes.latest_request(session)
        hermes.resume(database.root, ALPHA)
    assert uris and all(uri.startswith("file:") and "mode=ro" in uri for uri in uris)
    for statement in statements:
        if statement.lstrip().upper().startswith("SELECT"):
            assert "model_config" not in statement.lower()
            assert "auth_tokens" not in statement.lower()
    with pytest.raises(OSError), hermes.connect(database.root) as conn:
        conn.execute("DELETE FROM messages")
    with pytest.raises(OSError), hermes.connect(database.root) as conn:
        conn.execute("SELECT token FROM auth_tokens")
    with pytest.raises(OSError), hermes.connect(database.root) as conn:
        conn.execute("SELECT model_config FROM sessions")


def test_missing_corrupt_unsupported_and_symlink_databases(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert not hermes.is_root(missing)
    with pytest.raises(OSError):
        hermes.inventory(missing, 10)
    assert not missing.exists()
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / "state.db").write_bytes(b"PRIVATE invalid database")
    with pytest.raises(OSError):
        hermes.inventory(corrupt, 10)
    unsupported = tmp_path / "unsupported"
    unsupported.mkdir()
    with sqlite3.connect(unsupported / "state.db") as conn:
        conn.execute("CREATE TABLE sessions (id TEXT)")
    sessions, coverage = hermes.inventory(unsupported, 10)
    assert sessions == []
    assert coverage["unsupported_schema"]
    assert hermes.resume(unsupported, ALPHA) is None
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "state.db").symlink_to(unsupported / "state.db")
    with pytest.raises(OSError):
        hermes.inventory(linked, 10)


def test_discovery_default_profiles_and_environment_relocation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    default = Fixture(home / ".hermes")
    profile = Fixture(home / ".hermes" / "profiles" / "research")
    custom = Fixture(tmp_path / "custom hermes")
    try:
        explicit = [p for p in discover_profiles(home) if p.kind == "hermes"]
        assert {Path(p.root) for p in explicit} == {default.root.resolve(), profile.root.resolve()}
        with (
            patch.object(Path, "home", return_value=home),
            patch.dict(os.environ, {"HERMES_HOME": str(custom.root)}),
        ):
            configured = [p for p in discover_profiles() if p.kind == "hermes"]
        assert custom.root.resolve() in {Path(p.root) for p in configured}
        assert not hermes.is_root(home)
        assert hermes.is_root(default.root)
        assert hermes.is_root(default.root / "state.db")
    finally:
        default.conn.close()
        profile.conn.close()
        custom.conn.close()


def test_resume_command_quotes_profile_and_cwd(database: Fixture) -> None:
    cwd = "/invented/odd dir"
    database.conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (cwd, ALPHA))
    database.commit()
    command = hermes.resume(database.root, ALPHA)
    assert command == (
        f"cd {shlex.quote(cwd)} && HERMES_HOME={shlex.quote(str(database.root))} "
        f"hermes --profile default --resume {ALPHA}"
    )
    assert hermes.resume(database.root, "missing-session") is None
    run = {**database.scan()[ALPHA], "mode": "observation"}
    runtime = database.root / "runtime"
    runtime.mkdir()
    (runtime / "active_sessions.json").write_text(json.dumps({"entries": []}))
    target = focus.resolve(run, [database.profile()])
    assert target.kind == "resume"
    assert target.command == command


def test_named_profile_resume_keeps_its_explicit_home(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path / ".hermes" / "profiles" / "research")
    try:
        fixture.session(ALPHA)
        fixture.commit()
        expected = (
            f"cd /invented/alpha && HERMES_HOME={shlex.quote(str(fixture.root))} "
            f"hermes --resume {ALPHA}"
        )
        assert hermes.resume(fixture.root, ALPHA) == expected
        assert hermes.resume(fixture.root / "state.db", ALPHA) == expected
    finally:
        fixture.conn.close()


def registry(database: Fixture, value: object) -> Path:
    runtime = database.root / "runtime"
    runtime.mkdir(exist_ok=True)
    path = runtime / "active_sessions.json"
    path.write_text(json.dumps(value))
    return path


def lease(sid: str = ALPHA, pid: int = 4242) -> dict[str, object]:
    return {
        "lease_id": f"lease-{pid}",
        "session_id": sid,
        "surface": "cli",
        "pid": pid,
        "process_start_time": NOW - 3600,
        "started_at": NOW - 3600,
        "updated_at": NOW - 3600,
    }


def live_process(pid: int = 4242, started: float = NOW - 3600) -> Process:
    start = datetime.fromtimestamp(started, UTC).strftime("%a %b %d %H:%M:%S %Y")
    return Process(pid, 1, "ttys001", start, "hermes")


def test_live_registry_verifies_pid_identity_without_heartbeat_expiry(database: Fixture) -> None:
    database.session(BETA)
    database.commit()
    registry(database, {"entries": [lease()]})
    with patch.object(hermes.processes, "process", return_value=live_process()):
        assert hermes.live_sessions(database.root) == frozenset({ALPHA})
        assert hermes.session_pid(database.root, ALPHA) == (True, 4242)
        assert hermes.session_pid(database.root, BETA) == (True, None)
    with patch.object(hermes.processes, "process", return_value=live_process(started=NOW)):
        assert hermes.live_sessions(database.root) == frozenset()
        assert hermes.session_pid(database.root, ALPHA) == (True, None)
    with patch.object(hermes.processes, "process", return_value=None):
        assert hermes.live_sessions(database.root) == frozenset()
        assert hermes.session_pid(database.root, ALPHA) == (True, None)
    with patch.object(hermes.processes, "process", side_effect=OSError("PRIVATE error")):
        assert hermes.live_sessions(database.root) is None
        assert hermes.session_pid(database.root, ALPHA) == (False, None)


def test_registry_empty_and_duplicate_live_processes(database: Fixture) -> None:
    registry(database, {"entries": []})
    assert hermes.live_sessions(database.root) == frozenset()
    assert hermes.session_pid(database.root, ALPHA) == (True, None)
    registry(database, {"entries": [lease(pid=4242), lease(pid=4343)]})
    with patch.object(hermes.processes, "process", side_effect=lambda pid: live_process(pid)):
        assert hermes.live_sessions(database.root) == frozenset({ALPHA})
        assert hermes.session_pid(database.root, ALPHA) == (False, None)


@pytest.mark.parametrize(
    "value",
    [
        [],
        {},
        {"entries": "invalid"},
        {"entries": [{}]},
        {"entries": [{**lease(), "process_start_time": None}]},
        {"entries": [{**lease(), "process_start_time": 10**400}]},
        {"entries": [{**lease(), "process_start_time": float("inf")}]},
        {"entries": [{**lease(), "process_start_time": float("nan")}]},
        {"entries": [{**lease(), "pid": True}]},
        {"entries": [{**lease(), "pid": -1}]},
    ],
)
def test_missing_or_unverifiable_registry_is_unknown(database: Fixture, value: object) -> None:
    assert hermes.live_sessions(database.root) is None
    registry(database, value)
    assert hermes.live_sessions(database.root) is None
    assert hermes.session_pid(database.root, ALPHA) == (False, None)


def test_corrupt_and_linked_registry_is_unknown(database: Fixture, tmp_path: Path) -> None:
    path = registry(database, {"entries": []})
    path.write_text("PRIVATE broken json {")
    assert hermes.live_sessions(database.root) is None
    path.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"entries": []}))
    path.symlink_to(target)
    assert hermes.live_sessions(database.root) is None


def test_read_limits_bound_message_count_and_bodies(database: Fixture) -> None:
    for _ in range(100):
        database.message(ALPHA, "assistant", "x" * 100_000)
    database.commit()
    context = hermes.goal(database.inventory()[ALPHA])
    assert len(context.messages) <= 40
    assert all(isinstance(body, str) and len(body) <= 16_384 for _, body, _ in context.messages)
    assert len(database.scan()[ALPHA]["activity"]) <= 160
