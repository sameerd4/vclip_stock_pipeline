from types import SimpleNamespace
from xml.etree.ElementTree import Element

from vclip_pipeline.stockify.families import (
    aspect_preservation_rank,
    ranking_key,
)


def _project(
    *,
    name: str,
    timeline_width: int,
    timeline_height: int,
    source_width: int,
    source_height: int,
    mod_date: str,
):
    media = SimpleNamespace(width=source_width, height=source_height)
    record = SimpleNamespace(
        creative_effects=["Custom LUT"],
        proposed_duration_seconds=10.0,
        original_duration_seconds=10.0,
    )
    candidate = SimpleNamespace(
        media_record=media,
        segment_report=None,
        candidate_record=record,
        eligibility_status="accepted",
    )
    source_project = Element("project", {"modDate": mod_date})
    return SimpleNamespace(
        format_info={"width": timeline_width, "height": timeline_height},
        accepted=[candidate],
        candidates=[candidate],
        source_project=source_project,
        source_project_uid=f"uid-{name}",
        source_project_name=name,
        source_project_id=f"id-{name}",
    )


def test_native_landscape_project_beats_newer_square_revision():
    landscape = _project(
        name="SF Landscape",
        timeline_width=3840,
        timeline_height=2160,
        source_width=3840,
        source_height=2160,
        mod_date="2026-06-13 08:00:00 +0000",
    )
    square = _project(
        name="SF Square",
        timeline_width=2160,
        timeline_height=2160,
        source_width=3840,
        source_height=2160,
        mod_date="2026-06-13 09:00:00 +0000",
    )

    assert aspect_preservation_rank(landscape) == 2
    assert aspect_preservation_rank(square) == 0
    assert ranking_key(landscape) > ranking_key(square)


def test_native_vertical_project_is_preferred_for_vertical_source():
    vertical = _project(
        name="Native Vertical",
        timeline_width=2160,
        timeline_height=3840,
        source_width=2160,
        source_height=3840,
        mod_date="2026-07-30 19:00:00 +0000",
    )
    landscape = _project(
        name="Landscape Derivative",
        timeline_width=3840,
        timeline_height=2160,
        source_width=2160,
        source_height=3840,
        mod_date="2026-07-30 20:00:00 +0000",
    )

    assert aspect_preservation_rank(vertical) == 2
    assert aspect_preservation_rank(landscape) == 1
    assert ranking_key(vertical) > ranking_key(landscape)


def test_unknown_source_geometry_is_neutral():
    project = _project(
        name="Unknown Source",
        timeline_width=3840,
        timeline_height=2160,
        source_width=0,
        source_height=0,
        mod_date="2026-01-01 00:00:00 +0000",
    )

    assert aspect_preservation_rank(project) == 1
