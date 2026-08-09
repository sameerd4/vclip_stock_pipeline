"""Eligibility semantics for short-clip recovery.

Policy under test:
- Absolute input floor: original duration < 0.5s → reject (short_duration)
- Below 3.0s: acceptance requires successful recovery to >= 3.0s
- Failed / disabled recovery must reject, not emit the original short clip
- >= 3.0s: accepted without recovery
- Expansion targets remain 10s ideal / 5s preferred / 3s minimum
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import ProjectSpec, run_stockify, write_srt
from vclip_pipeline.stockify.clips import recover_short_clip
from vclip_pipeline.stockify.models import (
    SidecarIndex,
    SidecarMatchResult,
    SidecarSummary,
    SrtWindowScore,
    VisualMotionScore,
)
from vclip_pipeline.stockify.sidecars import parse_srt_info


def _single_clip_run(
    tmp_path: Path,
    *,
    duration: float,
    start: float = 10.0,
    asset_duration: float = 60.0,
    recover: bool = False,
    require_srt: bool = False,
    visual_score: bool = False,
    require_visual: bool = False,
    stamp: str = "20251115120000",
):
    result = run_stockify(
        tmp_path,
        [
            ProjectSpec(
                name="Short Clip Case",
                stamp=stamp,
                latitude=47.2529,
                longitude=-122.4443,
                graded=True,
                clip_count=1,
                clip_starts=(start,),
                clip_durations=(duration,),
                asset_duration_seconds=asset_duration,
            )
        ],
        option_overrides={
            "recover_short_clips": recover,
            "require_srt_for_expansion": require_srt,
            "visual_score": visual_score,
            "require_visual_for_expansion": require_visual,
            "layout": "project-per-clip",
            "include_compilations": False,
        },
    )
    candidates = result["repository"].candidates_for_run(result["result"].stockify_run_id)
    assert len(candidates) == 1
    return result, candidates[0]


def _run_without_srt(
    tmp_path: Path,
    *,
    duration: float,
    start: float = 10.0,
    asset_duration: float = 60.0,
    recover: bool = True,
    require_srt: bool = True,
):
    """Build source XML whose media has no sibling SRT."""
    from fractions import Fraction

    from vclip_pipeline.db import CatalogRepository, Database
    from vclip_pipeline.geo import CatalogLocationResolver, CompositeLocationResolver
    from vclip_pipeline.stockify import StockifyOptions, StockifyService
    from vclip_pipeline.stockify.core import format_time

    root = ET.Element("fcpxml", {"version": "1.11"})
    resources = ET.SubElement(root, "resources")
    ET.SubElement(
        resources,
        "format",
        {
            "id": "r1",
            "name": "FFVideoFormat1080p30",
            "frameDuration": "1/30s",
            "width": "1920",
            "height": "1080",
            "colorSpace": "1-1-1 (Rec. 709)",
        },
    )
    ET.SubElement(resources, "effect", {"id": "rfx", "name": "Custom LUT", "uid": "custom-lut"})
    media = tmp_path / "DJI_NOSRT_0001_D.MP4"
    media.write_bytes(b"")
    asset = ET.SubElement(
        resources,
        "asset",
        {
            "id": "r2",
            "name": media.name,
            "uid": "ASSET-NOSRT",
            "start": "0s",
            "duration": format_time(Fraction(str(asset_duration))),
            "hasVideo": "1",
            "format": "r1",
            "videoSources": "1",
        },
    )
    ET.SubElement(
        asset,
        "media-rep",
        {"kind": "original-media", "src": media.resolve().as_uri()},
    )
    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", {"name": "Evt", "uid": "EV1"})
    project = ET.SubElement(event, "project", {"name": "Short", "uid": "P1"})
    sequence = ET.SubElement(
        project,
        "sequence",
        {
            "format": "r1",
            "duration": format_time(Fraction(str(duration))),
            "tcStart": "0s",
            "tcFormat": "NDF",
            "audioLayout": "stereo",
            "audioRate": "48k",
        },
    )
    spine = ET.SubElement(sequence, "spine")
    clip = ET.SubElement(
        spine,
        "asset-clip",
        {
            "ref": "r2",
            "offset": "0s",
            "name": "Clip 1",
            "start": format_time(Fraction(str(start))),
            "duration": format_time(Fraction(str(duration))),
        },
    )
    ET.SubElement(clip, "filter-video", {"ref": "rfx", "name": "Custom LUT"})
    source = tmp_path / "source.fcpxml"
    source.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))

    database = Database(tmp_path / "vclip.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    resolver = CompositeLocationResolver(
        [
            CatalogLocationResolver.from_json(
                Path(__file__).parents[1]
                / "src"
                / "vclip_pipeline"
                / "data"
                / "places.json"
            )
        ]
    )
    options = StockifyOptions(
        input_path=source,
        output_path=tmp_path / "review.fcpxml",
        report_path=tmp_path / "report.json",
        database_path=tmp_path / "vclip.sqlite3",
        manifest_path=tmp_path / "manifest.json",
        layout="project-per-clip",
        include_compilations=False,
        recover_short_clips=recover,
        require_srt_for_expansion=require_srt,
        sidecar_roots=(),
    )
    result = StockifyService(repository, resolver).run(options)
    candidate = repository.candidates_for_run(result.stockify_run_id)[0]
    return result, candidate


# --- Duration buckets with recovery disabled ---


def test_below_absolute_floor_rejected_recovery_off(tmp_path: Path):
    _, candidate = _single_clip_run(tmp_path, duration=0.4, recover=False)
    assert candidate["eligibility_status"] == "rejected"
    assert candidate["rejection_reason"] == "short_duration"


def test_half_to_one_second_rejected_when_recovery_off(tmp_path: Path):
    _, candidate = _single_clip_run(tmp_path, duration=0.6, recover=False)
    assert candidate["eligibility_status"] == "rejected"
    assert candidate["rejection_reason"] == "short_clip_unrecovered"


def test_one_to_three_seconds_rejected_when_recovery_off(tmp_path: Path):
    _, candidate = _single_clip_run(tmp_path, duration=1.5, recover=False)
    assert candidate["eligibility_status"] == "rejected"
    assert candidate["rejection_reason"] == "short_clip_unrecovered"


def test_just_under_three_seconds_rejected_when_recovery_off(tmp_path: Path):
    _, candidate = _single_clip_run(tmp_path, duration=2.9, recover=False)
    assert candidate["eligibility_status"] == "rejected"
    assert candidate["rejection_reason"] == "short_clip_unrecovered"


def test_exactly_three_seconds_accepted_when_recovery_off(tmp_path: Path):
    _, candidate = _single_clip_run(tmp_path, duration=3.0, recover=False)
    assert candidate["eligibility_status"] == "accepted"
    assert candidate["proposed_duration_seconds"] == pytest.approx(3.0)
    assert candidate["short_clip_recovery"] == "not_applicable"


def test_three_to_five_seconds_accepted_when_recovery_off(tmp_path: Path):
    _, candidate = _single_clip_run(tmp_path, duration=4.0, recover=False)
    assert candidate["eligibility_status"] == "accepted"
    assert candidate["proposed_duration_seconds"] == pytest.approx(4.0)


def test_above_five_seconds_accepted_when_recovery_off(tmp_path: Path):
    _, candidate = _single_clip_run(tmp_path, duration=8.0, recover=False)
    assert candidate["eligibility_status"] == "accepted"
    assert candidate["proposed_duration_seconds"] == pytest.approx(8.0)


# --- Duration buckets with recovery enabled ---


def test_below_absolute_floor_rejected_even_with_recovery_on(tmp_path: Path):
    _, candidate = _single_clip_run(tmp_path, duration=0.4, recover=True)
    assert candidate["eligibility_status"] == "rejected"
    assert candidate["rejection_reason"] == "short_duration"


def test_half_to_one_second_expands_and_accepts_when_recovery_on(tmp_path: Path):
    _, candidate = _single_clip_run(tmp_path, duration=0.6, recover=True)
    assert candidate["eligibility_status"] == "accepted"
    assert candidate["short_clip_recovery"] in {"expanded", "expanded_review"}
    assert candidate["proposed_duration_seconds"] >= 3.0
    assert candidate["proposed_duration_seconds"] >= candidate["original_duration_seconds"]


def test_one_to_three_seconds_expands_and_accepts_when_recovery_on(tmp_path: Path):
    _, candidate = _single_clip_run(tmp_path, duration=1.5, recover=True)
    assert candidate["eligibility_status"] == "accepted"
    assert candidate["short_clip_recovery"] in {"expanded", "expanded_review"}
    assert candidate["proposed_duration_seconds"] >= 3.0


def test_exactly_three_seconds_accepted_without_expansion(tmp_path: Path):
    _, candidate = _single_clip_run(tmp_path, duration=3.0, recover=True)
    assert candidate["eligibility_status"] == "accepted"
    assert candidate["short_clip_recovery"] == "not_applicable"
    assert candidate["proposed_duration_seconds"] == pytest.approx(3.0)


def test_three_to_five_and_above_unchanged_with_recovery_on(tmp_path: Path):
    _, mid = _single_clip_run(tmp_path / "mid", duration=4.0, recover=True)
    _, long = _single_clip_run(tmp_path / "long", duration=12.0, recover=True)
    assert mid["eligibility_status"] == "accepted"
    assert mid["proposed_duration_seconds"] == pytest.approx(4.0)
    assert long["eligibility_status"] == "accepted"
    assert long["proposed_duration_seconds"] == pytest.approx(12.0)


# --- Recovery outcomes ---


def test_successful_expansion_prefers_longest_viable_target(tmp_path: Path):
    _, candidate = _single_clip_run(tmp_path, duration=1.0, start=20.0, recover=True)
    assert candidate["eligibility_status"] == "accepted"
    # Ideal 10s should win when handles + SRT allow it.
    assert candidate["proposed_duration_seconds"] == pytest.approx(10.0)


def test_missing_srt_with_require_srt_rejects_short_clip(tmp_path: Path):
    _, candidate = _run_without_srt(
        tmp_path,
        duration=1.0,
        recover=True,
        require_srt=True,
    )
    assert candidate["eligibility_status"] == "rejected"
    assert candidate["rejection_reason"] == "short_clip_unrecovered"
    assert candidate["short_clip_recovery"] == "missing_srt"


def test_insufficient_source_handles_rejects_short_clip(tmp_path: Path):
    # Asset only 2s long; cannot expand a 1s clip to >= 3s.
    _, candidate = _single_clip_run(
        tmp_path,
        duration=1.0,
        start=0.0,
        asset_duration=2.0,
        recover=True,
    )
    assert candidate["eligibility_status"] == "rejected"
    assert candidate["rejection_reason"] == "short_clip_unrecovered"
    assert candidate["short_clip_recovery"] == "not_enough_source_media"


def test_asymmetric_recovery_near_asset_start(tmp_path: Path):
    """Only forward handles exist; expansion must still reach >= 3s."""
    _, candidate = _single_clip_run(
        tmp_path,
        duration=1.0,
        start=0.0,
        asset_duration=60.0,
        recover=True,
    )
    assert candidate["eligibility_status"] == "accepted"
    assert candidate["proposed_duration_seconds"] >= 3.0
    assert candidate["proposed_start"] in {"0s", "0/1s"}


def test_asymmetric_recovery_near_asset_end(tmp_path: Path):
    """Only backward handles exist near the end of the asset."""
    from vclip_pipeline.stockify.core import parse_time

    _, candidate = _single_clip_run(
        tmp_path,
        duration=1.0,
        start=59.0,
        asset_duration=60.0,
        recover=True,
    )
    assert candidate["eligibility_status"] == "accepted"
    assert candidate["proposed_duration_seconds"] >= 3.0
    start = float(parse_time(str(candidate["proposed_start"])))
    assert start >= 0.0
    assert start + float(candidate["proposed_duration_seconds"]) <= 60.0 + 1e-6


def test_telemetry_rejection_rejects_short_clip(tmp_path: Path):
    reject = SrtWindowScore(
        status="reject",
        sample_count=10,
        coverage=1.0,
        reasons=("ground_speed_spike",),
    )
    with patch(
        "vclip_pipeline.stockify.clips.score_srt_window",
        return_value=reject,
    ):
        _, candidate = _single_clip_run(tmp_path, duration=1.0, recover=True)
    assert candidate["eligibility_status"] == "rejected"
    assert candidate["rejection_reason"] == "short_clip_unrecovered"
    assert candidate["short_clip_recovery"] == "srt_rejected_expansion"


def test_visual_rejection_rejects_short_clip(tmp_path: Path):
    from vclip_pipeline.stockify.models import VisualPreflightReport

    visual_reject = VisualMotionScore(
        status="reject",
        frame_count=12,
        reasons=("frame_diff_spike",),
    )
    with (
        patch(
            "vclip_pipeline.stockify.service.preflight_visual_scoring",
            return_value=VisualPreflightReport(),
        ),
        patch(
            "vclip_pipeline.stockify.clips.score_visual_window",
            return_value=visual_reject,
        ),
    ):
        _, candidate = _single_clip_run(
            tmp_path,
            duration=1.0,
            recover=True,
            visual_score=True,
            require_visual=True,
        )
    assert candidate["eligibility_status"] == "rejected"
    assert candidate["rejection_reason"] == "short_clip_unrecovered"
    assert candidate["short_clip_recovery"] == "visual_rejected_expansion"


def test_failed_recovery_does_not_emit_original_short_duration(tmp_path: Path):
    _, candidate = _single_clip_run(
        tmp_path,
        duration=1.0,
        start=0.0,
        asset_duration=2.0,
        recover=True,
    )
    assert candidate["eligibility_status"] == "rejected"
    # Review XML must not contain an accepted short asset-clip project.
    root = ET.parse(tmp_path / "review.fcpxml").getroot()
    accepted_clips = [
        node
        for node in root.iter()
        if node.tag.endswith("asset-clip") and node.get("duration")
    ]
    # Rejected candidates are not written as stock projects.
    assert accepted_clips == []


# --- Unit-level recover_short_clip mutation / status coverage ---


def _recovery_fixture(tmp_path: Path, *, start: str, duration: str, asset_duration: str):
    media = tmp_path / "clip.MP4"
    media.write_bytes(b"")
    srt = tmp_path / "clip.SRT"
    write_srt(srt, "20251115120000", 47.25, -122.44)
    asset = ET.Element(
        "asset",
        {
            "id": "r2",
            "name": "clip.MP4",
            "duration": asset_duration,
            "hasVideo": "1",
        },
    )
    ET.SubElement(
        asset,
        "media-rep",
        {"kind": "original-media", "src": media.resolve().as_uri()},
    )
    clean = ET.Element(
        "asset-clip",
        {"ref": "r2", "name": "Clip", "start": start, "duration": duration},
    )
    source = ET.Element(
        "asset-clip",
        {"ref": "r2", "name": "Clip", "start": start, "duration": duration},
    )
    index = SidecarIndex(
        archive_by_stem={},
        summary=SidecarSummary(),
        by_asset_id={
            "r2": SidecarMatchResult(path=srt, method="exact_sibling", confidence="high")
        },
    )
    return clean, source, asset, index, srt


def test_recover_short_clip_mutates_clean_clip_on_success(tmp_path: Path):
    clean, source, asset, index, srt = _recovery_fixture(
        tmp_path, start="10s", duration="1s", asset_duration="60s"
    )
    result = recover_short_clip(
        clean,
        source,
        asset,
        sidecar_index=index,
        srt_cache={srt: parse_srt_info(srt)},
        enabled=True,
        short_clip_threshold_seconds=3.0,
        minimum_duration_seconds=3.0,
        preferred_duration_seconds=5.0,
        ideal_duration_seconds=10.0,
        require_srt_for_expansion=False,
    )
    from vclip_pipeline.stockify.core import parse_time

    assert result.status in {"expanded", "expanded_review"}
    assert float(result.output_duration) >= 3.0
    assert parse_time(clean.get("duration") or "0s") == result.output_duration
    assert parse_time(clean.get("start") or "0s") == result.output_start
