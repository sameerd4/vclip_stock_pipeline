#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Iterable


VCLIP_RE = re.compile(r"VCLIP_[0-9A-F]{16,64}")


@dataclass
class Anchor:
    vclip_id: str
    project_name: str
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class Sample:
    t: float
    pitch: float | None
    gyaw: float | None
    drone_yaw: float | None
    rel_yaw: float | None
    pitch_limit: bool
    yaw_limit: bool
    stuck: bool


def parse_fraction_seconds(value: str | None) -> float | None:
    if not value:
        return None
    value = value.rstrip("s")
    try:
        return float(Fraction(value))
    except Exception:
        return None


def iso_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def num(row: dict[str, str], key: str) -> float | None:
    try:
        v = row.get(key)
        if v in (None, ""):
            return None
        return float(v)
    except Exception:
        return None


def boolean(row: dict[str, str], key: str) -> bool:
    return str(row.get(key, "")).strip().lower() in {"1", "true", "yes", "on"}


def wrap180(x: float) -> float:
    return ((x + 180.0) % 360.0) - 180.0


def unwrap(values: list[float | None]) -> list[float | None]:
    out: list[float | None] = []
    prev: float | None = None
    for x in values:
        if x is None:
            out.append(None)
            continue
        y = x if prev is None else prev + wrap180(x - prev)
        out.append(y)
        prev = y
    return out


def ffprobe_media(path: Path) -> tuple[datetime, float]:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:format_tags=creation_time",
        "-of", "json", str(path),
    ]
    data = json.loads(subprocess.check_output(cmd, text=True))
    fmt = data["format"]
    duration = float(fmt["duration"])
    creation = fmt.get("tags", {}).get("creation_time")
    if not creation:
        raise RuntimeError(f"No creation_time in {path}")
    return iso_dt(creation), duration


def parse_anchors(fcpxml: Path, source_name: str) -> list[Anchor]:
    root = ET.parse(fcpxml).getroot()
    anchors: list[Anchor] = []

    for event in root.iter("event"):
        for project in event.iter("project"):
            blob = ET.tostring(project, encoding="unicode")
            ids = sorted(set(VCLIP_RE.findall(blob)))
            if not ids:
                continue

            for elem in project.iter():
                if elem.tag != "asset-clip":
                    continue
                name = (elem.get("name") or "").rsplit(".", 1)[0]
                if name.casefold() != source_name.rsplit(".", 1)[0].casefold():
                    continue

                start = parse_fraction_seconds(elem.get("start"))
                duration = parse_fraction_seconds(elem.get("duration"))
                if start is None or duration is None:
                    continue

                for vid in ids:
                    anchors.append(
                        Anchor(
                            vclip_id=vid,
                            project_name=project.get("name") or "",
                            start=start,
                            duration=duration,
                        )
                    )
                break

    # One physical project per VClip should be normal. Keep deterministic first.
    dedup: dict[str, Anchor] = {}
    for a in sorted(anchors, key=lambda x: (x.vclip_id, x.start, x.duration, x.project_name)):
        dedup.setdefault(a.vclip_id, a)
    return sorted(dedup.values(), key=lambda x: (x.start, x.end, x.vclip_id))


def load_samples(csv_path: Path, source_start: datetime, source_duration: float) -> list[Sample]:
    rows: list[dict[str, str]] = []
    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))

    raw: list[tuple[float, dict[str, str]]] = []
    for row in rows:
        try:
            dt = iso_dt(row["CUSTOM.dateTime"])
        except Exception:
            continue
        t = (dt - source_start).total_seconds()
        if -0.25 <= t <= source_duration + 0.25:
            raw.append((t, row))

    raw.sort(key=lambda x: x[0])
    gyaws = [num(row, "GIMBAL.yaw") for _, row in raw]
    dyaws = [num(row, "OSD.yaw") for _, row in raw]
    rel_raw = [
        wrap180(g - d) if g is not None and d is not None else None
        for g, d in zip(gyaws, dyaws)
    ]
    rel_unwrapped = unwrap(rel_raw)

    out: list[Sample] = []
    for (t, row), rel in zip(raw, rel_unwrapped):
        out.append(
            Sample(
                t=t,
                pitch=num(row, "GIMBAL.pitch"),
                gyaw=num(row, "GIMBAL.yaw"),
                drone_yaw=num(row, "OSD.yaw"),
                rel_yaw=rel,
                pitch_limit=boolean(row, "GIMBAL.isPitchAtLimit"),
                yaw_limit=boolean(row, "GIMBAL.isYawAtLimit"),
                stuck=boolean(row, "GIMBAL.isStuck"),
            )
        )
    return out


def rolling_delta(
    times: list[float],
    values: list[float | None],
    window: float,
    tolerance: float = 0.22,
) -> list[tuple[float, float, float]]:
    """Return (start_t, end_t, signed_delta) for usable windows."""
    out: list[tuple[float, float, float]] = []
    for i, t0 in enumerate(times):
        v0 = values[i]
        if v0 is None:
            continue
        target = t0 + window
        j = bisect.bisect_left(times, target)
        if j >= len(times) or abs(times[j] - target) > tolerance:
            continue
        v1 = values[j]
        if v1 is None:
            continue
        out.append((t0, times[j], v1 - v0))
    return out


def build_bad_mask(
    samples: list[Sample],
    source_duration: float,
    *,
    pitch_2s: float,
    pitch_3s: float,
    rel_yaw_1s: float,
    pad: float,
) -> tuple[list[bool], list[str]]:
    times = [s.t for s in samples]
    pitch = [s.pitch for s in samples]
    rel = [s.rel_yaw for s in samples]
    bad = [False] * len(samples)
    reasons = [""] * len(samples)

    def mark(t0: float, t1: float, why: str) -> None:
        lo = bisect.bisect_left(times, max(0.0, t0 - pad))
        hi = bisect.bisect_right(times, min(source_duration, t1 + pad))
        for i in range(lo, hi):
            bad[i] = True
            reasons[i] = (reasons[i] + "," + why).strip(",")

    for s, is_bad in zip(samples, bad):
        if s.stuck:
            mark(s.t, s.t, "gimbal_stuck")
        elif s.pitch_limit:
            mark(s.t, s.t, "pitch_limit")
        elif s.yaw_limit:
            mark(s.t, s.t, "yaw_limit")

    for t0, t1, delta in rolling_delta(times, pitch, 2.0):
        if abs(delta) >= pitch_2s:
            mark(t0, t1, "pitch_2s")

    for t0, t1, delta in rolling_delta(times, pitch, 3.0):
        if abs(delta) >= pitch_3s:
            mark(t0, t1, "pitch_3s")

    for t0, t1, delta in rolling_delta(times, rel, 1.0):
        if abs(delta) >= rel_yaw_1s:
            mark(t0, t1, "relative_yaw_1s")

    return bad, reasons


def stable_intervals(
    samples: list[Sample],
    bad: list[bool],
    source_duration: float,
    *,
    min_stable: float,
    max_gap: float = 0.35,
) -> list[tuple[float, float]]:
    if not samples:
        return []

    intervals: list[tuple[float, float]] = []
    start: float | None = None
    prev_t: float | None = None

    for s, is_bad in zip(samples, bad):
        if prev_t is not None and s.t - prev_t > max_gap:
            if start is not None and prev_t - start >= min_stable:
                intervals.append((max(0.0, start), min(source_duration, prev_t)))
            start = None

        if is_bad:
            if start is not None and prev_t is not None and prev_t - start >= min_stable:
                intervals.append((max(0.0, start), min(source_duration, prev_t)))
            start = None
        else:
            if start is None:
                start = s.t

        prev_t = s.t

    if start is not None and prev_t is not None and prev_t - start >= min_stable:
        intervals.append((max(0.0, start), min(source_duration, prev_t)))

    # Merge tiny telemetry sampling gaps.
    merged: list[tuple[float, float]] = []
    for a, b in intervals:
        if merged and a - merged[-1][1] <= 0.25:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def choose_interval(anchor: Anchor, intervals: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not intervals:
        return None
    ranked = sorted(
        intervals,
        key=lambda iv: (
            overlap(anchor.start, anchor.end, iv[0], iv[1]),
            -abs(((iv[0] + iv[1]) / 2) - ((anchor.start + anchor.end) / 2)),
            iv[1] - iv[0],
        ),
        reverse=True,
    )
    if overlap(anchor.start, anchor.end, ranked[0][0], ranked[0][1]) <= 0:
        return None
    return ranked[0]


def propose(anchor: Anchor, interval: tuple[float, float], target: float, max_len: float) -> tuple[float, float]:
    a, b = interval
    available = b - a
    want = min(max_len, max(target, anchor.duration), available)

    # Prefer to preserve the current anchor center, while expanding equally.
    center = (anchor.start + anchor.end) / 2.0
    start = center - want / 2.0
    start = max(a, min(start, b - want))
    end = start + want
    return start, end


def interval_metrics(samples: list[Sample], a: float, b: float) -> dict[str, float | None]:
    pts = [s for s in samples if a <= s.t <= b]
    if len(pts) < 2:
        return {"pitch_span": None, "net_pitch": None, "rel_yaw_span": None}
    p = [s.pitch for s in pts if s.pitch is not None]
    ry = [s.rel_yaw for s in pts if s.rel_yaw is not None]
    return {
        "pitch_span": (max(p) - min(p)) if p else None,
        "net_pitch": (p[-1] - p[0]) if len(p) >= 2 else None,
        "rel_yaw_span": (max(ry) - min(ry)) if ry else None,
    }


def dedupe_proposals(rows: list[dict], iou_threshold: float = 0.80) -> list[dict]:
    kept: list[dict] = []
    for row in sorted(rows, key=lambda x: (-(x["new_end"] - x["new_start"]), x["new_start"])):
        duplicate = False
        for k in kept:
            inter = overlap(row["new_start"], row["new_end"], k["new_start"], k["new_end"])
            union = max(row["new_end"], k["new_end"]) - min(row["new_start"], k["new_start"])
            iou = inter / union if union > 0 else 0
            if iou >= iou_threshold:
                duplicate = True
                row["dedupe_into"] = k["vclip_id"]
                break
        if not duplicate:
            kept.append(row)
    return sorted(kept, key=lambda x: (x["new_start"], x["new_end"]))


def main() -> int:
    ap = argparse.ArgumentParser(description="Telemetry-only recut probe around existing VClip anchors.")
    ap.add_argument("--fcpxml", required=True, type=Path)
    ap.add_argument("--source-name", required=True)
    ap.add_argument("--media", required=True, type=Path)
    ap.add_argument("--telemetry-csv", required=True, type=Path)
    ap.add_argument("--target-duration", type=float, default=12.0)
    ap.add_argument("--max-duration", type=float, default=15.0)
    ap.add_argument("--min-stable", type=float, default=4.0)
    ap.add_argument("--pitch-2s", type=float, default=8.0)
    ap.add_argument("--pitch-3s", type=float, default=10.0)
    ap.add_argument("--relative-yaw-1s", type=float, default=15.0)
    ap.add_argument("--boundary-pad", type=float, default=0.30)
    ap.add_argument("--json-out", type=Path)
    args = ap.parse_args()

    creation, duration = ffprobe_media(args.media)
    anchors = parse_anchors(args.fcpxml, args.source_name)
    samples = load_samples(args.telemetry_csv, creation, duration)

    if not anchors:
        raise SystemExit("No matching physical VClip anchors found.")
    if not samples:
        raise SystemExit("No telemetry samples overlap the source MP4.")

    bad, reasons = build_bad_mask(
        samples,
        duration,
        pitch_2s=args.pitch_2s,
        pitch_3s=args.pitch_3s,
        rel_yaw_1s=args.relative_yaw_1s,
        pad=args.boundary_pad,
    )
    intervals = stable_intervals(
        samples,
        bad,
        duration,
        min_stable=args.min_stable,
    )

    print("=" * 88)
    print("SOURCE")
    print("=" * 88)
    print("media:          ", args.media)
    print("creation UTC:   ", creation.isoformat())
    print("duration:       ", f"{duration:.3f}s")
    print("anchors:        ", len(anchors))
    print("telemetry rows: ", len(samples))
    print()

    print("=" * 88)
    print("TELEMETRY-STABLE INTERVALS")
    print("=" * 88)
    for i, (a, b) in enumerate(intervals, 1):
        m = interval_metrics(samples, a, b)
        print(
            f"{i:2d}. {a:7.3f} -> {b:7.3f}  dur={b-a:6.2f}s  "
            f"pitch_span={m['pitch_span'] if m['pitch_span'] is not None else float('nan'):6.2f}  "
            f"net_pitch={m['net_pitch'] if m['net_pitch'] is not None else float('nan'):+6.2f}"
        )

    proposals: list[dict] = []
    print()
    print("=" * 88)
    print("ANCHOR RE-EDIT PROPOSALS")
    print("=" * 88)

    for anchor in anchors:
        iv = choose_interval(anchor, intervals)
        cur_m = interval_metrics(samples, anchor.start, anchor.end)
        if iv is None:
            print()
            print(anchor.vclip_id, anchor.project_name)
            print(f"  existing: {anchor.start:7.3f} -> {anchor.end:7.3f} ({anchor.duration:5.2f}s)")
            print("  proposal: NO STABLE INTERVAL OVERLAPS THIS ANCHOR")
            continue

        new_start, new_end = propose(anchor, iv, args.target_duration, args.max_duration)
        new_m = interval_metrics(samples, new_start, new_end)
        delta_len = (new_end - new_start) - anchor.duration

        row = {
            "vclip_id": anchor.vclip_id,
            "project_name": anchor.project_name,
            "old_start": anchor.start,
            "old_end": anchor.end,
            "old_duration": anchor.duration,
            "new_start": new_start,
            "new_end": new_end,
            "new_duration": new_end - new_start,
            "stable_interval_start": iv[0],
            "stable_interval_end": iv[1],
            "length_change": delta_len,
            "current_metrics": cur_m,
            "proposal_metrics": new_m,
            "dedupe_into": None,
        }
        proposals.append(row)

        verb = "EXTEND" if delta_len > 0.25 else "TRIM" if delta_len < -0.25 else "KEEP"
        print()
        print(anchor.vclip_id, anchor.project_name)
        print(f"  existing: {anchor.start:7.3f} -> {anchor.end:7.3f} ({anchor.duration:5.2f}s)")
        print(f"  stable:   {iv[0]:7.3f} -> {iv[1]:7.3f} ({iv[1]-iv[0]:5.2f}s)")
        print(f"  proposal: {new_start:7.3f} -> {new_end:7.3f} ({new_end-new_start:5.2f}s)  {verb} {delta_len:+.2f}s")
        print(
            "  metrics:  "
            f"pitch_span {cur_m['pitch_span']!s:>6} -> {new_m['pitch_span']!s:>6}, "
            f"net_pitch {cur_m['net_pitch']!s:>6} -> {new_m['net_pitch']!s:>6}"
        )

    canonical = dedupe_proposals(proposals)

    print()
    print("=" * 88)
    print("DEDUPED PROPOSED WINDOWS")
    print("=" * 88)
    print(f"existing anchors: {len(anchors)}")
    print(f"deduped windows:  {len(canonical)}")
    for row in canonical:
        print(
            f"{row['new_start']:7.3f} -> {row['new_end']:7.3f} "
            f"({row['new_duration']:5.2f}s)  from {row['vclip_id']}  {row['project_name']}"
        )

    if args.json_out:
        payload = {
            "source": {
                "media": str(args.media),
                "source_name": args.source_name,
                "creation_time_utc": creation.isoformat(),
                "duration_seconds": duration,
            },
            "thresholds": {
                "pitch_2s": args.pitch_2s,
                "pitch_3s": args.pitch_3s,
                "relative_yaw_1s": args.relative_yaw_1s,
                "boundary_pad": args.boundary_pad,
                "target_duration": args.target_duration,
                "max_duration": args.max_duration,
            },
            "stable_intervals": [
                {"start": a, "end": b, "duration": b-a}
                for a, b in intervals
            ],
            "proposals": proposals,
            "deduped_proposals": canonical,
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2))
        print()
        print("JSON:", args.json_out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
