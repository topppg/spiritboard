"""Explicit, reversible installation of native lifecycle event adapters."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from . import input_claude, input_codex, input_extensions
from .harnesses.opencode import database
from .input_events import InputSignals
from .sources import SourceProfile
from .transcripts import READ_FLAGS


def hook_command(parts: list[str], *, windows: bool | None = None) -> str:
    windows = os.name == "nt" if windows is None else windows
    if windows:
        # Windows argv quoting cannot escape cmd.exe expansion in hook command strings.
        if any(character in part for part in parts for character in "%&|<>^!\r\n"):
            raise ValueError("Hook paths contain unsupported command-shell characters")
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def _read(path: Path) -> str | None:
    try:
        with os.fdopen(os.open(path, READ_FLAGS), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Configuration must be a regular file")
            raw = stream.read(1_048_577)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("Configuration could not be read safely") from None
    if len(raw) > 1_048_576:
        raise ValueError("Configuration is too large")
    return raw.decode("utf-8")


def _owned(command: object, profile: str) -> bool:
    if not isinstance(command, str):
        return False
    try:
        words = shlex.split(command)
        index = words.index("--profile-id")
        return "spiritboard" in words and "input-event" in words and words[index + 1] == profile
    except (ValueError, IndexError):
        return False


def _settings(
    previous: str | None, additions: dict[str, list[dict[str, object]]], profile: str
) -> str:
    value = json.loads(previous) if previous else {}
    if not isinstance(value, dict) or not isinstance(value.get("hooks", {}), dict):
        raise ValueError("Invalid hook configuration")
    remaining: dict[str, list[dict[str, object]]] = {}
    for event, groups in value.get("hooks", {}).items():
        if not isinstance(groups, list):
            raise ValueError("Invalid hook group")
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("Invalid hook entry")
            hooks = [
                item
                for item in group["hooks"]
                if not (isinstance(item, dict) and _owned(item.get("command"), profile))
            ]
            if hooks:
                kept.append({**group, "hooks": hooks})
        if kept:
            remaining[event] = kept
    for event, groups in additions.items():
        remaining.setdefault(event, []).extend(groups)
    if remaining:
        value["hooks"] = remaining
    else:
        value.pop("hooks", None)
    return json.dumps(value, indent=2) + "\n"


def _write(path: Path, content: str, previous: str | None) -> None:
    if previous == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("Configuration must not be a symbolic link")
    if _read(path) != previous:
        raise ValueError("Configuration changed during setup; retry without overwriting it")
    if previous is not None:
        backup = path.with_name(path.name + ".spiritboard-backup-" + uuid.uuid4().hex)
        with os.fdopen(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
            stream.write(previous)
    descriptor, temporary = tempfile.mkstemp(prefix=".spiritboard-input-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install(
    profiles: list[SourceProfile],
    directory: Path,
    *,
    remove: bool = False,
    opencode_config: Path | None = None,
) -> list[dict[str, str]]:
    """Change only named Spiritboard hook entries/assets; preserve all other settings."""
    plans: list[tuple[SourceProfile, Path, str, str | None, bool]] = []
    results: list[dict[str, str]] = []
    opencode = [profile for profile in profiles if profile.kind == "opencode"]
    if len(opencode) > 1:
        raise ValueError("Select one OpenCode profile per configuration directory")
    for profile in profiles:
        root = Path(profile.root).expanduser().resolve()
        command = [
            sys.executable,
            "-m",
            "spiritboard",
            "input-event",
            "--source",
            profile.kind,
            "--profile-id",
            profile.id,
            "--data-dir",
            str(directory.resolve()),
        ]
        extension = False
        if profile.kind in {"claude", "codex"}:
            adapter = input_claude if profile.kind == "claude" else input_codex
            path = root / ("settings.json" if profile.kind == "claude" else "hooks.json")
            previous = _read(path)
            content = _settings(
                previous, {} if remove else adapter.hooks(hook_command(command)), profile.id
            )
        elif profile.kind in {"pi", "opencode"}:
            extension = True
            if profile.kind == "pi":
                if not (root / "sessions").is_dir():
                    results.append({"profile": profile.id, "status": "agent root required"})
                    continue
                path = root / "extensions" / "spiritboard-input.js"
                generate = input_extensions.pi_extension
            else:
                configured = os.environ.get("OPENCODE_CONFIG_DIR")
                data_home = os.environ.get("XDG_DATA_HOME", "")
                data_root = (
                    Path(data_home) if os.path.isabs(data_home) else Path.home() / ".local/share"
                )
                selected = Path(os.environ.get("OPENCODE_DB", "opencode.db"))
                active_database = (
                    selected if selected.is_absolute() else data_root / "opencode" / selected
                )
                if not opencode_config and database(root).resolve() != active_database.resolve():
                    raise ValueError(
                        "A custom OpenCode database requires its configuration directory"
                    )
                xdg = os.environ.get("XDG_CONFIG_HOME", "")
                default = Path(xdg) if os.path.isabs(xdg) else Path.home() / ".config"
                config = opencode_config or (
                    Path(configured) if configured else default / "opencode"
                )
                path = config.expanduser().resolve() / "plugins" / "spiritboard-input.js"
                generate = input_extensions.opencode_plugin
            previous = _read(path)
            marker = f"// Spiritboard input status: {profile.id}\n"
            if previous is not None and not previous.startswith(marker):
                raise ValueError("An unrelated extension occupies the Spiritboard filename")
            content = marker + generate(command)
        else:
            results.append({"profile": profile.id, "status": "native transcript signals"})
            continue
        plans.append((profile, path, content, previous, extension))
    signals = InputSignals(directory / "inputs.sqlite3")
    try:
        for profile, path, content, previous, extension in plans:
            if remove and extension:
                if _read(path) != previous:
                    raise ValueError("Extension changed during removal")
                path.unlink(missing_ok=True)
            elif not remove or previous is not None:
                _write(path, content, previous)
            signals.configure(profile.id, profile.kind, enabled=not remove)
            status = (
                "removed"
                if remove
                else "review hooks in codex"
                if profile.kind == "codex"
                else "configured"
            )
            results.append({"profile": profile.id, "status": status})
    finally:
        signals.close()
    return results
