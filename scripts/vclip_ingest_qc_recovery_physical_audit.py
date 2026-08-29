#!/usr/bin/env python3
"""Ingest physically audited QC-recovery renders into the isolated VClip pool DB.

Prerequisite:
  Register every export-plan manifest referenced by the physical audit with
  scripts/vclip_pool_db.py register-plan.

This script:
- requires exactly one PASS row per active reconstructed candidate,
- verifies every render + complete receipt,
- verifies receipt plan/batch/clip against registered export-plan tables,
- computes SHA-256,
- upserts rendered_masters,
- marks the actual plan item ingested and batch rendered,
- marks fully ingested plans "ingested" and partial plans "running",
- verifies all active recovery candidates have one rendered master.

It never mutates the main historical VClip database.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--physical-audit", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--write", action="store_true")
    args = p.parse_args()

    db = args.db.expanduser().resolve()
    audit_path = args.physical_audit.expanduser().resolve()
    report_path = args.report.expanduser().resolve()

    audit_rows = read_csv(audit_path)

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")

    active_ids = {
        str(row["stock_clip_id"])
        for row in con.execute(
            """
            SELECT stock_clip_id
            FROM reconstructed_candidates
            WHERE active=1
            """
        ).fetchall()
    }

    if len(active_ids) != 96:
        raise SystemExit(
            f"Expected 96 active recovery candidates, got {len(active_ids)}"
        )

    audit_by_id: dict[str, dict[str, str]] = {}
    for row in audit_rows:
        sid = row["stock_clip_id"]
        if sid in audit_by_id:
            raise SystemExit(f"Duplicate physical-audit row for {sid}")
        audit_by_id[sid] = row

    if set(audit_by_id) != active_ids:
        missing = sorted(active_ids - set(audit_by_id))
        extra = sorted(set(audit_by_id) - active_ids)
        raise SystemExit(
            "Physical audit / active ID mismatch: "
            f"missing={missing[:10]} extra={extra[:10]}"
        )

    ingest_rows: list[dict[str, Any]] = []
    problems: list[str] = []

    for sid in sorted(active_ids):
        row = audit_by_id[sid]

        if row.get("status") != "PASS":
            problems.append(f"{sid}: physical audit status={row.get('status')!r}")
            continue

        path = Path(row["path"]).expanduser().resolve()
        receipt_path = Path(row["receipt"]).expanduser().resolve()

        if not path.is_file():
            problems.append(f"{sid}: rendered file missing: {path}")
            continue

        if not receipt_path.is_file():
            problems.append(f"{sid}: receipt missing: {receipt_path}")
            continue

        try:
            receipt = read_json(receipt_path)
        except Exception as exc:
            problems.append(
                f"{sid}: receipt JSON failed: {type(exc).__name__}: {exc}"
            )
            continue

        if receipt.get("status") != "complete":
            problems.append(
                f"{sid}: receipt status={receipt.get('status')!r}"
            )
            continue

        plan_id = str(receipt.get("plan_id") or "")
        batch_id = str(receipt.get("batch_id") or "")

        plan = con.execute(
            """
            SELECT *
            FROM master_export_plans
            WHERE id=?
            """,
            (plan_id,),
        ).fetchone()
        if plan is None:
            problems.append(
                f"{sid}: export plan {plan_id!r} is not registered"
            )
            continue

        batch = con.execute(
            """
            SELECT *
            FROM master_export_batches
            WHERE id=?
              AND export_plan_id=?
            """,
            (batch_id, plan_id),
        ).fetchone()
        if batch is None:
            problems.append(
                f"{sid}: batch {batch_id!r} not registered under {plan_id}"
            )
            continue

        item = con.execute(
            """
            SELECT *
            FROM master_export_items
            WHERE export_plan_id=?
              AND stock_clip_id=?
            """,
            (plan_id, sid),
        ).fetchone()
        if item is None:
            problems.append(
                f"{sid}: not registered as an item in plan {plan_id}"
            )
            continue

        receipt_match = next(
            (
                f
                for f in receipt.get("files", [])
                if str(f.get("stock_clip_id") or "") == sid
            ),
            None,
        )
        if receipt_match is None:
            problems.append(
                f"{sid}: complete receipt does not contain this stock_clip_id"
            )
            continue

        receipt_file = Path(receipt_match["path"]).expanduser().resolve()
        if receipt_file != path:
            problems.append(
                f"{sid}: audit path != receipt path: {path} != {receipt_file}"
            )
            continue

        expected_basename = str(item["expected_basename"])
        if path.stem != expected_basename:
            problems.append(
                f"{sid}: filename stem {path.stem!r} != expected {expected_basename!r}"
            )
            continue

        ingest_rows.append(
            {
                "stock_clip_id": sid,
                "plan_id": plan_id,
                "batch_id": batch_id,
                "path": path,
                "receipt_path": receipt_path,
                "file_size_bytes": path.stat().st_size,
                "duration_seconds": safe_float(row.get("duration_seconds")),
                "width": int(float(row["width"])),
                "height": int(float(row["height"])),
                "frame_rate": safe_float(row.get("frame_rate")),
                "codec_name": row.get("codec") or "",
            }
        )

    if problems:
        con.close()
        print("QC RECOVERY RENDER INGEST PREFLIGHT: FAILED")
        for problem in problems[:30]:
            print(" -", problem)
        return 2

    plan_counts: dict[str, int] = {}
    for row in ingest_rows:
        plan_counts[row["plan_id"]] = plan_counts.get(row["plan_id"], 0) + 1

    report: dict[str, Any] = {
        "mode": "write" if args.write else "dry_run",
        "active_candidates": len(active_ids),
        "audited_renders": len(ingest_rows),
        "by_actual_plan": dict(sorted(plan_counts.items())),
        "problems": [],
        "generated_at": now(),
    }

    print("QC RECOVERY RENDER INGEST PREFLIGHT")
    print("===================================")
    print("active candidates :", len(active_ids))
    print("audited renders   :", len(ingest_rows))
    print("actual plans      :", len(plan_counts))
    for plan_id, count in sorted(plan_counts.items()):
        print(f"{count:4d}  {plan_id}")
    print("mode              :", "WRITE" if args.write else "DRY RUN")

    if not args.write:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2) + "\n",
            encoding="utf-8",
        )
        con.close()
        print()
        print("report:", report_path)
        print("QC RECOVERY RENDER INGEST PREFLIGHT: PASS")
        return 0

    created = now()

    try:
        print()
        print("Hashing and cataloging rendered masters...")

        for index, row in enumerate(ingest_rows, 1):
            path: Path = row["path"]
            checksum = sha256_file(path)

            render_id = (
                "MASTER_"
                + hashlib.sha256(
                    (
                        row["plan_id"]
                        + "|"
                        + row["stock_clip_id"]
                    ).encode("utf-8")
                ).hexdigest()[:20].upper()
            )

            con.execute(
                """
                INSERT INTO rendered_masters(
                    id,
                    export_plan_id,
                    batch_id,
                    stock_clip_id,
                    exported_filename,
                    exported_path,
                    file_size_bytes,
                    duration_seconds,
                    width,
                    height,
                    frame_rate,
                    codec_name,
                    sha256,
                    receipt_path,
                    ingested_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(stock_clip_id) DO UPDATE SET
                    id=excluded.id,
                    export_plan_id=excluded.export_plan_id,
                    batch_id=excluded.batch_id,
                    exported_filename=excluded.exported_filename,
                    exported_path=excluded.exported_path,
                    file_size_bytes=excluded.file_size_bytes,
                    duration_seconds=excluded.duration_seconds,
                    width=excluded.width,
                    height=excluded.height,
                    frame_rate=excluded.frame_rate,
                    codec_name=excluded.codec_name,
                    sha256=excluded.sha256,
                    receipt_path=excluded.receipt_path,
                    ingested_at=excluded.ingested_at
                """,
                (
                    render_id,
                    row["plan_id"],
                    row["batch_id"],
                    row["stock_clip_id"],
                    path.name,
                    str(path),
                    row["file_size_bytes"],
                    row["duration_seconds"],
                    row["width"],
                    row["height"],
                    row["frame_rate"],
                    row["codec_name"],
                    checksum,
                    str(row["receipt_path"]),
                    created,
                ),
            )

            con.execute(
                """
                UPDATE master_export_items
                SET status='ingested'
                WHERE export_plan_id=?
                  AND stock_clip_id=?
                """,
                (
                    row["plan_id"],
                    row["stock_clip_id"],
                ),
            )

            con.execute(
                """
                UPDATE master_export_batches
                SET status='rendered',
                    receipt_path=?,
                    completed_at=COALESCE(completed_at, ?),
                    error_text=NULL
                WHERE export_plan_id=?
                  AND id=?
                """,
                (
                    str(row["receipt_path"]),
                    created,
                    row["plan_id"],
                    row["batch_id"],
                ),
            )

            if index % 20 == 0 or index == len(ingest_rows):
                print(f"  cataloged {index}/{len(ingest_rows)}")

        registered_plans = con.execute(
            "SELECT id FROM master_export_plans ORDER BY id"
        ).fetchall()

        plan_statuses: dict[str, dict[str, Any]] = {}
        for plan in registered_plans:
            plan_id = str(plan["id"])
            counts = con.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='ingested' THEN 1 ELSE 0 END) AS ingested
                FROM master_export_items
                WHERE export_plan_id=?
                """,
                (plan_id,),
            ).fetchone()

            total = int(counts["total"] or 0)
            ingested = int(counts["ingested"] or 0)

            if total and ingested == total:
                status = "ingested"
            elif ingested:
                status = "running"
            else:
                status = "planned"

            con.execute(
                """
                UPDATE master_export_plans
                SET status=?,
                    updated_at=?
                WHERE id=?
                """,
                (status, created, plan_id),
            )

            plan_statuses[plan_id] = {
                "total_items": total,
                "ingested_items": ingested,
                "status": status,
            }

        rendered = con.execute(
            """
            SELECT COUNT(*) AS n
            FROM rendered_masters rm
            JOIN reconstructed_candidates c
              USING(stock_clip_id)
            WHERE c.active=1
            """
        ).fetchone()["n"]

        unique_paths = con.execute(
            """
            SELECT COUNT(DISTINCT rm.exported_path) AS n
            FROM rendered_masters rm
            JOIN reconstructed_candidates c
              USING(stock_clip_id)
            WHERE c.active=1
            """
        ).fetchone()["n"]

        unique_sha = con.execute(
            """
            SELECT COUNT(DISTINCT rm.sha256) AS n
            FROM rendered_masters rm
            JOIN reconstructed_candidates c
              USING(stock_clip_id)
            WHERE c.active=1
            """
        ).fetchone()["n"]

        if int(rendered) != 96:
            raise RuntimeError(
                f"Expected 96 active rendered masters, got {rendered}"
            )
        if int(unique_paths) != 96:
            raise RuntimeError(
                f"Expected 96 unique rendered paths, got {unique_paths}"
            )

        con.commit()

    except Exception:
        con.rollback()
        raise

    report.update(
        {
            "rendered_masters": int(rendered),
            "unique_rendered_paths": int(unique_paths),
            "unique_sha256": int(unique_sha),
            "plan_statuses": plan_statuses,
        }
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )

    con.close()

    print()
    print("QC RECOVERY RENDER CATALOG")
    print("==========================")
    print("active rendered masters :", rendered)
    print("unique rendered paths   :", unique_paths)
    print("unique SHA-256          :", unique_sha)
    for plan_id, item in sorted(plan_statuses.items()):
        print(
            f"{plan_id}  "
            f"{item['ingested_items']}/{item['total_items']}  "
            f"{item['status']}"
        )
    print("report                  :", report_path)
    print()
    print("QC RECOVERY RENDER INGEST: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
