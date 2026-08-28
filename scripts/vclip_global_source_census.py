#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
}

SKIP_DIR_NAMES = {
    ".Trashes",
    ".Spotlight-V100",
    ".fseventsd",
    ".TemporaryItems",
    "VClip Rendered Masters",
    "VClip AX Libraries",
}

SUSPICIOUS_TOKENS = (
    "/proxy media/",
    "/transcoded media/",
    "/render files/",
    "/rendered masters/",
    "/shard export/",
    "/exports/",
    "/export/",
)


def normalize_stem(value: str) -> str:
    name = Path(value).stem

    # FCP / Finder duplicate suffixes we have actually seen:
    # DJI_xxx copy.mp4
    # DJI_xxx copy 2.mp4
    # DJI_xxx [00015556 +12446].mov
    name = re.sub(
        r"\s+\[\d+\s+\+\d+\]$",
        "",
        name,
    )

    name = re.sub(
        r"\s+copy(?:\s+\d+)?$",
        "",
        name,
        flags=re.IGNORECASE,
    )

    name = re.sub(
        r"\s+\(\d+\)$",
        "",
        name,
    )

    return name.casefold()


def modern_dji_identity(key: str) -> bool:
    return bool(
        re.fullmatch(
            r"dji_\d{14}_\d{4}_[a-z]",
            key,
        )
    )


def probe(path: Path) -> dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        (
            "stream=width,height,codec_name,profile,"
            "pix_fmt,r_frame_rate,bit_rate:"
            "stream_side_data=rotation:"
            "format=duration,size,bit_rate:"
            "format_tags=creation_time"
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
    stream = (
        data.get("streams")
        or [{}]
    )[0]

    fmt = data.get("format") or {}

    rotation = 0.0

    for side in (
        stream.get("side_data_list")
        or []
    ):
        try:
            rotation = float(
                side.get("rotation")
                or 0
            )
        except Exception:
            pass

    creation_time = (
        (fmt.get("tags") or {})
        .get("creation_time")
        or ""
    )

    width = int(
        stream.get("width") or 0
    )
    height = int(
        stream.get("height") or 0
    )

    quarter_turn = (
        int(
            round(rotation / 90.0)
        ) % 2 != 0
    )

    effective_width = (
        height if quarter_turn
        else width
    )

    effective_height = (
        width if quarter_turn
        else height
    )

    return {
        "width": width,
        "height": height,
        "effective_width":
            effective_width,
        "effective_height":
            effective_height,
        "rotation": rotation,
        "codec":
            stream.get("codec_name")
            or "",
        "profile":
            stream.get("profile")
            or "",
        "pix_fmt":
            stream.get("pix_fmt")
            or "",
        "stream_bitrate":
            int(
                stream.get("bit_rate")
                or 0
            ),
        "format_bitrate":
            int(
                fmt.get("bit_rate")
                or 0
            ),
        "duration":
            float(
                fmt.get("duration")
                or 0
            ),
        "size":
            int(
                fmt.get("size")
                or 0
            ),
        "creation_time":
            creation_time,
    }


def bit_depth(
    pix_fmt: str,
) -> int:
    value = pix_fmt.casefold()

    for depth in (
        16,
        14,
        12,
        10,
        9,
    ):
        if str(depth) in value:
            return depth

    if value in {
        "yuv420p",
        "yuv422p",
        "yuv444p",
        "nv12",
        "yuvj420p",
    }:
        return 8

    return 0


def path_class(path: Path) -> str:
    value = str(path).casefold()

    if any(
        token in value
        for token in SUSPICIOUS_TOKENS
    ):
        return "SUSPICIOUS_DERIVATIVE"

    if "/original media/" in value:
        return "FCP_ORIGINAL_MEDIA"

    if (
        path.name
        .casefold()
        .startswith("dji_")
    ):
        return "DIRECT_DJI_MEDIA"

    return "OTHER"


def parse_creation_time(
    value: str,
) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00",
            )
        )
    except Exception:
        return None


def same_identity(
    logical_key: str,
    current: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[bool, str]:
    if candidate["path"] == current["path"]:
        return True, "current_path"

    duration_delta = abs(
        candidate["duration"]
        - current["duration"]
    )

    current_ct = parse_creation_time(
        current["creation_time"]
    )

    candidate_ct = parse_creation_time(
        candidate["creation_time"]
    )

    creation_matches = False

    if (
        current_ct is not None
        and candidate_ct is not None
    ):
        creation_matches = (
            abs(
                (
                    current_ct
                    - candidate_ct
                ).total_seconds()
            )
            <= 5.0
        )

    # Timestamp-style DJI filenames are effectively
    # globally unique. Still demand sensible duration.
    if modern_dji_identity(
        logical_key
    ):
        if duration_delta <= 1.0:
            return (
                True,
                "timestamp_name+duration",
            )

    # Older DJI_0434-style names can repeat.
    # Strongest test is creation timestamp + duration.
    if (
        creation_matches
        and duration_delta <= 1.0
    ):
        return (
            True,
            "creation_time+duration",
        )

    # Exact duration is useful but not enough to silently
    # auto-promote an old short DJI name. Keep it reviewable.
    if duration_delta <= 0.10:
        return (
            True,
            "duration_only_review",
        )

    return False, "identity_mismatch"


def native_scale_required(
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
) -> float:
    if min(
        source_width,
        source_height,
        target_width,
        target_height,
    ) <= 0:
        return math.inf

    source_ar = (
        source_width
        / source_height
    )

    target_ar = (
        target_width
        / target_height
    )

    if target_ar <= source_ar:
        crop_height = float(
            source_height
        )
        crop_width = (
            crop_height
            * target_ar
        )
    else:
        crop_width = float(
            source_width
        )
        crop_height = (
            crop_width
            / target_ar
        )

    return max(
        target_width
        / crop_width,
        target_height
        / crop_height,
    )


def quality_tuple(
    media: dict[str, Any],
) -> tuple:
    path_type = media["path_class"]

    trusted = (
        path_type
        != "SUSPICIOUS_DERIVATIVE"
    )

    pixels = (
        media["effective_width"]
        * media["effective_height"]
    )

    depth = bit_depth(
        media["pix_fmt"]
    )

    bitrate = (
        media["stream_bitrate"]
        or media["format_bitrate"]
    )

    return (
        1 if trusted else 0,
        pixels,
        depth,
        bitrate,
        media["size"],
    )


def scan_volumes(
    volume_root: Path,
    wanted_keys: set[str],
) -> list[Path]:
    results = []

    allowed_volumes = {
        "Extreme SSD",
        "FD_2TB_EXT",
        "November PRO-G40 4TB",
        "PRO-G40 2TB",
        "PRO-G40 4TB",
        "T7",
        "T9",
        "Untitled",
    }

    volumes = sorted(
        path
        for path in volume_root.iterdir()
        if path.is_dir()
        and path.name in allowed_volumes
    )

    print()
    print("SCANNING MOUNTED VOLUMES")
    print("------------------------")

    for volume in volumes:
        print(
            f"  scanning {volume} ...",
            flush=True,
        )

        volume_matches = 0
        directories_seen = 0

        for root, dirs, files in os.walk(
            volume
        ):
            directories_seen += 1

            if directories_seen % 5000 == 0:
                print(
                    f"    {directories_seen:,} dirs scanned; "
                    f"{volume_matches:,} candidate file(s) found",
                    flush=True,
                )
            dirs[:] = [
                d
                for d in dirs
                if d not in SKIP_DIR_NAMES
            ]

            for filename in files:
                path = Path(
                    root,
                    filename,
                )

                if (
                    path.suffix.casefold()
                    not in VIDEO_EXTENSIONS
                ):
                    continue

                key = normalize_stem(
                    filename
                )

                if key in wanted_keys:
                    results.append(path)
                    volume_matches += 1

        print(
            f"    done: {directories_seen:,} dirs; "
            f"{volume_matches:,} candidate file(s)",
            flush=True,
        )

    return results


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--db",
        type=Path,
        required=True,
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
    )

    parser.add_argument(
        "--volume-root",
        type=Path,
        default=Path("/Volumes"),
    )

    parser.add_argument(
        "--expect-products",
        type=int,
        default=1660,
    )

    args = parser.parse_args()

    args.db = (
        args.db
        .expanduser()
        .resolve()
    )

    args.output_dir = (
        args.output_dir
        .expanduser()
        .resolve()
    )

    con = sqlite3.connect(
        args.db
    )

    con.row_factory = sqlite3.Row

    products = con.execute(
        """
        SELECT
            stock_clip_id,
            project_name,
            product_role,
            source_name,
            source_path,
            width,
            height,
            frame_rate,
            duration_seconds
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

    if (
        len(products)
        != args.expect_products
    ):
        raise RuntimeError(
            f"Expected "
            f"{args.expect_products} "
            f"products, found "
            f"{len(products)}"
        )

    # A source path is the current DB-selected
    # physical representation.
    products_by_current_path = (
        defaultdict(list)
    )

    for row in products:
        products_by_current_path[
            row["source_path"]
        ].append(row)

    current_sources = {}

    print(
        "PROBING CURRENT DB SOURCES"
    )
    print(
        "--------------------------"
    )

    for index, (
        source_path,
        rows,
    ) in enumerate(
        products_by_current_path.items(),
        start=1,
    ):
        path = Path(source_path)

        if not path.is_file():
            raise RuntimeError(
                f"Current source missing: "
                f"{path}"
            )

        media = probe(path)

        logical_name = (
            rows[0]["source_name"]
            or path.stem
        )

        logical_key = normalize_stem(
            logical_name
        )

        current_sources[
            source_path
        ] = {
            "logical_key":
                logical_key,
            "source_name":
                logical_name,
            "path":
                str(path),
            "path_class":
                path_class(path),
            **media,
        }

        if (
            index % 50 == 0
            or index
            == len(
                products_by_current_path
            )
        ):
            print(
                f"  {index}/"
                f"{len(products_by_current_path)}",
                flush=True,
            )

    wanted_keys = {
        row["logical_key"]
        for row
        in current_sources.values()
    }

    found_paths = scan_volumes(
        args.volume_root,
        wanted_keys,
    )

    print()
    print(
        f"Found {len(found_paths):,} "
        "filename candidate(s)."
    )

    candidate_probe_cache = {}

    print()
    print("PROBING CANDIDATE COPIES")
    print("------------------------")

    for index, path in enumerate(
        sorted(set(found_paths)),
        start=1,
    ):
        try:
            media = probe(path)

            candidate_probe_cache[
                str(path)
            ] = {
                "path":
                    str(path),
                "logical_key":
                    normalize_stem(
                        path.name
                    ),
                "path_class":
                    path_class(path),
                **media,
            }

        except Exception as exc:
            candidate_probe_cache[
                str(path)
            ] = {
                "path":
                    str(path),
                "logical_key":
                    normalize_stem(
                        path.name
                    ),
                "path_class":
                    path_class(path),
                "probe_error":
                    f"{type(exc).__name__}: "
                    f"{exc}",
            }

        if (
            index % 100 == 0
            or index
            == len(
                set(found_paths)
            )
        ):
            print(
                f"  {index}/"
                f"{len(set(found_paths))}",
                flush=True,
            )

    candidates_by_key = (
        defaultdict(list)
    )

    for media in (
        candidate_probe_cache
        .values()
    ):
        if "probe_error" in media:
            continue

        candidates_by_key[
            media["logical_key"]
        ].append(media)

    source_rows = []
    product_rows = []

    sources_with_upgrade = 0
    sources_ambiguous = 0

    current_blocked = 0
    best_blocked = 0
    products_resolved = 0

    for current_path, current in (
        current_sources.items()
    ):
        matches = []

        for candidate in (
            candidates_by_key[
                current["logical_key"]
            ]
        ):
            matched, reason = (
                same_identity(
                    current[
                        "logical_key"
                    ],
                    current,
                    candidate,
                )
            )

            if not matched:
                continue

            candidate = dict(
                candidate
            )

            candidate[
                "identity_reason"
            ] = reason

            matches.append(
                candidate
            )

        if not any(
            m["path"]
            == current_path
            for m in matches
        ):
            matches.append(
                {
                    **current,
                    "identity_reason":
                        "current_path",
                }
            )

        matches.sort(
            key=quality_tuple,
            reverse=True,
        )

        best = matches[0]

        current_quality = (
            quality_tuple(current)
        )

        best_quality = (
            quality_tuple(best)
        )

        better = (
            best["path"]
            != current_path
            and best_quality
            > current_quality
        )

        review_only_best = (
            best.get(
                "identity_reason"
            )
            == "duration_only_review"
        )

        if better:
            sources_with_upgrade += 1

        if review_only_best:
            sources_ambiguous += 1

        source_rows.append({
            "source_name":
                current["source_name"],
            "logical_key":
                current["logical_key"],
            "current_path":
                current_path,
            "current_class":
                current["path_class"],
            "current_width":
                current[
                    "effective_width"
                ],
            "current_height":
                current[
                    "effective_height"
                ],
            "current_pix_fmt":
                current["pix_fmt"],
            "current_bit_depth":
                bit_depth(
                    current["pix_fmt"]
                ),
            "copy_count":
                len(matches),
            "best_path":
                best["path"],
            "best_class":
                best["path_class"],
            "best_width":
                best[
                    "effective_width"
                ],
            "best_height":
                best[
                    "effective_height"
                ],
            "best_pix_fmt":
                best["pix_fmt"],
            "best_bit_depth":
                bit_depth(
                    best["pix_fmt"]
                ),
            "best_identity_reason":
                best[
                    "identity_reason"
                ],
            "better_copy_found":
                better,
            "best_requires_review":
                review_only_best,
        })

        for product in (
            products_by_current_path[
                current_path
            ]
        ):
            target_w = int(
                product["width"]
                or 0
            )

            target_h = int(
                product["height"]
                or 0
            )

            current_scale = (
                native_scale_required(
                    current[
                        "effective_width"
                    ],
                    current[
                        "effective_height"
                    ],
                    target_w,
                    target_h,
                )
            )

            best_scale = (
                native_scale_required(
                    best[
                        "effective_width"
                    ],
                    best[
                        "effective_height"
                    ],
                    target_w,
                    target_h,
                )
            )

            was_blocked = (
                current_scale > 1.001
            )

            remains_blocked = (
                best_scale > 1.001
            )

            if was_blocked:
                current_blocked += 1

            if remains_blocked:
                best_blocked += 1

            if (
                was_blocked
                and not remains_blocked
            ):
                products_resolved += 1

            product_rows.append({
                "stock_clip_id":
                    product[
                        "stock_clip_id"
                    ],
                "project_name":
                    product[
                        "project_name"
                    ],
                "source_name":
                    current[
                        "source_name"
                    ],
                "target_width":
                    target_w,
                "target_height":
                    target_h,
                "current_path":
                    current_path,
                "current_effective_width":
                    current[
                        "effective_width"
                    ],
                "current_effective_height":
                    current[
                        "effective_height"
                    ],
                "current_required_scale":
                    round(
                        current_scale,
                        6,
                    ),
                "best_path":
                    best["path"],
                "best_effective_width":
                    best[
                        "effective_width"
                    ],
                "best_effective_height":
                    best[
                        "effective_height"
                    ],
                "best_bit_depth":
                    bit_depth(
                        best["pix_fmt"]
                    ),
                "best_identity_reason":
                    best[
                        "identity_reason"
                    ],
                "best_required_scale":
                    round(
                        best_scale,
                        6,
                    ),
                "current_blocked":
                    was_blocked,
                "blocked_with_best_copy":
                    remains_blocked,
                "resolved_by_better_copy":
                    (
                        was_blocked
                        and not
                        remains_blocked
                    ),
                "best_requires_review":
                    review_only_best,
            })

    args.output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    source_csv = (
        args.output_dir
        / "global-source-census.csv"
    )

    product_csv = (
        args.output_dir
        / "product-best-source-analysis.csv"
    )

    json_path = (
        args.output_dir
        / "global-source-census.json"
    )

    with source_csv.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(
                source_rows[0].keys()
            ),
        )
        writer.writeheader()
        writer.writerows(
            source_rows
        )

    with product_csv.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(
                product_rows[0].keys()
            ),
        )
        writer.writeheader()
        writer.writerows(
            product_rows
        )

    json_path.write_text(
        json.dumps(
            {
                "source_rows":
                    source_rows,
                "product_rows":
                    product_rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print(
        "VCLIP GLOBAL SOURCE CENSUS"
    )
    print(
        "=========================="
    )
    print(
        f"Products:                  "
        f"{len(products):,}"
    )
    print(
        f"Current physical sources:  "
        f"{len(current_sources):,}"
    )
    print(
        f"Physical candidates found: "
        f"{len(candidate_probe_cache):,}"
    )
    print(
        f"Sources with better copy:  "
        f"{sources_with_upgrade:,}"
    )
    print(
        f"Best-copy identity review: "
        f"{sources_ambiguous:,}"
    )

    print()
    print(
        "UPSCALE STATUS"
    )
    print(
        "--------------"
    )
    print(
        f"Blocked with current DB copy: "
        f"{current_blocked:,}"
    )
    print(
        f"Resolved by better copy:       "
        f"{products_resolved:,}"
    )
    print(
        f"Still blocked with best copy:  "
        f"{best_blocked:,}"
    )

    print()
    print(
        "BEST SOURCE RESOLUTIONS"
    )
    print(
        "-----------------------"
    )

    for value, count in Counter(
        (
            row[
                "best_effective_width"
            ],
            row[
                "best_effective_height"
            ],
        )
        for row in source_rows
    ).most_common():
        print(
            f"{count:4d}  "
            f"{value[0]}x{value[1]}"
        )

    print()
    print(
        "QUALITY UPGRADES"
    )
    print(
        "----------------"
    )

    upgrades = [
        row
        for row in source_rows
        if row[
            "better_copy_found"
        ]
    ]

    if not upgrades:
        print("none")
    else:
        for row in upgrades[:100]:
            print()
            print(
                row["source_name"]
            )
            print(
                "  current: "
                f"{row['current_width']}x"
                f"{row['current_height']} "
                f"{row['current_bit_depth']}-bit"
            )
            print(
                "           "
                f"{row['current_path']}"
            )
            print(
                "  best:    "
                f"{row['best_width']}x"
                f"{row['best_height']} "
                f"{row['best_bit_depth']}-bit"
            )
            print(
                "           "
                f"{row['best_path']}"
            )
            print(
                "  identity: "
                f"{row['best_identity_reason']}"
            )

    print()
    print("MIAMI / DJI_0434")
    print("----------------")

    miami = [
        row
        for row in source_rows
        if (
            "dji_0434"
            in row[
                "logical_key"
            ]
        )
    ]

    if not miami:
        print("not found")
    else:
        for row in miami:
            print(
                f"current: "
                f"{row['current_width']}x"
                f"{row['current_height']} "
                f"{row['current_pix_fmt']}"
            )
            print(
                f"  {row['current_path']}"
            )
            print(
                f"best:    "
                f"{row['best_width']}x"
                f"{row['best_height']} "
                f"{row['best_pix_fmt']}"
            )
            print(
                f"  {row['best_path']}"
            )
            print(
                f"identity: "
                f"{row['best_identity_reason']}"
            )

    print()
    print(
        "OUTPUT"
    )
    print(
        "------"
    )
    print(
        "Sources: ",
        source_csv,
    )
    print(
        "Products:",
        product_csv,
    )
    print(
        "JSON:    ",
        json_path,
    )

    print()
    print(
        "RESULT: READ-ONLY CENSUS COMPLETE"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
