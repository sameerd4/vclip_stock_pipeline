from __future__ import annotations

from vclip_pipeline.workflow.camera_scope import (
    SCOPE_DRONE,
    SCOPE_OUT_OF_SCOPE_NON_DRONE,
    classify_appearance_camera_scope,
    classify_vclip_camera_scope,
)
from vclip_pipeline.workflow.editorial_group_forensics import (
    SourceGeoEvidence,
    analyze_editorial_groups,
)


def test_osmo_pocket_lut_is_out_of_scope():
    result = classify_vclip_camera_scope(
        source_basename="DJI_20251206000100_0300_D.mp4",
        camera_lut="LUT:abc (DJI OSMO Pocket 3 D-Log M to Rec.709 V1)",
        source_project_name="Pocket Night",
    )
    assert result["camera_scope"] == SCOPE_OUT_OF_SCOPE_NON_DRONE
    assert result["camera_family"] == "osmo_pocket"


def test_iphone_uuid_mov_is_out_of_scope():
    result = classify_vclip_camera_scope(
        source_basename="161EF2DA-9F4E-409C-8A77-DCE5FD809681.mov",
        source_project_name="Hong Kong iPhone",
    )
    assert result["camera_scope"] == SCOPE_OUT_OF_SCOPE_NON_DRONE
    assert result["camera_family"] == "iphone_export"


def test_air3_lut_remains_drone_scope():
    result = classify_vclip_camera_scope(
        source_basename="DJI_20251031173525_0022_D.mp4",
        camera_lut="LUT:x (DJI Air 3 D-Log M to Rec.709 V1_)",
        media_path="/Volumes/T7/Nova.fcpbundle/October in Seattle/Original Media/x.mp4",
    )
    assert result["camera_scope"] == SCOPE_DRONE


def test_summary_excludes_pocket_from_unresolved_backlog():
    from vclip_pipeline.stockify.sidecars import normalized_stem

    appearances = [
        {
            "stockify_run_id": "RUN",
            "stock_clip_id": "P1",
            "event_name": "Unknown Location — 2025-12-06 — Night",
            "source_basename": "DJI_20251206000100_0300_D.mp4",
            "relative_xml": "a.fcpxml",
            "row": {
                "source_filename": "DJI_20251206000100_0300_D.mp4",
                "camera_lut": "LUT:abc (DJI OSMO Pocket 3 D-Log M to Rec.709 V1)",
                "source_project_name": "Pocket Night",
                "source_event_name": "December",
            },
        },
        {
            "stockify_run_id": "RUN",
            "stock_clip_id": "D1",
            "event_name": "Unknown Location — 2025-10-31 — Afternoon",
            "source_basename": "DJI_20251031173525_0022_D.mp4",
            "relative_xml": "b.fcpxml",
            "row": {
                "source_filename": "DJI_20251031173525_0022_D.mp4",
                "camera_lut": "LUT:x (DJI Air 3 D-Log M to Rec.709 V1_)",
                "source_project_name": "5PM in Fremont",
                "source_event_name": "October in Seattle",
            },
        },
    ]
    evidence = {
        normalized_stem(item["source_basename"]): SourceGeoEvidence(
            source_basename=item["source_basename"],
            stem=normalized_stem(item["source_basename"]),
            evidence_kind="none",
            stock_clip_ids=[item["stock_clip_id"]],
        )
        for item in appearances
    }
    _groups, summary = analyze_editorial_groups(
        unknown_appearances=appearances,
        source_evidence=evidence,
    )
    assert summary["clips_out_of_scope_non_drone"] == 1
    assert summary["clips_still_fully_unresolved"] == 1
    assert summary["clips_still_fully_unresolved_including_out_of_scope"] == 2
    assert summary["still_fully_unresolved_clip_ids"] == ["D1"]
    assert classify_appearance_camera_scope(appearances[0])["camera_scope"] == (
        SCOPE_OUT_OF_SCOPE_NON_DRONE
    )
