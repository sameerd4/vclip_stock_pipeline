#!/usr/bin/env python3
"""Build a Final Cut Pro LUT calibration project for VClip Production Palette v1.

The generated FCPXML contains the same local video source repeated ten times.
Each timeline clip is named for exactly one approved production LUT and carries
VClip metadata with the expected LUT name. The operator imports the project,
applies Final Cut's Custom LUT effect to each clip, selects the LUT named on the
clip, leaves Mix at its default value, then exports the project as FCPXML.

This script intentionally applies no Custom LUT itself. Its purpose is to let
Final Cut serialize its own opaque Custom LUT selection payloads so VClip can
build a deterministic LUT-name registry.
"""

from __future__ import annotations

import argparse
import csv
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

from vclip_pipeline.packaging.media import find_video_files, probe_media
from vclip_pipeline.stockify.core import format_time, stable_uid
from vclip_pipeline.stockify.fcpxml import (
    add_vclip_metadata,
    validate_fcpxml,
    write_fcpxml,
)

FCPXML_VERSION = "1.12"
DEFAULT_CANONICAL_ROOT = Path("/Volumes/PRO-G40 2TB/VClip Library")
DEFAULT_PALETTE = (
    Path.home()
    / "Desktop"
    / "vclip-work"
    / "work"
    / "lut-census-v1"
    / "production-palette-v1.csv"
)
DEFAULT_OUTPUT = (
    Path.home()
    / "Desktop"
    / "vclip-work"
    / "work"
    / "lut-census-v1"
    / "vclip-production-palette-v1-calibration.fcpxml"
)
DEFAULT_SECONDS_PER_CLIP = 5.0

EXPECTED_PALETTE = [
    "Dark Sunset",
    "Glowing Sky",
    "Golden Light",
    "Grey Waters",
    "Icy Fjords",
    "Natural Glare",
    "Ocean Blues",
    "Shallow Seas",
    "Turquoise Delight",
    "Violet Night",
]


def frame_duration(fps: float) -> Fraction:
    common = [
        (23.976, Fraction(1001, 24000)),
        (24.0, Fraction(1, 24)),
        (25.0, Fraction(1, 25)),
        (29.97, Fraction(1001, 30000)),
        (30.0, Fraction(1, 30)),
        (50.0, Fraction(1, 50)),
        (59.94, Fraction(1001, 60000)),
        (60.0, Fraction(1, 60)),
    ]
    value, grid = min(common, key=lambda row: abs(row[0] - fps))
    if abs(value - fps) <= 0.08:
        return grid
    return Fraction(1, max(1, round(fps)))


def snap(value: float, grid: Fraction, mode: str = "nearest") -> Fraction:
    ratio = Fraction(str(round(value, 9))) / grid
    if mode == "floor":
        frames = ratio.numerator // ratio.denominator
    elif mode == "ceil":
        frames = -((-ratio.numerator) // ratio.denominator)
    else:
        frames = round(ratio)
    return frames * grid


def read_palette(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise RuntimeError(f"Palette CSV not found: {path}")
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    names = [row.get("lut_name", "").strip() for row in rows]
    if names != EXPECTED_PALETTE:
        raise RuntimeError(
            "Production palette does not match expected V1 order.\n"
            f"Expected: {EXPECTED_PALETTE}\n"
            f"Found:    {names}"
        )
    return rows


def choose_source(explicit: Path | None, root: Path, seconds: float):
    candidates: list[Path]
    if explicit is not None:
        candidates = [explicit]
    else:
        if not root.is_dir():
            raise RuntimeError(
                f"Canonical root not mounted: {root}\n"
                "Pass --source /path/to/video.mp4 to use another local clip."
            )
        all_videos = find_video_files(root)
        canonical_named = [
            path for path in all_videos if path.name.upper().startswith("VCLIP_")
        ]
        candidates = canonical_named or all_videos

    errors: list[str] = []
    for path in candidates:
        if not path.is_file():
            errors.append(f"missing:{path}")
            continue
        probe = probe_media(path)
        if (
            probe.duration_seconds is None
            or probe.width is None
            or probe.height is None
            or probe.frame_rate is None
        ):
            errors.append(f"unprobeable:{path}")
            continue
        if probe.duration_seconds < seconds + 0.5:
            errors.append(f"too_short:{path}:{probe.duration_seconds:.3f}s")
            continue
        return path.resolve(), probe

    detail = "\n".join(errors[:20])
    raise RuntimeError(
        "Could not find a usable calibration video."
        + (f"\n{detail}" if detail else "")
    )


def build_project(
    *,
    source: Path,
    width: int,
    height: int,
    fps: float,
    source_duration: float,
    seconds_per_clip: float,
    palette: list[dict[str, str]],
) -> ET.Element:
    grid = frame_duration(fps)
    clip_duration = snap(seconds_per_clip, grid, "floor")
    if clip_duration <= 0:
        raise RuntimeError("Calibration clip duration snapped to zero")
    asset_duration = snap(source_duration, grid, "floor")

    root = ET.Element("fcpxml", {"version": FCPXML_VERSION})
    resources = ET.SubElement(root, "resources")

    format_id = "r1"
    asset_id = "r2"

    ET.SubElement(
        resources,
        "format",
        {
            "id": format_id,
            "name": f"FFVideoFormat{width}x{height}",
            "frameDuration": format_time(grid),
            "width": str(width),
            "height": str(height),
            "colorSpace": "1-1-1 (Rec. 709)",
        },
    )

    asset = ET.SubElement(
        resources,
        "asset",
        {
            "id": asset_id,
            "name": source.name,
            "uid": stable_uid("vclip-lut-calibration-asset", str(source)),
            "start": "0s",
            "duration": format_time(asset_duration),
            "hasVideo": "1",
            "format": format_id,
        },
    )
    ET.SubElement(
        asset,
        "media-rep",
        {
            "kind": "original-media",
            "src": source.as_uri(),
        },
    )

    library = ET.SubElement(root, "library")
    event_name = "VClip LUT Calibration"
    event = ET.SubElement(
        library,
        "event",
        {
            "name": event_name,
            "uid": stable_uid("vclip-lut-calibration-event", event_name),
        },
    )

    project_name = "VClip Production Palette v1 — Calibration"
    project = ET.SubElement(
        event,
        "project",
        {
            "name": project_name,
            "uid": stable_uid("vclip-lut-calibration-project", project_name),
        },
    )

    total_duration = clip_duration * len(palette)
    sequence = ET.SubElement(
        project,
        "sequence",
        {
            "format": format_id,
            "duration": format_time(total_duration),
            "tcStart": "0s",
            "tcFormat": "NDF",
        },
    )
    spine = ET.SubElement(sequence, "spine")

    offset = Fraction(0)
    for index, row in enumerate(palette, start=1):
        name = row["lut_name"].strip()
        clip = ET.SubElement(
            spine,
            "asset-clip",
            {
                "name": f"{index:02d} — {name}",
                "ref": asset_id,
                "offset": format_time(offset),
                "start": "0s",
                "duration": format_time(clip_duration),
            },
        )
        add_vclip_metadata(
            clip,
            {
                "com.vclip.lut_calibration.version": "production-palette-v1",
                "com.vclip.lut_calibration.index": str(index),
                "com.vclip.lut_calibration.expected_lut_name": name,
                "com.vclip.lut_calibration.cube_sha256": row.get(
                    "cube_sha256", ""
                ),
                "com.vclip.lut_calibration.production_approved": row.get(
                    "production_approved", ""
                ),
            },
        )
        offset += clip_duration

    return root


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path)
    p.add_argument(
        "--canonical-root",
        type=Path,
        default=DEFAULT_CANONICAL_ROOT,
    )
    p.add_argument("--palette", type=Path, default=DEFAULT_PALETTE)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument(
        "--seconds-per-clip",
        type=float,
        default=DEFAULT_SECONDS_PER_CLIP,
    )
    return p


def main() -> int:
    args = parser().parse_args()
    if args.seconds_per_clip <= 0:
        raise SystemExit("--seconds-per-clip must be positive")

    palette_path = args.palette.expanduser().resolve()
    output = args.output.expanduser().resolve()
    canonical_root = args.canonical_root.expanduser()
    explicit_source = args.source.expanduser() if args.source else None

    try:
        palette = read_palette(palette_path)
        source, probe = choose_source(
            explicit_source,
            canonical_root,
            args.seconds_per_clip,
        )
        root = build_project(
            source=source,
            width=int(probe.width or 0),
            height=int(probe.height or 0),
            fps=float(probe.frame_rate or 0.0),
            source_duration=float(probe.duration_seconds or 0.0),
            seconds_per_clip=args.seconds_per_clip,
            palette=palette,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    validation = validate_fcpxml(root)
    if not validation.passed:
        print("FCPXML validation failed:", file=sys.stderr)
        for error in validation.errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    output.parent.mkdir(parents=True, exist_ok=True)
    write_fcpxml(root, output)

    print("VCLIP LUT CALIBRATION PROJECT")
    print("=============================")
    print("source     :", source)
    print(
        "media      :",
        f"{probe.width}x{probe.height} {probe.frame_rate:.3f}fps "
        f"{probe.duration_seconds:.2f}s",
    )
    print("palette    :", palette_path)
    print("clips      :", len(palette))
    print("clip length:", f"{args.seconds_per_clip:.2f}s")
    print("FCPXML     :", output)
    print()
    print("TIMELINE")
    print("--------")
    for index, row in enumerate(palette, start=1):
        print(f"{index:02d}  {row['lut_name']}")
    print()
    print("IMPORT THIS FILE INTO FINAL CUT PRO.")
    print("For each numbered clip:")
    print("  1. Add Final Cut's Custom LUT effect.")
    print("  2. Select the LUT named on that clip.")
    print("  3. Leave Mix at its default value.")
    print("When all ten are done, export this project as FCPXML and send it back.")
    print()
    print("VCLIP LUT CALIBRATION PROJECT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
