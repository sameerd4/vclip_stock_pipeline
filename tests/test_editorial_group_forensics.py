from __future__ import annotations

from vclip_pipeline.stockify.jpg_exif_same_shoot import EVIDENCE_SOURCE as JPG
from vclip_pipeline.workflow.editorial_group_forensics import (
    EDITORIAL_CONSENSUS_EVIDENCE,
    SourceGeoEvidence,
    analyze_editorial_groups,
    classify_group_coherence,
)


def _src(
    stem: str,
    *,
    kind: str,
    city: str | None = "Seattle",
    neighborhood: str | None = None,
    state: str = "Washington",
    country: str = "United States",
    lat: float | None = 47.61,
    lon: float | None = -122.32,
    confidence: str = "high",
    clips: list[str] | None = None,
) -> SourceGeoEvidence:
    return SourceGeoEvidence(
        source_basename=f"{stem}.mp4",
        stem=stem,
        evidence_kind=kind,
        confidence=confidence,
        latitude=lat,
        longitude=lon,
        neighborhood=neighborhood,
        city=city,
        state=state,
        country=country,
        public_label=(
            f"{neighborhood}, {city}" if neighborhood and city else f"{city}, {state}"
        ),
        stock_clip_ids=clips or [f"VCLIP_{stem.upper()}"],
    )


def test_seattle_neighborhoods_remain_one_city_editorial_group():
    located = [
        _src("a", kind="srt_gps", neighborhood="Capitol Hill", lat=47.625, lon=-122.32),
        _src("b", kind=JPG, neighborhood="Downtown", lat=47.606, lon=-122.332),
        _src("c", kind=JPG, neighborhood="First Hill", lat=47.608, lon=-122.324),
        _src("d", kind=JPG, neighborhood="South Lake Union", lat=47.625, lon=-122.338),
    ]
    coherence, label, level, contradictions = classify_group_coherence(located)
    assert coherence == "city"
    assert level == "city"
    assert label == "Seattle, Washington"
    assert contradictions == []


def test_mixed_when_cities_are_discontinuous():
    located = [
        _src("sea", kind="srt_gps", city="Seattle", lat=47.61, lon=-122.33),
        _src("pdx", kind=JPG, city="Portland", state="Oregon", lat=45.52, lon=-122.68),
    ]
    coherence, label, level, contradictions = classify_group_coherence(located)
    assert coherence == "mixed"
    assert label is None
    assert level is None
    assert contradictions


def test_group_consensus_inheritance_without_fabricated_gps():
    evidence = {
        "loc1": _src("loc1", kind="srt_gps", neighborhood="Capitol Hill", clips=["C1"]),
        "loc2": _src("loc2", kind=JPG, neighborhood="Downtown", clips=["C2"]),
        "miss1": SourceGeoEvidence(
            source_basename="miss1.mp4",
            stem="miss1",
            evidence_kind="none",
            stock_clip_ids=["C3"],
        ),
        "miss2": SourceGeoEvidence(
            source_basename="miss2.mp4",
            stem="miss2",
            evidence_kind="none",
            stock_clip_ids=["C4"],
        ),
    }
    appearances = []
    for _stem, item in evidence.items():
        for clip_id in item.stock_clip_ids:
            appearances.append(
                {
                    "stockify_run_id": "RUN",
                    "stock_clip_id": clip_id,
                    "event_name": "Unknown Location — Night Seattle",
                    "source_basename": item.source_basename,
                    "relative_xml": "shard.fcpxml",
                }
            )
    groups, summary = analyze_editorial_groups(
        unknown_appearances=appearances,
        source_evidence=evidence,
    )
    assert len(groups) == 1
    group = groups[0]
    assert group.geographic_coherence == "city"
    assert group.recommended_group_label == "Seattle, Washington"
    assert set(group.unknown_clips_eligible_to_inherit) == {"C3", "C4"}
    assert group.provenance["evidence_source"] == EDITORIAL_CONSENSUS_EVIDENCE
    assert group.provenance["coordinates_inherited"] is False
    assert summary["additional_clips_eligible_for_group_consensus"] == 2
    assert summary["clips_gaining_source_level_jpg_context"] == 1
    assert summary["clips_with_direct_srt_gps_context"] == 1
    # Inheritors must not get source coordinates fabricated onto evidence_kind none.
    for item in group.source_evidence:
        if item["stem"] in {"miss1", "miss2"}:
            assert item["latitude"] is None
            assert item["longitude"] is None
            assert item["evidence_kind"] == "none"


def test_no_event_split_from_spatial_clusters_inside_one_city():
    # Far-apart Seattle neighborhoods still one editorial city group.
    located = [
        _src("north", kind=JPG, neighborhood="University District", lat=47.655, lon=-122.303),
        _src("south", kind=JPG, neighborhood="Georgetown", lat=47.55, lon=-122.33),
    ]
    coherence, label, level, _ = classify_group_coherence(located)
    assert coherence == "city"
    assert label == "Seattle, Washington"
    assert level == "city"


def test_stale_event_label_contradicted_by_source_gps():
    evidence = {
        "a": _src(
            "a",
            kind=JPG,
            city="Seattle",
            neighborhood="Fremont",
            lat=47.65,
            lon=-122.35,
            clips=["C1"],
        ),
        "b": _src(
            "b",
            kind=JPG,
            city="Seattle",
            neighborhood="Fremont",
            lat=47.651,
            lon=-122.351,
            clips=["C2"],
        ),
    }
    appearances = [
        {
            "stockify_run_id": "RUN",
            "stock_clip_id": "C1",
            "event_name": "Troutville, Virginia — 2025-08-28",
            "source_basename": "a.mp4",
            "relative_xml": "shard.fcpxml",
        },
        {
            "stockify_run_id": "RUN",
            "stock_clip_id": "C2",
            "event_name": "Troutville, Virginia — 2025-08-28",
            "source_basename": "b.mp4",
            "relative_xml": "shard.fcpxml",
        },
    ]
    groups, _summary = analyze_editorial_groups(
        unknown_appearances=appearances,
        source_evidence=evidence,
    )
    assert len(groups) == 1
    assert groups[0].geographic_coherence in {"neighborhood", "city"}
    assert any(
        "stale_event_label_contradicted_by_source_gps" in note
        for note in groups[0].contradictory_evidence
    )
    assert groups[0].review_required is True


def test_coords_without_place_labels_are_not_false_mixed():
    located = [
        SourceGeoEvidence(
            source_basename="a.mp4",
            stem="a",
            evidence_kind=JPG,
            confidence="high",
            latitude=49.309,
            longitude=-123.08,
        ),
        SourceGeoEvidence(
            source_basename="b.mp4",
            stem="b",
            evidence_kind=JPG,
            confidence="high",
            latitude=49.296,
            longitude=-123.104,
        ),
    ]
    coherence, label, level, contradictions = classify_group_coherence(located)
    assert coherence == "unresolved"
    assert label is None
    assert level is None
    assert any("coords_without_place_labels" in note for note in contradictions)


def test_british_columbia_region_label_is_canada_not_united_states():
    located = [
        _src(
            "westvan",
            kind=JPG,
            city="West Vancouver",
            neighborhood="Sunset Beach",
            state="British Columbia",
            country="Canada",
            lat=49.401449,
            lon=-123.25558,
        ),
        _src(
            "garibaldi",
            kind=JPG,
            city="Area D (Elaho/Garibaldi)",
            neighborhood=None,
            state="British Columbia",
            country="Canada",
            lat=49.561865,
            lon=-123.234647,
        ),
    ]
    coherence, label, level, contradictions = classify_group_coherence(located)
    assert coherence == "region"
    assert level == "region"
    assert label == "British Columbia, Canada"
    assert "United States" not in (label or "")
    assert located[0].latitude == 49.401449
    assert located[1].longitude == -123.234647
    assert any("multiple_cities_same_region" in note for note in contradictions)


def test_washington_region_label_stays_united_states():
    located = [
        _src("sea", kind=JPG, city="Seattle", lat=47.61, lon=-122.33),
        _src(
            "bli",
            kind=JPG,
            city="Bellingham",
            state="Washington",
            lat=48.75,
            lon=-122.48,
        ),
    ]
    coherence, label, level, contradictions = classify_group_coherence(located)
    assert coherence == "region"
    assert label == "Washington, United States"
    assert any("multiple_cities_same_region" in note for note in contradictions)


def test_mixed_canada_and_united_states_session_stays_unlabeled():
    located = [
        _src(
            "slus",
            kind=JPG,
            city="Seattle",
            neighborhood="South Lake Union",
            state="Washington",
            country="United States",
            lat=47.619795,
            lon=-122.34343,
        ),
        _src(
            "van",
            kind=JPG,
            city="Vancouver",
            neighborhood="Mount Pleasant",
            state="British Columbia",
            country="Canada",
            lat=49.258858,
            lon=-123.111019,
        ),
        _src(
            "delta",
            kind=JPG,
            city="Delta",
            neighborhood=None,
            state="British Columbia",
            country="Canada",
            lat=49.094659,
            lon=-122.933468,
        ),
    ]
    coherence, label, level, contradictions = classify_group_coherence(located)
    assert coherence == "mixed"
    assert label is None
    assert level is None
    assert any("multiple_countries" in note for note in contradictions)


def test_source_jpg_evidence_precedes_mixed_editorial_group():
    evidence = {
        "jpg1": _src(
            "jpg1",
            kind=JPG,
            city="Seattle",
            neighborhood="South Lake Union",
            clips=["C_JPG_SEA"],
        ),
        "jpg2": _src(
            "jpg2",
            kind=JPG,
            city="Vancouver",
            neighborhood="Mount Pleasant",
            state="British Columbia",
            country="Canada",
            lat=49.258858,
            lon=-123.111019,
            clips=["C_JPG_VAN"],
        ),
        "miss": SourceGeoEvidence(
            source_basename="miss.mp4",
            stem="miss",
            evidence_kind="none",
            stock_clip_ids=["C_UNRESOLVED"],
        ),
    }
    appearances = []
    for item in evidence.values():
        for clip_id in item.stock_clip_ids:
            appearances.append(
                {
                    "stockify_run_id": "RUN",
                    "stock_clip_id": clip_id,
                    "event_name": "Unknown Location — 2025-01-18 — Session 16",
                    "source_basename": item.source_basename,
                    "relative_xml": "session-16.fcpxml",
                }
            )
    groups, summary = analyze_editorial_groups(
        unknown_appearances=appearances,
        source_evidence=evidence,
    )
    assert len(groups) == 1
    group = groups[0]
    assert group.geographic_coherence == "mixed"
    assert group.recommended_group_label is None
    assert group.unknown_clips_eligible_to_inherit == []
    by_stem = {item["stem"]: item for item in group.source_evidence}
    assert by_stem["jpg1"]["evidence_kind"] == JPG
    assert by_stem["jpg1"]["latitude"] == 47.61
    assert by_stem["jpg2"]["evidence_kind"] == JPG
    assert by_stem["jpg2"]["city"] == "Vancouver"
    assert by_stem["miss"]["evidence_kind"] == "none"
    assert by_stem["miss"]["latitude"] is None
    assert summary["clips_gaining_source_level_jpg_context"] == 2
    assert summary["additional_clips_eligible_for_group_consensus"] == 0
