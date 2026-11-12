"""Private restart settings for a dashboard, without credentials or session data."""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import tempfile
from pathlib import Path

from .harnesses import HARNESSES
from .launcher import LaunchError, _private_directory, _regular_descriptor

_ERROR = "Saved dashboard startup settings could not be used safely."
_MAX_BYTES = 65536
_FLAGS = ("watch", "no_discovery", "demo", "summaries")
_NUMBERS = ("demo_delay", "stale_after", "limit_per_profile", "max_age_days", "retention_days")
_PATHS = ("config", "openrouter_key_file")
ENVIRONMENT_SELECTORS = frozenset(harness.env for harness in HARNESSES.values()) | {
    "PI_CODING_AGENT_SESSION_DIR",
    "XDG_DATA_HOME",
}


def _path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\0" in value
        or not Path(value).is_absolute()
    ):
        raise ValueError
    return value


def _validated(value: object) -> tuple[list[str], dict[str, str]]:
    if not isinstance(value, dict) or set(value) != {"version", "args", "env"}:
        raise ValueError
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError
    arguments, environment = value["args"], value["env"]
    if not isinstance(arguments, list) or len(arguments) > 24:
        raise ValueError
    if not all(isinstance(item, str) and len(item) <= 4096 for item in arguments):
        raise ValueError
    flags = {"--" + name.replace("_", "-") for name in _FLAGS}
    numbers = {"--" + name.replace("_", "-") for name in _NUMBERS}
    paths = {"--" + name.replace("_", "-") for name in _PATHS}
    seen: set[str] = set()
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option in seen or option not in flags | numbers | paths | {"--port"}:
            raise ValueError
        seen.add(option)
        index += 1
        if option in flags:
            continue
        if index >= len(arguments):
            raise ValueError
        item = arguments[index]
        index += 1
        if option in paths:
            _path(item)
        elif option == "--port":
            if item != "0":
                raise ValueError
        else:
            number = float(item)
            if not math.isfinite(number) or number < 0:
                raise ValueError
            if option == "--stale-after" and number <= 0:
                raise ValueError
            if option == "--limit-per-profile" and (
                not item.isascii() or not item.isdecimal() or not 1 <= int(item) <= 5000
            ):
                raise ValueError
    if not numbers | {"--port"} <= seen:
        raise ValueError
    if "--no-discovery" in seen and {"--watch", "--config"} & seen:
        raise ValueError
    if "--demo" in seen and {"--watch", "--config", "--summaries"} & seen:
        raise ValueError
    if not isinstance(environment, dict) or set(environment) - ENVIRONMENT_SELECTORS:
        raise ValueError
    restored = {name: _path(item) for name, item in environment.items()}
    return list(arguments), restored


def load_startup(directory: Path) -> tuple[list[str], dict[str, str]] | None:
    """Read validated restart options without creating missing dashboard state."""
    directory = Path(directory)
    try:
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode) or (os.name == "posix" and info.st_mode & 0o077):
            raise ValueError
        path = directory / "startup.json"
        if not path.exists() and not path.is_symlink():
            return None
        descriptor = _regular_descriptor(path)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError
        return _validated(json.loads(raw))
    except (OSError, ValueError, TypeError, RecursionError, LaunchError):
        raise LaunchError(_ERROR) from None


def save_startup(args: argparse.Namespace) -> None:
    """Atomically retain the owner's restart settings after it publishes readiness."""
    temporary: Path | None = None
    try:
        arguments = ["--port", "0"]
        for name in _FLAGS:
            if getattr(args, name):
                arguments.append("--" + name.replace("_", "-"))
        for name in _NUMBERS:
            arguments.extend(["--" + name.replace("_", "-"), str(getattr(args, name))])
        for name in _PATHS:
            selected = getattr(args, name)
            if selected is not None:
                arguments.extend(
                    ["--" + name.replace("_", "-"), str(Path(selected).expanduser().resolve())]
                )
        environment: dict[str, str] = {}
        xdg = os.environ.get("XDG_DATA_HOME", "")
        data = Path(xdg) if os.path.isabs(xdg) else Path.home() / ".local" / "share"
        for name in ENVIRONMENT_SELECTORS:
            selected = os.environ.get(name, "")
            if not selected or (name == "XDG_DATA_HOME" and not os.path.isabs(selected)):
                continue
            if name == "OPENCODE_DB" and not os.path.isabs(selected):
                environment[name] = str((data / "opencode" / selected).resolve())
            else:
                environment[name] = str(Path(selected).expanduser().resolve())
        value = {"version": 1, "args": arguments, "env": environment}
        _validated(value)
        payload = json.dumps(value, allow_nan=False).encode("utf-8")
        if len(payload) > _MAX_BYTES:
            raise ValueError
        directory = Path(args.data_dir)
        _private_directory(directory)
        load_startup(directory)
        descriptor, name = tempfile.mkstemp(prefix=".startup-", dir=directory)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "startup.json")
    except (OSError, ValueError, TypeError, RecursionError, LaunchError):
        raise LaunchError(_ERROR) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
