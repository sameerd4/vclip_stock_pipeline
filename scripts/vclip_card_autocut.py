#!/usr/bin/env python3
"""Create a telemetry-aware Final Cut best-of project directly from DJI card media.

Scans raw DJI video + sibling SRT files, proposes source ranges, ranks them
using DJI motion evidence and optional Apple Vision coherence, selects a
diverse non-overlapping set, and writes one importable FCPXML compilation.
The source media is referenced directly; nothing is transcoded or canonicalized.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from statistics import median
from typing import Any, Sequence

from vclip_pipeline.packaging.media import MediaProbe, probe_media
from vclip_pipeline.stockify.core import format_time, stable_uid
from vclip_pipeline.stockify.fcpxml import add_vclip_metadata, validate_fcpxml, write_fcpxml
from vclip_pipeline.stockify.sidecars import extract_srt_color_md, parse_srt_info, same_stem_srt_paths

try:
    from vclip_visual_coherence import VisualSettings, assess_visual_coherence
except Exception:
    VisualSettings = None
    assess_visual_coherence = None

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
AUTOCUT_VERSION = "card-autocut-v1"


@dataclass(frozen=True)
class SourceMedia:
    path: Path
    probe: MediaProbe
    srt_path: Path | None
    color_md: str | None
    capture_time: str | None

    @property
    def duration(self) -> float:
        return float(self.probe.duration_seconds or 0.0)

    @property
    def width(self) -> int:
        return int(self.probe.width or 0)

    @property
    def height(self) -> int:
        return int(self.probe.height or 0)

    @property
    def fps(self) -> float:
        return float(self.probe.frame_rate or 30.0)

    @property
    def orientation(self) -> str:
        return "vertical" if self.height > self.width else "landscape"


@dataclass
class MotionMetrics:
    srt_samples: int = 0
    gps_coverage: float = 0.0
    median_horizontal_speed_mps: float | None = None
    max_horizontal_speed_mps: float | None = None
    speed_variation_mps: float | None = None
    max_abs_vertical_speed_mps: float | None = None
    altitude_min_m: float | None = None
    altitude_span_m: float | None = None


@dataclass
class Candidate:
    source: SourceMedia
    start_s: float
    duration_s: float
    score: float
    status: str
    reasons: list[str]
    motion: MotionMetrics
    visual_status: str = "NOT_RUN"
    visual_reasons: list[str] | None = None
    visual_metrics: dict[str, Any] | None = None

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s

    @property
    def candidate_id(self) -> str:
        text = f"{self.source.path}|{self.start_s:.4f}|{self.duration_s:.4f}|{AUTOCUT_VERSION}"
        return "AUTO_" + hashlib.sha256(text.encode()).hexdigest()[:14].upper()


def _run_json(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
    return json.loads(result.stdout)


def _capture_time(path: Path) -> str | None:
    try:
        payload = _run_json([
            "ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
            "-of", "json", str(path),
        ])
        value = ((payload.get("format") or {}).get("tags") or {}).get("creation_time")
        return str(value) if value else None
    except Exception:
        return None


def _sibling_srt(path: Path) -> Path | None:
    for candidate in same_stem_srt_paths(path):
        if candidate.is_file():
            return candidate.resolve()
    return None


def scan_sources(root: Path) -> list[SourceMedia]:
    paths = sorted(
        (p.resolve() for p in root.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and not p.name.startswith("._")),
        key=lambda p: str(p).lower(),
    )
    rows: list[SourceMedia] = []
    for path in paths:
        probe = probe_media(path)
        if not probe.duration_seconds or not probe.width or not probe.height:
            continue
        srt = _sibling_srt(path)
        color_md = extract_srt_color_md(srt) if srt else None
        capture = _capture_time(path)
        if capture is None and srt:
            try:
                info = parse_srt_info(srt)
                capture = next((s.captured_at for s in info.samples if s.captured_at), None)
            except Exception:
                pass
        rows.append(SourceMedia(path, probe, srt, color_md, capture))
    return rows


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def motion_metrics(source: SourceMedia, start_s: float, duration_s: float) -> MotionMetrics:
    if source.srt_path is None:
        return MotionMetrics()
    try:
        info = parse_srt_info(source.srt_path)
    except Exception:
        return MotionMetrics()
    end_s = start_s + duration_s
    samples = [s for s in info.samples if start_s - 0.05 <= float(s.time) <= end_s + 0.05]
    if not samples:
        return MotionMetrics()
    hs: list[float] = []
    vs: list[float] = []
    alt = [float(s.rel_alt) for s in samples if s.rel_alt is not None]
    gps = sum(1 for s in samples if s.latitude is not None and s.longitude is not None)
    for a, b in zip(samples, samples[1:]):
        dt = float(b.time - a.time)
        if dt <= 0 or dt > 2.5:
            continue
        if None not in (a.latitude, a.longitude, b.latitude, b.longitude):
            hs.append(_haversine_m(float(a.latitude), float(a.longitude), float(b.latitude), float(b.longitude)) / dt)
        if a.rel_alt is not None and b.rel_alt is not None:
            vs.append((float(b.rel_alt) - float(a.rel_alt)) / dt)
    hmed = float(median(hs)) if hs else None
    return MotionMetrics(
        srt_samples=len(samples),
        gps_coverage=gps / len(samples),
        median_horizontal_speed_mps=hmed,
        max_horizontal_speed_mps=max(hs, default=None),
        speed_variation_mps=max((abs(v - hmed) for v in hs), default=None) if hmed is not None else None,
        max_abs_vertical_speed_mps=max((abs(v) for v in vs), default=None),
        altitude_min_m=min(alt, default=None),
        altitude_span_m=(max(alt) - min(alt)) if alt else None,
    )


def motion_score(metrics: MotionMetrics) -> tuple[float, list[str]]:
    if metrics.srt_samples == 0:
        return 0.0, ["no_srt_motion_evidence"]
    score = min(1.0, metrics.gps_coverage) * 1.4
    reasons: list[str] = []
    if metrics.max_abs_vertical_speed_mps is not None:
        if metrics.max_abs_vertical_speed_mps <= 1.0:
            score += 1.0
            reasons.append("stable_vertical_motion")
        elif metrics.max_abs_vertical_speed_mps > 2.0:
            score -= 1.25
            reasons.append("rapid_vertical_reposition")
    if metrics.speed_variation_mps is not None:
        if metrics.speed_variation_mps <= 2.0:
            score += 1.0
            reasons.append("consistent_horizontal_motion")
        elif metrics.speed_variation_mps > 4.5:
            score -= 1.0
            reasons.append("horizontal_speed_change")
    if metrics.max_horizontal_speed_mps is not None and metrics.max_horizontal_speed_mps > 24.0:
        score -= 0.75
        reasons.append("high_translation_speed")
    if metrics.altitude_min_m is not None and metrics.altitude_min_m < 1.5:
        score -= 1.2
        reasons.append("near_ground_takeoff_or_landing_risk")
    return score, reasons


def windows(source: SourceMedia, min_s: float, max_s: float, stride_s: float, edge_s: float) -> list[tuple[float, float]]:
    start, end = edge_s, source.duration - edge_s
    if end - start < min_s:
        return []
    durations = sorted({min_s, min(max_s, 10.0), max_s}, reverse=True)
    out: dict[tuple[int, int], tuple[float, float]] = {}
    cursor = start
    while cursor + min_s <= end + 1e-6:
        for duration in durations:
            if cursor + duration <= end + 1e-6:
                out[(round(cursor * 10), round(duration * 10))] = (cursor, duration)
        cursor += stride_s
    tail_d = min(max_s, end - start)
    out[(round((end - tail_d) * 10), round(tail_d * 10))] = (end - tail_d, tail_d)
    return list(out.values())


def build_candidates(source: SourceMedia, args: argparse.Namespace) -> list[Candidate]:
    target = min(args.max_seconds, max(args.min_seconds, 10.0))
    rows: list[Candidate] = []
    for start, duration in windows(source, args.min_seconds, args.max_seconds, args.stride_seconds, args.edge_trim_seconds):
        motion = motion_metrics(source, start, duration)
        mscore, reasons = motion_score(motion)
        duration_score = 1.0 - min(1.0, abs(duration - target) / max(1.0, target))
        rows.append(Candidate(source, start, duration, mscore + duration_score, "CANDIDATE", reasons, motion))
    return rows


def apply_visual(candidate: Candidate, args: argparse.Namespace, cache: Path) -> None:
    if args.visual_helper is None or assess_visual_coherence is None or VisualSettings is None:
        candidate.reasons.append("visual_not_run")
        return
    assessment = assess_visual_coherence(
        media=candidate.source.path,
        start_s=candidate.start_s,
        duration_s=candidate.duration_s,
        source_anchors=[], telemetry_samples=[], cache_root=cache,
        vision_helper=args.visual_helper, ffmpeg=args.ffmpeg,
        min_duration_s=args.min_seconds,
        settings=VisualSettings(fps=args.visual_fps, width=args.visual_width),
    )
    candidate.visual_status = assessment.status
    candidate.visual_reasons = list(assessment.reasons)
    candidate.visual_metrics = dict(assessment.metrics)
    if assessment.status == "COHERENT":
        candidate.score += 2.5
        candidate.reasons.append("visual_coherent")
    elif assessment.status == "ADVISORY":
        candidate.score += 0.25
        candidate.reasons.append("visual_advisory")
    elif assessment.status == "TRANSITION":
        candidate.score -= 3.0
        candidate.status = "REJECT"
        candidate.reasons.append("visual_transition")
    else:
        candidate.score -= 0.4
        candidate.reasons.append("visual_unavailable")


def _overlap(a: Candidate, b: Candidate) -> float:
    if a.source.path != b.source.path:
        return 0.0
    return max(0.0, min(a.end_s, b.end_s) - max(a.start_s, b.start_s))


def choose_best(candidates: Sequence[Candidate], target_s: float, max_per_source: int) -> list[Candidate]:
    ordered = sorted((c for c in candidates if c.status != "REJECT"), key=lambda c: (-c.score, str(c.source.path), c.start_s))
    selected: list[Candidate] = []
    counts: dict[Path, int] = {}
    total = 0.0
    for candidate in ordered:
        if counts.get(candidate.source.path, 0) >= max_per_source:
            continue
        if any(_overlap(candidate, other) >= min(candidate.duration_s, other.duration_s) * 0.35 for other in selected):
            continue
        candidate.status = "SELECTED"
        selected.append(candidate)
        counts[candidate.source.path] = counts.get(candidate.source.path, 0) + 1
        total += candidate.duration_s
        if total >= target_s:
            break
    return sorted(selected, key=lambda c: (c.source.capture_time or "", str(c.source.path), c.start_s))


def frame_duration(fps: float) -> Fraction:
    common = [
        (23.976, Fraction(1001, 24000)), (24.0, Fraction(1, 24)),
        (25.0, Fraction(1, 25)), (29.97, Fraction(1001, 30000)),
        (30.0, Fraction(1, 30)), (50.0, Fraction(1, 50)),
        (59.94, Fraction(1001, 60000)), (60.0, Fraction(1, 60)),
    ]
    value, grid = min(common, key=lambda row: abs(row[0] - fps))
    return grid if abs(value - fps) <= 0.08 else Fraction(1, max(1, round(fps)))


def snap(value: float, grid: Fraction, mode: str = "nearest") -> Fraction:
    ratio = Fraction(str(round(value, 9))) / grid
    frames = ratio.numerator // ratio.denominator if mode == "floor" else round(ratio)
    return frames * grid


def lut_from_template(path: Path) -> str | None:
    root = ET.parse(path).getroot()
    values = sorted({a.get("customLUTOverride") for a in root.iter() if a.tag.split("}")[-1] == "asset" and a.get("customLUTOverride")})
    if len(values) > 1:
        raise RuntimeError("Template FCPXML contains multiple Camera LUT overrides: " + " | ".join(values))
    return values[0] if values else None


def lut_from_db(path: Path) -> str | None:
    con = sqlite3.connect(path)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(stock_candidates)")}
        if "camera_lut" not in columns:
            return None
        rows = con.execute("SELECT camera_lut, COUNT(*) n FROM stock_candidates WHERE camera_lut IS NOT NULL AND TRIM(camera_lut) != '' AND eligibility_status='accepted' GROUP BY camera_lut ORDER BY n DESC").fetchall()
    finally:
        con.close()
    values = [str(row[0]) for row in rows]
    dji = [value for value in values if "dji" in value.casefold() or "dlog" in value.casefold()]
    if len(dji) == 1:
        return dji[0]
    return values[0] if len(values) == 1 else None


def resolve_lut(args: argparse.Namespace, sources: Sequence[SourceMedia]) -> tuple[str | None, str]:
    if args.camera_lut != "auto":
        return args.camera_lut, "explicit"
    if args.lut_template_fcpxml:
        value = lut_from_template(args.lut_template_fcpxml)
        if value:
            return value, "template_fcpxml"
    if args.db:
        value = lut_from_db(args.db)
        if value:
            return value, "database_unique_dji_camera_lut"
    colors = sorted({str(source.color_md) for source in sources if source.color_md})
    return None, "unresolved:" + (" | ".join(colors) if colors else "no_srt_color_metadata")


def build_fcpxml(selected: Sequence[Candidate], event_name: str, project_name: str, camera_lut: str | None) -> ET.Element:
    if not selected:
        raise RuntimeError("No selected candidates")
    counts: dict[tuple[int, int, Fraction], int] = {}
    for c in selected:
        key = (c.source.width, c.source.height, frame_duration(c.source.fps))
        counts[key] = counts.get(key, 0) + 1
    project_key = max(counts, key=lambda key: (counts[key], key[0] * key[1]))
    root = ET.Element("fcpxml", {"version": "1.13"})
    resources = ET.SubElement(root, "resources")
    formats: dict[tuple[int, int, Fraction], str] = {}
    assets: dict[Path, str] = {}

    def ensure_format(key: tuple[int, int, Fraction]) -> str:
        if key in formats:
            return formats[key]
        width, height, grid = key
        rid = f"r{len(formats) + 1}"
        formats[key] = rid
        ET.SubElement(resources, "format", {"id": rid, "name": f"FFVideoFormat{width}x{height}", "frameDuration": format_time(grid), "width": str(width), "height": str(height), "colorSpace": "1-1-1 (Rec. 709)"})
        return rid

    ensure_format(project_key)
    for c in selected:
        source = c.source
        if source.path in assets:
            continue
        key = (source.width, source.height, frame_duration(source.fps))
        aid = f"a{len(assets) + 1}"
        assets[source.path] = aid
        attrs = {"id": aid, "name": source.path.name, "uid": stable_uid("vclip-card-autocut-asset", str(source.path)), "start": "0s", "duration": format_time(snap(source.duration, key[2], "floor")), "hasVideo": "1", "format": ensure_format(key)}
        if camera_lut:
            attrs["customLUTOverride"] = camera_lut
        asset = ET.SubElement(resources, "asset", attrs)
        ET.SubElement(asset, "media-rep", {"kind": "original-media", "src": source.path.as_uri()})

    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", {"name": event_name, "uid": stable_uid("vclip-card-autocut-event", event_name)})
    project = ET.SubElement(event, "project", {"name": project_name, "uid": stable_uid("vclip-card-autocut-project", project_name)})
    project_grid = project_key[2]
    durations = [min(snap(c.duration_s, frame_duration(c.source.fps), "floor"), snap(c.duration_s, project_grid, "floor")) for c in selected]
    sequence = ET.SubElement(project, "sequence", {"format": formats[project_key], "duration": format_time(sum(durations, Fraction(0))), "tcStart": "0s", "tcFormat": "NDF"})
    spine = ET.SubElement(sequence, "spine")
    offset = Fraction(0)
    for index, (candidate, duration) in enumerate(zip(selected, durations, strict=True), start=1):
        clip = ET.SubElement(spine, "asset-clip", {"name": f"Auto Select {index:02d} — {candidate.source.path.stem}", "ref": assets[candidate.source.path], "offset": format_time(offset), "start": format_time(snap(candidate.start_s, frame_duration(candidate.source.fps))), "duration": format_time(duration)})
        add_vclip_metadata(clip, {
            "com.vclip.autocut.version": AUTOCUT_VERSION,
            "com.vclip.autocut.candidate_id": candidate.candidate_id,
            "com.vclip.autocut.source_start_s": f"{candidate.start_s:.6f}",
            "com.vclip.autocut.duration_s": f"{candidate.duration_s:.6f}",
            "com.vclip.autocut.score": f"{candidate.score:.6f}",
            "com.vclip.autocut.visual_status": candidate.visual_status,
            "com.vclip.autocut.srt_path": str(candidate.source.srt_path or ""),
            "com.vclip.autocut.color_md": candidate.source.color_md or "",
        })
        offset += duration
    return root


def candidate_row(candidate: Candidate) -> dict[str, Any]:
    payload = {
        "candidate_id": candidate.candidate_id, "source_path": str(candidate.source.path), "source_name": candidate.source.path.name,
        "capture_time": candidate.source.capture_time or "", "srt_path": str(candidate.source.srt_path or ""), "color_md": candidate.source.color_md or "",
        "orientation": candidate.source.orientation, "width": candidate.source.width, "height": candidate.source.height, "fps": candidate.source.fps,
        "start_s": round(candidate.start_s, 6), "duration_s": round(candidate.duration_s, 6), "end_s": round(candidate.end_s, 6),
        "score": round(candidate.score, 6), "status": candidate.status, "reasons": "|".join(candidate.reasons),
        "visual_status": candidate.visual_status, "visual_reasons": "|".join(candidate.visual_reasons or []),
    }
    payload.update({f"motion_{key}": value for key, value in asdict(candidate.motion).items()})
    return payload


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--media-root", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--report", type=Path)
    p.add_argument("--event-name")
    p.add_argument("--project-name")
    p.add_argument("--target-seconds", type=float, default=120.0)
    p.add_argument("--min-seconds", type=float, default=5.0)
    p.add_argument("--max-seconds", type=float, default=14.0)
    p.add_argument("--stride-seconds", type=float, default=4.0)
    p.add_argument("--edge-trim-seconds", type=float, default=2.0)
    p.add_argument("--max-per-source", type=int, default=2)
    p.add_argument("--camera-lut", default="auto")
    p.add_argument("--lut-template-fcpxml", type=Path)
    p.add_argument("--db", type=Path)
    p.add_argument("--visual-helper", type=Path)
    p.add_argument("--visual-cache", type=Path)
    p.add_argument("--visual-fps", type=float, default=1.0)
    p.add_argument("--visual-width", type=int, default=320)
    p.add_argument("--ffmpeg", default="ffmpeg")
    p.add_argument("--require-visual", action="store_true")
    p.add_argument("--require-camera-lut", action="store_true")
    return p


def main() -> int:
    args = parser().parse_args()
    args.media_root = args.media_root.expanduser().resolve()
    args.output = args.output.expanduser().resolve()
    args.report = args.report.expanduser().resolve() if args.report else args.output.with_suffix(".json")
    args.db = args.db.expanduser().resolve() if args.db else None
    args.lut_template_fcpxml = args.lut_template_fcpxml.expanduser().resolve() if args.lut_template_fcpxml else None
    args.visual_helper = args.visual_helper.expanduser().resolve() if args.visual_helper else None
    visual_cache = args.visual_cache.expanduser().resolve() if args.visual_cache else args.report.parent / "card-autocut-visual-cache"
    if not args.media_root.is_dir():
        raise SystemExit(f"Media root does not exist: {args.media_root}")
    if args.require_visual and args.visual_helper is None:
        raise SystemExit("--require-visual needs --visual-helper")
    if args.visual_helper and not args.visual_helper.is_file():
        raise SystemExit(f"Visual helper does not exist: {args.visual_helper}")

    print("Scanning DJI media...")
    sources = scan_sources(args.media_root)
    if not sources:
        raise SystemExit("No probeable video media found")
    duration_by_orientation: dict[str, float] = {}
    for source in sources:
        duration_by_orientation[source.orientation] = duration_by_orientation.get(source.orientation, 0.0) + source.duration
    orientation = max(duration_by_orientation, key=duration_by_orientation.get)
    included = [source for source in sources if source.orientation == orientation]
    omitted = [source for source in sources if source.orientation != orientation]

    camera_lut, lut_method = resolve_lut(args, included)
    if args.require_camera_lut and camera_lut is None:
        raise SystemExit("Camera LUT could not be resolved safely: " + lut_method)

    candidates: list[Candidate] = []
    for index, source in enumerate(included, start=1):
        print(f"  {index}/{len(included)} {source.path.name} {source.duration:.1f}s")
        source_candidates = build_candidates(source, args)
        visual_pool = sorted(source_candidates, key=lambda c: (-c.score, c.start_s))[: max(6, args.max_per_source * 4)]
        for candidate in visual_pool:
            apply_visual(candidate, args, visual_cache)
        candidates.extend(source_candidates)

    selected = choose_best(candidates, args.target_seconds, args.max_per_source)
    if not selected:
        raise SystemExit("No usable candidate ranges were selected")
    selected_ids = {candidate.candidate_id for candidate in selected}
    for candidate in candidates:
        if candidate.candidate_id not in selected_ids and candidate.status != "REJECT":
            candidate.status = "NOT_SELECTED"

    date_label = datetime.now().date().isoformat()
    event_name = args.event_name or f"{date_label} — DJI Auto Selects"
    project_name = args.project_name or f"{date_label} — Best Of"
    xml = build_fcpxml(selected, event_name, project_name, camera_lut)
    validation = validate_fcpxml(xml)
    if not validation.passed:
        raise RuntimeError("Generated FCPXML failed validation:\n" + "\n".join(validation.errors[:30]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_fcpxml(xml, args.output)

    rows = [candidate_row(candidate) for candidate in candidates]
    csv_path = args.report.with_suffix(".csv")
    write_csv(csv_path, rows)
    payload = {
        "version": AUTOCUT_VERSION, "media_root": str(args.media_root), "output_fcpxml": str(args.output),
        "event_name": event_name, "project_name": project_name, "sources_found": len(sources), "sources_included": len(included),
        "sources_omitted_other_orientation": [str(source.path) for source in omitted], "orientation": orientation,
        "candidate_count": len(candidates), "selected_count": len(selected), "selected_duration_seconds": sum(c.duration_s for c in selected),
        "target_seconds": args.target_seconds, "camera_lut": camera_lut, "camera_lut_method": lut_method,
        "visual_enabled": args.visual_helper is not None, "visual_helper": str(args.visual_helper) if args.visual_helper else None,
        "visual_cache": str(visual_cache), "selected": [candidate_row(c) for c in selected], "validation": asdict(validation),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print()
    print("VCLIP CARD AUTOCUT")
    print("==================")
    print(f"sources found       : {len(sources)}")
    print(f"sources used        : {len(included)}")
    print(f"orientation         : {orientation}")
    print(f"candidates          : {len(candidates)}")
    print(f"selected            : {len(selected)}")
    print(f"selected duration   : {sum(c.duration_s for c in selected):.1f}s")
    print(f"visual ranking      : {'ON' if args.visual_helper else 'OFF'}")
    print(f"camera LUT          : {camera_lut or '(not applied)'}")
    print(f"camera LUT method   : {lut_method}")
    print(f"FCPXML              : {args.output}")
    print(f"JSON                : {args.report}")
    print(f"CSV                 : {csv_path}")
    if camera_lut is None:
        print("WARNING: Camera LUT was not applied; use --camera-lut or --lut-template-fcpxml for D-Log M")
    print("VCLIP CARD AUTOCUT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
