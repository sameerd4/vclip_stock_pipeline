from __future__ import annotations

from pathlib import Path

from vclip_pipeline.cli import main
from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.stockify.location_diagnose import (
    LocationDiagnosticsService,
    format_location_diagnostics_report,
)
from vclip_pipeline.util import json_dumps


def _seed_run(
    repository: CatalogRepository,
    *,
    run_id: str,
    library_name: str,
    capture_date: str,
    filename: str,
    media_path: str | None,
    srt_path: str | None,
    srt_match_method: str = "missing",
    srt_match_ambiguous: bool = False,
    srt_has_position: bool | None = None,
    location: dict | None = None,
    clip_count: int = 2,
) -> None:
    database = repository.database
    event_id = f"EVT_{run_id}"
    session_id = f"SESS_{run_id}"
    project_id = f"PROJ_{run_id}"
    media_id = f"MEDIA_{run_id}"
    stem = Path(filename).stem.lower()
    location = location or {"status": "unresolved", "evidence_sources": ["missing_srt_gps"]}
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO stockify_runs (
                id, source_xml_path, source_xml_sha256, output_xml_path, report_path,
                pipeline_version, status, options_json, started_at, completed_at
            ) VALUES (?, ?, 'abc', 'out.fcpxml', 'r.json', '0.1.0', 'complete', '{}', 't', 't')
            """,
            (run_id, f"/Volumes/Archive/{library_name}.fcpbundle/Info.fcpxml"),
        )
        connection.execute(
            """
            INSERT INTO processed_libraries (
                id, library_name, library_path, first_stockify_run_id, last_stockify_run_id,
                first_processed_at, last_processed_at
            ) VALUES (?, ?, ?, ?, ?, 't', 't')
            """,
            (
                f"LIB_{run_id}",
                f"{library_name}.fcpbundle",
                f"/Volumes/Archive/{library_name}.fcpbundle",
                run_id,
                run_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO source_events (id, run_id, source_index, source_name, source_uid)
            VALUES (?, ?, 0, 'Source Event', NULL)
            """,
            (event_id, run_id),
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
                ?, ?, ?, ?, ?, 'America/Los_Angeles',
                NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                'morning', 'medium', ?, 'Unknown Location Morning',
                'CLIP_0', 'not_enriched', ?, ?, 't', 't'
            )
            """,
            (
                session_id,
                run_id,
                f"unknown-{capture_date}",
                capture_date,
                f"{capture_date}T08:00:00",
                f"Unknown Location — {capture_date}",
                json_dumps({"status": "unknown"}),
                json_dumps({"date": capture_date}),
            ),
        )
        connection.execute(
            """
            INSERT INTO source_projects (
                id, run_id, source_event_id, source_index, source_name, source_uid,
                classification, session_id, accepted_clip_count, skipped_clip_count,
                generated_event_name, generated_project_label, created_at, updated_at
            ) VALUES (?, ?, ?, 0, 'Project', NULL, 'accepted', ?, ?, 0, ?, 'Unknown Location Morning', 't', 't')
            """,
            (
                project_id,
                run_id,
                event_id,
                session_id,
                clip_count,
                f"Unknown Location — {capture_date}",
            ),
        )
        connection.execute(
            """
            INSERT INTO source_media (
                id, run_id, asset_ref, asset_name, original_filename, media_path,
                normalized_stem, srt_path, srt_match_method, srt_match_confidence,
                srt_match_ambiguous, srt_has_position, location_json, created_at, updated_at
            ) VALUES (?, ?, 'r1', ?, ?, ?, ?, ?, ?, 'low', ?, ?, '{}', 't', 't')
            """,
            (
                media_id,
                run_id,
                filename,
                filename,
                media_path,
                stem,
                srt_path,
                srt_match_method,
                int(srt_match_ambiguous),
                None if srt_has_position is None else int(srt_has_position),
            ),
        )
        for index in range(clip_count):
            clip_id = f"CLIP_{run_id}_{index}"
            connection.execute(
                """
                INSERT INTO stock_candidates (
                    run_id, stock_clip_id, source_project_id, source_media_id, session_id,
                    source_segment_index, source_ref, source_name, eligibility_status,
                    rejection_reason, rejection_detail, original_start, original_duration,
                    original_duration_seconds, proposed_start, proposed_duration,
                    proposed_duration_seconds, short_clip_recovery, candidate_tier,
                    sidecar_path, srt_status, srt_window_status, srt_reasons_json,
                    visual_status, visual_reasons_json, visual_metrics_json, location_json,
                    capture_time_json, time_of_day_json, weather_json, creative_effects_json,
                    generated_event_name, generated_project_label, clip_sequence,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, NULL, ?, 'accepted',
                    NULL, NULL, '0s', '5s', 5.0, '0s', '5s', 5.0, NULL, 'primary',
                    ?, 'missing', 'missing', '[]', NULL, '[]', '{}', ?,
                    '{}', '{"label":"morning"}', '{}', '[]',
                    ?, 'Unknown Location Morning', ?, 't', 't'
                )
                """,
                (
                    run_id,
                    clip_id,
                    project_id,
                    media_id,
                    session_id,
                    index,
                    filename,
                    srt_path,
                    json_dumps(location),
                    f"Unknown Location — {capture_date}",
                    index + 1,
                ),
            )


def test_diagnose_locations_summary_and_actionable_media_hunt(tmp_path: Path):
    database = Database(tmp_path / "diag.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)

    present_media = tmp_path / "mounted" / "DJI_0001.MP4"
    present_media.parent.mkdir(parents=True)
    present_media.write_bytes(b"media")

    _seed_run(
        repository,
        run_id="STOCKIFY_MISSING",
        library_name="August 2026",
        capture_date="2026-08-01",
        filename="DJI_0999.MP4",
        media_path="/Volumes/Offsite/DJI_0999.MP4",
        srt_path=None,
        clip_count=3,
    )
    _seed_run(
        repository,
        run_id="STOCKIFY_SRT",
        library_name="June 2026 Part 2",
        capture_date="2026-06-19",
        filename="DJI_0001.MP4",
        media_path=str(present_media),
        srt_path=None,
        clip_count=2,
    )
    _seed_run(
        repository,
        run_id="STOCKIFY_PLACE",
        library_name="Nova",
        capture_date="2025-08-28",
        filename="DJI_0500.MP4",
        media_path=str(present_media),
        srt_path=str(tmp_path / "mounted" / "DJI_0500.SRT"),
        srt_match_method="exact_sibling",
        srt_has_position=True,
        location={
            "status": "gps_unresolved",
            "center_lat": 1.0,
            "center_lon": 2.0,
            "evidence_sources": ["srt_gps"],
        },
        clip_count=1,
    )
    # Plant SRT path as missing on disk for place-failed case; GPS already on candidate.
    # Create the SRT so reason becomes place lookup, not srt missing.
    (tmp_path / "mounted" / "DJI_0500.SRT").write_text("1\n", encoding="utf-8")

    class SeattleResolver:
        def resolve(self, latitude: float, longitude: float):
            if abs(latitude - 1.0) < 0.1 and abs(longitude - 2.0) < 0.1:
                return {
                    "city": "Nowhere",
                    "state": "Test",
                    "country": "Test",
                    "neighborhood": None,
                    "provider": "test",
                }
            return None

    report = LocationDiagnosticsService(
        repository,
        SeattleResolver(),
        scan_roots=[tmp_path / "mounted"],
    ).run()

    assert report.unknown_sessions == 3
    assert report.clips_affected == 6
    assert report.reason_counts["source_media_missing"]["sessions"] == 1
    assert report.reason_counts["source_media_missing"]["clips"] == 3
    assert report.reason_counts["srt_missing"]["sessions"] == 1
    assert report.reason_counts["srt_missing"]["clips"] == 2
    # Catalog GPS at 1,2 with no city on the candidate remains a place issue
    # unless a live SRT re-parse supplies a coherent flight place.
    assert (
        report.reason_counts.get("place_resolution_failed", {}).get("sessions", 0)
        + report.reason_counts.get("insufficient_evidence", {}).get("sessions", 0)
        + report.reason_counts.get("flight_gps_ready", {}).get("sessions", 0)
    ) >= 1

    lines = "\n".join(format_location_diagnostics_report(report))
    assert "LOCATION DIAGNOSTICS" in lines
    assert "Unknown sessions: 3" in lines
    assert "Clips affected:   6" in lines
    assert "Missing original media / sidecar" in lines
    assert "Original media found, SRT missing" in lines
    assert "BIGGEST UNKNOWN GROUPS" in lines
    assert "August 2026" in lines
    assert "MAY REQUIRE ANOTHER DRIVE / SD CARD / DRONE OFFLOAD" in lines
    assert "locate or offload original DJI files/SRTs" in lines
    assert "locate corresponding SRT sidecars" in lines
    # Place lookup failures are not "go find another drive" items.
    assert "Nova — 2025-08-28" not in lines.split("MAY REQUIRE ANOTHER DRIVE")[1]


def test_diagnose_locations_verbose_lists_filenames(tmp_path: Path):
    database = Database(tmp_path / "verbose.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    _seed_run(
        repository,
        run_id="STOCKIFY_V",
        library_name="Nova",
        capture_date="2025-09-24",
        filename="DJI_1234.MP4",
        media_path="/Volumes/Missing/DJI_1234.MP4",
        srt_path=None,
        clip_count=1,
    )
    report = LocationDiagnosticsService(
        repository,
        scan_roots=[tmp_path / "empty"],
    ).run(verbose=True)
    lines = "\n".join(format_location_diagnostics_report(report, verbose=True))
    assert "VERBOSE SESSION DETAIL" in lines
    assert "DJI_1234.MP4" in lines
    assert "source_media_missing" in lines


def test_diagnose_locations_cli(tmp_path: Path, capsys):
    database = Database(tmp_path / "cli.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    _seed_run(
        repository,
        run_id="STOCKIFY_CLI",
        library_name="Nova",
        capture_date="2025-09-24",
        filename="DJI_7777.MP4",
        media_path="/Volumes/Missing/DJI_7777.MP4",
        srt_path=None,
        clip_count=4,
    )
    code = main(
        [
            "diagnose-locations",
            "--db",
            str(tmp_path / "cli.sqlite3"),
            "--scan",
            str(tmp_path / "empty-scan"),
            "--quiet",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "LOCATION DIAGNOSTICS" in out
    assert "Unknown sessions: 1" in out
    assert "Clips affected:   4" in out
