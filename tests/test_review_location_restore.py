from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_review_location_materialize import _write_minimal_shard
from test_review_location_recover import _seed_unknown_clip

from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.errors import VClipError
from vclip_pipeline.util import json_dumps
from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.review_location_restore import (
    GPS_KIND_JPG,
    HOURS_IN_SILENCE_CAPTURE_DATE,
    HOURS_IN_SILENCE_RUN_ID,
    HOURS_IN_SILENCE_SCREEN_RECORDING,
    HistoricalLocationRestoreService,
    create_pre_restore_backup,
    parse_historical_plan,
)


def _setup(tmp_path: Path) -> tuple[Database, CatalogRepository, WorkflowCatalog, Path]:
    db_path = tmp_path / "vclip.sqlite3"
    database = Database(db_path)
    database.migrate()
    catalog = WorkflowCatalog(database)
    catalog.ensure_schema()
    return database, CatalogRepository(database), catalog, db_path


def _jpg_location(
    lat: float = 47.614093,
    lon: float = -122.318109,
    *,
    neighborhood: str = "Capitol Hill",
) -> dict:
    return {
        "status": "resolved",
        "confidence": "high",
        "evidence_sources": ["jpg_exif_same_shoot", "review_location_materialize"],
        "center_lat": lat,
        "center_lon": lon,
        "city": "Seattle",
        "state": "Washington",
        "country": "United States",
        "neighborhood": neighborhood,
        "public_label": f"{neighborhood}, Seattle",
        "direct_source_gps": False,
        "gps_kind": GPS_KIND_JPG,
    }


def _consensus_location() -> dict:
    return {
        "status": "resolved",
        "confidence": "high",
        "evidence_sources": ["editorial_group_consensus", "review_location_materialize"],
        "center_lat": None,
        "center_lon": None,
        "city": "Seattle",
        "state": "Washington",
        "country": "United States",
        "neighborhood": None,
        "public_label": "Seattle, Washington",
        "direct_source_gps": False,
        "gps_kind": None,
        "recovery": {"method": "editorial_group_consensus", "confidence": "high"},
    }


def _forensic_evidence(
    *,
    source: str,
    clip_id: str,
    lat: float,
    lon: float,
    confidence: str = "high",
) -> dict:
    return {
        "stem": Path(source).stem.lower(),
        "source_basename": source,
        "evidence_kind": "jpg_exif_same_shoot",
        "latitude": lat,
        "longitude": lon,
        "city": "Seattle",
        "neighborhood": "Capitol Hill",
        "state": "Washington",
        "country": "United States",
        "public_label": "Capitol Hill, Seattle",
        "confidence": confidence,
        "review_required": False,
        "direct_source_gps": False,
        "stock_clip_ids": [clip_id],
        "provenance": {
            "jpg_exif_same_shoot": {
                "source_basename": source,
                "latitude": lat,
                "longitude": lon,
                "confidence": confidence,
                "note": (
                    "Coordinates are inferred from same-shoot JPG EXIF GPS; "
                    "not direct source GPS."
                ),
            }
        },
    }


def _plan_row(
    *,
    run_id: str,
    clip_id: str,
    source: str,
    reason: str,
    location: dict,
    lat: float | None,
    lon: float | None,
    confidence: str = "high",
    event: str = "Capitol Hill, Seattle — 2025-11-08",
    project: str = "Capitol Hill, Seattle Afternoon — Clip 01",
    original_event: str = "Unknown Location — 2025-11-08",
    original_project: str = "Unknown Location Afternoon — Clip 01",
) -> dict:
    provenance = {
        "location": location,
        "capture_date": "2025-11-08",
        "recovery_reason": reason,
        "direct_source_gps": False,
        "evidence_sources": list(location.get("evidence_sources") or []),
    }
    if reason == "jpg_exif_same_shoot":
        provenance["gps_kind"] = GPS_KIND_JPG
        provenance["jpg_exif_same_shoot"] = {
            "source_basename": source,
            "latitude": lat,
            "longitude": lon,
            "confidence": confidence,
        }
    return {
        "stockify_run_id": run_id,
        "stock_clip_id": clip_id,
        "original_event_name": original_event,
        "new_event_name": event,
        "original_project_name": original_project,
        "new_project_name": project,
        "source_media": source,
        "srt_paths": [],
        "representative_lat": lat,
        "representative_lon": lon,
        "resolution_confidence": confidence,
        "recovery_reason": reason,
        "source_shard": "shard.fcpxml",
        "input_xml": "shard.fcpxml",
        "output_xml": None,
        "provenance": provenance,
    }


def _write_artifacts(
    tmp_path: Path,
    *,
    recoveries: list[dict],
    evidence: list[dict],
) -> tuple[Path, Path]:
    plan_path = tmp_path / "location-materialization-plan.json"
    forensic_path = tmp_path / "jpg-exif-forensic.json"
    plan_path.write_text(
        json.dumps({"recoveries": recoveries, "total_candidate_mutations": len(recoveries)}),
        encoding="utf-8",
    )
    forensic_path.write_text(
        json.dumps({"jpg_exif_forensic": {"source_level_evidence": evidence}}),
        encoding="utf-8",
    )
    return plan_path, forensic_path


def _update_candidate(
    database: Database,
    *,
    run_id: str,
    clip_id: str,
    location: dict,
    event_name: str,
    project_name: str,
) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE stock_candidates
            SET location_json=?, generated_event_name=?, generated_clip_project_name=?
            WHERE run_id=? AND stock_clip_id=?
            """,
            (json_dumps(location), event_name, project_name, run_id, clip_id),
        )


def _load_candidate(database: Database, run_id: str, clip_id: str) -> dict:
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT location_json, generated_event_name, generated_clip_project_name, session_id
            FROM stock_candidates
            WHERE run_id=? AND stock_clip_id=?
            """,
            (run_id, clip_id),
        ).fetchone()
    return {
        "location": json.loads(row["location_json"]),
        "generated_event_name": row["generated_event_name"],
        "generated_clip_project_name": row["generated_clip_project_name"],
        "session_id": row["session_id"],
    }


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_parse_165_style_plan_rows_deterministically() -> None:
    recoveries = []
    for index in range(165):
        recoveries.append(
            _plan_row(
                run_id="STOCKIFY_PLAN165",
                clip_id=f"VCLIP_{index:04d}",
                source=f"DJI_{index:04d}_D.mp4",
                reason="jpg_exif_same_shoot",
                location=_jpg_location(47.6, -122.3),
                lat=47.6,
                lon=-122.3,
            )
        )
    parsed = parse_historical_plan({"recoveries": recoveries})
    assert len(parsed) == 165
    assert parsed[0].identity == ("STOCKIFY_PLAN165", "VCLIP_0000")
    assert parsed[-1].stock_clip_id == "VCLIP_0164"
    assert parsed[7].recovery_reason == "jpg_exif_same_shoot"


def test_missing_candidate_is_detected_and_blocks_write(tmp_path: Path) -> None:
    database, repository, catalog, db_path = _setup(tmp_path)
    run_id = "STOCKIFY_MISSING"
    clip_id = "VCLIP_MISSING01"
    source = "DJI_20251108213435_0576_D.mp4"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id="VCLIP_OTHER",
        source_name="other.mp4",
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2025-11-08",
        capture_date="2025-11-08",
    )
    location = _jpg_location()
    plan_path, forensic_path = _write_artifacts(
        tmp_path,
        recoveries=[
            _plan_row(
                run_id=run_id,
                clip_id=clip_id,
                source=source,
                reason="jpg_exif_same_shoot",
                location=location,
                lat=47.614093,
                lon=-122.318109,
            )
        ],
        evidence=[
            _forensic_evidence(
                source=source, clip_id=clip_id, lat=47.614093, lon=-122.318109
            )
        ],
    )
    service = HistoricalLocationRestoreService(repository, catalog)
    report = service.validate(plan_path=plan_path, forensic_json=forensic_path)
    assert report.plan_mutations == 1
    assert report.missing_candidates == 1
    assert report.mutations[0]["safety_class"] == "missing_candidate"
    with pytest.raises(VClipError, match="missing candidate"):
        service.restore(
            plan_path=plan_path,
            forensic_json=forensic_path,
            write=True,
            backup_path=tmp_path / "backup.bak",
        )
    assert not (tmp_path / "backup.bak").exists()
    digest_before = _file_digest(db_path)
    service.validate(plan_path=plan_path, forensic_json=forensic_path)
    assert _file_digest(db_path) == digest_before


def test_jpg_over_unknown_is_safe_to_restore(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    run_id = "STOCKIFY_SAFE"
    clip_id = "VCLIP_SAFE01"
    source = "DJI_20251108213435_0576_D.mp4"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2025-11-08",
        capture_date="2025-11-08",
    )
    location = _jpg_location()
    plan_path, forensic_path = _write_artifacts(
        tmp_path,
        recoveries=[
            _plan_row(
                run_id=run_id,
                clip_id=clip_id,
                source=source,
                reason="jpg_exif_same_shoot",
                location=location,
                lat=47.614093,
                lon=-122.318109,
            )
        ],
        evidence=[
            _forensic_evidence(
                source=source, clip_id=clip_id, lat=47.614093, lon=-122.318109
            )
        ],
    )
    service = HistoricalLocationRestoreService(repository, catalog)
    report = service.validate(plan_path=plan_path, forensic_json=forensic_path)
    assert report.mutations[0]["safety_class"] == "safe_to_restore"
    written = service.restore(
        plan_path=plan_path,
        forensic_json=forensic_path,
        write=True,
        backup_path=tmp_path / "pre.bak",
    )
    assert written.backup_path.endswith("pre.bak")
    row = _load_candidate(database, run_id, clip_id)
    assert row["generated_event_name"] == "Capitol Hill, Seattle — 2025-11-08"
    assert row["location"]["gps_kind"] == GPS_KIND_JPG
    assert row["location"]["direct_source_gps"] is False
    with database.connect() as connection:
        recovery = connection.execute(
            "SELECT recovery_reason, provenance_json FROM review_location_recoveries"
        ).fetchone()
    assert recovery["recovery_reason"] == "jpg_exif_same_shoot"
    provenance = json.loads(recovery["provenance_json"])
    assert provenance["location"]["direct_source_gps"] is False
    assert provenance["location"]["gps_kind"] == GPS_KIND_JPG


def test_jpg_does_not_overwrite_direct_srt(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    run_id = "STOCKIFY_SRT"
    clip_id = "VCLIP_SRT01"
    source = "DJI_20251108213435_0576_D.mp4"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2025-11-08",
        capture_date="2025-11-08",
    )
    _update_candidate(
        database,
        run_id=run_id,
        clip_id=clip_id,
        location={
            "status": "resolved",
            "confidence": "confirmed_gps",
            "evidence_sources": ["srt_gps", "srt_gps_review_recovery"],
            "center_lat": 47.61,
            "center_lon": -122.33,
            "city": "Seattle",
            "direct_source_gps": True,
            "gps_kind": "source_srt",
        },
        event_name="South Lake Union, Seattle — 2025-11-08",
        project_name="South Lake Union, Seattle Afternoon — Clip 01",
    )
    plan_path, forensic_path = _write_artifacts(
        tmp_path,
        recoveries=[
            _plan_row(
                run_id=run_id,
                clip_id=clip_id,
                source=source,
                reason="jpg_exif_same_shoot",
                location=_jpg_location(),
                lat=47.614093,
                lon=-122.318109,
            )
        ],
        evidence=[
            _forensic_evidence(
                source=source, clip_id=clip_id, lat=47.614093, lon=-122.318109
            )
        ],
    )
    service = HistoricalLocationRestoreService(repository, catalog)
    report = service.validate(plan_path=plan_path, forensic_json=forensic_path)
    assert report.mutations[0]["safety_class"] == "stronger_current_evidence"
    written = service.restore(
        plan_path=plan_path,
        forensic_json=forensic_path,
        write=True,
        backup_path=tmp_path / "pre.bak",
    )
    assert written.post_write_audit["intended_rows_written"] == 0
    row = _load_candidate(database, run_id, clip_id)
    assert row["generated_event_name"] == "South Lake Union, Seattle — 2025-11-08"
    assert row["location"]["direct_source_gps"] is True


def test_jpg_does_not_overwrite_manual_override(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    run_id = "STOCKIFY_MANUAL"
    clip_id = "VCLIP_MANUAL01"
    source = "DJI_20251108213435_0576_D.mp4"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2025-11-08",
        capture_date="2025-11-08",
    )
    _update_candidate(
        database,
        run_id=run_id,
        clip_id=clip_id,
        location={
            "status": "manual_gps_override",
            "confidence": "confirmed_gps",
            "evidence_sources": ["srt_gps", "manual_gps_override"],
            "center_lat": 47.60,
            "center_lon": -122.33,
            "city": "Seattle",
            "direct_source_gps": True,
        },
        event_name="Manual Place, Seattle — 2025-11-08",
        project_name="Manual Place, Seattle Afternoon — Clip 01",
    )
    plan_path, forensic_path = _write_artifacts(
        tmp_path,
        recoveries=[
            _plan_row(
                run_id=run_id,
                clip_id=clip_id,
                source=source,
                reason="jpg_exif_same_shoot",
                location=_jpg_location(),
                lat=47.614093,
                lon=-122.318109,
            )
        ],
        evidence=[
            _forensic_evidence(
                source=source, clip_id=clip_id, lat=47.614093, lon=-122.318109
            )
        ],
    )
    report = HistoricalLocationRestoreService(repository, catalog).validate(
        plan_path=plan_path, forensic_json=forensic_path
    )
    assert report.mutations[0]["safety_class"] == "stronger_current_evidence"


def test_identical_jpg_result_is_already_applied(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    run_id = "STOCKIFY_APPLIED"
    clip_id = "VCLIP_APPLIED01"
    source = "DJI_20251108213435_0576_D.mp4"
    location = _jpg_location()
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        project_name="Capitol Hill, Seattle Afternoon — Clip 01",
        event_name="Capitol Hill, Seattle — 2025-11-08",
        capture_date="2025-11-08",
    )
    _update_candidate(
        database,
        run_id=run_id,
        clip_id=clip_id,
        location=location,
        event_name="Capitol Hill, Seattle — 2025-11-08",
        project_name="Capitol Hill, Seattle Afternoon — Clip 01",
    )
    plan_path, forensic_path = _write_artifacts(
        tmp_path,
        recoveries=[
            _plan_row(
                run_id=run_id,
                clip_id=clip_id,
                source=source,
                reason="jpg_exif_same_shoot",
                location=location,
                lat=47.614093,
                lon=-122.318109,
            )
        ],
        evidence=[
            _forensic_evidence(
                source=source, clip_id=clip_id, lat=47.614093, lon=-122.318109
            )
        ],
    )
    service = HistoricalLocationRestoreService(repository, catalog)
    report = service.validate(plan_path=plan_path, forensic_json=forensic_path)
    assert report.mutations[0]["safety_class"] == "already_applied"
    written = service.restore(
        plan_path=plan_path,
        forensic_json=forensic_path,
        write=True,
        backup_path=tmp_path / "pre.bak",
    )
    assert written.post_write_audit["intended_rows_written"] == 0


def test_different_inferred_jpg_is_conflicting(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    run_id = "STOCKIFY_CONFLICT"
    clip_id = "VCLIP_CONFLICT01"
    source = "DJI_20251108213435_0576_D.mp4"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        project_name="Fremont, Seattle Afternoon — Clip 01",
        event_name="Fremont, Seattle — 2025-11-08",
        capture_date="2025-11-08",
    )
    _update_candidate(
        database,
        run_id=run_id,
        clip_id=clip_id,
        location=_jpg_location(47.65, -122.35, neighborhood="Fremont"),
        event_name="Fremont, Seattle — 2025-11-08",
        project_name="Fremont, Seattle Afternoon — Clip 01",
    )
    plan_path, forensic_path = _write_artifacts(
        tmp_path,
        recoveries=[
            _plan_row(
                run_id=run_id,
                clip_id=clip_id,
                source=source,
                reason="jpg_exif_same_shoot",
                location=_jpg_location(),
                lat=47.614093,
                lon=-122.318109,
            )
        ],
        evidence=[
            _forensic_evidence(
                source=source, clip_id=clip_id, lat=47.614093, lon=-122.318109
            )
        ],
    )
    service = HistoricalLocationRestoreService(repository, catalog)
    report = service.validate(plan_path=plan_path, forensic_json=forensic_path)
    assert report.mutations[0]["safety_class"] == "conflicting_current_evidence"
    with pytest.raises(VClipError, match="conflicting_current_evidence"):
        service.restore(
            plan_path=plan_path,
            forensic_json=forensic_path,
            write=True,
            backup_path=tmp_path / "pre.bak",
        )


def test_editorial_group_consensus_retains_own_provenance(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    run_id = "STOCKIFY_CONSENSUS"
    clip_id = "VCLIP_SCREEN01"
    source = HOURS_IN_SILENCE_SCREEN_RECORDING
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2025-11-08",
        capture_date="2025-11-08",
    )
    location = _consensus_location()
    plan_path, forensic_path = _write_artifacts(
        tmp_path,
        recoveries=[
            _plan_row(
                run_id=run_id,
                clip_id=clip_id,
                source=source,
                reason="editorial_group_consensus",
                location=location,
                lat=None,
                lon=None,
                event="Seattle, Washington — 2025-11-08",
                project="Seattle, Washington Afternoon — Clip 01",
            )
        ],
        evidence=[],
    )
    service = HistoricalLocationRestoreService(repository, catalog)
    report = service.validate(plan_path=plan_path, forensic_json=forensic_path)
    proposed = report.mutations[0]["proposed_location_snapshot"]["location"]
    assert report.mutations[0]["safety_class"] == "safe_to_restore"
    assert report.mutations[0]["recovery_reason"] == "editorial_group_consensus"
    assert "jpg_exif_same_shoot" not in proposed["evidence_sources"]
    assert proposed["direct_source_gps"] is False
    assert proposed["gps_kind"] in (None, "")
    service.restore(
        plan_path=plan_path,
        forensic_json=forensic_path,
        write=True,
        backup_path=tmp_path / "pre.bak",
    )
    with database.connect() as connection:
        recovery = connection.execute(
            "SELECT recovery_reason, provenance_json FROM review_location_recoveries"
        ).fetchone()
    provenance = json.loads(recovery["provenance_json"])
    assert recovery["recovery_reason"] == "editorial_group_consensus"
    assert "jpg_exif_same_shoot" not in (provenance["location"]["evidence_sources"])


def test_write_is_atomic_injected_failure_rolls_back(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    run_id = "STOCKIFY_ATOMIC"
    recoveries = []
    evidence = []
    for index in range(2):
        clip_id = f"VCLIP_ATOMIC{index}"
        source = f"DJI_20251108200000_{index:04d}_D.mp4"
        _seed_unknown_clip(
            database,
            run_id=run_id,
            clip_id=clip_id,
            source_name=source,
            project_name=f"Unknown Location Afternoon — Clip {index + 1:02d}",
            event_name="Unknown Location — 2025-11-08",
            capture_date="2025-11-08",
            segment_index=index,
        )
        recoveries.append(
            _plan_row(
                run_id=run_id,
                clip_id=clip_id,
                source=source,
                reason="jpg_exif_same_shoot",
                location=_jpg_location(),
                lat=47.614093,
                lon=-122.318109,
                project=f"Capitol Hill, Seattle Afternoon — Clip {index + 1:02d}",
            )
        )
        evidence.append(
            _forensic_evidence(
                source=source, clip_id=clip_id, lat=47.614093, lon=-122.318109
            )
        )
    plan_path, forensic_path = _write_artifacts(
        tmp_path, recoveries=recoveries, evidence=evidence
    )
    service = HistoricalLocationRestoreService(repository, catalog)
    with pytest.raises(RuntimeError, match="injected restore failure"):
        service.restore(
            plan_path=plan_path,
            forensic_json=forensic_path,
            write=True,
            backup_path=tmp_path / "pre.bak",
            fail_after=1,
        )
    for index in range(2):
        row = _load_candidate(database, run_id, f"VCLIP_ATOMIC{index}")
        assert row["generated_event_name"] == "Unknown Location — 2025-11-08"
    with database.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM review_location_recoveries"
        ).fetchone()["n"]
    assert count == 0


def test_backup_refuses_overwrite_on_temp_db(tmp_path: Path) -> None:
    database, _repository, _catalog, db_path = _setup(tmp_path)
    _seed_unknown_clip(
        database,
        run_id="STOCKIFY_BACKUP",
        clip_id="VCLIP_BACKUP01",
        source_name="clip.mp4",
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2025-11-08",
        capture_date="2025-11-08",
    )
    backup = tmp_path / "explicit.bak"
    created = create_pre_restore_backup(db_path, backup_path=backup)
    assert created == backup.resolve()
    assert backup.is_file()
    with pytest.raises(VClipError, match="already exists"):
        create_pre_restore_backup(db_path, backup_path=backup)


def test_session_summary_does_not_flatten_clip_locations(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    run_id = "STOCKIFY_SESSION"
    jpg_source = "DJI_20251108213435_0576_D.mp4"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id="VCLIP_SESSJPG",
        source_name=jpg_source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2025-11-08",
        capture_date="2025-11-08",
        segment_index=0,
    )
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id="VCLIP_SESSSCREEN",
        source_name=HOURS_IN_SILENCE_SCREEN_RECORDING,
        project_name="Unknown Location Afternoon — Clip 02",
        event_name="Unknown Location — 2025-11-08",
        capture_date="2025-11-08",
        segment_index=1,
    )
    plan_path, forensic_path = _write_artifacts(
        tmp_path,
        recoveries=[
            _plan_row(
                run_id=run_id,
                clip_id="VCLIP_SESSJPG",
                source=jpg_source,
                reason="jpg_exif_same_shoot",
                location=_jpg_location(),
                lat=47.614093,
                lon=-122.318109,
            ),
            _plan_row(
                run_id=run_id,
                clip_id="VCLIP_SESSSCREEN",
                source=HOURS_IN_SILENCE_SCREEN_RECORDING,
                reason="editorial_group_consensus",
                location=_consensus_location(),
                lat=None,
                lon=None,
                event="Seattle, Washington — 2025-11-08",
                project="Seattle, Washington Afternoon — Clip 02",
            ),
        ],
        evidence=[
            _forensic_evidence(
                source=jpg_source,
                clip_id="VCLIP_SESSJPG",
                lat=47.614093,
                lon=-122.318109,
            )
        ],
    )
    HistoricalLocationRestoreService(repository, catalog).restore(
        plan_path=plan_path,
        forensic_json=forensic_path,
        write=True,
        backup_path=tmp_path / "pre.bak",
    )
    jpg_row = _load_candidate(database, run_id, "VCLIP_SESSJPG")
    screen_row = _load_candidate(database, run_id, "VCLIP_SESSSCREEN")
    assert jpg_row["location"]["neighborhood"] == "Capitol Hill"
    assert jpg_row["generated_event_name"] == "Capitol Hill, Seattle — 2025-11-08"
    assert screen_row["location"]["neighborhood"] is None
    assert screen_row["generated_event_name"] == "Seattle, Washington — 2025-11-08"
    with database.connect() as connection:
        session = connection.execute(
            "SELECT city, neighborhood, public_label, location_json FROM shoot_sessions"
        ).fetchone()
    assert session["city"] == "Seattle"
    assert session["neighborhood"] is None
    assert "Capitol Hill" not in (session["public_label"] or "")


def test_hours_in_silence_shaped_fixture(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    run_id = HOURS_IN_SILENCE_RUN_ID
    recoveries = []
    evidence = []
    for index in range(31):
        clip_id = f"VCLIP_HIS{index:02d}"
        source = f"DJI_20251108200000_{index:04d}_D.mp4"
        _seed_unknown_clip(
            database,
            run_id=run_id,
            clip_id=clip_id,
            source_name=source,
            project_name=f"Unknown Location Afternoon — Clip {index + 1:02d}",
            event_name="Unknown Location — 2025-11-08",
            capture_date=HOURS_IN_SILENCE_CAPTURE_DATE,
            segment_index=index,
        )
        recoveries.append(
            _plan_row(
                run_id=run_id,
                clip_id=clip_id,
                source=source,
                reason="jpg_exif_same_shoot",
                location=_jpg_location(),
                lat=47.614093,
                lon=-122.318109,
                project=f"Capitol Hill, Seattle Afternoon — Clip {index + 1:02d}",
            )
        )
        evidence.append(
            _forensic_evidence(
                source=source, clip_id=clip_id, lat=47.614093, lon=-122.318109
            )
        )
    screen_id = "VCLIP_HISSCREEN"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=screen_id,
        source_name=HOURS_IN_SILENCE_SCREEN_RECORDING,
        project_name="Unknown Location Afternoon — Clip 32",
        event_name="Unknown Location — 2025-11-08",
        capture_date=HOURS_IN_SILENCE_CAPTURE_DATE,
        segment_index=31,
    )
    recoveries.append(
        _plan_row(
            run_id=run_id,
            clip_id=screen_id,
            source=HOURS_IN_SILENCE_SCREEN_RECORDING,
            reason="editorial_group_consensus",
            location=_consensus_location(),
            lat=None,
            lon=None,
            event="Seattle, Washington — 2025-11-08",
            project="Seattle, Washington Afternoon — Clip 32",
        )
    )
    plan_path, forensic_path = _write_artifacts(
        tmp_path, recoveries=recoveries, evidence=evidence
    )
    report = HistoricalLocationRestoreService(repository, catalog).validate(
        plan_path=plan_path,
        forensic_json=forensic_path,
        hours_in_silence_session_id=f"SESS_{run_id}",
    )
    hours = report.hours_in_silence
    assert hours["historical_mutations"] == 32
    assert hours["matched_candidates"] == 32
    assert hours["jpg_mutations"] == 31
    assert hours["consensus_mutations"] == 1
    assert hours["missing"] == 0
    assert hours["screen_recording"]["editorial_group_consensus"] is True
    assert hours["screen_recording"]["no_fake_direct_or_jpg_gps"] is True
    screen_mutation = next(
        item for item in report.mutations if item["stock_clip_id"] == screen_id
    )
    assert screen_mutation["recovery_reason"] == "editorial_group_consensus"
    sources = screen_mutation["proposed_location_snapshot"]["location"]["evidence_sources"]
    assert "jpg_exif_same_shoot" not in sources


def test_validator_performs_zero_writes(tmp_path: Path) -> None:
    database, repository, catalog, db_path = _setup(tmp_path)
    run_id = "STOCKIFY_READONLY"
    clip_id = "VCLIP_READONLY01"
    source = "DJI_20251108213435_0576_D.mp4"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2025-11-08",
        capture_date="2025-11-08",
    )
    plan_path, forensic_path = _write_artifacts(
        tmp_path,
        recoveries=[
            _plan_row(
                run_id=run_id,
                clip_id=clip_id,
                source=source,
                reason="jpg_exif_same_shoot",
                location=_jpg_location(),
                lat=47.614093,
                lon=-122.318109,
            )
        ],
        evidence=[
            _forensic_evidence(
                source=source, clip_id=clip_id, lat=47.614093, lon=-122.318109
            )
        ],
    )
    before = _file_digest(db_path)
    report = HistoricalLocationRestoreService(repository, catalog).validate(
        plan_path=plan_path, forensic_json=forensic_path
    )
    assert report.read_only is True
    assert _file_digest(db_path) == before
    with database.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM review_location_recoveries"
        ).fetchone()["n"]
        event = connection.execute(
            "SELECT generated_event_name FROM stock_candidates WHERE stock_clip_id=?",
            (clip_id,),
        ).fetchone()["generated_event_name"]
    assert count == 0
    assert event == "Unknown Location — 2025-11-08"


def test_fcpxml_cross_check_is_read_only(tmp_path: Path) -> None:
    database, repository, catalog, _db_path = _setup(tmp_path)
    run_id = "STOCKIFY_XML"
    clip_id = "VCLIP_XML01"
    source = "DJI_20251108213435_0576_D.mp4"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2025-11-08",
        capture_date="2025-11-08",
    )
    review_root = tmp_path / "review-shards-location-final"
    event_name = "Capitol Hill, Seattle — 2025-11-08"
    project_name = "Capitol Hill, Seattle Afternoon — Clip 01"
    _write_minimal_shard(
        review_root,
        relative="november-2025/unknown-01.fcpxml",
        event_name=event_name,
        project_name=project_name,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
    )
    xml_path = review_root / "november-2025/unknown-01.fcpxml"
    before = xml_path.read_bytes()
    plan_path, forensic_path = _write_artifacts(
        tmp_path,
        recoveries=[
            _plan_row(
                run_id=run_id,
                clip_id=clip_id,
                source=source,
                reason="jpg_exif_same_shoot",
                location=_jpg_location(),
                lat=47.614093,
                lon=-122.318109,
                event=event_name,
                project=project_name,
            )
        ],
        evidence=[
            _forensic_evidence(
                source=source, clip_id=clip_id, lat=47.614093, lon=-122.318109
            )
        ],
    )
    report = HistoricalLocationRestoreService(repository, catalog).validate(
        plan_path=plan_path,
        forensic_json=forensic_path,
        review_root=review_root,
    )
    xml = report.fcpxml_cross_check
    assert xml["read_only"] is True
    assert xml["plan_rows_represented_in_final_corpus"] == 1
    assert xml["missing_from_final_corpus_count"] == 0
    assert xml["name_mismatch_count"] == 0
    assert xml_path.read_bytes() == before
