#!/usr/bin/env python3
"""Reconstruct visual-coherence + telemetry-aware stock shots from one physical VClip review shard.

This is intentionally non-destructive:
- reads one existing review FCPXML
- uses the old VClip ranges as editorial anchors
- resolves DJI SRT sidecars and DJI flight-record telemetry
- scores existing cuts with telemetry AND on-device visual coherence
- treats historical cuts as human anchors, not automatic customer-ready truth
- can derive visual trim candidates when a coherent shot turns into repositioning
- clusters overlapping anchors per source media
- reconstructs longer telemetry + visually gated master shots around those anchors
- writes a NEW FCPXML plus JSON/CSV report
- never writes the canonical SQLite DB or modifies the input shard

It expects scripts/vclip_telemetry_qc.py and scripts/vclip_visual_coherence.py
beside this file, plus the compiled on-device Apple Vision feature-print helper.
Set PYTHONPATH=<repo>/src:<repo>/scripts so existing VClip helpers are used.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Sequence

# Reuse the already-proven corpus telemetry machinery.
try:
    import vclip_telemetry_qc as tqc
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Could not import scripts/vclip_telemetry_qc.py. Put this script in the same "
        f"scripts/ directory. Import error: {exc}"
    )

try:
    from vclip_visual_coherence import (
        VisualAssessment,
        VisualSettings,
        assess_visual_coherence,
        visual_trim_ranges,
    )
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Could not import scripts/vclip_visual_coherence.py. Put it beside this "
        f"script. Import error: {exc}"
    )

try:
    from vclip_pipeline.stockify.core import format_time, parse_time, stable_uid, stock_clip_id
    from vclip_pipeline.stockify.fcpxml import (
        add_vclip_metadata,
        read_vclip_metadata,
        build_resource_index,
        first_direct_child,
        local_name,
        validate_fcpxml,
        write_fcpxml,
    )
    from vclip_pipeline.stockify.metadata import extract_gps_summary
    from vclip_pipeline.stockify.sidecars import (
        build_sidecar_index,
        extract_srt_color_md,
        parse_srt_info,
        sidecar_match_for_asset,
    )
    from vclip_pipeline.workflow.camera_scope import (
        SCOPE_OUT_OF_SCOPE_NON_DRONE,
        classify_vclip_camera_scope,
    )
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Could not import the VClip package. Run with PYTHONPATH=<repo>/src. "
        f"Import error: {exc}"
    )

VCLIP_RE = re.compile(r"VCLIP_[0-9A-F]{12,64}")
CLIP_NUMBER_RE = re.compile(r"\s+—\s+Clip\s+\d+(?:\s*[A-Z])?$", re.I)


@dataclass
class Anchor:
    stock_clip_id: str
    event_name: str
    project_name: str
    source_ref: str
    source_name: str
    source_start_s: float
    duration_s: float
    source_media_path: str | None
    asset_start_s: float
    asset_start: Fraction
    source_frame_duration: Fraction
    sequence_frame_duration: Fraction
    original_clip_start: str
    original_clip_duration: str
    original_sequence_duration: str | None
    template_project: ET.Element = field(repr=False)
    template_source_clip: ET.Element = field(repr=False)
    camera_lut: str | None = None
    media_path: str | None = None
    media_resolution: str | None = None
    scope: str | None = None
    camera_family: str | None = None

    @property
    def end_s(self) -> float:
        return self.source_start_s + self.duration_s


@dataclass
class SourceSample:
    t: float
    pitch: float
    camera_yaw: float
    rel_yaw: float
    aircraft_yaw: float
    h_speed: float
    z_speed: float
    pitch_limit: bool
    yaw_limit: bool
    roll_limit: bool
    stuck: bool


@dataclass
class SourceContext:
    source_name: str
    media_path: str | None
    media_creation_time: str | None
    media_duration_s: float | None
    media_resolution: str | None
    flight_log: str | None
    flight_version: int | None
    aircraft_name: str | None
    alignment_method: str | None
    srt_path: str | None
    srt_method: str | None
    srt_confidence: str | None
    srt_sample_count: int = 0
    srt_has_position: bool = False
    srt_has_altitude: bool = False
    srt_has_orientation: bool = False
    srt_color_md: str | None = None
    error: str | None = None


@dataclass
class ReconstructedShot:
    stock_clip_id: str
    source_name: str
    start_s: float
    duration_s: float
    end_s: float
    parent_ids: list[str]
    parent_projects: list[str]
    template_parent_id: str
    project_name: str
    event_name: str
    telemetry_status: str
    telemetry_reasons: list[str]
    editorial_anchor_count: int
    editorial_support_seconds: float
    flight_log: str | None
    srt_path: str | None
    srt_gps: dict[str, Any] | None
    transition_free_interval: tuple[float, float] | None


@dataclass
class ReadyVariant:
    stock_clip_id: str
    parent_id: str
    source_name: str
    start_s: float
    duration_s: float
    project_name: str
    event_name: str
    status: str
    reasons: list[str]
    action: str
    flight_log: str | None
    srt_path: str | None


@dataclass
class OperatorMotionAssessment:
    status: str  # CLEAN | MOVEMENT_ADVISORY | TRANSITION | NO_TELEMETRY
    reasons: list[str]
    metrics: dict[str, float]


@dataclass(frozen=True)
class OperatorMotionThresholds:
    # Hard transition/repositioning thresholds. These are deliberately
    # conservative: hard events define boundaries we should not ship through.
    hard_camera_yaw_1s: float = 12.0
    hard_camera_yaw_2s: float = 22.0
    hard_camera_yaw_3s: float = 30.0
    hard_aircraft_yaw_1s: float = 12.0
    hard_aircraft_yaw_2s: float = 22.0
    hard_aircraft_yaw_3s: float = 30.0
    hard_h_speed_delta_1s: float = 2.5
    hard_z_speed_delta_1s: float = 1.5
    hard_combined_pitch_2s: float = 5.0
    hard_combined_camera_yaw_2s: float = 8.0

    # Movement advisories do not mean the footage is bad. They mean a generated
    # cut/master should not be promoted automatically without visual approval.
    advisory_camera_yaw_3s: float = 6.0
    advisory_aircraft_yaw_3s: float = 6.0
    advisory_camera_yaw_span: float = 10.0
    advisory_aircraft_yaw_span: float = 10.0
    advisory_h_speed_delta_1s: float = 1.25
    advisory_z_speed_delta_1s: float = 0.75


def sec_fraction(value: float) -> Fraction:
    return Fraction(round(value * 60000), 60000)


def fmt(value: float) -> str:
    return format_time(sec_fraction(value))


def _fraction_from_float(value: float) -> Fraction:
    """Convert telemetry-derived seconds to a stable exact rational."""
    return Fraction(str(round(float(value), 9)))


def _snap_to_grid(value: float, grid: Fraction, mode: str = "nearest") -> Fraction:
    """Snap seconds to an exact FCP edit-frame grid."""
    if grid <= 0:
        raise ValueError(f"Invalid frame duration: {grid}")
    raw = _fraction_from_float(value)
    ratio = raw / grid
    if mode == "floor":
        frames = ratio.numerator // ratio.denominator
    elif mode == "ceil":
        frames = -((-ratio.numerator) // ratio.denominator)
    elif mode == "nearest":
        frames = round(ratio)
    else:
        raise ValueError(f"Unsupported snap mode: {mode}")
    return frames * grid


def quantize_generated_range(
    anchor: Anchor,
    start_s: float,
    duration_s: float,
) -> tuple[float, float, str, str]:
    """Quantize a machine-generated source range to Final Cut frame boundaries.

    Source start is snapped to the source-media frame grid. Timeline duration is
    floored to the project sequence frame grid so a reconstructed clip never
    extends beyond the telemetry-selected end because of rounding.
    """
    source_start = _snap_to_grid(start_s, anchor.source_frame_duration, "nearest")
    duration = _snap_to_grid(duration_s, anchor.sequence_frame_duration, "floor")
    if duration <= 0:
        duration = anchor.sequence_frame_duration
    return (
        float(source_start),
        float(duration),
        format_time(source_start),
        format_time(duration),
    )


def _is_grid_aligned(value: Fraction, grid: Fraction) -> bool:
    if grid <= 0:
        return False
    ratio = value / grid
    return ratio.denominator == 1


def edit_frame_boundary_errors(root: ET.Element) -> list[str]:
    # Audit only machine-generated project timing on exact edit-frame grids.
    # Historical review projects are preserved byte-for-byte. Final Cut can
    # import legacy projects whose durations are exact seconds even when those
    # values are not integer multiples of NTSC rational frame durations.
    resources = first_direct_child(root, "resources")
    if resources is None:
        return ["missing <resources>"]
    index = build_resource_index(resources)
    errors: list[str] = []

    for event in root.iter():
        if local_name(event.tag) != "event":
            continue
        for project in list(event):
            if local_name(project.tag) != "project":
                continue

            project_name = project.get("name", "")
            sequence = first_direct_child(project, "sequence")
            if sequence is None:
                continue

            fmt_res = index.get(sequence.get("format") or "")
            frame_raw = fmt_res.get("frameDuration") if fmt_res is not None else None
            if not frame_raw:
                errors.append(f"{project_name}: missing sequence frameDuration")
                continue
            frame = parse_time(frame_raw)

            asset_clips = [
                clip
                for clip in sequence.iter()
                if local_name(clip.tag) == "asset-clip"
            ]
            if not asset_clips:
                continue

            generated_clips: list[tuple[ET.Element, bool]] = []
            project_has_generated_timing = False

            for clip in asset_clips:
                metadata = read_vclip_metadata(clip)
                variant = metadata.get("com.vclip.telemetry.variant")
                current_id = metadata.get("com.vclip.stock_clip_id")
                parent_ids = {
                    value.strip()
                    for value in metadata.get(
                        "com.vclip.telemetry.parent_ids", ""
                    ).split(",")
                    if value.strip()
                }

                generated_timing = variant in {
                    "extended_master",
                    "repair_candidate",
                } or (
                    variant == "ready_cut"
                    and current_id not in parent_ids
                )
                generated_clips.append((clip, generated_timing))
                project_has_generated_timing = (
                    project_has_generated_timing or generated_timing
                )

            # Historical originals, QC-original copies, and historical-ready
            # copies retain their legacy timing exactly and are not audited here.
            if not project_has_generated_timing:
                continue

            sequence_duration_raw = sequence.get("duration")
            if sequence_duration_raw:
                sequence_duration = parse_time(sequence_duration_raw)
                if not _is_grid_aligned(sequence_duration, frame):
                    errors.append(
                        f"{project_name}: sequence duration "
                        f"{sequence_duration_raw} is not aligned to {frame_raw}"
                    )

            for clip, generated_timing in generated_clips:
                if not generated_timing:
                    continue

                duration_raw = clip.get("duration")
                if duration_raw and not _is_grid_aligned(
                    parse_time(duration_raw), frame
                ):
                    errors.append(
                        f"{project_name}: asset-clip duration {duration_raw} "
                        f"is not aligned to sequence {frame_raw}"
                    )

                ref = clip.get("ref") or ""
                asset = index.get(ref)
                if asset is None or local_name(asset.tag) != "asset":
                    continue

                asset_fmt = index.get(asset.get("format") or "")
                source_frame_raw = (
                    asset_fmt.get("frameDuration")
                    if asset_fmt is not None
                    else None
                )
                start_raw = clip.get("start")
                if source_frame_raw and start_raw:
                    source_frame = parse_time(source_frame_raw)
                    relative_start = (
                        parse_time(start_raw) - parse_time(asset.get("start"))
                    )
                    if not _is_grid_aligned(relative_start, source_frame):
                        errors.append(
                            f"{project_name}: generated source start {start_raw} "
                            f"is not aligned to source {source_frame_raw} "
                            f"relative to asset origin"
                        )

    return errors

def parse_input_shard(path: Path) -> tuple[ET.ElementTree, list[Anchor], dict[str, ET.Element]]:
    tree = ET.parse(path)
    root = tree.getroot()
    resources = first_direct_child(root, "resources")
    if resources is None:
        raise RuntimeError("Input shard has no <resources>.")
    resource_index = build_resource_index(resources)

    anchors: list[Anchor] = []
    for event in root.iter():
        if local_name(event.tag) != "event":
            continue
        event_name = event.get("name", "")
        for project in list(event):
            if local_name(project.tag) != "project":
                continue
            project_name = project.get("name", "")
            blob = ET.tostring(project, encoding="unicode")
            ids = sorted(set(VCLIP_RE.findall(blob)))
            if not ids:
                continue

            sequence = first_direct_child(project, "sequence")
            if sequence is None:
                continue
            sequence_format = resource_index.get(sequence.get("format") or "")
            sequence_frame_raw = (
                sequence_format.get("frameDuration")
                if sequence_format is not None
                else None
            )

            source_candidates: list[ET.Element] = []
            for elem in project.iter():
                if local_name(elem.tag) == "asset-clip" and elem.get("ref"):
                    source_candidates.append(elem)
            if not source_candidates:
                continue
            source_clip = source_candidates[0]
            ref = source_clip.get("ref") or ""
            asset = resource_index.get(ref)
            if asset is None or local_name(asset.tag) != "asset":
                continue

            asset_format = resource_index.get(asset.get("format") or "")
            source_frame_raw = (
                asset_format.get("frameDuration") if asset_format is not None else None
            )
            source_frame_duration = (
                parse_time(source_frame_raw)
                if source_frame_raw
                else (
                    parse_time(sequence_frame_raw)
                    if sequence_frame_raw
                    else Fraction(1, 30)
                )
            )
            sequence_frame_duration = (
                parse_time(sequence_frame_raw)
                if sequence_frame_raw
                else source_frame_duration
            )

            absolute_start = tqc.parse_fraction_seconds(source_clip.get("start"))
            asset_start_exact = parse_time(asset.get("start"))
            asset_start = float(asset_start_exact)
            duration = tqc.parse_fraction_seconds(source_clip.get("duration"))
            if absolute_start is None or duration is None or duration <= 0:
                continue
            start = absolute_start - asset_start
            if start < -0.001:
                # Defensive compatibility with legacy FCPXMLs where clip start
                # was already emitted source-relative.
                start = absolute_start

            uri = tqc.find_media_uri(asset)
            xml_media_path = tqc.file_uri_to_path(uri)
            camera_lut = asset.get("customLUTOverride")
            source_name = source_clip.get("name") or asset.get("name") or ""
            if not source_name:
                continue

            # Generated review projects should contain one canonical VClip ID.
            # If legacy metadata causes multiple IDs to appear, preserve one
            # appearance per ID in the report but use the same physical source range.
            for stock_id in ids:
                anchors.append(
                    Anchor(
                        stock_clip_id=stock_id,
                        event_name=event_name,
                        project_name=project_name,
                        source_ref=ref,
                        source_name=source_name,
                        source_start_s=start,
                        duration_s=duration,
                        source_media_path=xml_media_path,
                        asset_start_s=asset_start,
                        asset_start=asset_start_exact,
                        source_frame_duration=source_frame_duration,
                        sequence_frame_duration=sequence_frame_duration,
                        original_clip_start=source_clip.get("start") or "0s",
                        original_clip_duration=source_clip.get("duration") or "0s",
                        original_sequence_duration=sequence.get("duration"),
                        template_project=project,
                        template_source_clip=source_clip,
                        camera_lut=camera_lut,
                    )
                )

    # Deduplicate by physical VClip ID deterministically.
    by_id: dict[str, Anchor] = {}
    for anchor in sorted(anchors, key=lambda a: (a.stock_clip_id, a.project_name, a.source_start_s)):
        by_id.setdefault(anchor.stock_clip_id, anchor)
    return tree, list(by_id.values()), resource_index


def to_clip_appearance(anchor: Anchor, shard: Path) -> tqc.ClipAppearance:
    return tqc.ClipAppearance(
        stock_clip_id=anchor.stock_clip_id,
        review_root=str(shard.parent),
        shard_path=str(shard),
        event_name=anchor.event_name,
        project_name=anchor.project_name,
        source_ref=anchor.source_ref,
        source_name=anchor.source_name,
        source_start_s=anchor.source_start_s,
        duration_s=anchor.duration_s,
        xml_media_path=anchor.source_media_path,
        camera_lut=anchor.camera_lut,
    )


def resolve_media(anchors: list[Anchor], shard: Path, media_roots: Sequence[Path]) -> None:
    appearances = [to_clip_appearance(anchor, shard) for anchor in anchors]
    tqc.resolve_media_paths(appearances, media_roots)
    by_id = {clip.stock_clip_id: clip for clip in appearances}
    for anchor in anchors:
        clip = by_id[anchor.stock_clip_id]
        anchor.media_path = clip.media_path
        anchor.media_resolution = clip.media_resolution
        scope = classify_vclip_camera_scope(
            source_basename=anchor.source_name,
            media_path=anchor.media_path or anchor.source_media_path,
            camera_lut=anchor.camera_lut,
            source_event_name=anchor.event_name,
            source_project_name=anchor.project_name,
        )
        anchor.scope = str(scope.get("camera_scope") or "unknown")
        anchor.camera_family = str(scope.get("camera_family") or "unknown")


def build_srt_context(
    resource_index: dict[str, ET.Element],
    media_roots: Sequence[Path],
) -> tuple[Any, dict[str, Any]]:
    assets = [
        value
        for value in resource_index.values()
        if local_name(value.tag) == "asset"
    ]
    index = build_sidecar_index(assets, media_roots)
    return index, {"summary": asdict(index.summary) if hasattr(index.summary, "__dataclass_fields__") else {}}


def unique_source_groups(anchors: Sequence[Anchor]) -> dict[str, list[Anchor]]:
    groups: dict[str, list[Anchor]] = defaultdict(list)
    for anchor in anchors:
        groups[tqc.normalize_source_stem(anchor.source_name) or anchor.source_name.casefold()].append(anchor)
    return groups


def source_relative_samples(
    flight: tqc.FlightSamples,
    creation_dt: datetime,
    media_duration_s: float,
) -> list[SourceSample]:
    gyaw = tqc.unwrap_degrees(flight.gimbal_yaw)
    ayaw = tqc.unwrap_degrees(flight.aircraft_yaw)
    rel = tqc.unwrap_degrees([tqc.wrap180(g - a) for g, a in zip(gyaw, ayaw)])
    samples: list[SourceSample] = []
    for i, ts in enumerate(flight.timestamps):
        t = (ts - creation_dt).total_seconds()
        if -0.25 <= t <= media_duration_s + 0.25:
            samples.append(
                SourceSample(
                    t=t,
                    pitch=flight.pitch[i],
                    camera_yaw=gyaw[i],
                    rel_yaw=rel[i],
                    aircraft_yaw=ayaw[i],
                    h_speed=flight.h_speed[i],
                    z_speed=flight.z_speed[i],
                    pitch_limit=flight.pitch_limit[i],
                    yaw_limit=flight.yaw_limit[i],
                    roll_limit=flight.roll_limit[i],
                    stuck=flight.stuck[i],
                )
            )
    return samples


def rolling_windows(
    samples: Sequence[SourceSample],
    attr: str,
    window_s: float,
    tolerance_s: float = 0.22,
) -> list[tuple[float, float, float]]:
    times = [s.t for s in samples]
    values = [float(getattr(s, attr)) for s in samples]
    out: list[tuple[float, float, float]] = []
    for i, t0 in enumerate(times):
        target = t0 + window_s
        j = bisect_left(times, target)
        if j >= len(times) or abs(times[j] - target) > tolerance_s:
            continue
        out.append((t0, times[j], values[j] - values[i]))
    return out




def _samples_in_range(
    samples: Sequence[SourceSample],
    start_s: float,
    duration_s: float,
) -> list[SourceSample]:
    end_s = start_s + duration_s
    return [sample for sample in samples if start_s - 0.05 <= sample.t <= end_s + 0.05]


def _span(samples: Sequence[SourceSample], attr: str) -> float:
    if not samples:
        return 0.0
    values = [float(getattr(sample, attr)) for sample in samples]
    return max(values) - min(values)


def _max_abs_window_delta(
    samples: Sequence[SourceSample],
    attr: str,
    window_s: float,
) -> float:
    rows = rolling_windows(samples, attr, window_s)
    return max((abs(delta) for _a, _b, delta in rows), default=0.0)


def operator_motion_assessment(
    samples: Sequence[SourceSample],
    start_s: float,
    duration_s: float,
    thresholds: OperatorMotionThresholds,
) -> OperatorMotionAssessment:
    """Classify inferred controller/aircraft behavior inside one candidate range.

    This intentionally separates "cinematic movement may exist" from
    "operator transition/repositioning is visible". Generated cuts are promoted
    automatically only when this returns CLEAN. MOVEMENT_ADVISORY is not a
    rejection; it means visual approval is required because a sustained pan,
    orbit, turn, or acceleration may still be intentional/cinematic.
    """
    window = _samples_in_range(samples, start_s, duration_s)
    if len(window) < 2:
        return OperatorMotionAssessment("NO_TELEMETRY", ["insufficient_samples"], {})

    metrics = {
        "camera_yaw_span_deg": _span(window, "camera_yaw"),
        "aircraft_yaw_span_deg": _span(window, "aircraft_yaw"),
        "pitch_span_deg": _span(window, "pitch"),
        "relative_yaw_span_deg": _span(window, "rel_yaw"),
        "max_camera_yaw_delta_1s_deg": _max_abs_window_delta(window, "camera_yaw", 1.0),
        "max_camera_yaw_delta_2s_deg": _max_abs_window_delta(window, "camera_yaw", 2.0),
        "max_camera_yaw_delta_3s_deg": _max_abs_window_delta(window, "camera_yaw", 3.0),
        "max_aircraft_yaw_delta_1s_deg": _max_abs_window_delta(window, "aircraft_yaw", 1.0),
        "max_aircraft_yaw_delta_2s_deg": _max_abs_window_delta(window, "aircraft_yaw", 2.0),
        "max_aircraft_yaw_delta_3s_deg": _max_abs_window_delta(window, "aircraft_yaw", 3.0),
        "max_pitch_delta_2s_deg": _max_abs_window_delta(window, "pitch", 2.0),
        "max_h_speed_delta_1s_mps": _max_abs_window_delta(window, "h_speed", 1.0),
        "max_z_speed_delta_1s_mps": _max_abs_window_delta(window, "z_speed", 1.0),
    }

    transition: list[str] = []
    advisory: list[str] = []

    # Gimbal/aircraft limit and stuck conditions are always transition-level.
    if any(s.stuck for s in window):
        transition.append("gimbal_stuck")
    if any(s.pitch_limit or s.yaw_limit or s.roll_limit for s in window):
        transition.append("gimbal_limit")

    # Hard world-space heading change: this catches the case where relative yaw
    # looks calm because the whole aircraft is turning underneath the gimbal.
    if metrics["max_camera_yaw_delta_1s_deg"] >= thresholds.hard_camera_yaw_1s:
        transition.append("rapid_camera_yaw_1s")
    if metrics["max_camera_yaw_delta_2s_deg"] >= thresholds.hard_camera_yaw_2s:
        transition.append("rapid_camera_yaw_2s")
    if metrics["max_camera_yaw_delta_3s_deg"] >= thresholds.hard_camera_yaw_3s:
        transition.append("rapid_camera_yaw_3s")
    if metrics["max_aircraft_yaw_delta_1s_deg"] >= thresholds.hard_aircraft_yaw_1s:
        transition.append("rapid_aircraft_yaw_1s")
    if metrics["max_aircraft_yaw_delta_2s_deg"] >= thresholds.hard_aircraft_yaw_2s:
        transition.append("rapid_aircraft_yaw_2s")
    if metrics["max_aircraft_yaw_delta_3s_deg"] >= thresholds.hard_aircraft_yaw_3s:
        transition.append("rapid_aircraft_yaw_3s")

    # Start/stop/braking evidence from scalar speed changes.
    if metrics["max_h_speed_delta_1s_mps"] >= thresholds.hard_h_speed_delta_1s:
        transition.append("rapid_horizontal_speed_change")
    if metrics["max_z_speed_delta_1s_mps"] >= thresholds.hard_z_speed_delta_1s:
        transition.append("rapid_vertical_speed_change")

    # Combined camera reorientation is especially characteristic of setup/repositioning.
    if (
        metrics["max_pitch_delta_2s_deg"] >= thresholds.hard_combined_pitch_2s
        and metrics["max_camera_yaw_delta_2s_deg"] >= thresholds.hard_combined_camera_yaw_2s
    ):
        transition.append("combined_pitch_yaw_reorientation")

    # Sustained movement is not automatically bad; it is an advisory because it
    # can be a cinematic pan/orbit. Generated assets require visual approval.
    if metrics["max_camera_yaw_delta_3s_deg"] >= thresholds.advisory_camera_yaw_3s:
        advisory.append("sustained_camera_yaw")
    if metrics["max_aircraft_yaw_delta_3s_deg"] >= thresholds.advisory_aircraft_yaw_3s:
        advisory.append("sustained_aircraft_yaw")
    if metrics["camera_yaw_span_deg"] >= thresholds.advisory_camera_yaw_span:
        advisory.append("large_camera_heading_span")
    if metrics["aircraft_yaw_span_deg"] >= thresholds.advisory_aircraft_yaw_span:
        advisory.append("large_aircraft_heading_span")
    if metrics["max_h_speed_delta_1s_mps"] >= thresholds.advisory_h_speed_delta_1s:
        advisory.append("horizontal_acceleration_advisory")
    if metrics["max_z_speed_delta_1s_mps"] >= thresholds.advisory_z_speed_delta_1s:
        advisory.append("vertical_acceleration_advisory")

    if transition:
        return OperatorMotionAssessment("TRANSITION", sorted(set(transition + advisory)), metrics)
    if advisory:
        return OperatorMotionAssessment("MOVEMENT_ADVISORY", sorted(set(advisory)), metrics)
    return OperatorMotionAssessment("CLEAN", [], metrics)


def non_overridable_operator_failure(motion: OperatorMotionAssessment) -> bool:
    """Mechanical/setup failures that visual smoothness must not silently excuse.

    Large sustained yaw alone may be a beautiful orbit/pan and can be visually
    rescued. Gimbal faults and combined pitch+yaw reorientation are stronger
    evidence that the operator is actively resetting the shot.
    """
    reasons = set(motion.reasons)
    return bool(
        reasons
        & {
            "gimbal_stuck",
            "gimbal_limit",
            "combined_pitch_yaw_reorientation",
        }
    )


def historical_ready_pass(
    score: tqc.ClipScore,
    motion: OperatorMotionAssessment,
    visual: VisualAssessment,
) -> tuple[bool, str]:
    """Customer-facing v3 readiness for an existing human-edited interval."""
    old_ready = (
        score.status in {"PASS", "NO_TELEMETRY"}
        and motion.status != "TRANSITION"
    )

    if visual.status == "TRANSITION":
        return False, "visual_transition"
    if visual.status == "COHERENT":
        if non_overridable_operator_failure(motion):
            return False, "hard_operator_failure"
        if old_ready:
            return True, "telemetry_and_visual_ready"
        # The visual layer may rescue a historical human selection that was
        # demoted only because motion magnitude looked suspicious. This is how
        # intentional pans/orbits can recover from conservative telemetry.
        return True, "visual_rescue"
    if visual.status == "ADVISORY":
        # Require the old telemetry gate too when visual evidence is ambiguous.
        return old_ready, "visual_advisory"
    return old_ready, "visual_unavailable_fallback"


def generated_ready_pass(
    score: tqc.ClipScore,
    motion: OperatorMotionAssessment,
    visual: VisualAssessment,
) -> tuple[bool, str]:
    """Generated trims/masters require clean visual coherence plus no hard fault."""
    if visual.status != "COHERENT":
        return False, f"visual_{visual.status.lower()}"
    if non_overridable_operator_failure(motion):
        return False, "hard_operator_failure"
    if score.status != "PASS":
        return False, f"legacy_qc_{score.status.lower()}"
    # A sustained smooth pan/orbit may be MOVEMENT_ADVISORY but can still be a
    # coherent stock shot when the visual evidence confirms it.
    if motion.status not in {"CLEAN", "MOVEMENT_ADVISORY"}:
        return False, f"operator_{motion.status.lower()}"
    return True, "generated_visual_ready"


def transition_free_intervals(
    samples: Sequence[SourceSample],
    media_duration_s: float,
    thresholds: tqc.Thresholds,
    motion_thresholds: OperatorMotionThresholds,
    *,
    pad_s: float = 0.30,
    min_interval_s: float = 4.0,
) -> list[tuple[float, float]]:
    if not samples:
        return []
    times = [s.t for s in samples]
    bad = [False] * len(samples)

    def mark(t0: float, t1: float) -> None:
        lo = bisect_left(times, max(-0.25, t0 - pad_s))
        hi = bisect_left(times, min(media_duration_s + 0.25, t1 + pad_s))
        hi = min(len(times), hi + 1)
        for i in range(lo, hi):
            bad[i] = True

    for sample in samples:
        if sample.stuck or sample.pitch_limit or sample.yaw_limit or sample.roll_limit:
            mark(sample.t, sample.t)

    for t0, t1, delta in rolling_windows(samples, "pitch", 1.0):
        if abs(delta) >= thresholds.hard_pitch_delta_1s_deg:
            mark(t0, t1)
    for t0, t1, delta in rolling_windows(samples, "pitch", 2.0):
        if abs(delta) >= thresholds.hard_pitch_delta_2s_deg:
            mark(t0, t1)
    for t0, t1, delta in rolling_windows(samples, "pitch", 3.0):
        if abs(delta) >= thresholds.hard_pitch_delta_3s_deg:
            mark(t0, t1)
    for t0, t1, delta in rolling_windows(samples, "rel_yaw", 1.0):
        if abs(delta) >= thresholds.hard_relative_yaw_delta_1s_deg:
            mark(t0, t1)

    # v2: world-space camera/aircraft behavior. Relative yaw alone misses the
    # common case where the whole drone turns while the gimbal remains steady
    # relative to the aircraft.
    for window_s, attr, limit in (
        (1.0, "camera_yaw", motion_thresholds.hard_camera_yaw_1s),
        (2.0, "camera_yaw", motion_thresholds.hard_camera_yaw_2s),
        (3.0, "camera_yaw", motion_thresholds.hard_camera_yaw_3s),
        (1.0, "aircraft_yaw", motion_thresholds.hard_aircraft_yaw_1s),
        (2.0, "aircraft_yaw", motion_thresholds.hard_aircraft_yaw_2s),
        (3.0, "aircraft_yaw", motion_thresholds.hard_aircraft_yaw_3s),
        (1.0, "h_speed", motion_thresholds.hard_h_speed_delta_1s),
        (1.0, "z_speed", motion_thresholds.hard_z_speed_delta_1s),
    ):
        for t0, t1, delta in rolling_windows(samples, attr, window_s):
            if abs(delta) >= limit:
                mark(t0, t1)

    pitch2 = {(round(a, 2), round(b, 2)): d for a, b, d in rolling_windows(samples, "pitch", 2.0)}
    yaw2 = {(round(a, 2), round(b, 2)): d for a, b, d in rolling_windows(samples, "camera_yaw", 2.0)}
    for key in set(pitch2) & set(yaw2):
        if (
            abs(pitch2[key]) >= motion_thresholds.hard_combined_pitch_2s
            and abs(yaw2[key]) >= motion_thresholds.hard_combined_camera_yaw_2s
        ):
            mark(key[0], key[1])

    intervals: list[tuple[float, float]] = []
    start: float | None = None
    prev: float | None = None
    for sample, is_bad in zip(samples, bad):
        if prev is not None and sample.t - prev > 0.40:
            if start is not None and prev - start >= min_interval_s:
                intervals.append((max(0.0, start), min(media_duration_s, prev)))
            start = None
        if is_bad:
            if start is not None and prev is not None and prev - start >= min_interval_s:
                intervals.append((max(0.0, start), min(media_duration_s, prev)))
            start = None
        elif start is None:
            start = sample.t
        prev = sample.t
    if start is not None and prev is not None and prev - start >= min_interval_s:
        intervals.append((max(0.0, start), min(media_duration_s, prev)))

    merged: list[tuple[float, float]] = []
    for a, b in intervals:
        if merged and a - merged[-1][1] <= 0.25:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def iou(a0: float, a1: float, b0: float, b1: float) -> float:
    inter = overlap(a0, a1, b0, b1)
    union = max(a1, b1) - min(a0, b0)
    return inter / union if union > 0 else 0.0


def cluster_anchors(anchors: Sequence[Anchor], merge_gap_s: float) -> list[list[Anchor]]:
    ordered = sorted(anchors, key=lambda a: (a.source_start_s, a.end_s, a.stock_clip_id))
    clusters: list[list[Anchor]] = []
    current: list[Anchor] = []
    current_end = -1.0
    for anchor in ordered:
        if not current or anchor.source_start_s <= current_end + merge_gap_s:
            current.append(anchor)
            current_end = max(current_end, anchor.end_s)
        else:
            clusters.append(current)
            current = [anchor]
            current_end = anchor.end_s
    if current:
        clusters.append(current)
    return clusters


def editorial_support(anchors: Sequence[Anchor], a: float, b: float) -> tuple[int, float]:
    touched = sum(1 for anchor in anchors if overlap(anchor.source_start_s, anchor.end_s, a, b) >= 0.25)
    support = sum(overlap(anchor.source_start_s, anchor.end_s, a, b) for anchor in anchors)
    return touched, support


def best_window_within(
    a: float,
    b: float,
    anchors: Sequence[Anchor],
    length: float,
) -> tuple[float, float]:
    if b - a <= length:
        return a, b
    starts = {a, b - length}
    for anchor in anchors:
        starts.add(max(a, min(b - length, anchor.source_start_s)))
        starts.add(max(a, min(b - length, anchor.end_s - length)))
        center = (anchor.source_start_s + anchor.end_s) / 2
        starts.add(max(a, min(b - length, center - length / 2)))
    ranked = []
    for start in starts:
        end = start + length
        touched, support = editorial_support(anchors, start, end)
        ranked.append((touched, support, -start, start, end))
    _, _, _, start, end = max(ranked)
    return start, end


def expand_to_target(
    start: float,
    end: float,
    regime: tuple[float, float],
    anchors: Sequence[Anchor],
    target_s: float,
    max_s: float,
    max_extension_each_side_s: float,
) -> tuple[float, float]:
    ra, rb = regime
    current = end - start
    if current > max_s:
        return best_window_within(start, end, anchors, max_s)
    if current >= target_s:
        return start, end

    wanted = min(target_s, max_s, rb - ra)
    extra = wanted - current
    left_room = min(start - ra, max_extension_each_side_s)
    right_room = min(rb - end, max_extension_each_side_s)
    left = min(left_room, extra / 2)
    right = min(right_room, extra - left)
    if left + right < extra:
        left += min(left_room - left, extra - left - right)
    if left + right < extra:
        right += min(right_room - right, extra - left - right)
    return start - left, end + right


def reconstruct_windows(
    anchors: Sequence[Anchor],
    regimes: Sequence[tuple[float, float]],
    *,
    min_duration_s: float,
    target_duration_s: float,
    max_duration_s: float,
    max_extension_each_side_s: float,
) -> list[tuple[float, float, list[Anchor], tuple[float, float]]]:
    proposals: list[tuple[float, float, list[Anchor], tuple[float, float]]] = []
    for cluster in cluster_anchors(anchors, 0.75):
        c_start = min(a.source_start_s for a in cluster)
        c_end = max(a.end_s for a in cluster)
        regime_candidates = []
        for regime in regimes:
            ra, rb = regime
            touched = [a for a in cluster if overlap(a.source_start_s, a.end_s, ra, rb) >= 0.25]
            if not touched:
                continue
            supported_start = max(ra, min(a.source_start_s for a in touched))
            supported_end = min(rb, max(a.end_s for a in touched))
            if supported_end <= supported_start:
                continue
            count, support = editorial_support(touched, supported_start, supported_end)
            regime_candidates.append((count, support, supported_end - supported_start, regime, touched, supported_start, supported_end))

        # If a transition splits one editorial cluster into multiple meaningful
        # regimes, keep each regime that has a useful supported segment.
        for _count, _support, _dur, regime, touched, start, end in sorted(
            regime_candidates,
            key=lambda row: (row[3][0], row[3][1]),
        ):
            start, end = expand_to_target(
                start,
                end,
                regime,
                touched,
                target_duration_s,
                max_duration_s,
                max_extension_each_side_s,
            )
            if end - start >= min_duration_s:
                proposals.append((start, end, touched, regime))

        # With no flight regime, we intentionally do NOT invent a longer master.
        # The existing ready cuts remain available in the output XML.

    # Collapse highly overlapping machine proposals from the same source.
    ranked = sorted(
        proposals,
        key=lambda row: (
            -len(row[2]),
            -editorial_support(row[2], row[0], row[1])[1],
            -(row[1] - row[0]),
            row[0],
        ),
    )
    kept: list[tuple[float, float, list[Anchor], tuple[float, float]]] = []
    for proposal in ranked:
        if any(iou(proposal[0], proposal[1], k[0], k[1]) >= 0.80 for k in kept):
            continue
        kept.append(proposal)
    return sorted(kept, key=lambda row: (row[0], row[1]))


def replace_vclip_ids(element: ET.Element, new_id: str) -> None:
    for node in element.iter():
        for key, value in list(node.attrib.items()):
            if "VCLIP_" in value:
                node.set(key, VCLIP_RE.sub(new_id, value))
        if node.text and "VCLIP_" in node.text:
            node.text = VCLIP_RE.sub(new_id, node.text)
        if node.tail and "VCLIP_" in node.tail:
            node.tail = VCLIP_RE.sub(new_id, node.tail)


def primary_asset_clip(project: ET.Element, source_ref: str) -> ET.Element:
    candidates = [
        elem
        for elem in project.iter()
        if local_name(elem.tag) == "asset-clip" and elem.get("ref") == source_ref
    ]
    if not candidates:
        candidates = [elem for elem in project.iter() if local_name(elem.tag) == "asset-clip"]
    if not candidates:
        raise RuntimeError("Template project contains no asset-clip.")
    return candidates[0]


def rewrite_project(
    anchor: Anchor,
    *,
    new_id: str,
    new_name: str,
    start_s: float,
    duration_s: float,
    metadata: dict[str, str],
    preserve_original_timing: bool = False,
) -> ET.Element:
    project = copy.deepcopy(anchor.template_project)
    project.set("name", new_name)
    project.set("uid", stable_uid("telemetry-reconstruct", new_id, new_name))
    replace_vclip_ids(project, new_id)

    clip = primary_asset_clip(project, anchor.source_ref)
    sequence = next((node for node in project.iter() if local_name(node.tag) == "sequence"), None)
    if sequence is None:
        raise RuntimeError(f"Project {new_name!r} has no sequence.")

    if preserve_original_timing:
        # Input shard timing is already known-good in Final Cut. Preserve its
        # exact rational values instead of round-tripping through float seconds.
        clip.set("start", anchor.original_clip_start)
        clip.set("duration", anchor.original_clip_duration)
        if anchor.original_sequence_duration:
            sequence.set("duration", anchor.original_sequence_duration)
    else:
        snapped_start_s, _snapped_duration_s, _start_text, duration_text = (
            quantize_generated_range(anchor, start_s, duration_s)
        )
        # Asset-clip start is source time. Keep the existing asset start origin,
        # but snap the relative source position to the source media's frame grid.
        absolute_start = anchor.asset_start + _snap_to_grid(
            snapped_start_s, anchor.source_frame_duration, "nearest"
        )
        clip.set("start", format_time(absolute_start))
        clip.set("duration", duration_text)
        sequence.set("duration", duration_text)

    if clip.get("offset") is not None:
        clip.set("offset", "0s")

    add_vclip_metadata(clip, metadata)
    return project


def clean_project_base(name: str) -> str:
    out = CLIP_NUMBER_RE.sub("", name).strip()
    out = re.sub(r"\s+—\s+Graded(?:\s+\d+)?$", "", out, flags=re.I)
    out = re.sub(r"\s+—\s+Mixed$", "", out, flags=re.I)
    return out.strip() or "Drone Shot"


def derive_scope_prefix(anchors: Sequence[Anchor]) -> str:
    if not anchors:
        return "Telemetry Reconstruction"
    event = anchors[0].event_name
    parts = [p.strip() for p in event.split(" — ") if p.strip()]
    if len(parts) >= 2 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]):
        return " — ".join(parts[:2])
    return parts[0] if parts else "Telemetry Reconstruction"


def score_anchor(
    anchor: Anchor,
    shard: Path,
    probe: tqc.MediaProbe,
    header: tqc.FlightHeader,
    method: str,
    creation: datetime,
    flight: tqc.FlightSamples,
    thresholds: tqc.Thresholds,
) -> tqc.ClipScore:
    clip = to_clip_appearance(anchor, shard)
    clip.media_path = anchor.media_path
    clip.media_resolution = anchor.media_resolution
    clip.camera_scope = anchor.scope
    clip.camera_family = anchor.camera_family
    return tqc.score_from_samples(clip, probe, header, method, creation, flight, thresholds)


def adjusted_ready_ranges(anchor: Anchor, score: tqc.ClipScore, min_duration_s: float) -> list[tuple[float, float, str]]:
    """Return source start,duration,variant label for safe automatic ready-cut variants."""
    if score.status == "PASS" or score.status == "NO_TELEMETRY":
        return [(anchor.source_start_s, anchor.duration_s, "ready")]
    action = score.suggested_action
    if action == "trim_end" and score.suggested_trim_end_s is not None:
        duration = score.suggested_trim_end_s
        if duration >= min_duration_s:
            return [(anchor.source_start_s, duration, "trim-end")]
    if action == "trim_start" and score.suggested_trim_start_s is not None:
        start = anchor.source_start_s + score.suggested_trim_start_s
        duration = anchor.duration_s - score.suggested_trim_start_s
        if duration >= min_duration_s:
            return [(start, duration, "trim-start")]
    if action == "split_or_review":
        rows: list[tuple[float, float, str]] = []
        if score.suggested_trim_end_s is not None and score.suggested_trim_end_s >= min_duration_s:
            rows.append((anchor.source_start_s, score.suggested_trim_end_s, "split-A"))
        if score.suggested_trim_start_s is not None:
            duration = anchor.duration_s - score.suggested_trim_start_s
            if duration >= min_duration_s:
                rows.append((anchor.source_start_s + score.suggested_trim_start_s, duration, "split-B"))
        if rows:
            return rows
    return []


def candidate_gps(srt_info: Any, start_s: float, duration_s: float) -> dict[str, Any] | None:
    if srt_info is None:
        return None
    try:
        return extract_gps_summary(
            srt_info,
            start=sec_fraction(start_s),
            duration=sec_fraction(duration_s),
            allow_full_sidecar_fallback=False,
        )
    except Exception:
        return None




def historical_original_project(anchor: Anchor) -> ET.Element:
    """Exact-timing copy of one legacy human-edited VClip for side-by-side audit."""
    name = anchor.project_name
    meta = {
        "com.vclip.stock_clip_id": anchor.stock_clip_id,
        "com.vclip.telemetry.parent_ids": anchor.stock_clip_id,
        "com.vclip.telemetry.variant": "historical_original",
        "com.vclip.telemetry.reconstruction_version": "3",
    }
    return rewrite_project(
        anchor,
        new_id=anchor.stock_clip_id,
        new_name=name,
        start_s=anchor.source_start_s,
        duration_s=anchor.duration_s,
        metadata=meta,
        preserve_original_timing=True,
    )


def build_output_tree(
    input_tree: ET.ElementTree,
    *,
    original_projects: list[tuple[str, ET.Element]],
    ready_projects: list[tuple[str, ET.Element]],
    master_projects: list[tuple[str, ET.Element]],
    repair_projects: list[tuple[str, ET.Element]],
    review_projects: list[tuple[str, ET.Element]],
    scope_prefix: str,
) -> ET.Element:
    root = copy.deepcopy(input_tree.getroot())
    library = next((node for node in root.iter() if local_name(node.tag) == "library"), None)
    if library is None:
        raise RuntimeError("Input shard has no <library>.")
    for child in list(library):
        if local_name(child.tag) == "event":
            library.remove(child)

    buckets = [
        (f"{scope_prefix} — Historical Originals — Reconstruction v3", original_projects),
        (f"{scope_prefix} — Ready Cuts — Reconstruction v3", ready_projects),
        (f"{scope_prefix} — Extended Masters — Reconstruction v3", master_projects),
        (f"{scope_prefix} — Repair Candidates — Reconstruction v3", repair_projects),
        (f"{scope_prefix} — QC Review — Reconstruction v3", review_projects),
    ]
    for event_name, projects in buckets:
        if not projects:
            continue
        event = ET.SubElement(
            library,
            "event",
            {
                "name": event_name,
                "uid": stable_uid("reconstruction-v3-event", event_name),
            },
        )
        for _name, project in projects:
            event.append(project)
    return root


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in keys})


def run(args: argparse.Namespace) -> int:
    input_path = args.input.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    report_path = args.report.expanduser().resolve()
    cache_dir = args.cache_dir.expanduser().resolve()
    flight_root = args.flight_record_root.expanduser().resolve()
    media_roots = [Path(p).expanduser().resolve() for p in args.media_root]
    cache_dir.mkdir(parents=True, exist_ok=True)

    print("Reading review shard...")
    input_tree, anchors, resource_index = parse_input_shard(input_path)
    if not anchors:
        raise RuntimeError("No VClip projects found in input shard.")

    print(f"  physical anchors: {len(anchors)}")
    resolve_media(anchors, input_path, media_roots)
    non_drone = [a for a in anchors if a.scope == SCOPE_OUT_OF_SCOPE_NON_DRONE]
    anchors = [a for a in anchors if a.scope != SCOPE_OUT_OF_SCOPE_NON_DRONE]
    print(f"  known non-drone excluded: {len(non_drone)}")
    print(f"  drone/unknown-camera anchors: {len(anchors)}")

    print("Indexing SRT sidecars...")
    sidecar_index, sidecar_diag = build_srt_context(resource_index, media_roots)
    srt_cache: dict[str, Any] = {}
    srt_meta_by_ref: dict[str, dict[str, Any]] = {}
    for ref, asset in resource_index.items():
        if local_name(asset.tag) != "asset":
            continue
        match = sidecar_match_for_asset(asset, sidecar_index)
        meta: dict[str, Any] = {
            "path": str(match.path) if match.path else None,
            "method": match.method,
            "confidence": match.confidence,
        }
        if match.path:
            key = str(match.path)
            try:
                info = srt_cache.get(key)
                if info is None:
                    info = parse_srt_info(match.path)
                    srt_cache[key] = info
                meta.update(
                    {
                        "sample_count": info.sample_count,
                        "has_position": info.has_position,
                        "has_altitude": info.has_altitude,
                        "has_orientation": getattr(info, "has_orientation", False),
                        "color_md": extract_srt_color_md(match.path),
                    }
                )
            except Exception as exc:
                meta["error"] = f"{type(exc).__name__}:{exc}"
        srt_meta_by_ref[ref] = meta

    print("Probing source media...")
    probe_cache_path = cache_dir / "media-probe-cache.json"
    probe_cache = tqc.load_json_file(probe_cache_path, {})
    probes: dict[str, tqc.MediaProbe] = {}
    for media in sorted({a.media_path for a in anchors if a.media_path}):
        probes[media] = tqc.ffprobe_media(Path(media), args.ffprobe, probe_cache)
    tqc.atomic_write_json(probe_cache_path, probe_cache)

    print("Indexing flight records...")
    flight_paths, flight_inventory = tqc.enumerate_flight_logs(flight_root)
    headers, header_cache_stats = tqc.read_flight_headers(
        flight_paths,
        cache_dir / "flight-header-cache.json",
    )
    usable_headers = [h for h in headers if h.start_dt is not None and h.total_time_s]
    print(f"  usable flight headers: {len(usable_headers)}")

    thresholds = tqc.Thresholds(min_clean_seconds=args.min_duration)
    source_groups = unique_source_groups(anchors)
    source_contexts: dict[str, SourceContext] = {}
    source_flights: dict[str, tuple[tqc.MediaProbe, tqc.FlightHeader, str, datetime, tqc.FlightSamples, list[SourceSample]]] = {}
    flight_decode_cache: dict[str, tqc.FlightSamples] = {}
    api_key = os.environ.get(args.api_key_env)

    print(f"Matching telemetry for {len(source_groups)} source video(s)...")
    for source_key, group in sorted(source_groups.items()):
        representative = group[0]
        media_path = representative.media_path
        srt_meta = srt_meta_by_ref.get(representative.source_ref, {})
        ctx = SourceContext(
            source_name=representative.source_name,
            media_path=media_path,
            media_creation_time=None,
            media_duration_s=None,
            media_resolution=representative.media_resolution,
            flight_log=None,
            flight_version=None,
            aircraft_name=None,
            alignment_method=None,
            srt_path=srt_meta.get("path"),
            srt_method=srt_meta.get("method"),
            srt_confidence=srt_meta.get("confidence"),
            srt_sample_count=int(srt_meta.get("sample_count") or 0),
            srt_has_position=bool(srt_meta.get("has_position")),
            srt_has_altitude=bool(srt_meta.get("has_altitude")),
            srt_has_orientation=bool(srt_meta.get("has_orientation")),
            srt_color_md=srt_meta.get("color_md"),
        )
        source_contexts[source_key] = ctx
        if not media_path:
            ctx.error = "missing_source_media"
            continue
        probe = probes.get(media_path)
        if probe is None or probe.error or probe.creation_dt is None or not probe.duration_s:
            ctx.error = probe.error if probe else "missing_media_probe"
            continue
        ctx.media_creation_time = probe.creation_time
        ctx.media_duration_s = probe.duration_s
        appearance = to_clip_appearance(representative, input_path)
        appearance.media_path = media_path
        candidates = tqc.candidate_flights(probe, appearance, usable_headers)
        if not candidates:
            ctx.error = "no_matching_flight_log"
            continue
        _, header, method, creation = candidates[0]
        ctx.flight_log = header.path
        ctx.flight_version = header.version
        ctx.aircraft_name = header.aircraft_name
        ctx.alignment_method = method
        try:
            flight = flight_decode_cache.get(header.path)
            if flight is None:
                flight = tqc.decode_flight(header, api_key)
                flight_decode_cache[header.path] = flight
            samples = source_relative_samples(flight, creation, probe.duration_s)
            if len(samples) < 2:
                ctx.error = "no_source_frame_overlap"
                continue
            source_flights[source_key] = (probe, header, method, creation, flight, samples)
        except Exception as exc:
            ctx.error = f"flight_decode_failed:{type(exc).__name__}:{exc}"

    print("Scoring cuts and reconstructing shots (telemetry + visual coherence v3)...")
    original_projects: list[tuple[str, ET.Element]] = []
    ready_projects: list[tuple[str, ET.Element]] = []
    repair_projects: list[tuple[str, ET.Element]] = []
    review_projects: list[tuple[str, ET.Element]] = []
    master_projects: list[tuple[str, ET.Element]] = []
    ready_rows: list[dict[str, Any]] = []
    shot_rows: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    scope_prefix = derive_scope_prefix(anchors)
    motion_thresholds = OperatorMotionThresholds()
    visual_settings = VisualSettings(
        fps=args.visual_fps,
        width=args.visual_width,
    )
    visual_cache: dict[tuple[str, float, float], VisualAssessment] = {}

    def assess_visual(
        anchor: Anchor,
        start_s: float,
        duration_s: float,
        samples: Sequence[SourceSample],
    ) -> VisualAssessment:
        source_key = tqc.normalize_source_stem(anchor.source_name) or anchor.source_name.casefold()
        key = (
            source_key,
            round(float(start_s), 4),
            round(float(duration_s), 4),
        )
        cached = visual_cache.get(key)
        if cached is not None:
            return cached

        if not anchor.media_path:
            result = VisualAssessment(
                "NO_VISUAL",
                ["missing_source_media"],
                {},
            )
        else:
            media = Path(anchor.media_path)
            if not media.is_file():
                result = VisualAssessment(
                    "NO_VISUAL",
                    ["missing_source_media"],
                    {"media_path": str(media)},
                )
            else:
                result = assess_visual_coherence(
                    media=media,
                    start_s=float(start_s),
                    duration_s=float(duration_s),
                    source_anchors=source_groups.get(source_key, []),
                    telemetry_samples=samples,
                    cache_root=args.visual_cache_dir,
                    vision_helper=args.visual_helper,
                    ffmpeg=args.ffmpeg,
                    min_duration_s=args.min_duration,
                    settings=visual_settings,
                )
        visual_cache[key] = result
        return result

    # Preserve every historical cut in the new shard for direct A/B inspection.
    for anchor in sorted(anchors, key=lambda a: (a.project_name, a.stock_clip_id)):
        original_projects.append((anchor.project_name, historical_original_project(anchor)))

    # Existing ready cuts: preserve clean historical selections. Generated
    # repairs are never promoted until the GENERATED range is re-scored.
    scores_by_id: dict[str, tqc.ClipScore] = {}
    for anchor in sorted(anchors, key=lambda a: (a.project_name, a.stock_clip_id)):
        source_key = tqc.normalize_source_stem(anchor.source_name) or anchor.source_name.casefold()
        telemetry = source_flights.get(source_key)
        ctx = source_contexts[source_key]
        source_samples: list[SourceSample] = []

        if telemetry is None:
            score = tqc.score_without_telemetry(
                to_clip_appearance(anchor, input_path),
                "NO_TELEMETRY",
                ctx.error or "no_flight_telemetry",
                probes.get(anchor.media_path or ""),
            )
            original_motion = OperatorMotionAssessment(
                "NO_TELEMETRY", [ctx.error or "no_flight_telemetry"], {}
            )
        else:
            probe, header, method, creation, flight, source_samples = telemetry
            score = score_anchor(anchor, input_path, probe, header, method, creation, flight, thresholds)
            original_motion = operator_motion_assessment(
                source_samples,
                anchor.source_start_s,
                anchor.duration_s,
                motion_thresholds,
            )
        original_visual = assess_visual(
            anchor,
            anchor.source_start_s,
            anchor.duration_s,
            source_samples,
        )
        scores_by_id[anchor.stock_clip_id] = score

        # v3: historical selections are human evidence that "something useful
        # is here", but the entire old interval is not automatically ship-ready.
        historical_ready, readiness_basis = historical_ready_pass(
            score,
            original_motion,
            original_visual,
        )
        if historical_ready:
            meta = {
                "com.vclip.stock_clip_id": anchor.stock_clip_id,
                "com.vclip.telemetry.parent_ids": anchor.stock_clip_id,
                "com.vclip.telemetry.variant": "ready_cut",
                "com.vclip.telemetry.reconstruction_version": "3",
                "com.vclip.telemetry.qc_status": score.status,
                "com.vclip.telemetry.qc_reasons": ",".join(score.reasons),
                "com.vclip.telemetry.operator_status": original_motion.status,
                "com.vclip.telemetry.operator_reasons": ",".join(original_motion.reasons),
                "com.vclip.visual.status": original_visual.status,
                "com.vclip.visual.reasons": ",".join(original_visual.reasons),
                "com.vclip.visual.suggested_action": original_visual.suggested_action or "",
                "com.vclip.visual.suggested_boundary_s": (
                    f"{original_visual.suggested_boundary_s:.6f}"
                    if original_visual.suggested_boundary_s is not None
                    else ""
                ),
                "com.vclip.readiness_basis": readiness_basis,
                "com.vclip.telemetry.source_start_s": f"{anchor.source_start_s:.6f}",
                "com.vclip.telemetry.duration_s": f"{anchor.duration_s:.6f}",
            }
            project = rewrite_project(
                anchor,
                new_id=anchor.stock_clip_id,
                new_name=anchor.project_name,
                start_s=anchor.source_start_s,
                duration_s=anchor.duration_s,
                metadata=meta,
                preserve_original_timing=True,
            )
            ready_projects.append((anchor.project_name, project))
            ready_rows.append({
                "bucket": "ready",
                "stock_clip_id": anchor.stock_clip_id,
                "parent_id": anchor.stock_clip_id,
                "project_name": anchor.project_name,
                "source_name": anchor.source_name,
                "start_s": round(anchor.source_start_s, 6),
                "duration_s": round(anchor.duration_s, 6),
                "qc_status": score.status,
                "qc_reasons": ",".join(score.reasons),
                "operator_status": original_motion.status,
                "operator_reasons": ",".join(original_motion.reasons),
                "operator_metrics": json.dumps(original_motion.metrics, sort_keys=True),
                "visual_status": original_visual.status,
                "visual_reasons": ",".join(original_visual.reasons),
                "visual_metrics": json.dumps(original_visual.metrics, sort_keys=True),
                "visual_suggested_action": original_visual.suggested_action or "",
                "visual_suggested_boundary_s": (
                    round(original_visual.suggested_boundary_s, 6)
                    if original_visual.suggested_boundary_s is not None
                    else ""
                ),
                "readiness_basis": readiness_basis,
                "action": "historical-ready",
                "flight_log": ctx.flight_log,
                "srt_path": ctx.srt_path,
            })
            continue

        # Problematic original goes to QC Review, while any suggested repairs are
        # generated as candidates and then re-scored independently.
        review_name = f"QC ORIGINAL — {anchor.project_name}"
        review_meta = {
            "com.vclip.stock_clip_id": anchor.stock_clip_id,
            "com.vclip.telemetry.parent_ids": anchor.stock_clip_id,
            "com.vclip.telemetry.variant": "qc_review",
            "com.vclip.telemetry.reconstruction_version": "3",
            "com.vclip.telemetry.qc_status": score.status,
            "com.vclip.telemetry.qc_reasons": ",".join(score.reasons),
            "com.vclip.telemetry.operator_status": original_motion.status,
            "com.vclip.telemetry.operator_reasons": ",".join(original_motion.reasons),
            "com.vclip.visual.status": original_visual.status,
            "com.vclip.visual.reasons": ",".join(original_visual.reasons),
            "com.vclip.visual.suggested_action": original_visual.suggested_action or "",
            "com.vclip.visual.suggested_boundary_s": (
                f"{original_visual.suggested_boundary_s:.6f}"
                if original_visual.suggested_boundary_s is not None
                else ""
            ),
            "com.vclip.readiness_basis": readiness_basis,
            "com.vclip.telemetry.suggested_action": (
                original_visual.suggested_action
                or score.suggested_action
                or "manual_review"
            ),
        }
        review_projects.append((
            review_name,
            rewrite_project(
                anchor,
                new_id=anchor.stock_clip_id,
                new_name=review_name,
                start_s=anchor.source_start_s,
                duration_s=anchor.duration_s,
                metadata=review_meta,
                preserve_original_timing=True,
            ),
        ))
        ready_rows.append({
            "bucket": "qc_original",
            "stock_clip_id": anchor.stock_clip_id,
            "parent_id": anchor.stock_clip_id,
            "project_name": review_name,
            "source_name": anchor.source_name,
            "start_s": round(anchor.source_start_s, 6),
            "duration_s": round(anchor.duration_s, 6),
            "qc_status": score.status,
            "qc_reasons": ",".join(score.reasons),
            "operator_status": original_motion.status,
            "operator_reasons": ",".join(original_motion.reasons),
            "operator_metrics": json.dumps(original_motion.metrics, sort_keys=True),
            "visual_status": original_visual.status,
            "visual_reasons": ",".join(original_visual.reasons),
            "visual_metrics": json.dumps(original_visual.metrics, sort_keys=True),
            "visual_suggested_action": original_visual.suggested_action or "",
            "visual_suggested_boundary_s": (
                round(original_visual.suggested_boundary_s, 6)
                if original_visual.suggested_boundary_s is not None
                else ""
            ),
            "readiness_basis": readiness_basis,
            "action": (
                original_visual.suggested_action
                or score.suggested_action
                or "manual_review"
            ),
            "flight_log": ctx.flight_log,
            "srt_path": ctx.srt_path,
        })

        variants: list[tuple[float, float, str]] = []
        if score.status not in {"PASS", "NO_TELEMETRY"}:
            variants.extend(adjusted_ready_ranges(anchor, score, args.min_duration))
        variants.extend(
            visual_trim_ranges(
                original_visual,
                start_s=anchor.source_start_s,
                duration_s=anchor.duration_s,
                min_duration_s=args.min_duration,
            )
        )
        # Deduplicate equivalent repair windows from telemetry and visual analysis.
        unique_variants: list[tuple[float, float, str]] = []
        seen_ranges: set[tuple[int, int]] = set()
        for candidate_start, candidate_duration, candidate_variant in variants:
            range_key = (
                round(candidate_start * 100),
                round(candidate_duration * 100),
            )
            if range_key in seen_ranges:
                continue
            seen_ranges.add(range_key)
            unique_variants.append(
                (candidate_start, candidate_duration, candidate_variant)
            )

        for index, (start_s, duration_s, variant) in enumerate(unique_variants, 1):
            start_s, duration_s, _start_text, _duration_text = quantize_generated_range(
                anchor, start_s, duration_s
            )
            candidate_id = stock_clip_id(
                "reconstruction-repair-v3",
                anchor.stock_clip_id,
                anchor.source_name,
                fmt(start_s),
                fmt(duration_s),
                variant,
            )

            if telemetry is None:
                candidate_score = score
                candidate_motion = OperatorMotionAssessment(
                    "NO_TELEMETRY", ["no_flight_telemetry"], {}
                )
            else:
                probe, header, method, creation, flight, source_samples = telemetry
                synthetic = to_clip_appearance(anchor, input_path)
                synthetic.stock_clip_id = candidate_id
                synthetic.source_start_s = start_s
                synthetic.duration_s = duration_s
                synthetic.media_path = anchor.media_path
                synthetic.media_resolution = anchor.media_resolution
                synthetic.camera_scope = anchor.scope
                synthetic.camera_family = anchor.camera_family
                candidate_score = tqc.score_from_samples(
                    synthetic, probe, header, method, creation, flight, thresholds
                )
                candidate_motion = operator_motion_assessment(
                    source_samples, start_s, duration_s, motion_thresholds
                )

            candidate_visual = assess_visual(
                anchor,
                start_s,
                duration_s,
                source_samples,
            )
            promoted, readiness_basis = generated_ready_pass(
                candidate_score,
                candidate_motion,
                candidate_visual,
            )
            suffix = f" — {variant}"
            project_name = f"{anchor.project_name}{suffix}"
            bucket = "ready" if promoted else "repair_candidate"
            telemetry_variant = "ready_cut" if promoted else "repair_candidate"
            meta = {
                "com.vclip.stock_clip_id": candidate_id,
                "com.vclip.telemetry.parent_ids": anchor.stock_clip_id,
                "com.vclip.telemetry.variant": telemetry_variant,
                "com.vclip.telemetry.reconstruction_version": "3",
                "com.vclip.telemetry.qc_status": candidate_score.status,
                "com.vclip.telemetry.qc_reasons": ",".join(candidate_score.reasons),
                "com.vclip.telemetry.operator_status": candidate_motion.status,
                "com.vclip.telemetry.operator_reasons": ",".join(candidate_motion.reasons),
                "com.vclip.visual.status": candidate_visual.status,
                "com.vclip.visual.reasons": ",".join(candidate_visual.reasons),
                "com.vclip.visual.suggested_action": candidate_visual.suggested_action or "",
                "com.vclip.visual.suggested_boundary_s": (
                    f"{candidate_visual.suggested_boundary_s:.6f}"
                    if candidate_visual.suggested_boundary_s is not None
                    else ""
                ),
                "com.vclip.readiness_basis": readiness_basis,
                "com.vclip.telemetry.source_start_s": f"{start_s:.6f}",
                "com.vclip.telemetry.duration_s": f"{duration_s:.6f}",
                "com.vclip.telemetry.repair_action": variant,
            }
            project = rewrite_project(
                anchor,
                new_id=candidate_id,
                new_name=project_name,
                start_s=start_s,
                duration_s=duration_s,
                metadata=meta,
            )
            target = ready_projects if promoted else repair_projects
            target.append((project_name, project))
            ready_rows.append({
                "bucket": bucket,
                "stock_clip_id": candidate_id,
                "parent_id": anchor.stock_clip_id,
                "project_name": project_name,
                "source_name": anchor.source_name,
                "start_s": round(start_s, 6),
                "duration_s": round(duration_s, 6),
                "qc_status": candidate_score.status,
                "qc_reasons": ",".join(candidate_score.reasons),
                "operator_status": candidate_motion.status,
                "operator_reasons": ",".join(candidate_motion.reasons),
                "operator_metrics": json.dumps(candidate_motion.metrics, sort_keys=True),
                "visual_status": candidate_visual.status,
                "visual_reasons": ",".join(candidate_visual.reasons),
                "visual_metrics": json.dumps(candidate_visual.metrics, sort_keys=True),
                "visual_suggested_action": candidate_visual.suggested_action or "",
                "visual_suggested_boundary_s": (
                    round(candidate_visual.suggested_boundary_s, 6)
                    if candidate_visual.suggested_boundary_s is not None
                    else ""
                ),
                "readiness_basis": readiness_basis,
                "action": variant,
                "flight_log": ctx.flight_log,
                "srt_path": ctx.srt_path,
            })

    # Reconstructed masters: source-level overlapping editorial anchors plus
    # transition-free telemetry boundaries.
    master_counter = 0
    for source_key, group in sorted(source_groups.items()):
        ctx = source_contexts[source_key]
        telemetry = source_flights.get(source_key)
        source_rows.append(asdict(ctx))
        if telemetry is None or not ctx.media_duration_s:
            continue
        probe, header, method, creation, flight, samples = telemetry
        regimes = transition_free_intervals(
            samples,
            ctx.media_duration_s,
            thresholds,
            motion_thresholds,
            pad_s=args.transition_pad,
            min_interval_s=args.min_duration,
        )
        proposals = reconstruct_windows(
            group,
            regimes,
            min_duration_s=args.min_duration,
            target_duration_s=args.target_duration,
            max_duration_s=args.max_duration,
            max_extension_each_side_s=args.max_extension_each_side,
        )
        for start, end, parents, regime in proposals:
            duration = end - start
            if duration < args.min_duration:
                continue
            master_counter += 1
            parent_ids = sorted({a.stock_clip_id for a in parents})
            status_rank = {"PASS": 3, "SOFT_REVIEW": 2, "REVIEW": 1, "NO_TELEMETRY": 0}

            def template_rank(anchor: Anchor) -> tuple[int, float, float]:
                score = scores_by_id.get(anchor.stock_clip_id)
                return (
                    status_rank.get(score.status if score is not None else "NO_TELEMETRY", 0),
                    overlap(anchor.source_start_s, anchor.end_s, start, end),
                    anchor.duration_s,
                )

            template = max(parents, key=template_rank)
            start, duration, _start_text, _duration_text = quantize_generated_range(
                template, start, duration
            )
            end = start + duration
            if duration < args.min_duration:
                continue
            touched, support = editorial_support(parents, start, end)
            new_id = stock_clip_id(
                "reconstruction-master-v3",
                template.source_name,
                fmt(start),
                fmt(duration),
                *parent_ids,
            )
            synthetic = to_clip_appearance(template, input_path)
            synthetic.stock_clip_id = new_id
            synthetic.source_start_s = start
            synthetic.duration_s = duration
            synthetic.media_path = template.media_path
            synthetic.media_resolution = template.media_resolution
            synthetic.camera_scope = template.scope
            synthetic.camera_family = template.camera_family
            shot_score = tqc.score_from_samples(
                synthetic,
                probe,
                header,
                method,
                creation,
                flight,
                thresholds,
            )
            master_motion = operator_motion_assessment(
                samples, start, duration, motion_thresholds
            )
            master_visual = assess_visual(
                template,
                start,
                duration,
                samples,
            )
            base = clean_project_base(template.project_name)
            project_name = f"{base} — Extended Master {master_counter:02d} — {duration:.1f}s"

            srt_meta = srt_meta_by_ref.get(template.source_ref, {})
            srt_info = srt_cache.get(str(srt_meta.get("path"))) if srt_meta.get("path") else None
            gps = candidate_gps(srt_info, start, duration)
            meta = {
                "com.vclip.stock_clip_id": new_id,
                "com.vclip.telemetry.parent_ids": ",".join(parent_ids),
                "com.vclip.telemetry.variant": "extended_master",
                "com.vclip.telemetry.reconstruction_version": "3",
                "com.vclip.telemetry.qc_status": shot_score.status,
                "com.vclip.telemetry.qc_reasons": ",".join(shot_score.reasons),
                "com.vclip.telemetry.operator_status": master_motion.status,
                "com.vclip.telemetry.operator_reasons": ",".join(master_motion.reasons),
                "com.vclip.visual.status": master_visual.status,
                "com.vclip.visual.reasons": ",".join(master_visual.reasons),
                "com.vclip.visual.suggested_action": master_visual.suggested_action or "",
                "com.vclip.visual.suggested_boundary_s": (
                    f"{master_visual.suggested_boundary_s:.6f}"
                    if master_visual.suggested_boundary_s is not None
                    else ""
                ),
                "com.vclip.telemetry.source_start_s": f"{start:.6f}",
                "com.vclip.telemetry.duration_s": f"{duration:.6f}",
                "com.vclip.telemetry.flight_log": ctx.flight_log or "",
                "com.vclip.telemetry.srt_path": ctx.srt_path or "",
                "com.vclip.telemetry.editorial_anchor_count": str(touched),
                "com.vclip.telemetry.editorial_support_seconds": f"{support:.6f}",
                "com.vclip.telemetry.transition_interval": f"{regime[0]:.6f}-{regime[1]:.6f}",
            }
            project = rewrite_project(
                template,
                new_id=new_id,
                new_name=project_name,
                start_s=start,
                duration_s=duration,
                metadata=meta,
            )
            master_promoted, readiness_basis = generated_ready_pass(
                shot_score,
                master_motion,
                master_visual,
            )
            bucket = master_projects if master_promoted else review_projects
            if not master_promoted:
                project.set("name", f"MASTER REVIEW — {project_name}")
                project_name = project.get("name") or project_name
            bucket.append((project_name, project))
            shot = ReconstructedShot(
                stock_clip_id=new_id,
                source_name=template.source_name,
                start_s=start,
                duration_s=duration,
                end_s=end,
                parent_ids=parent_ids,
                parent_projects=sorted({a.project_name for a in parents}),
                template_parent_id=template.stock_clip_id,
                project_name=project_name,
                event_name=(
                    f"{scope_prefix} — Extended Masters — Reconstruction v3"
                    if master_promoted
                    else f"{scope_prefix} — QC Review — Reconstruction v3"
                ),
                telemetry_status=shot_score.status,
                telemetry_reasons=shot_score.reasons,
                editorial_anchor_count=touched,
                editorial_support_seconds=support,
                flight_log=ctx.flight_log,
                srt_path=ctx.srt_path,
                srt_gps=gps,
                transition_free_interval=regime,
            )
            shot_payload = asdict(shot)
            shot_payload["promoted"] = master_promoted
            shot_payload["operator_status"] = master_motion.status
            shot_payload["operator_reasons"] = master_motion.reasons
            shot_payload["operator_metrics"] = master_motion.metrics
            shot_payload["visual_status"] = master_visual.status
            shot_payload["visual_reasons"] = master_visual.reasons
            shot_payload["visual_metrics"] = master_visual.metrics
            shot_payload["visual_suggested_action"] = master_visual.suggested_action
            shot_payload["visual_suggested_boundary_s"] = master_visual.suggested_boundary_s
            shot_payload["readiness_basis"] = readiness_basis
            shot_rows.append(shot_payload)

    output_root = build_output_tree(
        input_tree,
        original_projects=original_projects,
        ready_projects=ready_projects,
        master_projects=master_projects,
        repair_projects=repair_projects,
        review_projects=review_projects,
        scope_prefix=scope_prefix,
    )
    boundary_errors = edit_frame_boundary_errors(output_root)
    if boundary_errors:
        raise RuntimeError(
            "Generated reconstructed FCPXML failed edit-frame boundary audit:\n"
            + "\n".join(boundary_errors[:40])
        )
    print("  edit-frame boundary audit: PASS")

    validation = validate_fcpxml(output_root)
    if not validation.passed:
        raise RuntimeError(
            "Generated reconstructed FCPXML failed VClip validation:\n" + "\n".join(validation.errors[:20])
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_fcpxml(output_root, output_path)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_fcpxml": str(input_path),
        "output_fcpxml": str(output_path),
        "scope_prefix": scope_prefix,
        "anchors_total": len(anchors),
        "known_non_drone_excluded": len(non_drone),
        "source_video_count": len(source_groups),
        "source_with_flight_telemetry": len(source_flights),
        "historical_original_count": len(original_projects),
        "ready_project_count": len(ready_projects),
        "extended_master_count": len(master_projects),
        "repair_candidate_count": len(repair_projects),
        "qc_review_project_count": len(review_projects),
        "flight_inventory": flight_inventory,
        "flight_header_cache": header_cache_stats,
        "sidecars": sidecar_diag,
        "thresholds": asdict(thresholds),
        "operator_motion_thresholds": asdict(motion_thresholds),
        "visual_settings": asdict(visual_settings),
        "visual_status_counts": dict(
            Counter(
                str(row.get("visual_status") or "UNKNOWN")
                for row in ready_rows
            )
        ),
        "settings": {
            "min_duration": args.min_duration,
            "target_duration": args.target_duration,
            "max_duration": args.max_duration,
            "max_extension_each_side": args.max_extension_each_side,
            "transition_pad": args.transition_pad,
        },
        "sources": source_rows,
        "ready_variants": ready_rows,
        "reconstructed_shots": shot_rows,
        "validation": asdict(validation),
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    write_csv(report_path.with_suffix(".ready.csv"), ready_rows)
    write_csv(report_path.with_suffix(".masters.csv"), shot_rows)

    print()
    print("RECONSTRUCTION COMPLETE")
    print("=======================")
    print(f"Input VClip anchors:          {len(anchors):4d}")
    print(f"Source videos:                {len(source_groups):4d}")
    print(f"Sources with flight telemetry:{len(source_flights):4d}")
    print(f"Historical originals:         {len(original_projects):4d}")
    print(f"Ready-cut projects:           {len(ready_projects):4d}")
    print(f"Extended masters:             {len(master_projects):4d}")
    print(f"Repair candidates:            {len(repair_projects):4d}")
    print(f"QC review projects:           {len(review_projects):4d}")
    print(f"Output XML: {output_path}")
    print(f"Report:     {report_path}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Reconstruct telemetry + on-device-visual-coherence ready cuts and masters from one review shard."
    )
    p.add_argument("--input", required=True, type=Path, help="Existing physical review-shard FCPXML.")
    p.add_argument("--output", required=True, type=Path, help="New reconstructed FCPXML.")
    p.add_argument("--report", required=True, type=Path, help="JSON reconstruction report.")
    p.add_argument("--flight-record-root", required=True, type=Path)
    p.add_argument("--media-root", action="append", default=[], help="Media/SRT archive root; repeatable.")
    p.add_argument("--cache-dir", required=True, type=Path, help="Reuse telemetry-QC flight/media caches.")
    p.add_argument("--ffprobe", default="ffprobe")
    p.add_argument("--ffmpeg", default="ffmpeg")
    p.add_argument(
        "--visual-helper",
        required=True,
        type=Path,
        help="Compiled macOS Apple Vision feature-print helper.",
    )
    p.add_argument(
        "--visual-cache-dir",
        required=True,
        type=Path,
        help="Cache for low-res source frames and Vision distance matrices.",
    )
    p.add_argument("--visual-fps", type=float, default=2.0)
    p.add_argument("--visual-width", type=int, default=320)
    p.add_argument("--api-key-env", default="DJI_API_KEY")
    p.add_argument("--min-duration", type=float, default=5.0)
    p.add_argument("--target-duration", type=float, default=12.0)
    p.add_argument("--max-duration", type=float, default=20.0)
    p.add_argument("--max-extension-each-side", type=float, default=5.0)
    p.add_argument("--transition-pad", type=float, default=0.50)
    return p


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
