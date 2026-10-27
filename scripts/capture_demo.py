"""Capture promotional media using only temporary, invented agent observations."""

from __future__ import annotations

import argparse
import secrets
import subprocess
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image

from spiritboard.focus import UNSUPPORTED, Target
from spiritboard.model import TERMINAL
from spiritboard.server import Collector
from spiritboard.store import Store

ROOT = Path(__file__).resolve().parents[1]


APPS = {
    "claude": "VS Code",
    "codex": "Ghostty",
    "opencode": "Ghostty",
    "pi": "Terminal",
    "dsh": "DeepSeek Harness",
    "grokbot": "Grok Bot",
}


class CaptureCollector(Collector):
    """Display invented native sessions without inspecting or activating any app."""

    def snapshot(self, at: float | None = None) -> dict:
        data = super().snapshot(at)
        data["mode"] = "demo"
        data["profiles"] = [{"id": f"media-{kind}", "kind": kind, "label": "demo"} for kind in APPS]
        for run in data["runs"]:
            run["live"] = True if at is None else None
        return data

    def focus_target(self, run_id: str) -> Target:
        run = self.store.run(run_id)
        if (
            run
            and run["session_id"] == f"media-{run['source']}:observed"
            and run["state"] not in TERMINAL
        ):
            return Target("focus", APPS[run["source"]])
        return UNSUPPORTED


def seed_board(store: Store) -> None:
    """Invent concrete SWE work and its observation history; never read transcripts."""
    scenarios = (
        (
            "orders",
            "codex",
            "Orders cursor pagination",
            "Add keyset pagination to GET /orders. Preserve filters and cover "
            "tied created_at values.",
            "24 tests pass, including tied timestamps; review the cursor shape "
            "before I update OpenAPI.",
            "idle",
            "harbor-api",
            None,
        ),
        (
            "reconnect",
            "claude",
            "WebSocket listener leak",
            "Fix retained room listeners after reconnect; delegate the call-site "
            "audit and soak test.",
            "Disconnect cleanup patched; soak worker is measuring heap growth "
            "across 5,000 reconnects.",
            "idle",
            "relay-chat",
            None,
        ),
        (
            "staging",
            "claude",
            "Staging write-test approval",
            "Verify room cleanup against the seeded staging database after the soak test.",
            "Dry run targets only seeded rooms; need approval before running the "
            "staging write test.",
            "waiting_user",
            "relay-chat",
            "reconnect",
        ),
        (
            "listeners",
            "claude",
            "Listener call-site audit",
            "Audit room listeners in socket.ts and typing.ts; replace per-room "
            "handlers where needed.",
            "Found the same leak in typing.ts; replacing its per-room listener "
            "with one shared handler.",
            "working",
            "relay-chat",
            "reconnect",
        ),
        (
            "soak",
            "claude",
            "5,000-reconnect soak test",
            "Add a regression that reconnects 5,000 times and compares retained "
            "heap after collection.",
            "Baseline retained 18 MB; patched run is on reconnect 3,800/5,000 with a flat heap.",
            "working",
            "relay-chat",
            "reconnect",
        ),
        (
            "currency",
            "claude",
            "Invoice currency backfill",
            "Backfill invoice_currency from ledger entries; report unmatched "
            "accounts before writing.",
            "Dry run covers 41k invoices with no unmatched accounts; approve the backfill plan.",
            "waiting_user",
            "harbor-billing",
            None,
        ),
        (
            "upload",
            "opencode",
            "Flaky upload retry test",
            "Reproduce the upload test flake with a fake clock; fix the retry-timer race.",
            "Fake clock exposed a retry-timer race; patch is rerunning the "
            "chunk-upload test 100 times.",
            "working",
            "ingest-worker",
            None,
        ),
        (
            "retry",
            "opencode",
            "Retry queue audit",
            "Check abort cleanup and make a 503 response enqueue exactly one retry job.",
            "Cleared timers on abort; checking that a 503 response creates only one retry job.",
            "working",
            "ingest-worker",
            "upload",
        ),
        (
            "tokens",
            "codex",
            "Theme token cleanup",
            "Replace hard-coded settings colours with theme tokens and check "
            "contrast in both themes.",
            "Settings now use theme tokens; contrast checks pass in both themes "
            "and screenshots match.",
            "idle",
            "atlas-web",
            None,
        ),
        (
            "contrast",
            "codex",
            "Theme contrast checks",
            "Verify focus rings and labels meet contrast targets in light and dark mode.",
            "Focus rings and labels pass contrast checks in both themes.",
            "completed",
            "atlas-web",
            "tokens",
        ),
        (
            "json",
            "pi",
            "CLI JSON output",
            "Add --json output to every inspect command; keep diagnostics on stderr.",
            "7/9 commands emit stable JSON; fixing snapshot tests for deploy status.",
            "working",
            "forge-cli",
            None,
        ),
        (
            "snapshots",
            "pi",
            "CLI snapshot review",
            "Check stdout snapshots for every --json command and keep warnings on stderr.",
            "Two commands still mix warnings into stdout; moving those messages to stderr.",
            "working",
            "forge-cli",
            "json",
        ),
        (
            "oauth",
            "pi",
            "OAuth redirect validation",
            "Restrict callback redirects to the configured origin; cover encoded "
            "and mixed-case URLs.",
            "Encoded-host cases pass; choose whether localhost aliases belong in "
            "the redirect allowlist.",
            "waiting_user",
            "atlas-auth",
            None,
        ),
        (
            "cache",
            "dsh",
            "Redis cache stampede",
            "Add single-flight cache refresh; benchmark 200 concurrent readers "
            "at the expiry boundary.",
            "Single-flight refresh added; load test is checking p95 latency with "
            "200 concurrent readers.",
            "working",
            "harbor-api",
            None,
        ),
        (
            "index",
            "grokbot",
            "Orders index rollout",
            "Review the concurrent orders index migration and compare EXPLAIN "
            "plans for list queries.",
            "EXPLAIN uses orders_created_at_id_idx; review the query plan before "
            "merging the migration.",
            "idle",
            "harbor-api",
            None,
        ),
    )
    started = datetime.now(UTC) - timedelta(minutes=10)
    observations: list[dict[str, str | None]] = []
    for key, kind, name, task, activity, state, project, parent in scenarios:
        observation = {
            "run_id": f"media-{kind}:{key}",
            "session_id": f"media-{kind}:observed",
            "native_id": key,
            "parent_run_id": f"media-{kind}:{parent}" if parent else None,
            "source": kind,
            "account": "demo",
            "name": name,
            "task": task,
            "activity": activity,
            "state": state,
            "project": project,
            "started_at": started.isoformat(),
            "app": APPS[kind],
            "evidence": "invented observation for promotional media",
        }
        observations.append(observation)
    for bucket in range(60):
        stamp = started + timedelta(seconds=bucket * 10)
        for index, observation in enumerate(observations):
            for offset in range((bucket * (index + 3) + index) % 4):
                store.observe(
                    {**observation, "last_seen": (stamp + timedelta(seconds=offset)).isoformat()}
                )
    current = datetime.now(UTC).isoformat()
    for observation in observations:
        store.observe({**observation, "last_seen": current})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node", help="Node executable with Playwright available")
    args = parser.parse_args()
    output = ROOT / "docs/media"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spiritboard-media-") as temporary:
        frames = Path(temporary)
        store = Store(frames / "events.sqlite3", stale_after=120)
        seed_board(store)
        server = CaptureCollector(store, secrets.token_urlsafe(32), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
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
