"""Capture promotional media using only temporary, invented agent observations."""

from __future__ import annotations

import argparse
import secrets
import subprocess
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random

from PIL import Image

from spiritboard.focus import UNSUPPORTED, Target
from spiritboard.model import TERMINAL
from spiritboard.server import Collector
from spiritboard.store import Store

ROOT = Path(__file__).resolve().parents[1]


FOCUS_APPS = {
    "claude": "Ghostty",
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
        data["profiles"] = [
            {"id": f"media-{kind}", "kind": kind, "label": "demo"} for kind in FOCUS_APPS
        ]
        for run in data["runs"]:
            run["live"] = True if at is None and run["source"] != "grokbot" else None
        return data

    def focus_target(self, run_id: str) -> Target:
        run = self.store.run(run_id)
        if (
            run
            and run["session_id"] == f"media-{run['source']}:observed"
            and run["state"] not in TERMINAL
        ):
            return Target("focus", FOCUS_APPS[run["source"]])
        return UNSUPPORTED


def seed_board(store: Store) -> None:
    """Invent concrete SWE work and its observation history; never read transcripts."""
    scenarios = (
        (
            "orders",
            "codex",
            "Orders cursor pagination",
            "Add keyset pagination to GET /orders. Preserve filters and cover tied "
            "created_at values.",
            "Cursor pagination is ready; 24 tests pass, including tied timestamps.",
            "idle",
            "harbor-api",
            None,
        ),
        (
            "reconnect",
            "claude",
            "WebSocket listener leak",
            "Fix retained room listeners after reconnect; delegate the call-site audit "
            "and soak test.",
            "Fixed disconnect cleanup; testing 5,000 reconnects for retained listeners.",
            "idle",
            "relay-chat",
            None,
        ),
        (
            "staging",
            "claude",
            "Reconnect regression test",
            "Add a regression for room cleanup after reconnect; verify listener counts "
            "after disconnect.",
            "Added a reconnect regression; checking listener counts after disconnect.",
            "waiting_tool",
            "relay-chat",
            "reconnect",
        ),
        (
            "listeners",
            "claude",
            "Listener call-site audit",
            "Audit room listeners in socket.ts and typing.ts; replace per-room handlers "
            "where needed.",
            "Found a second leak in typing.ts; removing its per-room listener.",
            "working",
            "relay-chat",
            "reconnect",
        ),
        (
            "soak",
            "claude",
            "5,000-reconnect soak test",
            "Add a regression that reconnects 5,000 times and compares retained heap "
            "after collection.",
            "3,800 of 5,000 reconnects checked; retained heap is flat.",
            "working",
            "relay-chat",
            "reconnect",
        ),
        (
            "currency",
            "claude",
            "Invoice currency backfill",
            "Backfill invoice_currency from ledger entries; report unmatched accounts "
            "before writing.",
            "Dry run checked 41k invoices. Please review the backfill before I apply it.",
            "idle",
            "harbor-billing",
            None,
        ),
        (
            "upload",
            "opencode",
            "Flaky upload retry test",
            "Reproduce the upload test flake with a fake clock; fix the retry-timer race.",
            "Reproduced the retry-timer race; rerunning the upload test 100 times.",
            "working",
            "ingest-worker",
            None,
        ),
        (
            "retry",
            "opencode",
            "Retry queue audit",
            "Check abort cleanup and make a 503 response enqueue exactly one retry job.",
            "Abort cleanup is patched; checking that a 503 queues only one retry.",
            "working",
            "ingest-worker",
            "upload",
        ),
        (
            "tokens",
            "codex",
            "Theme token cleanup",
            "Replace hard-coded settings colours with theme tokens and check contrast in "
            "both themes.",
            "Replaced hard-coded colours; both themes pass contrast checks.",
            "idle",
            "atlas-web",
            None,
        ),
        (
            "contrast",
            "codex",
            "Theme contrast checks",
            "",
            "Focus rings and labels pass contrast checks in both themes.",
            "idle",
            "atlas-web",
            "tokens",
        ),
        (
            "json",
            "pi",
            "CLI JSON output",
            "Add --json output to every inspect command; keep diagnostics on stderr.",
            "7 of 9 commands return JSON; fixing the deploy-status snapshot.",
            "working",
            "forge-cli",
            None,
        ),
        (
            "snapshots",
            "pi",
            "CLI snapshot review",
            "Check stdout snapshots for every --json command and keep warnings on stderr.",
            "Moving warnings to stderr so --json stdout stays valid JSON.",
            "working",
            "forge-cli",
            None,
        ),
        (
            "oauth",
            "pi",
            "OAuth redirect validation",
            "Restrict callback redirects to the configured origin; cover encoded and "
            "mixed-case URLs.",
            "Redirect tests pass. Should localhost aliases stay in the allowlist?",
            "idle",
            "atlas-auth",
            None,
        ),
        (
            "cache",
            "dsh",
            "Cache load-test approval",
            "Add single-flight cache refresh; benchmark 200 concurrent readers at the "
            "expiry boundary.",
            "Approval requested",
            "waiting_user",
            "harbor-api",
            None,
        ),
        (
            "index",
            "grokbot",
            "Orders index rollout",
            "Review the concurrent orders index migration and compare EXPLAIN plans for "
            "list queries.",
            "Should I roll out the concurrent index after the query-plan check?",
            "waiting_user",
            "",
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
            "evidence": "invented observation for promotional media",
        }
        observations.append(observation)
    random = Random(81)
    history: list[tuple[float, dict[str, str | None]]] = []
    for observation in observations:
        store.observe({**observation, "state": "working", "last_seen": started.isoformat()})
        for _ in range(random.randint(8, 24)):
            history.append((random.uniform(0, 598), observation))
    for _ in range(9):
        center = random.uniform(15, 580)
        spread = random.uniform(3, 16)
        for kind in random.sample(list(FOCUS_APPS), random.randint(1, 4)):
            workers = [item for item in observations if item["source"] == kind]
            peak = center + random.uniform(-12, 12)
            for _ in range(random.randint(14, 55)):
                offset = random.gauss(peak, spread)
                if 0 <= offset < 599:
                    history.append((offset, random.choice(workers)))
    for offset, observation in sorted(history, key=lambda item: item[0]):
        store.observe(
            {
                **observation,
                "state": "working",
                "last_seen": (started + timedelta(seconds=offset)).isoformat(),
            }
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
