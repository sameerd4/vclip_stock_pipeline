#!/usr/bin/env python3
"""Hyper-analyze one Ready Cut from the v2 Pleasure Point motion-audit traces.

Read-only. No DJI decode/API key is needed: it consumes the ~10 Hz trace CSV that
run_pleasure_point_ready_motion_audit.sh already generated.

The point is NOT to produce another pass/fail score. It exposes the underlying
motion so a human can compare what Final Cut shows against:
- world-camera yaw
- aircraft yaw
- gimbal pitch
- relative yaw
- true 3D camera pointing change
- angular velocities / acceleration-like regime changes
- horizontal/vertical speed changes
- motion reversals / corrections
- localized 0.5s / 1s / 2s / 3s windows

Outputs:
  <out>/summary.txt
  <out>/timeline-0.5s.csv
  <out>/top-windows.csv
  <out>/transition-points.csv
  <out>/derived-trace.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from bisect import bisect_left
from pathlib import Path
from typing import Any, Iterable


def f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


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
                fields.append(key)
                seen.add(key)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def moving_average(times: list[float], values: list[float], radius_s: float) -> list[float]:
    out: list[float] = []
    left = 0
    right = 0
    running = 0.0
    n = len(values)
    for i, t in enumerate(times):
        while left < n and times[left] < t - radius_s:
            if left < right:
                running -= values[left]
            left += 1
        if right < left:
            right = left
            running = 0.0
        while right < n and times[right] <= t + radius_s:
            running += values[right]
            right += 1
        count = max(1, right - left)
        out.append(running / count)
    return out


def derivative(times: list[float], values: list[float]) -> list[float]:
    n = len(values)
    if n < 2:
        return [0.0] * n
    out: list[float] = []
    for i in range(n):
        if i == 0:
            a, b = 0, 1
        elif i == n - 1:
            a, b = n - 2, n - 1
        else:
            a, b = i - 1, i + 1
        dt = times[b] - times[a]
        out.append((values[b] - values[a]) / dt if dt > 1e-9 else 0.0)
    return out


def look_vector(yaw_deg: float, pitch_deg: float) -> tuple[float, float, float]:
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    cp = math.cos(pitch)
    return cp * math.cos(yaw), cp * math.sin(yaw), math.sin(pitch)


def angular_distance_deg(
    yaw1: float, pitch1: float, yaw2: float, pitch2: float
) -> float:
    a = look_vector(yaw1, pitch1)
    b = look_vector(yaw2, pitch2)
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))
    return math.degrees(math.acos(dot))


def nearest_index(times: list[float], target: float, tolerance: float = 0.16) -> int | None:
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


def path_stats(values: list[float], epsilon: float) -> dict[str, float | int]:
    if not values:
        return {
            "start": 0.0, "end": 0.0, "net": 0.0, "span": 0.0,
            "path": 0.0, "efficiency": 0.0, "reversals": 0,
        }
    deltas = [b - a for a, b in zip(values, values[1:])]
    path = sum(abs(x) for x in deltas)
    net = values[-1] - values[0]
    signs: list[int] = []
    for delta in deltas:
        if abs(delta) < epsilon:
            continue
        signs.append(1 if delta > 0 else -1)
    reversals = sum(a != b for a, b in zip(signs, signs[1:]))
    return {
        "start": values[0],
        "end": values[-1],
        "net": net,
        "span": max(values) - min(values),
        "path": path,
        "efficiency": abs(net) / path if path > 1e-9 else 1.0,
        "reversals": reversals,
    }


def mean_between(times: list[float], values: list[float], start: float, end: float) -> float:
    vals = [v for t, v in zip(times, values) if start <= t <= end]
    return statistics.fmean(vals) if vals else 0.0


def max_abs_with_time(times: list[float], values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    i = max(range(len(values)), key=lambda k: abs(values[k]))
    return values[i], times[i]


def range_between(
    times: list[float],
    values: list[float],
    start: float,
    end: float,
) -> tuple[float, float]:
    vals = [v for t, v in zip(times, values) if start <= t <= end]
    if not vals:
        return 0.0, 0.0
    return min(vals), max(vals)


def main(args: argparse.Namespace) -> int:
    ranked = read_csv(args.ranked_csv)
    matches = [r for r in ranked if r.get("project_name") == args.project_name]
    if not matches:
        raise SystemExit(f"Project not found in ranked CSV: {args.project_name!r}")
    meta = matches[0]
    trace_path = Path(meta["trace_csv"]).expanduser()
    if not trace_path.is_file():
        raise SystemExit(f"Trace CSV not found: {trace_path}")

    raw = read_csv(trace_path)
    if len(raw) < 3:
        raise SystemExit("Trace contains too few samples.")

    times = [f(r["clip_t_s"]) for r in raw]
    source_times = [f(r["source_t_s"]) for r in raw]
    pitch = [f(r["pitch_deg"]) for r in raw]
    cam_yaw = [f(r["camera_yaw_deg_unwrapped"]) for r in raw]
    air_yaw = [f(r["aircraft_yaw_deg_unwrapped"]) for r in raw]
    rel_yaw = [f(r["relative_yaw_deg_unwrapped"]) for r in raw]
    h_speed = [f(r["horizontal_speed_mps"]) for r in raw]
    z_speed = [f(r["vertical_speed_mps"]) for r in raw]

    # Light smoothing: enough to suppress ~10 Hz numeric jitter without erasing
    # controller-scale changes that last a few tenths of a second.
    pitch_s = moving_average(times, pitch, 0.15)
    cam_yaw_s = moving_average(times, cam_yaw, 0.15)
    air_yaw_s = moving_average(times, air_yaw, 0.15)
    rel_yaw_s = moving_average(times, rel_yaw, 0.15)
    h_speed_s = moving_average(times, h_speed, 0.15)
    z_speed_s = moving_average(times, z_speed, 0.15)

    cam_yaw_rate = derivative(times, cam_yaw_s)
    air_yaw_rate = derivative(times, air_yaw_s)
    pitch_rate = derivative(times, pitch_s)
    rel_yaw_rate = derivative(times, rel_yaw_s)
    h_accel = derivative(times, h_speed_s)
    z_accel = derivative(times, z_speed_s)

    # True 3D camera pointing angular speed from yaw+pitch.
    camera_angular_speed = [0.0] * len(times)
    for i in range(1, len(times)):
        dt = times[i] - times[i - 1]
        if dt > 1e-9:
            camera_angular_speed[i] = (
                angular_distance_deg(
                    cam_yaw_s[i - 1], pitch_s[i - 1],
                    cam_yaw_s[i], pitch_s[i],
                ) / dt
            )
    if len(camera_angular_speed) > 1:
        camera_angular_speed[0] = camera_angular_speed[1]
    camera_angular_accel = derivative(times, camera_angular_speed)

    derived: list[dict[str, Any]] = []
    for i in range(len(times)):
        derived.append({
            "clip_t_s": round(times[i], 4),
            "source_t_s": round(source_times[i], 4),
            "pitch_deg_smoothed": round(pitch_s[i], 4),
            "camera_yaw_deg_smoothed": round(cam_yaw_s[i], 4),
            "aircraft_yaw_deg_smoothed": round(air_yaw_s[i], 4),
            "relative_yaw_deg_smoothed": round(rel_yaw_s[i], 4),
            "horizontal_speed_mps_smoothed": round(h_speed_s[i], 4),
            "vertical_speed_mps_smoothed": round(z_speed_s[i], 4),
            "camera_yaw_rate_deg_s": round(cam_yaw_rate[i], 4),
            "aircraft_yaw_rate_deg_s": round(air_yaw_rate[i], 4),
            "pitch_rate_deg_s": round(pitch_rate[i], 4),
            "relative_yaw_rate_deg_s": round(rel_yaw_rate[i], 4),
            "camera_3d_angular_speed_deg_s": round(camera_angular_speed[i], 4),
            "camera_3d_angular_accel_deg_s2": round(camera_angular_accel[i], 4),
            "horizontal_accel_mps2": round(h_accel[i], 4),
            "vertical_accel_mps2": round(z_accel[i], 4),
        })

    duration = times[-1] - times[0]
    median_dt = statistics.median(
        b - a for a, b in zip(times, times[1:]) if b > a
    )
    hz = 1.0 / median_dt if median_dt > 0 else 0.0

    pitch_stats = path_stats(pitch_s, 0.08)
    cam_stats = path_stats(cam_yaw_s, 0.10)
    air_stats = path_stats(air_yaw_s, 0.10)
    rel_stats = path_stats(rel_yaw_s, 0.10)

    camera_net_3d = angular_distance_deg(
        cam_yaw_s[0], pitch_s[0], cam_yaw_s[-1], pitch_s[-1]
    )
    camera_path_3d = sum(
        angular_distance_deg(a_y, a_p, b_y, b_p)
        for a_y, a_p, b_y, b_p in zip(
            cam_yaw_s, pitch_s, cam_yaw_s[1:], pitch_s[1:]
        )
    )
    camera_path_eff = (
        camera_net_3d / camera_path_3d if camera_path_3d > 1e-9 else 1.0
    )

    # Local windows: expose maxima without assuming "bad".
    window_rows: list[dict[str, Any]] = []
    for window_s in (0.5, 1.0, 2.0, 3.0):
        candidates: list[dict[str, Any]] = []
        for i, start in enumerate(times):
            j = nearest_index(times, start + window_s)
            if j is None or j <= i:
                continue
            actual = times[j] - times[i]
            if actual < window_s * 0.75:
                continue
            cam3d = angular_distance_deg(
                cam_yaw_s[i], pitch_s[i], cam_yaw_s[j], pitch_s[j]
            )
            row = {
                "window_s": window_s,
                "clip_start_s": times[i],
                "clip_end_s": times[j],
                "source_start_s": source_times[i],
                "source_end_s": source_times[j],
                "camera_3d_angle_deg": cam3d,
                "camera_yaw_delta_deg": cam_yaw_s[j] - cam_yaw_s[i],
                "aircraft_yaw_delta_deg": air_yaw_s[j] - air_yaw_s[i],
                "pitch_delta_deg": pitch_s[j] - pitch_s[i],
                "relative_yaw_delta_deg": rel_yaw_s[j] - rel_yaw_s[i],
                "horizontal_speed_delta_mps": h_speed_s[j] - h_speed_s[i],
                "vertical_speed_delta_mps": z_speed_s[j] - z_speed_s[i],
                "max_camera_angular_speed_deg_s": max(
                    camera_angular_speed[i:j + 1]
                ),
                "camera_angular_speed_range_deg_s": (
                    max(camera_angular_speed[i:j + 1])
                    - min(camera_angular_speed[i:j + 1])
                ),
                "camera_yaw_rate_range_deg_s": (
                    max(cam_yaw_rate[i:j + 1]) - min(cam_yaw_rate[i:j + 1])
                ),
                "pitch_rate_range_deg_s": (
                    max(pitch_rate[i:j + 1]) - min(pitch_rate[i:j + 1])
                ),
                "horizontal_accel_abs_max_mps2": max(
                    abs(x) for x in h_accel[i:j + 1]
                ),
            }
            candidates.append(row)

        # Keep top windows for several different physical questions.
        criteria = {
            "camera_reorientation": lambda r: r["camera_3d_angle_deg"],
            "camera_speed_regime_change": lambda r: r["camera_angular_speed_range_deg_s"],
            "yaw_rate_regime_change": lambda r: r["camera_yaw_rate_range_deg_s"],
            "pitch_rate_regime_change": lambda r: r["pitch_rate_range_deg_s"],
            "translation_speed_change": lambda r: abs(r["horizontal_speed_delta_mps"]),
        }
        used: set[tuple[str, int, int]] = set()
        for criterion, key in criteria.items():
            ranked_candidates = sorted(candidates, key=key, reverse=True)
            kept = 0
            for row in ranked_candidates:
                # Avoid returning five windows centered on effectively one instant.
                center = (row["clip_start_s"] + row["clip_end_s"]) / 2
                signature = (
                    criterion,
                    round(center * 2),  # ~0.5 s center buckets
                    round(window_s * 10),
                )
                if signature in used:
                    continue
                used.add(signature)
                window_rows.append({
                    "criterion": criterion,
                    **{
                        k: round(v, 4) if isinstance(v, float) else v
                        for k, v in row.items()
                    },
                })
                kept += 1
                if kept >= 3:
                    break

    # Change-point style diagnostic: compare motion in 0.75 s before vs after.
    transition_rows: list[dict[str, Any]] = []
    half = 0.75
    for i, t in enumerate(times):
        if t - half < times[0] or t + half > times[-1]:
            continue
        before = (t - half, t)
        after = (t, t + half)

        by0 = mean_between(times, cam_yaw_rate, *before)
        by1 = mean_between(times, cam_yaw_rate, *after)
        bp0 = mean_between(times, pitch_rate, *before)
        bp1 = mean_between(times, pitch_rate, *after)
        ba0 = mean_between(times, camera_angular_speed, *before)
        ba1 = mean_between(times, camera_angular_speed, *after)
        bh0 = mean_between(times, h_speed_s, *before)
        bh1 = mean_between(times, h_speed_s, *after)
        br0 = mean_between(times, rel_yaw_rate, *before)
        br1 = mean_between(times, rel_yaw_rate, *after)

        # Dimensionless exploratory change magnitude. This is deliberately a
        # localization score, not a pass/fail threshold.
        score = math.sqrt(
            ((by1 - by0) / 3.0) ** 2
            + ((bp1 - bp0) / 2.0) ** 2
            + ((ba1 - ba0) / 3.0) ** 2
            + ((bh1 - bh0) / 0.8) ** 2
            + ((br1 - br0) / 3.0) ** 2
        )
        transition_rows.append({
            "clip_t_s": round(t, 4),
            "source_t_s": round(source_times[i], 4),
            "change_score": round(score, 4),
            "camera_yaw_rate_before_deg_s": round(by0, 4),
            "camera_yaw_rate_after_deg_s": round(by1, 4),
            "pitch_rate_before_deg_s": round(bp0, 4),
            "pitch_rate_after_deg_s": round(bp1, 4),
            "camera_3d_speed_before_deg_s": round(ba0, 4),
            "camera_3d_speed_after_deg_s": round(ba1, 4),
            "horizontal_speed_before_mps": round(bh0, 4),
            "horizontal_speed_after_mps": round(bh1, 4),
            "relative_yaw_rate_before_deg_s": round(br0, 4),
            "relative_yaw_rate_after_deg_s": round(br1, 4),
        })

    # Non-max suppress transition points within 0.6 seconds.
    selected_transitions: list[dict[str, Any]] = []
    for row in sorted(transition_rows, key=lambda r: r["change_score"], reverse=True):
        if any(
            abs(row["clip_t_s"] - prior["clip_t_s"]) < 0.6
            for prior in selected_transitions
        ):
            continue
        selected_transitions.append(row)
        if len(selected_transitions) >= 10:
            break
    selected_transitions.sort(key=lambda r: r["clip_t_s"])

    # Half-second timeline.
    timeline_rows: list[dict[str, Any]] = []
    bin_s = 0.5
    start = 0.0
    while start < times[-1]:
        end = min(times[-1], start + bin_s)
        i = nearest_index(times, start, tolerance=0.3)
        j = nearest_index(times, end, tolerance=0.3)
        if i is not None and j is not None and j > i:
            cam3d = angular_distance_deg(
                cam_yaw_s[i], pitch_s[i], cam_yaw_s[j], pitch_s[j]
            )
            timeline_rows.append({
                "clip_start_s": round(times[i], 4),
                "clip_end_s": round(times[j], 4),
                "source_start_s": round(source_times[i], 4),
                "source_end_s": round(source_times[j], 4),
                "camera_3d_angle_deg": round(cam3d, 4),
                "camera_yaw_delta_deg": round(cam_yaw_s[j] - cam_yaw_s[i], 4),
                "aircraft_yaw_delta_deg": round(air_yaw_s[j] - air_yaw_s[i], 4),
                "pitch_delta_deg": round(pitch_s[j] - pitch_s[i], 4),
                "relative_yaw_delta_deg": round(rel_yaw_s[j] - rel_yaw_s[i], 4),
                "horizontal_speed_start_mps": round(h_speed_s[i], 4),
                "horizontal_speed_end_mps": round(h_speed_s[j], 4),
                "horizontal_speed_delta_mps": round(h_speed_s[j] - h_speed_s[i], 4),
                "vertical_speed_delta_mps": round(z_speed_s[j] - z_speed_s[i], 4),
                "mean_camera_3d_speed_deg_s": round(
                    statistics.fmean(camera_angular_speed[i:j + 1]), 4
                ),
                "max_camera_3d_speed_deg_s": round(
                    max(camera_angular_speed[i:j + 1]), 4
                ),
                "mean_camera_yaw_rate_deg_s": round(
                    statistics.fmean(cam_yaw_rate[i:j + 1]), 4
                ),
                "mean_pitch_rate_deg_s": round(
                    statistics.fmean(pitch_rate[i:j + 1]), 4
                ),
            })
        start += bin_s

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "derived-trace.csv", derived)
    write_csv(args.output_dir / "timeline-0.5s.csv", timeline_rows)
    write_csv(args.output_dir / "top-windows.csv", window_rows)
    write_csv(args.output_dir / "transition-points.csv", selected_transitions)

    cam_yaw_max, cam_yaw_max_t = max_abs_with_time(times, cam_yaw_rate)
    pitch_max, pitch_max_t = max_abs_with_time(times, pitch_rate)
    cam3d_max, cam3d_max_t = max_abs_with_time(times, camera_angular_speed)
    h_acc_max, h_acc_max_t = max_abs_with_time(times, h_accel)

    lines: list[str] = []
    lines.append("VCLIP HYPER ANALYSIS")
    lines.append("=" * 100)
    lines.append(f"Project:   {args.project_name}")
    lines.append(f"VClip:     {meta.get('stock_clip_id')}")
    lines.append(f"Source:    {meta.get('source_name')}")
    lines.append(
        f"Range:     {meta.get('source_start_s')} -> {meta.get('source_end_s')} "
        f"({meta.get('duration_s')}s)"
    )
    lines.append(
        f"Existing classifier: {meta.get('current_operator_status')} / "
        f"{meta.get('current_operator_reasons') or '(none)'}"
    )
    lines.append(
        f"Old exploratory audit: {meta.get('exploratory_risk_band')} "
        f"score={meta.get('exploratory_risk_score')}"
    )
    lines.append(f"Trace:     {trace_path}")
    lines.append(f"Samples:   {len(times)}  ~{hz:.2f} Hz  measured span={duration:.3f}s")
    lines.append("")

    lines.append("WHOLE-CLIP CAMERA / AIRCRAFT MOTION")
    lines.append("=" * 100)
    def add_stats(label: str, stats: dict[str, float | int]) -> None:
        lines.append(
            f"{label:<18} "
            f"start={stats['start']:8.2f}°  end={stats['end']:8.2f}°  "
            f"net={stats['net']:+7.2f}°  span={stats['span']:7.2f}°  "
            f"path={stats['path']:7.2f}°  efficiency={stats['efficiency']:.3f}  "
            f"reversals={stats['reversals']}"
        )
    add_stats("Camera yaw", cam_stats)
    add_stats("Aircraft yaw", air_stats)
    add_stats("Gimbal pitch", pitch_stats)
    add_stats("Relative yaw", rel_stats)
    lines.append(
        f"Camera look-vector  net 3D angle={camera_net_3d:.2f}°  "
        f"3D path length={camera_path_3d:.2f}°  "
        f"path efficiency={camera_path_eff:.3f}"
    )
    lines.append("")

    lines.append("WHOLE-CLIP TRANSLATION")
    lines.append("=" * 100)
    lines.append(
        f"Horizontal speed: start={h_speed_s[0]:.2f}  end={h_speed_s[-1]:.2f} m/s  "
        f"min={min(h_speed_s):.2f}  max={max(h_speed_s):.2f}  "
        f"net={h_speed_s[-1]-h_speed_s[0]:+.2f} m/s"
    )
    lines.append(
        f"Vertical speed:   start={z_speed_s[0]:.2f}  end={z_speed_s[-1]:.2f} m/s  "
        f"min={min(z_speed_s):.2f}  max={max(z_speed_s):.2f}  "
        f"net={z_speed_s[-1]-z_speed_s[0]:+.2f} m/s"
    )
    lines.append("")

    lines.append("RATE DISTRIBUTIONS")
    lines.append("=" * 100)
    for label, values, unit in (
        ("3D camera angular speed", camera_angular_speed, "deg/s"),
        ("Camera yaw rate", [abs(x) for x in cam_yaw_rate], "deg/s"),
        ("Aircraft yaw rate", [abs(x) for x in air_yaw_rate], "deg/s"),
        ("Pitch rate", [abs(x) for x in pitch_rate], "deg/s"),
        ("Horizontal acceleration", [abs(x) for x in h_accel], "m/s²"),
    ):
        lines.append(
            f"{label:<28} "
            f"median={percentile(values, .50):6.2f}  "
            f"p90={percentile(values, .90):6.2f}  "
            f"p95={percentile(values, .95):6.2f}  "
            f"max={max(values):6.2f} {unit}"
        )
    lines.append("")
    lines.append(
        f"Max signed camera-yaw rate: {cam_yaw_max:+.2f}°/s at clip +{cam_yaw_max_t:.2f}s"
    )
    lines.append(
        f"Max signed pitch rate:      {pitch_max:+.2f}°/s at clip +{pitch_max_t:.2f}s"
    )
    lines.append(
        f"Max 3D camera angular speed:{abs(cam3d_max):.2f}°/s at clip +{cam3d_max_t:.2f}s"
    )
    lines.append(
        f"Max signed horiz accel:     {h_acc_max:+.2f}m/s² at clip +{h_acc_max_t:.2f}s"
    )
    lines.append("")

    lines.append("TOP MOTION-REGIME CHANGE POINTS")
    lines.append("=" * 100)
    lines.append(
        "These are the strongest before/after changes in motion behavior. "
        "They are scrub points, not automatic rejection points."
    )
    for row in sorted(selected_transitions, key=lambda r: r["change_score"], reverse=True):
        lines.append(
            f"clip +{row['clip_t_s']:5.2f}s  score={row['change_score']:6.2f}  "
            f"camYawRate {row['camera_yaw_rate_before_deg_s']:+6.2f}"
            f"->{row['camera_yaw_rate_after_deg_s']:+6.2f}°/s  "
            f"pitchRate {row['pitch_rate_before_deg_s']:+6.2f}"
            f"->{row['pitch_rate_after_deg_s']:+6.2f}°/s  "
            f"3Dspeed {row['camera_3d_speed_before_deg_s']:5.2f}"
            f"->{row['camera_3d_speed_after_deg_s']:5.2f}°/s  "
            f"hSpeed {row['horizontal_speed_before_mps']:5.2f}"
            f"->{row['horizontal_speed_after_mps']:5.2f}m/s"
        )
    lines.append("")

    lines.append("0.5-SECOND TIMELINE")
    lines.append("=" * 100)
    lines.append(
        "Each row is descriptive. Look for abrupt changes from one row to the next, "
        "not merely large values."
    )
    for row in timeline_rows:
        lines.append(
            f"+{row['clip_start_s']:5.2f}->{row['clip_end_s']:5.2f}s  "
            f"3D={row['camera_3d_angle_deg']:5.2f}°  "
            f"camYaw={row['camera_yaw_delta_deg']:+6.2f}°  "
            f"airYaw={row['aircraft_yaw_delta_deg']:+6.2f}°  "
            f"pitch={row['pitch_delta_deg']:+6.2f}°  "
            f"hSpeedΔ={row['horizontal_speed_delta_mps']:+5.2f}m/s  "
            f"mean3Dspeed={row['mean_camera_3d_speed_deg_s']:5.2f}°/s"
        )
    lines.append("")

    lines.append("FILES")
    lines.append("=" * 100)
    for name in (
        "derived-trace.csv",
        "timeline-0.5s.csv",
        "top-windows.csv",
        "transition-points.csv",
    ):
        lines.append(str(args.output_dir / name))

    summary = "\n".join(lines) + "\n"
    (args.output_dir / "summary.txt").write_text(summary, encoding="utf-8")
    print(summary)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--ranked-csv", required=True, type=Path)
    p.add_argument("--project-name", required=True)
    p.add_argument("--output-dir", required=True, type=Path)
    return p


if __name__ == "__main__":
    raise SystemExit(main(parser().parse_args()))
