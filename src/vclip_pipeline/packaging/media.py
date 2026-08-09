"""Inspect exported video files without mutating them."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}


@dataclass(frozen=True)
class MediaProbe:
    duration_seconds: float | None
    width: int | None
    height: int | None
    codec_name: str | None
    frame_rate: float | None


def find_video_files(root: Path) -> list[Path]:
    """Find exportable video files recursively in deterministic order."""
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        ),
        key=lambda path: str(path).lower(),
    )


def probe_media(path: Path) -> MediaProbe:
    """Use ffprobe when available; return unknown fields otherwise."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return MediaProbe(None, None, None, None, None)
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
        "-of",
        "json",
        str(path),
    ]
    try:
        process = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        payload = json.loads(process.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return MediaProbe(None, None, None, None, None)

    duration: float | None = None
    try:
        duration = float(payload.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        pass
    video = next(
        (
            stream
            for stream in payload.get("streams", [])
            if stream.get("codec_type") == "video"
        ),
        {},
    )
    frame_rate: float | None = None
    value = video.get("r_frame_rate")
    if isinstance(value, str) and "/" in value:
        numerator, denominator = value.split("/", 1)
        try:
            frame_rate = float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            pass
    return MediaProbe(
        duration_seconds=duration,
        width=int(video["width"]) if video.get("width") is not None else None,
        height=int(video["height"]) if video.get("height") is not None else None,
        codec_name=video.get("codec_name"),
        frame_rate=frame_rate,
    )
