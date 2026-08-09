from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.stockify.location_diagnose import LocationDiagnosticsService
from vclip_pipeline.stockify.location_recovery import LocationRecoveryService
from vclip_pipeline.stockify.metadata import (
    extract_gps_summary,
    is_usable_gps,
    summarize_gps_samples,
)
from vclip_pipeline.stockify.models import SrtSample
from vclip_pipeline.stockify.sidecars import parse_srt_info
from vclip_pipeline.util import json_dumps


def _write_dji_srt(path: Path, points: list[tuple[str, float, float]]) -> None:
    chunks: list[str] = []
    for index, (clock, lat, lon) in enumerate(points, start=1):
        chunks.extend(
            [
                str(index),
                f"{clock} --> {clock}",
                (
                    f"2026-08-01 20:01:51.{index:03d} "
                    f"[latitude: {lat:.6f}] [longitude: {lon:.6f}] "
                    f"rel_alt: 40.0"
                ),
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(chunks), encoding="utf-8")


class BayAreaResolver:
    def resolve(self, latitude: float, longitude: float):
        if abs(latitude - 37.538) < 0.05 and abs(longitude + 121.938) < 0.05:
            return {
                "provider": "test",
                "country": "United States",
                "state": "California",
                "city": "Fremont",
                "neighborhood": None,
                "poi": None,
                "timezone": "America/Los_Angeles",
            }
        return None


def test_is_usable_gps_rejects_null_island():
    assert is_usable_gps(0.0, 0.0) is False
    assert is_usable_gps(0.000000, 0.000000) is False
    assert is_usable_gps(37.538005, -121.938496) is True
    assert is_usable_gps(0.0, -121.938496) is True


def test_parse_srt_ignores_leading_zero_gps_and_keeps_later_valid(tmp_path: Path):
    path = tmp_path / "DJI_20260801200151_0001_D.SRT"
    _write_dji_srt(
        path,
        [
            ("00:00:00,000", 0.0, 0.0),
            ("00:00:00,033", 0.0, 0.0),
            ("00:00:00,066", 0.0, 0.0),
            ("00:00:01,000", 37.538005, -121.938496),
            ("00:00:01,033", 37.538010, -121.938490),
            ("00:00:01,066", 37.538020, -121.938480),
        ],
    )
    info = parse_srt_info(path)
    assert info.has_position is True
    assert info.sample_count == 6
    summary = extract_gps_summary(info)
    assert summary is not None
    assert summary["valid_sample_count"] == 3
    assert abs(float(summary["center_lat"]) - 37.53801) < 0.0001
    assert abs(float(summary["center_lon"]) + 121.93849) < 0.0001


def test_parse_srt_only_zero_gps_has_no_usable_position(tmp_path: Path):
    path = tmp_path / "zeros.SRT"
    _write_dji_srt(
        path,
        [
            ("00:00:00,000", 0.0, 0.0),
            ("00:00:00,033", 0.0, 0.0),
            ("00:00:00,066", 0.0, 0.0),
        ],
    )
    info = parse_srt_info(path)
    assert info.has_position is False
    assert extract_gps_summary(info) is None


def test_summarize_gps_uses_median_of_multiple_valid_points():
    samples = [
        SrtSample(time=Fraction(0), latitude=37.0, longitude=-121.0),
        SrtSample(time=Fraction(1), latitude=37.2, longitude=-121.2),
        SrtSample(time=Fraction(2), latitude=37.4, longitude=-121.4),
        SrtSample(time=Fraction(3), latitude=0.0, longitude=0.0),
    ]
    summary = summarize_gps_samples(samples)
    assert summary is not None
    assert summary["valid_sample_count"] == 3
    assert summary["center_lat"] == 37.2
    assert summary["center_lon"] == -121.2


def test_window_fallback_uses_later_valid_gps_outside_early_zero_window(tmp_path: Path):
    path = tmp_path / "window.SRT"
    _write_dji_srt(
        path,
        [
            ("00:00:00,000", 0.0, 0.0),
            ("00:00:00,500", 0.0, 0.0),
            ("00:00:05,000", 37.538005, -121.938496),
        ],
    )
    info = parse_srt_info(path)
    # Early window contains only zeros; shared extractor must fall back to full SRT.
    summary = extract_gps_summary(
        info,
        start=Fraction(0),
        duration=Fraction(1),
        allow_full_sidecar_fallback=True,
    )
    assert summary is not None
    assert float(summary["center_lat"]) == 37.538005


def test_recover_locations_resolves_session_from_later_valid_srt_gps(tmp_path: Path):
    database = Database(tmp_path / "recover-gps.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    srt = tmp_path / "media" / "DJI_20260801200151_0001_D.SRT"
    _write_dji_srt(
        srt,
        [
            ("00:00:00,000", 0.0, 0.0),
            ("00:00:00,033", 0.0, 0.0),
            ("00:00:02,000", 37.538005, -121.938496),
            ("00:00:02,033", 37.538015, -121.938486),
        ],
    )
    review = tmp_path / "review.fcpxml"
    review.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.12">
  <library>
    <event name="Unknown Location — 2026-08-01">
      <project name="Unknown Location Morning — Clip 01"/>
    </event>
  </library>
</fcpxml>
""",
        encoding="utf-8",
    )
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO stockify_runs (
                id, source_xml_path, source_xml_sha256, output_xml_path, report_path,
                pipeline_version, status, options_json, started_at, completed_at
            ) VALUES ('STOCKIFY_AUG', 'a.xml', 'h', ?, 'r.json', '0.1.0', 'complete', '{}', 't', 't')
            """,
            (str(review),),
        )
        connection.execute(
            """
            INSERT INTO source_events (id, run_id, source_index, source_name, source_uid)
            VALUES ('EVT_AUG', 'STOCKIFY_AUG', 0, 'August Shoot', NULL)
            """
        )
        connection.execute(
            """
            INSERT INTO shoot_sessions (
                id, run_id, session_key, capture_date, captured_at_local, timezone,
                center_lat, center_lon, gps_radius_meters, country, state, city,
                neighborhood, poi, public_label, location_confidence, time_of_day,
                time_of_day_confidence, generated_event_name, generated_base_label,
                anchor_stock_clip_id, weather_status, location_json, capture_json,
                created_at, updated_at
            ) VALUES (
                'SESS_AUG', 'STOCKIFY_AUG', 'aug-1', '2026-08-01', '2026-08-01T20:01:51',
                'America/Los_Angeles', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                'evening', 'medium', 'Unknown Location — 2026-08-01', 'Unknown Location Evening',
                'CLIP_AUG', 'not_enriched', ?, ?, 't', 't'
            )
            """,
            (
                json_dumps({"status": "unknown"}),
                json_dumps({"date": "2026-08-01"}),
            ),
        )
        connection.execute(
            """
            INSERT INTO source_projects (
                id, run_id, source_event_id, source_index, source_name, source_uid,
                classification, session_id, accepted_clip_count, skipped_clip_count,
                generated_event_name, generated_project_label, created_at, updated_at
            ) VALUES (
                'PROJ_AUG', 'STOCKIFY_AUG', 'EVT_AUG', 0, 'Project', NULL, 'accepted',
                'SESS_AUG', 1, 0, 'Unknown Location — 2026-08-01',
                'Unknown Location Evening', 't', 't'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO source_media (
                id, run_id, asset_ref, original_filename, media_path, normalized_stem,
                srt_path, srt_match_method, srt_match_ambiguous, srt_has_position,
                location_json, created_at, updated_at
            ) VALUES (
                'MEDIA_AUG', 'STOCKIFY_AUG', 'r1', 'DJI_20260801200151_0001_D.MP4',
                ?, 'dji_20260801200151_0001_d', ?, 'exact_sibling', 0, 0,
                '{}', 't', 't'
            )
            """,
            (str(srt.with_suffix(".MP4")), str(srt)),
        )
        connection.execute(
            """
            INSERT INTO stock_candidates (
                run_id, stock_clip_id, source_project_id, source_media_id, session_id,
                source_segment_index, source_name, eligibility_status,
                original_start, original_duration, original_duration_seconds,
                proposed_start, proposed_duration, proposed_duration_seconds,
                sidecar_path, srt_reasons_json, visual_reasons_json, visual_metrics_json,
                location_json, capture_time_json, time_of_day_json, weather_json,
                creative_effects_json, generated_event_name, generated_project_label,
                generated_clip_project_name, clip_sequence, created_at, updated_at
            ) VALUES (
                'STOCKIFY_AUG', 'CLIP_AUG', 'PROJ_AUG', 'MEDIA_AUG', 'SESS_AUG', 0,
                'DJI_20260801200151_0001_D.MP4', 'accepted',
                '0s', '1s', 1.0, '0s', '1s', 1.0,
                ?, '[]', '[]', '{}',
                ?, '{}', '{"label":"evening"}', '{}', '[]',
                'Unknown Location — 2026-08-01', 'Unknown Location Evening',
                'Unknown Location Evening — Clip 01', 1, 't', 't'
            )
            """,
            (
                str(srt),
                # Stale catalog row: window-only zeros previously looked GPS-less.
                json_dumps({"status": "unresolved", "evidence_sources": ["missing_srt_gps"]}),
            ),
        )

    report = LocationRecoveryService(repository, BayAreaResolver()).run(
        run_id=None,
        dry_run=False,
        rewrite_review_xml=True,
        report_path=None,
    )
    assert report.stockify_runs_scanned == 1
    assert report.resolved_by_srt_consensus == 1
    session = repository.sessions_for_run("STOCKIFY_AUG")[0]
    assert session["city"] == "Fremont"
    assert "Unknown Location" not in session["generated_event_name"]
    assert session["location"]["sample_count"] >= 2


def test_diagnose_does_not_mark_zero_then_valid_srt_as_without_gps(tmp_path: Path):
    database = Database(tmp_path / "diag-gps.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    srt = tmp_path / "mounted" / "DJI_20260801200151_0001_D.SRT"
    _write_dji_srt(
        srt,
        [
            ("00:00:00,000", 0.0, 0.0),
            ("00:00:00,033", 0.0, 0.0),
            ("00:00:01,000", 37.538005, -121.938496),
        ],
    )
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO stockify_runs (
                id, source_xml_path, source_xml_sha256, output_xml_path, report_path,
                pipeline_version, status, options_json, started_at, completed_at
            ) VALUES ('STOCKIFY_D', 'a.xml', 'h', 'o.xml', 'r.json', '0.1.0', 'complete', '{}', 't', 't')
            """
        )
        connection.execute(
            """
            INSERT INTO processed_libraries (
                id, library_name, library_path, first_stockify_run_id, last_stockify_run_id,
                first_processed_at, last_processed_at
            ) VALUES ('LIB_D', 'August 2026.fcpbundle', '/tmp/August 2026.fcpbundle',
                      'STOCKIFY_D', 'STOCKIFY_D', 't', 't')
            """
        )
        connection.execute(
            """
            INSERT INTO source_events (id, run_id, source_index, source_name, source_uid)
            VALUES ('EVT_D', 'STOCKIFY_D', 0, 'Event', NULL)
            """
        )
        connection.execute(
            """
            INSERT INTO shoot_sessions (
                id, run_id, session_key, capture_date, captured_at_local, timezone,
                center_lat, center_lon, gps_radius_meters, country, state, city,
                neighborhood, poi, public_label, location_confidence, time_of_day,
                time_of_day_confidence, generated_event_name, generated_base_label,
                anchor_stock_clip_id, weather_status, location_json, capture_json,
                created_at, updated_at
            ) VALUES (
                'SESS_D', 'STOCKIFY_D', 'd', '2026-08-01', '2026-08-01T20:01:51',
                'America/Los_Angeles', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                'evening', 'medium', 'Unknown Location — 2026-08-01', 'Unknown Location Evening',
                'CLIP_D', 'not_enriched', '{"status":"unknown"}', '{"date":"2026-08-01"}', 't', 't'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO source_projects (
                id, run_id, source_event_id, source_index, source_name, source_uid,
                classification, session_id, accepted_clip_count, skipped_clip_count,
                created_at, updated_at
            ) VALUES ('PROJ_D', 'STOCKIFY_D', 'EVT_D', 0, 'Project', NULL, 'accepted',
                      'SESS_D', 1, 0, 't', 't')
            """
        )
        connection.execute(
            """
            INSERT INTO source_media (
                id, run_id, asset_ref, original_filename, media_path, normalized_stem,
                srt_path, srt_match_method, srt_match_ambiguous, srt_has_position,
                location_json, created_at, updated_at
            ) VALUES (
                'MEDIA_D', 'STOCKIFY_D', 'r1', 'DJI_20260801200151_0001_D.MP4',
                ?, 'dji_20260801200151_0001_d', ?, 'exact_sibling', 0, 0,
                '{}', 't', 't'
            )
            """,
            (str(srt.with_suffix(".MP4")), str(srt)),
        )
        srt.with_suffix(".MP4").write_bytes(b"media")
        connection.execute(
            """
            INSERT INTO stock_candidates (
                run_id, stock_clip_id, source_project_id, source_media_id, session_id,
                source_segment_index, source_name, eligibility_status,
                original_start, original_duration, original_duration_seconds,
                proposed_start, proposed_duration, proposed_duration_seconds,
                sidecar_path, srt_reasons_json, visual_reasons_json, visual_metrics_json,
                location_json, capture_time_json, time_of_day_json, weather_json,
                creative_effects_json, created_at, updated_at
            ) VALUES (
                'STOCKIFY_D', 'CLIP_D', 'PROJ_D', 'MEDIA_D', 'SESS_D', 0,
                'DJI_20260801200151_0001_D.MP4', 'accepted',
                '0s', '1s', 1.0, '0s', '1s', 1.0,
                ?, '[]', '[]', '{}',
                '{"status":"unresolved","evidence_sources":["missing_srt_gps"]}',
                '{}', '{}', '{}', '[]', 't', 't'
            )
            """,
            (str(srt),),
        )

    class BayResolver:
        def resolve(self, latitude: float, longitude: float):
            if abs(latitude - 37.538) < 0.05 and abs(longitude + 121.938) < 0.05:
                return {
                    "city": "Fremont",
                    "state": "California",
                    "country": "United States",
                    "neighborhood": "Central Fremont",
                    "provider": "test",
                }
            return None

    report = LocationDiagnosticsService(
        repository,
        BayResolver(),
        scan_roots=[tmp_path / "mounted"],
    ).run()
    assert report.unknown_sessions == 1
    assert report.reason_counts["srt_without_gps"]["sessions"] == 0
    # Live SRT GPS is coherent; remaining unknown is not "no usable GPS".
    assert report.sessions[0]["reason_code"] in {
        "flight_gps_ready",
        "insufficient_evidence",
        "place_resolution_failed",
    }
