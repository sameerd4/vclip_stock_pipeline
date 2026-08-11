from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from vclip_pipeline.reconcile import ReconcileService
from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.review_dedupe import (
    NEAR_REASON,
    DedupeProject,
    ReviewDedupeService,
    aggressive_near_match,
    near_source_range_duplicate,
)
from vclip_pipeline.workflow.review_dedupe_batch import ReviewDedupeBatchService

from test_review_dedupe import (
    _clone_db_candidate,
    _inject_duplicate_project,
    _individual_projects,
    _project_clip_id,
    _write_shard,
)
from test_review_dedupe_batch import _prepare_corpus


def _project(
    clip_id: str,
    *,
    start: float,
    duration: float,
    order: int = 0,
    recovery: str = "not_applicable",
    media: str = "media:X",
) -> DedupeProject:
    return DedupeProject(
        order=order,
        project_name=clip_id,
        project_uid=None,
        event_name="e",
        stock_clip_id=clip_id,
        stockify_run_id="R",
        source_project_id="P",
        representation="individual",
        media_identity=media,
        source_start_seconds=start,
        source_duration_seconds=duration,
        source_start=f"{start}s",
        source_duration=f"{duration}s",
        short_clip_recovery=recovery,
        element=ET.Element("project"),
    )


def test_threshold_boundary_and_media_guardrails():
    base = _project("A", start=0.0, duration=100.0)
    # IoU exactly 0.92 with containment 1.0 (integer ranges avoid float drift).
    # A:0-100, B:8-100 => overlap=92, union=100, shorter=92.
    at_iou = _project("B", start=8.0, duration=92.0, order=1)
    assert aggressive_near_match(base, at_iou) is True

    # IoU just below 0.92 should be preserved.
    below_iou = _project("C", start=9.0, duration=91.0, order=2)
    assert aggressive_near_match(base, below_iou) is False
    assert near_source_range_duplicate(base, below_iou) is False

    # Containment below 0.95 is preserved.
    # A:0-100, D:10-100 => overlap=90, shorter=90 => containment=1.0 still.
    # Use a short clip barely overlapping: A:0-100, D:6-100 duration 94? containment=1.
    # Need shorter not mostly covered: A:0-100, D:20-40 => overlap=20, shorter=20 => 1.0.
    # For containment < 0.95: A:0-100, D:0-10 with... that's containment 1.0 of short.
    # containment = overlap/min(dur). To get <0.95: A:0-100, D:50-100 duration 50,
    # overlap=50, containment=1.0. Always 1.0 if one range contains the other.
    # Partial: A:0-100, D:10-110 duration 100 => overlap=90, containment=0.90.
    low_containment = _project("D", start=10.0, duration=100.0, order=3)
    assert aggressive_near_match(base, low_containment) is False

    other_media = _project("E", start=0.0, duration=100.0, media="media:Y", order=4)
    assert aggressive_near_match(base, other_media) is False


def test_deterministic_near_representative_prefers_exact_rep_and_non_expanded():
    expanded = _project(
        "EXP", start=0.0, duration=10.0, order=0, recovery="expanded_review"
    )
    exact_rep = _project(
        "KEEP", start=0.2, duration=9.7, order=2, recovery="not_applicable"
    )
    longer = _project(
        "LONG", start=0.1, duration=9.9, order=1, recovery="not_applicable"
    )
    cluster = [expanded, longer, exact_rep]
    removals = ReviewDedupeService._choose_near_removals(
        [cluster],
        exact_representatives={"KEEP"},
    )
    assert {item.removed_stock_clip_id for item in removals} == {"EXP", "LONG"}
    assert all(item.kept_stock_clip_id == "KEEP" for item in removals)
    assert all(item.reason == NEAR_REASON for item in removals)
    assert all(item.containment is not None and item.iou is not None for item in removals)


def test_two_and_three_way_near_clusters_on_shard(pipeline_run):
    shard_dir = pipeline_run["tmp_path"] / "near-2-3"
    shard_path = _write_shard(pipeline_run, shard_dir)
    tree = ET.parse(shard_path)
    root = tree.getroot()
    source = _individual_projects(root)[0]
    source_id = _project_clip_id(source)

    near_b = f"{source_id}_NEARB"
    near_c = f"{source_id}_NEARC"
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=source_id,
        new_clip_id=near_b,
        new_project_name=f"{source.get('name')} — NearB",
        short_clip_recovery="not_applicable",
        segment_index=9301,
    )
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=source_id,
        new_clip_id=near_c,
        new_project_name=f"{source.get('name')} — NearC",
        short_clip_recovery="expanded_review",
        segment_index=9302,
    )
    with pipeline_run["database"].transaction() as connection:
        connection.execute(
            """
            UPDATE stock_candidates
            SET proposed_start='0s', proposed_duration='10s',
                proposed_duration_seconds=10,
                original_start='0s', original_duration='10s'
            WHERE stock_clip_id=?
            """,
            (source_id,),
        )
        connection.execute(
            """
            UPDATE stock_candidates
            SET proposed_start='1/5s', proposed_duration='97/10s',
                proposed_duration_seconds=9.7,
                original_start='1/5s', original_duration='97/10s'
            WHERE stock_clip_id=?
            """,
            (near_b,),
        )
        connection.execute(
            """
            UPDATE stock_candidates
            SET proposed_start='3/10s', proposed_duration='96/10s',
                proposed_duration_seconds=9.6,
                original_start='3/10s', original_duration='96/10s'
            WHERE stock_clip_id=?
            """,
            (near_c,),
        )
    _inject_duplicate_project(
        root,
        source_project=source,
        new_name=f"{source.get('name')} — NearB",
        new_clip_id=near_b,
    )
    _inject_duplicate_project(
        root,
        source_project=source,
        new_name=f"{source.get('name')} — NearC",
        new_clip_id=near_c,
    )
    manifest_path = shard_path.with_name(f"{shard_path.stem}-shard-manifest.json")
    manifest = json.loads(manifest_path.read_text())
    manifest["stock_clip_ids"] = list(
        dict.fromkeys([*manifest.get("stock_clip_ids", []), near_b, near_c])
    )
    input_xml = shard_dir / "near-in.fcpxml"
    tree.write(input_xml, encoding="utf-8", xml_declaration=True)
    input_xml.with_name(f"{input_xml.stem}-shard-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    catalog = WorkflowCatalog(pipeline_run["database"])
    report = ReviewDedupeService(pipeline_run["repository"], catalog).run(
        input_xml=input_xml,
        output_xml=shard_dir / "near-out.fcpxml",
        report_path=shard_dir / "near.json",
        text_report_path=shard_dir / "near.txt",
        near_policy="aggressive",
        overwrite=True,
    )
    assert report.exact_projects_removed == 0
    assert report.near_clusters_found == 1
    assert report.near_projects_removed == 2
    out_ids = {
        _project_clip_id(project)
        for project in _individual_projects(
            ET.parse(shard_dir / "near-out.fcpxml").getroot()
        )
    }
    assert source_id in out_ids
    assert near_b not in out_ids
    assert near_c not in out_ids
    assert catalog.review_dedupe_removed_ids(
        pipeline_run["result"].stockify_run_id
    ) >= {near_b, near_c}
    with pipeline_run["database"].connect() as connection:
        rows = connection.execute(
            """
            SELECT removed_stock_clip_id, reason, containment, iou
            FROM review_dedupe_removals
            WHERE removed_stock_clip_id IN (?, ?)
            """,
            (near_b, near_c),
        ).fetchall()
    assert len(rows) == 2
    assert all(row["reason"] == NEAR_REASON for row in rows)
    assert all(row["containment"] is not None and row["iou"] is not None for row in rows)


def test_exact_then_near_and_reconcile_out_of_scope(pipeline_run):
    shard_dir = pipeline_run["tmp_path"] / "exact-then-near"
    shard_path = _write_shard(pipeline_run, shard_dir)
    tree = ET.parse(shard_path)
    root = tree.getroot()
    source = _individual_projects(root)[0]
    source_id = _project_clip_id(source)
    exact_id = f"{source_id}_EXACT"
    near_id = f"{source_id}_NEAR"
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=source_id,
        new_clip_id=exact_id,
        new_project_name=f"{source.get('name')} — Exact",
        short_clip_recovery="not_applicable",
        segment_index=9401,
    )
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=source_id,
        new_clip_id=near_id,
        new_project_name=f"{source.get('name')} — Near",
        short_clip_recovery="not_applicable",
        segment_index=9402,
    )
    with pipeline_run["database"].transaction() as connection:
        connection.execute(
            """
            UPDATE stock_candidates
            SET proposed_start='0s', proposed_duration='10s',
                proposed_duration_seconds=10,
                original_start='0s', original_duration='10s'
            WHERE stock_clip_id IN (?, ?)
            """,
            (source_id, exact_id),
        )
        connection.execute(
            """
            UPDATE stock_candidates
            SET proposed_start='1/5s', proposed_duration='97/10s',
                proposed_duration_seconds=9.7,
                original_start='1/5s', original_duration='97/10s'
            WHERE stock_clip_id=?
            """,
            (near_id,),
        )
    _inject_duplicate_project(
        root,
        source_project=source,
        new_name=f"{source.get('name')} — Exact",
        new_clip_id=exact_id,
    )
    _inject_duplicate_project(
        root,
        source_project=source,
        new_name=f"{source.get('name')} — Near",
        new_clip_id=near_id,
    )
    manifest = json.loads(
        shard_path.with_name(f"{shard_path.stem}-shard-manifest.json").read_text()
    )
    manifest["stock_clip_ids"] = list(
        dict.fromkeys([*manifest.get("stock_clip_ids", []), exact_id, near_id])
    )
    input_xml = shard_dir / "combo-in.fcpxml"
    tree.write(input_xml, encoding="utf-8", xml_declaration=True)
    input_xml.with_name(f"{input_xml.stem}-shard-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    catalog = WorkflowCatalog(pipeline_run["database"])
    output = shard_dir / "combo-out.fcpxml"
    report = ReviewDedupeService(pipeline_run["repository"], catalog).run(
        input_xml=input_xml,
        output_xml=output,
        report_path=shard_dir / "combo.json",
        text_report_path=shard_dir / "combo.txt",
        near_policy="aggressive",
        overwrite=True,
    )
    assert report.exact_projects_removed == 1
    assert report.near_projects_removed == 1
    assert report.projects_removed == 2

    reconcile = ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=output,
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="observed-projects",
        report_path=None,
        allow_conflicts=False,
    )
    rejected = {
        row["stock_clip_id"]
        for row in reconcile.decisions
        if row["review_status"] == "rejected"
    }
    assert exact_id not in rejected
    assert near_id not in rejected
    with pipeline_run["database"].connect() as connection:
        statuses = {
            row["stock_clip_id"]: row["review_status"]
            for row in connection.execute(
                """
                SELECT stock_clip_id, review_status FROM stock_candidates
                WHERE stock_clip_id IN (?, ?)
                """,
                (exact_id, near_id),
            ).fetchall()
        }
    assert statuses[exact_id] == "pending"
    assert statuses[near_id] == "pending"


def test_batch_aggressive_totals_and_idempotent_rerun(pipeline_run):
    root = pipeline_run["tmp_path"] / "batch-aggressive"
    corpus = _prepare_corpus(pipeline_run, root)
    # Exact on shard A.
    from test_review_dedupe_batch import _add_exact_duplicate, _add_near_duplicate

    exact_id = _add_exact_duplicate(
        pipeline_run,
        corpus["shard_a"],
        corpus["manifest_a"],
        "BEXACT",
        segment_index=9501,
    )
    near_id = _add_near_duplicate(
        pipeline_run,
        corpus["shard_b"],
        corpus["manifest_b"],
        "BNEAR",
        segment_index=9502,
    )
    catalog = WorkflowCatalog(pipeline_run["database"])
    service = ReviewDedupeBatchService(pipeline_run["repository"], catalog)
    kwargs = dict(
        input_root=corpus["portable_root"],
        manifest_root=corpus["manifest_root"],
        output_root=root / "clean",
        report_path=root / "bulk.json",
        text_report_path=root / "bulk.txt",
        overwrite=True,
        near_policy="aggressive",
    )
    first = service.run(**kwargs)
    second = service.run(**kwargs)
    assert first.exact_projects_removed >= 1
    assert first.near_projects_removed >= 1
    assert first.projects_removed == (
        first.exact_projects_removed + first.near_projects_removed
    )
    assert first.projects_removed == second.projects_removed
    assert first.exact_projects_removed == second.exact_projects_removed
    assert first.near_projects_removed == second.near_projects_removed
    payload = json.loads((root / "bulk.json").read_text())
    assert payload["near_policy"] == "aggressive"
    assert payload["exact_projects_removed"] == first.exact_projects_removed
    assert payload["near_projects_removed"] == first.near_projects_removed
    assert payload["thresholds"]["near_containment"] == 0.95
    assert payload["thresholds"]["near_iou"] == 0.92

    with pipeline_run["database"].connect() as connection:
        exact_count = connection.execute(
            """
            SELECT COUNT(*) AS n FROM review_dedupe_removals
            WHERE removed_stock_clip_id=? AND reason=?
            """,
            (exact_id, "exact_source_range_duplicate"),
        ).fetchone()["n"]
        near_count = connection.execute(
            """
            SELECT COUNT(*) AS n FROM review_dedupe_removals
            WHERE removed_stock_clip_id=? AND reason=?
            """,
            (near_id, NEAR_REASON),
        ).fetchone()["n"]
    assert exact_count == 1
    assert near_count == 1


def test_default_near_policy_none_does_not_remove_near(pipeline_run):
    root = pipeline_run["tmp_path"] / "batch-none"
    corpus = _prepare_corpus(pipeline_run, root)
    from test_review_dedupe_batch import _add_near_duplicate

    near_id = _add_near_duplicate(
        pipeline_run,
        corpus["shard_a"],
        corpus["manifest_a"],
        "KEEPNEAR",
        segment_index=9601,
    )
    report = ReviewDedupeBatchService(
        pipeline_run["repository"], WorkflowCatalog(pipeline_run["database"])
    ).run(
        input_root=corpus["portable_root"],
        manifest_root=corpus["manifest_root"],
        output_root=root / "clean",
        report_path=root / "bulk.json",
        text_report_path=root / "bulk.txt",
        overwrite=True,
        near_policy="none",
    )
    assert report.near_projects_removed == 0
    out_a = root / "clean" / "market-a" / corpus["shard_a"].name
    out_ids = {
        _project_clip_id(project)
        for project in _individual_projects(ET.parse(out_a).getroot())
    }
    assert near_id in out_ids
    assert report.near_duplicate_audit["near_duplicate_pairs"] >= 1
