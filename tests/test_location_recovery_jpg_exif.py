from __future__ import annotations

from pathlib import Path

from test_jpg_exif_same_shoot import _build_jpeg_with_gps
from test_location_recovery import _catalog_resolver, _write_unknown_review_xml

from vclip_pipeline.cli import main
from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.stockify.jpg_exif_same_shoot import EVIDENCE_SOURCE
from vclip_pipeline.stockify.location_diagnose import LocationDiagnosticsService
from vclip_pipeline.stockify.location_recovery import LocationRecoveryService
from vclip_pipeline.util import json_dumps

CAPITOL_LAT = 47.6231
CAPITOL_LON = -122.3165
VIDEO = "DJI_20251108214016_0580_D.MP4"
SCREEN = "ScreenRecording_11-07-2025 15-00-00.MP4"


def _seed_dji_unknown(
    repository: CatalogRepository,
    *,
    run_id: str = "STOCKIFY_JPG",
    session_id: str = "SESS_JPG",
    output_xml: Path | None = None,
    media_root: Path,
    sources: list[dict],
    clip_location: dict | None = None,
    capture_date: str = "2025-11-08",
) -> str:
    database = repository.database
    output = str(output_xml or f"{run_id}-review.fcpxml")
    event_id = f"EVT_{run_id}"
    project_id = f"PROJ_{run_id}"
    location = clip_location or {"status": "unknown"}
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO stockify_runs (
                id, source_xml_path, source_xml_sha256, output_xml_path, report_path,
                pipeline_version, status, options_json, started_at, completed_at
            ) VALUES (?, ?, 'abc', ?, 'report.json', '0.1.0', 'complete', '{}', 't', 't')
            """,
            (run_id, f"{run_id}.xml", output),
        )
        connection.execute(
            """
            INSERT INTO source_events (id, run_id, source_index, source_name, source_uid)
            VALUES (?, ?, 0, 'Hours in Silence', NULL)
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
                'night', 'medium', ?, 'Unknown Location Night',
                'CLIP_0', 'not_enriched', ?, ?, 't', 't'
            )
            """,
            (
                session_id,
                run_id,
                f"unknown-{capture_date}-{run_id}",
                capture_date,
                f"{capture_date}T21:40:16",
                f"Unknown Location — {capture_date}",
                json_dumps({"status": "unknown"}),
                json_dumps({"date": capture_date, "captured_at_local": f"{capture_date}T21:40:16"}),
            ),
        )
        connection.execute(
            """
            INSERT INTO source_projects (
                id, run_id, source_event_id, source_index, source_name, source_uid,
                classification, session_id, accepted_clip_count, skipped_clip_count,
                generated_event_name, generated_project_label, created_at, updated_at
            ) VALUES (?, ?, ?, 0, 'Hours in Silence', NULL, 'accepted', ?, ?, 0,
                      ?, 'Unknown Location Night', 't', 't')
            """,
            (
                project_id,
                run_id,
                event_id,
                session_id,
                len(sources),
                f"Unknown Location — {capture_date}",
            ),
        )
        for index, source in enumerate(sources):
            filename = source["filename"]
            media_id = f"MEDIA_{run_id}_{index}"
            media_path = source.get("media_path") or str(media_root / filename)
            Path(media_path).parent.mkdir(parents=True, exist_ok=True)
            if not Path(media_path).exists():
                Path(media_path).write_bytes(b"not-a-real-video")
            connection.execute(
                """
                INSERT INTO source_media (
                    id, run_id, asset_ref, original_filename, media_path,
                    normalized_stem, srt_path, srt_match_method, srt_match_ambiguous,
                    srt_has_position, location_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'missing', 0, 0, '{}', 't', 't')
                """,
                (
                    media_id,
                    run_id,
                    f"r{index}",
                    filename,
                    media_path,
                    Path(filename).stem.lower(),
                    source.get("srt_path"),
                ),
            )
            clip_id = source.get("clip_id") or f"CLIP_{run_id}_{index}"
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
                    ?, ?, ?, ?, ?, ?, ?, 'accepted',
                    '0s', '8s', 8.0, '0s', '8s', 8.0,
                    ?, '[]', '[]', '{}', ?, '{}', '{"label":"night"}', '{}', '[]',
                    ?, 'Unknown Location Night', ?, ?, 't', 't'
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
                    source.get("srt_path"),
                    json_dumps(source.get("location") or location),
                    f"Unknown Location — {capture_date}",
                    f"Unknown Location Night — Clip {index + 1:02d}",
                    index + 1,
                ),
            )
    return session_id


def _write_jpg(path: Path, *, seq: str, lat: float = CAPITOL_LAT, lon: float = CAPITOL_LON) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_build_jpeg_with_gps(latitude=lat, longitude=lon))


def _service(repository, tmp_path: Path, **kwargs) -> LocationRecoveryService:
    return LocationRecoveryService(
        repository,
        _catalog_resolver(repository),
        scan_roots=[tmp_path],
        **kwargs,
    )


def test_adjacent_same_shoot_jpg_recovers_unknown_session(tmp_path: Path):
    database = Database(tmp_path / "jpg.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    review = tmp_path / "review.fcpxml"
    _write_unknown_review_xml(review, "2025-11-08")
    media = tmp_path / "media"
    _write_jpg(media / "DJI_20251108213954_0578_D.JPG", seq="0578")
    _write_jpg(media / "DJI_20251108214100_0582_D.JPG", seq="0582")
    session_id = _seed_dji_unknown(
        repository,
        output_xml=review,
        media_root=media,
        sources=[{"filename": VIDEO}],
    )

    report = _service(repository, media).run(
        run_id="STOCKIFY_JPG",
        dry_run=False,
        rewrite_review_xml=True,
        report_path=None,
    )
    assert report.unknown_sessions_before == 1
    assert report.resolved_by_srt_consensus == 1
    assert report.resolved_by_jpg_exif == 1
    session = repository.sessions_for_run("STOCKIFY_JPG")[0]
    assert session["id"] == session_id
    assert session["city"] == "Seattle"
    assert EVIDENCE_SOURCE in session["location"]["evidence_sources"]
    assert session["location"]["gps_kind"] == "inferred_jpg_exif_same_shoot"
    assert session["location"]["direct_source_gps"] is False
    assert session["location"]["jpg_exif_same_shoot"]["inferences"]
    assert session["location"]["jpg_exif_same_shoot"]["inferences"][0]["confidence"] == "high"
    candidate = repository.candidates_for_run("STOCKIFY_JPG", accepted_only=True)[0]
    assert candidate["location"]["gps_kind"] == "inferred_jpg_exif_same_shoot"
    assert "jpg_exif_same_shoot" in candidate["location"]["evidence_sources"]


def test_bracketing_jpgs_recover_session(tmp_path: Path):
    database = Database(tmp_path / "bracket.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    media = tmp_path / "media"
    _write_jpg(media / "DJI_20251108213500_0570_D.JPG", seq="0570")
    _write_jpg(media / "DJI_20251108214500_0590_D.JPG", seq="0590")
    _seed_dji_unknown(repository, media_root=media, sources=[{"filename": VIDEO}])
    report = _service(repository, media).run(
        run_id="STOCKIFY_JPG",
        dry_run=False,
        rewrite_review_xml=False,
        report_path=None,
    )
    assert report.resolved_by_jpg_exif == 1
    inference = repository.sessions_for_run("STOCKIFY_JPG")[0]["location"]["jpg_exif_same_shoot"][
        "inferences"
    ][0]
    assert "bracketing" in inference["association_reason"]


def test_direct_srt_gps_takes_precedence_over_jpg(tmp_path: Path):
    database = Database(tmp_path / "srt-wins.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    media = tmp_path / "media"
    _write_jpg(media / "DJI_20251108213954_0578_D.JPG", seq="0578", lat=1.0, lon=2.0)
    _seed_dji_unknown(
        repository,
        media_root=media,
        sources=[
            {
                "filename": VIDEO,
                "location": {
                    "status": "resolved",
                    "center_lat": CAPITOL_LAT,
                    "center_lon": CAPITOL_LON,
                    "sample_count": 8,
                    "evidence_sources": ["srt_gps"],
                    "direct_source_gps": True,
                },
            }
        ],
    )
    report = _service(repository, media).run(
        run_id="STOCKIFY_JPG",
        dry_run=False,
        rewrite_review_xml=False,
        report_path=None,
    )
    assert report.resolved_by_srt_consensus == 1
    assert report.resolved_by_jpg_exif == 0
    session = repository.sessions_for_run("STOCKIFY_JPG")[0]
    assert abs(session["location"]["center_lat"] - CAPITOL_LAT) < 1e-6
    assert session["location"]["direct_source_gps"] is True
    assert EVIDENCE_SOURCE not in (session["location"].get("evidence_sources") or [])


def test_screen_recording_does_not_poison_dji_jpg_session(tmp_path: Path):
    database = Database(tmp_path / "screen.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    media = tmp_path / "media"
    _write_jpg(media / "DJI_20251108213954_0578_D.JPG", seq="0578")
    _write_jpg(media / "DJI_20251108214100_0582_D.JPG", seq="0582")
    _seed_dji_unknown(
        repository,
        media_root=media,
        sources=[{"filename": VIDEO}, {"filename": SCREEN}],
    )
    report = _service(repository, media).run(
        run_id="STOCKIFY_JPG",
        dry_run=False,
        rewrite_review_xml=False,
        report_path=None,
    )
    assert report.resolved_by_jpg_exif == 1
    session = repository.sessions_for_run("STOCKIFY_JPG")[0]
    assert session["city"] == "Seattle"
    candidates = repository.candidates_for_run("STOCKIFY_JPG", accepted_only=True)
    assert len(candidates) == 2
    assert all(item["location"].get("city") == "Seattle" for item in candidates)


def test_duplicate_jpg_copies_do_not_inflate_recovery_evidence(tmp_path: Path):
    database = Database(tmp_path / "dup.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    archive = tmp_path / "archive"
    bundle = tmp_path / "Hours in Silence.fcpbundle" / "Original Media"
    name = "DJI_20251108213954_0578_D.JPG"
    _write_jpg(archive / name, seq="0578")
    _write_jpg(bundle / name, seq="0578")
    _seed_dji_unknown(
        repository,
        media_root=archive,
        sources=[{"filename": VIDEO, "media_path": str(archive / VIDEO)}],
    )
    report = _service(repository, tmp_path).run(
        run_id="STOCKIFY_JPG",
        dry_run=False,
        rewrite_review_xml=False,
        report_path=None,
    )
    assert report.resolved_by_jpg_exif == 1
    inference = repository.sessions_for_run("STOCKIFY_JPG")[0]["location"]["jpg_exif_same_shoot"][
        "inferences"
    ][0]
    assert inference["sample_count"] == 1


def test_medium_jpg_inference_keeps_review_required(tmp_path: Path):
    database = Database(tmp_path / "medium.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    media = tmp_path / "media"
    _write_jpg(media / "DJI_20251108215000_0590_D.JPG", seq="0590")
    _write_jpg(media / "DJI_20251108215010_0591_D.JPG", seq="0591")
    _seed_dji_unknown(repository, media_root=media, sources=[{"filename": VIDEO}])
    report = _service(repository, media).run(
        run_id="STOCKIFY_JPG",
        dry_run=False,
        rewrite_review_xml=False,
        report_path=None,
    )
    assert report.resolved_by_jpg_exif == 1
    location = repository.sessions_for_run("STOCKIFY_JPG")[0]["location"]
    assert location["review_required"] is True
    assert location["confidence"] in {"medium", "low"}
    assert location["direct_source_gps"] is False
    assert location["jpg_exif_same_shoot"]["inferences"][0]["confidence"] == "medium"


def test_no_jpg_evidence_leaves_unknown(tmp_path: Path):
    database = Database(tmp_path / "none.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    media = tmp_path / "media"
    media.mkdir()
    _seed_dji_unknown(repository, media_root=media, sources=[{"filename": VIDEO}])
    report = _service(repository, media).run(
        run_id="STOCKIFY_JPG",
        dry_run=False,
        rewrite_review_xml=False,
        report_path=None,
    )
    assert report.resolved_by_jpg_exif == 0
    assert report.still_unknown == 1
    session = repository.sessions_for_run("STOCKIFY_JPG")[0]
    assert "Unknown Location" in session["generated_event_name"]


def test_jpg_recovery_dry_run_does_not_mutate_db_or_xml(tmp_path: Path):
    database = Database(tmp_path / "dry.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    review = tmp_path / "review.fcpxml"
    _write_unknown_review_xml(review, "2025-11-08")
    original_xml = review.read_text(encoding="utf-8")
    media = tmp_path / "media"
    _write_jpg(media / "DJI_20251108213954_0578_D.JPG", seq="0578")
    _write_jpg(media / "DJI_20251108214100_0582_D.JPG", seq="0582")
    _seed_dji_unknown(
        repository,
        output_xml=review,
        media_root=media,
        sources=[{"filename": VIDEO}],
    )
    report = _service(repository, media).run(
        run_id="STOCKIFY_JPG",
        dry_run=True,
        rewrite_review_xml=True,
        report_path=None,
    )
    assert report.resolved_by_jpg_exif == 1
    session = repository.sessions_for_run("STOCKIFY_JPG")[0]
    assert session["city"] is None
    assert "Unknown Location" in session["generated_event_name"]
    assert review.read_text(encoding="utf-8") == original_xml


def test_diagnostics_reports_jpg_recoverable_session(tmp_path: Path):
    database = Database(tmp_path / "diag.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    media = tmp_path / "media"
    _write_jpg(media / "DJI_20251108213954_0578_D.JPG", seq="0578")
    _write_jpg(media / "DJI_20251108214100_0582_D.JPG", seq="0582")
    _seed_dji_unknown(repository, media_root=media, sources=[{"filename": VIDEO}])
    report = LocationDiagnosticsService(
        repository,
        _catalog_resolver(repository),
        scan_roots=[media],
    ).run()
    assert report.unknown_sessions == 1
    assert report.sessions[0]["reason_code"] == "jpg_exif_recovery_ready"
    assert report.sessions[0]["jpg_exif"]["status"] == "recovery_ready"
    assert report.reason_counts["srt_missing"]["sessions"] == 0


def test_recover_locations_cli_session_filter_and_opt_out(tmp_path: Path, capsys):
    database = Database(tmp_path / "cli.sqlite3")
    database.migrate()
    repository = CatalogRepository(database)
    media = tmp_path / "media"
    _write_jpg(media / "DJI_20251108213954_0578_D.JPG", seq="0578")
    _write_jpg(media / "DJI_20251108214100_0582_D.JPG", seq="0582")
    _seed_dji_unknown(repository, media_root=media, sources=[{"filename": VIDEO}])
    code = main(
        [
            "recover-locations",
            "--db",
            str(database.path),
            "--scan",
            str(media),
            "--dry-run",
            "--session-id",
            "SESS_JPG",
            "--location-provider",
            "catalog",
            "--quiet",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Resolved by JPG EXIF" in out

    code = main(
        [
            "recover-locations",
            "--db",
            str(database.path),
            "--scan",
            str(media),
            "--dry-run",
            "--no-jpg-exif-recovery",
            "--location-provider",
            "catalog",
            "--quiet",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Resolved by JPG EXIF" not in out
    session = repository.sessions_for_run("STOCKIFY_JPG")[0]
    assert session["city"] is None
