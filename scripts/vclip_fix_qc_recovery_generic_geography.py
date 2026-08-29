#!/usr/bin/env python3
"""Correct five overly-generic QC Recovery v1 browse-geography rows.

This script updates:
- the isolated recovery DB location_json for the five direct-GPS clips,
- qc-recovery-96-final-location-truth.csv,
- canonical-master-plan-qc-recovery-96-v1.csv,
- canonical-master-plan-all-390-v2.csv,
- the five by-location and five by-shoot hardlink paths.

The immutable canonical masters are never modified.

Dry-run is default. Pass --write to apply.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any


FIXES = {
    "VCLIP_193D9C2A2500": {
        "lat": 41.286573,
        "lon": -122.326949,
        "country": "United States",
        "region": "California",
        "city": "Mount Shasta",
        "area": "Lake Siskiyou",
        "area_kind": "poi",
        "public_label": "Lake Siskiyou, Mount Shasta",
        "verification": "direct_gps_lake_siskiyou",
    },
    "VCLIP_6774BCBBF19E": {
        "lat": 41.282029,
        "lon": -122.330862,
        "country": "United States",
        "region": "California",
        "city": "Mount Shasta",
        "area": "Lake Siskiyou",
        "area_kind": "poi",
        "public_label": "Lake Siskiyou, Mount Shasta",
        "verification": "direct_gps_lake_siskiyou",
    },
    "VCLIP_41D0252D5511": {
        "lat": 47.354255,
        "lon": -122.212708,
        "country": "United States",
        "region": "Washington",
        "city": "Auburn",
        "area": "North Auburn",
        "area_kind": "neighborhood",
        "public_label": "North Auburn, Auburn",
        "verification": "direct_gps_north_auburn",
    },
    "VCLIP_A126D5D5C7AE": {
        "lat": 48.666498,
        "lon": -122.380172,
        "country": "United States",
        "region": "Washington",
        "city": "Bellingham",
        "area": "Lake Samish",
        "area_kind": "poi",
        "public_label": "Lake Samish, Bellingham",
        "verification": "direct_gps_lake_samish",
    },
    "VCLIP_D4D3A08F330E": {
        "lat": 46.883913,
        "lon": -121.506898,
        "country": "United States",
        "region": "Washington",
        "city": "Naches",
        "area": "Chinook Pass",
        "area_kind": "poi",
        "public_label": "Chinook Pass, Naches",
        "verification": "direct_gps_chinook_pass",
    },
}


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def parse_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        return {}
    return value


def slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return re.sub(r"_+", "_", text) or "Unknown"


def short_id(sid: str) -> str:
    return sid[6:14]


def same_inode(a: Path, b: Path) -> bool:
    sa = os.stat(a)
    sb = os.stat(b)
    return sa.st_dev == sb.st_dev and sa.st_ino == sb.st_ino


def build_paths(row: dict[str, str], fix: dict[str, Any]) -> tuple[str, str, str, str]:
    date = row["capture_date"]
    daypart = row["capture_daypart"]
    date_token = date.replace("-", "_")
    city_token = slug(fix["city"])
    area_token = slug(fix["area"])
    daypart_token = slug(daypart)
    browse = (
        f"{city_token}__{area_token}__{date_token}__"
        f"{daypart_token}__{short_id(row['stock_clip_id'])}.mp4"
    )
    shoot_id = f"SHOOT__{date_token}__{city_token}__{daypart.upper()}"
    loc_rel = (
        Path("views")
        / "by-location"
        / slug(fix["country"])
        / slug(fix["region"])
        / city_token
        / area_token
        / browse
    ).as_posix()
    shoot_rel = (
        Path("views")
        / "by-shoot"
        / shoot_id
        / browse
    ).as_posix()
    return shoot_id, browse, loc_rel, shoot_rel


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--canonical-root", type=Path, required=True)
    p.add_argument("--truth-csv", type=Path, required=True)
    p.add_argument("--delta-catalog", type=Path, required=True)
    p.add_argument("--combined-catalog", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--write", action="store_true")
    args = p.parse_args()

    db = args.db.expanduser().resolve()
    root = args.canonical_root.expanduser().resolve()
    truth_path = args.truth_csv.expanduser().resolve()
    delta_path = args.delta_catalog.expanduser().resolve()
    combined_path = args.combined_catalog.expanduser().resolve()
    report_path = args.report.expanduser().resolve()

    truth_rows, truth_fields = read_csv(truth_path)
    delta_rows, delta_fields = read_csv(delta_path)
    combined_rows, combined_fields = read_csv(combined_path)

    truth = {row["stock_clip_id"]: row for row in truth_rows}
    delta = {row["stock_clip_id"]: row for row in delta_rows}
    combined = {row["stock_clip_id"]: row for row in combined_rows}

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    db_rows = {
        row["stock_clip_id"]: dict(row)
        for row in con.execute(
            """
            SELECT stock_clip_id, location_json
            FROM reconstructed_candidates
            WHERE active=1
              AND stock_clip_id IN (?,?,?,?,?)
            """,
            tuple(FIXES),
        ).fetchall()
    }

    if set(db_rows) != set(FIXES):
        raise SystemExit(
            f"Recovery DB mismatch: expected {sorted(FIXES)}, found {sorted(db_rows)}"
        )

    planned = []
    problems = []

    for sid, fix in FIXES.items():
        if sid not in truth or sid not in delta or sid not in combined:
            problems.append(f"{sid}: missing from truth/delta/combined artifact")
            continue

        row = delta[sid]
        master = root / row["canonical_master_relative_path"]
        old_loc = root / row["location_view_relative_path"]
        old_shoot = root / row["shoot_view_relative_path"]

        if not master.is_file():
            problems.append(f"{sid}: canonical master missing: {master}")
            continue
        for old_view in (old_loc, old_shoot):
            if not old_view.is_file():
                problems.append(f"{sid}: existing generic view missing: {old_view}")
            elif not same_inode(master, old_view):
                problems.append(
                    f"{sid}: existing view is not hardlinked to canonical master: {old_view}"
                )

        loc = parse_json(db_rows[sid]["location_json"])
        lat = float(loc.get("center_lat"))
        lon = float(loc.get("center_lon"))
        if abs(lat - fix["lat"]) > 0.00001 or abs(lon - fix["lon"]) > 0.00001:
            problems.append(
                f"{sid}: DB GPS changed: found ({lat},{lon}), expected "
                f"({fix['lat']},{fix['lon']})"
            )
            continue
        if loc.get("gps_kind") != "direct_reconstruction_master_srt_gps":
            problems.append(
                f"{sid}: expected direct reconstruction GPS, got {loc.get('gps_kind')!r}"
            )
            continue

        shoot_id, browse, new_loc_rel, new_shoot_rel = build_paths(row, fix)
        new_loc = root / new_loc_rel
        new_shoot = root / new_shoot_rel

        for new_view in (new_loc, new_shoot):
            if new_view.exists() and not same_inode(master, new_view):
                problems.append(
                    f"{sid}: corrected destination collision: {new_view}"
                )

        planned.append(
            {
                "stock_clip_id": sid,
                "old_location_view": str(old_loc),
                "new_location_view": str(new_loc),
                "old_shoot_view": str(old_shoot),
                "new_shoot_view": str(new_shoot),
                "old_city": row["capture_city"],
                "new_city": fix["city"],
                "old_area": row["canonical_area"],
                "new_area": fix["area"],
                "public_label": fix["public_label"],
                "shoot_id": shoot_id,
                "browse_filename": browse,
            }
        )

    con.close()

    print("QC RECOVERY GENERIC-GEOGRAPHY CORRECTION PREFLIGHT")
    print("=================================================")
    print("clips                :", len(planned))
    print("problems             :", len(problems))
    print("mode                 :", "WRITE" if args.write else "DRY RUN")
    print()

    if problems:
        for problem in problems:
            print(" -", problem)
        return 2

    for item in planned:
        print(item["stock_clip_id"])
        print(
            f"  {item['old_city']} / {item['old_area']}"
            f"  ->  {item['new_city']} / {item['new_area']}"
        )
        print(f"  {item['public_label']}")
        print(f"  {item['old_location_view']}")
        print(f"  -> {item['new_location_view']}")
        print()

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "mode": "write" if args.write else "dry_run",
                "corrections": planned,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    if not args.write:
        print("report:", report_path)
        print("QC RECOVERY GENERIC-GEOGRAPHY CORRECTION PREFLIGHT: PASS")
        return 0

    # Re-open transaction only after all filesystem/csv checks have passed.
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    created: list[Path] = []
    removed: list[tuple[Path, Path]] = []

    try:
        for sid, fix in FIXES.items():
            loc = parse_json(
                con.execute(
                    """
                    SELECT location_json
                    FROM reconstructed_candidates
                    WHERE stock_clip_id=?
                    """,
                    (sid,),
                ).fetchone()["location_json"]
            )

            loc["country"] = fix["country"]
            loc["state"] = fix["region"]
            loc["city"] = fix["city"]
            loc["public_label"] = fix["public_label"]

            if fix["area_kind"] == "neighborhood":
                loc["neighborhood"] = fix["area"]
                loc.pop("poi", None)
            else:
                loc["poi"] = fix["area"]
                # Remove the synthetic state/city fallback if present.
                neighborhood = str(loc.get("neighborhood") or "")
                if neighborhood in {"Washington", "California"}:
                    loc.pop("neighborhood", None)

            recovery = loc.get("recovery")
            if not isinstance(recovery, dict):
                recovery = {}
            recovery = dict(recovery)
            recovery["public_label_basis"] = "direct_gps_geography_correction"
            recovery["canonical_geography_correction"] = fix["verification"]
            recovery["canonical_geography_correction_version"] = "1"
            loc["recovery"] = recovery

            con.execute(
                """
                UPDATE reconstructed_candidates
                SET location_json=?
                WHERE stock_clip_id=?
                  AND active=1
                """,
                (
                    json.dumps(loc, sort_keys=True, separators=(",", ":")),
                    sid,
                ),
            )

            # Truth CSV row.
            t = truth[sid]
            t["public_label"] = fix["public_label"]
            if "public_label_basis" in truth_fields:
                t["public_label_basis"] = "direct_gps_geography_correction"
            if "location_json" in truth_fields:
                t["location_json"] = json.dumps(
                    loc, sort_keys=True, separators=(",", ":")
                )

            # Catalog rows.
            for mapping in (delta, combined):
                row = mapping[sid]
                shoot_id, browse, loc_rel, shoot_rel = build_paths(row, fix)

                row["country"] = fix["country"]
                row["region"] = fix["region"]
                row["capture_city"] = fix["city"]
                row["raw_neighborhood"] = (
                    fix["area"] if fix["area_kind"] == "neighborhood" else ""
                )
                row["raw_geocoder_label"] = fix["public_label"]
                row["canonical_area"] = fix["area"]
                row["canonical_area_method"] = "direct_gps_geography_correction"
                row["provisional_shoot_id"] = shoot_id
                row["browse_filename"] = browse
                row["location_view_relative_path"] = loc_rel
                row["shoot_view_relative_path"] = shoot_rel
                reasons = row.get("location_review_reasons", "")
                extra = f"direct_gps_geography_correction={fix['verification']}"
                row["location_review_reasons"] = (
                    reasons + ("; " if reasons else "") + extra
                )
                action = row.get("migration_action", "")
                if "geography_fix_v1" not in action:
                    row["migration_action"] = (
                        action + ("+" if action else "") + "geography_fix_v1"
                    )

        # Filesystem links: create corrected paths before removing old paths.
        for item in planned:
            sid = item["stock_clip_id"]
            row = delta[sid]
            master = root / row["canonical_master_relative_path"]
            new_loc = root / row["location_view_relative_path"]
            new_shoot = root / row["shoot_view_relative_path"]

            for dst in (new_loc, new_shoot):
                if dst.exists():
                    if not same_inode(master, dst):
                        raise RuntimeError(f"{sid}: corrected view collision: {dst}")
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.link(master, dst)
                    created.append(dst)

            old_loc = Path(
                next(
                    x["old_location_view"]
                    for x in planned
                    if x["stock_clip_id"] == sid
                )
            )
            old_shoot = Path(
                next(
                    x["old_shoot_view"]
                    for x in planned
                    if x["stock_clip_id"] == sid
                )
            )
            for old in (old_loc, old_shoot):
                if old.exists():
                    if not same_inode(master, old):
                        raise RuntimeError(
                            f"{sid}: refusing to remove non-hardlinked old view: {old}"
                        )
                    removed.append((old, master))
                    old.unlink()

        # Write CSVs atomically.
        write_csv(truth_path, truth_rows, truth_fields)
        write_csv(delta_path, list(delta.values()), delta_fields)

        # Preserve combined baseline order exactly; update only the mapped rows in place.
        updated_combined_rows = [
            combined[row["stock_clip_id"]]
            if row["stock_clip_id"] in combined
            else row
            for row in combined_rows
        ]
        write_csv(combined_path, updated_combined_rows, combined_fields)

        con.commit()

        # Final physical audit.
        masters = list((root / "masters").rglob("*.mp4"))
        location_views = list((root / "views" / "by-location").rglob("*.mp4"))
        shoot_views = list((root / "views" / "by-shoot").rglob("*.mp4"))

        if len(masters) != 390:
            raise RuntimeError(f"Expected 390 masters, got {len(masters)}")
        if len(location_views) != 390:
            raise RuntimeError(
                f"Expected 390 location views, got {len(location_views)}"
            )
        if len(shoot_views) != 390:
            raise RuntimeError(f"Expected 390 shoot views, got {len(shoot_views)}")

        for sid in FIXES:
            row = delta[sid]
            master = root / row["canonical_master_relative_path"]
            loc_view = root / row["location_view_relative_path"]
            shoot_view = root / row["shoot_view_relative_path"]
            if not same_inode(master, loc_view):
                raise RuntimeError(f"{sid}: corrected location view inode mismatch")
            if not same_inode(master, shoot_view):
                raise RuntimeError(f"{sid}: corrected shoot view inode mismatch")

    except Exception:
        con.rollback()
        # Remove newly created paths.
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        # Recreate old view links if they were removed.
        for old, master in removed:
            try:
                old.parent.mkdir(parents=True, exist_ok=True)
                if not old.exists():
                    os.link(master, old)
            except OSError:
                pass
        raise
    finally:
        con.close()

    print("QC RECOVERY GENERIC-GEOGRAPHY CORRECTION")
    print("========================================")
    print("corrected clips  : 5")
    print("canonical masters: 390")
    print("location views   : 390")
    print("shoot views      : 390")
    print("same-inode verify: PASS")
    print("report           :", report_path)
    print()
    print("QC RECOVERY GENERIC-GEOGRAPHY CORRECTION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
