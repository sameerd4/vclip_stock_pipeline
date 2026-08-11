from __future__ import annotations

import json
from pathlib import Path

from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.util import json_dumps
from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.review_location_materialize import (
    EXPECTED_MUTATION_COUNTS,
    ReviewLocationMaterializeService,
)
from vclip_pipeline.workflow.review_location_recover import (
    ReviewLocationRecoverService,
)

from test_review_location_recover import FixedResolver, _seed_unknown_clip


def _write_minimal_shard(
    root: Path,
    *,
    relative: str,
    event_name: str,
    project_name: str,
    run_id: str,
    clip_id: str,
    source_name: str,
) -> None:
    xml_path = root / relative
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
  <resources>
    <format id="r1" name="FFVideoFormat1080p30" frameDuration="100/3000s" width="1920" height="1080"/>
    <asset id="r2" name="{source_name}" start="0s" duration="10s" hasVideo="1" format="r1"/>
  </resources>
  <library>
    <event name="{event_name}">
      <project name="{project_name}">
        <sequence format="r1" duration="10s" tcStart="0s" tcFormat="NDF">
          <spine>
            <asset-clip ref="r2" name="{source_name}" duration="10s" start="0s" offset="0s">
              <metadata>
                <md key="com.vclip.stockify_run_id" value="{run_id}"/>
                <md key="com.vclip.stock_clip_id" value="{clip_id}"/>
              </metadata>
            </asset-clip>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
""",
        encoding="utf-8",
    )
    manifest = {
        "stockify_run_id": run_id,
        "projects": [
            {
                "project_name": project_name,
                "event_name": event_name,
                "stock_clip_ids": [clip_id],
                "representation": "individual",
            }
        ],
    }
    xml_path.with_name(f"{xml_path.stem}-shard-manifest.json").write_text(
        json_dumps(manifest), encoding="utf-8"
    )


def test_materialize_dry_run_consumes_persisted_forensic(tmp_path: Path) -> None:
    db_path = tmp_path / "vclip.sqlite3"
    database = Database(db_path)
    database.migrate()
    WorkflowCatalog(database).ensure_schema()
    repository = CatalogRepository(database)

    run_id = "STOCKIFY_MATTEST"
    clip_id = "VCLIP_MATTESTJPG01"
    source = "DJI_20251108120000_0001_D.mp4"
    _seed_unknown_clip(
        database,
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
        project_name="Unknown Location Afternoon — Clip 01",
        event_name="Unknown Location — 2025-11-08",
        capture_date="2025-11-08",
    )

    input_root = tmp_path / "review-shards-t9-recovery"
    _write_minimal_shard(
        input_root,
        relative="november-2025/november-2025-restockified-review--unknown--01.fcpxml",
        event_name="Unknown Location — 2025-11-08",
        project_name="Unknown Location Afternoon — Clip 01",
        run_id=run_id,
        clip_id=clip_id,
        source_name=source,
    )

    forensic = {
        "recoveries": [],
        "jpg_exif_forensic": {
            "source_level_evidence": [
                {
                    "stem": "dji_20251108120000_0001_d",
                    "source_basename": source,
                    "evidence_kind": "jpg_exif_same_shoot",
                    "latitude": 47.62,
                    "longitude": -122.32,
                    "city": "Seattle",
                    "neighborhood": "Capitol Hill",
                    "state": "Washington",
                    "country": "United States",
                    "public_label": "Capitol Hill, Seattle",
                    "confidence": "high",
                    "review_required": False,
                    "direct_source_gps": False,
                    "stock_clip_ids": [clip_id],
                    "evidence_files": ["/media/fake/DJI_20251108120100_0002_D.JPG"],
                    "provenance": {
                        "jpg_exif_same_shoot": {
                            "source_basename": source,
                            "latitude": 47.62,
                            "longitude": -122.32,
                            "confidence": "high",
                            "evidence_photos": [
                                {
                                    "path": "/media/fake/DJI_20251108120100_0002_D.JPG",
                                    "latitude": 47.62,
                                    "longitude": -122.32,
                                    "role": "primary",
                                }
                            ],
                            "note": "Coordinates are inferred from same-shoot JPG EXIF GPS; not direct source GPS.",
                        }
                    },
                }
            ],
            "editorial_groups": [],
            "editorial_group_summary": {
                "out_of_scope_non_drone_keys": [],
                "out_of_scope_non_drone_unresolved_keys": [],
            },
        },
    }
    forensic_path = tmp_path / "jpg-exif-forensic.json"
    forensic_path.write_text(json_dumps(forensic), encoding="utf-8")

    output_root = tmp_path / "review-shards-location-final"
    plan_json = tmp_path / "location-materialization-plan.json"
    plan_text = tmp_path / "location-materialization-plan.txt"

    report = ReviewLocationMaterializeService(repository, WorkflowCatalog(database)).run(
        input_root=input_root,
        output_root=output_root,
        forensic_json=forensic_path,
        projected_coverage_json=None,
        plan_json=plan_json,
        plan_text=plan_text,
        dry_run=True,
        skip_color_integrity=True,
    )

    assert not output_root.exists()
    assert report.total_candidate_mutations == 1
    assert report.mutations_by_type.get("recoverable_jpg_exif") == 1
    assert report.plan_rows[0]["projected_location"] == "Capitol Hill, Seattle"
    assert report.plan_rows[0]["evidence_kind"] == "jpg_exif_same_shoot"
    assert report.plan_rows[0]["inferred_coordinates"]["kind"] == (
        "inferred_jpg_exif_same_shoot"
    )
    assert report.recoveries[0]["provenance"]["direct_source_gps"] is False
    assert plan_json.is_file()
    assert plan_text.is_file()
    # Fixture universe is tiny, so expected production counts should not match.
    assert report.dry_run_checks["expected_mutation_counts_match"] is False
    assert report.dry_run_checks["physical_manifest_assertions"]["matches_expected"] is False
    assert EXPECTED_MUTATION_COUNTS["recoverable_jpg_exif"] == 156
    assert report.candidates_after_known == 1
    assert report.candidates_after_unknown == 0


def test_discover_shards_still_works_after_optional_lat(tmp_path: Path) -> None:
    """Smoke: LocationRecoveryRow accepts null coordinates for consensus rows."""
    from vclip_pipeline.workflow.review_location_recover import LocationRecoveryRow

    row = LocationRecoveryRow(
        stockify_run_id="r",
        stock_clip_id="c",
        original_event_name="Unknown Location — 2025-11-08",
        new_event_name="Seattle, Washington — 2025-11-08",
        original_project_name="Unknown Location Afternoon — Clip 01",
        new_project_name="Seattle Afternoon — Clip 01",
        source_media="x.mp4",
        srt_paths=[],
        representative_lat=None,
        representative_lon=None,
        resolution_confidence="high",
        recovery_reason="editorial_group_consensus",
        source_shard="a.fcpxml",
        input_xml="/tmp/a.fcpxml",
        output_xml=None,
    )
    assert row.representative_lat is None
    resolver = FixedResolver({})
    database = Database(tmp_path / "db.sqlite3")
    database.migrate()
    service = ReviewLocationRecoverService(CatalogRepository(database), resolver)
    assert service is not None
