#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


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
            try:
                if item not in (None, ""):
                    return float(item)
            except (TypeError, ValueError):
                pass
    return None


def location_label(raw: str | None) -> str:
    obj = parse_json(raw)
    label = first_string(
        obj,
        ("public_label", "structured_location_label", "location_label", "place_label"),
    )
    if label:
        return label

    neighborhood = first_string(
        obj,
        ("neighborhood", "neighbourhood", "district", "suburb", "quarter"),
    )
    city = first_string(
        obj,
        ("city", "city_name", "locality", "town", "municipality"),
    )
    state = first_string(obj, ("state", "state_name", "region", "province"))
    country = first_string(obj, ("country", "country_name"))

    parts: list[str] = []
    for item in (neighborhood, city, state, country):
        if item and item not in parts:
            parts.append(item)
    return ", ".join(parts)


def safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def haversine_meters(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    radius_m = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius_m * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def merge_evidence_sources(obj: dict[str, Any], *sources: str) -> None:
    existing = obj.get("evidence_sources")
    values: list[str] = []
    if isinstance(existing, list):
        values.extend(str(x) for x in existing if x)
    elif isinstance(existing, str) and existing.strip():
        values.append(existing.strip())
    for source in sources:
        if source and source not in values:
            values.append(source)
    obj["evidence_sources"] = values


def latest_parent_rows(
    con: sqlite3.Connection,
    parent_ids: list[str],
) -> dict[str, dict[str, Any]]:
    placeholders = ",".join("?" for _ in parent_ids)
    rows = con.execute(
        f"""
        WITH ranked AS (
            SELECT
                sc.*,
                ROW_NUMBER() OVER (
                    PARTITION BY stock_clip_id
                    ORDER BY
                        COALESCE(updated_at, created_at, '') DESC,
                        rowid DESC
                ) AS rn
            FROM stock_candidates sc
            WHERE stock_clip_id IN ({placeholders})
              AND eligibility_status='accepted'
        )
        SELECT *
        FROM ranked
        WHERE rn=1
        """,
        parent_ids,
    ).fetchall()
    return {
        str(row["stock_clip_id"]): dict(row)
        for row in rows
    }


def scope_public_label(scope_prefix: str) -> str:
    value = (scope_prefix or "").strip()
    # Reconstruction scopes generally end in " — YYYY-MM-DD".
    value = re.sub(r"\s+—\s+\d{4}-\d{2}-\d{2}\s*$", "", value).strip()
    return value


def parent_geo(row: dict[str, Any]) -> tuple[float, float] | None:
    obj = parse_json(row.get("location_json"))
    lat = first_number(
        obj,
        (
            "center_lat",
            "latitude",
            "lat",
            "representative_lat",
        ),
    )
    lon = first_number(
        obj,
        (
            "center_lon",
            "longitude",
            "lon",
            "lng",
            "representative_lon",
        ),
    )
    if lat is None or lon is None:
        return None
    return lat, lon


def choose_parent_for_direct_gps(
    *,
    sid: str,
    parent_ids: list[str],
    located: dict[str, dict[str, Any]],
    template_parent_id: str,
    direct_lat: float,
    direct_lon: float,
    scope_prefix: str,
) -> tuple[dict[str, Any], str, str, list[str], float | None]:
    labels = sorted(
        {
            location_label(row.get("location_json"))
            for row in located.values()
            if location_label(row.get("location_json"))
        }
    )

    # Clean case: all located parents agree.
    if len(labels) == 1:
        if template_parent_id in located:
            chosen_id = template_parent_id
        else:
            chosen_id = next(
                (pid for pid in parent_ids if pid in located),
                sorted(located)[0],
            )
        return (
            located[chosen_id],
            chosen_id,
            "parent_consensus",
            labels,
            None,
        )

    # Strongest resolver for a conflict: the generated master itself has
    # direct SRT GPS, so choose the historical parent whose stored location
    # center is geographically nearest to that exact master-range GPS.
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    for pid in parent_ids:
        row = located.get(pid)
        if row is None:
            continue
        geo = parent_geo(row)
        if geo is None:
            continue
        distance = haversine_meters(
            direct_lat,
            direct_lon,
            geo[0],
            geo[1],
        )
        candidates.append((distance, pid, row))

    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]))
        distance, chosen_id, chosen = candidates[0]
        return (
            chosen,
            chosen_id,
            "nearest_parent_to_direct_gps",
            labels,
            distance,
        )

    # Last-resort label carrier only. We still keep the direct coordinates as
    # the actual private geographic truth. Prefer the template parent's metadata
    # shape, then any located parent, and label it with the reconstruction scope.
    scope_label = scope_public_label(scope_prefix)
    if not located:
        raise RuntimeError(
            f"{sid}: direct GPS exists but no accepted parent has location metadata"
        )

    if template_parent_id in located:
        chosen_id = template_parent_id
    else:
        chosen_id = next(
            (pid for pid in parent_ids if pid in located),
            sorted(located)[0],
        )
    chosen = dict(located[chosen_id])

    if not scope_label:
        raise RuntimeError(
            f"{sid}: parent labels conflict, no parent coordinates, and no usable scope label"
        )

    # Signal to the caller that the scope label should replace the parent's
    # conflicting public label.
    chosen["_vclip_scope_public_label"] = scope_label
    return (
        chosen,
        chosen_id,
        "scope_label_with_direct_gps",
        labels,
        None,
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Apply final QC recovery location truth to the isolated recovery DB. "
            "Direct reconstruction SRT GPS is authoritative for the 77 GPS-backed "
            "masters; parent consensus is required only for SRT-less fallbacks."
        )
    )
    p.add_argument("--recovery-db", type=Path, required=True)
    p.add_argument("--main-db", type=Path, required=True)
    p.add_argument("--evidence-csv", type=Path, required=True)
    p.add_argument("--output-csv", type=Path, required=True)
    p.add_argument("--write", action="store_true")
    return p


def main() -> int:
    args = parser().parse_args()

    recovery_db = args.recovery_db.expanduser().resolve()
    main_db = args.main_db.expanduser().resolve()
    evidence_csv = args.evidence_csv.expanduser().resolve()
    output_csv = args.output_csv.expanduser().resolve()

    evidence_rows = read_csv(evidence_csv)
    evidence = {
        row["stock_clip_id"]: row
        for row in evidence_rows
    }

    rcon = sqlite3.connect(recovery_db)
    rcon.row_factory = sqlite3.Row
    rcon.execute("PRAGMA foreign_keys=ON")

    mcon = sqlite3.connect(main_db)
    mcon.row_factory = sqlite3.Row

    active = {
        str(row["stock_clip_id"]): dict(row)
        for row in rcon.execute(
            """
            SELECT *
            FROM reconstructed_candidates
            WHERE active=1
            ORDER BY stock_clip_id
            """
        ).fetchall()
    }

    if len(active) != 96:
        raise SystemExit(
            f"Expected 96 active recovery candidates, got {len(active)}"
        )
    if set(evidence) != set(active):
        raise SystemExit(
            f"Evidence/DB ID mismatch: evidence={len(evidence)} active={len(active)}"
        )

    out_rows: list[dict[str, Any]] = []
    parent_links: list[tuple[str, str]] = []
    conflict_rows: list[dict[str, Any]] = []

    for sid in sorted(active):
        e = evidence[sid]
        parent_ids = [
            value
            for value in (e.get("parent_ids") or "").split("|")
            if value
        ]
        if not parent_ids:
            raise RuntimeError(
                f"{sid}: no parent_ids in reconstruction evidence"
            )

        parents = latest_parent_rows(mcon, parent_ids)
        located = {
            pid: row
            for pid, row in parents.items()
            if location_label(row.get("location_json"))
        }
        labels = sorted(
            {
                location_label(row.get("location_json"))
                for row in located.values()
                if location_label(row.get("location_json"))
            }
        )
        template_parent_id = e.get("template_parent_id") or ""

        direct = e.get("gps_available") == "YES"

        chosen_parent_distance_m: float | None = None

        if direct:
            direct_lat = safe_float(e.get("center_lat"))
            direct_lon = safe_float(e.get("center_lon"))
            if direct_lat is None or direct_lon is None:
                raise RuntimeError(
                    f"{sid}: direct GPS flag set but center is missing"
                )

            (
                chosen,
                chosen_parent_id,
                label_basis,
                labels,
                chosen_parent_distance_m,
            ) = choose_parent_for_direct_gps(
                sid=sid,
                parent_ids=parent_ids,
                located=located,
                template_parent_id=template_parent_id,
                direct_lat=direct_lat,
                direct_lon=direct_lon,
                scope_prefix=e.get("scope_prefix") or "",
            )

            if len(labels) > 1:
                conflict_rows.append(
                    {
                        "stock_clip_id": sid,
                        "parent_labels": " || ".join(labels),
                        "resolution": label_basis,
                        "chosen_parent_id": chosen_parent_id,
                        "chosen_parent_distance_m": (
                            round(chosen_parent_distance_m, 3)
                            if chosen_parent_distance_m is not None
                            else ""
                        ),
                        "scope_prefix": e.get("scope_prefix") or "",
                    }
                )

            loc = parse_json(chosen.get("location_json"))
            override_scope_label = chosen.get("_vclip_scope_public_label")
            if override_scope_label:
                loc["public_label"] = override_scope_label

            public_label = location_label(json.dumps(loc))
            if not public_label:
                raise RuntimeError(
                    f"{sid}: could not derive a conservative public label"
                )

            # Direct reconstruction-range GPS is the private geographic truth.
            parent_lat = first_number(
                loc,
                ("center_lat", "latitude", "lat", "representative_lat"),
            )
            parent_lon = first_number(
                loc,
                ("center_lon", "longitude", "lon", "lng", "representative_lon"),
            )
            direct_delta_m = None
            if parent_lat is not None and parent_lon is not None:
                direct_delta_m = haversine_meters(
                    direct_lat,
                    direct_lon,
                    parent_lat,
                    parent_lon,
                )

            loc["center_lat"] = direct_lat
            loc["center_lon"] = direct_lon

            samples = safe_float(e.get("sample_count"))
            radius = safe_float(e.get("radius_meters"))
            if samples is not None:
                loc["sample_count"] = int(samples)
                loc["valid_sample_count"] = int(samples)
            if radius is not None:
                loc["radius_meters"] = radius

            loc["direct_source_gps"] = True
            loc["gps_kind"] = "direct_reconstruction_master_srt_gps"
            loc["private_precision"] = "exact_clip_gps_internal_only"
            loc["confidence"] = "high"
            loc["review_required"] = False

            if label_basis == "parent_consensus":
                merge_evidence_sources(
                    loc,
                    "reconstruction_master_srt_gps",
                    "historical_parent_location_consensus",
                )
            elif label_basis == "nearest_parent_to_direct_gps":
                merge_evidence_sources(
                    loc,
                    "reconstruction_master_srt_gps",
                    "nearest_historical_parent_public_label",
                )
            else:
                merge_evidence_sources(
                    loc,
                    "reconstruction_master_srt_gps",
                    "reconstruction_scope_public_label",
                )

            method = "DIRECT_RECONSTRUCTION_SRT_GPS"
            consensus = len(labels) == 1

        else:
            # For SRT-less masters, historical parents are the strongest
            # location evidence. Here we require actual consensus.
            if not labels:
                raise RuntimeError(
                    f"{sid}: no accepted parent location for SRT-less fallback"
                )
            if len(labels) != 1:
                raise RuntimeError(
                    f"{sid}: SRT-less parent location conflict: {' || '.join(labels)}"
                )

            if template_parent_id in located:
                chosen_parent_id = template_parent_id
            else:
                chosen_parent_id = next(
                    (pid for pid in parent_ids if pid in located),
                    sorted(located)[0],
                )
            chosen = located[chosen_parent_id]
            loc = parse_json(chosen.get("location_json"))
            public_label = labels[0]
            if (
                not isinstance(loc.get("public_label"), str)
                or not loc.get("public_label", "").strip()
            ):
                loc["public_label"] = public_label

            loc["direct_source_gps"] = False
            loc["gps_kind"] = "historical_parent_location_consensus"
            merge_evidence_sources(
                loc,
                "historical_parent_location_consensus",
            )
            method = "PARENT_LOCATION_CONSENSUS"
            label_basis = "parent_consensus"
            consensus = True
            direct_delta_m = None

        recovery = loc.get("recovery")
        if not isinstance(recovery, dict):
            recovery = {}
        recovery = dict(recovery)
        recovery.update(
            {
                "qc_recovery_version": "qc-recovery-v1",
                "historical_parent_consensus": consensus,
                "public_label_basis": label_basis,
                "chosen_parent_id": chosen_parent_id,
                "parent_ids": parent_ids,
                "parent_labels": labels,
            }
        )
        if chosen_parent_distance_m is not None:
            recovery["chosen_parent_distance_m"] = round(
                chosen_parent_distance_m,
                3,
            )
        loc["recovery"] = recovery

        capture = parse_json(chosen.get("capture_time_json"))
        loc_json = json.dumps(
            loc,
            sort_keys=True,
            separators=(",", ":"),
        )
        capture_json = json.dumps(
            capture,
            sort_keys=True,
            separators=(",", ":"),
        )

        for parent_id in parent_ids:
            parent_links.append((sid, parent_id))

        out_rows.append(
            {
                "stock_clip_id": sid,
                "location_method": method,
                "public_label": public_label,
                "public_label_basis": label_basis,
                "parent_labels": " || ".join(labels),
                "center_lat": loc.get("center_lat", ""),
                "center_lon": loc.get("center_lon", ""),
                "sample_count": loc.get("sample_count", ""),
                "radius_meters": loc.get("radius_meters", ""),
                "chosen_parent_id": chosen_parent_id,
                "chosen_parent_distance_m": (
                    round(chosen_parent_distance_m, 3)
                    if chosen_parent_distance_m is not None
                    else ""
                ),
                "parent_ids": "|".join(parent_ids),
                "direct_vs_chosen_parent_center_delta_m": (
                    round(direct_delta_m, 3)
                    if direct_delta_m is not None
                    else ""
                ),
                "location_json": loc_json,
                "capture_time_json": capture_json,
            }
        )

    mcon.close()

    counts = Counter(
        row["location_method"]
        for row in out_rows
    )
    basis_counts = Counter(
        row["public_label_basis"]
        for row in out_rows
    )

    if counts["DIRECT_RECONSTRUCTION_SRT_GPS"] != 77:
        raise RuntimeError(
            "Expected 77 direct GPS rows, got "
            f"{counts['DIRECT_RECONSTRUCTION_SRT_GPS']}"
        )
    if counts["PARENT_LOCATION_CONSENSUS"] != 19:
        raise RuntimeError(
            "Expected 19 parent fallback rows, got "
            f"{counts['PARENT_LOCATION_CONSENSUS']}"
        )

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=list(out_rows[0].keys()),
        )
        writer.writeheader()
        writer.writerows(out_rows)

    conflict_csv = output_csv.with_name(
        output_csv.stem + "--direct-gps-parent-conflicts.csv"
    )
    if conflict_rows:
        with conflict_csv.open(
            "w",
            newline="",
            encoding="utf-8",
        ) as f:
            writer = csv.DictWriter(
                f,
                fieldnames=list(conflict_rows[0].keys()),
            )
            writer.writeheader()
            writer.writerows(conflict_rows)
    elif conflict_csv.exists():
        conflict_csv.unlink()

    print("QC RECOVERY LOCATION TRUTH V2")
    print("=============================")
    print("active candidates       :", len(active))
    print("direct SRT GPS          :", counts["DIRECT_RECONSTRUCTION_SRT_GPS"])
    print("parent fallback         :", counts["PARENT_LOCATION_CONSENSUS"])
    print("direct-GPS parent conflicts:", len(conflict_rows))
    print("parent links            :", len(set(parent_links)))
    print("output                  :", output_csv)
    if conflict_rows:
        print("conflict audit          :", conflict_csv)
    print("mode                    :", "WRITE" if args.write else "DRY RUN")

    print()
    print("PUBLIC LABEL BASIS")
    print("------------------")
    for key, value in basis_counts.most_common():
        print(f"{value:4d}  {key}")

    if conflict_rows:
        print()
        print("DIRECT-GPS PARENT CONFLICTS")
        print("---------------------------")
        for row in conflict_rows:
            print(
                f"{row['stock_clip_id']}  "
                f"{row['parent_labels']}  ->  "
                f"{row['resolution']}  "
                f"{row['chosen_parent_distance_m']}m"
            )

    if not args.write:
        rcon.close()
        print()
        print("QC RECOVERY LOCATION TRUTH V2 PREFLIGHT: PASS")
        return 0

    try:
        rcon.execute(
            "DELETE FROM reconstructed_candidate_parents"
        )
        rcon.executemany(
            """
            INSERT OR IGNORE INTO reconstructed_candidate_parents(
                stock_clip_id,
                parent_stock_clip_id
            ) VALUES(?,?)
            """,
            sorted(set(parent_links)),
        )

        by_id = {
            row["stock_clip_id"]: row
            for row in out_rows
        }
        for sid in sorted(active):
            row = by_id[sid]
            rcon.execute(
                """
                UPDATE reconstructed_candidates
                SET location_json=?,
                    capture_time_json=?
                WHERE stock_clip_id=?
                  AND active=1
                """,
                (
                    row["location_json"],
                    row["capture_time_json"],
                    sid,
                ),
            )

        located_count = rcon.execute(
            """
            SELECT COUNT(*)
            FROM reconstructed_candidates
            WHERE active=1
              AND location_json NOT IN ('', '{}')
            """
        ).fetchone()[0]

        parent_child_count = rcon.execute(
            """
            SELECT COUNT(DISTINCT stock_clip_id)
            FROM reconstructed_candidate_parents
            """
        ).fetchone()[0]

        if located_count != 96:
            raise RuntimeError(
                f"Expected 96 located rows, got {located_count}"
            )
        if parent_child_count != 96:
            raise RuntimeError(
                "Expected parent links for 96 candidates, got "
                f"{parent_child_count}"
            )

        try:
            rcon.execute(
                "DELETE FROM reconstructed_candidates_fts"
            )
            rows = rcon.execute(
                """
                SELECT
                    stock_clip_id,
                    project_name,
                    event_name,
                    source_name,
                    location_json,
                    product_role,
                    candidate_tier,
                    orientation
                FROM reconstructed_candidates
                WHERE active=1
                """
            ).fetchall()

            rcon.executemany(
                """
                INSERT INTO reconstructed_candidates_fts(
                    stock_clip_id,
                    project_name,
                    event_name,
                    source_name,
                    location_text,
                    product_role,
                    candidate_tier,
                    orientation
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        row["stock_clip_id"],
                        row["project_name"],
                        row["event_name"],
                        row["source_name"],
                        location_label(row["location_json"]),
                        row["product_role"],
                        row["candidate_tier"],
                        row["orientation"],
                    )
                    for row in rows
                ],
            )
        except sqlite3.OperationalError:
            pass

        rcon.commit()

    except Exception:
        rcon.rollback()
        raise
    finally:
        rcon.close()

    print()
    print("located active rows : 96")
    print("parent-linked rows  : 96")
    print("QC RECOVERY LOCATION TRUTH V2: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
