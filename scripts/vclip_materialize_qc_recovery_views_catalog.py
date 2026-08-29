#!/usr/bin/env python3
"""Build QC Recovery v1 canonical views and a combined 390-row canonical catalog.

Prerequisites:
- 96 recovery masters already exist under VClip Library/masters/<shard>/VCLIP_<id>.mp4
- the isolated recovery DB has 96 active candidates, location truth, capture-time
  metadata, and 96 rendered_masters rows
- the existing 294-row canonical catalog is intact

Dry-run is default. --write creates:
- one hardlinked views/by-location entry per recovery master
- one hardlinked views/by-shoot entry per recovery master
- a 96-row delta catalog
- a 390-row combined catalog preserving all 294 old rows verbatim by column value

The immutable masters are never modified.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


CATALOG_COLUMNS = [
    "stock_clip_id",
    "canonical_master_filename",
    "canonical_master_relative_path",
    "legacy_filename",
    "legacy_path",
    "source_name",
    "source_path",
    "source_start_seconds",
    "duration_seconds",
    "location_authority",
    "location_authority_rank",
    "evidence_lat",
    "evidence_lon",
    "direct_radius_m",
    "direct_window_samples",
    "country",
    "region",
    "capture_city",
    "raw_neighborhood",
    "raw_geocoder_label",
    "canonical_area",
    "canonical_area_method",
    "capture_date",
    "capture_time",
    "capture_daypart",
    "provisional_shoot_id",
    "browse_filename",
    "location_view_relative_path",
    "shoot_view_relative_path",
    "candidate_db",
    "legacy_project_name",
    "location_review_status",
    "location_review_reasons",
    "migration_action",
]


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def parse_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def first_string(value: Any, keys: tuple[str, ...]) -> str:
    for obj in walk_dicts(value):
        for key in keys:
            item = obj.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def first_number(value: Any, keys: tuple[str, ...]) -> float | None:
    for obj in walk_dicts(value):
        for key in keys:
            item = obj.get(key)
            if item in (None, ""):
                continue
            try:
                return float(item)
            except (TypeError, ValueError):
                pass
    return None


def slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    text = re.sub(r"_+", "_", text)
    return text or "Unknown"


def id_short(stock_clip_id: str) -> str:
    if not stock_clip_id.startswith("VCLIP_"):
        raise ValueError(f"Invalid VClip ID: {stock_clip_id}")
    return stock_clip_id[6:14]


def id_shard(stock_clip_id: str) -> str:
    if not stock_clip_id.startswith("VCLIP_") or len(stock_clip_id) < 8:
        raise ValueError(f"Invalid VClip ID: {stock_clip_id}")
    return stock_clip_id[6:8]


def same_inode(a: Path, b: Path) -> bool:
    sa = os.stat(a)
    sb = os.stat(b)
    return sa.st_dev == sb.st_dev and sa.st_ino == sb.st_ino


def normalize_country(value: str, region: str) -> str:
    text = value.strip()
    aliases = {
        "USA": "United States",
        "US": "United States",
        "U.S.": "United States",
        "United States of America": "United States",
    }
    if text in aliases:
        return aliases[text]
    if text:
        return text
    if region in {
        "Washington",
        "California",
        "Oregon",
        "Nevada",
        "New York",
        "Virginia",
    }:
        return "United States"
    if region in {"British Columbia", "Alberta", "Ontario"}:
        return "Canada"
    return ""


def public_label(location: dict[str, Any]) -> str:
    return first_string(
        location,
        (
            "public_label",
            "structured_location_label",
            "location_label",
            "place_label",
        ),
    )


def structured_location(location: dict[str, Any]) -> dict[str, str]:
    country = first_string(location, ("country", "country_name"))
    region = first_string(
        location,
        ("state", "state_name", "region", "province"),
    )
    city = first_string(
        location,
        ("city", "city_name", "locality", "town", "municipality"),
    )
    neighborhood = first_string(
        location,
        (
            "neighborhood",
            "neighbourhood",
            "district",
            "suburb",
            "quarter",
        ),
    )
    poi = first_string(
        location,
        ("poi", "point_of_interest", "place_name"),
    )
    raw_label = first_string(
        location,
        (
            "raw_geocoder_label",
            "display_name",
            "geocoder_label",
            "formatted_address",
            "public_label",
            "structured_location_label",
            "location_label",
            "place_label",
        ),
    )
    country = normalize_country(country, region)
    return {
        "country": country,
        "region": region,
        "city": city,
        "neighborhood": neighborhood,
        "poi": poi,
        "raw_label": raw_label,
        "public_label": public_label(location),
    }


def public_label_prefix(label: str) -> str:
    if not label:
        return ""
    return label.split(",", 1)[0].strip()


def derive_area(loc: dict[str, str]) -> tuple[str, str]:
    city = loc["city"]
    neighborhood = loc["neighborhood"]
    poi = loc["poi"]
    prefix = public_label_prefix(loc["public_label"])

    if neighborhood and neighborhood.casefold() != city.casefold():
        return neighborhood, "structured_neighborhood"

    if poi and poi.casefold() != city.casefold():
        return poi, "structured_poi"

    if (
        prefix
        and prefix.casefold() not in {
            city.casefold(),
            loc["region"].casefold(),
            loc["country"].casefold(),
        }
    ):
        return prefix, "public_label_prefix"

    if city:
        return city, "city_fallback"

    if prefix:
        return prefix, "public_label_prefix"

    if loc["region"]:
        return loc["region"], "region_fallback"

    return "", "unresolved"


def derive_city(loc: dict[str, str], area: str) -> tuple[str, str]:
    if loc["city"]:
        return loc["city"], "structured_city"

    prefix = public_label_prefix(loc["public_label"])
    if prefix and prefix.casefold() != loc["region"].casefold():
        return prefix, "public_label_prefix"

    if area:
        return area, "area_fallback"

    return "", "unresolved"


def parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def derive_capture(capture: dict[str, Any], project_name: str) -> dict[str, str]:
    date_value = first_string(
        capture,
        ("date", "capture_date", "captured_date", "local_date"),
    )
    local_value = first_string(
        capture,
        (
            "captured_at_local",
            "capture_datetime_local",
            "local_datetime",
            "captured_at",
            "capture_datetime",
        ),
    )
    dt = parse_datetime(local_value)

    if not date_value and dt is not None:
        date_value = dt.date().isoformat()

    time_value = first_string(
        capture,
        ("capture_time", "time", "local_time"),
    )
    if not time_value and dt is not None:
        time_value = dt.strftime("%H:%M:%S")

    daypart_literal = first_string(
        capture,
        ("capture_daypart", "daypart", "time_of_day", "label"),
    ).casefold()

    mapping = {
        "morning_golden_hour": "Morning",
        "morning": "Morning",
        "midday": "Midday",
        "afternoon": "Afternoon",
        "evening_golden_hour": "Evening",
        "evening": "Evening",
        "blue_hour": "Evening",
        "night": "Night",
    }

    daypart = mapping.get(daypart_literal, "")

    if not daypart and dt is not None:
        hour = dt.hour + dt.minute / 60
        if 5 <= hour < 11:
            daypart = "Morning"
        elif 11 <= hour < 15:
            daypart = "Midday"
        elif 15 <= hour < 18:
            daypart = "Afternoon"
        elif 18 <= hour < 22:
            daypart = "Evening"
        else:
            daypart = "Night"

    if not daypart:
        lowered = project_name.casefold()
        for token, label in (
            ("morning", "Morning"),
            ("midday", "Midday"),
            ("afternoon", "Afternoon"),
            ("evening", "Evening"),
            ("night", "Night"),
        ):
            if token in lowered:
                daypart = label
                break

    return {
        "date": date_value,
        "time": time_value,
        "daypart": daypart,
    }


def location_authority(location: dict[str, Any]) -> tuple[str, str]:
    kind = first_string(location, ("gps_kind",))
    if kind == "direct_reconstruction_master_srt_gps":
        return "reconstruction_master_srt_gps", "1"
    if kind == "historical_parent_location_consensus":
        return "historical_parent_location_consensus", "2"
    return kind or "unknown", "9"


def review_reason(location: dict[str, Any]) -> str:
    recovery = location.get("recovery")
    if not isinstance(recovery, dict):
        recovery = {}
    basis = str(recovery.get("public_label_basis") or "")
    labels = recovery.get("parent_labels")
    if isinstance(labels, list):
        label_text = " | ".join(str(x) for x in labels)
    else:
        label_text = str(labels or "")
    parts = [
        "qc_recovery_v1",
        f"public_label_basis={basis}" if basis else "",
        f"parent_labels={label_text}" if label_text else "",
    ]
    return "; ".join(x for x in parts if x)


def view_paths(
    *,
    country: str,
    region: str,
    city: str,
    area: str,
    date: str,
    daypart: str,
    stock_clip_id: str,
) -> tuple[str, str, str, str]:
    date_token = date.replace("-", "_")
    city_token = slug(city)
    area_token = slug(area)
    daypart_token = slug(daypart)
    short = id_short(stock_clip_id)

    shoot_id = (
        f"SHOOT__{date_token}__{city_token}__{daypart.upper()}"
    )
    browse = (
        f"{city_token}__{area_token}__{date_token}__"
        f"{daypart_token}__{short}.mp4"
    )
    location_rel = (
        Path("views")
        / "by-location"
        / slug(country)
        / slug(region)
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
    return shoot_id, browse, location_rel, shoot_rel


def write_catalog(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=CATALOG_COLUMNS,
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {column: row.get(column, "") for column in CATALOG_COLUMNS}
            )
    temp.replace(path)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--canonical-root", type=Path, required=True)
    p.add_argument("--existing-catalog", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    p.add_argument("--write", action="store_true")
    args = p.parse_args()

    db = args.db.expanduser().resolve()
    root = args.canonical_root.expanduser().resolve()
    existing_catalog = args.existing_catalog.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()

    old_rows, old_columns = read_csv(existing_catalog)

    if old_columns != CATALOG_COLUMNS:
        raise SystemExit(
            "Existing catalog column contract differs from expected.\n"
            f"Expected: {CATALOG_COLUMNS}\n"
            f"Actual:   {old_columns}"
        )

    old_ids = {
        row["stock_clip_id"]
        for row in old_rows
        if row.get("stock_clip_id")
    }
    if len(old_rows) != 294 or len(old_ids) != 294:
        raise SystemExit(
            f"Expected canonical baseline 294 unique rows; got "
            f"{len(old_rows)} rows / {len(old_ids)} IDs"
        )

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT
            c.stock_clip_id,
            c.project_name,
            c.source_name,
            c.source_path,
            c.source_start_seconds,
            c.duration_seconds,
            c.location_json,
            c.capture_time_json,
            r.exported_filename,
            r.exported_path,
            r.sha256
        FROM reconstructed_candidates c
        JOIN rendered_masters r USING(stock_clip_id)
        WHERE c.active=1
        ORDER BY c.stock_clip_id
        """
    ).fetchall()
    con.close()

    if len(rows) != 96:
        raise SystemExit(
            f"Expected 96 active rendered recovery rows, got {len(rows)}"
        )

    new_ids = {str(row["stock_clip_id"]) for row in rows}
    overlap = sorted(old_ids & new_ids)
    if overlap:
        raise SystemExit(
            "Recovery IDs already exist in baseline catalog: "
            + ", ".join(overlap[:20])
        )

    new_catalog: list[dict[str, Any]] = []
    problems: list[str] = []
    location_counts: Counter[str] = Counter()
    daypart_counts: Counter[str] = Counter()
    authority_counts: Counter[str] = Counter()
    area_method_counts: Counter[str] = Counter()

    for row in rows:
        sid = str(row["stock_clip_id"])
        location = parse_json(row["location_json"])
        capture = parse_json(row["capture_time_json"])

        loc = structured_location(location)
        area, area_method = derive_area(loc)
        city, city_method = derive_city(loc, area)
        cap = derive_capture(capture, str(row["project_name"]))

        country = loc["country"]
        region = loc["region"]

        missing = [
            name
            for name, value in (
                ("country", country),
                ("region", region),
                ("city", city),
                ("area", area),
                ("capture_date", cap["date"]),
                ("capture_daypart", cap["daypart"]),
            )
            if not value
        ]
        if missing:
            problems.append(
                f"{sid}: unresolved canonical fields: {','.join(missing)}; "
                f"public_label={loc['public_label']!r}; project={row['project_name']!r}"
            )
            continue

        authority, rank = location_authority(location)
        evidence_lat = first_number(
            location,
            ("center_lat", "latitude", "lat", "representative_lat"),
        )
        evidence_lon = first_number(
            location,
            ("center_lon", "longitude", "lon", "lng", "representative_lon"),
        )
        radius = first_number(location, ("radius_meters", "direct_radius_m"))
        samples = first_number(
            location,
            ("valid_sample_count", "sample_count", "direct_window_samples"),
        )

        master_rel = (
            Path("masters") / id_shard(sid) / f"{sid}.mp4"
        ).as_posix()
        master = root / master_rel

        if not master.is_file():
            problems.append(
                f"{sid}: canonical master missing: {master}"
            )
            continue

        shoot_id, browse, loc_rel, shoot_rel = view_paths(
            country=country,
            region=region,
            city=city,
            area=area,
            date=cap["date"],
            daypart=cap["daypart"],
            stock_clip_id=sid,
        )

        new_catalog.append(
            {
                "stock_clip_id": sid,
                "canonical_master_filename": f"{sid}.mp4",
                "canonical_master_relative_path": master_rel,
                "legacy_filename": row["exported_filename"],
                "legacy_path": row["exported_path"],
                "source_name": row["source_name"],
                "source_path": row["source_path"] or "",
                "source_start_seconds": row["source_start_seconds"],
                "duration_seconds": row["duration_seconds"],
                "location_authority": authority,
                "location_authority_rank": rank,
                "evidence_lat": evidence_lat if evidence_lat is not None else "",
                "evidence_lon": evidence_lon if evidence_lon is not None else "",
                "direct_radius_m": radius if radius is not None else "",
                "direct_window_samples": int(samples) if samples is not None else "",
                "country": country,
                "region": region,
                "capture_city": city,
                "raw_neighborhood": loc["neighborhood"],
                "raw_geocoder_label": loc["raw_label"] or loc["public_label"],
                "canonical_area": area,
                "canonical_area_method": area_method,
                "capture_date": cap["date"],
                "capture_time": cap["time"],
                "capture_daypart": cap["daypart"],
                "provisional_shoot_id": shoot_id,
                "browse_filename": browse,
                "location_view_relative_path": loc_rel,
                "shoot_view_relative_path": shoot_rel,
                "candidate_db": str(db),
                "legacy_project_name": row["project_name"],
                "location_review_status": "accepted",
                "location_review_reasons": (
                    review_reason(location)
                    + (
                        f"; city_basis={city_method}"
                        if city_method != "structured_city"
                        else ""
                    )
                ).strip("; "),
                "migration_action": "add_qc_recovery_v1",
            }
        )

        location_counts[f"{country} / {region} / {city} / {area}"] += 1
        daypart_counts[cap["daypart"]] += 1
        authority_counts[authority] += 1
        area_method_counts[area_method] += 1

    if problems:
        print("QC RECOVERY VIEW/CATALOG PREFLIGHT: FAILED")
        for problem in problems[:40]:
            print(" -", problem)
        return 2

    if len(new_catalog) != 96:
        raise SystemExit(
            f"Expected 96 proposed catalog rows, got {len(new_catalog)}"
        )

    location_paths = [
        row["location_view_relative_path"] for row in new_catalog
    ]
    shoot_paths = [
        row["shoot_view_relative_path"] for row in new_catalog
    ]
    browse_names = [row["browse_filename"] for row in new_catalog]

    for label, values in (
        ("location view", location_paths),
        ("shoot view", shoot_paths),
    ):
        if len(values) != len(set(values)):
            duplicates = [
                value
                for value, count in Counter(values).items()
                if count > 1
            ]
            raise SystemExit(
                f"Duplicate proposed {label} paths: {duplicates[:20]}"
            )

    if len(browse_names) != len(set(browse_names)):
        duplicates = [
            value
            for value, count in Counter(browse_names).items()
            if count > 1
        ]
        raise SystemExit(
            f"Duplicate proposed browse filenames: {duplicates[:20]}"
        )

    link_actions: list[tuple[Path, Path, str]] = []
    link_problems: list[str] = []
    to_create = 0
    already_linked = 0

    for row in new_catalog:
        master = root / row["canonical_master_relative_path"]
        for rel in (
            row["location_view_relative_path"],
            row["shoot_view_relative_path"],
        ):
            dst = root / rel
            if dst.exists():
                if not dst.is_file():
                    link_problems.append(
                        f"{row['stock_clip_id']}: view exists but is not a file: {dst}"
                    )
                elif same_inode(master, dst):
                    already_linked += 1
                    link_actions.append((master, dst, "already_hardlinked"))
                else:
                    link_problems.append(
                        f"{row['stock_clip_id']}: view collision with different inode: {dst}"
                    )
            else:
                to_create += 1
                link_actions.append((master, dst, "create_hardlink"))

    if link_problems:
        print("QC RECOVERY VIEW/CATALOG PREFLIGHT: FAILED")
        for problem in link_problems[:40]:
            print(" -", problem)
        return 2

    output_root.mkdir(parents=True, exist_ok=True)
    delta_path = output_root / "canonical-master-plan-qc-recovery-96-v1.csv"
    combined_path = output_root / "canonical-master-plan-all-390-v2.csv"
    report_path = output_root / "canonical-master-plan-all-390-v2-summary.json"

    write_catalog(delta_path, new_catalog)

    print("QC RECOVERY CANONICAL VIEW/CATALOG PREFLIGHT")
    print("============================================")
    print("baseline catalog rows  :", len(old_rows))
    print("recovery catalog rows  :", len(new_catalog))
    print("combined catalog rows  :", len(old_rows) + len(new_catalog))
    print("existing physical views:", sum(
        1 for p in (root / "views").rglob("*.mp4")
    ))
    print("view hardlinks to create:", to_create)
    print("views already linked    :", already_linked)
    print("projected physical views:", 588 + 192)
    print("mode                    :", "WRITE" if args.write else "DRY RUN")
    print()

    print("LOCATION AUTHORITY")
    print("------------------")
    for key, count in authority_counts.most_common():
        print(f"{count:4d}  {key}")

    print()
    print("DAYPARTS")
    print("--------")
    for key, count in daypart_counts.most_common():
        print(f"{count:4d}  {key}")

    print()
    print("AREA METHODS")
    print("------------")
    for key, count in area_method_counts.most_common():
        print(f"{count:4d}  {key}")

    print()
    print("TOP RECOVERY LOCATION VIEWS")
    print("---------------------------")
    for key, count in location_counts.most_common(30):
        print(f"{count:4d}  {key}")

    if not args.write:
        report = {
            "mode": "dry_run",
            "baseline_rows": len(old_rows),
            "recovery_rows": len(new_catalog),
            "combined_rows": len(old_rows) + len(new_catalog),
            "view_hardlinks_to_create": to_create,
            "views_already_linked": already_linked,
            "location_authority": dict(authority_counts),
            "dayparts": dict(daypart_counts),
            "area_methods": dict(area_method_counts),
            "delta_catalog": str(delta_path),
            "combined_catalog": str(combined_path),
        }
        report_path.write_text(
            json.dumps(report, indent=2) + "\n",
            encoding="utf-8",
        )
        print()
        print("delta catalog   :", delta_path)
        print("combined target :", combined_path)
        print("summary         :", report_path)
        print("QC RECOVERY CANONICAL VIEW/CATALOG PREFLIGHT: PASS")
        return 0

    created: list[Path] = []

    try:
        for index, (master, dst, action) in enumerate(link_actions, 1):
            if action == "already_hardlinked":
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.link(master, dst)
            created.append(dst)
            if not same_inode(master, dst):
                raise RuntimeError(
                    f"Hardlink inode verification failed: {dst}"
                )
            if index % 40 == 0 or index == len(link_actions):
                print(f"  materialized views {index}/{len(link_actions)}")

        combined_rows: list[dict[str, Any]] = []
        combined_rows.extend(old_rows)
        combined_rows.extend(new_catalog)

        if len(combined_rows) != 390:
            raise RuntimeError(
                f"Expected 390 combined rows, got {len(combined_rows)}"
            )
        if len({row["stock_clip_id"] for row in combined_rows}) != 390:
            raise RuntimeError("Combined catalog stock_clip_id uniqueness failed")

        write_catalog(combined_path, combined_rows)

        physical_masters = list((root / "masters").rglob("*.mp4"))
        physical_location_views = list(
            (root / "views" / "by-location").rglob("*.mp4")
        )
        physical_shoot_views = list(
            (root / "views" / "by-shoot").rglob("*.mp4")
        )

        if len(physical_masters) != 390:
            raise RuntimeError(
                f"Expected 390 physical masters, found {len(physical_masters)}"
            )
        if len(physical_location_views) != 390:
            raise RuntimeError(
                f"Expected 390 location views, found {len(physical_location_views)}"
            )
        if len(physical_shoot_views) != 390:
            raise RuntimeError(
                f"Expected 390 shoot views, found {len(physical_shoot_views)}"
            )

        for row in new_catalog:
            master = root / row["canonical_master_relative_path"]
            location_view = root / row["location_view_relative_path"]
            shoot_view = root / row["shoot_view_relative_path"]
            if not same_inode(master, location_view):
                raise RuntimeError(
                    f"{row['stock_clip_id']}: location view is not hardlinked to master"
                )
            if not same_inode(master, shoot_view):
                raise RuntimeError(
                    f"{row['stock_clip_id']}: shoot view is not hardlinked to master"
                )

    except Exception:
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            combined_path.unlink()
        except OSError:
            pass
        raise

    report = {
        "mode": "write",
        "baseline_rows": 294,
        "recovery_rows": 96,
        "combined_rows": 390,
        "created_view_hardlinks": len(created),
        "views_already_linked": already_linked,
        "physical_masters": 390,
        "physical_location_views": 390,
        "physical_shoot_views": 390,
        "physical_views_total": 780,
        "recovery_view_inode_verification": "PASS",
        "location_authority": dict(authority_counts),
        "dayparts": dict(daypart_counts),
        "area_methods": dict(area_method_counts),
        "delta_catalog": str(delta_path),
        "combined_catalog": str(combined_path),
    }
    report_path.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )

    print()
    print("QC RECOVERY CANONICAL VIEW/CATALOG MATERIALIZATION")
    print("==================================================")
    print("combined catalog rows :", 390)
    print("canonical masters     :", 390)
    print("location views        :", 390)
    print("shoot views           :", 390)
    print("physical views total  :", 780)
    print("new view hardlinks    :", len(created))
    print("same-inode verify     : PASS")
    print("delta catalog         :", delta_path)
    print("combined catalog      :", combined_path)
    print("summary               :", report_path)
    print()
    print("QC RECOVERY CANONICAL VIEW/CATALOG MATERIALIZATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
