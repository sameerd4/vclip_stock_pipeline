#!/usr/bin/env python3
"""Audit dedupe removals for parent-location disagreement before materializing.

This is read-only. It parses the reconstructed raw FCPXMLs to recover each
candidate's parent VClip IDs, then reads the latest accepted location_json for
those parents from stock_candidates.

A dedupe pair is flagged when both sides have known parent locations and their
normalized location-label sets do not overlap.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any

def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]

def read_metadata(clip: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for child in clip.iter():
        if local_name(child.tag) != "md":
            continue
        key = child.get("key")
        value = child.get("value")
        if key and value is not None:
            out[key] = value
    return out

def project_candidates(root: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*.fcpxml")):
        tree = ET.parse(path)
        for event in tree.getroot().iter():
            if local_name(event.tag) != "event":
                continue
            event_name = event.get("name") or ""
            for project in list(event):
                if local_name(project.tag) != "project":
                    continue
                clip = next(
                    (x for x in project.iter() if local_name(x.tag) == "asset-clip"),
                    None,
                )
                if clip is None:
                    continue
                meta = read_metadata(clip)
                stock_id = meta.get("com.vclip.stock_clip_id")
                if not stock_id:
                    continue
                parents = [
                    x.strip()
                    for x in meta.get(
                        "com.vclip.telemetry.parent_ids", stock_id
                    ).split(",")
                    if x.strip()
                ]
                rows[stock_id] = {
                    "stock_clip_id": stock_id,
                    "parent_ids": sorted(set(parents)),
                    "project_name": project.get("name") or "",
                    "event_name": event_name,
                    "xml": str(path),
                }
    return rows

def location_label(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
    except Exception:
        return ""
    preferred = (
        "public_label",
        "structured_location_label",
        "location_label",
        "place_label",
        "neighborhood",
        "city",
        "state",
        "country",
    )
    def walk(value: Any) -> str | None:
        if isinstance(value, dict):
            for key in preferred:
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    return item.strip()
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return None
    return walk(obj) or ""

def latest_parent_locations(
    con: sqlite3.Connection,
    parent_ids: set[str],
) -> dict[str, str]:
    if not parent_ids:
        return {}
    placeholders = ",".join("?" for _ in parent_ids)
    sql = f"""
    WITH ranked AS (
      SELECT sc.*,
             ROW_NUMBER() OVER (
               PARTITION BY stock_clip_id
               ORDER BY COALESCE(updated_at, created_at, '') DESC, rowid DESC
             ) rn
      FROM stock_candidates sc
      WHERE stock_clip_id IN ({placeholders})
        AND eligibility_status='accepted'
    )
    SELECT stock_clip_id, location_json
    FROM ranked
    WHERE rn=1
    """
    out: dict[str, str] = {}
    for row in con.execute(sql, sorted(parent_ids)):
        label = location_label(row["location_json"])
        if label:
            out[row["stock_clip_id"]] = label
    return out

def normalized_set(values) -> set[str]:
    return {
        " ".join(str(v).casefold().split())
        for v in values
        if str(v).strip()
    }

def run(args: argparse.Namespace) -> int:
    report = json.loads(args.report.read_text(encoding="utf-8"))
    removals = report.get("removals", [])
    candidates = project_candidates(args.raw_root)

    all_parent_ids: set[str] = set()
    missing_candidate_ids: set[str] = set()
    for r in removals:
        for key in ("removed_stock_clip_id", "canonical_stock_clip_id"):
            sid = r.get(key)
            row = candidates.get(sid)
            if row is None:
                missing_candidate_ids.add(str(sid))
                continue
            all_parent_ids.update(row["parent_ids"])

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    locations = latest_parent_locations(con, all_parent_ids)
    con.close()

    conflicts = []
    unknown_one_side = []
    both_unknown = []
    same_or_overlap = []

    for r in removals:
        removed = candidates.get(r.get("removed_stock_clip_id"))
        kept = candidates.get(r.get("canonical_stock_clip_id"))
        if removed is None or kept is None:
            continue

        removed_labels = {
            locations[p] for p in removed["parent_ids"] if p in locations
        }
        kept_labels = {
            locations[p] for p in kept["parent_ids"] if p in locations
        }
        rn = normalized_set(removed_labels)
        kn = normalized_set(kept_labels)

        row = {
            "reason": r.get("reason"),
            "role": r.get("role"),
            "source_name": r.get("source_name"),
            "iou": r.get("iou"),
            "containment": r.get("containment"),
            "removed_stock_clip_id": removed["stock_clip_id"],
            "removed_project_name": removed["project_name"],
            "removed_parent_ids": removed["parent_ids"],
            "removed_locations": sorted(removed_labels),
            "canonical_stock_clip_id": kept["stock_clip_id"],
            "kept_project_name": kept["project_name"],
            "kept_parent_ids": kept["parent_ids"],
            "kept_locations": sorted(kept_labels),
        }

        if rn and kn:
            if rn & kn:
                same_or_overlap.append(row)
            else:
                conflicts.append(row)
        elif rn or kn:
            unknown_one_side.append(row)
        else:
            both_unknown.append(row)

    print("VClip dedupe location-consistency audit")
    print("======================================")
    print(f"Removal pairs:                {len(removals):4d}")
    print(f"Both known + overlap:         {len(same_or_overlap):4d}")
    print(f"Both known + DISAGREE:        {len(conflicts):4d}")
    print(f"Known on only one side:       {len(unknown_one_side):4d}")
    print(f"Unknown on both sides:        {len(both_unknown):4d}")
    print(f"Missing candidate IDs:        {len(missing_candidate_ids):4d}")

    if conflicts:
        print("\nLOCATION CONFLICTS")
        print("------------------")
        for row in conflicts[:30]:
            print(
                f"{row['role']}  {row['source_name']}  "
                f"IoU={float(row['iou'] or 0):.4f}"
            )
            print(f"  REMOVE: {row['removed_project_name']}")
            print(f"    locations={row['removed_locations']}")
            print(f"  KEEP:   {row['kept_project_name']}")
            print(f"    locations={row['kept_locations']}")
            print()

    if unknown_one_side:
        print("\nKNOWN ON ONE SIDE (first 20)")
        print("----------------------------")
        for row in unknown_one_side[:20]:
            print(
                f"{row['role']}  {row['source_name']}  "
                f"{row['removed_locations']} -> {row['kept_locations']}"
            )
            print(f"  REMOVE: {row['removed_project_name']}")
            print(f"  KEEP:   {row['kept_project_name']}")

    payload = {
        "removal_pairs": len(removals),
        "both_known_overlap": len(same_or_overlap),
        "both_known_disagree": len(conflicts),
        "known_one_side": len(unknown_one_side),
        "both_unknown": len(both_unknown),
        "missing_candidate_ids": sorted(missing_candidate_ids),
        "conflicts": conflicts,
        "unknown_one_side_rows": unknown_one_side,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nReport: {args.output}")

    if missing_candidate_ids:
        print("\nRESULT: BLOCK — candidate identity lookup incomplete")
        return 2
    if conflicts:
        print("\nRESULT: REVIEW — known location disagreements exist")
        return 1
    print("\nRESULT: PASS — no known parent-location disagreement")
    return 0

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--raw-root", type=Path, required=True)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--output", type=Path)
    return p

if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
