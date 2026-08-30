#!/usr/bin/env python3
"""Re-apply a refined deterministic LUT policy to cached re-grade scene evidence.

No API calls are made. This reads regrade-plan-v1.csv, keeps the OpenAI-derived
scene evidence exactly as-is, and applies production-palette-policy-v2.

Policy v2 fixes the main v1 failure mode: distant/background snow should not
outvote the dominant subject or time-of-day. Temporal light gets first priority;
Icy Fjords is reserved for genuinely natural mountainous/fjord-like scenes.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

ROOT = (
    Path.home()
    / "Desktop"
    / "vclip-work"
    / "work"
    / "regrade-trial-vancouver-jan18-v1"
)
INPUT = ROOT / "regrade-plan-v1.csv"
OUTPUT = ROOT / "regrade-plan-v2.csv"
SUMMARY = ROOT / "regrade-plan-v2-summary.json"
POLICY_VERSION = "production-palette-policy-v2"

BUILT_CONTEXT = {
    "urban",
    "residential",
    "industrial",
    "rural_fields",
    "road_highway",
}


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
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


def scene_set(row: dict[str, str]) -> set[str]:
    return {
        value.strip()
        for value in (row.get("scene_types") or "").split("|")
        if value.strip()
    }


def recommend(row: dict[str, str]) -> dict[str, object]:
    lighting = row.get("lighting") or "unknown"
    weather = row.get("weather") or "unknown"
    water = row.get("water_type") or "unknown_water"
    water_prominence = row.get("water_prominence") or "none"
    sky_prominence = row.get("sky_prominence") or "none"
    snow = (row.get("snow_ice_visible") or "").upper() == "YES"
    scenes = scene_set(row)

    if lighting == "night":
        return {
            "lut": "Violet Night",
            "confidence": 0.96,
            "rule": "night",
            "reason": "Night light takes priority over scene-specific color cues.",
        }

    if lighting == "blue_hour":
        return {
            "lut": "Turquoise Delight",
            "confidence": 0.95,
            "rule": "blue_hour",
            "reason": "Blue-hour/post-sunset light takes priority over background scene cues.",
        }

    if lighting == "dark_sunset":
        return {
            "lut": "Dark Sunset",
            "confidence": 0.95,
            "rule": "dark_sunset",
            "reason": "Dark sunset maps directly to Dark Sunset.",
        }

    if lighting == "sunset":
        if sky_prominence in {"medium", "high"}:
            return {
                "lut": "Glowing Sky",
                "confidence": 0.93,
                "rule": "sunset_prominent_sky",
                "reason": "Sunset with a meaningful sky maps to Glowing Sky.",
            }
        return {
            "lut": "Golden Light",
            "confidence": 0.90,
            "rule": "sunset_low_sky",
            "reason": "Sunset without a dominant sky maps to Golden Light.",
        }

    if lighting == "golden_hour":
        return {
            "lut": "Golden Light",
            "confidence": 0.92,
            "rule": "golden_hour",
            "reason": "Golden-hour light takes priority over distant snow or water cues.",
        }

    if (
        water == "shallow_turquoise_water"
        and water_prominence in {"medium", "high"}
    ):
        return {
            "lut": "Shallow Seas",
            "confidence": 0.95,
            "rule": "shallow_turquoise_water",
            "reason": "Prominent shallow/turquoise water maps to Shallow Seas.",
        }

    natural_mountain_scene = (
        snow
        and "mountain" in scenes
        and not (scenes & BUILT_CONTEXT)
        and bool(scenes & {"mountain", "forest", "coastline", "lake", "river"})
    )
    if natural_mountain_scene:
        return {
            "lut": "Icy Fjords",
            "confidence": 0.89,
            "rule": "dominant_natural_snow_mountain",
            "reason": (
                "Snow/ice is paired with a natural mountain/fjord scene and is not merely "
                "background to an urban, residential, rural, or road subject."
            ),
        }

    if (
        water != "none"
        and water_prominence in {"medium", "high"}
        and (
            water == "grey_water"
            or weather in {"overcast", "fog_mist", "rain_wet"}
        )
    ):
        return {
            "lut": "Grey Waters",
            "confidence": 0.91,
            "rule": "grey_overcast_water",
            "reason": "Prominent water under grey/overcast conditions maps to Grey Waters.",
        }

    if water == "ocean_sea" and water_prominence in {"medium", "high"}:
        return {
            "lut": "Ocean Blues",
            "confidence": 0.91,
            "rule": "prominent_ocean",
            "reason": "Prominent ocean/sea maps to Ocean Blues.",
        }

    if lighting in {"bright_daylight", "neutral_daylight", "warm_daylight"}:
        return {
            "lut": "Natural Glare",
            "confidence": 0.86,
            "rule": "ordinary_daylight",
            "reason": (
                "Ordinary daylight with no stronger dominant palette cue maps to Natural Glare."
            ),
        }

    if lighting == "flat_overcast":
        return {
            "lut": "Natural Glare",
            "confidence": 0.70,
            "rule": "overcast_nonwater_fallback",
            "reason": (
                "No stronger approved-palette cue is dominant; Natural Glare is the neutral fallback."
            ),
        }

    return {
        "lut": "Natural Glare",
        "confidence": 0.62,
        "rule": "neutral_fallback",
        "reason": "No decisive approved-palette cue; Natural Glare is the neutral fallback.",
    }


def main() -> int:
    if not INPUT.is_file():
        raise SystemExit(f"Missing input plan: {INPUT}")

    rows = read_rows(INPUT)
    out: list[dict[str, str]] = []
    changed: list[dict[str, str]] = []

    for row in rows:
        old_lut = row.get("recommended_lut") or ""
        old_rule = row.get("recommendation_rule") or ""
        result = recommend(row)
        confidence = float(result["confidence"])
        new_lut = str(result["lut"])
        new_rule = str(result["rule"])
        status = "AUTO_READY" if confidence >= 0.85 else "REVIEW"

        updated = dict(row)
        updated.update(
            {
                "v1_recommended_lut": old_lut,
                "v1_recommendation_rule": old_rule,
                "recommended_lut": new_lut,
                "recommendation_confidence": f"{confidence:.2f}",
                "recommendation_rule": new_rule,
                "recommendation_reason": str(result["reason"]),
                "recommendation_status": status,
                "palette_policy_version": POLICY_VERSION,
                "recommendation_changed_from_v1": "YES" if new_lut != old_lut else "NO",
            }
        )
        out.append(updated)
        if new_lut != old_lut:
            changed.append(updated)

    write_rows(OUTPUT, out)

    lut_counts = Counter(row["recommended_lut"] for row in out)
    status_counts = Counter(row["recommendation_status"] for row in out)
    change_counts = Counter(
        (row["v1_recommended_lut"], row["recommended_lut"]) for row in changed
    )

    summary = {
        "policy_version": POLICY_VERSION,
        "rows": len(out),
        "changed_from_v1": len(changed),
        "lut_recommendations": dict(lut_counts),
        "recommendation_status": dict(status_counts),
        "changed_pairs": [
            {"from": old, "to": new, "count": count}
            for (old, new), count in change_counts.most_common()
        ],
    }
    SUMMARY.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print("VCLIP RE-GRADE POLICY V2")
    print("========================")
    print("rows            :", len(out))
    print("changed from v1 :", len(changed))
    print()
    print("LUT RECOMMENDATIONS")
    print("-------------------")
    for lut, count in lut_counts.most_common():
        print(f"{count:5d}  {lut}")
    print()
    print("RECOMMENDATION STATUS")
    print("---------------------")
    for status, count in status_counts.most_common():
        print(f"{count:5d}  {status}")
    print()
    print("CHANGED DECISIONS")
    print("-----------------")
    for row in changed:
        print()
        print(row["stock_clip_id"])
        print("project :", row["project_name"])
        print("caption :", row["scene_caption"])
        print("scene   :", row["lighting"], "/", row["weather"], "/", row["scene_types"])
        print("water   :", row["water_type"], row["water_prominence"])
        print("v1      :", row["v1_recommended_lut"], row["v1_recommendation_rule"])
        print("v2      :", row["recommended_lut"], row["recommendation_rule"])
        print("status  :", row["recommendation_status"])
    print()
    print("csv    :", OUTPUT)
    print("summary:", SUMMARY)
    print("VCLIP RE-GRADE POLICY V2: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
