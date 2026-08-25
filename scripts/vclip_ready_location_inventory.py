#!/usr/bin/env python3
"""Inventory location coverage for active VClip Ready Cuts.

Reads reconstructed_candidates from the VClip SQLite DB and reports:
- total/known/unknown Ready Cuts
- country/state/city/neighborhood coverage
- counts + duration + landscape/vertical mix by public location label
- suspiciously over-specific labels that may need metadata cleanup
- CSV + JSON outputs for further analysis

This is read-only.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PREFERRED_LABEL_KEYS = (
    "public_label",
    "structured_location_label",
    "location_label",
    "place_label",
)

FIELD_ALIASES = {
    "country": ("country", "country_name"),
    "state": ("state", "state_name", "region", "province"),
    "city": ("city", "city_name", "locality", "town", "municipality"),
    "neighborhood": (
        "neighborhood",
        "neighbourhood",
        "district",
        "suburb",
        "quarter",
    ),
}

SUSPICIOUS_LABEL_PATTERNS = [
    re.compile(r"^\d{1,6}[-\s]"),
    re.compile(r"\b(parking|garage|museum|restaurant|seafood|store|shop|hotel|school|hospital|building)\b", re.I),
]


def walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def first_string(obj: Any, keys: tuple[str, ...]) -> str | None:
    for d in walk_dicts(obj):
        for key in keys:
            value = d.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def public_label(obj: Any) -> str:
    value = first_string(obj, PREFERRED_LABEL_KEYS)
    if value:
        return value

    neighborhood = first_string(obj, FIELD_ALIASES["neighborhood"])
    city = first_string(obj, FIELD_ALIASES["city"])
    state = first_string(obj, FIELD_ALIASES["state"])
    country = first_string(obj, FIELD_ALIASES["country"])

    parts = []
    for item in (neighborhood, city, state, country):
        if item and item not in parts:
            parts.append(item)
    return ", ".join(parts)


def parse_location(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {}

    out = {
        "label": public_label(obj),
        "country": first_string(obj, FIELD_ALIASES["country"]) or "",
        "state": first_string(obj, FIELD_ALIASES["state"]) or "",
        "city": first_string(obj, FIELD_ALIASES["city"]) or "",
        "neighborhood": first_string(obj, FIELD_ALIASES["neighborhood"]) or "",
    }
    return out


def suspicious_label(label: str) -> bool:
    if not label:
        return False
    return any(p.search(label) for p in SUSPICIOUS_LABEL_PATTERNS)


def normalize(value: str) -> str:
    return " ".join(value.split()).strip()


def run(args: argparse.Namespace) -> int:
    db = args.db.expanduser().resolve()
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    rows = con.execute(
        """
        SELECT stock_clip_id,
               project_name,
               duration_seconds,
               orientation,
               candidate_tier,
               location_json
        FROM reconstructed_candidates
        WHERE active=1
          AND product_role='ready_cut'
        ORDER BY stock_clip_id
        """
    ).fetchall()
    con.close()

    enriched = []
    for row in rows:
        loc = parse_location(row["location_json"])
        enriched.append(
            {
                "stock_clip_id": row["stock_clip_id"],
                "project_name": row["project_name"],
                "duration_seconds": float(row["duration_seconds"] or 0),
                "orientation": row["orientation"] or "unknown",
                "candidate_tier": row["candidate_tier"] or "",
                "location_label": normalize(loc.get("label", "")),
                "country": normalize(loc.get("country", "")),
                "state": normalize(loc.get("state", "")),
                "city": normalize(loc.get("city", "")),
                "neighborhood": normalize(loc.get("neighborhood", "")),
            }
        )

    total = len(enriched)
    known = [r for r in enriched if r["location_label"]]
    unknown = [r for r in enriched if not r["location_label"]]

    print("VCLIP READY CUT LOCATION INVENTORY")
    print("=================================")
    print(f"Ready Cuts:          {total:,}")
    print(f"Known location:      {len(known):,} ({(100*len(known)/total if total else 0):.1f}%)")
    print(f"Unknown location:    {len(unknown):,} ({(100*len(unknown)/total if total else 0):.1f}%)")
    print(f"Unique labels:       {len({r['location_label'] for r in known}):,}")
    print(f"Unique cities:       {len({r['city'] for r in known if r['city']}):,}")
    print(f"Unique states:       {len({r['state'] for r in known if r['state']}):,}")
    print(f"Unique countries:    {len({r['country'] for r in known if r['country']}):,}")

    def print_counter(title: str, field: str, limit: int):
        c = Counter(r[field] for r in known if r[field])
        print()
        print(title)
        print("-" * len(title))
        for value, n in c.most_common(limit):
            duration = sum(r["duration_seconds"] for r in known if r[field] == value)
            land = sum(1 for r in known if r[field] == value and r["orientation"] == "landscape")
            vert = sum(1 for r in known if r[field] == value and r["orientation"] == "vertical")
            print(f"{n:4d}  {duration/60:7.1f} min  L={land:4d} V={vert:4d}  {value}")

    print_counter("COUNTRIES", "country", args.top)
    print_counter("STATES / REGIONS", "state", args.top)
    print_counter("CITIES", "city", args.top)
    print_counter("NEIGHBORHOODS / DISTRICTS", "neighborhood", args.top)

    by_label = defaultdict(list)
    for r in known:
        by_label[r["location_label"]].append(r)

    label_rows = []
    for label, items in by_label.items():
        label_rows.append(
            {
                "location_label": label,
                "ready_cuts": len(items),
                "duration_minutes": sum(x["duration_seconds"] for x in items) / 60,
                "landscape": sum(x["orientation"] == "landscape" for x in items),
                "vertical": sum(x["orientation"] == "vertical" for x in items),
                "tier_a": sum(x["candidate_tier"].startswith("A ") for x in items),
                "tier_b": sum(x["candidate_tier"].startswith("B ") for x in items),
                "tier_c": sum(x["candidate_tier"].startswith("C ") for x in items),
                "suspicious_label": suspicious_label(label),
            }
        )
    label_rows.sort(key=lambda x: (-x["ready_cuts"], x["location_label"].casefold()))

    print()
    print("PUBLIC LOCATION LABELS")
    print("----------------------")
    for row in label_rows[: args.labels]:
        marker = "  [CHECK]" if row["suspicious_label"] else ""
        print(
            f"{row['ready_cuts']:4d}  {row['duration_minutes']:7.1f} min  "
            f"L={row['landscape']:4d} V={row['vertical']:4d}  "
            f"{row['location_label']}{marker}"
        )

    suspicious = [x for x in label_rows if x["suspicious_label"]]
    print()
    print(f"Suspicious / over-specific public labels: {len(suspicious):,}")
    for row in suspicious[:30]:
        print(f"  {row['ready_cuts']:4d}  {row['location_label']}")

    print()
    print("UNKNOWN LOCATION READY CUTS")
    print("---------------------------")
    for row in unknown[:30]:
        print(f"  {row['stock_clip_id']}  {row['project_name']}")
    if len(unknown) > 30:
        print(f"  ... {len(unknown)-30:,} more")

    if args.output_dir:
        outdir = args.output_dir.expanduser().resolve()
        outdir.mkdir(parents=True, exist_ok=True)

        labels_csv = outdir / "ready-cut-location-labels.csv"
        with labels_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(label_rows[0].keys()) if label_rows else [
                "location_label","ready_cuts","duration_minutes","landscape","vertical",
                "tier_a","tier_b","tier_c","suspicious_label"
            ])
            w.writeheader()
            w.writerows(label_rows)

        cuts_csv = outdir / "ready-cut-location-inventory.csv"
        with cuts_csv.open("w", newline="", encoding="utf-8") as f:
            fields = list(enriched[0].keys()) if enriched else [
                "stock_clip_id","project_name","duration_seconds","orientation",
                "candidate_tier","location_label","country","state","city","neighborhood"
            ]
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(enriched)

        summary_json = outdir / "ready-cut-location-summary.json"
        summary_json.write_text(
            json.dumps(
                {
                    "ready_cuts": total,
                    "known_location": len(known),
                    "unknown_location": len(unknown),
                    "unique_labels": len({r["location_label"] for r in known}),
                    "unique_cities": sorted({r["city"] for r in known if r["city"]}),
                    "unique_states": sorted({r["state"] for r in known if r["state"]}),
                    "unique_countries": sorted({r["country"] for r in known if r["country"]}),
                    "location_labels": label_rows,
                    "unknown_ready_cuts": unknown,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        print()
        print(f"CSV:  {labels_csv}")
        print(f"CSV:  {cuts_csv}")
        print(f"JSON: {summary_json}")

    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--top", type=int, default=50)
    p.add_argument("--labels", type=int, default=100)
    p.add_argument("--output-dir", type=Path)
    return p


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
