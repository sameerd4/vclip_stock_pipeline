from __future__ import annotations

from pathlib import Path

from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.stockify.flight_location import (
    FlightIdentity,
    TrajectorySample,
    resolve_flight_trajectory,
)
from vclip_pipeline.stockify.location_recovery import LocationRecoveryService
from vclip_pipeline.stockify.sidecars import parse_srt_info
from vclip_pipeline.util import json_dumps


class FremontResolver:
    """Neighborhood-aware stub around Fremont, California."""

    def resolve(self, latitude: float, longitude: float):
        if abs(latitude - 37.54) < 0.08 and abs(longitude + 121.94) < 0.08:
            if latitude >= 37.545:
                neighborhood = "Mission San Jose"
            elif latitude <= 37.530:
                neighborhood = "Warm Springs"
            else:
                neighborhood = "Central Fremont"
            return {
                "provider": "test",
                "country": "United States",
                "state": "California",
                "city": "Fremont",
                "neighborhood": neighborhood,
                "poi": None,
                "timezone": "America/Los_Angeles",
            }
        if abs(latitude - 37.77) < 0.05 and abs(longitude + 122.42) < 0.05:
            return {
                "provider": "test",
                "country": "United States",
                "state": "California",
                "city": "San Francisco",
                "neighborhood": "Mission District",
                "poi": None,
                "timezone": "America/Los_Angeles",
            }
        return None


def _sample(lat: float, lon: float, source: str, clip: str | None = None) -> TrajectorySample:
    return TrajectorySample(
        latitude=lat,
        longitude=lon,
        source_key=source,
        stock_clip_id=clip,
        sample_count=5,
        source="srt_full",
        filename=f"{source}.MP4",
    )


def test_several_kilometer_move_within_city_resolves_to_city():
    samples = [
        _sample(37.520, -121.950, "rec_a", "CLIP_A"),
        _sample(37.560, -121.920, "rec_b", "CLIP_B"),
    ]
    result = resolve_flight_trajectory(
        samples,
        FremontResolver(),
        identity=FlightIdentity(run_id="R", session_id="S"),
    )
    assert result.status == "resolved"
    assert result.coherence == "city"
    assert result.location is not None
    assert result.location["city"] == "Fremont"
    assert result.location["neighborhood"] is None
    assert "Fremont" in str(result.location["public_label"])


def test_multiple_neighborhoods_same_city_resolves_to_city():
    samples = [
        _sample(37.548, -121.935, "rec_msj", "CLIP_1"),
        _sample(37.538, -121.938, "rec_central", "CLIP_2"),
        _sample(37.525, -121.945, "rec_ws", "CLIP_3"),
    ]
    result = resolve_flight_trajectory(samples, FremontResolver())
    assert result.status == "resolved"
    assert result.coherence == "city"
    assert result.location is not None
    assert result.location["city"] == "Fremont"
    assert result.location["neighborhood"] is None
    assert len(result.place_support) >= 2


def test_same_neighborhood_keeps_neighborhood():
    samples = [
        _sample(37.548, -121.935, "rec_a", "CLIP_A"),
        _sample(37.549, -121.934, "rec_b", "CLIP_B"),
    ]
    result = resolve_flight_trajectory(samples, FremontResolver())
    assert result.status == "resolved"
    assert result.coherence == "neighborhood"
    assert result.location is not None
    assert result.location["neighborhood"] == "Mission San Jose"


def test_different_cities_remain_conflict():
    samples = [
        _sample(37.538, -121.938, "rec_fremont", "CLIP_A"),
        _sample(37.770, -122.420, "rec_sf", "CLIP_B"),
    ]
    result = resolve_flight_trajectory(samples, FremontResolver())
    assert result.status == "conflict"
    assert result.coherence == "conflict"
    assert result.location is None
    cities = {item.city for item in result.place_support}
    assert cities == {"Fremont", "San Francisco"}


def _write_srt(path: Path, points: list[tuple[str, float, float]]) -> None:
    chunks: list[str] = []
    for index, (clock, lat, lon) in enumerate(points, start=1):
        chunks.extend(
            [
                str(index),
                f"{clock} --> {clock}",
                f"[latitude: {lat:.6f}] [longitude: {lon:.6f}]",
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(chunks), encoding="utf-8")


def _seed_flight_session(
    repository: CatalogRepository,
    *,
    run_id: str,
    srt_points: list[tuple[str, float, float]],
    proposed_duration: str = "1s",
) -> Path:
    database = repository.database
    srt = Path(database.path).parent / f"{run_id}.SRT"
    _write_srt(srt, srt_points)
    review = Path(database.path).parent / f"{run_id}-review.fcpxml"
    review.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.12"><library><event name="Unknown Location — 2026-08-01">
<project name="Unknown Location Evening — Clip 01"/>
</event></library></fcpxml>""",
        encoding="utf-8",
    )
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO stockify_runs (
                id, source_xml_path, source_xml_sha256, output_xml_path, report_path,
                pipeline_version, status, options_json, started_at, completed_at
            ) VALUES (?, 'a.xml', 'h', ?, 'r.json', '0.1.0', 'complete', '{}', 't', 't')
            """,
            (run_id, str(review)),
        )
        connection.execute(
            """
            INSERT INTO source_events (id, run_id, source_index, source_name, source_uid)
            VALUES (?, ?, 0, 'Event', NULL)
            """,
            (f"EVT_{run_id}", run_id),
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
                ?, ?, 'k', '2026-08-01', '2026-08-01T20:01:51', 'America/Los_Angeles',
                NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                'evening', 'medium', 'Unknown Location — 2026-08-01',
                'Unknown Location Evening', 'CLIP_1', 'not_enriched',
                '{"status":"unknown"}', '{"date":"2026-08-01"}', 't', 't'
            )
            """,
            (f"SESS_{run_id}", run_id),
        )
        connection.execute(
            """
            INSERT INTO source_projects (
                id, run_id, source_event_id, source_index, source_name, source_uid,
                classification, session_id, accepted_clip_count, skipped_clip_count,
                generated_event_name, generated_project_label, created_at, updated_at
            ) VALUES (?, ?, ?, 0, 'Project', NULL, 'accepted', ?, 1, 0,
                      'Unknown Location — 2026-08-01', 'Unknown Location Evening', 't', 't')
            """,
            (f"PROJ_{run_id}", run_id, f"EVT_{run_id}", f"SESS_{run_id}"),
        )
        connection.execute(
            """
            INSERT INTO source_media (
                id, run_id, asset_ref, original_filename, media_path, normalized_stem,
                srt_path, srt_match_method, srt_match_ambiguous, srt_has_position,
                location_json, created_at, updated_at
            ) VALUES (?, ?, 'r1', 'DJI_FLIGHT.MP4', ?, 'dji_flight', ?, 'exact_sibling',
                      0, 0, '{}', 't', 't')
            """,
            (f"MEDIA_{run_id}", run_id, str(srt.with_suffix(".MP4")), str(srt)),
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
                ?, 'CLIP_1', ?, ?, ?, 0, 'DJI_FLIGHT.MP4', 'accepted',
                '0s', ?, 1.0, '0s', ?, 1.0, ?, '[]', '[]', '{}',
                ?, '{}', '{"label":"evening"}', '{}', '[]',
                'Unknown Location — 2026-08-01', 'Unknown Location Evening',
                'Unknown Location Evening — Clip 01', 1, 't', 't'
            )
            """,
            (
                run_id,
                f"PROJ_{run_id}",
                f"MEDIA_{run_id}",
                f"SESS_{run_id}",
                proposed_duration,
                proposed_duration,
                str(srt),
                json_dumps({"status": "unresolved", "evidence_sources": ["missing_srt_gps"]}),
            ),
        )
    return srt


def test_zero_gps_beginning_inherits_flight_location(tmp_path: Path):
    database = Database(tmp_path / "zero.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    _seed_flight_session(
        repository,
        run_id="STOCKIFY_ZERO",
        srt_points=[
            ("00:00:00,000", 0.0, 0.0),
            ("00:00:00,033", 0.0, 0.0),
            ("00:00:02,000", 37.538005, -121.938496),
            ("00:00:02,033", 37.538015, -121.938486),
        ],
        proposed_duration="1s",  # window is only zeros
    )
    report = LocationRecoveryService(repository, FremontResolver()).run(
        run_id="STOCKIFY_ZERO",
        dry_run=False,
        rewrite_review_xml=False,
        report_path=None,
    )
    assert report.resolved_by_srt_consensus == 1
    session = repository.sessions_for_run("STOCKIFY_ZERO")[0]
    assert session["city"] == "Fremont"
    candidate = repository.candidates_for_run("STOCKIFY_ZERO", accepted_only=True)[0]
    assert candidate["location"]["city"] == "Fremont"
    assert "flight_trajectory" in (candidate["location"].get("evidence_sources") or [])


def test_clip_without_window_gps_inherits_flight_location(tmp_path: Path):
    database = Database(tmp_path / "inherit.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    srt = _seed_flight_session(
        repository,
        run_id="STOCKIFY_INHERIT",
        srt_points=[
            ("00:00:00,000", 0.0, 0.0),
            ("00:00:05,000", 37.538005, -121.938496),
        ],
        proposed_duration="1s",
    )
    # Prove the parser sees no usable GPS in the early window alone.
    info = parse_srt_info(srt)
    from fractions import Fraction

    from vclip_pipeline.stockify.metadata import extract_gps_summary

    window_only = extract_gps_summary(
        info,
        start=Fraction(0),
        duration=Fraction(1),
        allow_full_sidecar_fallback=False,
    )
    assert window_only is None

    report = LocationRecoveryService(repository, FremontResolver()).run(
        run_id="STOCKIFY_INHERIT",
        dry_run=False,
        rewrite_review_xml=True,
        report_path=None,
    )
    assert report.resolved_by_srt_consensus == 1
    candidate = repository.candidates_for_run("STOCKIFY_INHERIT", accepted_only=True)[0]
    assert candidate["location"]["city"] == "Fremont"
    assert candidate["location"]["recovery"]["inherited"] is True


def test_recovery_finds_srt_via_volume_scan(tmp_path: Path):
    """Catalog may lack sidecar_path; offload folder SRT still recovers the flight."""
    database = Database(tmp_path / "volume.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    offload = tmp_path / "Volumes" / "drone" / "august"
    offload.mkdir(parents=True)
    srt = offload / "DJI_FLIGHT.SRT"
    _write_srt(
        srt,
        [
            ("00:00:00,000", 0.0, 0.0),
            ("00:00:02,000", 37.538005, -121.938496),
        ],
    )
    # Seed with a catalog sidecar path that does not exist, then clear it so
    # recovery must discover the offload SRT via scan_roots.
    _seed_flight_session(
        repository,
        run_id="STOCKIFY_VOL",
        srt_points=[("00:00:02,000", 37.538005, -121.938496)],
        proposed_duration="1s",
    )
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE source_media
            SET srt_path = NULL, srt_match_method = 'missing', srt_has_position = 0
            WHERE run_id = 'STOCKIFY_VOL'
            """
        )
        connection.execute(
            """
            UPDATE stock_candidates
            SET sidecar_path = NULL
            WHERE run_id = 'STOCKIFY_VOL'
            """
        )
        # Move the seeded SRT into the scanned offload tree under the same stem.
        seeded = tmp_path / "DJI_FLIGHT.SRT"
        if seeded.exists():
            seeded.unlink()
        # _seed_flight_session writes next to media under tmp_path; relocate.
        for path in tmp_path.rglob("*.SRT"):
            if path != srt:
                path.unlink()

    report = LocationRecoveryService(
        repository,
        FremontResolver(),
        scan_roots=[tmp_path / "Volumes"],
    ).run(
        run_id="STOCKIFY_VOL",
        dry_run=False,
        rewrite_review_xml=False,
        report_path=None,
    )
    assert report.resolved_by_srt_consensus == 1
    session = repository.sessions_for_run("STOCKIFY_VOL")[0]
    assert session["city"] == "Fremont"


def test_recovery_keeps_genuine_city_conflict(tmp_path: Path):
    database = Database(tmp_path / "conflict.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    srt_a = tmp_path / "a.SRT"
    srt_b = tmp_path / "b.SRT"
    _write_srt(srt_a, [("00:00:01,000", 37.538005, -121.938496)])
    _write_srt(srt_b, [("00:00:01,000", 37.770000, -122.420000)])
    review = tmp_path / "review.fcpxml"
    review.write_text("<fcpxml><library><event name='Unknown Location — 2026-08-01'/></library></fcpxml>")
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO stockify_runs (
                id, source_xml_path, source_xml_sha256, output_xml_path, report_path,
                pipeline_version, status, options_json, started_at, completed_at
            ) VALUES ('STOCKIFY_C', 'a.xml', 'h', ?, 'r.json', '0.1.0', 'complete', '{}', 't', 't')
            """,
            (str(review),),
        )
        connection.execute(
            "INSERT INTO source_events (id, run_id, source_index, source_name, source_uid) "
            "VALUES ('EVT_C', 'STOCKIFY_C', 0, 'Event', NULL)"
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
                'SESS_C', 'STOCKIFY_C', 'k', '2026-08-01', '2026-08-01T12:00:00',
                'America/Los_Angeles', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                'midday', 'medium', 'Unknown Location — 2026-08-01', 'Unknown Location Midday',
                'CLIP_A', 'not_enriched', '{"status":"unknown"}', '{"date":"2026-08-01"}', 't', 't'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO source_projects (
                id, run_id, source_event_id, source_index, source_name, source_uid,
                classification, session_id, accepted_clip_count, skipped_clip_count,
                created_at, updated_at
            ) VALUES ('PROJ_C', 'STOCKIFY_C', 'EVT_C', 0, 'Project', NULL, 'accepted',
                      'SESS_C', 2, 0, 't', 't')
            """
        )
        for media_id, filename, srt in (
            ("MEDIA_A", "A.MP4", srt_a),
            ("MEDIA_B", "B.MP4", srt_b),
        ):
            connection.execute(
                """
                INSERT INTO source_media (
                    id, run_id, asset_ref, original_filename, media_path, normalized_stem,
                    srt_path, srt_match_method, srt_match_ambiguous, location_json,
                    created_at, updated_at
                ) VALUES (?, 'STOCKIFY_C', ?, ?, ?, ?, ?, 'exact_sibling', 0, '{}', 't', 't')
                """,
                (media_id, media_id, filename, str(srt.with_suffix(".MP4")), filename.lower(), str(srt)),
            )
        for index, (clip_id, media_id, srt) in enumerate(
            (("CLIP_A", "MEDIA_A", srt_a), ("CLIP_B", "MEDIA_B", srt_b))
        ):
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
                    'STOCKIFY_C', ?, 'PROJ_C', ?, 'SESS_C', ?, ?, 'accepted',
                    '0s', '5s', 5.0, '0s', '5s', 5.0, ?, '[]', '[]', '{}',
                    '{"status":"unresolved"}', '{}', '{}', '{}', '[]', 't', 't'
                )
                """,
                (clip_id, media_id, index, f"{clip_id}.MP4", str(srt)),
            )

    report = LocationRecoveryService(repository, FremontResolver()).run(
        run_id="STOCKIFY_C",
        dry_run=False,
        rewrite_review_xml=False,
        report_path=None,
    )
    assert report.resolved_by_srt_consensus == 0
    assert report.still_unknown == 1
    assert report.sessions[0]["reason"] == "conflicting_gps"
    assert report.sessions[0]["trajectory"]["coherence"] == "conflict"
