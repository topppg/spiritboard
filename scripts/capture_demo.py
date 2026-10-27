"""Capture promotional media using only temporary, invented SDK events."""

from __future__ import annotations

import argparse
import secrets
import subprocess
import tempfile
import threading
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from spiritboard.sdk import Client, Run
from spiritboard.server import Collector
from spiritboard.store import Store

ROOT = Path(__file__).resolve().parents[1]


def seed_board(server: Collector, clients: ExitStack) -> list[Run]:
    """Label invented SDK runs by harness without reading any native profiles."""
    scenarios = (
        (
            "Claude",
            "Gateway reconnect",
            "Fix duplicate listeners after reconnect",
            "Checking the listener cleanup",
            "working",
        ),
        (
            "Codex",
            "Search rehearsal",
            "Add cursor pagination to search",
            "Comparing two synthetic result sets",
            "working",
        ),
        (
            "Codex",
            "Settings dark mode",
            "Use theme tokens in the settings panel",
            "Theme tokens wired; ready for your review",
            "idle",
        ),
        (
            "opencode",
            "Upload retries",
            "Recover from interrupted uploads",
            "Running the synthetic retry fixture",
            "working",
        ),
        (
            "opencode",
            "Release notes",
            "Group the invented release changes",
            "Grouped changes into features and fixes",
            "idle",
        ),
        (
            "Pi",
            "CLI help text",
            "Make command help easier to scan",
            "Checking help output against the fixture",
            "working",
        ),
        (
            "Pi",
            "Keyboard navigation",
            "Add keyboard focus to the agent list",
            "Choose the focus order for this invented example",
            "waiting_user",
        ),
        (
            "DeepSeek Harness",
            "Cache expiry",
            "Refresh expired entries without duplicate work",
            "Checking the simulated expiry boundary",
            "working",
        ),
        (
            "Grok Bot",
            "Accessibility review",
            "Review labels and keyboard access",
            "Review complete; ready for your next direction",
            "idle",
        ),
    )
    runs: list[Run] = []
    producers: list[Client] = []
    for source, name, task, activity, state in scenarios:
        client = clients.enter_context(Client(server.url, server.token, account="demo"))
        producers.append(client)
        root = client.run(name, task, source=source, project="synthetic", demo=True)
        runs.append(root)
        root.state(state, activity)
        if name == "Gateway reconnect":
            worker = root.child("Docs worker", "Review the invented quick start")
            runs.append(worker)
            worker.state("waiting_user", "Choose the wording for this invented example")
            worker = root.child("Listener audit", "Check every synthetic listener site")
            runs.append(worker)
            worker.activity("Checking cleanup after a simulated disconnect")
            worker = root.child("Test worker", "Check the invented retry fixture")
            runs.append(worker)
            worker.activity("Running synthetic fixture checks")
        if name == "Settings dark mode":
            worker = root.child("Token audit", "Find colours missing a theme token")
            worker.finish(summary="All invented colours now use theme tokens")
        if name == "Upload retries":
            worker = root.child("Retry worker", "Exercise the synthetic retry fixture")
            runs.append(worker)
            worker.activity("Verifying the corrected fixture")
        if name == "CLI help text":
            worker = root.child("Help comparison", "Compare synthetic command output")
            runs.append(worker)
            worker.activity("Checking the final two command examples")
        if not client.flush(3):
            raise RuntimeError("Synthetic SDK delivery failed")
    started = datetime.now(UTC) - timedelta(minutes=10)
    for bucket in range(1, 61):
        stamp = (started + timedelta(seconds=bucket * 10)).isoformat()
        with (
            patch("spiritboard.sdk._now", return_value=stamp),
            patch("spiritboard.store.now", return_value=stamp),
        ):
            for index, run in enumerate(runs):
                for _ in range((bucket * (index + 3) + index) % 4):
                    run.heartbeat()
            for client in producers:
                if not client.flush(3):
                    raise RuntimeError("Synthetic history delivery failed")
    return runs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node", help="Node executable with Playwright available")
    args = parser.parse_args()
    output = ROOT / "docs/media"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spiritboard-media-") as temporary:
        frames = Path(temporary)
        store = Store(frames / "events.sqlite3", stale_after=120)
        server = Collector(store, secrets.token_urlsafe(32), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with ExitStack() as clients:
                started = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
                with (
                    patch("spiritboard.sdk._now", return_value=started),
                    patch("spiritboard.store.now", return_value=started),
                ):
                    runs = seed_board(server, clients)
                for run in runs:
                    run.heartbeat()
                for client in {run.client for run in runs}:
                    if not client.flush(3):
                        raise RuntimeError("Synthetic heartbeat delivery failed")
                subprocess.run(
                    [args.node, str(ROOT / "scripts/capture_demo.cjs"), server.url, str(frames)],
                    check=True,
                    cwd=ROOT,
                    timeout=120,
                )
        finally:
            server.stopping.set()
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
            store.close()
        images = [Image.open(frame).convert("RGB") for frame in sorted(frames.glob("frame-*.png"))]
        if len(images) != 50:
            raise RuntimeError("Expected 50 captured frames")
        palette = images[0].quantize(colors=160)
        indexed = [image.quantize(palette=palette, dither=Image.Dither.NONE) for image in images]
        path = output / "demo.gif"
        indexed[0].save(
            path,
            save_all=True,
            append_images=indexed[1:],
            duration=200,
            loop=0,
            optimize=True,
            disposal=1,
        )
        with Image.open(path) as result:
            duration = sum(
                result.seek(index) or result.info["duration"] for index in range(result.n_frames)
            )
            if result.size != (1280, 800) or duration != 10_000 or path.stat().st_size > 5_000_000:
                raise RuntimeError("GIF dimensions, duration, or size outside the media contract")
        social = output / "social-preview.png"
        with Image.open(social) as result:
            if result.size != (1280, 640) or social.stat().st_size > 1_000_000:
                raise RuntimeError("Social preview dimensions or size outside the media contract")
        print(f"{path.relative_to(ROOT)} · 1280×800 · 10 seconds · {path.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
