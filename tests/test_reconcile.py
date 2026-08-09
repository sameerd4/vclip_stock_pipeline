from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from vclip_pipeline.reconcile import ReconcileService


def _write_reviewed_xml(source: Path, destination: Path) -> None:
    tree = ET.parse(source)
    root = tree.getroot()
    library = root.find("library")
    for event in library.findall("event"):
        for project in list(event.findall("project")):
            name = project.get("name", "")
            spine = project.find("./sequence/spine")
            if name == "South Lake Union Evening — Graded 1 — Clip 01":
                clip = list(spine)[0]
                clip.set("start", "4s")
                clip.set("duration", "5s")
                project.find("sequence").set("duration", "5s")
            if name == "South Lake Union Evening — Graded 1 — Stock Compilation":
                # Leave Clip 02 in the compilation while deleting its individual
                # project. Compilation leftovers must not create conflicts.
                clips = list(spine)
                clips[0].set("start", "9s")
                clips[0].set("duration", "3s")
            if name == "South Lake Union Evening — Graded 1 — Clip 02":
                event.remove(project)
    ET.indent(root)
    tree.write(destination, encoding="utf-8", xml_declaration=True)


def test_reconcile_records_manual_trim_and_deletion(pipeline_run):
    reviewed = pipeline_run["tmp_path"] / "reviewed.fcpxml"
    _write_reviewed_xml(pipeline_run["output"], reviewed)
    service = ReconcileService(pipeline_run["repository"])
    report = service.run(
        reviewed_xml=reviewed,
        run_id=None,
        authority="auto",
        scope="observed-projects",
        report_path=pipeline_run["tmp_path"] / "reconcile-report.json",
        allow_conflicts=False,
    )
    assert report.rejected == 1
    assert report.modified == 1
    assert report.conflicts == 0

    candidates = pipeline_run["repository"].candidates_for_run(
        pipeline_run["result"].stockify_run_id,
        accepted_only=True,
    )
    by_name = {candidate["generated_clip_project_name"]: candidate for candidate in candidates}
    changed = by_name["South Lake Union Evening — Graded 1 — Clip 01"]
    assert changed["review_status"] == "approved"
    assert changed["final_start"] == "4s"
    assert changed["final_duration"] == "5s"
    assert changed["manually_modified"] is True
    assert changed["manual_change"]["authority"] == "individual"

    deleted = by_name["South Lake Union Evening — Graded 1 — Clip 02"]
    assert deleted["review_status"] == "rejected"
    assert deleted["manual_change"]["reason"] == "deleted_individual_project"
    assert deleted["final_start"] is None


def test_individual_deletion_ignores_modified_compilation_leftover(pipeline_run):
    """Olympia-style: deleted individuals reject even when compilation still has them."""
    tree = ET.parse(pipeline_run["output"])
    root = tree.getroot()
    deleted_names = {
        "South Lake Union Evening — Graded 1 — Clip 01",
        "South Lake Union Evening — Graded 1 — Clip 02",
        "Capitol Hill Afternoon — Clip 01",
    }
    for event in root.findall("./library/event"):
        for project in list(event.findall("project")):
            name = project.get("name", "")
            if name in deleted_names:
                event.remove(project)
                continue
            if "Stock Compilation" not in name:
                continue
            for clip in project.findall("./sequence/spine/asset-clip"):
                clip.set("duration", "4s")
    reviewed = pipeline_run["tmp_path"] / "reviewed-individual-deletes.fcpxml"
    tree.write(reviewed, encoding="utf-8", xml_declaration=True)

    report = ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=reviewed,
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="full-run",
        report_path=None,
        allow_conflicts=False,
    )
    assert report.conflicts == 0
    assert report.rejected == 3
    candidates = pipeline_run["repository"].candidates_for_run(
        pipeline_run["result"].stockify_run_id,
        accepted_only=True,
    )
    by_name = {candidate["generated_clip_project_name"]: candidate for candidate in candidates}
    for name in deleted_names:
        assert by_name[name]["review_status"] == "rejected"
        assert by_name[name]["manual_change"]["reason"] == "deleted_individual_project"
    survivors = [c for c in candidates if c["generated_clip_project_name"] not in deleted_names]
    assert all(c["review_status"] == "approved" for c in survivors)
    assert all(not c["manually_modified"] for c in survivors)


def test_partial_individual_project_export_does_not_reject_siblings(pipeline_run):
    tree = ET.parse(pipeline_run["output"])
    root = tree.getroot()
    library = root.find("library")
    for event in list(library.findall("event")):
        keep = [
            project
            for project in event.findall("project")
            if project.get("name") == "South Lake Union Evening — Graded 1 — Clip 01"
        ]
        if keep:
            for project in list(event.findall("project")):
                if project not in keep:
                    event.remove(project)
        else:
            library.remove(event)
    partial = pipeline_run["tmp_path"] / "partial-project.fcpxml"
    tree.write(partial, encoding="utf-8", xml_declaration=True)

    service = ReconcileService(pipeline_run["repository"])
    report = service.run(
        reviewed_xml=partial,
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="observed-projects",
        report_path=None,
        allow_conflicts=False,
    )
    assert report.candidates_considered == 1
    assert report.out_of_scope == 6


def test_unknown_embedded_clip_id_is_reported_without_breaking_reconcile(pipeline_run):
    tree = ET.parse(pipeline_run["output"])
    root = tree.getroot()
    event = root.find("library/event")
    original = event.findall("project")[1]
    fake = ET.fromstring(ET.tostring(original, encoding="unicode"))
    fake.set("name", "Unknown VClip Candidate")
    clip = fake.find("./sequence/spine/asset-clip")
    for md in clip.findall("./metadata/md"):
        if md.get("key") == "com.vclip.stock_clip_id":
            md.set("value", "VCLIP_UNKNOWN_TEST")
            break
    event.append(fake)
    reviewed = pipeline_run["tmp_path"] / "reviewed-with-unknown.fcpxml"
    tree.write(reviewed, encoding="utf-8", xml_declaration=True)

    report = ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=reviewed,
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="full-run",
        report_path=None,
        allow_conflicts=False,
    )
    assert report.unknown_stock_clip_ids == ["VCLIP_UNKNOWN_TEST"]
    assert report.conflicts == 0


def test_full_run_reconcile_supports_compilation_only_layout(pipeline_run):
    options = pipeline_run["options"]
    compilation_only = type(options)(
        **{
            **options.__dict__,
            "output_path": pipeline_run["tmp_path"] / "compilation-only.fcpxml",
            "report_path": pipeline_run["tmp_path"] / "compilation-only-report.json",
            "manifest_path": pipeline_run["tmp_path"] / "compilation-only-manifest.json",
            "layout": "timeline-batch",
            "include_compilations": True,
        }
    )
    from vclip_pipeline.stockify import StockifyService

    result = StockifyService(
        pipeline_run["repository"], pipeline_run["resolver"]
    ).run(compilation_only)
    report = ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=compilation_only.output_path,
        run_id=result.stockify_run_id,
        authority="auto",
        scope="full-run",
        report_path=None,
        allow_conflicts=False,
    )
    assert report.approved == 7
    assert report.rejected == 0
    assert report.conflicts == 0
    assert report.modified == 0


def test_reconcile_persists_reviewed_compilation_timecodes(pipeline_run):
    tree = ET.parse(pipeline_run["output"])
    root = tree.getroot()
    for project in root.findall("./library/event/project"):
        if project.get("name") != "South Lake Union Evening — Graded 1 — Stock Compilation":
            continue
        clips = project.findall("./sequence/spine/asset-clip")
        clips[0].set("duration", "5s")
        clips[1].set("offset", "5s")
        project.find("sequence").set("duration", "13s")
        break
    reviewed = pipeline_run["tmp_path"] / "reviewed-compilation-timecode.fcpxml"
    tree.write(reviewed, encoding="utf-8", xml_declaration=True)

    # Compilation timeline offsets are recorded only under explicit compilation
    # authority; the normal individual-authoritative model ignores compilation edits.
    report = ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=reviewed,
        run_id=pipeline_run["result"].stockify_run_id,
        authority="compilation",
        scope="full-run",
        report_path=None,
        allow_conflicts=False,
    )
    assert report.modified == 1
    candidates = pipeline_run["repository"].candidates_for_run(
        pipeline_run["result"].stockify_run_id,
        accepted_only=True,
    )
    by_name = {candidate["generated_clip_project_name"]: candidate for candidate in candidates}
    second = by_name["South Lake Union Evening — Graded 1 — Clip 02"]
    assert second["final_compilation_timeline_offset"] == "5s"
    assert second["final_project_timecode"] == "00000500"


def test_auto_authority_ignores_compilation_only_edits(pipeline_run):
    tree = ET.parse(pipeline_run["output"])
    root = tree.getroot()
    for project in root.findall("./library/event/project"):
        if project.get("name") != "South Lake Union Evening — Graded 1 — Stock Compilation":
            continue
        clips = project.findall("./sequence/spine/asset-clip")
        clips[0].set("duration", "5s")
        clips[1].set("offset", "5s")
        break
    reviewed = pipeline_run["tmp_path"] / "reviewed-compilation-edits-ignored.fcpxml"
    tree.write(reviewed, encoding="utf-8", xml_declaration=True)

    report = ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=reviewed,
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="full-run",
        report_path=None,
        allow_conflicts=False,
    )
    assert report.conflicts == 0
    assert report.modified == 0
    assert report.rejected == 0
    candidates = pipeline_run["repository"].candidates_for_run(
        pipeline_run["result"].stockify_run_id,
        accepted_only=True,
    )
    slu = [
        c
        for c in candidates
        if c["generated_project_label"] == "South Lake Union Evening — Graded 1"
    ]
    assert all(c["review_status"] == "approved" for c in slu)
    assert all(not c["manually_modified"] for c in slu)
    assert all(c["final_compilation_timeline_offset"] is None for c in slu)


def test_individual_project_name_recovers_missing_custom_metadata(pipeline_run):
    tree = ET.parse(pipeline_run["output"])
    root = tree.getroot()
    target_name = "South Lake Union Evening — Graded 1 — Clip 01"
    for project in root.findall("./library/event/project"):
        if project.get("name") != target_name:
            continue
        clip = project.find("./sequence/spine/asset-clip")
        metadata = clip.find("metadata")
        clip.remove(metadata)
        break
    reviewed = pipeline_run["tmp_path"] / "reviewed-metadata-stripped.fcpxml"
    tree.write(reviewed, encoding="utf-8", xml_declaration=True)

    report = ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=reviewed,
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="full-run",
        report_path=None,
        allow_conflicts=False,
    )
    assert report.rejected == 0
    assert any("Recovered 1 candidate ID" in warning for warning in report.warnings)


def test_reconcile_ignores_custom_lut_round_trip_serialization_noise(pipeline_run):
    tree = ET.parse(pipeline_run["output"])
    root = tree.getroot()
    for filt in root.iter("filter-video"):
        if filt.get("name") == "Custom LUT":
            filt.set("ref", "r-roundtrip")
    # Stale pre-normalization catalog hashes must not force false modifications.
    with pipeline_run["repository"].database.transaction() as connection:
        connection.execute(
            "UPDATE stock_candidates SET effect_signature=? WHERE run_id=?",
            ("deadbeef" * 8, pipeline_run["result"].stockify_run_id),
        )
    reviewed = pipeline_run["tmp_path"] / "reviewed-lut-roundtrip.fcpxml"
    tree.write(reviewed, encoding="utf-8", xml_declaration=True)

    report = ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=reviewed,
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="full-run",
        report_path=None,
        allow_conflicts=False,
    )
    assert report.conflicts == 0
    assert report.modified == 0
    assert report.rejected == 0
    assert report.approved >= 1
