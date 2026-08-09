"""Stockify's in-memory domain model."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..db.records import CandidateRecord, SourceMediaRecord
from .models import ProjectReport, SegmentReport


@dataclass(frozen=True)
class StockifyOptions:
    input_path: Path
    output_path: Path
    report_path: Path
    database_path: Path
    manifest_path: Path | None
    requested_path: Path | None = None
    library_name: str = "VClip Stock Review"
    layout: str = "both"
    include_compilations: bool = True
    # Absolute input floor: originals shorter than this are rejected before recovery.
    min_duration_seconds: float = 0.5
    max_segments_per_project: int | None = None
    force_disable_audio: bool = True
    # When False, clips below short_clip_threshold_seconds are rejected
    # (short_clip_unrecovered) instead of being emitted at their original length.
    recover_short_clips: bool = False
    # Originals shorter than this require successful recovery to at least
    # expanded_minimum_duration_seconds before acceptance.
    short_clip_threshold_seconds: float = 3.0
    expanded_minimum_duration_seconds: float = 3.0
    expanded_preferred_duration_seconds: float = 5.0
    expanded_ideal_duration_seconds: float = 10.0
    sidecar_roots: tuple[Path, ...] = ()
    require_srt_for_expansion: bool = False
    visual_score: bool = False
    require_visual_for_expansion: bool = False
    visual_fps: int = 12
    visual_width: int = 320
    visual_height: int = 180
    visual_reject_shift_px: float = 12.0
    visual_reject_frame_diff: float = 12.0
    visual_timeout_seconds: float = 120.0
    require_camera_lut: bool = False
    require_custom_lut: bool = False
    project_names: frozenset[str] | None = None
    session_gap_hours: float = 4.0

    def as_json(self) -> dict[str, Any]:
        """Return a path-safe representation for run provenance."""
        return {
            key: (
                [str(item) for item in value]
                if key == "sidecar_roots"
                else sorted(value)
                if key == "project_names" and value is not None
                else str(value)
                if isinstance(value, Path)
                else value
            )
            for key, value in self.__dict__.items()
        }


@dataclass
class CandidateBuild:
    """Keep candidate XML, analysis, and persistence data together."""

    stock_clip_id: str
    segment_index: int
    source_clip: ET.Element
    asset: ET.Element | None
    clean_clip: ET.Element | None
    segment_report: SegmentReport | None
    eligibility_status: str
    rejection_reason: str | None = None
    rejection_detail: str | None = None
    source_media_id: str | None = None
    media_record: SourceMediaRecord | None = None
    candidate_record: CandidateRecord | None = None
    clip_sequence: int | None = None


@dataclass
class ProjectBuild:
    """Represent one original project before output events are constructed."""

    source_event_id: str
    source_event_name: str
    source_project_id: str
    source_project_name: str
    source_project_uid: str | None
    source_project_index: int
    source_project: ET.Element
    project_report: ProjectReport
    sequence_format: str | None = None
    tc_format: str | None = None
    audio_layout: str | None = None
    audio_rate: str | None = None
    format_info: dict[str, Any] = field(default_factory=dict)
    candidates: list[CandidateBuild] = field(default_factory=list)
    accepted: list[CandidateBuild] = field(default_factory=list)
    session_id: str | None = None
    generated_event_name: str | None = None
    generated_project_label: str | None = None
    generated_compilation_name: str | None = None
    anchor: CandidateBuild | None = None
    project_family_id: str | None = None
    family_role: str | None = None
    family_selection_reason: str | None = None
    grading_coverage: float | None = None
    timeline_signature: tuple | None = None

    @property
    def project_treatment(self) -> str:
        """Describe the project's creative treatment for name collisions."""
        accepted_reports = [
            item.segment_report for item in self.accepted if item.segment_report is not None
        ]
        if not accepted_reports:
            return "Variant"
        graded = [bool(report.creative_video_effects) for report in accepted_reports]
        if all(graded):
            return "Graded"
        if not any(graded):
            return "Natural"
        return "Mixed"


ProgressCallback = Callable[[str], None]
