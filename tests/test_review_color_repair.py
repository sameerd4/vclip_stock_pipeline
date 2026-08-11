from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from vclip_pipeline.db import CatalogRepository, Database
from vclip_pipeline.stockify.core import local_name
from vclip_pipeline.stockify.fcpxml import (
    asset_conversion_lut,
    build_resource_index,
    first_direct_child,
    parse_source,
    read_vclip_metadata,
)
from vclip_pipeline.util import json_dumps
from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.review_color_integrity import ReviewColorIntegrityService
from vclip_pipeline.workflow.review_color_repair import (
    REPAIR_REASON,
    TARGET_LUT_IDENTITY,
    TARGET_LUT_OVERRIDE,
    WRONG_LUT_IDENTITY,
    ReviewColorRepairService,
    is_camera_conversion_lut_filter,
)
from vclip_pipeline.workflow.review_dlog_lut_audit import CLASS_CORRECT, CLASS_WRONG

from test_review_color_integrity import _seed_candidate, _write_color_srt


NS = "http://www.apple.com/finalcutpro/fcpxml"

AIR3_OVERRIDE = (
    "LUT:944ff715997edde7b09b7b767fd51df2 (DJI Air 3 D-Log M to Rec.709 V1_)"
)
MINI5_OVERRIDE = TARGET_LUT_OVERRIDE
AIR3_BLOB = (
    "PD94bWwgdmVyc2lvbj0iMS4wIiBlbmNvZGluZz0iVVRGLTgiPz4KPCFET0NUWVBFIG96eG1sc2Nl"
    "bmU+PG96eG1sPkFpcjNXb25nTHV0QmxvYjwvb3p4bWw+"
)
MINI5_BLOB = (
    "PD94bWwgdmVyc2lvbj0iMS4wIiBlbmNvZGluZz0iVVRGLTgiPz4KPCFET0NUWVBFIG96eG1sc2Nl"
    "bmU+PG96eG1sPk1pbmk1R29vZEx1dEJsb2I8L296eG1sPg=="
)
EFFECT_UID = "FxPlug:14B39AEF-607D-42DF-98DD-DB3DD345E925"


def _write_conversion_shard(
    path: Path,
    *,
    run_id: str,
    clip_id: str,
    project_name: str,
    override: str,
    lut_blob: str,
    include_creative: bool = False,
    include_color_correction: bool = False,
    effect_id: str = "rfx",
) -> None:
    creative = ""
    if include_creative:
        creative = """              <fcp:filter-video ref="rfxCream" name="Creamy Dream">
                <fcp:param name="LUT Name" key="1" value="Creamy Dream Look"/>
                <fcp:param name="Amount" key="2" value="0.35"/>
              </fcp:filter-video>
"""
    color_corr = ""
    if include_color_correction:
        color_corr = """              <fcp:filter-video ref="rfxCC" name="Color Correction">
                <fcp:param name="Saturation" key="1" value="1.1"/>
              </fcp:filter-video>
"""
    extra_effects = ""
    if include_creative:
        extra_effects += (
            '    <fcp:effect id="rfxCream" name="Creamy Dream" uid="FxPlug:CREAM"/>\n'
        )
    if include_color_correction:
        extra_effects += (
            '    <fcp:effect id="rfxCC" name="Color Correction" uid="FxPlug:CC"/>\n'
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""<?xml version='1.0' encoding='utf-8'?>
<fcp:fcpxml xmlns:fcp="{NS}" version="1.12">
  <fcp:resources>
    <fcp:format id="r1" name="FFVideoFormat3840x2160p30" frameDuration="100/3000s" width="3840" height="2160"/>
    <fcp:asset id="r2" name="media" uid="ASSET-{clip_id}" start="0s" duration="10s" hasVideo="1" format="r1" customLUTOverride="{override}">
      <fcp:media-rep kind="original-media" src="file:///tmp/{clip_id}.mov"/>
    </fcp:asset>
    <fcp:effect id="{effect_id}" name="Custom LUT" uid="{EFFECT_UID}"/>
{extra_effects}  </fcp:resources>
  <fcp:library>
    <fcp:event name="Test Event">
      <fcp:project name="{project_name}" uid="proj-{clip_id}">
        <fcp:sequence format="r1" duration="10s" tcStart="0s" tcFormat="NDF">
          <fcp:spine>
            <fcp:asset-clip ref="r2" name="clip" offset="0s" duration="10s" start="0s" tcFormat="NDF">
              <fcp:filter-video ref="{effect_id}" name="Custom LUT">
                <fcp:param name="LUT" key="3" value="{lut_blob}"/>
                <fcp:param name="Input" key="100/101" value="0 (Rec. 709)"/>
                <fcp:param name="Output" key="100/102" value="0 (Rec. 709)"/>
              </fcp:filter-video>
{creative}{color_corr}              <fcp:adjust-transform position="0 0" scale="1 1"/>
              <fcp:metadata>
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


def _prepare_corpus(tmp_path: Path):
    database = Database(tmp_path / "color-repair.sqlite3")
    database.migrate()
    WorkflowCatalog(database)
    run_id = "STOCKIFY_COLOR_REPAIR"
    media = tmp_path / "media" / "DJI Mini 5 Pro"
    input_root = tmp_path / "review-shards-located" / "market"
    cases = [
        (
            "VCLIP_DONOR",
            "DJI_20260801120000_0001_D.MP4",
            MINI5_OVERRIDE,
            MINI5_BLOB,
            False,
            False,
        ),
        (
            "VCLIP_WRONG",
            "DJI_20251112163939_0050_D.MP4",
            AIR3_OVERRIDE,
            AIR3_BLOB,
            True,
            True,
        ),
        (
            "VCLIP_DEFAULT",
            "DJI_20251112164000_0051_D.MP4",
            AIR3_OVERRIDE,
            AIR3_BLOB,
            False,
            False,
        ),
        (
            "VCLIP_AIR3_SRC",
            "DJI_20250701120000_0001_D.MP4",
            AIR3_OVERRIDE,
            AIR3_BLOB,
            False,
            False,
        ),
    ]
    for index, (clip_id, source, override, blob, creative, cc) in enumerate(cases):
        camera_folder = (
            tmp_path / "media" / "DJI Air 3"
            if clip_id == "VCLIP_AIR3_SRC"
            else media
        )
        camera_folder.mkdir(parents=True, exist_ok=True)
        _seed_candidate(
            database,
            run_id=run_id,
            clip_id=clip_id,
            source_name=source,
            camera_lut=override,
            media_path=str(camera_folder / source),
            source_index=index,
            capture_date="2025-11-12" if "20251112" in source else "2026-08-01",
        )
        color_md = "default" if clip_id == "VCLIP_DEFAULT" else "dlog_m"
        _write_color_srt(camera_folder / f"{Path(source).stem}.SRT", color_md=color_md)
        _write_conversion_shard(
            input_root / f"{clip_id}.fcpxml",
            run_id=run_id,
            clip_id=clip_id,
            project_name=f"Project — {clip_id}",
            override=override,
            lut_blob=blob,
            include_creative=creative,
            include_color_correction=cc,
        )
    # Air 3 source lives under Air 3 media root for model detection.
    return database, run_id, input_root.parent, [tmp_path / "media"]


def test_exact_mini5_air3_replacement_preserves_unrelated_effects(tmp_path: Path):
    database, run_id, input_root, media_roots = _prepare_corpus(tmp_path)
    output_root = tmp_path / "review-shards-color-fixed"
    report = ReviewColorRepairService(CatalogRepository(database)).run(
        input_root=input_root,
        output_root=output_root,
        media_roots=media_roots,
        report_path=tmp_path / "library-audits" / "color-repair.json",
        text_report_path=tmp_path / "library-audits" / "color-repair.txt",
        dry_run=False,
        overwrite=True,
    )
    assert report.eligible_repairs == 1
    assert report.repaired == 1
    assert report.repairs[0]["stock_clip_id"] == "VCLIP_WRONG"
    assert report.repairs[0]["repair_reason"] == REPAIR_REASON

    out_xml = output_root / "market" / "VCLIP_WRONG.fcpxml"
    root = parse_source(out_xml).getroot()
    resource_index = build_resource_index(first_direct_child(root, "resources"))
    clip = next(node for node in root.iter() if str(node.tag).endswith("asset-clip"))
    asset = resource_index[clip.get("ref")]
    assert asset_conversion_lut(asset) == MINI5_OVERRIDE

    filter_names = []
    conversion_params = None
    for child in list(clip):
        tag = local_name(child.tag)
        if tag == "filter-video":
            filter_names.append(child.get("name"))
            if is_camera_conversion_lut_filter(child, resource_index):
                conversion_params = {
                    (p.get("name"), p.get("key")): p.get("value")
                    for p in list(child)
                    if local_name(p.tag) == "param"
                }
        if tag == "adjust-transform":
            assert child.get("scale") == "1 1"
    assert "Creamy Dream" in filter_names
    assert "Color Correction" in filter_names
    assert conversion_params[("LUT", "3")] == MINI5_BLOB
    assert AIR3_BLOB not in conversion_params[("LUT", "3")]

    with database.connect() as connection:
        row = connection.execute(
            "SELECT camera_lut FROM stock_candidates WHERE stock_clip_id=?",
            ("VCLIP_WRONG",),
        ).fetchone()
        media = connection.execute(
            """
            SELECT camera_lut FROM source_media
            WHERE id=(SELECT source_media_id FROM stock_candidates WHERE stock_clip_id=?)
            """,
            ("VCLIP_WRONG",),
        ).fetchone()
        provenance = connection.execute(
            """
            SELECT previous_camera_lut, new_camera_lut, repair_reason
            FROM review_color_repairs
            WHERE stockify_run_id=? AND stock_clip_id=?
            """,
            (run_id, "VCLIP_WRONG"),
        ).fetchone()
    assert row["camera_lut"] == MINI5_OVERRIDE
    assert media["camera_lut"] == MINI5_OVERRIDE
    assert provenance["previous_camera_lut"] == AIR3_OVERRIDE
    assert provenance["new_camera_lut"] == MINI5_OVERRIDE
    assert provenance["repair_reason"] == REPAIR_REASON
    assert report.post_write_audit["still_wrong_camera_lut"] == 0
    assert report.post_write_audit["db_xml_mismatches"] == 0


def test_rejects_ambiguous_and_non_dlog_inputs(tmp_path: Path):
    database, _run_id, input_root, media_roots = _prepare_corpus(tmp_path)
    report = ReviewColorRepairService(CatalogRepository(database)).run(
        input_root=input_root,
        output_root=tmp_path / "out",
        media_roots=media_roots,
        report_path=tmp_path / "r.json",
        text_report_path=tmp_path / "r.txt",
        dry_run=True,
    )
    repaired_ids = {item["stock_clip_id"] for item in report.repairs}
    assert repaired_ids == {"VCLIP_WRONG"}
    reasons = {item["stock_clip_id"]: item["reason"] for item in report.rejections}
    assert reasons["VCLIP_DEFAULT"] == "color_md_not_dlog_m"
    assert reasons["VCLIP_AIR3_SRC"] in {
        "camera_model_not_mini5_pro",
        "not_dlog_wrong_camera_lut",
    }
    assert "VCLIP_DONOR" not in repaired_ids
    assert report.skipped_already_correct >= 1


def test_dry_run_writes_reports_but_not_output_tree(tmp_path: Path):
    database, _run_id, input_root, media_roots = _prepare_corpus(tmp_path)
    output_root = tmp_path / "review-shards-color-fixed"
    report_path = tmp_path / "library-audits" / "color-repair.json"
    text_path = tmp_path / "library-audits" / "color-repair.txt"
    report = ReviewColorRepairService(CatalogRepository(database)).run(
        input_root=input_root,
        output_root=output_root,
        media_roots=media_roots,
        report_path=report_path,
        text_report_path=text_path,
        dry_run=True,
    )
    assert report.dry_run is True
    assert report.eligible_repairs == 1
    assert report_path.is_file()
    assert text_path.is_file()
    assert not output_root.exists() or not any(output_root.rglob("*.fcpxml"))
    with database.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM review_color_repairs"
        ).fetchone()["n"]
        lut = connection.execute(
            "SELECT camera_lut FROM stock_candidates WHERE stock_clip_id='VCLIP_WRONG'"
        ).fetchone()["camera_lut"]
    assert count == 0
    assert lut == AIR3_OVERRIDE


def test_idempotent_second_pass_on_fixed_tree(tmp_path: Path):
    database, _run_id, input_root, media_roots = _prepare_corpus(tmp_path)
    output_root = tmp_path / "review-shards-color-fixed"
    service = ReviewColorRepairService(CatalogRepository(database))
    first = service.run(
        input_root=input_root,
        output_root=output_root,
        media_roots=media_roots,
        report_path=tmp_path / "a.json",
        text_report_path=tmp_path / "a.txt",
        overwrite=True,
    )
    assert first.repaired == 1
    second_out = tmp_path / "review-shards-color-fixed-2"
    second = service.run(
        input_root=output_root,
        output_root=second_out,
        media_roots=media_roots,
        report_path=tmp_path / "b.json",
        text_report_path=tmp_path / "b.txt",
        overwrite=True,
    )
    assert second.eligible_repairs == 0
    assert second.repaired == 0
    assert second.skipped_already_correct >= 1


def test_post_audit_counts_match_color_integrity(tmp_path: Path):
    database, _run_id, input_root, media_roots = _prepare_corpus(tmp_path)
    output_root = tmp_path / "review-shards-color-fixed"
    ReviewColorRepairService(CatalogRepository(database)).run(
        input_root=input_root,
        output_root=output_root,
        media_roots=media_roots,
        report_path=tmp_path / "repair.json",
        text_report_path=tmp_path / "repair.txt",
        overwrite=True,
    )
    integrity = ReviewColorIntegrityService(CatalogRepository(database)).run(
        input_root=output_root,
        report_path=tmp_path / "integrity.json",
        text_report_path=tmp_path / "integrity.txt",
        media_roots=media_roots,
        csv_report_path=tmp_path / "dlog.csv",
    )
    by_id = {item["stock_clip_id"]: item for item in integrity.dlog_records}
    assert by_id["VCLIP_WRONG"]["classification"] == CLASS_CORRECT
    assert by_id["VCLIP_WRONG"]["xml_normalized_lut_identity"] == TARGET_LUT_IDENTITY
    assert by_id["VCLIP_DONOR"]["classification"] == CLASS_CORRECT
    assert CLASS_WRONG not in {
        item["classification"] for item in integrity.dlog_records
    }


def test_db_xml_consistency_after_repair(tmp_path: Path):
    database, run_id, input_root, media_roots = _prepare_corpus(tmp_path)
    output_root = tmp_path / "out"
    ReviewColorRepairService(CatalogRepository(database)).run(
        input_root=input_root,
        output_root=output_root,
        media_roots=media_roots,
        report_path=tmp_path / "r.json",
        text_report_path=tmp_path / "r.txt",
        overwrite=True,
    )
    root = parse_source(output_root / "market" / "VCLIP_WRONG.fcpxml").getroot()
    resource_index = build_resource_index(first_direct_child(root, "resources"))
    clip = next(node for node in root.iter() if str(node.tag).endswith("asset-clip"))
    assert read_vclip_metadata(clip)["com.vclip.stock_clip_id"] == "VCLIP_WRONG"
    override = asset_conversion_lut(resource_index[clip.get("ref")])
    with database.connect() as connection:
        db_lut = connection.execute(
            """
            SELECT camera_lut FROM stock_candidates
            WHERE run_id=? AND stock_clip_id=?
            """,
            (run_id, "VCLIP_WRONG"),
        ).fetchone()["camera_lut"]
    assert db_lut == override == MINI5_OVERRIDE
    assert WRONG_LUT_IDENTITY not in db_lut
    manifest = json.loads(
        (output_root / "market" / "VCLIP_WRONG-shard-manifest.json").read_text()
    )
    assert manifest["color_repair"]["repaired_stock_clip_ids"] == ["VCLIP_WRONG"]
