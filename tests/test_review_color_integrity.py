from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.stockify.fcpxml import parse_source, read_vclip_metadata
from vclip_pipeline.stockify.sidecars import extract_srt_color_md
from vclip_pipeline.util import json_dumps
from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.review_color_integrity import (
    KNOWN_REGRESSION_CLIP_ID,
    ReviewColorIntegrityService,
    naive_project_effect_inventory,
)
from vclip_pipeline.workflow.review_dlog_lut_audit import (
    CLASS_CORRECT,
    CLASS_DB_XML_MISSING,
    CLASS_NO_LUT,
    CLASS_UNKNOWN_IDENTITY,
    CLASS_WRONG,
    CLASS_XML_DB_MISSING,
    classify_dlog_candidate,
    collect_xml_lut_details,
    color_md_for_source,
    detect_camera_model,
    normalize_lut_identity,
    srt_lookup_stems,
)
from vclip_pipeline.stockify.fcpxml import build_resource_index, first_direct_child
from vclip_pipeline.workflow.review_shard import ReviewShardService

from test_review_dedupe import _individual_projects, _markets_path, _project_clip_id


NS = "http://www.apple.com/finalcutpro/fcpxml"
NSMAP = {"fcp": NS}


def _write_namespaced_shard(path: Path, *, project_name: str, clip_id: str) -> None:
    """Build a minimal namespaced FCPXML that naive tag matching cannot traverse."""
    path.write_text(
        f"""<?xml version='1.0' encoding='utf-8'?>
<fcp:fcpxml xmlns:fcp="{NS}" version="1.12">
  <fcp:resources>
    <fcp:format id="r1" name="FFVideoFormat3840x2160p30" frameDuration="100/3000s" width="3840" height="2160"/>
    <fcp:asset id="r2" name="DJI_20250515120000_0001_D" uid="ASSET1" start="0s" duration="10s" hasVideo="1" format="r1" customLUTOverride="LUT:test (DJI Mini 5 Pro D-Log M to Rec.709 LUT)">
      <fcp:media-rep kind="original-media" src="file:///tmp/DJI_20250515120000_0001_D.mov"/>
    </fcp:asset>
    <fcp:effect id="rfx" name="Custom LUT" uid="custom-lut"/>
  </fcp:resources>
  <fcp:library>
    <fcp:event name="Test Event — 2025-05-15">
      <fcp:project name="{project_name}" uid="proj-1">
        <fcp:sequence format="r1" duration="10s" tcStart="0s" tcFormat="NDF">
          <fcp:spine>
            <fcp:asset-clip ref="r2" name="clip" offset="0s" duration="10s" start="0s">
              <fcp:filter-video ref="rfx" name="Custom LUT">
                <fcp:param name="LUT Name" key="1" value="DJI Mini 5 Pro D-Log M to Rec.709 LUT"/>
              </fcp:filter-video>
              <fcp:metadata>
                <fcp:md key="com.vclip.stock_clip_id" value="{clip_id}"/>
                <fcp:md key="com.vclip.stockify_run_id" value="STOCKIFY_TEST_COLOR"/>
              </fcp:metadata>
            </fcp:asset-clip>
          </fcp:spine>
        </fcp:sequence>
      </fcp:project>
    </fcp:event>
  </fcp:library>
</fcp:fcpxml>
""",
        encoding="utf-8",
    )


def test_naive_hand_parser_fails_on_namespaced_fcpxml(tmp_path: Path):
    xml_path = tmp_path / "namespaced.fcpxml"
    project_name = "Wallingford Afternoon — Mixed 3 — Clip 17"
    clip_id = KNOWN_REGRESSION_CLIP_ID
    _write_namespaced_shard(xml_path, project_name=project_name, clip_id=clip_id)

    # Historical bug: ElementTree iter("project") does not match Clark-notation tags.
    root = ET.parse(xml_path).getroot()
    assert list(root.iter("project")) == []
    assert naive_project_effect_inventory(xml_path, project_name) is None

    # Production loader + local-name traversal still resolves the project/clip.
    tree = parse_source(xml_path)
    service = ReviewColorIntegrityService.__new__(ReviewColorIntegrityService)
    indexed = ReviewColorIntegrityService._index_projects(service, xml_path)
    assert clip_id in indexed
    assert indexed[clip_id]["xml_has_lut"] is True
    assert "Custom LUT" in indexed[clip_id]["xml_custom_lut_names"]
    clip = next(
        node
        for node in tree.getroot().iter()
        if node.tag.endswith("asset-clip")
    )
    assert read_vclip_metadata(clip)["com.vclip.stock_clip_id"] == clip_id


def test_color_integrity_resolves_real_shard_fixture(pipeline_run, tmp_path: Path):
    shard_dir = tmp_path / "final-shard"
    ReviewShardService(pipeline_run["repository"]).run(
        review_xml=pipeline_run["output"],
        output_directory=shard_dir,
        markets_path=_markets_path(),
        group_by="none",
        representation="individual",
        max_projects=100,
        max_megabytes=None,
        include_scope_markers=True,
        include_compilations=False,
        overwrite=True,
        dry_run=False,
        report_path=None,
    )
    xml_path = next(shard_dir.glob("*.fcpxml"))
    manifest_path = xml_path.with_name(f"{xml_path.stem}-shard-manifest.json")
    assert manifest_path.is_file()

    input_root = tmp_path / "review-shards-final"
    market = input_root / "market-a"
    market.mkdir(parents=True)
    dest_xml = market / xml_path.name
    dest_xml.write_bytes(xml_path.read_bytes())
    dest_manifest = dest_xml.with_name(f"{dest_xml.stem}-shard-manifest.json")
    dest_manifest.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")

    # Ensure at least one accepted candidate has a camera_lut for aggregate coverage.
    project = _individual_projects(ET.parse(dest_xml).getroot())[0]
    clip_id = _project_clip_id(project)
    with pipeline_run["database"].transaction() as connection:
        connection.execute(
            """
            UPDATE stock_candidates
            SET camera_lut=?,
                effect_signature=COALESCE(effect_signature, 'sig-test')
            WHERE stock_clip_id=?
            """,
            (
                "LUT:test (DJI Mini 5 Pro D-Log M to Rec.709 LUT)",
                clip_id,
            ),
        )

    report_path = tmp_path / "final-color-integrity.json"
    text_path = tmp_path / "final-color-integrity.txt"
    catalog = WorkflowCatalog(pipeline_run["database"])
    report = ReviewColorIntegrityService(pipeline_run["repository"]).run(
        input_root=input_root,
        report_path=report_path,
        text_report_path=text_path,
    )

    assert report.manifest_identities >= 1
    assert report.resolved_projects == report.manifest_identities
    assert report.unresolved_identities == 0
    assert not any(item.status == "xml_parse_error" for item in report.records)
    matched = next(item for item in report.records if item.stock_clip_id == clip_id)
    assert matched.status == "ok"
    assert matched.db_has_lut is True
    assert matched.shard_path.endswith(dest_xml.name)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["aggregates"]["db_lut_xml_lut"] + payload["aggregates"][
        "db_lut_xml_no_lut"
    ] + payload["aggregates"]["db_no_lut_xml_lut"] + payload["aggregates"][
        "db_no_lut_xml_no_lut"
    ] == report.resolved_projects
    assert text_path.is_file()
    # Silence unused catalog lint in some environments.
    assert catalog is not None


def test_color_integrity_indexes_namespaced_corpus_end_to_end(pipeline_run, tmp_path: Path):
    run_id = pipeline_run["result"].stockify_run_id
    with pipeline_run["database"].connect() as connection:
        row = connection.execute(
            """
            SELECT stock_clip_id, generated_clip_project_name
            FROM stock_candidates
            WHERE run_id=? AND eligibility_status='accepted'
            ORDER BY clip_sequence
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
    clip_id = row["stock_clip_id"]
    project_name = row["generated_clip_project_name"] or f"Project — {clip_id}"

    input_root = tmp_path / "review-shards-final" / "ns"
    input_root.mkdir(parents=True)
    xml_path = input_root / "namespaced-review.fcpxml"
    _write_namespaced_shard(xml_path, project_name=project_name, clip_id=clip_id)
    xml_path.with_name(f"{xml_path.stem}-shard-manifest.json").write_text(
        json.dumps(
            {
                "stockify_run_id": run_id,
                "stock_clip_ids": [clip_id],
                "projects": [
                    {
                        "event_name": "Test Event — 2025-05-15",
                        "project_name": project_name,
                        "representation": "individual",
                        "stock_clip_ids": [clip_id],
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    with pipeline_run["database"].transaction() as connection:
        connection.execute(
            """
            UPDATE stock_candidates
            SET camera_lut=?
            WHERE run_id=? AND stock_clip_id=?
            """,
            (
                "LUT:test (DJI Mini 5 Pro D-Log M to Rec.709 LUT)",
                run_id,
                clip_id,
            ),
        )

    report = ReviewColorIntegrityService(pipeline_run["repository"]).run(
        input_root=input_root.parent,
        report_path=tmp_path / "ns.json",
        text_report_path=tmp_path / "ns.txt",
    )
    assert report.manifest_identities == 1
    assert report.resolved_projects == 1
    assert report.unresolved_identities == 0
    assert report.records[0].xml_has_lut is True
    assert report.records[0].db_has_lut is True
    assert report.db_lut_xml_lut == 1


def _write_color_srt(path: Path, *, color_md: str, lat: float = 47.6, lon: float = -122.3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "1",
                "00:00:00,000 --> 00:00:00,033",
                (
                    f'<font size="28">FrameCnt: 1, DiffTime: 33ms\n'
                    f"2025-03-15 12:00:00.000\n"
                    f"[iso: 100] [color_md: {color_md}] "
                    f"[latitude: {lat:.6f}] [longitude: {lon:.6f}] "
                    f"[rel_alt: 10.0]</font>"
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )


def _seed_candidate(
    database: Database,
    *,
    run_id: str,
    clip_id: str,
    source_name: str,
    camera_lut: str | None,
    media_path: str,
    capture_date: str = "2025-03-15",
    source_index: int = 0,
) -> None:
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
            (f"EVT_{run_id}", run_id),
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
                'afternoon', 'medium', 'Event', 'Base',
                ?, 'not_enriched', '{}', ?, 't', 't'
            )
            """,
            (
                f"SESS_{run_id}",
                run_id,
                f"sess-{run_id}",
                capture_date,
                f"{capture_date}T12:00:00",
                clip_id,
                json_dumps({"date": capture_date}),
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO source_media (
                id, run_id, asset_ref, asset_name, original_filename, media_path,
                normalized_stem, duration, duration_seconds, fps, location_json,
                camera_lut, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '60s', 60.0, 30, '{}', ?, 't', 't')
            """,
            (
                f"MEDIA_{clip_id}",
                run_id,
                f"r-{clip_id}",
                source_name,
                source_name,
                media_path,
                Path(source_name).stem,
                camera_lut,
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
                ?, ?, 'Event', 'Label', 'Compilation', 1, 0, 't', 't'
            )
            """,
            (
                f"PROJ_{clip_id}",
                run_id,
                f"EVT_{run_id}",
                source_index,
                f"SESS_{run_id}",
                source_index,
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
                camera_lut, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, NULL, ?, 'accepted',
                NULL, NULL, '0s', '10s', 10.0, '0s', '10s', 10.0, 'not_applicable', 'A_clean_10s',
                NULL, 'matched', 'matched', '[]', NULL, '[]', '{}', '{}',
                ?, '{"label":"afternoon"}', '{}', '[]',
                'Event', 'Label', 'Compilation', ?, 1, ?,
                ?, 't', 't'
            )
            """,
            (
                run_id,
                clip_id,
                f"PROJ_{clip_id}",
                f"MEDIA_{clip_id}",
                f"SESS_{run_id}",
                source_index,
                source_name,
                json_dumps({"date": capture_date}),
                f"Project — {clip_id}",
                f"Project — {clip_id}",
                camera_lut,
            ),
        )


def _write_lut_shard(
    path: Path,
    *,
    run_id: str,
    clip_id: str,
    project_name: str,
    lut_name: str | None,
    effect_uid: str = "custom-lut-mini5",
) -> None:
    lut_filter = ""
    custom_override = ""
    effect_resource = ""
    if lut_name is not None:
        custom_override = f' customLUTOverride="LUT:test ({lut_name})"'
        effect_resource = (
            f'    <fcp:effect id="rfx" name="Custom LUT" uid="{effect_uid}"/>\n'
        )
        lut_filter = f"""              <fcp:filter-video ref="rfx" name="Custom LUT">
                <fcp:param name="LUT Name" key="1" value="{lut_name}"/>
              </fcp:filter-video>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""<?xml version='1.0' encoding='utf-8'?>
<fcp:fcpxml xmlns:fcp="{NS}" version="1.12">
  <fcp:resources>
    <fcp:format id="r1" name="FFVideoFormat3840x2160p30" frameDuration="100/3000s" width="3840" height="2160"/>
    <fcp:asset id="r2" name="media" uid="ASSET1" start="0s" duration="10s" hasVideo="1" format="r1"{custom_override}>
      <fcp:media-rep kind="original-media" src="file:///tmp/media.mov"/>
    </fcp:asset>
{effect_resource}  </fcp:resources>
  <fcp:library>
    <fcp:event name="Test Event">
      <fcp:project name="{project_name}" uid="proj-1">
        <fcp:sequence format="r1" duration="10s" tcStart="0s" tcFormat="NDF">
          <fcp:spine>
            <fcp:asset-clip ref="r2" name="clip" offset="0s" duration="10s" start="0s">
{lut_filter}              <fcp:metadata>
                <fcp:md key="com.vclip.stock_clip_id" value="{clip_id}"/>
                <fcp:md key="com.vclip.stockify_run_id" value="{run_id}"/>
              </fcp:metadata>
            </fcp:asset-clip>
          </fcp:spine>
        </fcp:sequence>
      </fcp:project>
    </fcp:event>
  </fcp:library>
</fcp:fcpxml>
""",
        encoding="utf-8",
    )
    path.with_name(f"{path.stem}-shard-manifest.json").write_text(
        json.dumps(
            {
                "stockify_run_id": run_id,
                "stock_clip_ids": [clip_id],
                "projects": [
                    {
                        "event_name": "Test Event",
                        "project_name": project_name,
                        "representation": "individual",
                        "stock_clip_ids": [clip_id],
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_extract_srt_color_md_literals(tmp_path: Path):
    dlog = tmp_path / "dlog.SRT"
    default = tmp_path / "default.SRT"
    _write_color_srt(dlog, color_md="dlog_m")
    _write_color_srt(default, color_md="default")
    assert extract_srt_color_md(dlog) == "dlog_m"
    assert extract_srt_color_md(default) == "default"


def test_srt_lookup_matches_fcp_duplicate_filename(tmp_path: Path):
    media = tmp_path / "media"
    srt = media / "DJI_20251219160353_0100_D.SRT"
    _write_color_srt(srt, color_md="dlog_m")
    stems = srt_lookup_stems("DJI_20251219160353_0100_D (fcp1).mov")
    assert "dji_20251219160353_0100_d" in stems
    index = {"dji_20251219160353_0100_d": [srt]}
    color_md, path = color_md_for_source(
        "DJI_20251219160353_0100_D (fcp1).mov",
        index,
    )
    assert color_md == "dlog_m"
    assert path == str(srt)


def test_collect_xml_lut_details_uses_asset_override_for_ozxml_blob(tmp_path: Path):
    """Corpus Custom LUTs often store opaque ozxml in param LUT/key=3."""
    opaque = "PD94bWwgdmVyc2lvbj0iMS4wIj8+PG96eG1sPk1pbmk1UHJvRExvZ01EVW1teTwvb3p4bWw+"
    path = tmp_path / "ozxml.fcpxml"
    path.write_text(
        f"""<?xml version='1.0' encoding='utf-8'?>
<fcp:fcpxml xmlns:fcp="{NS}" version="1.12">
  <fcp:resources>
    <fcp:format id="r1" name="FFVideoFormat3840x2160p30" frameDuration="100/3000s" width="3840" height="2160"/>
    <fcp:asset id="r2" name="media" uid="ASSET1" start="0s" duration="10s" hasVideo="1" format="r1"
      customLUTOverride="LUT:908403d40286925c5b19129c4be6c0f4 (DJI Mini 5 Pro D-Log M to Rec.709 LUT)">
      <fcp:media-rep kind="original-media" src="file:///tmp/media.mov"/>
    </fcp:asset>
    <fcp:effect id="rfx" name="Custom LUT" uid="FxPlug:14B39AEF-607D-42DF-98DD-DB3DD345E925"/>
  </fcp:resources>
  <fcp:library>
    <fcp:event name="Test Event">
      <fcp:project name="Project — ozxml" uid="proj-1">
        <fcp:sequence format="r1" duration="10s" tcStart="0s" tcFormat="NDF">
          <fcp:spine>
            <fcp:asset-clip ref="r2" name="clip" offset="0s" duration="10s" start="0s">
              <fcp:filter-video ref="rfx" name="Custom LUT">
                <fcp:param name="LUT" key="3" value="{opaque}"/>
                <fcp:param name="Input Color Space" key="4" value="1"/>
                <fcp:param name="Output Color Space" key="5" value="1"/>
              </fcp:filter-video>
              <fcp:metadata>
                <fcp:md key="com.vclip.stock_clip_id" value="VCLIP_OZXML"/>
              </fcp:metadata>
            </fcp:asset-clip>
          </fcp:spine>
        </fcp:sequence>
      </fcp:project>
    </fcp:event>
  </fcp:library>
</fcp:fcpxml>
""",
        encoding="utf-8",
    )
    root = parse_source(path).getroot()
    resource_index = build_resource_index(first_direct_child(root, "resources"))
    clip = next(node for node in root.iter() if str(node.tag).endswith("asset-clip"))
    details = collect_xml_lut_details(clip, resource_index)
    assert len(details) == 1
    assert details[0]["normalized_lut_identity"] == (
        "DJI Mini 5 Pro D-Log M to Rec.709 LUT"
    )
    assert details[0]["lut_camera_model"] == "DJI Mini 5 Pro"
    assert details[0]["lut_data_fingerprint"]
    assert opaque not in str(details[0]["params"])
    classification, *_ = classify_dlog_candidate(
        source_camera_model="DJI Mini 5 Pro",
        db_camera_lut=(
            "LUT:908403d40286925c5b19129c4be6c0f4 "
            "(DJI Mini 5 Pro D-Log M to Rec.709 LUT)"
        ),
        xml_details=details,
    )
    assert classification == CLASS_CORRECT


def test_normalize_and_classify_lut_helpers():
    identity = normalize_lut_identity(
        "LUT:test (DJI Mini 5 Pro D-Log M to Rec.709 LUT)"
    )
    assert identity is not None
    assert "Mini 5 Pro" in identity
    model, _ = detect_camera_model("/Volumes/T7/drone/DJI Air 3/clip.SRT")
    assert model == "DJI Air 3"

    detail_correct = {
        "normalized_lut_identity": "DJI Air 3 D-Log M to Rec.709",
        "lut_camera_model": "DJI Air 3",
    }
    classification, *_ = classify_dlog_candidate(
        source_camera_model="DJI Air 3",
        db_camera_lut="LUT:test (DJI Air 3 D-Log M to Rec.709)",
        xml_details=[detail_correct],
    )
    assert classification == CLASS_CORRECT

    classification, *_ = classify_dlog_candidate(
        source_camera_model="DJI Air 3",
        db_camera_lut="LUT:test (DJI Mini 5 Pro D-Log M to Rec.709)",
        xml_details=[
            {
                "normalized_lut_identity": "DJI Mini 5 Pro D-Log M to Rec.709",
                "lut_camera_model": "DJI Mini 5 Pro",
            }
        ],
    )
    assert classification == CLASS_WRONG

    classification, *_ = classify_dlog_candidate(
        source_camera_model="DJI Air 3",
        db_camera_lut="LUT:test (DJI Air 3 D-Log M to Rec.709)",
        xml_details=[],
    )
    assert classification == CLASS_DB_XML_MISSING

    classification, *_ = classify_dlog_candidate(
        source_camera_model="DJI Air 3",
        db_camera_lut=None,
        xml_details=[detail_correct],
    )
    assert classification == CLASS_XML_DB_MISSING

    classification, *_ = classify_dlog_candidate(
        source_camera_model="DJI Air 3",
        db_camera_lut=None,
        xml_details=[],
    )
    assert classification == CLASS_NO_LUT

    classification, *_ = classify_dlog_candidate(
        source_camera_model="DJI Air 3",
        db_camera_lut=None,
        xml_details=[
            {"normalized_lut_identity": "Mystery Film Look", "lut_camera_model": None}
        ],
    )
    assert classification == CLASS_UNKNOWN_IDENTITY


def test_dlog_audit_end_to_end_classifications(tmp_path: Path):
    database = Database(tmp_path / "color.sqlite3")
    database.migrate()
    run_id = "STOCKIFY_DLOG_AUDIT"
    media = tmp_path / "media" / "DJI Air 3"
    cases = [
        (
            "VCLIP_OK",
            "DJI_20250315120000_0001_D.MP4",
            "DJI Air 3 D-Log M to Rec.709 LUT",
            "DJI Air 3 D-Log M to Rec.709 LUT",
            CLASS_CORRECT,
        ),
        (
            "VCLIP_WRONG",
            "DJI_20250315120100_0002_D.MP4",
            "DJI Mini 5 Pro D-Log M to Rec.709 LUT",
            "DJI Mini 5 Pro D-Log M to Rec.709 LUT",
            CLASS_WRONG,
        ),
        (
            "VCLIP_DBONLY",
            "DJI_20250315120200_0003_D.MP4",
            "DJI Air 3 D-Log M to Rec.709 LUT",
            None,
            CLASS_DB_XML_MISSING,
        ),
        (
            "VCLIP_UNKNOWN",
            "DJI_20250315120300_0004_D.MP4",
            "Mystery Cream LUT",
            "Mystery Cream LUT",
            CLASS_UNKNOWN_IDENTITY,
        ),
        (
            "VCLIP_NONE",
            "DJI_20250315120400_0005_D.MP4",
            None,
            None,
            CLASS_NO_LUT,
        ),
    ]
    input_root = tmp_path / "review-shards-located" / "market"
    for index, (clip_id, source, db_lut, xml_lut, _expected) in enumerate(cases):
        media_path = str(media / source)
        _seed_candidate(
            database,
            run_id=run_id,
            clip_id=clip_id,
            source_name=source,
            camera_lut=f"LUT:test ({db_lut})" if db_lut else None,
            media_path=media_path,
            source_index=index,
        )
        _write_color_srt(media / f"{Path(source).stem}.SRT", color_md="dlog_m")
        _write_lut_shard(
            input_root / f"{clip_id}.fcpxml",
            run_id=run_id,
            clip_id=clip_id,
            project_name=f"Project — {clip_id}",
            lut_name=xml_lut,
        )

    # Non-dlog source must not enter D-Log audit rows.
    other = ("VCLIP_DEFAULT", "DJI_20250315120500_0006_D.MP4")
    _seed_candidate(
        database,
        run_id=run_id,
        clip_id=other[0],
        source_name=other[1],
        camera_lut=None,
        media_path=str(media / other[1]),
        source_index=len(cases),
    )
    _write_color_srt(media / f"{Path(other[1]).stem}.SRT", color_md="default")
    _write_lut_shard(
        input_root / f"{other[0]}.fcpxml",
        run_id=run_id,
        clip_id=other[0],
        project_name=f"Project — {other[0]}",
        lut_name=None,
    )

    report = ReviewColorIntegrityService(CatalogRepository(database)).run(
        input_root=input_root.parent,
        report_path=tmp_path / "library-audits" / "final-color-integrity.json",
        text_report_path=tmp_path / "library-audits" / "final-color-integrity.txt",
        media_roots=[tmp_path / "media"],
        csv_report_path=tmp_path / "library-audits" / "dlog-camera-lut-audit.csv",
    )

    assert report.camera_lut_signatures
    assert any(
        "Mini 5 Pro" in str(item.get("normalized_lut_identity") or "")
        or "Air 3" in str(item.get("normalized_lut_identity") or "")
        for item in report.camera_lut_signatures
    )
    by_id = {item["stock_clip_id"]: item for item in report.dlog_records}
    assert other[0] not in by_id
    for clip_id, _source, _db_lut, _xml_lut, expected in cases:
        assert by_id[clip_id]["classification"] == expected
        assert by_id[clip_id]["color_md"] == "dlog_m"
    assert by_id["VCLIP_WRONG"]["camera_model"] == "DJI Air 3"
    assert by_id["VCLIP_WRONG"]["xml_lut_camera_model"] == "DJI Mini 5 Pro"
    assert (tmp_path / "library-audits" / "dlog-camera-lut-audit.csv").is_file()
    text = (tmp_path / "library-audits" / "final-color-integrity.txt").read_text()
    assert "D-LOG M CAMERA LUT INTEGRITY AUDIT" in text
    assert "DLOG_WRONG_CAMERA_LUT" in text
