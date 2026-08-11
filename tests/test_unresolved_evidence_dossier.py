from __future__ import annotations

from pathlib import Path

from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.stockify.jpg_exif_same_shoot import (
    DjiFileIdentity,
    JpgExifPhoto,
    enumerate_nearby_jpg_evidence,
    find_dji_sequence_neighbors,
)
from vclip_pipeline.workflow.editorial_group_forensics import (
    SourceGeoEvidence,
    fill_missing_place_labels,
)
from vclip_pipeline.workflow.unresolved_evidence_dossier import (
    build_unresolved_evidence_dossiers,
)


class LabelResolver:
    def __init__(self, mapping: dict[tuple[float, float], dict]):
        self.mapping = mapping

    def resolve(self, latitude: float, longitude: float):
        for (lat, lon), place in self.mapping.items():
            if abs(latitude - lat) < 1e-4 and abs(longitude - lon) < 1e-4:
                return dict(place)
        return None


def test_enumerate_nearby_jpg_below_threshold(tmp_path: Path):
    # Photo is same day but 3 hours away — below inference threshold, still listed.
    photo = JpgExifPhoto(
        path=tmp_path / "DJI_20251206120000_0100_D.JPG",
        identity=DjiFileIdentity(
            stem="DJI_20251206120000_0100_D",
            filename="DJI_20251206120000_0100_D.JPG",
            capture_datetime=__import__("datetime").datetime(2025, 12, 6, 12, 0, 0),
            sequence=100,
            date="2025-12-06",
        ),
        latitude=47.61,
        longitude=-122.33,
    )
    index = {"2025-12-06": [photo]}
    result = enumerate_nearby_jpg_evidence(
        "DJI_20251206090000_0001_D.mp4",
        jpg_index=index,
    )
    assert result["accepted_inference"] is False
    assert result["rejection_reason"] == "nearest_same_day_gps_jpg_below_confidence_threshold"
    assert len(result["gps_photos"]) == 1
    assert result["gps_photos"][0]["below_inference_threshold"] is True


def test_find_dji_sequence_neighbors_sibling_dir(tmp_path: Path):
    media = tmp_path / "DJI_20251206090000_0010_D.mp4"
    media.write_bytes(b"x")
    neighbor = tmp_path / "DJI_20251206090100_0012_D.mp4"
    neighbor.write_bytes(b"y")
    (tmp_path / "DJI_20251206090200_0015_D.JPG").write_bytes(b"z")
    found = find_dji_sequence_neighbors(
        source_basename=media.name,
        media_path=str(media),
        media_roots=[tmp_path],
        sequence_window=10,
    )
    names = {item["filename"] for item in found}
    assert "DJI_20251206090100_0012_D.mp4" in names
    assert "DJI_20251206090200_0015_D.JPG" in names


def test_fill_missing_place_labels_preserves_gps_provenance():
    evidence = {
        "a": SourceGeoEvidence(
            source_basename="DJI_A.mp4",
            stem="a",
            evidence_kind="jpg_exif_same_shoot",
            confidence="high",
            latitude=49.309442,
            longitude=-123.079746,
            provenance={"evidence_sources": ["jpg_exif_same_shoot"]},
        )
    }
    resolver = LabelResolver(
        {
            (49.309442, -123.079746): {
                "city": "North Vancouver",
                "state": "British Columbia",
                "country": "Canada",
                "neighborhood": "Lower Lonsdale",
                "provider": "test",
                "match_confidence": "high",
            }
        }
    )
    summary = fill_missing_place_labels(evidence, resolver)
    assert summary["labeled_sources"] == 1
    item = evidence["a"]
    assert item.latitude == 49.309442
    assert item.longitude == -123.079746
    assert item.evidence_kind == "jpg_exif_same_shoot"
    assert item.city == "North Vancouver"
    assert item.neighborhood == "Lower Lonsdale"
    assert item.provenance["place_label_retry"]["gps_provenance_unchanged"] is True


def test_unresolved_dossier_ranks_events_and_lists_evidence(tmp_path: Path):
    database = Database(tmp_path / "dossier.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)

    evidence = {
        "dji_20251206090000_0010_d": SourceGeoEvidence(
            source_basename="DJI_20251206090000_0010_D.mp4",
            stem="dji_20251206090000_0010_d",
            evidence_kind="none",
            stock_clip_ids=["C1", "C2"],
        ),
        "clip": SourceGeoEvidence(
            source_basename="clip.mov",
            stem="clip",
            evidence_kind="none",
            stock_clip_ids=["C3"],
        ),
        "dji_20251206080000_0001_d": SourceGeoEvidence(
            source_basename="DJI_20251206080000_0001_D.mp4",
            stem="dji_20251206080000_0001_d",
            evidence_kind="jpg_exif_same_shoot",
            city="Seattle",
            state="Washington",
            latitude=47.61,
            longitude=-122.33,
            stock_clip_ids=["C9"],
        ),
    }
    appearances = [
        {
            "stockify_run_id": "RUN",
            "stock_clip_id": "C1",
            "event_name": "Unknown Location — Big",
            "source_basename": "DJI_20251206090000_0010_D.mp4",
            "relative_xml": "a.fcpxml",
            "row": {
                "source_filename": "DJI_20251206090000_0010_D.mp4",
                "source_media_path": str(tmp_path / "DJI_20251206090000_0010_D.mp4"),
                "source_event_name": "Night Flight",
                "source_project_name": "Project A",
                "location": {},
            },
        },
        {
            "stockify_run_id": "RUN",
            "stock_clip_id": "C2",
            "event_name": "Unknown Location — Big",
            "source_basename": "DJI_20251206090000_0010_D.mp4",
            "relative_xml": "a.fcpxml",
            "row": {
                "source_filename": "DJI_20251206090000_0010_D.mp4",
                "source_media_path": str(tmp_path / "DJI_20251206090000_0010_D.mp4"),
                "source_event_name": "Night Flight",
                "source_project_name": "Project A",
                "location": {},
            },
        },
        {
            "stockify_run_id": "RUN",
            "stock_clip_id": "C3",
            "event_name": "Unknown Location — Small",
            "source_basename": "clip.mov",
            "relative_xml": "b.fcpxml",
            "row": {
                "source_filename": "clip.mov",
                "source_media_path": str(tmp_path / "clip.mov"),
                "source_event_name": "Misc",
                "source_project_name": "Project B",
                "location": {"city": None},
            },
        },
    ]
    (tmp_path / "DJI_20251206090000_0010_D.mp4").write_bytes(b"x")
    (tmp_path / "DJI_20251206090100_0012_D.mp4").write_bytes(b"y")
    summary = {
        "still_fully_unresolved_keys": [
            {"stockify_run_id": "RUN", "stock_clip_id": "C1"},
            {"stockify_run_id": "RUN", "stock_clip_id": "C2"},
            {"stockify_run_id": "RUN", "stock_clip_id": "C3"},
        ]
    }
    dossiers = build_unresolved_evidence_dossiers(
        unknown_appearances=appearances,
        source_evidence=evidence,
        editorial_summary=summary,
        jpg_index={},
        media_roots=[tmp_path],
        repository=repository,
    )
    assert dossiers["fully_unresolved_clips"] == 3
    events = dossiers["events_ranked_by_clip_count"]
    assert events[0]["original_event_name"] == "Unknown Location — Big"
    assert events[0]["unresolved_clip_count"] == 2
    big_source = events[0]["sources"][0]
    assert big_source["inference_status"] == "no_location_inferred"
    assert any(
        item["filename"] == "DJI_20251206090100_0012_D.mp4"
        for item in big_source["nearby_dji_sequence_neighbors"]
    )
    # Same-day resolved source from evidence map.
    assert any(
        item["stem"] == "dji_20251206080000_0001_d"
        for item in big_source["same_day_resolved_sources"]
    )
