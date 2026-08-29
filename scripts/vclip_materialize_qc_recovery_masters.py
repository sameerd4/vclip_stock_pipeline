#!/usr/bin/env python3
"""Materialize QC Recovery v1 rendered masters into the canonical VClip masters tree.

Non-destructive by default. Pass --write to create hardlinks.

The existing canonical catalog is used as a contract check: every existing row
must follow masters/<first-two-id-chars>/VCLIP_<id>.mp4 before any new link is
created. New recovery IDs must not overlap the existing canonical set.

No location/shoot views are created here. This deliberately makes the immutable
canonical-master layer a separate checkpoint before friendly view generation.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return rows, list(reader.fieldnames or [])


def shard_for(stock_clip_id: str) -> str:
    prefix = "VCLIP_"
    if not stock_clip_id.startswith(prefix):
        raise ValueError(f"Invalid VClip ID: {stock_clip_id}")
    body = stock_clip_id[len(prefix):]
    if len(body) < 2:
        raise ValueError(f"VClip ID too short: {stock_clip_id}")
    return body[:2]


def rel_master(stock_clip_id: str) -> Path:
    return Path("masters") / shard_for(stock_clip_id) / f"{stock_clip_id}.mp4"


def same_inode(a: Path, b: Path) -> bool:
    sa = os.stat(a)
    sb = os.stat(b)
    return sa.st_dev == sb.st_dev and sa.st_ino == sb.st_ino


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--canonical-root", type=Path, required=True)
    p.add_argument("--existing-catalog", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--write", action="store_true")
    args = p.parse_args()

    db = args.db.expanduser().resolve()
    root = args.canonical_root.expanduser().resolve()
    catalog_path = args.existing_catalog.expanduser().resolve()
    report_path = args.report.expanduser().resolve()

    if not root.is_dir():
        raise SystemExit(f"Canonical root missing: {root}")

    old, fields = read_csv(catalog_path)
    required = {
        "stock_clip_id",
        "canonical_master_filename",
        "canonical_master_relative_path",
    }
    missing_fields = sorted(required - set(fields))
    if missing_fields:
        raise SystemExit(
            f"Existing catalog missing required columns: {missing_fields}"
        )

    old_ids: set[str] = set()
    contract_errors: list[str] = []

    for row in old:
        sid = (row.get("stock_clip_id") or "").strip()
        if not sid:
            contract_errors.append("existing catalog row missing stock_clip_id")
            continue
        if sid in old_ids:
            contract_errors.append(f"duplicate existing ID: {sid}")
            continue
        old_ids.add(sid)

        expected_name = f"{sid}.mp4"
        expected_rel = rel_master(sid).as_posix()
        actual_name = (row.get("canonical_master_filename") or "").strip()
        actual_rel = (row.get("canonical_master_relative_path") or "").strip()

        if actual_name != expected_name:
            contract_errors.append(
                f"{sid}: canonical filename {actual_name!r} != {expected_name!r}"
            )
        if actual_rel != expected_rel:
            contract_errors.append(
                f"{sid}: canonical relative path {actual_rel!r} != {expected_rel!r}"
            )

        physical = root / actual_rel
        if not physical.is_file():
            contract_errors.append(
                f"{sid}: existing canonical master missing: {physical}"
            )

    if contract_errors:
        print("CANONICAL MASTER CONTRACT: FAILED")
        for error in contract_errors[:30]:
            print(" -", error)
        return 2

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    rows = con.execute(
        """
        SELECT
            c.stock_clip_id,
            c.project_name,
            c.duration_seconds,
            c.width,
            c.height,
            c.frame_rate,
            c.orientation,
            c.location_json,
            c.capture_time_json,
            r.exported_path,
            r.sha256,
            r.file_size_bytes
        FROM reconstructed_candidates c
        JOIN rendered_masters r USING(stock_clip_id)
        WHERE c.active=1
        ORDER BY c.stock_clip_id
        """
    ).fetchall()
    con.close()

    if len(rows) != 96:
        raise SystemExit(f"Expected 96 active rendered recovery rows, got {len(rows)}")

    new_ids = {str(row["stock_clip_id"]) for row in rows}
    overlap = sorted(old_ids & new_ids)
    if overlap:
        raise SystemExit(
            "Canonical ID collision: " + ", ".join(overlap[:20])
        )

    planned: list[dict[str, Any]] = []
    problems: list[str] = []
    hardlink_ready = 0
    already_linked = 0

    for row in rows:
        sid = str(row["stock_clip_id"])
        src = Path(row["exported_path"]).expanduser().resolve()
        rel = rel_master(sid)
        dst = root / rel

        if not src.is_file():
            problems.append(f"{sid}: rendered source missing: {src}")
            continue

        if os.stat(src).st_dev != os.stat(root).st_dev:
            problems.append(
                f"{sid}: source and canonical root are on different filesystems"
            )
            continue

        if dst.exists():
            if not dst.is_file():
                problems.append(f"{sid}: destination exists but is not a file: {dst}")
                continue
            if same_inode(src, dst):
                action = "already_hardlinked"
                already_linked += 1
            else:
                problems.append(
                    f"{sid}: destination already exists with a different inode: {dst}"
                )
                continue
        else:
            action = "create_hardlink"
            hardlink_ready += 1

        planned.append(
            {
                "stock_clip_id": sid,
                "source_render": str(src),
                "canonical_master_filename": f"{sid}.mp4",
                "canonical_master_relative_path": rel.as_posix(),
                "canonical_master_path": str(dst),
                "action": action,
                "sha256": row["sha256"],
                "file_size_bytes": row["file_size_bytes"],
                "duration_seconds": row["duration_seconds"],
                "width": row["width"],
                "height": row["height"],
                "frame_rate": row["frame_rate"],
                "orientation": row["orientation"],
                "project_name": row["project_name"],
            }
        )

    print("QC RECOVERY CANONICAL MASTER PREFLIGHT")
    print("======================================")
    print("existing canonical IDs :", len(old_ids))
    print("recovery IDs           :", len(new_ids))
    print("ID overlap             :", len(overlap))
    print("contract errors        :", len(contract_errors))
    print("hardlinks to create    :", hardlink_ready)
    print("already hardlinked     :", already_linked)
    print("problems               :", len(problems))
    print("projected masters      :", len(old_ids) + len(new_ids))
    print("mode                   :", "WRITE" if args.write else "DRY RUN")

    if problems:
        print()
        print("PROBLEMS")
        for problem in problems[:30]:
            print(" -", problem)
        return 2

    if len(planned) != 96:
        raise SystemExit(
            f"Expected 96 planned recovery masters, got {len(planned)}"
        )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path = report_path.with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=list(planned[0].keys()),
        )
        writer.writeheader()
        writer.writerows(planned)

    if not args.write:
        payload = {
            "mode": "dry_run",
            "existing_canonical_ids": len(old_ids),
            "recovery_ids": len(new_ids),
            "hardlinks_to_create": hardlink_ready,
            "already_hardlinked": already_linked,
            "projected_masters": len(old_ids) + len(new_ids),
            "csv": str(csv_path),
        }
        report_path.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        print()
        print("report:", report_path)
        print("csv   :", csv_path)
        print("QC RECOVERY CANONICAL MASTER PREFLIGHT: PASS")
        return 0

    created: list[Path] = []

    try:
        for index, item in enumerate(planned, 1):
            if item["action"] == "already_hardlinked":
                continue

            src = Path(item["source_render"])
            dst = Path(item["canonical_master_path"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.link(src, dst)
            created.append(dst)

            if not same_inode(src, dst):
                raise RuntimeError(
                    f"{item['stock_clip_id']}: hardlink inode verification failed"
                )

            if index % 20 == 0 or index == len(planned):
                print(f"  materialized {index}/96")

        physical = list((root / "masters").rglob("*.mp4"))
        if len(physical) != len(old_ids) + len(new_ids):
            raise RuntimeError(
                f"Expected {len(old_ids)+len(new_ids)} physical canonical masters, "
                f"found {len(physical)}"
            )

        for item in planned:
            src = Path(item["source_render"])
            dst = Path(item["canonical_master_path"])
            if not dst.is_file():
                raise RuntimeError(
                    f"{item['stock_clip_id']}: canonical master missing after write"
                )
            if not same_inode(src, dst):
                raise RuntimeError(
                    f"{item['stock_clip_id']}: canonical master is not a hardlink "
                    "to the audited render"
                )

    except Exception:
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        raise

    payload = {
        "mode": "write",
        "existing_canonical_ids": len(old_ids),
        "recovery_ids": len(new_ids),
        "created_hardlinks": len(created),
        "already_hardlinked": already_linked,
        "canonical_masters": len(old_ids) + len(new_ids),
        "all_recovery_links_verified_same_inode": True,
        "csv": str(csv_path),
    }
    report_path.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )

    print()
    print("QC RECOVERY CANONICAL MASTER MATERIALIZATION")
    print("============================================")
    print("created hardlinks :", len(created))
    print("already linked    :", already_linked)
    print("canonical masters :", len(old_ids) + len(new_ids))
    print("same-inode verify : PASS")
    print("report            :", report_path)
    print("csv               :", csv_path)
    print()
    print("QC RECOVERY CANONICAL MASTER MATERIALIZATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
