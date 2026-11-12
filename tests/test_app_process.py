"""Exercise the Mac backend's real startup protocol with isolated, synthetic boards."""

from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from spiritboard.startup import ENVIRONMENT_SELECTORS

BACKEND = Path(__file__).resolve().parents[1] / "src" / "app" / "backend.py"
READY = "Spiritboard app ready: "


class AppProcesses:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.home.mkdir()
        self.data = root / "selected board"
        self.default_data = (
            self.home / "Library" / "Application Support" / "spiritboard"
            if sys.platform == "darwin"
            else self.home / "data" / "spiritboard"
        )
        self.environment = os.environ.copy()
        for name in ENVIRONMENT_SELECTORS | {"OPENROUTER_API_KEY"}:
            self.environment.pop(name, None)
        self.environment.update(
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            LOCALAPPDATA=str(self.home / "data"),
            XDG_DATA_HOME=str(self.home / "data"),
        )
        self.processes: list[subprocess.Popen[str]] = []
        self.output: dict[int, list[str]] = {}
        self.readers: dict[int, threading.Thread] = {}

    def start(self, *arguments: str) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, *arguments],
            cwd=self.root,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.processes.append(process)
        lines: list[str] = []
        self.output[process.pid] = lines

        def read() -> None:
            assert process.stdout is not None
            lines.extend(process.stdout)

        reader = threading.Thread(target=read, daemon=True)
        self.readers[process.pid] = reader
        reader.start()
        return process

    def stop(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            if os.name == "posix":
                process.send_signal(signal.SIGINT)
            else:
                process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        self.readers[process.pid].join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()

    def captured(self, process: subprocess.Popen[str]) -> str:
        return "".join(self.output[process.pid])

    def metadata(self, process: subprocess.Popen[str]) -> dict[str, object]:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                metadata = json.loads((self.data / "instance.json").read_text())
                if isinstance(metadata, dict) and (self.data / "startup.json").is_file():
                    return metadata
            except (OSError, ValueError):
                pass
            assert process.poll() is None, self.captured(process)
            time.sleep(0.02)
        pytest.fail("Backend did not publish private state: " + self.captured(process))

    def ready(self, process: subprocess.Popen[str]) -> dict[str, str]:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            for line in list(self.output[process.pid]):
                if not line.startswith(READY):
                    continue
                payload = json.loads(line.removeprefix(READY))
                assert isinstance(payload, dict) and set(payload) == {"url", "instance_id"}
                assert all(isinstance(value, str) for value in payload.values())
                uuid.UUID(payload["instance_id"])
                assert urlsplit(payload["url"]).hostname == "127.0.0.1"
                health = json.loads(self.get(payload["url"], "/api/health"))
                assert health == {
                    "ok": True,
                    "version": 1,
                    "service": "spiritboard",
                    "instance_id": payload["instance_id"],
                }
                return payload
            if process.poll() is not None:
                self.readers[process.pid].join(timeout=2)
                pytest.fail("Backend exited without readiness: " + self.captured(process))
            time.sleep(0.02)
        pytest.fail("Backend did not announce readiness: " + self.captured(process))

    def get(self, url: str, path: str) -> bytes:
        connection = http.client.HTTPConnection("127.0.0.1", urlsplit(url).port, timeout=2)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == 200
            return response.read()
        finally:
            connection.close()

    def seed(self, *options: str) -> dict[str, object]:
        process = self.start(
            "-m",
            "spiritboard",
            "serve",
            "--data-dir",
            str(self.data),
            "--no-open",
            "--port",
            "0",
            *options,
        )
        metadata = self.metadata(process)
        self.stop(process)
        assert not (self.data / "instance.json").exists()
        return metadata

    def profile_config(self) -> Path:
        profile = self.root / "explicit synthetic profile"
        (profile / "sessions").mkdir(parents=True)
        (self.home / ".codex" / "sessions").mkdir(parents=True)
        config = self.root / "sources.json"
        config.write_text(
            json.dumps(
                [
                    {
                        "id": "explicit",
                        "kind": "codex",
                        "label": "Synthetic explicit profile",
                        "root": str(profile),
                    }
                ]
            )
        )
        return config


@pytest.fixture
def app_processes(tmp_path: Path) -> Iterator[AppProcesses]:
    processes = AppProcesses(tmp_path)
    try:
        yield processes
    finally:
        for process in reversed(processes.processes):
            processes.stop(process)


def test_saved_custom_board_cold_start_join_and_restart(app_processes: AppProcesses) -> None:
    app = app_processes
    initial = app.seed(
        "--no-discovery",
        "--max-age-days",
        "0",
        "--limit-per-profile",
        "17",
        "--retention-days",
        "0",
        "--stale-after",
        "23",
    )
    first = app.start(str(BACKEND), "--data-dir", str(app.data))
    first_ready = app.ready(first)
    assert app.metadata(first)["settings"] == initial["settings"]
    snapshot = json.loads(app.get(first_ready["url"], "/api/snapshot"))
    assert snapshot["profiles"] == [] and snapshot["runs"] == []
    assert snapshot["ai"]["enabled"] is False
    for asset in ("/", "/app.js", "/style.css"):
        assert app.get(first_ready["url"], asset)

    joined = app.start(str(BACKEND), "--data-dir", str(app.data))
    assert app.ready(joined) == first_ready
    assert joined.wait(timeout=5) == 0
    assert first.poll() is None

    app.stop(first)
    restarted = app.start(str(BACKEND), "--data-dir", str(app.data))
    assert app.ready(restarted)["instance_id"] != first_ready["instance_id"]
    assert app.metadata(restarted)["settings"] == initial["settings"]
    assert not app.default_data.exists()


def test_saved_explicit_profiles_survive_backend_restart(app_processes: AppProcesses) -> None:
    app = app_processes
    config = app.profile_config()
    initial = app.seed("--config", str(config), "--max-age-days", "0")
    restarted = app.start(str(BACKEND), "--data-dir", str(app.data))
    ready = app.ready(restarted)
    assert app.metadata(restarted)["settings"] == initial["settings"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = json.loads(app.get(ready["url"], "/api/snapshot"))
        if snapshot["profiles"]:
            break
        time.sleep(0.02)
    assert [profile["id"] for profile in snapshot["profiles"]] == ["explicit"]
    assert snapshot["runs"] == [] and snapshot["ai"]["enabled"] is False
    assert not app.default_data.exists()


def test_missing_saved_config_never_starts_default_profiles(app_processes: AppProcesses) -> None:
    app = app_processes
    config = app.profile_config()
    app.seed("--config", str(config))
    config.unlink()
    refused = app.start(str(BACKEND), "--data-dir", str(app.data))
    assert refused.wait(timeout=8) == 1
    app.stop(refused)
    assert READY not in app.captured(refused)
    assert not (app.data / "instance.json").exists()
    assert not app.default_data.exists()
