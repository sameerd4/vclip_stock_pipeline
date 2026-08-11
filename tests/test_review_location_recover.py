from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.stockify.core import local_name
from vclip_pipeline.stockify.fcpxml import validate_fcpxml
from vclip_pipeline.util import json_dumps
from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.review_location_recover import (
    CONFIDENCE,
    OVERRIDE_REASON,
    STATUS_AUTOMATIC,
    STATUS_OVERRIDE,
    STATUS_UNRESOLVED,
    ReviewLocationRecoverService,
    geographic_cluster_id,
)

from test_srt_gps import _write_dji_srt


class FixedResolver:
    """Deterministic reverse geocoder for review-location-recover tests."""

    def __init__(self, mapping: dict[tuple[float, float], dict]):
        self.mapping = mapping

    def resolve(self, latitude: float, longitude: float):
        for (lat, lon), place in self.mapping.items():
            if abs(latitude - lat) < 0.05 and abs(longitude - lon) < 0.05:
                return dict(place)
        return None


SEATTLE = {
    "provider": "test",
    "country": "United States",
    "state": "Washington",
    "city": "Seattle",
    "neighborhood": "South Lake Union",
    "poi": None,
    "timezone": "America/Los_Angeles",
}
PORTLAND = {
    "provider": "test",
    "country": "United States",
    "state": "Oregon",
    "city": "Portland",
    "neighborhood": "Pearl District",
    "poi": None,
    "timezone": "America/Los_Angeles",
}


def _seed_unknown_clip(
    database: Database,
    *,
    run_id: str,
    clip_id: str,
    source_name: str,
    project_name: str,
    event_name: str,
    segment_index: int = 0,
    capture_date: str = "2026-04-17",
) -> None:
    event_id = f"EVT_{run_id}"
    session_id = f"SESS_{run_id}"
    project_id = f"PROJ_{run_id}_{segment_index}"
    media_id = f"MEDIA_{clip_id}"
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO stockify_runs (
                id, source_xml_path, source_xml_sha256, output_xml_path, report_path,
                pipeline_version, status, options_json, started_at, completed_at
            ) VALUES (?, 'a.xml', 'h', 'o.xml', 'r.json', '0.1.0', 'complete', '{}', 't', 't')
            """,
            (run_id,),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO source_events
            (id, run_id, source_index, source_name, source_uid)
            VALUES (?, ?, 0, 'Source Event', NULL)
            """,
            (event_id, run_id),
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
                f"unknown-{capture_date}-{run_id}",
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
                Path(source_name).stem,
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
                json_dumps({"status": "unknown", "public_label": None}),
                json_dumps(
                    {"date": capture_date, "captured_at_local": f"{capture_date}T15:00:00"}
                ),
                event_name,
                project_name,
                segment_index + 1,
                project_name,
            ),
        )
        connection.execute(
            """
            INSERT INTO generated_occurrences (
                run_id, stock_clip_id, representation, generated_event_name,
                generated_project_name, source_start, duration,
                timeline_offset, effect_signature
            ) VALUES (?, ?, 'individual', ?, ?, '0s', '10s', '0s', NULL)
            """,
            (run_id, clip_id, event_name, project_name),
        )


def _write_shard(
    path: Path,
    *,
    run_id: str,
    projects: list[tuple[str, str, str]],
) -> tuple[Path, Path]:
    """projects: (event_name, project_name, clip_id)"""
    root = ET.Element("fcpxml", {"version": "1.12"})
    resources = ET.SubElement(root, "resources")
    ET.SubElement(
        resources,
        "format",
        {
            "id": "r1",
            "name": "FFVideoFormat1080p30",
            "frameDuration": "1/30s",
            "width": "1920",
            "height": "1080",
        },
    )
    library = ET.SubElement(root, "library")
    events: dict[str, ET.Element] = {}
    manifest_projects = []
    clip_ids = []
    for index, (event_name, project_name, clip_id) in enumerate(projects, start=1):
        event = events.get(event_name)
        if event is None:
            event = ET.SubElement(
                library, "event", {"name": event_name, "uid": f"event-{index}"}
            )
            events[event_name] = event
        asset_id = f"r{index + 1}"
        asset = ET.SubElement(
            resources,
            "asset",
            {
                "id": asset_id,
                "name": f"media-{clip_id}",
                "uid": f"asset-{clip_id}",
                "start": "0s",
                "duration": "10s",
                "hasVideo": "1",
                "format": "r1",
            },
        )
        ET.SubElement(
            asset,
            "media-rep",
            {
                "kind": "original-media",
                "src": f"file:///tmp/media-{clip_id}.mov",
            },
        )
        project = ET.SubElement(
            event, "project", {"name": project_name, "uid": f"proj-{clip_id}"}
        )
        sequence = ET.SubElement(
            project, "sequence", {"format": "r1", "duration": "10s", "tcStart": "0s"}
        )
        spine = ET.SubElement(sequence, "spine")
        clip = ET.SubElement(
            spine,
            "asset-clip",
            {
                "ref": asset_id,
                "name": project_name,
                "offset": "0s",
                "duration": "10s",
                "start": "0s",
            },
        )
        metadata = ET.SubElement(clip, "metadata")
        ET.SubElement(
            metadata, "md", {"key": "com.vclip.stock_clip_id", "value": clip_id}
        )
        ET.SubElement(
            metadata, "md", {"key": "com.vclip.stockify_run_id", "value": run_id}
        )
        manifest_projects.append(
            {
                "event_name": event_name,
                "project_name": project_name,
                "representation": "individual",
                "stock_clip_ids": [clip_id],
            }
        )
        clip_ids.append(clip_id)

    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root)
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    manifest_path = path.with_name(f"{path.stem}-shard-manifest.json")
    manifest_path.write_text(
        json.dumps(
            {
                "stockify_run_id": run_id,
                "stock_clip_ids": clip_ids,
                "projects": manifest_projects,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path, manifest_path


def _run(
    tmp_path: Path,
    *,
    database: Database,
    resolver: FixedResolver,
    input_root: Path,
    media_root: Path,
    dry_run: bool = False,
    overwrite: bool = True,
    location_overrides: Path | None = None,
):
    repository = CatalogRepository(database)
    catalog = WorkflowCatalog(database)
    output_root = tmp_path / "review-shards-located"
    report = ReviewLocationRecoverService(
        repository, resolver, catalog
    ).run(
        input_root=input_root,
        output_root=output_root,
        media_roots=[media_root],
        report_path=tmp_path / "library-audits" / "location-recovery.json",
        text_report_path=tmp_path / "library-audits" / "location-recovery.txt",
        dry_run=dry_run,
        overwrite=overwrite,
        location_overrides=location_overrides,
    )
    return report, output_root, catalog


def test_homogeneous_gps_event(tmp_path: Path):
    database = Database(tmp_path / "homog.sqlite3")
    database.migrate()
    run_id = "STOCKIFY_HOMOG"
    event = "Unknown Location — 2026-04-17"
    clips = [
        ("VCLIP_H1", "DJI_20260417120000_0001_D.MP4", "Unknown Location Afternoon — Clip 01"),
        ("VCLIP_H2", "DJI_20260417120100_0002_D.MP4", "Unknown Location Afternoon — Clip 02"),
    ]
    media = tmp_path / "media"
    media.mkdir()
    for clip_id, source, project in clips:
        _seed_unknown_clip(
            database,
            run_id=run_id,
            clip_id=clip_id,
            source_name=source,
            project_name=project,
            event_name=event,
            segment_index=int(clip_id[-1]),
        )
        _write_dji_srt(
            media / f"{Path(source).stem}.SRT",
            [
                ("00:00:00,000", 0.0, 0.0),
                ("00:00:01,000", 47.6253, -122.3377),
                ("00:00:01,033", 47.6254, -122.3376),
            ],
        )
    input_root = tmp_path / "review-shards-final" / "market"
    _write_shard(
        input_root / "shard.fcpxml",
        run_id=run_id,
        projects=[(event, project, clip_id) for clip_id, _source, project in clips],
    )
    resolver = FixedResolver({(47.6253, -122.3377): SEATTLE})
    report, output_root, catalog = _run(
        tmp_path,
        database=database,
        resolver=resolver,
        input_root=input_root.parent,
        media_root=media,
    )
    assert report.homogeneous_events == 1
    assert report.mixed_location_events == 0
    assert report.candidates_moved_or_relabelled == 2
    assert report.unknown_clips_after == 0
    out_xml = next(output_root.rglob("*.fcpxml"))
    root = ET.parse(out_xml).getroot()
    assert validate_fcpxml(root).passed
    event_names = {
        node.get("name")
        for node in root.iter()
        if local_name(node.tag) == "event"
    }
    assert any("South Lake Union" in (name or "") for name in event_names)
    assert not any("Unknown Location" in (name or "") for name in event_names)
    assert "VCLIP_H1" in catalog.review_location_recovered_ids(run_id)
    with database.connect() as connection:
        row = connection.execute(
            "SELECT generated_event_name, location_json FROM stock_candidates WHERE stock_clip_id='VCLIP_H1'"
        ).fetchone()
    assert "South Lake Union" in row["generated_event_name"]
    location = json.loads(row["location_json"])
    assert location["confidence"] == CONFIDENCE
    assert location["city"] == "Seattle"


def test_mixed_location_event_splitting(tmp_path: Path):
    database = Database(tmp_path / "mixed.sqlite3")
    database.migrate()
    run_id = "STOCKIFY_MIXED"
    event = "Unknown Location — 2026-04-17"
    seattle_clip = ("VCLIP_M1", "DJI_20260417120000_0001_D.MP4", "Unknown Location Afternoon — Clip 01")
    # Portland is >50km from Seattle so complete-linkage clustering splits.
    portland_clip = ("VCLIP_M2", "DJI_20260417130000_0002_D.MP4", "Unknown Location Afternoon — Clip 02")
    media = tmp_path / "media"
    media.mkdir()
    for clip_id, source, project in (seattle_clip, portland_clip):
        _seed_unknown_clip(
            database,
            run_id=run_id,
            clip_id=clip_id,
            source_name=source,
            project_name=project,
            event_name=event,
            segment_index=int(clip_id[-1]),
        )
    _write_dji_srt(
        media / f"{Path(seattle_clip[1]).stem}.SRT",
        [("00:00:01,000", 47.6253, -122.3377)],
    )
    _write_dji_srt(
        media / f"{Path(portland_clip[1]).stem}.SRT",
        [("00:00:01,000", 45.5300, -122.6850)],
    )
    input_root = tmp_path / "review-shards-final" / "market"
    _write_shard(
        input_root / "shard.fcpxml",
        run_id=run_id,
        projects=[
            (event, seattle_clip[2], seattle_clip[0]),
            (event, portland_clip[2], portland_clip[0]),
        ],
    )
    resolver = FixedResolver(
        {
            (47.6253, -122.3377): SEATTLE,
            (45.5300, -122.6850): PORTLAND,
        }
    )
    report, output_root, _ = _run(
        tmp_path,
        database=database,
        resolver=resolver,
        input_root=input_root.parent,
        media_root=media,
    )
    assert report.mixed_location_events == 1
    assert report.recovered_geographic_clusters == 2
    assert report.candidates_moved_or_relabelled == 2
    assert len(report.clusters) == 2
    assert all(cluster["mixed_location_event"] for cluster in report.clusters)
    assert {cluster["resolved_city"] for cluster in report.clusters} == {
        "Seattle",
        "Portland",
    }
    out_xml = next(output_root.rglob("*.fcpxml"))
    event_names = {
        node.get("name")
        for node in ET.parse(out_xml).getroot().iter()
        if local_name(node.tag) == "event"
    }
    assert any("South Lake Union" in (name or "") for name in event_names)
    assert any("Portland" in (name or "") or "Pearl" in (name or "") for name in event_names)
    assert len([name for name in event_names if name]) >= 2


def test_zero_gps_srt_ignored(tmp_path: Path):
    database = Database(tmp_path / "zero.sqlite3")
    database.migrate()
    run_id = "STOCKIFY_ZERO"
    event = "Unknown Location — 2026-04-17"
    clip_id = "VCLIP_Z1"
    source = "DJI_20260417120000_0001_D.MP4"
    project = "Unknown Location Afternoon — Clip 01"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        project_name=project,
        event_name=event,
    )
    media = tmp_path / "media"
    media.mkdir()
    _write_dji_srt(
        media / f"{Path(source).stem}.SRT",
        [("00:00:00,000", 0.0, 0.0), ("00:00:01,000", 0.0, 0.0)],
    )
    input_root = tmp_path / "review-shards-final" / "market"
    _write_shard(
        input_root / "shard.fcpxml",
        run_id=run_id,
        projects=[(event, project, clip_id)],
    )
    report, output_root, catalog = _run(
        tmp_path,
        database=database,
        resolver=FixedResolver({}),
        input_root=input_root.parent,
        media_root=media,
    )
    assert report.candidates_moved_or_relabelled == 0
    assert report.unknown_clips_after == 1
    assert catalog.review_location_recovered_ids(run_id) == set()
    out_xml = next(output_root.rglob("*.fcpxml"))
    assert "Unknown Location" in ET.parse(out_xml).getroot().find(".//event").get("name")


def test_duplicate_srt_copies_not_double_counted(tmp_path: Path):
    database = Database(tmp_path / "dup.sqlite3")
    database.migrate()
    run_id = "STOCKIFY_DUP"
    event = "Unknown Location — 2026-04-17"
    clip_id = "VCLIP_D1"
    source = "DJI_20260417120000_0001_D.MP4"
    project = "Unknown Location Afternoon — Clip 01"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        project_name=project,
        event_name=event,
    )
    media = tmp_path / "media"
    copy_dir = media / "copies"
    copy_dir.mkdir(parents=True)
    points = [("00:00:01,000", 47.6253, -122.3377)]
    _write_dji_srt(media / f"{Path(source).stem}.SRT", points)
    _write_dji_srt(copy_dir / f"{Path(source).stem}.SRT", points)
    input_root = tmp_path / "review-shards-final" / "market"
    _write_shard(
        input_root / "shard.fcpxml",
        run_id=run_id,
        projects=[(event, project, clip_id)],
    )
    report, _, _ = _run(
        tmp_path,
        database=database,
        resolver=FixedResolver({(47.6253, -122.3377): SEATTLE}),
        input_root=input_root.parent,
        media_root=media,
    )
    assert report.candidates_moved_or_relabelled == 1
    assert report.recovered_geographic_clusters == 1
    recovery = report.recoveries[0]
    assert len(recovery["srt_paths"]) >= 2
    # One observation/vote despite two SRT files.
    assert recovery["provenance"]["cluster_source_count"] == 1


def test_candidate_with_no_srt_untouched(tmp_path: Path):
    database = Database(tmp_path / "nosrt.sqlite3")
    database.migrate()
    run_id = "STOCKIFY_NOSRT"
    event = "Unknown Location — 2026-04-17"
    known_event = "South Lake Union, Seattle — 2026-04-17"
    unknown = ("VCLIP_N1", "DJI_20260417120000_0001_D.MP4", "Unknown Location Afternoon — Clip 01")
    known = ("VCLIP_N2", "DJI_20260417130000_0002_D.MP4", "South Lake Union Afternoon — Clip 01")
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=unknown[0],
        source_name=unknown[1],
        project_name=unknown[2],
        event_name=event,
        segment_index=1,
    )
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=known[0],
        source_name=known[1],
        project_name=known[2],
        event_name=known_event,
        segment_index=2,
    )
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE stock_candidates
            SET location_json=?, generated_event_name=?
            WHERE stock_clip_id=?
            """,
            (
                json_dumps(
                    {
                        "status": "resolved",
                        "public_label": "South Lake Union, Seattle",
                        "city": "Seattle",
                    }
                ),
                known_event,
                known[0],
            ),
        )
    media = tmp_path / "media"
    media.mkdir()
    # No SRT for unknown clip.
    input_root = tmp_path / "review-shards-final" / "market"
    _write_shard(
        input_root / "shard.fcpxml",
        run_id=run_id,
        projects=[
            (event, unknown[2], unknown[0]),
            (known_event, known[2], known[0]),
        ],
    )
    report, output_root, _ = _run(
        tmp_path,
        database=database,
        resolver=FixedResolver({(47.6253, -122.3377): SEATTLE}),
        input_root=input_root.parent,
        media_root=media,
    )
    assert report.candidates_moved_or_relabelled == 0
    assert report.post_write_audit["known_location_candidates_changed"] == []
    out_manifest = json.loads(
        next(output_root.rglob("*-shard-manifest.json")).read_text()
    )
    names = {project["project_name"] for project in out_manifest["projects"]}
    assert unknown[2] in names
    assert known[2] in names


def test_dry_run_mutation_free(tmp_path: Path):
    database = Database(tmp_path / "dry.sqlite3")
    database.migrate()
    run_id = "STOCKIFY_DRY"
    event = "Unknown Location — 2026-04-17"
    clip_id = "VCLIP_DRY1"
    source = "DJI_20260417120000_0001_D.MP4"
    project = "Unknown Location Afternoon — Clip 01"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        project_name=project,
        event_name=event,
    )
    media = tmp_path / "media"
    media.mkdir()
    _write_dji_srt(
        media / f"{Path(source).stem}.SRT",
        [("00:00:01,000", 47.6253, -122.3377)],
    )
    input_root = tmp_path / "review-shards-final" / "market"
    xml_path, _ = _write_shard(
        input_root / "shard.fcpxml",
        run_id=run_id,
        projects=[(event, project, clip_id)],
    )
    before = xml_path.read_bytes()
    report, output_root, catalog = _run(
        tmp_path,
        database=database,
        resolver=FixedResolver({(47.6253, -122.3377): SEATTLE}),
        input_root=input_root.parent,
        media_root=media,
        dry_run=True,
    )
    assert report.dry_run is True
    assert report.candidates_moved_or_relabelled == 1
    assert xml_path.read_bytes() == before
    assert not output_root.exists() or not any(output_root.rglob("*.fcpxml"))
    assert catalog.review_location_recovered_ids(run_id) == set()
    with database.connect() as connection:
        event_name = connection.execute(
            "SELECT generated_event_name FROM stock_candidates WHERE stock_clip_id=?",
            (clip_id,),
        ).fetchone()["generated_event_name"]
    assert "Unknown Location" in event_name


def test_idempotency_and_manifest_xml_consistency(tmp_path: Path):
    database = Database(tmp_path / "idem.sqlite3")
    database.migrate()
    run_id = "STOCKIFY_IDEM"
    event = "Unknown Location — 2026-04-17"
    clip_id = "VCLIP_I1"
    source = "DJI_20260417120000_0001_D.MP4"
    project = "Unknown Location Afternoon — Clip 01"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        project_name=project,
        event_name=event,
    )
    media = tmp_path / "media"
    media.mkdir()
    _write_dji_srt(
        media / f"{Path(source).stem}.SRT",
        [("00:00:01,000", 47.6253, -122.3377)],
    )
    input_root = tmp_path / "review-shards-final" / "market"
    _write_shard(
        input_root / "shard.fcpxml",
        run_id=run_id,
        projects=[(event, project, clip_id)],
    )
    resolver = FixedResolver({(47.6253, -122.3377): SEATTLE})
    report1, output_root, catalog = _run(
        tmp_path,
        database=database,
        resolver=resolver,
        input_root=input_root.parent,
        media_root=media,
    )
    assert report1.candidates_moved_or_relabelled == 1
    out_xml = next(output_root.rglob("*.fcpxml"))
    out_manifest = json.loads(
        out_xml.with_name(f"{out_xml.stem}-shard-manifest.json").read_text()
    )
    xml_project_names = {
        node.get("name")
        for node in ET.parse(out_xml).getroot().iter()
        if local_name(node.tag) == "project"
    }
    manifest_project_names = {
        project["project_name"] for project in out_manifest["projects"]
    }
    assert xml_project_names == manifest_project_names
    assert "location_recover" in out_manifest

    # Second pass on located corpus should be a no-op for unknowns.
    located_input = tmp_path / "located-input"
    shutil.copytree(output_root, located_input)
    report2, output2, _ = _run(
        tmp_path / "second",
        database=database,
        resolver=resolver,
        input_root=located_input,
        media_root=media,
    )
    assert report2.unknown_clips_before == 0
    assert report2.candidates_moved_or_relabelled == 0
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) AS n FROM review_location_recoveries"
            ).fetchone()["n"]
            == 1
        )
    assert clip_id in catalog.review_location_recovered_ids(run_id)
    assert output2.exists()


def test_cluster_report_includes_failed_and_mixed_geocodes(tmp_path: Path):
    database = Database(tmp_path / "clusters.sqlite3")
    database.migrate()
    run_id = "STOCKIFY_CLUSTERS"
    event = "Unknown Location — 2026-04-17"
    seattle = ("VCLIP_C1", "DJI_20260417120000_0001_D.MP4", "Unknown Location Afternoon — Clip 01")
    portland = ("VCLIP_C2", "DJI_20260417130000_0002_D.MP4", "Unknown Location Afternoon — Clip 02")
    remote = ("VCLIP_C3", "DJI_20260417140000_0003_D.MP4", "Unknown Location Afternoon — Clip 03")
    media = tmp_path / "media"
    media.mkdir()
    for index, (clip_id, source, project) in enumerate((seattle, portland, remote), start=1):
        _seed_unknown_clip(
            database,
            run_id=run_id,
            clip_id=clip_id,
            source_name=source,
            project_name=project,
            event_name=event,
            segment_index=index,
        )
    _write_dji_srt(
        media / f"{Path(seattle[1]).stem}.SRT",
        [("00:00:01,000", 47.6253, -122.3377)],
    )
    _write_dji_srt(
        media / f"{Path(portland[1]).stem}.SRT",
        [("00:00:01,000", 45.5300, -122.6850)],
    )
    # Far enough to form its own cluster; FixedResolver has no mapping.
    _write_dji_srt(
        media / f"{Path(remote[1]).stem}.SRT",
        [("00:00:01,000", 64.2008, -149.4937)],
    )
    input_root = tmp_path / "review-shards-final" / "market"
    xml_path, _ = _write_shard(
        input_root / "shard.fcpxml",
        run_id=run_id,
        projects=[
            (event, seattle[2], seattle[0]),
            (event, portland[2], portland[0]),
            (event, remote[2], remote[0]),
        ],
    )
    before_xml = xml_path.read_bytes()
    resolver = FixedResolver(
        {
            (47.6253, -122.3377): SEATTLE,
            (45.5300, -122.6850): PORTLAND,
        }
    )
    report, output_root, catalog = _run(
        tmp_path,
        database=database,
        resolver=resolver,
        input_root=input_root.parent,
        media_root=media,
        dry_run=True,
    )

    assert report.recovered_geographic_clusters == 3
    assert len(report.clusters) == 3
    assert report.mixed_location_events == 1
    assert all(cluster.get("mixed_location_event") is True for cluster in report.clusters)

    payload = json.loads(
        (tmp_path / "library-audits" / "location-recovery.json").read_text()
    )
    assert len(payload["clusters"]) == 3
    assert payload["recovered_geographic_clusters"] == 3

    by_status = {c["resolution_status"] for c in report.clusters}
    assert by_status == {STATUS_AUTOMATIC, STATUS_UNRESOLVED}
    failed = [c for c in report.clusters if c["resolution_status"] == STATUS_UNRESOLVED]
    assert len(failed) == 1
    assert failed[0]["resolved_location"] is None
    assert failed[0]["representative_latitude"] == 64.2008
    assert failed[0]["stock_clip_ids"] == ["VCLIP_C3"]
    assert failed[0]["source_names"]
    assert failed[0]["gps_confidence"] == CONFIDENCE
    assert failed[0]["cluster_id"] == geographic_cluster_id(
        event, 64.2008, -149.4937
    )

    resolved = [c for c in report.clusters if c["resolution_status"] == STATUS_AUTOMATIC]
    assert len(resolved) == 2
    assert {c["resolved_city"] for c in resolved} == {"Seattle", "Portland"}
    assert report.candidates_moved_or_relabelled == 2
    assert len(report.recoveries) == 2

    text = (tmp_path / "library-audits" / "location-recovery.txt").read_text()
    assert "Geographic clusters" in text
    assert "unresolved GPS cluster" in text
    assert "automatic reverse-geocode" in text
    assert "VCLIP_C3" in text

    assert xml_path.read_bytes() == before_xml
    assert not output_root.exists() or not any(output_root.rglob("*.fcpxml"))
    assert catalog.review_location_recovered_ids(run_id) == set()
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) AS n FROM review_location_recoveries"
            ).fetchone()["n"]
            == 0
        )
        events = {
            row["generated_event_name"]
            for row in connection.execute(
                "SELECT generated_event_name FROM stock_candidates WHERE run_id=?",
                (run_id,),
            )
        }
    assert events == {event}


REDMOND_OVERRIDE = {
    "neighborhood": "Overlake",
    "city": "Redmond",
    "state": "Washington",
    "region": "Washington",
    "country": "United States",
    "timezone": "America/Los_Angeles",
}


def test_location_override_applies_to_gps_cluster(tmp_path: Path):
    database = Database(tmp_path / "override.sqlite3")
    database.migrate()
    run_id = "STOCKIFY_OVERRIDE"
    event = "Unknown Location — 2026-04-17"
    clip_id = "VCLIP_O1"
    source = "DJI_20260417120000_0001_D.MP4"
    project = "Unknown Location Afternoon — Clip 01"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        project_name=project,
        event_name=event,
    )
    media = tmp_path / "media"
    media.mkdir()
    _write_dji_srt(
        media / f"{Path(source).stem}.SRT",
        [("00:00:01,000", 47.638078, -122.133280)],
    )
    input_root = tmp_path / "review-shards-final" / "market"
    _write_shard(
        input_root / "shard.fcpxml",
        run_id=run_id,
        projects=[(event, project, clip_id)],
    )
    overrides_path = tmp_path / "overrides.json"
    overrides_path.write_text(
        json.dumps(
            {
                "overrides": [
                    {
                        "original_event": event,
                        "representative_latitude": 47.638078,
                        "representative_longitude": -122.133280,
                        **REDMOND_OVERRIDE,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    # Empty resolver proves the label comes from the override, not reverse-geocode.
    report, output_root, catalog = _run(
        tmp_path,
        database=database,
        resolver=FixedResolver({}),
        input_root=input_root.parent,
        media_root=media,
        location_overrides=overrides_path,
    )
    assert report.overrides_applied == 1
    assert report.overrides_unused == 0
    assert report.candidates_moved_or_relabelled == 1
    assert len(report.clusters) == 1
    cluster = report.clusters[0]
    assert cluster["resolution_status"] == STATUS_OVERRIDE
    assert cluster["resolved_city"] == "Redmond"
    assert cluster["resolved_location"] == "Overlake, Redmond"
    assert cluster["cluster_id"] == geographic_cluster_id(event, 47.638078, -122.133280)
    recovery = report.recoveries[0]
    assert recovery["recovery_reason"] == OVERRIDE_REASON
    assert recovery["resolution_confidence"] == CONFIDENCE
    assert recovery["provenance"]["resolution_status"] == STATUS_OVERRIDE
    location = recovery["provenance"]["location"]
    assert location["center_lat"] == 47.638078
    assert location["center_lon"] == -122.133280
    assert location["evidence_sources"] == ["srt_gps", OVERRIDE_REASON]
    assert location["recovery"]["method"] == OVERRIDE_REASON
    assert "Redmond" in recovery["new_event_name"]
    text = (tmp_path / "library-audits" / "location-recovery.txt").read_text()
    assert "manual GPS override" in text
    assert clip_id in catalog.review_location_recovered_ids(run_id)
    assert any(output_root.rglob("*.fcpxml"))


def test_invalid_override_cluster_id_warns_and_skips(tmp_path: Path):
    database = Database(tmp_path / "bad-override.sqlite3")
    database.migrate()
    run_id = "STOCKIFY_BAD_OVR"
    event = "Unknown Location — 2026-04-17"
    clip_id = "VCLIP_BO1"
    source = "DJI_20260417120000_0001_D.MP4"
    project = "Unknown Location Afternoon — Clip 01"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        project_name=project,
        event_name=event,
    )
    media = tmp_path / "media"
    media.mkdir()
    _write_dji_srt(
        media / f"{Path(source).stem}.SRT",
        [("00:00:01,000", 47.6253, -122.3377)],
    )
    input_root = tmp_path / "review-shards-final" / "market"
    _write_shard(
        input_root / "shard.fcpxml",
        run_id=run_id,
        projects=[(event, project, clip_id)],
    )
    overrides_path = tmp_path / "overrides.json"
    overrides_path.write_text(
        json.dumps(
            {
                "overrides": [
                    {
                        "cluster_id": "gcluster_does_not_exist",
                        **REDMOND_OVERRIDE,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    report, _, _ = _run(
        tmp_path,
        database=database,
        resolver=FixedResolver({(47.6253, -122.3377): SEATTLE}),
        input_root=input_root.parent,
        media_root=media,
        location_overrides=overrides_path,
    )
    assert report.overrides_applied == 0
    assert report.overrides_unused == 1
    assert any("did not match any GPS cluster" in warning for warning in report.warnings)
    assert report.clusters[0]["resolution_status"] == STATUS_AUTOMATIC
    assert report.clusters[0]["resolved_city"] == "Seattle"


def test_override_rejects_gps_only_identity(tmp_path: Path):
    from vclip_pipeline.errors import VClipError
    from vclip_pipeline.workflow.review_location_recover import load_location_overrides

    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "overrides": [
                    {
                        "representative_latitude": 47.6253,
                        "representative_longitude": -122.3377,
                        **REDMOND_OVERRIDE,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    try:
        load_location_overrides(path)
        raise AssertionError("expected VClipError")
    except VClipError as exc:
        assert "cluster_id or original_event" in str(exc)


def test_mixed_event_override_targets_one_cluster(tmp_path: Path):
    database = Database(tmp_path / "mixed-ovr.sqlite3")
    database.migrate()
    run_id = "STOCKIFY_MIX_OVR"
    event = "Unknown Location — 2026-04-17"
    seattle = ("VCLIP_MO1", "DJI_20260417120000_0001_D.MP4", "Unknown Location Afternoon — Clip 01")
    portland = ("VCLIP_MO2", "DJI_20260417130000_0002_D.MP4", "Unknown Location Afternoon — Clip 02")
    media = tmp_path / "media"
    media.mkdir()
    for index, (clip_id, source, project) in enumerate((seattle, portland), start=1):
        _seed_unknown_clip(
            database,
            run_id=run_id,
            clip_id=clip_id,
            source_name=source,
            project_name=project,
            event_name=event,
            segment_index=index,
        )
    _write_dji_srt(
        media / f"{Path(seattle[1]).stem}.SRT",
        [("00:00:01,000", 47.6253, -122.3377)],
    )
    _write_dji_srt(
        media / f"{Path(portland[1]).stem}.SRT",
        [("00:00:01,000", 45.5300, -122.6850)],
    )
    input_root = tmp_path / "review-shards-final" / "market"
    _write_shard(
        input_root / "shard.fcpxml",
        run_id=run_id,
        projects=[
            (event, seattle[2], seattle[0]),
            (event, portland[2], portland[0]),
        ],
    )
    portland_cluster_id = geographic_cluster_id(event, 45.5300, -122.6850)
    overrides_path = tmp_path / "overrides.json"
    overrides_path.write_text(
        json.dumps(
            {
                "overrides": [
                    {
                        "cluster_id": portland_cluster_id,
                        "neighborhood": "Pearl District",
                        "city": "Portland",
                        "state": "Oregon",
                        "country": "United States",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    report, _, _ = _run(
        tmp_path,
        database=database,
        resolver=FixedResolver({(47.6253, -122.3377): SEATTLE}),
        input_root=input_root.parent,
        media_root=media,
        location_overrides=overrides_path,
    )
    assert report.mixed_location_events == 1
    assert len(report.clusters) == 2
    by_id = {cluster["cluster_id"]: cluster for cluster in report.clusters}
    assert by_id[portland_cluster_id]["resolution_status"] == STATUS_OVERRIDE
    assert by_id[portland_cluster_id]["resolved_city"] == "Portland"
    seattle_cluster = next(
        cluster
        for cluster in report.clusters
        if cluster["cluster_id"] != portland_cluster_id
    )
    assert seattle_cluster["resolution_status"] == STATUS_AUTOMATIC
    assert seattle_cluster["resolved_city"] == "Seattle"
    assert report.overrides_applied == 1
    assert report.candidates_moved_or_relabelled == 2


def test_override_idempotent_and_dry_run_mutation_free(tmp_path: Path):
    database = Database(tmp_path / "ovr-idem.sqlite3")
    database.migrate()
    run_id = "STOCKIFY_OVR_IDEM"
    event = "Unknown Location — 2026-04-17"
    clip_id = "VCLIP_OI1"
    source = "DJI_20260417120000_0001_D.MP4"
    project = "Unknown Location Afternoon — Clip 01"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        project_name=project,
        event_name=event,
    )
    media = tmp_path / "media"
    media.mkdir()
    _write_dji_srt(
        media / f"{Path(source).stem}.SRT",
        [("00:00:01,000", 47.638078, -122.133280)],
    )
    input_root = tmp_path / "review-shards-final" / "market"
    xml_path, _ = _write_shard(
        input_root / "shard.fcpxml",
        run_id=run_id,
        projects=[(event, project, clip_id)],
    )
    overrides_path = tmp_path / "overrides.json"
    overrides_path.write_text(
        json.dumps(
            {
                "overrides": [
                    {
                        "cluster_id": geographic_cluster_id(
                            event, 47.638078, -122.133280
                        ),
                        **REDMOND_OVERRIDE,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    before = xml_path.read_bytes()
    dry_report, dry_out, catalog = _run(
        tmp_path / "dry",
        database=database,
        resolver=FixedResolver({}),
        input_root=input_root.parent,
        media_root=media,
        location_overrides=overrides_path,
        dry_run=True,
    )
    assert dry_report.dry_run is True
    assert dry_report.overrides_applied == 1
    assert dry_report.candidates_moved_or_relabelled == 1
    assert xml_path.read_bytes() == before
    assert not dry_out.exists() or not any(dry_out.rglob("*.fcpxml"))
    assert catalog.review_location_recovered_ids(run_id) == set()

    report1, output_root, catalog = _run(
        tmp_path / "pass1",
        database=database,
        resolver=FixedResolver({}),
        input_root=input_root.parent,
        media_root=media,
        location_overrides=overrides_path,
    )
    assert report1.overrides_applied == 1
    assert "Redmond" in report1.recoveries[0]["new_event_name"]

    located_input = tmp_path / "located-input"
    shutil.copytree(output_root, located_input)
    report2, _, _ = _run(
        tmp_path / "pass2",
        database=database,
        resolver=FixedResolver({}),
        input_root=located_input,
        media_root=media,
        location_overrides=overrides_path,
    )
    assert report2.unknown_clips_before == 0
    assert report2.candidates_moved_or_relabelled == 0
    assert report2.overrides_applied == 0
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) AS n FROM review_location_recoveries"
            ).fetchone()["n"]
            == 1
        )
