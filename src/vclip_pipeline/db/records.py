"""Write models shared between Stockify and the catalog repository."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SourceEventRecord:
    id: str
    run_id: str
    source_index: int
    source_name: str
    source_uid: str | None


@dataclass(frozen=True)
class ShootSessionRecord:
    id: str
    run_id: str
    session_key: str
    capture_date: str | None
    captured_at_local: str | None
    timezone: str | None
    center_lat: float | None
    center_lon: float | None
    gps_radius_meters: float | None
    country: str | None
    state: str | None
    city: str | None
    neighborhood: str | None
    poi: str | None
    public_label: str | None
    location_confidence: str | None
    time_of_day: str | None
    time_of_day_confidence: str | None
    generated_event_name: str
    generated_base_label: str
    anchor_stock_clip_id: str | None
    weather_status: str
    astronomy_status: str = "not_enriched"
    location: dict[str, Any] = field(default_factory=dict)
    capture: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceProjectRecord:
    id: str
    run_id: str
    source_event_id: str
    source_index: int
    source_name: str
    source_uid: str | None
    classification: str
    session_id: str | None
    anchor_segment_index: int | None
    generated_event_name: str | None
    generated_project_label: str | None
    generated_compilation_name: str | None
    accepted_clip_count: int
    skipped_clip_count: int
    sequence_format: str | None
    tc_format: str | None
    audio_layout: str | None
    audio_rate: str | None
    source_mod_date: str | None = None
    project_family_id: str | None = None
    family_role: str | None = None
    family_selection_reason: str | None = None
    grading_coverage: float | None = None
    timeline_signature_json: str | None = None


@dataclass(frozen=True)
class ProjectFamilyRecord:
    id: str
    run_id: str
    session_id: str | None
    selected_source_project_id: str | None
    member_count: int
    similarity: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceMediaRecord:
    id: str
    run_id: str
    asset_ref: str | None
    asset_name: str | None
    original_filename: str | None
    media_path: str | None
    normalized_stem: str | None
    duration: str | None
    duration_seconds: float | None
    format_id: str | None
    width: int | None
    height: int | None
    fps: int | None
    camera_lut: str | None
    srt_path: str | None
    srt_match_method: str | None
    srt_match_confidence: str | None
    srt_match_ambiguous: bool
    srt_match_candidate_count: int | None
    srt_sample_count: int | None
    srt_start: str | None
    srt_end: str | None
    srt_has_position: bool | None
    srt_has_altitude: bool | None
    srt_has_orientation: bool | None
    captured_at_local: str | None
    captured_at_utc: str | None
    capture_date: str | None
    timezone: str | None
    location: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateRecord:
    run_id: str
    stock_clip_id: str
    source_project_id: str
    source_media_id: str | None
    session_id: str | None
    source_segment_index: int
    source_ref: str | None
    source_name: str
    eligibility_status: str
    rejection_reason: str | None
    rejection_detail: str | None
    original_start: str | None
    original_duration: str | None
    original_duration_seconds: float | None
    proposed_start: str | None
    proposed_duration: str | None
    proposed_duration_seconds: float | None
    short_clip_recovery: str | None
    candidate_tier: str | None
    sidecar_path: str | None
    srt_status: str | None
    srt_window_status: str | None
    srt_reasons: list[str] = field(default_factory=list)
    visual_status: str | None = None
    visual_reasons: list[str] = field(default_factory=list)
    visual_metrics: dict[str, Any] = field(default_factory=dict)
    location: dict[str, Any] = field(default_factory=dict)
    capture_time: dict[str, Any] = field(default_factory=dict)
    time_of_day: dict[str, Any] = field(default_factory=dict)
    weather: dict[str, Any] = field(default_factory=dict)
    replacement_ref: str | None = None
    creative_effects: list[str] = field(default_factory=list)
    camera_lut: str | None = None
    effect_signature: str | None = None
    generated_event_name: str | None = None
    generated_project_label: str | None = None
    generated_compilation_name: str | None = None
    generated_clip_project_name: str | None = None
    clip_sequence: int | None = None
    expected_export_basename: str | None = None
    compilation_timeline_offset: str | None = None
    project_timecode: str | None = None


@dataclass(frozen=True)
class GeneratedOccurrenceRecord:
    run_id: str
    stock_clip_id: str
    representation: str
    generated_event_name: str
    generated_project_name: str
    project_uid: str | None
    source_start: str
    duration: str
    timeline_offset: str | None
    effect_signature: str | None


@dataclass(frozen=True)
class StockifySnapshot:
    events: list[SourceEventRecord]
    sessions: list[ShootSessionRecord]
    projects: list[SourceProjectRecord]
    media: list[SourceMediaRecord]
    candidates: list[CandidateRecord]
    occurrences: list[GeneratedOccurrenceRecord]
    families: list[ProjectFamilyRecord] = field(default_factory=list)
