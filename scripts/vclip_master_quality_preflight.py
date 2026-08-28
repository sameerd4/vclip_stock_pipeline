#!/usr/bin/env python3
"""Read-only source/master-quality preflight for canonical VClip candidates."""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any


SUSPICIOUS_PATH_TOKENS = (
    "/proxy media/",
    "/transcoded media/",
    "/render files/",
    "/rendered masters/",
    "/shard export/",
    "/exports/",
    "/export/",
)


def probe(path: Path, ffprobe: str) -> dict[str, Any]:
    cmd = [
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        (
            "stream=width,height,codec_name,profile,pix_fmt,"
            "r_frame_rate,avg_frame_rate,bit_rate,"
            "color_space,color_transfer,color_primaries:"
            "stream_side_data=rotation:"
            "format=duration,size,bit_rate,format_name"
        ),
        "-of", "json",
        str(path),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or "ffprobe failed"
        )

    data = json.loads(result.stdout)
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}

    rotation = 0.0

    for side in stream.get("side_data_list") or []:
        try:
            rotation = float(
                side.get("rotation") or 0
            )
        except Exception:
            pass

    return {
        "width":
            int(stream.get("width") or 0),
        "height":
            int(stream.get("height") or 0),
        "codec":
            stream.get("codec_name") or "",
        "profile":
            stream.get("profile") or "",
        "pix_fmt":
            stream.get("pix_fmt") or "",
        "rotation":
            rotation,
        "stream_bitrate":
            int(stream.get("bit_rate") or 0),
        "format_bitrate":
            int(fmt.get("bit_rate") or 0),
        "duration":
            float(fmt.get("duration") or 0),
        "size":
            int(fmt.get("size") or 0),
        "color_space":
            stream.get("color_space") or "",
        "color_transfer":
            stream.get("color_transfer") or "",
        "color_primaries":
            stream.get("color_primaries") or "",
    }


def source_class(path: Path) -> str:
    value = str(path).casefold()

    if any(
        token in value
        for token in SUSPICIOUS_PATH_TOKENS
    ):
        return "SUSPICIOUS_DERIVATIVE"

    if "/original media/" in value:
        return "FCP_ORIGINAL_MEDIA"

    if path.name.casefold().startswith("dji_"):
        return "DIRECT_DJI_MEDIA"

    return "OTHER"


def effective_dimensions(
    width: int,
    height: int,
    rotation: float,
) -> tuple[int, int]:
    quarter_turn = (
        int(round(rotation / 90.0)) % 2 != 0
    )

    if quarter_turn:
        return height, width

    return width, height


def required_native_scale(
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
) -> tuple[float, float, float]:
    """
    Minimum scale required to fill target aspect ratio from the
    effective source raster.

    >1.0 means an upscale is mathematically unavoidable even with
    the largest possible native-resolution crop.
    """

    if min(
        source_width,
        source_height,
        target_width,
        target_height,
    ) <= 0:
        return math.inf, 0.0, 0.0

    source_ar = source_width / source_height
    target_ar = target_width / target_height

    if target_ar <= source_ar:
        crop_height = float(source_height)
        crop_width = crop_height * target_ar
    else:
        crop_width = float(source_width)
        crop_height = crop_width / target_ar

    scale = max(
        target_width / crop_width,
        target_height / crop_height,
    )

    return scale, crop_width, crop_height


def bit_depth_from_pix_fmt(value: str) -> int | None:
    lower = value.casefold()

    for depth in (16, 14, 12, 10, 9):
        if str(depth) in lower:
            return depth

    if lower in {
        "yuv420p",
        "yuv422p",
        "yuv444p",
        "nv12",
    }:
        return 8

    return None


def prores_hq_estimate_bytes(
    *,
    width: int,
    height: int,
    fps: float,
    duration: float,
) -> float:
    # Apple reference target:
    # ~220 Mbps at 1920x1080 / 29.97 fps.
    base_bps = 220_000_000.0
    base_pixels_per_second = (
        1920 * 1080 * 29.97
    )

    pixel_rate = (
        width * height * max(fps, 1.0)
    )

    bps = (
        base_bps
        * pixel_rate
        / base_pixels_per_second
    )

    return bps * duration / 8.0


def run(args: argparse.Namespace) -> int:
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    candidates = con.execute(
        """
        SELECT
            stock_clip_id,
            project_name,
            product_role,
            source_name,
            source_path,
            source_start_seconds,
            duration_seconds,
            width,
            height,
            frame_rate,
            orientation,
            active
        FROM reconstructed_candidates
        WHERE active=1
          AND product_role IN (
              'ready_cut',
              'extended_master'
          )
        ORDER BY stock_clip_id
        """
    ).fetchall()

    con.close()

    if args.expect is not None:
        if len(candidates) != args.expect:
            raise RuntimeError(
                f"Expected {args.expect} active "
                f"exportable candidates, found "
                f"{len(candidates)}"
            )

    cache: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []

    for index, candidate in enumerate(
        candidates,
        start=1,
    ):
        path = Path(candidate["source_path"])

        if str(path) not in cache:
            state: dict[str, Any] = {
                "exists": path.is_file(),
                "probe": None,
                "probe_error": "",
            }

            if state["exists"]:
                try:
                    state["probe"] = probe(
                        path,
                        args.ffprobe,
                    )
                except Exception as exc:
                    state["probe_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )

            cache[str(path)] = state

        state = cache[str(path)]
        media = state["probe"] or {}

        source_w = int(
            media.get("width") or 0
        )
        source_h = int(
            media.get("height") or 0
        )
        rotation = float(
            media.get("rotation") or 0
        )

        display_w, display_h = (
            effective_dimensions(
                source_w,
                source_h,
                rotation,
            )
        )

        target_w = int(
            candidate["width"] or 0
        )
        target_h = int(
            candidate["height"] or 0
        )

        scale, native_crop_w, native_crop_h = (
            required_native_scale(
                display_w,
                display_h,
                target_w,
                target_h,
            )
        )

        path_type = source_class(path)
        issues = []

        if not state["exists"]:
            issues.append(
                "SOURCE_MISSING"
            )
        elif state["probe"] is None:
            issues.append(
                "SOURCE_UNREADABLE"
            )

        if (
            path_type
            == "SUSPICIOUS_DERIVATIVE"
        ):
            issues.append(
                "SUSPICIOUS_SOURCE_PATH"
            )

        # This is a definite geometry failure:
        # even the largest possible crop at source
        # pixel density cannot fill the output.
        unavoidable_upscale = (
            math.isfinite(scale)
            and scale > 1.001
        )

        if unavoidable_upscale:
            issues.append(
                "UNAVOIDABLE_SPATIAL_UPSCALE"
            )

        source_depth = (
            bit_depth_from_pix_fmt(
                media.get("pix_fmt", "")
            )
        )

        duration = float(
            candidate["duration_seconds"]
            or 0
        )
        fps = float(
            candidate["frame_rate"]
            or 0
        )

        estimated_hq_bytes = (
            prores_hq_estimate_bytes(
                width=target_w,
                height=target_h,
                fps=fps,
                duration=duration,
            )
            if target_w
            and target_h
            and fps
            and duration
            else 0.0
        )

        rows.append({
            "stock_clip_id":
                candidate["stock_clip_id"],
            "project_name":
                candidate["project_name"],
            "product_role":
                candidate["product_role"],
            "orientation":
                candidate["orientation"],
            "source_name":
                candidate["source_name"],
            "source_path":
                str(path),
            "source_class":
                path_type,
            "source_exists":
                state["exists"],
            "source_readable":
                state["probe"] is not None,
            "source_codec":
                media.get("codec", ""),
            "source_profile":
                media.get("profile", ""),
            "source_pix_fmt":
                media.get("pix_fmt", ""),
            "source_bit_depth":
                source_depth or "",
            "stored_width":
                source_w,
            "stored_height":
                source_h,
            "rotation":
                rotation,
            "effective_width":
                display_w,
            "effective_height":
                display_h,
            "target_width":
                target_w,
            "target_height":
                target_h,
            "max_native_crop_width":
                round(native_crop_w, 3),
            "max_native_crop_height":
                round(native_crop_h, 3),
            "minimum_required_scale":
                (
                    round(scale, 6)
                    if math.isfinite(scale)
                    else ""
                ),
            "unavoidable_upscale":
                unavoidable_upscale,
            "duration_seconds":
                duration,
            "frame_rate":
                fps,
            "estimated_prores_hq_bytes":
                int(estimated_hq_bytes),
            "issues":
                " | ".join(issues),
            "probe_error":
                state["probe_error"],
        })

        if (
            index % 100 == 0
            or index == len(candidates)
        ):
            print(
                f"probed candidates "
                f"{index}/{len(candidates)} "
                f"(unique sources={len(cache)})",
                flush=True,
            )

    args.output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    csv_path = (
        args.output_dir
        / "master-quality-preflight.csv"
    )
    json_path = (
        args.output_dir
        / "master-quality-preflight.json"
    )

    with csv_path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(rows[0].keys()),
        )
        writer.writeheader()
        writer.writerows(rows)

    json_path.write_text(
        json.dumps(
            {
                "candidate_count":
                    len(rows),
                "unique_source_count":
                    len(cache),
                "rows":
                    rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    hard_blocks = [
        row
        for row in rows
        if row["issues"]
    ]

    upscales = [
        row
        for row in rows
        if row["unavoidable_upscale"]
    ]

    hq_bytes = sum(
        row["estimated_prores_hq_bytes"]
        for row in rows
    )

    print()
    print(
        "VCLIP MASTER QUALITY PREFLIGHT"
    )
    print(
        "=============================="
    )
    print(
        f"Candidates:          "
        f"{len(rows):,}"
    )
    print(
        f"Unique sources:      "
        f"{len(cache):,}"
    )
    print(
        f"Hard-block rows:     "
        f"{len(hard_blocks):,}"
    )

    print()
    print("SOURCE PATH CLASS")
    print("-----------------")

    for value, count in Counter(
        row["source_class"]
        for row in rows
    ).most_common():
        print(
            f"{count:5d}  {value}"
        )

    print()
    print("SOURCE CODEC / PIXEL FORMAT")
    print("---------------------------")

    for value, count in Counter(
        (
            row["source_codec"],
            row["source_pix_fmt"],
        )
        for row in rows
    ).most_common():
        print(
            f"{count:5d}  "
            f"{value[0]:8s}  "
            f"{value[1]}"
        )

    print()
    print("SOURCE BIT DEPTH")
    print("----------------")

    for value, count in Counter(
        str(row["source_bit_depth"])
        or "unknown"
        for row in rows
    ).most_common():
        print(
            f"{count:5d}  {value}"
        )

    print()
    print("TARGET RESOLUTIONS")
    print("------------------")

    for value, count in Counter(
        (
            row["target_width"],
            row["target_height"],
        )
        for row in rows
    ).most_common():
        print(
            f"{count:5d}  "
            f"{value[0]}x{value[1]}"
        )

    print()
    print("UNAVOIDABLE SPATIAL UPSCALES")
    print("----------------------------")
    print(
        f"Count: {len(upscales):,}"
    )

    for row in upscales[:100]:
        print()
        print(
            row["stock_clip_id"],
            row["project_name"],
        )
        print(
            "  source:    "
            f"{row['effective_width']}x"
            f"{row['effective_height']} "
            f"(stored "
            f"{row['stored_width']}x"
            f"{row['stored_height']}, "
            f"rotation={row['rotation']})"
        )
        print(
            "  target:    "
            f"{row['target_width']}x"
            f"{row['target_height']}"
        )
        print(
            "  native max crop: "
            f"{row['max_native_crop_width']}x"
            f"{row['max_native_crop_height']}"
        )
        print(
            "  required scale: "
            f"{row['minimum_required_scale']}x"
        )
        print(
            "  path:      "
            f"{row['source_path']}"
        )

    print()
    print("PRORES 422 HQ STORAGE ESTIMATE")
    print("-----------------------------")
    print(
        "Approximate only; scaled from "
        "Apple's 1080p29.97 target rate."
    )
    print(
        f"Estimated pool size: "
        f"{hq_bytes / 1e12:.2f} TB "
        f"({hq_bytes / 1024**3:.1f} GiB)"
    )

    if args.render_root:
        usage = shutil.disk_usage(
            args.render_root
        )
        print(
            f"Render-root free:    "
            f"{usage.free / 1024**3:.1f} GiB"
        )

    print()
    print("CSV: ", csv_path)
    print("JSON:", json_path)

    if hard_blocks:
        print()
        print(
            "RESULT: BLOCK — quality "
            "preflight found hard failures"
        )
        return 2

    print()
    print("RESULT: PASS")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__
    )

    p.add_argument(
        "--db",
        type=Path,
        required=True,
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        required=True,
    )
    p.add_argument(
        "--expect",
        type=int,
    )
    p.add_argument(
        "--ffprobe",
        default="ffprobe",
    )
    p.add_argument(
        "--render-root",
        type=Path,
    )

    return p


if __name__ == "__main__":
    args = parser().parse_args()

    args.db = (
        args.db.expanduser().resolve()
    )
    args.output_dir = (
        args.output_dir
        .expanduser()
        .resolve()
    )

    if args.render_root:
        args.render_root = (
            args.render_root
            .expanduser()
            .resolve()
        )

    raise SystemExit(run(args))
