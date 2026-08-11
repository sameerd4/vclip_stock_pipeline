from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from vclip_pipeline.reconcile import ReconcileService
from vclip_pipeline.stockify.fcpxml import (
    first_direct_child,
    local_name,
    read_vclip_metadata,
    validate_fcpxml,
)
from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.review_dedupe import ReviewDedupeService
from vclip_pipeline.workflow.review_shard import ReviewShardService


def _markets_path() -> Path:
    return (
        Path(__file__).parents[1]
        / "src"
        / "vclip_pipeline"
        / "data"
        / "workflow_markets.json"
    )


def _write_shard(pipeline_run, output: Path) -> Path:
    ReviewShardService(pipeline_run["repository"]).run(
        review_xml=pipeline_run["output"],
        output_directory=output,
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
    return next(output.glob("*.fcpxml"))


def _individual_projects(root: ET.Element) -> list[ET.Element]:
    return [
        project
        for project in root.findall("./library/event/project")
        if "Stock Compilation" not in project.get("name", "")
    ]


def _set_clip_metadata(project: ET.Element, updates: dict[str, str]) -> None:
    sequence = first_direct_child(project, "sequence")
    spine = first_direct_child(sequence, "spine") if sequence is not None else None
    assert spine is not None
    for node in spine.iter():
        if node is spine or local_name(node.tag) not in {"asset-clip", "video"}:
            continue
        metadata = first_direct_child(node, "metadata")
        assert metadata is not None
        by_key = {
            md.get("key"): md
            for md in metadata.findall("md")
            if md.get("key")
        }
        for key, value in updates.items():
            md = by_key.get(key)
            if md is None:
                md = ET.SubElement(metadata, "md", {"key": key})
                by_key[key] = md
            md.set("value", value)
        return
    raise AssertionError("No clip found in project")


def _project_clip_id(project: ET.Element) -> str:
    sequence = first_direct_child(project, "sequence")
    spine = first_direct_child(sequence, "spine") if sequence is not None else None
    assert spine is not None
    for node in spine.iter():
        if node is spine or local_name(node.tag) not in {"asset-clip", "video"}:
            continue
        metadata = read_vclip_metadata(node)
        clip_id = metadata.get("com.vclip.stock_clip_id")
        if clip_id:
            return clip_id
    raise AssertionError("Missing stock_clip_id")


def _clone_db_candidate(
    database,
    *,
    source_clip_id: str,
    new_clip_id: str,
    new_project_name: str,
    short_clip_recovery: str,
    segment_index: int,
) -> dict:
    with database.connect() as connection:
        source = connection.execute(
            "SELECT * FROM stock_candidates WHERE stock_clip_id=?",
            (source_clip_id,),
        ).fetchone()
        assert source is not None
        row = dict(source)
    row.update(
        {
            "stock_clip_id": new_clip_id,
            "source_segment_index": segment_index,
            "generated_clip_project_name": new_project_name,
            "expected_export_basename": new_project_name,
            "short_clip_recovery": short_clip_recovery,
            "clip_sequence": (row.get("clip_sequence") or 0) + 100 + segment_index,
        }
    )
    columns = list(row.keys())
    placeholders = ", ".join("?" for _ in columns)
    with database.transaction() as connection:
        connection.execute(
            f"INSERT INTO stock_candidates ({', '.join(columns)}) VALUES ({placeholders})",
            [row[column] for column in columns],
        )
        occ = connection.execute(
            """
            SELECT * FROM generated_occurrences
            WHERE run_id=? AND stock_clip_id=? AND representation='individual'
            """,
            (row["run_id"], source_clip_id),
        ).fetchone()
        if occ is not None:
            occ_row = dict(occ)
            occ_row.pop("id", None)
            occ_row.update(
                {
                    "stock_clip_id": new_clip_id,
                    "generated_project_name": new_project_name,
                }
            )
            occ_columns = list(occ_row.keys())
            connection.execute(
                f"INSERT INTO generated_occurrences ({', '.join(occ_columns)}) "
                f"VALUES ({', '.join('?' for _ in occ_columns)})",
                [occ_row[column] for column in occ_columns],
            )
    return row


def _inject_duplicate_project(
    root: ET.Element,
    *,
    source_project: ET.Element,
    new_name: str,
    new_clip_id: str,
    insert_after: bool = True,
) -> ET.Element:
    clone = copy.deepcopy(source_project)
    clone.set("name", new_name)
    clone.set("uid", f"{source_project.get('uid')}-dup-{new_clip_id[-8:]}")
    _set_clip_metadata(
        clone,
        {
            "com.vclip.stock_clip_id": new_clip_id,
            "com.vclip.generated_clip_project_name": new_name,
        },
    )
    for event in root.findall("./library/event"):
        projects = list(event.findall("project"))
        if source_project in projects:
            index = list(event).index(source_project)
            if insert_after:
                event.insert(index + 1, clone)
            else:
                event.insert(index, clone)
            return clone
    raise AssertionError("Source project not found in any event")


def _shift_candidate_range(database, clip_id: str, *, start: str, duration: str) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE stock_candidates
            SET proposed_start=?, proposed_duration=?,
                proposed_duration_seconds=?,
                original_start=?, original_duration=?
            WHERE stock_clip_id=?
            """,
            (start, duration, float(duration.rstrip("s")), start, duration, clip_id),
        )


def test_two_way_exact_cluster_removes_one(pipeline_run):
    shard_dir = pipeline_run["tmp_path"] / "dedupe-2way"
    shard_path = _write_shard(pipeline_run, shard_dir)
    tree = ET.parse(shard_path)
    root = tree.getroot()
    source = _individual_projects(root)[0]
    source_id = _project_clip_id(source)
    dup_name = f"{source.get('name')} — Dup"
    dup_id = f"{source_id}_DUP"
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=source_id,
        new_clip_id=dup_id,
        new_project_name=dup_name,
        short_clip_recovery="not_applicable",
        segment_index=9001,
    )
    _inject_duplicate_project(
        root, source_project=source, new_name=dup_name, new_clip_id=dup_id
    )
    # Refresh manifest stock_clip_ids so both are in scope.
    manifest_path = shard_path.with_name(f"{shard_path.stem}-shard-manifest.json")
    manifest = json.loads(manifest_path.read_text())
    manifest["stock_clip_ids"] = list(
        dict.fromkeys([*manifest.get("stock_clip_ids", []), dup_id])
    )
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    input_xml = shard_dir / "with-dup.fcpxml"
    tree.write(input_xml, encoding="utf-8", xml_declaration=True)
    input_manifest = input_xml.with_name(f"{input_xml.stem}-shard-manifest.json")
    input_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    output = shard_dir / "deduped.fcpxml"
    catalog = WorkflowCatalog(pipeline_run["database"])
    report = ReviewDedupeService(pipeline_run["repository"], catalog).run(
        input_xml=input_xml,
        output_xml=output,
        report_path=shard_dir / "duplicate-removal.json",
        text_report_path=shard_dir / "duplicate-removal.txt",
        dry_run=False,
        overwrite=True,
    )
    assert report.projects_removed == 1
    assert report.clusters_found == 1
    assert report.removals[0].reason == "exact_source_range_duplicate"
    out_root = ET.parse(output).getroot()
    assert validate_fcpxml(out_root).passed
    names = {project.get("name") for project in _individual_projects(out_root)}
    assert source.get("name") in names
    assert dup_name not in names
    # DB rows preserved.
    with pipeline_run["database"].connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) AS n FROM stock_candidates WHERE stock_clip_id=?",
                (dup_id,),
            ).fetchone()["n"]
            == 1
        )


def test_three_way_cluster_keeps_earliest_non_expanded(pipeline_run):
    shard_dir = pipeline_run["tmp_path"] / "dedupe-3way"
    shard_path = _write_shard(pipeline_run, shard_dir)
    tree = ET.parse(shard_path)
    root = tree.getroot()
    source = _individual_projects(root)[0]
    source_id = _project_clip_id(source)
    source_name = source.get("name")

    mid_name = f"{source_name} — Mid"
    mid_id = f"{source_id}_MID"
    late_name = f"{source_name} — Late"
    late_id = f"{source_id}_LATE"

    # Make the original an expanded_review so the earliest non-expanded wins.
    with pipeline_run["database"].transaction() as connection:
        connection.execute(
            """
            UPDATE stock_candidates
            SET short_clip_recovery='expanded_review'
            WHERE stock_clip_id=?
            """,
            (source_id,),
        )
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=source_id,
        new_clip_id=mid_id,
        new_project_name=mid_name,
        short_clip_recovery="not_applicable",
        segment_index=9002,
    )
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=source_id,
        new_clip_id=late_id,
        new_project_name=late_name,
        short_clip_recovery="expanded_review",
        segment_index=9003,
    )
    _inject_duplicate_project(
        root, source_project=source, new_name=mid_name, new_clip_id=mid_id
    )
    _inject_duplicate_project(
        root, source_project=source, new_name=late_name, new_clip_id=late_id
    )

    manifest_path = shard_path.with_name(f"{shard_path.stem}-shard-manifest.json")
    manifest = json.loads(manifest_path.read_text())
    manifest["stock_clip_ids"] = list(
        dict.fromkeys([*manifest.get("stock_clip_ids", []), mid_id, late_id])
    )
    input_xml = shard_dir / "three-way.fcpxml"
    tree.write(input_xml, encoding="utf-8", xml_declaration=True)
    input_xml.with_name(f"{input_xml.stem}-shard-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    output = shard_dir / "three-way-out.fcpxml"
    report = ReviewDedupeService(
        pipeline_run["repository"], WorkflowCatalog(pipeline_run["database"])
    ).run(
        input_xml=input_xml,
        output_xml=output,
        report_path=shard_dir / "three.json",
        text_report_path=shard_dir / "three.txt",
        overwrite=True,
    )
    assert report.clusters_found == 1
    assert report.projects_removed == 2
    kept_names = {item.kept_project_name for item in report.removals}
    assert kept_names == {mid_name}
    out_names = {
        project.get("name") for project in _individual_projects(ET.parse(output).getroot())
    }
    assert mid_name in out_names
    assert source_name not in out_names
    assert late_name not in out_names


def test_expanded_review_loses_to_original_when_original_is_later(pipeline_run):
    shard_dir = pipeline_run["tmp_path"] / "dedupe-pref"
    shard_path = _write_shard(pipeline_run, shard_dir)
    tree = ET.parse(shard_path)
    root = tree.getroot()
    source = _individual_projects(root)[0]
    source_id = _project_clip_id(source)
    with pipeline_run["database"].transaction() as connection:
        connection.execute(
            """
            UPDATE stock_candidates
            SET short_clip_recovery='expanded_review'
            WHERE stock_clip_id=?
            """,
            (source_id,),
        )
    later_name = f"{source.get('name')} — OriginalLater"
    later_id = f"{source_id}_ORIG"
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=source_id,
        new_clip_id=later_id,
        new_project_name=later_name,
        short_clip_recovery="not_applicable",
        segment_index=9010,
    )
    _inject_duplicate_project(
        root, source_project=source, new_name=later_name, new_clip_id=later_id
    )
    manifest = json.loads(
        shard_path.with_name(f"{shard_path.stem}-shard-manifest.json").read_text()
    )
    manifest["stock_clip_ids"] = list(
        dict.fromkeys([*manifest.get("stock_clip_ids", []), later_id])
    )
    input_xml = shard_dir / "pref.fcpxml"
    tree.write(input_xml, encoding="utf-8", xml_declaration=True)
    input_xml.with_name(f"{input_xml.stem}-shard-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    report = ReviewDedupeService(
        pipeline_run["repository"], WorkflowCatalog(pipeline_run["database"])
    ).run(
        input_xml=input_xml,
        output_xml=shard_dir / "pref-out.fcpxml",
        report_path=shard_dir / "pref.json",
        text_report_path=shard_dir / "pref.txt",
        overwrite=True,
    )
    assert report.projects_removed == 1
    assert report.removals[0].kept_project_name == later_name
    assert report.removals[0].removed_project_name == source.get("name")


def test_merely_overlapping_windows_are_not_removed(pipeline_run):
    shard_dir = pipeline_run["tmp_path"] / "dedupe-overlap"
    shard_path = _write_shard(pipeline_run, shard_dir)
    tree = ET.parse(shard_path)
    root = tree.getroot()
    source = _individual_projects(root)[0]
    source_id = _project_clip_id(source)
    other_name = f"{source.get('name')} — Overlap"
    other_id = f"{source_id}_OVER"
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=source_id,
        new_clip_id=other_id,
        new_project_name=other_name,
        short_clip_recovery="not_applicable",
        segment_index=9020,
    )
    # Same media, overlapping but not within 0.05s equality.
    _shift_candidate_range(
        pipeline_run["database"],
        other_id,
        start="1s",
        duration="8s",
    )
    _inject_duplicate_project(
        root, source_project=source, new_name=other_name, new_clip_id=other_id
    )
    manifest = json.loads(
        shard_path.with_name(f"{shard_path.stem}-shard-manifest.json").read_text()
    )
    manifest["stock_clip_ids"] = list(
        dict.fromkeys([*manifest.get("stock_clip_ids", []), other_id])
    )
    input_xml = shard_dir / "overlap.fcpxml"
    tree.write(input_xml, encoding="utf-8", xml_declaration=True)
    input_xml.with_name(f"{input_xml.stem}-shard-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    report = ReviewDedupeService(
        pipeline_run["repository"], WorkflowCatalog(pipeline_run["database"])
    ).run(
        input_xml=input_xml,
        output_xml=shard_dir / "overlap-out.fcpxml",
        report_path=shard_dir / "overlap.json",
        text_report_path=shard_dir / "overlap.txt",
        overwrite=True,
    )
    assert report.projects_removed == 0
    assert report.clusters_found == 0


def test_shard_manifest_scopes_candidates(pipeline_run):
    shard_dir = pipeline_run["tmp_path"] / "dedupe-scope"
    shard_path = _write_shard(pipeline_run, shard_dir)
    tree = ET.parse(shard_path)
    root = tree.getroot()
    source = _individual_projects(root)[0]
    source_id = _project_clip_id(source)
    dup_name = f"{source.get('name')} — ScopedOut"
    dup_id = f"{source_id}_SCOPED"
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=source_id,
        new_clip_id=dup_id,
        new_project_name=dup_name,
        short_clip_recovery="not_applicable",
        segment_index=9030,
    )
    _inject_duplicate_project(
        root, source_project=source, new_name=dup_name, new_clip_id=dup_id
    )
    # Manifest intentionally omits the duplicate id.
    manifest = json.loads(
        shard_path.with_name(f"{shard_path.stem}-shard-manifest.json").read_text()
    )
    assert dup_id not in manifest["stock_clip_ids"]
    input_xml = shard_dir / "scoped.fcpxml"
    tree.write(input_xml, encoding="utf-8", xml_declaration=True)
    input_xml.with_name(f"{input_xml.stem}-shard-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    report = ReviewDedupeService(
        pipeline_run["repository"], WorkflowCatalog(pipeline_run["database"])
    ).run(
        input_xml=input_xml,
        output_xml=shard_dir / "scoped-out.fcpxml",
        report_path=shard_dir / "scoped.json",
        text_report_path=shard_dir / "scoped.txt",
        overwrite=True,
    )
    assert report.projects_removed == 0
    out_names = {
        project.get("name")
        for project in _individual_projects(ET.parse(shard_dir / "scoped-out.fcpxml").getroot())
    }
    assert dup_name in out_names


def test_dedupe_removal_reconciles_as_out_of_scope_not_rejected(pipeline_run):
    shard_dir = pipeline_run["tmp_path"] / "dedupe-reconcile"
    shard_path = _write_shard(pipeline_run, shard_dir)
    tree = ET.parse(shard_path)
    root = tree.getroot()
    source = _individual_projects(root)[0]
    source_id = _project_clip_id(source)
    dup_name = f"{source.get('name')} — ReconcileDup"
    dup_id = f"{source_id}_RECON"
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=source_id,
        new_clip_id=dup_id,
        new_project_name=dup_name,
        short_clip_recovery="not_applicable",
        segment_index=9040,
    )
    _inject_duplicate_project(
        root, source_project=source, new_name=dup_name, new_clip_id=dup_id
    )
    manifest = json.loads(
        shard_path.with_name(f"{shard_path.stem}-shard-manifest.json").read_text()
    )
    manifest["stock_clip_ids"] = list(
        dict.fromkeys([*manifest.get("stock_clip_ids", []), dup_id])
    )
    input_xml = shard_dir / "recon-in.fcpxml"
    tree.write(input_xml, encoding="utf-8", xml_declaration=True)
    input_xml.with_name(f"{input_xml.stem}-shard-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    catalog = WorkflowCatalog(pipeline_run["database"])
    deduped = shard_dir / "recon-out.fcpxml"
    ReviewDedupeService(pipeline_run["repository"], catalog).run(
        input_xml=input_xml,
        output_xml=deduped,
        report_path=shard_dir / "recon.json",
        text_report_path=shard_dir / "recon.txt",
        overwrite=True,
    )
    assert dup_id in catalog.review_dedupe_removed_ids(
        pipeline_run["result"].stockify_run_id
    )

    report = ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=deduped,
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="observed-projects",
        report_path=None,
        allow_conflicts=False,
    )
    rejected = {
        row["stock_clip_id"]
        for row in report.decisions
        if row["review_status"] == "rejected"
    }
    assert dup_id not in rejected
    with pipeline_run["database"].connect() as connection:
        status = connection.execute(
            "SELECT review_status FROM stock_candidates WHERE stock_clip_id=?",
            (dup_id,),
        ).fetchone()["review_status"]
    assert status == "pending"


def test_dry_run_does_not_write_output_or_db_rows(pipeline_run):
    shard_dir = pipeline_run["tmp_path"] / "dedupe-dry"
    shard_path = _write_shard(pipeline_run, shard_dir)
    tree = ET.parse(shard_path)
    root = tree.getroot()
    source = _individual_projects(root)[0]
    source_id = _project_clip_id(source)
    dup_name = f"{source.get('name')} — Dry"
    dup_id = f"{source_id}_DRY"
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=source_id,
        new_clip_id=dup_id,
        new_project_name=dup_name,
        short_clip_recovery="not_applicable",
        segment_index=9050,
    )
    _inject_duplicate_project(
        root, source_project=source, new_name=dup_name, new_clip_id=dup_id
    )
    manifest = json.loads(
        shard_path.with_name(f"{shard_path.stem}-shard-manifest.json").read_text()
    )
    manifest["stock_clip_ids"] = list(
        dict.fromkeys([*manifest.get("stock_clip_ids", []), dup_id])
    )
    input_xml = shard_dir / "dry-in.fcpxml"
    tree.write(input_xml, encoding="utf-8", xml_declaration=True)
    input_xml.with_name(f"{input_xml.stem}-shard-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    output = shard_dir / "dry-out.fcpxml"
    catalog = WorkflowCatalog(pipeline_run["database"])
    report = ReviewDedupeService(pipeline_run["repository"], catalog).run(
        input_xml=input_xml,
        output_xml=output,
        report_path=shard_dir / "dry.json",
        text_report_path=shard_dir / "dry.txt",
        dry_run=True,
        overwrite=True,
    )
    assert report.dry_run is True
    assert report.projects_removed == 1
    assert not output.exists()
    assert catalog.review_dedupe_removed_ids(pipeline_run["result"].stockify_run_id) == set()
