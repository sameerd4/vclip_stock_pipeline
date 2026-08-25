from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.errors import VClipError
from vclip_pipeline.util import json_dumps
from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.review_location_propagate import (
    AMBIGUOUS_SOURCE,
    CORPUS_EVIDENCE,
    PHASE1_CORPUS,
    PHASE1_OLDER_DB,
    PROPAGATION_EVIDENCE,
    SAFE_TO_INHERIT,
    SOURCE_IDENTITY_BUCKET,
    HistoricalLocationPropagateService,
)
from vclip_pipeline.workflow.review_location_restore import (
    GPS_KIND_JPG,
    create_pre_restore_backup,
)


def _setup(tmp_path: Path) -> tuple[Database, CatalogRepository, WorkflowCatalog, Path]:
    db_path = tmp_path / "vclip.sqlite3"
    database = Database(db_path)
    database.migrate()
    catalog = WorkflowCatalog(database)
    catalog.ensure_schema()
    return database, CatalogRepository(database), catalog, db_path


def _srt_location(
    *,
    lat: float = 47.619967,
    lon: float = -122.318109,
    neighborhood: str = "Capitol Hill",
    city: str = "Seattle",
    state: str = "Washington",
    public_label: str = "Capitol Hill, Seattle",
) -> dict:
    return {
        "status": "resolved",
        "confidence": "high",
        "evidence_sources": [
            "flight_session_trajectory",
            "flight_trajectory",
            "srt_gps",
        ],
        "center_lat": lat,
        "center_lon": lon,
        "city": city,
        "state": state,
        "country": "United States",
        "neighborhood": neighborhood,
        "public_label": public_label,
        "direct_source_gps": True,
        "gps_kind": "srt_gps",
    }


def _unknown_location() -> dict:
    return {
        "status": "unresolved",
        "confidence": "low",
        "evidence_sources": ["missing_srt_gps"],
        "center_lat": None,
        "center_lon": None,
        "city": None,
        "state": None,
        "country": None,
        "neighborhood": None,
        "public_label": None,
        "direct_source_gps": False,
    }


def _seed_clip(
    database: Database,
    *,
    run_id: str,
    clip_id: str,
    source_name: str,
    project_name: str,
    event_name: str,
    location: dict,
    started_at: str,
    source_xml: str = "library.fcpxml",
    capture_date: str = "2026-08-06",
    segment_index: int = 0,
) -> None:
    event_id = f"EVT_{run_id}"
    session_id = f"SESS_{run_id}"
    project_id = f"PROJ_{run_id}_{clip_id}"
    media_id = f"MEDIA_{run_id}_{clip_id}"
    stem = Path(source_name).stem.lower()
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO stockify_runs (
                id, source_xml_path, source_xml_sha256, output_xml_path, report_path,
                pipeline_version, status, options_json, started_at, completed_at
            ) VALUES (?, ?, 'h', 'o.xml', 'r.json', '0.1.0', 'complete', '{}', ?, ?)
            """,
            (run_id, source_xml, started_at, started_at),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO source_events
            (id, run_id, source_index, source_name, source_uid)
            VALUES (?, ?, 0, ?, NULL)
            """,
            (event_id, run_id, Path(source_xml).stem),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO shoot_sessions (
                id, run_id, session_key, capture_date, captured_at_local, timezone,
                center_lat, center_lon, gps_radius_meters, country, state, city,
                neighborhood, poi, public_label, location_confidence, time_of_day,
                time_of_day_confidence, generated_event_name, generated_base_label,
                anchor_stock_clip_id, weather_status, location_json, capture_json,
                created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, 'America/Los_Angeles',
                NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                'afternoon', 'medium', ?, 'Unknown Location Afternoon',
                ?, 'not_enriched', ?, ?, 't', 't'
            )
            """,
            (
                session_id,
                run_id,
                f"sess-{capture_date}-{run_id}",
                capture_date,
                f"{capture_date}T15:00:00",
                event_name,
                clip_id,
                json_dumps({"status": "unknown"}),
                json_dumps(
                    {"date": capture_date, "captured_at_local": f"{capture_date}T15:00:00"}
                ),
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO source_media (
                id, run_id, asset_ref, asset_name, original_filename, media_path,
                normalized_stem, duration, duration_seconds, fps, location_json,
                created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, '60s', 60.0, 30, '{}', 't', 't'
            )
            """,
            (
                media_id,
                run_id,
                f"r-{clip_id}",
                source_name,
                source_name,
                f"/tmp/{source_name}",
                stem,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO source_projects (
                id, run_id, source_event_id, source_index, source_name, source_uid,
                classification, session_id, anchor_segment_index, generated_event_name,
                generated_project_label, generated_compilation_name,
                accepted_clip_count, skipped_clip_count, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, 'Source Project', NULL, 'accepted',
                ?, ?, ?, 'Unknown Location Afternoon',
                'Unknown Location Afternoon — Stock Compilation',
                1, 0, 't', 't'
            )
            """,
            (
                project_id,
                run_id,
                event_id,
                segment_index,
                session_id,
                segment_index,
                event_name,
            ),
        )
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
                generated_event_name, generated_project_label, generated_compilation_name,
                generated_clip_project_name, clip_sequence, expected_export_basename,
                created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, NULL, ?, 'accepted',
                NULL, NULL, '0s', '10s', 10.0, '0s', '10s', 10.0, 'not_applicable', 'A_clean_10s',
                NULL, 'matched', 'matched', '[]', NULL, '[]', '{}', ?,
                ?, '{"label":"afternoon","confidence":"medium"}', '{}', '[]',
                ?, 'Unknown Location Afternoon',
                'Unknown Location Afternoon — Stock Compilation',
                ?, ?, ?, 't', 't'
            )
            """,
            (
                run_id,
                clip_id,
                project_id,
                media_id,
                session_id,
                segment_index,
                source_name,
                json_dumps(location),
                json_dumps(
                    {"date": capture_date, "captured_at_local": f"{capture_date}T15:00:00"}
                ),
                event_name,
                project_name,
                segment_index + 1,
                project_name,
            ),
        )


def _clip_row(
    *,
    clip_id: str,
    run_id: str,
    bucket: str,
    library: str = "Nova",
    source_name: str = "DJI_20260806120000_0001_D.mp4",
    event_name: str = "Unknown Location — 2026-08-06",
    corpus_event: str | None = None,
    corpus_project: str | None = None,
    source_identity: dict | None = None,
) -> dict:
    return {
        "stock_clip_id": clip_id,
        "current_run_id": run_id,
        "exclusive_bucket": bucket,
        "library": library,
        "source_filename": source_name,
        "source_stem": Path(source_name).stem.lower(),
        "capture_date": "2026-08-06",
        "current_generated_event_name": event_name,
        "final_corpus": {
            "chosen": (
                {
                    "stockify_run_id": run_id,
                    "event_name": corpus_event,
                    "project_name": corpus_project,
                    "known": bool(corpus_event),
                    "source": "fcpxml_vclip_metadata",
                }
                if corpus_event
                else None
            )
        },
        "older_db_resolved": [],
        "source_identity": source_identity or {},
        "flags": {
            "older_db_resolved": bucket == PHASE1_OLDER_DB,
            "final_corpus_known": bucket == PHASE1_CORPUS,
        },
    }


def _write_reconciliation(path: Path, clips: list[dict]) -> Path:
    path.write_text(
        json.dumps({"mode": "test_reconciliation", "clips": clips}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _load_candidate(database: Database, run_id: str, clip_id: str) -> dict:
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT location_json, generated_event_name, generated_clip_project_name
            FROM stock_candidates
            WHERE run_id=? AND stock_clip_id=?
            """,
            (run_id, clip_id),
        ).fetchone()
    assert row is not None
    return {
        "location": json.loads(row["location_json"]),
        "generated_event_name": row["generated_event_name"],
        "generated_clip_project_name": row["generated_clip_project_name"],
    }


def test_validate_is_read_only(tmp_path: Path) -> None:
    database, repository, catalog, db_path = _setup(tmp_path)
    older = "STOCKIFY_OLD"
    latest = "STOCKIFY_NEW"
    clip_id = "VCLIP_RO1"
    source = "DJI_20260806120000_0001_D.mp4"
    _seed_clip(
        database,
        run_id=older,
        clip_id=clip_id,
        source_name=source,
        project_name="Capitol Hill Afternoon — Clip 01",
        event_name="Capitol Hill, Seattle — 2026-08-06",
        location=_srt_location(),
        started_at="2026-01-01T00:00:00Z",
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id=clip_id,
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2026-08-06",
        location=_unknown_location(),
        started_at="2026-08-01T00:00:00Z",
    )
    recon = _write_reconciliation(
        tmp_path / "recon.json",
        [_clip_row(clip_id=clip_id, run_id=latest, bucket=PHASE1_OLDER_DB, source_name=source)],
    )
    before = db_path.read_bytes()
    service = HistoricalLocationPropagateService(repository, catalog)
    report = service.validate(reconciliation_path=recon)
    assert report.read_only is True
    assert report.safe_to_restore == 1
    assert db_path.read_bytes() == before


def test_older_db_safe_copy_preserves_srt_and_adds_propagation(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    older = "STOCKIFY_OLD"
    latest = "STOCKIFY_NEW"
    clip_id = "VCLIP_OLD1"
    source = "DJI_20260806120000_0001_D.mp4"
    _seed_clip(
        database,
        run_id=older,
        clip_id=clip_id,
        source_name=source,
        project_name="Capitol Hill Afternoon — Clip 01",
        event_name="Capitol Hill, Seattle — 2026-08-06",
        location=_srt_location(),
        started_at="2026-01-01T00:00:00Z",
        source_xml="/tmp/8-6-26.fcpxmld/info.fcpxml",
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id=clip_id,
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2026-08-06",
        location=_unknown_location(),
        started_at="2026-08-01T00:00:00Z",
        source_xml="/tmp/8-6-26.fcpxmld/info.fcpxml",
    )
    recon = _write_reconciliation(
        tmp_path / "recon.json",
        [
            _clip_row(
                clip_id=clip_id,
                run_id=latest,
                bucket=PHASE1_OLDER_DB,
                library="8-6-26",
                source_name=source,
            )
        ],
    )
    service = HistoricalLocationPropagateService(repository, catalog)
    report = service.validate(reconciliation_path=recon)
    mutation = report.mutations[0]
    proposed = mutation["proposed_location_snapshot"]["location"]
    assert mutation["safety_class"] == "safe_to_restore"
    assert mutation["is_8_6_26"] is True
    assert proposed["gps_kind"] == "srt_gps"
    assert proposed["direct_source_gps"] is True
    assert "srt_gps" in proposed["evidence_sources"]
    assert PROPAGATION_EVIDENCE in proposed["evidence_sources"]
    assert proposed["propagation"]["inherited_from"] == "older_stock_candidates"
    assert proposed["propagation"]["source_run_id"] == older


def test_corpus_name_only_does_not_invent_gps(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    latest = "STOCKIFY_CORPUS"
    clip_id = "VCLIP_CORPUS1"
    source = "DJI_0922.mp4"
    _seed_clip(
        database,
        run_id=latest,
        clip_id=clip_id,
        source_name=source,
        project_name="Unknown Location Footage — Clip 01",
        event_name="Unknown Location — Unknown Date",
        location=_unknown_location(),
        started_at="2026-08-01T00:00:00Z",
    )
    recon = _write_reconciliation(
        tmp_path / "recon.json",
        [
            _clip_row(
                clip_id=clip_id,
                run_id=latest,
                bucket=PHASE1_CORPUS,
                source_name=source,
                corpus_event="Capitol Hill, Seattle — Unknown Date",
                corpus_project="Capitol Hill Footage — Clip 01",
            )
        ],
    )
    service = HistoricalLocationPropagateService(repository, catalog)
    report = service.validate(reconciliation_path=recon)
    proposed = report.mutations[0]["proposed_location_snapshot"]["location"]
    assert report.mutations[0]["safety_class"] == "safe_to_restore"
    assert proposed["public_label"] == "Capitol Hill, Seattle"
    assert proposed["center_lat"] is None
    assert proposed["direct_source_gps"] is False
    assert proposed["gps_kind"] in (None, "")
    assert CORPUS_EVIDENCE in proposed["evidence_sources"]
    assert "srt_gps" not in proposed["evidence_sources"]
    assert proposed["propagation"]["inherited_from"] == "final_review_corpus"


def test_corpus_uses_older_gps_when_compatible(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    older = "STOCKIFY_OLD"
    latest = "STOCKIFY_NEW"
    clip_id = "VCLIP_CORPUSGPS"
    source = "DJI_20250629123903_0470_D.mp4"
    _seed_clip(
        database,
        run_id=older,
        clip_id=clip_id,
        source_name=source,
        project_name="South Lake Union Midday — Clip 03",
        event_name="South Lake Union, Seattle — 2025-06-29",
        location=_srt_location(
            neighborhood="South Lake Union",
            public_label="South Lake Union, Seattle",
        ),
        started_at="2025-07-01T00:00:00Z",
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id=clip_id,
        source_name=source,
        project_name="South Lake Union Midday — Clip 03",
        event_name="South Lake Union, Seattle — 2025-06-29",
        location=_unknown_location(),
        started_at="2026-08-01T00:00:00Z",
    )
    recon = _write_reconciliation(
        tmp_path / "recon.json",
        [
            _clip_row(
                clip_id=clip_id,
                run_id=latest,
                bucket=PHASE1_CORPUS,
                source_name=source,
                corpus_event="South Lake Union, Seattle — 2025-06-29 — Seattle 02",
                corpus_project="South Lake Union Midday — Clip 03",
            )
        ],
    )
    service = HistoricalLocationPropagateService(repository, catalog)
    report = service.validate(reconciliation_path=recon)
    proposed = report.mutations[0]["proposed_location_snapshot"]["location"]
    assert report.mutations[0]["safety_class"] == "safe_to_restore"
    assert proposed["gps_kind"] == "srt_gps"
    assert proposed["public_label"] == "South Lake Union, Seattle"
    assert proposed["propagation"]["inherited_from"] == "older_stock_candidates"


def test_missing_candidate(tmp_path: Path) -> None:
    _database, repository, catalog, _db_path = _setup(tmp_path)
    recon = _write_reconciliation(
        tmp_path / "recon.json",
        [_clip_row(clip_id="VCLIP_MISSING", run_id="STOCKIFY_NONE", bucket=PHASE1_OLDER_DB)],
    )
    service = HistoricalLocationPropagateService(repository, catalog)
    report = service.validate(reconciliation_path=recon)
    assert report.missing_candidates == 1
    assert report.mutations[0]["safety_class"] == "missing_candidate"


def test_stronger_current_is_not_overwritten(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    older = "STOCKIFY_OLD"
    latest = "STOCKIFY_NEW"
    clip_id = "VCLIP_STRONG"
    source = "DJI_20260806120000_0001_D.mp4"
    jpg = dict(_srt_location())
    jpg["evidence_sources"] = ["jpg_exif_same_shoot"]
    jpg["direct_source_gps"] = False
    jpg["gps_kind"] = "inferred_jpg_exif_same_shoot"
    current = _srt_location(
        lat=37.2296,
        lon=-80.4139,
        neighborhood=None,
        city="Blacksburg",
        state="Virginia",
        public_label="Blacksburg, Virginia",
    )
    _seed_clip(
        database,
        run_id=older,
        clip_id=clip_id,
        source_name=source,
        project_name="Capitol Hill Afternoon — Clip 01",
        event_name="Capitol Hill, Seattle — 2026-08-06",
        location=jpg,
        started_at="2026-01-01T00:00:00Z",
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id=clip_id,
        source_name=source,
        project_name="Blacksburg Afternoon — Clip 01",
        event_name="Blacksburg, Virginia — 2026-08-06",
        location=current,
        started_at="2026-08-01T00:00:00Z",
    )
    recon = _write_reconciliation(
        tmp_path / "recon.json",
        [_clip_row(clip_id=clip_id, run_id=latest, bucket=PHASE1_OLDER_DB, source_name=source)],
    )
    service = HistoricalLocationPropagateService(repository, catalog)
    report = service.validate(reconciliation_path=recon)
    assert report.mutations[0]["safety_class"] == "stronger_current_evidence"


def test_conflicting_gps_is_blocked(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    older = "STOCKIFY_OLD"
    latest = "STOCKIFY_NEW"
    clip_id = "VCLIP_CONFLICT"
    source = "DJI_20260806120000_0001_D.mp4"
    _seed_clip(
        database,
        run_id=older,
        clip_id=clip_id,
        source_name=source,
        project_name="Capitol Hill Afternoon — Clip 01",
        event_name="Capitol Hill, Seattle — 2026-08-06",
        location=_srt_location(),
        started_at="2026-01-01T00:00:00Z",
    )
    blacksburg = _srt_location(
        lat=37.2296,
        lon=-80.4139,
        neighborhood=None,
        city="Blacksburg",
        state="Virginia",
        public_label="Blacksburg, Virginia",
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id=clip_id,
        source_name=source,
        project_name="Blacksburg Afternoon — Clip 01",
        event_name="Blacksburg, Virginia — 2026-08-06",
        location=blacksburg,
        started_at="2026-08-01T00:00:00Z",
    )
    recon = _write_reconciliation(
        tmp_path / "recon.json",
        [_clip_row(clip_id=clip_id, run_id=latest, bucket=PHASE1_OLDER_DB, source_name=source)],
    )
    service = HistoricalLocationPropagateService(repository, catalog)
    report = service.validate(reconciliation_path=recon)
    assert report.mutations[0]["safety_class"] == "conflicting_current_evidence"
    with pytest.raises(VClipError, match="conflicting_current_evidence"):
        service.propagate(
            reconciliation_path=recon,
            write=True,
            backup_path=tmp_path / "pre.bak",
        )


def test_write_only_safe_rows_and_is_atomic(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    older = "STOCKIFY_OLD"
    latest = "STOCKIFY_NEW"
    clips = []
    for index in range(2):
        clip_id = f"VCLIP_SAFE{index}"
        source = f"DJI_20260806120000_{index:04d}_D.mp4"
        _seed_clip(
            database,
            run_id=older,
            clip_id=clip_id,
            source_name=source,
            project_name="Capitol Hill Afternoon — Clip 01",
            event_name="Capitol Hill, Seattle — 2026-08-06",
            location=_srt_location(),
            started_at="2026-01-01T00:00:00Z",
            source_xml="/tmp/8-6-26.fcpxmld/info.fcpxml",
            segment_index=index,
        )
        _seed_clip(
            database,
            run_id=latest,
            clip_id=clip_id,
            source_name=source,
            project_name="Unknown Location Afternoon — Clip 01",
            event_name="Unknown Location — 2026-08-06",
            location=_unknown_location(),
            started_at="2026-08-01T00:00:00Z",
            source_xml="/tmp/8-6-26.fcpxmld/info.fcpxml",
            segment_index=index,
        )
        clips.append(
            _clip_row(
                clip_id=clip_id,
                run_id=latest,
                bucket=PHASE1_OLDER_DB,
                library="8-6-26",
                source_name=source,
            )
        )
    recon = _write_reconciliation(tmp_path / "recon.json", clips)
    service = HistoricalLocationPropagateService(repository, catalog)
    with pytest.raises(RuntimeError, match="injected propagate failure"):
        service.propagate(
            reconciliation_path=recon,
            write=True,
            backup_path=tmp_path / "fail.bak",
            fail_after=1,
        )
    for index in range(2):
        row = _load_candidate(database, latest, f"VCLIP_SAFE{index}")
        assert row["generated_event_name"] == "Unknown Location — 2026-08-06"
        assert row["location"]["public_label"] is None
    with database.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM review_location_recoveries"
        ).fetchone()["n"]
    assert count == 0

    report = service.propagate(
        reconciliation_path=recon,
        write=True,
        backup_path=tmp_path / "ok.bak",
    )
    assert report.read_only is False
    assert report.safe_to_restore == 2
    assert report.post_write_audit["intended_rows_written"] == 2
    assert report.post_write_audit["unintended_rows_changed"] == []
    assert report.post_write_audit["source_identity_rows_written"] == 0
    assert report.post_write_audit["fcpxml_writes"] == 0
    row = _load_candidate(database, latest, "VCLIP_SAFE0")
    assert row["location"]["public_label"] == "Capitol Hill, Seattle"
    assert "srt_gps" in row["location"]["evidence_sources"]
    assert PROPAGATION_EVIDENCE in row["location"]["evidence_sources"]
    assert row["location"]["propagation"]["source_run_id"] == older


def test_write_skips_source_identity_rows(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    older = "STOCKIFY_OLD"
    latest = "STOCKIFY_NEW"
    safe_id = "VCLIP_WRITESAFE"
    identity_id = "VCLIP_IDENTITY"
    donor_id = "VCLIP_DONOR"
    source = "DJI_20260806120000_0001_D.mp4"
    identity_source = "DJI_20251108120000_0099_D.mp4"
    _seed_clip(
        database,
        run_id=older,
        clip_id=safe_id,
        source_name=source,
        project_name="Capitol Hill Afternoon — Clip 01",
        event_name="Capitol Hill, Seattle — 2026-08-06",
        location=_srt_location(),
        started_at="2026-01-01T00:00:00Z",
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id=safe_id,
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2026-08-06",
        location=_unknown_location(),
        started_at="2026-08-01T00:00:00Z",
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id=identity_id,
        source_name=identity_source,
        project_name="Unknown Location Afternoon — Clip 02",
        event_name="Unknown Location — 2026-08-06",
        location=_unknown_location(),
        started_at="2026-08-01T00:00:00Z",
        segment_index=1,
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id=donor_id,
        source_name=identity_source,
        project_name="Capitol Hill Afternoon — Clip 02",
        event_name="Capitol Hill, Seattle — 2026-08-06",
        location=_srt_location(),
        started_at="2026-08-01T00:00:00Z",
        segment_index=2,
    )
    recon = _write_reconciliation(
        tmp_path / "recon.json",
        [
            _clip_row(clip_id=safe_id, run_id=latest, bucket=PHASE1_OLDER_DB, source_name=source),
            _clip_row(
                clip_id=identity_id,
                run_id=latest,
                bucket=SOURCE_IDENTITY_BUCKET,
                source_name=identity_source,
                source_identity={
                    "geographically_consistent": True,
                    "locations": ["Capitol Hill, Seattle"],
                },
            ),
        ],
    )
    service = HistoricalLocationPropagateService(repository, catalog)
    report = service.propagate(
        reconciliation_path=recon,
        write=True,
        backup_path=tmp_path / "mix.bak",
    )
    assert report.safe_to_restore == 1
    assert report.source_identity["safe_to_inherit"] == 1
    written = _load_candidate(database, latest, safe_id)
    untouched = _load_candidate(database, latest, identity_id)
    assert written["location"]["public_label"] == "Capitol Hill, Seattle"
    assert untouched["location"]["public_label"] is None
    with database.connect() as connection:
        recovery_clips = {
            row["stock_clip_id"]
            for row in connection.execute(
                "SELECT stock_clip_id FROM review_location_recoveries"
            )
        }
    assert recovery_clips == {safe_id}


def test_backup_uses_propagate_prefix_and_refuses_overwrite(tmp_path: Path) -> None:
    _database, _repository, _catalog, db_path = _setup(tmp_path)
    created = create_pre_restore_backup(
        db_path,
        clock=lambda: datetime(2026, 8, 21, 12, 0, 0),
        name_prefix="pre-location-propagate",
    )
    assert created.name == "vclip.sqlite3.pre-location-propagate-20260821-120000.bak"
    with pytest.raises(VClipError, match="already exists"):
        create_pre_restore_backup(db_path, backup_path=created)


def test_eight_six_twenty_six_all_safe(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    older = "STOCKIFY_5277C41BE21B4B798B3B6D051CA3CF1A"
    latest = "STOCKIFY_4D48A864B5154870AAC74FFB7A96985E"
    clips = []
    xml = "/media/8-6-26.fcpxmld/info.fcpxml"
    for index in range(18):
        clip_id = f"VCLIP_826{index:02d}"
        source = f"DJI_20260806120000_{index:04d}_D.mp4"
        _seed_clip(
            database,
            run_id=older,
            clip_id=clip_id,
            source_name=source,
            project_name="Capitol Hill Afternoon — Clip 01",
            event_name="Capitol Hill, Seattle — 2026-08-06",
            location=_srt_location(),
            started_at="2026-01-01T00:00:00Z",
            source_xml=xml,
            segment_index=index,
        )
        _seed_clip(
            database,
            run_id=latest,
            clip_id=clip_id,
            source_name=source,
            project_name="Unknown Location Afternoon — Clip 01",
            event_name="Unknown Location — 2026-08-06",
            location=_unknown_location(),
            started_at="2026-08-01T00:00:00Z",
            source_xml=xml,
            segment_index=index,
        )
        clips.append(
            _clip_row(
                clip_id=clip_id,
                run_id=latest,
                bucket=PHASE1_OLDER_DB,
                library="8-6-26",
                source_name=source,
            )
        )
    recon = _write_reconciliation(tmp_path / "recon.json", clips)
    service = HistoricalLocationPropagateService(repository, catalog)
    report = service.validate(reconciliation_path=recon)
    assert report.library_8_6_26["phase1_rows"] == 18
    assert report.library_8_6_26["matched"] == 18
    assert report.library_8_6_26["safe_to_restore"] == 18
    assert report.library_8_6_26["all_eighteen_safe"] is True
    assert report.locations_grouped_by_count == [
        {"label": "Capitol Hill, Seattle", "count": 18}
    ]


def test_source_identity_datetime_srt_is_safe(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    latest = "STOCKIFY_NEW"
    source = "DJI_20251108120000_0099_D.mp4"
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_TGT",
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2025-11-08",
        location=_unknown_location(),
        started_at="2026-08-01T00:00:00Z",
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_DONOR",
        source_name=source,
        project_name="Capitol Hill Afternoon — Clip 02",
        event_name="Capitol Hill, Seattle — 2025-11-08",
        location=_srt_location(),
        started_at="2026-08-01T00:00:00Z",
        segment_index=1,
    )
    recon = _write_reconciliation(
        tmp_path / "recon.json",
        [
            _clip_row(
                clip_id="VCLIP_TGT",
                run_id=latest,
                bucket=SOURCE_IDENTITY_BUCKET,
                source_name=source,
                source_identity={"geographically_consistent": True},
            )
        ],
    )
    report = HistoricalLocationPropagateService(repository, catalog).validate(
        reconciliation_path=recon
    )
    row = report.source_identity["rows"][0]
    assert row["safety_class"] == "safe_to_inherit"
    assert row["identity_kind"] == "dji_datetime"
    assert row["would_write"] is False
    assert "srt_gps" in row["proposed_location"]["evidence_sources"]
    assert row["proposed_location"]["gps_kind"] == "srt_gps"
    assert report.source_identity["projected_unresolved_if_safe_later_propagated"] == 0
    assert report.coverage_before["unresolved"] == 1


def test_source_identity_distinct_labels_are_ambiguous(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    latest = "STOCKIFY_NEW"
    source = "DJI_20250828130638_0481_D.mp4"
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_TGT",
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2025-08-28",
        location=_unknown_location(),
        started_at="2026-08-01T00:00:00Z",
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_A",
        source_name=source,
        project_name="Charlottesville Afternoon — Clip 01",
        event_name="Charlottesville, Virginia — 2025-08-28",
        location=_srt_location(
            lat=38.03,
            lon=-78.51,
            neighborhood=None,
            city="Charlottesville",
            state="Virginia",
            public_label="Charlottesville, Virginia",
        ),
        started_at="2026-08-01T00:00:00Z",
        segment_index=1,
    )
    uva = _srt_location(
        lat=38.03,
        lon=-78.51,
        neighborhood=None,
        city="Charlottesville",
        state="Virginia",
        public_label="University of Virginia, Charlottesville",
    )
    uva["evidence_sources"] = ["manual_gps_override", "srt_gps"]
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_B",
        source_name=source,
        project_name="UVA Afternoon — Clip 01",
        event_name="University of Virginia, Charlottesville — 2025-08-28",
        location=uva,
        started_at="2026-08-01T00:00:00Z",
        segment_index=2,
    )
    recon = _write_reconciliation(
        tmp_path / "recon.json",
        [
            _clip_row(
                clip_id="VCLIP_TGT",
                run_id=latest,
                bucket=SOURCE_IDENTITY_BUCKET,
                source_name=source,
                source_identity={
                    "geographically_consistent": True,
                    "locations": [
                        "Charlottesville, Virginia",
                        "University of Virginia, Charlottesville",
                    ],
                },
            )
        ],
    )
    report = HistoricalLocationPropagateService(repository, catalog).validate(
        reconciliation_path=recon
    )
    row = report.source_identity["rows"][0]
    assert row["safety_class"] == "ambiguous_source_identity"
    assert report.source_identity["safe_to_inherit"] == 0


def test_source_identity_name_hint_only_is_ambiguous(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    latest = "STOCKIFY_NEW"
    source = "DJI_0430.mp4"
    weak = {
        "status": "resolved",
        "confidence": "low",
        "evidence_sources": ["name_hint_corroboration"],
        "center_lat": None,
        "center_lon": None,
        "city": "Seattle",
        "neighborhood": "Capitol Hill",
        "public_label": "Capitol Hill, Seattle",
        "direct_source_gps": False,
        "gps_kind": None,
    }
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_TGT",
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — Unknown Date",
        location=_unknown_location(),
        started_at="2026-08-01T00:00:00Z",
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_DONOR",
        source_name=source,
        project_name="Capitol Hill Footage — Clip 01",
        event_name="Capitol Hill, Seattle — Unknown Date",
        location=weak,
        started_at="2026-08-01T00:00:00Z",
        segment_index=1,
    )
    recon = _write_reconciliation(
        tmp_path / "recon.json",
        [
            _clip_row(
                clip_id="VCLIP_TGT",
                run_id=latest,
                bucket=SOURCE_IDENTITY_BUCKET,
                source_name=source,
                source_identity={"geographically_consistent": True},
            )
        ],
    )
    report = HistoricalLocationPropagateService(repository, catalog).validate(
        reconciliation_path=recon
    )
    assert report.source_identity["rows"][0]["safety_class"] == "ambiguous_source_identity"


def test_source_identity_conflicting_cities(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    latest = "STOCKIFY_NEW"
    source = "DJI_0214.mp4"
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_TGT",
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — Unknown Date",
        location=_unknown_location(),
        started_at="2026-08-01T00:00:00Z",
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_SEA",
        source_name=source,
        project_name="Capitol Hill Afternoon — Clip 01",
        event_name="Capitol Hill, Seattle — Unknown Date",
        location=_srt_location(),
        started_at="2026-08-01T00:00:00Z",
        segment_index=1,
    )
    mumbai = _srt_location(
        lat=19.0176,
        lon=72.8562,
        neighborhood="Dadar West",
        city="Mumbai",
        state=None,
        public_label="Dadar West, Mumbai",
    )
    mumbai["evidence_sources"] = ["manual_gps_override", "srt_gps"]
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_BOM",
        source_name=source,
        project_name="Dadar West Afternoon — Clip 01",
        event_name="Dadar West, Mumbai — Unknown Date",
        location=mumbai,
        started_at="2026-08-01T00:00:00Z",
        segment_index=2,
    )
    recon = _write_reconciliation(
        tmp_path / "recon.json",
        [
            _clip_row(
                clip_id="VCLIP_TGT",
                run_id=latest,
                bucket=SOURCE_IDENTITY_BUCKET,
                source_name=source,
            )
        ],
    )
    report = HistoricalLocationPropagateService(repository, catalog).validate(
        reconciliation_path=recon
    )
    assert report.source_identity["rows"][0]["safety_class"] == "conflicting_source_identity"


def test_locations_grouped_by_count_and_coverage(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    older = "STOCKIFY_OLD"
    latest = "STOCKIFY_NEW"
    clips = []
    for index, (clip_id, label, neighborhood, city) in enumerate(
        [
            ("VCLIP_SLU", "South Lake Union, Seattle", "South Lake Union", "Seattle"),
            ("VCLIP_CH", "Capitol Hill, Seattle", "Capitol Hill", "Seattle"),
            ("VCLIP_CH2", "Capitol Hill, Seattle", "Capitol Hill", "Seattle"),
        ]
    ):
        source = f"DJI_20260806120000_{index:04d}_D.mp4"
        loc = _srt_location(neighborhood=neighborhood, city=city, public_label=label)
        _seed_clip(
            database,
            run_id=older,
            clip_id=clip_id,
            source_name=source,
            project_name=f"{neighborhood} Afternoon — Clip 01",
            event_name=f"{label} — 2026-08-06",
            location=loc,
            started_at="2026-01-01T00:00:00Z",
            segment_index=index,
        )
        _seed_clip(
            database,
            run_id=latest,
            clip_id=clip_id,
            source_name=source,
            project_name="Unknown Location Afternoon — Clip 01",
            event_name="Unknown Location — 2026-08-06",
            location=_unknown_location(),
            started_at="2026-08-01T00:00:00Z",
            segment_index=index,
        )
        clips.append(
            _clip_row(
                clip_id=clip_id,
                run_id=latest,
                bucket=PHASE1_OLDER_DB,
                source_name=source,
            )
        )
    recon = _write_reconciliation(tmp_path / "recon.json", clips)
    report = HistoricalLocationPropagateService(repository, catalog).validate(
        reconciliation_path=recon
    )
    assert report.safe_to_restore == 3
    assert report.locations_grouped_by_count == [
        {"label": "Capitol Hill, Seattle", "count": 2},
        {"label": "South Lake Union, Seattle", "count": 1},
    ]
    assert report.coverage_before["unresolved"] == 3
    assert report.coverage_after["projected_unresolved"] == 0


def _jpg_location() -> dict:
    return {
        "status": "resolved",
        "confidence": "high",
        "evidence_sources": ["jpg_exif_same_shoot", "review_location_materialize"],
        "center_lat": 47.614093,
        "center_lon": -122.318109,
        "city": "Seattle",
        "state": "Washington",
        "country": "United States",
        "neighborhood": "Capitol Hill",
        "public_label": "Capitol Hill, Seattle",
        "direct_source_gps": False,
        "gps_kind": GPS_KIND_JPG,
    }


def _source_identity_row(
    *,
    clip_id: str,
    run_id: str,
    source_name: str,
    safety_class: str = SAFE_TO_INHERIT,
    labels: list[str] | None = None,
) -> dict:
    return {
        "stock_clip_id": clip_id,
        "stockify_run_id": run_id,
        "source_stem": Path(source_name).stem.lower(),
        "source_filename": source_name,
        "safety_class": safety_class,
        "source_identity": {"geographically_consistent": True, "locations": labels or []},
    }


def _write_source_identity(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        json.dumps(
            {
                "mode": "source_identity_propagation_safety",
                "rows": rows,
                "safe_to_inherit": sum(1 for row in rows if row["safety_class"] == SAFE_TO_INHERIT),
                "ambiguous": sum(1 for row in rows if row["safety_class"] == AMBIGUOUS_SOURCE),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_phase2_validate_is_read_only(tmp_path: Path) -> None:
    database, repository, catalog, db_path = _setup(tmp_path)
    latest = "STOCKIFY_NEW"
    source = "DJI_20251108120000_0099_D.mp4"
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_TGT",
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2025-11-08",
        location=_unknown_location(),
        started_at="2026-08-01T00:00:00Z",
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_DONOR",
        source_name=source,
        project_name="Capitol Hill Afternoon — Clip 02",
        event_name="Capitol Hill, Seattle — 2025-11-08",
        location=_srt_location(),
        started_at="2026-08-01T00:00:00Z",
        segment_index=1,
    )
    journal = _write_source_identity(
        tmp_path / "safety.json",
        [
            _source_identity_row(
                clip_id="VCLIP_TGT",
                run_id=latest,
                source_name=source,
            )
        ],
    )
    before = db_path.read_bytes()
    report = HistoricalLocationPropagateService(repository, catalog).validate(
        phase=2, source_identity_path=journal
    )
    assert report.phase == 2
    assert report.read_only is True
    assert report.safe_to_restore == 1
    assert db_path.read_bytes() == before


def test_phase2_skips_ambiguous_and_writes_only_safe(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    latest = "STOCKIFY_NEW"
    safe_source = "DJI_20251108120000_0099_D.mp4"
    uva_source = "DJI_20250828130638_0481_D.mp4"
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_TGT",
        source_name=safe_source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2025-11-08",
        location=_unknown_location(),
        started_at="2026-08-01T00:00:00Z",
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_DONOR",
        source_name=safe_source,
        project_name="Capitol Hill Afternoon — Clip 02",
        event_name="Capitol Hill, Seattle — 2025-11-08",
        location=_srt_location(),
        started_at="2026-08-01T00:00:00Z",
        segment_index=1,
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_UVA",
        source_name=uva_source,
        project_name="Unknown Location Afternoon — Clip 03",
        event_name="Unknown Location — 2025-08-28",
        location=_unknown_location(),
        started_at="2026-08-01T00:00:00Z",
        segment_index=2,
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_UVA_A",
        source_name=uva_source,
        project_name="Charlottesville Afternoon — Clip 01",
        event_name="Charlottesville, Virginia — 2025-08-28",
        location=_srt_location(
            lat=38.03,
            lon=-78.51,
            neighborhood=None,
            city="Charlottesville",
            state="Virginia",
            public_label="Charlottesville, Virginia",
        ),
        started_at="2026-08-01T00:00:00Z",
        segment_index=3,
    )
    uva = _srt_location(
        lat=38.03,
        lon=-78.51,
        neighborhood=None,
        city="Charlottesville",
        state="Virginia",
        public_label="University of Virginia, Charlottesville",
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_UVA_B",
        source_name=uva_source,
        project_name="UVA Afternoon — Clip 01",
        event_name="University of Virginia, Charlottesville — 2025-08-28",
        location=uva,
        started_at="2026-08-01T00:00:00Z",
        segment_index=4,
    )
    journal = _write_source_identity(
        tmp_path / "safety.json",
        [
            _source_identity_row(
                clip_id="VCLIP_TGT", run_id=latest, source_name=safe_source
            ),
            _source_identity_row(
                clip_id="VCLIP_UVA",
                run_id=latest,
                source_name=uva_source,
                safety_class=AMBIGUOUS_SOURCE,
                labels=[
                    "Charlottesville, Virginia",
                    "University of Virginia, Charlottesville",
                ],
            ),
        ],
    )
    service = HistoricalLocationPropagateService(repository, catalog)
    report = service.propagate(
        phase=2,
        source_identity_path=journal,
        write=True,
        backup_path=tmp_path / "phase2.bak",
    )
    assert report.phase2_targets == 1
    assert report.safe_to_restore == 1
    assert report.ambiguous_excluded == 1
    assert report.ambiguous_excluded_ids == ["VCLIP_UVA"]
    assert report.post_write_audit["ambiguous_rows_written"] == []
    assert report.post_write_audit["session_summaries_changed"] == []
    assert report.post_write_audit["session_summaries_written"] == 0
    written = _load_candidate(database, latest, "VCLIP_TGT")
    untouched = _load_candidate(database, latest, "VCLIP_UVA")
    assert written["location"]["public_label"] == "Capitol Hill, Seattle"
    assert "srt_gps" in written["location"]["evidence_sources"]
    prop = written["location"]["propagation"]
    assert prop["source_stem"] == "dji_20251108120000_0099_d"
    assert prop["donor_stock_clip_id"] == "VCLIP_DONOR"
    assert prop["donor_run_id"] == latest
    assert prop["donor_evidence_kind"] == "direct_source_srt_gps"
    assert prop["inherited_existing_historical_source_location"] is True
    assert untouched["location"]["public_label"] is None
    with database.connect() as connection:
        recovery_clips = {
            row["stock_clip_id"]
            for row in connection.execute(
                "SELECT stock_clip_id FROM review_location_recoveries"
            )
        }
        session = connection.execute(
            "SELECT location_json FROM shoot_sessions WHERE id=?",
            (f"SESS_{latest}",),
        ).fetchone()
    assert recovery_clips == {"VCLIP_TGT"}
    assert json.loads(session["location_json"])["status"] == "unknown"


def test_phase2_jpg_is_not_rewritten_as_direct_gps(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    latest = "STOCKIFY_NEW"
    source = "DJI_20251108120000_0576_D.mp4"
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_TGT",
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2025-11-08",
        location=_unknown_location(),
        started_at="2026-08-01T00:00:00Z",
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_DONOR",
        source_name=source,
        project_name="Capitol Hill Afternoon — Clip 02",
        event_name="Capitol Hill, Seattle — 2025-11-08",
        location=_jpg_location(),
        started_at="2026-08-01T00:00:00Z",
        segment_index=1,
    )
    journal = _write_source_identity(
        tmp_path / "safety.json",
        [_source_identity_row(clip_id="VCLIP_TGT", run_id=latest, source_name=source)],
    )
    report = HistoricalLocationPropagateService(repository, catalog).propagate(
        phase=2,
        source_identity_path=journal,
        write=True,
        backup_path=tmp_path / "jpg.bak",
    )
    proposed = report.mutations[0]["proposed_location_snapshot"]["location"]
    assert proposed["direct_source_gps"] is False
    assert proposed["gps_kind"] == GPS_KIND_JPG
    written = _load_candidate(database, latest, "VCLIP_TGT")
    assert written["location"]["direct_source_gps"] is False
    assert written["location"]["gps_kind"] == GPS_KIND_JPG
    assert "jpg_exif_same_shoot" in written["location"]["evidence_sources"]
    assert report.post_write_audit["jpg_direct_source_gps_violations"] == []


def test_phase2_write_is_atomic(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    latest = "STOCKIFY_NEW"
    clips = []
    for index in range(2):
        clip_id = f"VCLIP_P2{index}"
        donor_id = f"VCLIP_P2D{index}"
        source = f"DJI_20251108120000_{index:04d}_D.mp4"
        _seed_clip(
            database,
            run_id=latest,
            clip_id=clip_id,
            source_name=source,
            project_name="Unknown Location Afternoon — Clip 01",
            event_name="Unknown Location — 2025-11-08",
            location=_unknown_location(),
            started_at="2026-08-01T00:00:00Z",
            segment_index=index,
        )
        _seed_clip(
            database,
            run_id=latest,
            clip_id=donor_id,
            source_name=source,
            project_name="Capitol Hill Afternoon — Clip 02",
            event_name="Capitol Hill, Seattle — 2025-11-08",
            location=_srt_location(),
            started_at="2026-08-01T00:00:00Z",
            segment_index=index + 10,
        )
        clips.append(
            _source_identity_row(clip_id=clip_id, run_id=latest, source_name=source)
        )
    journal = _write_source_identity(tmp_path / "safety.json", clips)
    service = HistoricalLocationPropagateService(repository, catalog)
    with pytest.raises(RuntimeError, match="injected propagate failure"):
        service.propagate(
            phase=2,
            source_identity_path=journal,
            write=True,
            backup_path=tmp_path / "fail.bak",
            fail_after=1,
        )
    for index in range(2):
        row = _load_candidate(database, latest, f"VCLIP_P2{index}")
        assert row["location"]["public_label"] is None


def test_phase2_backup_prefix(tmp_path: Path) -> None:
    _database, _repository, _catalog, db_path = _setup(tmp_path)
    created = create_pre_restore_backup(
        db_path,
        clock=lambda: datetime(2026, 8, 21, 12, 0, 0),
        name_prefix="pre-location-propagate-phase2",
    )
    assert created.name == "vclip.sqlite3.pre-location-propagate-phase2-20260821-120000.bak"


def test_phase2_state_only_donor_does_not_make_specific_donor_ambiguous(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    latest = "STOCKIFY_NEW"
    source = "DJI_20250813082050_0030_D copy 2.mov"
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_TGT",
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2025-08-13",
        location=_unknown_location(),
        started_at="2026-08-01T00:00:00Z",
    )
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_SEQUIM",
        source_name=source,
        project_name="Sequim Morning — Clip 01",
        event_name="Sequim, Washington — 2025-08-13",
        location=_srt_location(
            lat=48.074719,
            lon=-123.1019,
            neighborhood=None,
            city="Sequim",
            state="Washington",
            public_label="Sequim, Washington",
        ),
        started_at="2026-08-01T00:00:00Z",
        segment_index=1,
    )
    coarse = {
        "status": "resolved",
        "confidence": "medium",
        "evidence_sources": ["final_review_corpus", "historical_location_propagation"],
        "center_lat": None,
        "center_lon": None,
        "city": None,
        "state": "Washington",
        "country": "United States",
        "neighborhood": None,
        "public_label": "Washington",
        "direct_source_gps": False,
        "gps_kind": None,
    }
    _seed_clip(
        database,
        run_id=latest,
        clip_id="VCLIP_STATE",
        source_name=source,
        project_name="Washington Footage — Clip 01",
        event_name="Washington — 2025-08-13",
        location=coarse,
        started_at="2026-08-01T00:00:00Z",
        segment_index=2,
    )
    journal = _write_source_identity(
        tmp_path / "safety.json",
        [_source_identity_row(clip_id="VCLIP_TGT", run_id=latest, source_name=source)],
    )
    report = HistoricalLocationPropagateService(repository, catalog).validate(
        phase=2, source_identity_path=journal
    )
    assert report.safe_to_restore == 1
    assert report.malformed_historical_recovery == 0
    proposed = report.mutations[0]["proposed_location_snapshot"]["location"]
    assert proposed["public_label"] == "Sequim, Washington"
    assert proposed["gps_kind"] == "srt_gps"
