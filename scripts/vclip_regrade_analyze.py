#!/usr/bin/env python3
"""Analyze exact legacy ready-cut ranges and recommend VClip Production Palette v1 LUTs.

This is stage 2 of the Vancouver January 18, 2025 re-grade trial.

The model does NOT choose a LUT. It sees six deterministic frames from the exact
historical source range and returns structured visible scene evidence. A small,
versioned VClip policy then selects one approved production LUT from that
evidence plus the declared daypart.

The source frames come from original media, not the old creative grade. DJI
D-Log M footage may therefore look flat; the prompt explicitly tells the model
to judge scene content, weather and lighting structure rather than saturation.

Dry-run is the default. Pass --write to make OpenAI calls. Per-clip JSON results
are cached so interrupted runs can resume without paying twice.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from vclip_pipeline.errors import VClipError
from vclip_pipeline.workflow.providers.openai import (
    OpenAIResponsesClient,
    OpenAIVisualAnalyzer,
)


PROMPT_VERSION = "vclip-regrade-scene-v1"
POLICY_VERSION = "production-palette-policy-v1"
MODEL_DEFAULT = "gpt-5-mini"
FRAME_POSITIONS = (0.10, 0.25, 0.40, 0.60, 0.75, 0.90)
MAX_DIMENSION = 1024
JPEG_QUALITY = 3

DEFAULT_ROOT = (
    Path.home()
    / "Desktop"
    / "vclip-work"
    / "work"
    / "regrade-trial-vancouver-jan18-v1"
)

LIGHTING = [
    "bright_daylight",
    "neutral_daylight",
    "warm_daylight",
    "golden_hour",
    "sunset",
    "dark_sunset",
    "blue_hour",
    "night",
    "flat_overcast",
    "mixed",
    "unknown",
]
WEATHER = [
    "clear",
    "partly_cloudy",
    "overcast",
    "fog_mist",
    "rain_wet",
    "snow_ice",
    "mixed",
    "unknown",
]
SCENE_TYPES = [
    "urban",
    "residential",
    "industrial",
    "rural_fields",
    "mountain",
    "forest",
    "coastline",
    "lake",
    "river",
    "harbor_marina",
    "park",
    "road_highway",
    "mixed",
]
WATER_TYPES = [
    "none",
    "ocean_sea",
    "lake",
    "river",
    "harbor_marina",
    "grey_water",
    "clear_blue_water",
    "shallow_turquoise_water",
    "unknown_water",
]
PROMINENCE = ["none", "low", "medium", "high"]

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "caption": {"type": "string"},
        "lighting": {"type": "string", "enum": LIGHTING},
        "weather": {"type": "string", "enum": WEATHER},
        "scene_types": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {"type": "string", "enum": SCENE_TYPES},
        },
        "water_type": {"type": "string", "enum": WATER_TYPES},
        "water_prominence": {"type": "string", "enum": PROMINENCE},
        "sky_prominence": {"type": "string", "enum": PROMINENCE},
        "snow_ice_visible": {"type": "boolean"},
        "visual_temperature": {
            "type": "string",
            "enum": ["cool", "neutral", "warm", "mixed", "unknown"],
        },
        "evidence": {"type": "string"},
    },
    "required": [
        "caption",
        "lighting",
        "weather",
        "scene_types",
        "water_type",
        "water_prominence",
        "sky_prominence",
        "snow_ice_visible",
        "visual_temperature",
        "evidence",
    ],
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
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


def declared_daypart(project_name: str) -> str:
    value = project_name.casefold()
    for label, tokens in [
        ("Blue Hour", ("blue hour",)),
        ("Morning", ("morning",)),
        ("Midday", ("midday",)),
        ("Afternoon", ("afternoon",)),
        ("Evening", ("evening",)),
        ("Night", ("night",)),
    ]:
        if any(token in value for token in tokens):
            return label
    return "Unknown"


def cache_key(row: dict[str, str]) -> str:
    payload = "|".join(
        [
            row["stock_clip_id"],
            row["resolved_source_media"],
            row["source_start_s"],
            row["duration_s"],
            PROMPT_VERSION,
            POLICY_VERSION,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def extract_frames(row: dict[str, str], cache_root: Path) -> tuple[Path, ...]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required but was not found")

    source = Path(row["resolved_source_media"])
    if not source.is_file():
        raise RuntimeError(f"source media missing: {source}")
    start = float(row["source_start_s"])
    duration = float(row["duration_s"])
    if duration <= 0:
        raise RuntimeError("candidate duration is not positive")

    directory = cache_root / f"{row['stock_clip_id']}-{cache_key(row)}"
    expected = tuple(
        directory / f"frame-{index:02d}.jpg"
        for index in range(1, len(FRAME_POSITIONS) + 1)
    )
    if all(path.is_file() for path in expected):
        return expected

    directory.mkdir(parents=True, exist_ok=True)
    for index, (position, output) in enumerate(
        zip(FRAME_POSITIONS, expected, strict=True),
        start=1,
    ):
        timestamp = start + max(0.0, min(duration - 0.001, duration * position))
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp:.6f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            (
                f"scale={MAX_DIMENSION}:{MAX_DIMENSION}:"
                "force_original_aspect_ratio=decrease"
            ),
            "-q:v",
            str(JPEG_QUALITY),
            "-y",
            str(output),
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "ffmpeg failed").strip()
            raise RuntimeError(
                f"frame extraction failed at sample {index}: {detail}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"frame extraction timed out at sample {index}"
            ) from exc
    return expected


def visual_prompt(row: dict[str, str]) -> str:
    daypart = declared_daypart(row["project_name"])
    return json.dumps(
        {
            "task": (
                "Analyze six ordered frames from one exact historical drone-footage cut. "
                "Describe visible scene content, lighting structure, weather, water and sky. "
                "Do not choose a color LUT. Do not judge the old grade. These frames come from "
                "original source media and may be DJI D-Log M, so flat contrast or low saturation "
                "is not evidence of overcast weather. Use structural visual cues instead."
            ),
            "rules": [
                "Only report what is visibly supported across the frames.",
                "A place name in project metadata is provenance, not proof of visible content.",
                "Declared daypart may be used as temporal context but frames remain primary evidence.",
                "If weather cannot be distinguished reliably, choose unknown.",
                "Use grey_water only when the water itself visually reads grey/steel rather than merely unsaturated D-Log.",
                "Use shallow_turquoise_water only when visibly supported by water appearance/depth cues.",
            ],
            "context": {
                "stock_clip_id": row["stock_clip_id"],
                "project_name": row["project_name"],
                "declared_daypart": daypart,
                "capture_time_from_filename": row.get("capture_time_from_filename") or None,
                "source_color_note": "Original media may be DJI D-Log M",
            },
            "required": {
                "caption": "one factual sentence",
                "lighting": LIGHTING,
                "weather": WEATHER,
                "scene_types": SCENE_TYPES,
                "water_type": WATER_TYPES,
                "water_prominence": PROMINENCE,
                "sky_prominence": PROMINENCE,
                "snow_ice_visible": "boolean",
                "visual_temperature": ["cool", "neutral", "warm", "mixed", "unknown"],
                "evidence": "short explanation of the visual cues",
            },
            "prompt_version": PROMPT_VERSION,
        },
        ensure_ascii=False,
    )


def call_openai(
    client: OpenAIResponsesClient,
    row: dict[str, str],
    frames: tuple[Path, ...],
    model: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {"type": "input_text", "text": visual_prompt(row)}
    ]
    for frame in frames:
        encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{encoded}",
                "detail": "low",
            }
        )
    payload = {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "max_output_tokens": 1800,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "vclip_regrade_scene",
                "strict": True,
                "schema": SCHEMA,
            }
        },
    }
    response = client.post(payload)
    text = OpenAIVisualAnalyzer._response_text(response)
    parsed = OpenAIVisualAnalyzer._parse_json(text)
    usage = OpenAIVisualAnalyzer.parse_usage(response, model=model)
    return parsed, usage.as_dict()


def lut_policy(scene: dict[str, Any], daypart: str) -> dict[str, Any]:
    lighting = str(scene["lighting"])
    weather = str(scene["weather"])
    water = str(scene["water_type"])
    water_prominence = str(scene["water_prominence"])
    sky = str(scene["sky_prominence"])
    snow = bool(scene["snow_ice_visible"])

    if lighting == "night" or daypart == "Night":
        return {
            "recommended_lut": "Violet Night",
            "confidence": 0.96,
            "rule": "night",
            "reason": "Night scene maps to Violet Night.",
        }

    if lighting == "blue_hour" or daypart == "Blue Hour":
        return {
            "recommended_lut": "Turquoise Delight",
            "confidence": 0.95,
            "rule": "blue_hour",
            "reason": "Blue-hour / post-sunset light maps to Turquoise Delight.",
        }

    if lighting == "dark_sunset":
        return {
            "recommended_lut": "Dark Sunset",
            "confidence": 0.95,
            "rule": "dark_sunset",
            "reason": "Dark sunset maps to Dark Sunset.",
        }

    if lighting == "sunset":
        if sky in {"medium", "high"}:
            return {
                "recommended_lut": "Glowing Sky",
                "confidence": 0.93,
                "rule": "sunset_prominent_sky",
                "reason": "Sunset with a meaningful sky maps to Glowing Sky.",
            }
        return {
            "recommended_lut": "Golden Light",
            "confidence": 0.89,
            "rule": "sunset_low_sky",
            "reason": "Sunset without a dominant sky maps to Golden Light.",
        }

    if snow or weather == "snow_ice":
        return {
            "recommended_lut": "Icy Fjords",
            "confidence": 0.95,
            "rule": "snow_ice",
            "reason": "Visible snow/ice maps to Icy Fjords.",
        }

    if water == "shallow_turquoise_water" and water_prominence in {"medium", "high"}:
        return {
            "recommended_lut": "Shallow Seas",
            "confidence": 0.95,
            "rule": "shallow_turquoise_water",
            "reason": "Prominent shallow/turquoise water maps to Shallow Seas.",
        }

    if (
        water == "grey_water"
        or (
            water_prominence in {"medium", "high"}
            and weather in {"overcast", "fog_mist", "rain_wet"}
            and water != "none"
        )
    ):
        return {
            "recommended_lut": "Grey Waters",
            "confidence": 0.91,
            "rule": "grey_overcast_water",
            "reason": "Prominent grey/overcast water maps to Grey Waters.",
        }

    if water == "ocean_sea" and water_prominence in {"medium", "high"}:
        return {
            "recommended_lut": "Ocean Blues",
            "confidence": 0.91,
            "rule": "prominent_ocean",
            "reason": "Prominent ocean/sea maps to Ocean Blues.",
        }

    if lighting == "golden_hour":
        return {
            "recommended_lut": "Golden Light",
            "confidence": 0.88,
            "rule": "golden_hour",
            "reason": "Golden-hour light maps to Golden Light.",
        }

    if lighting in {"bright_daylight", "neutral_daylight", "warm_daylight"}:
        return {
            "recommended_lut": "Natural Glare",
            "confidence": 0.86,
            "rule": "ordinary_daylight",
            "reason": "Ordinary daylight maps to Natural Glare.",
        }

    if lighting == "flat_overcast":
        return {
            "recommended_lut": "Natural Glare",
            "confidence": 0.70,
            "rule": "overcast_nonwater_fallback",
            "reason": "No stronger palette-specific cue; Natural Glare is the neutral approved fallback.",
        }

    return {
        "recommended_lut": "Natural Glare",
        "confidence": 0.62,
        "rule": "neutral_fallback",
        "reason": "No decisive palette-specific cue; Natural Glare is the neutral approved fallback.",
    }


def flatten_result(
    row: dict[str, str],
    scene: dict[str, Any],
    policy: dict[str, Any],
    usage: dict[str, Any],
) -> dict[str, Any]:
    confidence = float(policy["confidence"])
    review_state = "AUTO_READY" if confidence >= 0.85 else "REVIEW"
    return {
        **row,
        "declared_daypart": declared_daypart(row["project_name"]),
        "scene_caption": scene["caption"],
        "lighting": scene["lighting"],
        "weather": scene["weather"],
        "scene_types": " | ".join(scene["scene_types"]),
        "water_type": scene["water_type"],
        "water_prominence": scene["water_prominence"],
        "sky_prominence": scene["sky_prominence"],
        "snow_ice_visible": "YES" if scene["snow_ice_visible"] else "NO",
        "visual_temperature": scene["visual_temperature"],
        "visual_evidence": scene["evidence"],
        "recommended_lut": policy["recommended_lut"],
        "recommendation_confidence": f"{confidence:.2f}",
        "recommendation_rule": policy["rule"],
        "recommendation_reason": policy["reason"],
        "recommendation_status": review_state,
        "analysis_prompt_version": PROMPT_VERSION,
        "palette_policy_version": POLICY_VERSION,
        "analysis_model": usage.get("model") or "",
        "estimated_cost_usd": usage.get("estimated_total_cost_usd"),
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    p.add_argument("--model", default=MODEL_DEFAULT)
    p.add_argument("--limit", type=int)
    p.add_argument("--write", action="store_true")
    p.add_argument("--force", action="store_true")
    return p


def main() -> int:
    args = parser().parse_args()
    root = args.root.expanduser().resolve()
    candidates_path = root / "candidates.csv"
    if not candidates_path.is_file():
        raise SystemExit(f"Candidate CSV not found: {candidates_path}")

    candidates = [
        row
        for row in read_csv(candidates_path)
        if row.get("regrade_eligible") == "YES"
    ]
    if args.limit is not None:
        candidates = candidates[: max(0, args.limit)]

    cache_root = root / "frame-cache"
    result_root = root / "scene-analysis-cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    result_root.mkdir(parents=True, exist_ok=True)

    print("VCLIP RE-GRADE SCENE ANALYSIS PREFLIGHT")
    print("=======================================")
    print("candidates       :", len(candidates))
    print("model            :", args.model)
    print("prompt version   :", PROMPT_VERSION)
    print("policy version   :", POLICY_VERSION)
    print("mode             :", "WRITE" if args.write else "DRY RUN")
    print("frame cache      :", cache_root)
    print()

    if not args.write:
        for row in candidates:
            result_path = result_root / f"{row['stock_clip_id']}.json"
            print(
                row["stock_clip_id"],
                "cached" if result_path.is_file() and not args.force else "would_analyze",
                row["project_name"],
            )
        print()
        print("DRY RUN: pass --write to analyze exact source ranges.")
        return 0

    client = OpenAIResponsesClient()
    results: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    analyzed = 0
    cached = 0
    total_cost = 0.0

    for index, row in enumerate(candidates, start=1):
        stock_id = row["stock_clip_id"]
        result_path = result_root / f"{stock_id}.json"
        print(f"{index}/{len(candidates)}  {stock_id}  {row['project_name']}")
        try:
            if result_path.is_file() and not args.force:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                scene = payload["scene"]
                usage = payload.get("usage") or {}
                cached += 1
                print("  cached")
            else:
                frames = extract_frames(row, cache_root)
                scene, usage = call_openai(client, row, frames, args.model)
                result_path.write_text(
                    json.dumps(
                        {
                            "stock_clip_id": stock_id,
                            "prompt_version": PROMPT_VERSION,
                            "scene": scene,
                            "usage": usage,
                            "frames": [str(path) for path in frames],
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                analyzed += 1
                cost = usage.get("estimated_total_cost_usd")
                if cost is not None:
                    total_cost += float(cost)
                print(
                    "  analyzed",
                    f"cost=${float(cost):.5f}" if cost is not None else "cost=?",
                )

            policy = lut_policy(scene, declared_daypart(row["project_name"]))
            flat = flatten_result(row, scene, policy, usage)
            results.append(flat)
            print(
                "  ->",
                policy["recommended_lut"],
                f"confidence={policy['confidence']:.2f}",
                policy["rule"],
            )
        except Exception as exc:
            failed.append(
                {
                    "stock_clip_id": stock_id,
                    "project_name": row["project_name"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            print("  FAILED", failed[-1]["error"])

    results_path = root / "regrade-plan-v1.csv"
    failures_path = root / "scene-analysis-failures.csv"
    write_csv(results_path, results)
    write_csv(failures_path, failed)

    lut_counts = Counter(row["recommended_lut"] for row in results)
    rule_counts = Counter(row["recommendation_rule"] for row in results)
    status_counts = Counter(row["recommendation_status"] for row in results)

    summary = {
        "candidate_count": len(candidates),
        "analyzed": analyzed,
        "cached": cached,
        "failed": len(failed),
        "estimated_incremental_cost_usd": total_cost,
        "prompt_version": PROMPT_VERSION,
        "policy_version": POLICY_VERSION,
        "model": args.model,
        "lut_recommendations": dict(lut_counts),
        "rules": dict(rule_counts),
        "recommendation_status": dict(status_counts),
    }
    (root / "regrade-plan-v1-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print()
    print("VCLIP RE-GRADE SCENE ANALYSIS")
    print("==============================")
    print("candidates        :", len(candidates))
    print("analyzed          :", analyzed)
    print("cached            :", cached)
    print("failed            :", len(failed))
    print("incremental cost  :", f"${total_cost:.4f}")
    print()
    print("LUT RECOMMENDATIONS")
    print("-------------------")
    for key, count in lut_counts.most_common():
        print(f"{count:5d}  {key}")
    print()
    print("RECOMMENDATION STATUS")
    print("---------------------")
    for key, count in status_counts.most_common():
        print(f"{count:5d}  {key}")
    print()
    print("PLAN")
    print("----")
    for row in results:
        print()
        print(row["stock_clip_id"])
        print("project :", row["project_name"])
        print("caption :", row["scene_caption"])
        print(
            "scene   :",
            row["lighting"],
            "/",
            row["weather"],
            "/",
            row["water_type"],
        )
        print(
            "new LUT :",
            row["recommended_lut"],
            f"({row['recommendation_confidence']}, {row['recommendation_rule']})",
        )
        print("status  :", row["recommendation_status"])

    print()
    print("csv    :", results_path)
    print("summary:", root / "regrade-plan-v1-summary.json")
    print("VCLIP RE-GRADE SCENE ANALYSIS:", "PASS" if not failed else "FAILED")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
