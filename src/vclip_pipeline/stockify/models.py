"""Data objects passed between Stockify's parsing, scoring, and reporting layers."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path


class StockifyError(RuntimeError):
    """Raised when a conversion cannot continue without risking bad output."""


@dataclass
class SidecarSummary:
    roots: list[str] = field(default_factory=list)
    candidate_asset_stems: int = 0
    srt_files_scanned: int = 0
    matched_asset_stems: int = 0
    ambiguous_asset_stems: int = 0
    scan_errors: list[str] = field(default_factory=list)


@dataclass
class CandidateSummary:
    short_segments_seen: int = 0
    short_segments_expanded: int = 0
    short_segments_unexpanded: int = 0
    segments_with_srt: int = 0
    segments_missing_srt: int = 0
    srt_clean_windows: int = 0
    srt_review_windows: int = 0
    srt_reject_windows: int = 0
    visual_clean_windows: int = 0
    visual_review_windows: int = 0
    visual_reject_windows: int = 0
    visual_unavailable_windows: int = 0
    visual_blocked_expansions: int = 0


@dataclass
class VisualPreflightReport:
    requested: bool = False
    required_for_expansion: bool = False
    available: bool = False
    ffmpeg_path: str | None = None
    numpy_available: bool | None = None
    blockers: list[str] = field(default_factory=list)


@dataclass
class ColorPolicyReport:
    require_camera_lut: bool = False
    require_custom_lut: bool = False
    evaluated_segments: int = 0
    evaluated_with_camera_lut: int = 0
    evaluated_without_camera_lut: int = 0
    evaluated_with_custom_lut: int = 0
    evaluated_without_custom_lut: int = 0
    written_with_camera_lut: int = 0
    written_without_camera_lut: int = 0
    written_with_custom_lut: int = 0
    written_without_custom_lut: int = 0
    written_with_creative_video_effects: int = 0
    written_without_creative_video_effects: int = 0
    skipped_missing_camera_lut: int = 0
    skipped_missing_custom_lut: int = 0
    creative_video_effects: dict[str, int] = field(default_factory=dict)


@dataclass
class SegmentReport:
    stock_clip_id: str
    source_event: str
    source_project: str
    source_segment_index: int
    output_project: str
    timeline_project: str | None
    timeline_offset: str | None
    project_timecode: str | None
    source_ref: str | None
    source_name: str
    start: str
    duration: str
    output_start: str
    output_duration: str
    had_time_map: bool
    retime_normalized: bool
    short_clip_recovery: str = "not_applicable"
    original_duration_seconds: float = 0.0
    output_duration_seconds: float = 0.0
    sidecar_path: str | None = None
    srt_match_method: str | None = None
    srt_status: str = "not_checked"
    srt_window_status: str = "not_checked"
    visual_status: str = "not_checked"
    visual_reasons: list[str] = field(default_factory=list)
    visual_metrics: dict[str, float | int | None] = field(default_factory=dict)
    smoothness_reasons: list[str] = field(default_factory=list)
    candidate_tier: str = "unclassified"
    location: dict[str, object] = field(default_factory=dict)
    capture_time: dict[str, object] = field(default_factory=dict)
    time_of_day: dict[str, object] = field(default_factory=dict)
    weather: dict[str, object] = field(default_factory=dict)
    replacement_ref: str | None = None
    creative_video_effects: list[str] = field(default_factory=list)
    asset_conversion_lut: str | None = None
    generated_event: str | None = None
    generated_project_label: str | None = None
    generated_compilation_name: str | None = None
    generated_clip_project_name: str | None = None
    session_id: str | None = None
    source_project_id: str | None = None
    source_media_id: str | None = None
    effect_signature: str | None = None
    expected_export_basename: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class ProjectReport:
    event: str
    project: str
    segments_found: int = 0
    segments_written: int = 0
    segments_skipped: int = 0
    still_segments: int = 0
    classification: str = "unknown"
    skip_reasons: dict[str, int] = field(default_factory=dict)
    compilation_written: bool = False
    generated_event: str | None = None
    generated_project_label: str | None = None
    generated_compilation_name: str | None = None
    session_id: str | None = None
    anchor_segment_index: int | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class InputReport:
    requested_path: str = ""
    resolved_xml_path: str = ""
    fcpxml_version: str = ""


@dataclass
class ResourcesReport:
    source_count: int = 0
    output_count: int = 0
    source_assets: int = 0
    output_assets: int = 0
    assets_with_conversion_lut: int = 0
    source_assets_missing_media_rep: int = 0
    output_assets_missing_media_rep: int = 0
    missing_resource_ids: list[str] = field(default_factory=list)
    duplicate_resource_ids: list[str] = field(default_factory=list)
    dropped_malformed_asset_ids: list[str] = field(default_factory=list)
    replaced_malformed_refs: dict[str, str] = field(default_factory=dict)


@dataclass
class ProjectSummary:
    source_projects: int = 0
    eligible_video_projects: int = 0
    photo_only_projects: int = 0
    empty_projects: int = 0
    unsupported_projects: int = 0


@dataclass
class SegmentSummary:
    found: int = 0
    written: int = 0
    skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)


@dataclass
class ValidationReport:
    passed: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    input_file: str
    output_file: str
    output_library_name: str
    fcpxml_version: str
    source_events: int = 0
    source_projects: int = 0
    assets_in_resources: int = 0
    assets_with_conversion_lut: int = 0
    layout: str = "both"
    export_manifest_file: str | None = None
    export_manifest_groups: int = 0
    export_manifest_clips: int = 0
    timeline_segments_found: int = 0
    stock_projects_written: int = 0
    compilation_projects_written: int = 0
    skipped_segments: int = 0
    stockify_run_id: str | None = None
    database_file: str | None = None
    shoot_sessions_generated: int = 0
    input: InputReport = field(default_factory=InputReport)
    resources: ResourcesReport = field(default_factory=ResourcesReport)
    project_summary: ProjectSummary = field(default_factory=ProjectSummary)
    segment_summary: SegmentSummary = field(default_factory=SegmentSummary)
    sidecars: SidecarSummary = field(default_factory=SidecarSummary)
    candidates: CandidateSummary = field(default_factory=CandidateSummary)
    validation: ValidationReport = field(default_factory=ValidationReport)
    visual_preflight: VisualPreflightReport = field(default_factory=VisualPreflightReport)
    color_policy: ColorPolicyReport = field(default_factory=ColorPolicyReport)
    projects: list[ProjectReport] = field(default_factory=list)
    segments: list[SegmentReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SidecarMatchResult:
    path: Path | None = None
    method: str = "missing"
    confidence: str | None = None
    ambiguous: bool = False
    archive_candidate_count: int = 0


@dataclass(frozen=True)
class SidecarIndex:
    archive_by_stem: dict[str, tuple[Path, ...]]
    summary: SidecarSummary
    by_asset_id: dict[str, SidecarMatchResult] = field(default_factory=dict)


@dataclass(frozen=True)
class SrtSample:
    time: Fraction
    latitude: float | None = None
    longitude: float | None = None
    rel_alt: float | None = None
    captured_at: str | None = None


@dataclass(frozen=True)
class SrtInfo:
    path: Path
    start: Fraction
    end: Fraction
    sample_count: int
    samples: tuple[SrtSample, ...]
    has_position: bool
    has_altitude: bool
    has_orientation: bool


@dataclass(frozen=True)
class SrtWindowScore:
    status: str
    sample_count: int
    coverage: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class VisualMotionScore:
    status: str
    frame_count: int = 0
    max_shift_px: float | None = None
    avg_shift_px: float | None = None
    max_frame_diff: float | None = None
    avg_frame_diff: float | None = None
    spike_time_seconds: float | None = None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecoveryResult:
    output_start: Fraction
    output_duration: Fraction
    status: str
    candidate_tier: str
    sidecar_path: str | None
    srt_status: str
    srt_window_status: str
    visual_status: str = "not_checked"
    visual_reasons: tuple[str, ...] = ()
    visual_metrics: dict[str, float | int | None] = field(default_factory=dict)
    smoothness_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass
class AcceptedCandidate:
    stock_clip_id: str
    segment_index: int
    source_clip: ET.Element
    clean_clip: ET.Element
    asset: ET.Element | None
    segment_report: SegmentReport
