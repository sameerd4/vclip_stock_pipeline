#!/usr/bin/env python3
"""Localize likely controller/repositioning motion inside VClip Ready Cuts.

This is a diagnostic/calibration tool only. It does NOT mutate FCPXML, SQLite,
Ready Cut membership, or reconstruction thresholds.

Inputs:
- one telemetry-v2 reconstruction JSON report
- the current vclip_reconstruct_shard.py beside this script
- scripts/vclip_telemetry_qc.py
- DJI_API_KEY for v13/v14 flight-log decoding

Outputs:
- ready-cuts-ranked.csv      one row per Ready Cut, ranked by exploratory motion risk
- hotspots.csv               top localized movement windows per Ready Cut
- traces/*.csv               ~10 Hz telemetry trace for every Ready Cut
- summary.txt                human-readable list + hotspot times

"Hotspot" means "worth looking at visually", NOT "bad footage".
Smooth pans/orbits can score highly. The goal is to find where a human should scrub.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from bisect import bisect_left
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

try:
    import vclip_telemetry_qc as tqc
    import vclip_reconstruct_shard as recon
except Exception as exc:
    raise SystemExit(
        "Could not import VClip telemetry/reconstruction scripts. "
        "Run with PYTHONPATH=<repo>/src:<repo>/scripts. "
        f"Import error: {exc}"
    )


WINDOW_REFS = {
    0.5: {
        "camera_yaw": 4.0,
        "aircraft_yaw": 4.0,
        "pitch": 2.5,
        "h_speed": 0.70,
        "z_speed": 0.40,
        "turn_rate_change": 5.0,
        "pitch_rate_change": 4.0,
    },
    1.0: {
        "camera_yaw": 6.0,
        "aircraft_yaw": 6.0,
        "pitch": 4.0,
        "h_speed": 1.00,
        "z_speed": 0.70,
        "turn_rate_change": 4.0,
        "pitch_rate_change": 3.0,
    },
    2.0: {
        "camera_yaw": 12.0,
        "aircraft_yaw": 12.0,
        "pitch": 6.0,
        "h_speed": 1.50,
        "z_speed": 1.00,
        "turn_rate_change": 3.0,
        "pitch_rate_change": 2.5,
    },
}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_jsonish(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


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
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def resolve_media_path(source: dict[str, Any], ready: dict[str, Any]) -> Path | None:
    candidates: list[Path] = []

    raw = source.get("media_path")
    if raw:
        candidates.append(Path(str(raw)).expanduser())

    srt = ready.get("srt_path") or source.get("srt_path")
    if srt:
        p = Path(str(srt)).expanduser()
        for suffix in (".MP4", ".mp4", ".MOV", ".mov"):
            candidates.append(p.with_suffix(suffix))

    source_name = str(ready.get("source_name") or source.get("source_name") or "")
    if srt and source_name:
        parent = Path(str(srt)).expanduser().parent
        for suffix in (".MP4", ".mp4", ".MOV", ".mov"):
            candidates.append(parent / f"{source_name}{suffix}")

    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def exact_flight_header(
    flight_path: Path,
    cache_path: Path,
) -> tqc.FlightHeader:
    headers, _stats = tqc.read_flight_headers([flight_path], cache_path)
    if not headers:
        raise RuntimeError(f"No flight header for {flight_path}")
    header = headers[0]
    if header.error:
        raise RuntimeError(f"Flight header failed for {flight_path}: {header.error}")
    return header


def source_samples_for(
    *,
    source: dict[str, Any],
    ready: dict[str, Any],
    api_key: str | None,
    cache_dir: Path,
    ffprobe: str,
    flight_decode_cache: dict[str, tqc.FlightSamples],
    source_sample_cache: dict[tuple[str, str], list[recon.SourceSample]],
) -> tuple[list[recon.SourceSample], Path]:
    media = resolve_media_path(source, ready)
    if media is None:
        raise RuntimeError(
            f"Could not resolve media for {ready.get('source_name')}; "
            f"source media={source.get('media_path')!r}, srt={ready.get('srt_path')!r}"
        )

    flight_raw = ready.get("flight_log") or source.get("flight_log")
    if not flight_raw:
        raise RuntimeError(f"No flight log recorded for {ready.get('project_name')}")
    flight_path = Path(str(flight_raw)).expanduser()
    if not flight_path.is_file():
        raise RuntimeError(f"Flight log missing: {flight_path}")

    cache_key = (str(media), str(flight_path))
    if cache_key in source_sample_cache:
        return source_sample_cache[cache_key], media

    probe = tqc.ffprobe_media(media, ffprobe, {})
    if probe.error or probe.creation_dt is None or not probe.duration_s:
        raise RuntimeError(
            f"Could not probe {media}: {probe.error or 'missing creation/duration'}"
        )

    header = exact_flight_header(
        flight_path,
        cache_dir / "ready-motion-flight-header-cache.json",
    )
    flight = flight_decode_cache.get(str(flight_path))
    if flight is None:
        flight = tqc.decode_flight(header, api_key)
        flight_decode_cache[str(flight_path)] = flight

    samples = recon.source_relative_samples(
        flight,
        probe.creation_dt,
        float(probe.duration_s),
    )
    source_sample_cache[cache_key] = samples
    return samples, media


def clip_samples(
    samples: Sequence[recon.SourceSample],
    start_s: float,
    duration_s: float,
) -> list[recon.SourceSample]:
    end_s = start_s + duration_s
    return [
        sample
        for sample in samples
        if start_s - 0.05 <= sample.t <= end_s + 0.05
    ]


def nearest_index(times: list[float], target: float, tolerance: float = 0.18) -> int | None:
    j = bisect_left(times, target)
    choices: list[int] = []
    if j < len(times):
        choices.append(j)
    if j > 0:
        choices.append(j - 1)
    if not choices:
        return None
    best = min(choices, key=lambda idx: abs(times[idx] - target))
    return best if abs(times[best] - target) <= tolerance else None


def interval_metrics(
    rows: Sequence[recon.SourceSample],
    i: int,
    j: int,
    window_s: float,
) -> dict[str, float]:
    a = rows[i]
    b = rows[j]
    dt = max(1e-6, b.t - a.t)

    d_camera = b.camera_yaw - a.camera_yaw
    d_aircraft = b.aircraft_yaw - a.aircraft_yaw
    d_pitch = b.pitch - a.pitch
    d_rel = b.rel_yaw - a.rel_yaw
    d_h = b.h_speed - a.h_speed
    d_z = b.z_speed - a.z_speed

    # Compare the first-half and second-half angular rates. A large rate change
    # is useful evidence for "pilot starts/stops/repositions" rather than one
    # constant cinematic move.
    mid_target = a.t + dt / 2.0
    times = [sample.t for sample in rows]
    m = nearest_index(times, mid_target, tolerance=max(0.18, dt * 0.15))
    camera_rate_change = 0.0
    pitch_rate_change = 0.0
    if m is not None and i < m < j:
        first_dt = max(1e-6, rows[m].t - a.t)
        second_dt = max(1e-6, b.t - rows[m].t)
        camera_rate_1 = (rows[m].camera_yaw - a.camera_yaw) / first_dt
        camera_rate_2 = (b.camera_yaw - rows[m].camera_yaw) / second_dt
        pitch_rate_1 = (rows[m].pitch - a.pitch) / first_dt
        pitch_rate_2 = (b.pitch - rows[m].pitch) / second_dt
        camera_rate_change = abs(camera_rate_2 - camera_rate_1)
        pitch_rate_change = abs(pitch_rate_2 - pitch_rate_1)

    return {
        "actual_window_s": dt,
        "camera_yaw_delta_deg": d_camera,
        "aircraft_yaw_delta_deg": d_aircraft,
        "pitch_delta_deg": d_pitch,
        "relative_yaw_delta_deg": d_rel,
        "h_speed_delta_mps": d_h,
        "z_speed_delta_mps": d_z,
        "combined_camera_angle_deg": math.hypot(d_camera, d_pitch),
        "camera_yaw_rate_deg_s": d_camera / dt,
        "aircraft_yaw_rate_deg_s": d_aircraft / dt,
        "pitch_rate_deg_s": d_pitch / dt,
        "camera_turn_rate_change_deg_s": camera_rate_change,
        "pitch_rate_change_deg_s": pitch_rate_change,
    }


def hotspot_score(metrics: dict[str, float], window_s: float) -> tuple[float, list[str]]:
    refs = WINDOW_REFS[window_s]
    ratios = {
        "camera_yaw": abs(metrics["camera_yaw_delta_deg"]) / refs["camera_yaw"],
        "aircraft_yaw": abs(metrics["aircraft_yaw_delta_deg"]) / refs["aircraft_yaw"],
        "pitch": abs(metrics["pitch_delta_deg"]) / refs["pitch"],
        "horizontal_speed_change": abs(metrics["h_speed_delta_mps"]) / refs["h_speed"],
        "vertical_speed_change": abs(metrics["z_speed_delta_mps"]) / refs["z_speed"],
        "camera_turn_rate_change": (
            metrics["camera_turn_rate_change_deg_s"] / refs["turn_rate_change"]
        ),
        "pitch_rate_change": (
            metrics["pitch_rate_change_deg_s"] / refs["pitch_rate_change"]
        ),
    }

    combined_ref = math.hypot(refs["camera_yaw"], refs["pitch"])
    combined_ratio = metrics["combined_camera_angle_deg"] / combined_ref

    reasons = [name for name, ratio in ratios.items() if ratio >= 1.0]
    if combined_ratio >= 1.0:
        reasons.append("combined_pitch_yaw")

    score = max([combined_ratio, *ratios.values()], default=0.0)

    # Multi-axis and start/stop behavior are especially worth visual inspection.
    if (
        ratios["camera_yaw"] >= 0.60
        and ratios["pitch"] >= 0.60
    ):
        score += 0.25
    if (
        ratios["camera_turn_rate_change"] >= 1.0
        or ratios["pitch_rate_change"] >= 1.0
    ):
        score += 0.20
    if (
        ratios["horizontal_speed_change"] >= 0.8
        and (ratios["camera_yaw"] >= 0.6 or ratios["pitch"] >= 0.6)
    ):
        score += 0.15

    return score, sorted(set(reasons))


def candidate_hotspots(
    rows: Sequence[recon.SourceSample],
    clip_start_s: float,
    clip_duration_s: float,
    min_score: float,
) -> list[dict[str, Any]]:
    if len(rows) < 2:
        return []

    times = [sample.t for sample in rows]
    clip_end = clip_start_s + clip_duration_s
    candidates: list[dict[str, Any]] = []

    for window_s in sorted(WINDOW_REFS):
        for i, t0 in enumerate(times):
            if t0 < clip_start_s - 0.06:
                continue
            target = t0 + window_s
            if target > clip_end + 0.06:
                break
            j = nearest_index(times, target)
            if j is None or j <= i:
                continue

            metrics = interval_metrics(rows, i, j, window_s)
            score, reasons = hotspot_score(metrics, window_s)
            if score < min_score:
                continue

            t1 = rows[j].t
            candidates.append({
                "source_start_s": t0,
                "source_end_s": t1,
                "clip_start_s": max(0.0, t0 - clip_start_s),
                "clip_end_s": min(clip_duration_s, t1 - clip_start_s),
                "window_s": window_s,
                "hotspot_score": score,
                "reasons": reasons,
                **metrics,
            })

    candidates.sort(key=lambda row: (-row["hotspot_score"], row["clip_start_s"]))

    # Non-max suppression: keep distinct moments rather than five overlapping
    # windows describing the same movement.
    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        c0 = float(candidate["clip_start_s"])
        c1 = float(candidate["clip_end_s"])
        center = (c0 + c1) / 2.0
        duplicate = False
        for existing in selected:
            e0 = float(existing["clip_start_s"])
            e1 = float(existing["clip_end_s"])
            ecenter = (e0 + e1) / 2.0
            overlap = max(0.0, min(c1, e1) - max(c0, e0))
            shorter = max(1e-6, min(c1 - c0, e1 - e0))
            if abs(center - ecenter) < 0.65 or overlap / shorter >= 0.60:
                duplicate = True
                break
        if not duplicate:
            selected.append(candidate)
        if len(selected) >= 4:
            break
    return selected


def risk_band(score: float) -> str:
    if score >= 1.60:
        return "HIGH"
    if score >= 1.15:
        return "MEDIUM"
    if score >= 0.85:
        return "WATCH"
    return "LOW"


def trace_rows(
    project_name: str,
    stock_clip_id: str,
    source_name: str,
    rows: Sequence[recon.SourceSample],
    clip_start_s: float,
    clip_duration_s: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    end = clip_start_s + clip_duration_s
    for sample in rows:
        if sample.t < clip_start_s - 0.05 or sample.t > end + 0.05:
            continue
        out.append({
            "project_name": project_name,
            "stock_clip_id": stock_clip_id,
            "source_name": source_name,
            "clip_t_s": round(sample.t - clip_start_s, 4),
            "source_t_s": round(sample.t, 4),
            "pitch_deg": round(sample.pitch, 4),
            "camera_yaw_deg_unwrapped": round(sample.camera_yaw, 4),
            "aircraft_yaw_deg_unwrapped": round(sample.aircraft_yaw, 4),
            "relative_yaw_deg_unwrapped": round(sample.rel_yaw, 4),
            "horizontal_speed_mps": round(sample.h_speed, 4),
            "vertical_speed_mps": round(sample.z_speed, 4),
            "pitch_limit": sample.pitch_limit,
            "yaw_limit": sample.yaw_limit,
            "roll_limit": sample.roll_limit,
            "gimbal_stuck": sample.stuck,
        })
    return out


def run(args: argparse.Namespace) -> int:
    report = json.loads(args.report.read_text(encoding="utf-8"))
    ready = [
        row
        for row in report.get("ready_variants", [])
        if row.get("bucket") == "ready"
    ]
    sources = {
        str(row.get("source_name")): row
        for row in report.get("sources", [])
        if row.get("source_name")
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    traces_dir = args.output_dir / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    api_key = os.environ.get(args.api_key_env)
    flight_decode_cache: dict[str, tqc.FlightSamples] = {}
    source_sample_cache: dict[tuple[str, str], list[recon.SourceSample]] = {}

    ranked_rows: list[dict[str, Any]] = []
    all_hotspots: list[dict[str, Any]] = []
    errors: list[str] = []

    for ready_row in ready:
        project = str(ready_row.get("project_name") or "")
        stock_id = str(ready_row.get("stock_clip_id") or "")
        source_name = str(ready_row.get("source_name") or "")
        start_s = safe_float(ready_row.get("start_s"))
        duration_s = safe_float(ready_row.get("duration_s"))
        end_s = start_s + duration_s
        source = sources.get(source_name, {})

        metrics = parse_jsonish(ready_row.get("operator_metrics"))
        try:
            samples, media = source_samples_for(
                source=source,
                ready=ready_row,
                api_key=api_key,
                cache_dir=args.cache_dir,
                ffprobe=args.ffprobe,
                flight_decode_cache=flight_decode_cache,
                source_sample_cache=source_sample_cache,
            )
            clip_rows = clip_samples(samples, start_s, duration_s)
            hotspots = candidate_hotspots(
                clip_rows,
                start_s,
                duration_s,
                args.min_hotspot_score,
            )
            top_score = max((float(h["hotspot_score"]) for h in hotspots), default=0.0)

            trace = trace_rows(
                project,
                stock_id,
                source_name,
                clip_rows,
                start_s,
                duration_s,
            )
            trace_name = f"{stock_id}__{source_name}.csv"
            write_csv(traces_dir / trace_name, trace)

            for rank, hotspot in enumerate(hotspots, 1):
                all_hotspots.append({
                    "project_name": project,
                    "stock_clip_id": stock_id,
                    "source_name": source_name,
                    "clip_duration_s": round(duration_s, 4),
                    "rank_within_clip": rank,
                    "risk_band": risk_band(float(hotspot["hotspot_score"])),
                    **{
                        key: (
                            round(value, 4)
                            if isinstance(value, float)
                            else ",".join(value)
                            if isinstance(value, list)
                            else value
                        )
                        for key, value in hotspot.items()
                    },
                })

            top = hotspots[0] if hotspots else None
            ranked_rows.append({
                "project_name": project,
                "stock_clip_id": stock_id,
                "source_name": source_name,
                "source_start_s": round(start_s, 4),
                "source_end_s": round(end_s, 4),
                "duration_s": round(duration_s, 4),
                "action": ready_row.get("action"),
                "current_operator_status": ready_row.get("operator_status"),
                "current_operator_reasons": ready_row.get("operator_reasons"),
                "current_camera_yaw_span_deg": round(
                    safe_float(metrics.get("camera_yaw_span_deg")), 4
                ),
                "current_aircraft_yaw_span_deg": round(
                    safe_float(metrics.get("aircraft_yaw_span_deg")), 4
                ),
                "current_pitch_span_deg": round(
                    safe_float(metrics.get("pitch_span_deg")), 4
                ),
                "exploratory_risk_score": round(top_score, 4),
                "exploratory_risk_band": risk_band(top_score),
                "hotspot_count": len(hotspots),
                "top_hotspot_clip_start_s": (
                    round(float(top["clip_start_s"]), 4) if top else ""
                ),
                "top_hotspot_clip_end_s": (
                    round(float(top["clip_end_s"]), 4) if top else ""
                ),
                "top_hotspot_reasons": (
                    ",".join(top["reasons"]) if top else ""
                ),
                "media_path": str(media),
                "trace_csv": str(traces_dir / trace_name),
            })
        except Exception as exc:
            errors.append(f"{project}: {type(exc).__name__}: {exc}")
            ranked_rows.append({
                "project_name": project,
                "stock_clip_id": stock_id,
                "source_name": source_name,
                "source_start_s": round(start_s, 4),
                "source_end_s": round(end_s, 4),
                "duration_s": round(duration_s, 4),
                "action": ready_row.get("action"),
                "current_operator_status": ready_row.get("operator_status"),
                "current_operator_reasons": ready_row.get("operator_reasons"),
                "exploratory_risk_score": "",
                "exploratory_risk_band": "ERROR",
                "hotspot_count": 0,
                "error": f"{type(exc).__name__}: {exc}",
            })

    ranked_rows.sort(
        key=lambda row: (
            -safe_float(row.get("exploratory_risk_score"), -1.0),
            str(row.get("project_name")),
        )
    )
    all_hotspots.sort(
        key=lambda row: (
            -safe_float(row.get("hotspot_score")),
            str(row.get("project_name")),
            int(row.get("rank_within_clip") or 0),
        )
    )

    write_csv(args.output_dir / "ready-cuts-ranked.csv", ranked_rows)
    write_csv(args.output_dir / "hotspots.csv", all_hotspots)

    summary: list[str] = []
    summary.append("PLEASURE POINT READY CUT MOTION AUDIT")
    summary.append("=" * 92)
    summary.append(
        "Exploratory only: a hotspot means 'scrub here', not 'reject this footage'."
    )
    summary.append(
        "Smooth intentional pans/orbits can legitimately appear as movement hotspots."
    )
    summary.append("")
    summary.append(f"Ready Cuts: {len(ready)}")
    summary.append(f"Decoded flight logs: {len(flight_decode_cache)}")
    summary.append(f"Errors: {len(errors)}")
    summary.append("")
    summary.append("READY CUTS — RANKED BY LOCALIZED MOVEMENT RISK")
    summary.append("=" * 92)

    for index, row in enumerate(ranked_rows, 1):
        summary.append(
            f"{index:02d}. [{row.get('exploratory_risk_band','?'):>6}] "
            f"score={row.get('exploratory_risk_score','?')}  "
            f"{row.get('project_name')}"
        )
        summary.append(
            f"    source={row.get('source_name')}  "
            f"range={row.get('source_start_s')}->{row.get('source_end_s')}  "
            f"dur={row.get('duration_s')}s"
        )
        summary.append(
            f"    current operator={row.get('current_operator_status')}  "
            f"reasons={row.get('current_operator_reasons') or '(none)'}"
        )
        if row.get("top_hotspot_clip_start_s") != "":
            summary.append(
                f"    TOP SCRUB: clip +{float(row['top_hotspot_clip_start_s']):.2f}s"
                f" -> +{float(row['top_hotspot_clip_end_s']):.2f}s"
                f"  reasons={row.get('top_hotspot_reasons') or '(none)'}"
            )
        else:
            summary.append("    TOP SCRUB: none above exploratory threshold")
        summary.append("")

    summary.append("LOCALIZED HOTSPOTS")
    summary.append("=" * 92)
    by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in all_hotspots:
        by_project[str(row["project_name"])].append(row)

    for row in ranked_rows:
        project = str(row["project_name"])
        spots = by_project.get(project, [])
        if not spots:
            continue
        summary.append(project)
        for spot in spots:
            summary.append(
                f"  #{spot['rank_within_clip']} "
                f"[{spot['risk_band']}] score={float(spot['hotspot_score']):.2f} "
                f"clip +{float(spot['clip_start_s']):.2f}"
                f" -> +{float(spot['clip_end_s']):.2f}s "
                f"(source {float(spot['source_start_s']):.2f}"
                f"->{float(spot['source_end_s']):.2f}s)"
            )
            summary.append(
                "     "
                f"camYaw={float(spot['camera_yaw_delta_deg']):+.1f}°  "
                f"airYaw={float(spot['aircraft_yaw_delta_deg']):+.1f}°  "
                f"pitch={float(spot['pitch_delta_deg']):+.1f}°  "
                f"ΔhSpeed={float(spot['h_speed_delta_mps']):+.2f}m/s  "
                f"turnRateΔ={float(spot['camera_turn_rate_change_deg_s']):.1f}°/s"
            )
            summary.append(f"     reasons={spot.get('reasons') or '(none)'}")
        summary.append("")

    if errors:
        summary.append("ERRORS")
        summary.append("=" * 92)
        summary.extend(f"- {error}" for error in errors)

    summary_text = "\n".join(summary) + "\n"
    (args.output_dir / "summary.txt").write_text(summary_text, encoding="utf-8")
    print(summary_text)
    print(f"Ranked CSV: {args.output_dir / 'ready-cuts-ranked.csv'}")
    print(f"Hotspots CSV: {args.output_dir / 'hotspots.csv'}")
    print(f"Traces: {traces_dir}")
    print(f"Summary: {args.output_dir / 'summary.txt'}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="List v2 Ready Cuts and localize likely controller-motion hotspots."
    )
    p.add_argument("--report", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--cache-dir", required=True, type=Path)
    p.add_argument("--ffprobe", default="ffprobe")
    p.add_argument("--api-key-env", default="DJI_API_KEY")
    p.add_argument(
        "--min-hotspot-score",
        type=float,
        default=0.85,
        help="Exploratory hotspot threshold; lower means more candidate windows.",
    )
    return p


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
