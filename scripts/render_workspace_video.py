"""Animate the synthetic workspace illustration without inspecting or activating apps."""

from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_WIDTH = 1586
SOURCE_HEIGHT = 992
WIDTH = 1600
HEIGHT = 1000
DURATION = 18
FPS = 24
GIF_FPS = 8
GIF_LIMIT = 5_000_000


@dataclass(frozen=True)
class Highlight:
    start: int
    end: int
    headline: str
    source: tuple[int, int, int, int]
    row: tuple[int, int, int, int]
    color: str


HIGHLIGHTS = (
    Highlight(
        2,
        5,
        "Orders cursor pagination is ready for review",
        (1070, 122, 241, 164),
        (44, 502, 923, 33),
        "0xffd15c",
    ),
    Highlight(
        5,
        8,
        "WebSocket listener leak is still running",
        (1317, 122, 243, 164),
        (44, 791, 923, 31),
        "0xb79aff",
    ),
    Highlight(
        8,
        11,
        "Other terminal agents appear on the same board",
        (1070, 287, 490, 174),
        (44, 820, 923, 59),
        "0xb79aff",
    ),
    Highlight(
        11,
        15,
        "Claude Desktop Code appears on the same board too",
        (1063, 513, 502, 405),
        (44, 470, 923, 32),
        "0xffd15c",
    ),
)


def run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def scaled_box(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x, y, width, height = box
    return (
        round(x * WIDTH / SOURCE_WIDTH),
        round(y * HEIGHT / SOURCE_HEIGHT),
        round(width * WIDTH / SOURCE_WIDTH),
        round(height * HEIGHT / SOURCE_HEIGHT),
    )


def box_filter(
    box: tuple[int, int, int, int], color: str, enabled: str, thickness: int | str = 2
) -> str:
    x, y, width, height = box
    return (
        f"drawbox=x={x}:y={y}:w={width}:h={height}:color={color}:t={thickness}:enable='{enabled}'"
    )


def text_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def title_filter(text: str, start: int, end: int, font: Path | None) -> str:
    face = f"fontfile='{text_value(str(font))}'" if font else "font=Sans"
    return (
        f"drawtext={face}:text='{text_value(text)}':fontsize=26:fontcolor=0xd9ccff:"
        f"x=(w-text_w)/2:y=23:enable='gte(t,{start})*lt(t,{end})'"
    )


def video_filters(with_titles: bool, font: Path | None) -> str:
    base = [f"scale={WIDTH}:{HEIGHT}:flags=lanczos", f"fps={FPS}", "setsar=1"]
    if with_titles:
        base.append("drawbox=x=0:y=0:w=iw:h=67:color=0x100d1e:t=fill")
        base.append(title_filter("Your agents. One board.", 0, 2, font))
        base.append(title_filter("Local activity. One place to see what needs you.", 15, 18, font))

    graph: list[str] = []
    for number, highlight in enumerate(HIGHLIGHTS):
        enabled = f"gte(t,{highlight.start})*lt(t,{highlight.end})"
        source = scaled_box(highlight.source)
        row = scaled_box(highlight.row)
        if with_titles:
            base.append(title_filter(highlight.headline, highlight.start, highlight.end, font))
        for box in (source, row):
            base.append(box_filter(box, highlight.color, enabled))

        # Route activity through the gap between windows, away from interface text.
        source_x = round(1063 * WIDTH / SOURCE_WIDTH)
        source_y = source[1] + source[3] // 2
        target_x = row[0] + row[2]
        target_y = row[1] + row[3] // 2
        bend_x = round(1055 * WIDTH / SOURCE_WIDTH)
        first_leg = source_x - bend_x
        vertical_leg = abs(target_y - source_y)
        last_leg = bend_x - target_x
        distance = first_leg + vertical_leg + last_leg
        for line in (
            (bend_x, source_y, first_leg, 2),
            (bend_x, min(source_y, target_y), 2, vertical_leg),
            (target_x, target_y, last_leg, 2),
        ):
            base.append(box_filter(line, f"{highlight.color}@0.38", enabled, thickness="fill"))

        traveled = f"mod(t-{highlight.start},1.6)*{distance}/1.6"
        dot_x = (
            f"{source_x}-min({traveled},{first_leg})-max(0,{traveled}-{first_leg + vertical_leg})-4"
        )
        direction = 1 if target_y >= source_y else -1
        dot_y = f"{source_y}+{direction}*min(max(0,{traveled}-{first_leg}),{vertical_leg})-4"
        input_label = "0:v" if number == 0 else f"phase{number - 1}"
        graph.append(f"[{input_label}]{','.join(base)}[highlight{number}]")
        graph.append(
            f"color=c={highlight.color}:s=8x8:r={FPS}:d={DURATION},format=rgba[dot{number}]"
        )
        graph.append(
            f"[highlight{number}][dot{number}]overlay=x='{dot_x}':y='{dot_y}':"
            f"enable='{enabled}':shortest=1[phase{number}]"
        )
        base = []
    graph.append(f"[phase{len(HIGHLIGHTS) - 1}]format=yuv420p[video]")
    return ";".join(graph)


def verify_decode(ffmpeg: str, path: Path, fps: int) -> None:
    progress = run(
        [
            ffmpeg,
            "-hide_banner",
            "-v",
            "error",
            "-nostats",
            "-progress",
            "pipe:1",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-f",
            "null",
            "-",
        ]
    )
    values = dict(line.split("=", 1) for line in progress.splitlines() if "=" in line)
    frames = int(values["frame"])
    duration = int(values["out_time_us"]) / 1_000_000
    if frames != DURATION * fps or abs(duration - DURATION) > 1 / fps:
        raise RuntimeError(f"Unexpected timing for {path.name}: {frames} frames, {duration}s")
    print(f"Decoded {path.name}: {frames} frames, {duration:.2f}s, {path.stat().st_size:,} bytes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    parser.add_argument("--font", type=Path, help="Optional font file for phase headlines")
    parser.add_argument("--image", type=Path, default=ROOT / "docs/media/workspace-overview.png")
    parser.add_argument("--video", type=Path, default=ROOT / "docs/media/workspace-overview.mp4")
    parser.add_argument("--gif", type=Path, default=ROOT / "docs/media/workspace-overview.gif")
    args = parser.parse_args()
    if not args.ffmpeg:
        parser.error("FFmpeg is required; pass --ffmpeg or install it on PATH")
    if args.font and not args.font.is_file():
        parser.error("--font must name an existing font file")
    with args.image.open("rb") as source:
        header = source.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", header[16:24]) != (
        SOURCE_WIDTH,
        SOURCE_HEIGHT,
    ):
        parser.error(f"--image must be the {SOURCE_WIDTH}x{SOURCE_HEIGHT} workspace PNG")
    filters = run([args.ffmpeg, "-hide_banner", "-filters"])
    with_titles = " drawtext " in filters
    if not with_titles:
        print("FFmpeg has no drawtext filter; keeping the illustration's existing title and labels")
    args.video.parent.mkdir(parents=True, exist_ok=True)
    args.gif.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            args.ffmpeg,
            "-hide_banner",
            "-y",
            "-loop",
            "1",
            "-framerate",
            str(FPS),
            "-i",
            str(args.image),
            "-filter_complex_threads",
            "1",
            "-filter_complex",
            video_filters(with_titles, args.font),
            "-map",
            "[video]",
            "-t",
            str(DURATION),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(args.video),
        ]
    )
    run(
        [
            args.ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            str(args.video),
            "-filter_complex_threads",
            "1",
            "-filter_complex",
            f"fps={GIF_FPS},scale=1200:-2:flags=lanczos,split[frames][colors];"
            "[colors]palettegen=max_colors=128:reserve_transparent=0:stats_mode=diff[palette];"
            "[frames][palette]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle",
            "-an",
            "-loop",
            "0",
            str(args.gif),
        ]
    )
    verify_decode(args.ffmpeg, args.video, FPS)
    verify_decode(args.ffmpeg, args.gif, GIF_FPS)
    if args.gif.stat().st_size >= GIF_LIMIT:
        raise RuntimeError(f"GIF exceeds the {GIF_LIMIT:,}-byte promotional media limit")


if __name__ == "__main__":
    main()
