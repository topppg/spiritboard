"""Saved startup options contain selectors and settings, never credentials."""

import argparse
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from spiritboard import cli
from spiritboard.harnesses import opencode, pi
from spiritboard.launcher import LaunchError
from spiritboard.startup import load_startup, save_startup


def arguments(directory: Path) -> argparse.Namespace:
    return argparse.Namespace(
        data_dir=directory,
        port=8766,
        no_open=False,
        watch=False,
        no_discovery=False,
        config=None,
        demo=False,
        demo_delay=0.5,
        stale_after=15.0,
        limit_per_profile=5000,
        max_age_days=0.0,
        retention_days=0.0,
        summaries=True,
        openrouter_key_file=None,
    )


def test_settings_and_selectors_survive_without_credentials(tmp_path: Path) -> None:
    args = arguments(tmp_path / "board")
    args.config = tmp_path / "profiles.json"
    args.openrouter_key_file = tmp_path / "key.env"
    args.openrouter_key_file.write_text("synthetic-key-file-content")
    with patch.dict(
        os.environ,
        {"CODEX_HOME": str(tmp_path / "codex"), "OPENROUTER_API_KEY": "synthetic-secret"},
        clear=True,
    ):
        save_startup(args)
    saved = load_startup(args.data_dir)
    assert saved is not None
    options, environment = saved
    assert options[options.index("--port") + 1] == "0"
    assert options[options.index("--config") + 1] == str(args.config.resolve())
    assert options[options.index("--openrouter-key-file") + 1] == str(
        args.openrouter_key_file.resolve()
    )
    assert "--summaries" in options
    assert options[options.index("--max-age-days") + 1] == "0.0"
    assert "--data-dir" not in options and "--no-open" not in options and "serve" not in options
    assert environment == {"CODEX_HOME": str(tmp_path / "codex")}
    raw = (args.data_dir / "startup.json").read_text()
    assert "synthetic-secret" not in raw and "OPENROUTER_API_KEY" not in raw
    assert "synthetic-key-file-content" not in raw
    if os.name == "posix":
        assert stat.S_IMODE(args.data_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((args.data_dir / "startup.json").stat().st_mode) == 0o600


def test_missing_startup_does_not_create_data(tmp_path: Path) -> None:
    directory = tmp_path / "missing"
    assert load_startup(directory) is None
    assert not directory.exists()


def test_additional_discovery_selectors_survive_restart(tmp_path: Path) -> None:
    home = tmp_path / "home"
    xdg = tmp_path / "data"
    session_directory = tmp_path / "pi-sessions"
    with (
        patch.dict(
            os.environ,
            {
                "XDG_DATA_HOME": str(xdg),
                "PI_CODING_AGENT_SESSION_DIR": str(session_directory),
                "OPENCODE_DB": "channel.db",
            },
            clear=True,
        ),
        patch.object(Path, "home", return_value=home),
    ):
        original_opencode = opencode.home_roots(home)
        original_pi = pi.home_roots(home)
        save_startup(arguments(tmp_path / "board"))
    saved = load_startup(tmp_path / "board")
    assert saved is not None
    _, environment = saved
    assert environment == {
        "XDG_DATA_HOME": str(xdg),
        "PI_CODING_AGENT_SESSION_DIR": str(session_directory),
        "OPENCODE_DB": str(xdg / "opencode" / "channel.db"),
    }
    with (
        patch.dict(os.environ, environment, clear=True),
        patch.object(Path, "home", return_value=home),
    ):
        assert pi.home_roots(home) == original_pi
        assert opencode.home_roots(home)[0] == original_opencode[0]
    assert Path(environment["OPENCODE_DB"]) == original_opencode[-1][1]


def test_relative_xdg_keeps_default_opencode_database_location(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with (
        patch.dict(
            os.environ,
            {"XDG_DATA_HOME": "ignored-relative-data", "OPENCODE_DB": "channel.db"},
            clear=True,
        ),
        patch.object(Path, "home", return_value=home),
    ):
        original = opencode.home_roots(home)[-1][1]
        save_startup(arguments(tmp_path / "board"))
    saved = load_startup(tmp_path / "board")
    assert saved is not None
    _, environment = saved
    assert "XDG_DATA_HOME" not in environment
    assert environment["OPENCODE_DB"] == str(original)


@pytest.mark.parametrize(
    "kind", ["invalid", "oversize", "nested", "symlink", "hardlink", "directory", "insecure"]
)
def test_unsafe_existing_startup_is_rejected_without_overwriting(tmp_path: Path, kind: str) -> None:
    if kind in {"insecure", "symlink"} and os.name != "posix":
        pytest.skip("POSIX permissions and symbolic links")
    args = arguments(tmp_path / "board")
    save_startup(args)
    path = args.data_dir / "startup.json"
    if kind == "invalid":
        path.write_text('{"version": 99}')
    elif kind == "oversize":
        path.write_bytes(b"x" * 65537)
    elif kind == "nested":
        path.write_text("[" * 2000 + "]" * 2000)
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_text("synthetic untouched content")
        path.unlink()
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(path, tmp_path / "linked")
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    else:
        path.chmod(0o644)
    before = path.lstat()
    message = "Saved dashboard startup settings could not be used safely"
    with pytest.raises(LaunchError, match=message):
        load_startup(args.data_dir)
    with pytest.raises(LaunchError, match=message):
        save_startup(args)
    assert path.lstat().st_ino == before.st_ino


@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("version", True),
        ("args", ["--data-dir", "/invented"]),
        ("args", ["--port", "8766"]),
        ("args", ["--max-age-days", "nan"]),
        ("args", ["--summaries", "--summaries"]),
        ("env", {"OPENROUTER_API_KEY": "synthetic forbidden"}),
        ("env", {"CODEX_HOME": "relative/path"}),
    ],
)
def test_saved_options_reject_unknown_or_invalid_values(
    tmp_path: Path, key: str, replacement: object
) -> None:
    save_startup(arguments(tmp_path))
    path = tmp_path / "startup.json"
    data = json.loads(path.read_text())
    data[key] = replacement
    path.write_text(json.dumps(data))
    with pytest.raises(LaunchError):
        load_startup(tmp_path)


def test_cli_saves_before_readiness_and_retains_settings_after_shutdown(tmp_path: Path) -> None:
    events: list[str] = []

    def ready(url: str, instance_id: str) -> None:
        assert url == "http://127.0.0.1:12345"
        assert instance_id == "synthetic-instance"
        assert load_startup(tmp_path) is not None
        events.append("ready")

    with (
        patch("spiritboard.cli.Store"),
        patch("spiritboard.cli.Collector") as collector,
        patch("spiritboard.cli.Instance") as instance,
        patch("spiritboard.monitor.Monitor") as monitor,
        patch("spiritboard.cli.open_dashboard"),
    ):
        owner = instance.return_value.__enter__.return_value
        owner.owner = True
        owner.publish.side_effect = lambda *_: events.append("publish")
        server = collector.return_value
        server.url = "http://127.0.0.1:12345"
        server.instance_id = "synthetic-instance"
        server.stopping.wait.return_value = True
        monitor.return_value.scan_once.side_effect = lambda: events.append("scan")
        assert cli.main(["--data-dir", str(tmp_path), "--no-open"], on_ready=ready) == 0
    assert events == ["publish", "ready", "scan"]
    assert load_startup(tmp_path) is not None


def test_cli_reuse_reports_verified_identity_without_overwriting_startup(tmp_path: Path) -> None:
    with (
        patch("spiritboard.cli.Instance") as instance,
        patch("spiritboard.cli.open_dashboard"),
        patch("spiritboard.cli.save_startup") as save,
    ):
        owner = instance.return_value.__enter__.return_value
        owner.owner = False
        owner.existing_url.return_value = "http://127.0.0.1:12345"
        owner.instance_id = "synthetic-reused"
        received: list[tuple[str, str]] = []
        assert (
            cli.main(
                ["--data-dir", str(tmp_path), "--no-open"],
                on_ready=lambda url, identity: received.append((url, identity)),
            )
            == 0
        )
        save.assert_not_called()
    assert received == [("http://127.0.0.1:12345", "synthetic-reused")]
