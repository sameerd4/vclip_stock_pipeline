#!/usr/bin/env python3
"""Build a deterministic re-grade candidate census from ready-cut grade audit output.

The first trial is intentionally narrow: the January 18, 2025 Vancouver review
corpus. A candidate is a ready_cut whose creative grade contains at least one
selected Custom LUT outside VClip Production Palette v1.

This script is read-only. It does not modify FCPXML or media. For each candidate
it resolves the exact historical ready-cut asset-clip back to its source media,
source range, Camera LUT, current creative LUT stack, capture timestamp when
recoverable from DJI filenames, and any already-persisted canonical OpenAI
caption for the same VCLIP ID.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from vclip_pipeline.stockify.core import parse_time
from vclip_pipeline.stockify.fcpxml import (
    build_resource_index,
    first_direct_child,
    local_name,
    read_vclip_metadata,
)


DEFAULT_GRADE_AUDIT = (
    Path.home()
    / "Desktop"
    / "vclip-work"
    / "work"
    / "ready-grade-audit-v2"
    / "ready-grade-appearances.csv"
)
DEFAULT_DB = Path.home() / "Desktop" / "vclip-work" / "work" / "vclip.sqlite3"
DEFAULT_OUTPUT_ROOT = (
    Path.home()
    / "Desktop"
    / "vclip-work"
    / "work"
    / "regrade-trial-vancouver-jan18-v1"
)

TARGET_CREATIVE_STATUS = "REVIEW_UNNAMED_CUSTOM_LUT"
CAMERA_OK = {"PASS", "PASS_INFERRED_FROM_CAMERA_LUT", "NOT_REQUIRED"}
_DJI_CAPTURE_RE = re.compile(r"DJI_(\d{8})(\d{6})_", re.IGNORECASE)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def is_target_jan18(row: dict[str, str]) -> bool:
    xml_file = (row.get("xml_file") or "").replace("\\", "/").casefold()
    if "/jan18/" in xml_file:
        return True
    source = row.get("source_name") or ""
    match = _DJI_CAPTURE_RE.search(source)
    return bool(match and match.group(1) == "20250118")


def dji_capture_iso(source_name: str) -> str:
    match = _DJI_CAPTURE_RE.search(source_name)
    if not match:
        return ""
    day, clock = match.groups()
    return (
        f"{day[0:4]}-{day[4:6]}-{day[6:8]}T"
        f"{clock[0:2]}:{clock[2:4]}:{clock[4:6]}"
    )


def media_path_from_asset(asset: ET.Element) -> str:
    media_rep = first_direct_child(asset, "media-rep")
    if media_rep is None:
        return ""
    raw = media_rep.get("src") or ""
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        return unquote(parsed.path)
    return raw


def locate_ready_cut(
    xml_path: Path,
    project_name: str,
    stock_clip_id: str,
) -> dict[str, str]:
    root = ET.parse(xml_path).getroot()
    resources = first_direct_child(root, "resources")
    if resources is None:
        raise RuntimeError("missing resources")
    index = build_resource_index(resources)

    matched_projects = [
        node
        for node in root.iter()
        if local_name(node.tag) == "project"
        and (node.get("name") or "") == project_name
    ]
    if not matched_projects:
        raise RuntimeError(f"project not found: {project_name}")

    for project in matched_projects:
        sequence = first_direct_child(project, "sequence")
        if sequence is None:
            continue
        for clip in sequence.iter():
            if local_name(clip.tag) != "asset-clip":
                continue
            meta = read_vclip_metadata(clip)
            if meta.get("com.vclip.stock_clip_id") != stock_clip_id:
                continue
            if meta.get("com.vclip.telemetry.variant") != "ready_cut":
                continue

            ref = clip.get("ref") or ""
            asset = index.get(ref)
            if asset is None or local_name(asset.tag) != "asset":
                raise RuntimeError(f"asset resource missing for ref={ref}")

            clip_start = parse_time(clip.get("start"))
            asset_start = parse_time(asset.get("start"))
            duration = parse_time(clip.get("duration"))
            source_start = clip_start - asset_start

            return {
                "resolved_source_media": media_path_from_asset(asset),
                "source_start_s": f"{float(source_start):.6f}",
                "duration_s": f"{float(duration):.6f}",
                "fcpxml_clip_start": clip.get("start") or "",
                "fcpxml_asset_start": asset.get("start") or "",
                "fcpxml_duration": clip.get("duration") or "",
                "resolved_camera_lut": asset.get("customLUTOverride") or "",
            }

    raise RuntimeError(
        f"ready_cut asset-clip not found for {stock_clip_id} in {project_name}"
    )


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def existing_caption_lookup(db: Path) -> dict[str, str]:
    if not db.is_file():
        return {}
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        if not table_exists(con, "canonical_clip_visual_analysis"):
            return {}
        rows = con.execute(
            """
            SELECT stock_clip_id, caption, updated_at
            FROM canonical_clip_visual_analysis
            WHERE status='complete'
              AND TRIM(caption) != ''
            ORDER BY stock_clip_id, updated_at DESC
            """
        ).fetchall()
    finally:
        con.close()

    captions: dict[str, str] = {}
    for row in rows:
        stock_id = str(row["stock_clip_id"])
        captions.setdefault(stock_id, str(row["caption"]))
    return captions


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--grade-audit", type=Path, default=DEFAULT_GRADE_AUDIT)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument(
        "--scope",
        choices=["jan18-vancouver", "all-legacy-ready"],
        default="jan18-vancouver",
    )
    return p


def main() -> int:
    args = parser().parse_args()
    grade_audit = args.grade_audit.expanduser().resolve()
    db = args.db.expanduser().resolve()
    output = args.output_root.expanduser().resolve()

    if not grade_audit.is_file():
        raise SystemExit(f"Grade audit CSV not found: {grade_audit}")

    all_rows = read_rows(grade_audit)
    legacy = [
        row
        for row in all_rows
        if row.get("creative_grade_status") == TARGET_CREATIVE_STATUS
    ]
    if args.scope == "jan18-vancouver":
        scoped = [row for row in legacy if is_target_jan18(row)]
    else:
        scoped = legacy

    captions = existing_caption_lookup(db)
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for index, row in enumerate(scoped, start=1):
        xml_path = Path(row.get("xml_file") or "")
        stock_id = row.get("stock_clip_id") or ""
        project_name = row.get("project_name") or ""
        resolved: dict[str, str] = {}
        error = ""
        try:
            resolved = locate_ready_cut(xml_path, project_name, stock_id)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            errors.append(
                {
                    "stock_clip_id": stock_id,
                    "project_name": project_name,
                    "xml_file": str(xml_path),
                    "error": error,
                }
            )

        camera_status = row.get("camera_lut_status") or ""
        source_name = row.get("source_name") or ""
        media_path = resolved.get("resolved_source_media", "")
        media_exists = bool(media_path and Path(media_path).is_file())
        eligible = (
            not error
            and camera_status in CAMERA_OK
            and media_exists
        )

        candidates.append(
            {
                "trial_index": index,
                "stock_clip_id": stock_id,
                "project_name": project_name,
                "event_name": row.get("event_name") or "",
                "source_name": source_name,
                "capture_time_from_filename": dji_capture_iso(source_name),
                "xml_file": str(xml_path),
                "current_grade_status": row.get("grade_status") or "",
                "camera_lut_status": camera_status,
                "camera_lut_name": row.get("camera_lut_name") or "",
                "camera_lut_raw": row.get("camera_lut_raw") or "",
                "current_custom_lut_stack": row.get("custom_lut_stack") or "",
                "selected_custom_lut_count": row.get(
                    "selected_custom_lut_count"
                )
                or "",
                "existing_openai_caption": captions.get(stock_id, ""),
                **resolved,
                "source_media_exists": "YES" if media_exists else "NO",
                "regrade_eligible": "YES" if eligible else "NO",
                "resolution_error": error,
            }
        )

    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "candidates.csv", candidates)
    write_csv(output / "resolution-errors.csv", errors)

    camera_counts = Counter(row["camera_lut_status"] for row in candidates)
    stack_counts = Counter(row["current_custom_lut_stack"] for row in candidates)
    eligible_count = sum(row["regrade_eligible"] == "YES" for row in candidates)
    caption_count = sum(bool(row["existing_openai_caption"]) for row in candidates)
    media_count = sum(row["source_media_exists"] == "YES" for row in candidates)

    summary = {
        "scope": args.scope,
        "ready_cut_rows_total": len(all_rows),
        "legacy_palette_ready_rows": len(legacy),
        "scoped_candidates": len(candidates),
        "regrade_eligible": eligible_count,
        "source_media_resolved_and_present": media_count,
        "existing_openai_captions": caption_count,
        "resolution_errors": len(errors),
        "camera_lut_status": dict(camera_counts),
        "top_current_grade_stacks": [
            {"stack": stack, "count": count}
            for stack, count in stack_counts.most_common(30)
        ],
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print("VCLIP RE-GRADE CANDIDATE CENSUS")
    print("================================")
    print("scope                    :", args.scope)
    print("all ready-cut rows       :", len(all_rows))
    print("legacy-palette ready     :", len(legacy))
    print("scoped candidates        :", len(candidates))
    print("re-grade eligible        :", eligible_count)
    print("source media present     :", media_count)
    print("existing OpenAI captions :", caption_count)
    print("resolution errors        :", len(errors))

    print()
    print("CAMERA LUT STATUS")
    print("-----------------")
    for key, count in camera_counts.most_common():
        print(f"{count:5d}  {key}")

    print()
    print("TOP CURRENT LEGACY GRADE STACKS")
    print("-------------------------------")
    for stack, count in stack_counts.most_common(20):
        print(f"{count:5d}  {stack or '<empty>'}")

    print()
    print("CANDIDATE SAMPLE")
    print("----------------")
    for row in candidates[:20]:
        print()
        print(f"{int(row['trial_index']):03d}  {row['stock_clip_id']}")
        print("project :", row["project_name"])
        print("source  :", row["source_name"])
        print("range   :", row.get("source_start_s", "?"), "+", row.get("duration_s", "?"))
        print("camera  :", row["camera_lut_status"], row["camera_lut_name"])
        print("old LUT :", row["current_custom_lut_stack"])
        print("caption :", row["existing_openai_caption"] or "<none>")
        print("eligible:", row["regrade_eligible"])

    print()
    print("output:", output)
    print("VCLIP RE-GRADE CANDIDATE CENSUS: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
