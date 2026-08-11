from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from vclip_pipeline.reconcile import ReconcileService
from vclip_pipeline.stockify.fcpxml import validate_fcpxml
from vclip_pipeline.util import utc_now
from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.review_prune import (
    REASON,
    ReviewPruneService,
    effective_duration_seconds,
)

from test_review_dedupe import (
    _clone_db_candidate,
    _inject_duplicate_project,
    _individual_projects,
    _project_clip_id,
)
from test_review_dedupe_global import _append_manifest_project, _prepare_global_input


def _set_durations(
    database,
    clip_id: str,
    *,
    final: float | None = None,
    proposed: float | None = None,
    original: float | None = None,
    run_id: str | None = None,
    candidate_tier: str | None = "Review_unexpanded_short",
    short_clip_recovery: str | None = "not_enough_source_media",
) -> None:
    assignments = []
    values: list = []
    if final is not None:
        assignments.append("final_duration_seconds=?")
        values.append(final)
        assignments.append("final_duration=?")
        values.append(f"{final}s")
    if proposed is not None:
        assignments.append("proposed_duration_seconds=?")
        values.append(proposed)
        assignments.append("proposed_duration=?")
        values.append(f"{proposed}s")
    if original is not None:
        assignments.append("original_duration_seconds=?")
        values.append(original)
        assignments.append("original_duration=?")
        values.append(f"{original}s")
    if candidate_tier is not None:
        assignments.append("candidate_tier=?")
        values.append(candidate_tier)
    if short_clip_recovery is not None:
        assignments.append("short_clip_recovery=?")
        values.append(short_clip_recovery)
    where = "stock_clip_id=?"
    values.append(clip_id)
    if run_id is not None:
        where += " AND run_id=?"
        values.append(run_id)
    with database.transaction() as connection:
        connection.execute(
            f"UPDATE stock_candidates SET {', '.join(assignments)} WHERE {where}",
            values,
        )


def _first_clip(xml_path: Path) -> tuple[str, str]:
    project = _individual_projects(ET.parse(xml_path).getroot())[0]
    return _project_clip_id(project), project.get("name") or ""


def _run_prune(
    pipeline_run,
    input_root: Path,
    output_root: Path,
    *,
    min_duration: float = 3.0,
    dry_run: bool = False,
    overwrite: bool = True,
):
    catalog = WorkflowCatalog(pipeline_run["database"])
    reports = output_root.parent / "library-audits"
    report = ReviewPruneService(pipeline_run["repository"], catalog).run(
        input_root=input_root,
        output_root=output_root,
        report_path=reports / "short-prune.json",
        text_report_path=reports / "short-prune.txt",
        min_duration=min_duration,
        dry_run=dry_run,
        overwrite=overwrite,
    )
    return report, catalog, reports


def test_effective_duration_precedence():
    assert effective_duration_seconds(
        {
            "final_duration_seconds": 2.5,
            "proposed_duration_seconds": 9.0,
            "original_duration_seconds": 12.0,
        }
    ) == (2.5, "final_duration_seconds")
    assert effective_duration_seconds(
        {
            "final_duration_seconds": None,
            "proposed_duration_seconds": 2.0,
            "original_duration_seconds": 12.0,
        }
    ) == (2.0, "proposed_duration_seconds")
    assert effective_duration_seconds(
        {
            "final_duration_seconds": None,
            "proposed_duration_seconds": None,
            "original_duration_seconds": 1.5,
        }
    ) == (1.5, "original_duration_seconds")


def test_threshold_boundaries_lt_eq_gt(pipeline_run):
    root = pipeline_run["tmp_path"] / "prune-threshold"
    corpus = _prepare_global_input(pipeline_run, root)
    short_id, short_name = _first_clip(corpus["shard_a"])
    exact_id, exact_name = _first_clip(corpus["shard_b"])
    # Ensure shard-b has a distinct clip; if same, clone one for exact boundary.
    if exact_id == short_id:
        tree = ET.parse(corpus["shard_b"])
        xml_root = tree.getroot()
        source = _individual_projects(xml_root)[0]
        exact_name = f"{source.get('name')} — EXACT3"
        exact_id = f"{short_id}_EXACT3"
        _clone_db_candidate(
            pipeline_run["database"],
            source_clip_id=short_id,
            new_clip_id=exact_id,
            new_project_name=exact_name,
            short_clip_recovery="not_applicable",
            segment_index=9301,
        )
        _inject_duplicate_project(
            xml_root, source_project=source, new_name=exact_name, new_clip_id=exact_id
        )
        tree.write(corpus["shard_b"], encoding="utf-8", xml_declaration=True)
        _append_manifest_project(
            corpus["manifest_b"], clip_id=exact_id, project_name=exact_name
        )

    _set_durations(pipeline_run["database"], short_id, proposed=2.9)
    _set_durations(pipeline_run["database"], exact_id, proposed=3.0)

    # Keep a clearly long survivor on shard_a if possible by cloning.
    tree = ET.parse(corpus["shard_a"])
    xml_root = tree.getroot()
    source = _individual_projects(xml_root)[0]
    long_name = f"{source.get('name')} — LONG"
    long_id = f"{short_id}_LONG"
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=short_id,
        new_clip_id=long_id,
        new_project_name=long_name,
        short_clip_recovery="not_applicable",
        segment_index=9302,
    )
    _inject_duplicate_project(
        xml_root, source_project=source, new_name=long_name, new_clip_id=long_id
    )
    tree.write(corpus["shard_a"], encoding="utf-8", xml_declaration=True)
    _append_manifest_project(
        corpus["manifest_a"], clip_id=long_id, project_name=long_name
    )
    _set_durations(
        pipeline_run["database"],
        long_id,
        proposed=5.0,
        candidate_tier="Standard",
        short_clip_recovery="not_applicable",
    )

    report, catalog, _ = _run_prune(
        pipeline_run, corpus["input_root"], root / "final"
    )
    removed = catalog.review_short_prune_removed_ids(
        pipeline_run["result"].stockify_run_id
    )
    assert short_id in removed
    assert exact_id not in removed
    assert long_id not in removed
    assert report.candidates_removed >= 1
    assert report.post_write_verification["remaining_short_candidates"] == 0

    out_a = root / "final" / "market-a" / corpus["shard_a"].name
    names = {p.get("name") for p in _individual_projects(ET.parse(out_a).getroot())}
    assert short_name not in names
    assert long_name in names
    assert any("Stock Compilation" in (n or "") for n in {
        p.get("name", "")
        for p in ET.parse(out_a).getroot().findall("./library/event/project")
    })


def test_duplicate_clip_ids_across_different_runs(pipeline_run):
    root = pipeline_run["tmp_path"] / "prune-cross-run"
    corpus = _prepare_global_input(pipeline_run, root)
    clip_id, project_name = _first_clip(corpus["shard_a"])
    run_a = pipeline_run["result"].stockify_run_id
    run_b = "STOCKIFY_PRUNE_OTHER_RUN"

    with pipeline_run["database"].transaction() as connection:
        connection.execute(
            """
            INSERT INTO stockify_runs(
                id, source_xml_path, source_xml_sha256, output_xml_path,
                report_path, pipeline_version, status, options_json, started_at,
                completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'complete', '{}', ?, ?)
            """,
            (
                run_b,
                "/tmp/other.xml",
                "sha-other",
                "/tmp/other-out.xml",
                "/tmp/other-report.json",
                "test",
                utc_now(),
                utc_now(),
            ),
        )
        source = dict(
            connection.execute(
                "SELECT * FROM stock_candidates WHERE stock_clip_id=? AND run_id=?",
                (clip_id, run_a),
            ).fetchone()
        )
        project = dict(
            connection.execute(
                "SELECT * FROM source_projects WHERE id=?",
                (source["source_project_id"],),
            ).fetchone()
        )
        event = dict(
            connection.execute(
                "SELECT * FROM source_events WHERE id=?",
                (project["source_event_id"],),
            ).fetchone()
        )
        event_id = f"{event['id']}_B"
        event.update({"id": event_id, "run_id": run_b})
        connection.execute(
            f"INSERT INTO source_events ({', '.join(event)}) VALUES "
            f"({', '.join('?' for _ in event)})",
            list(event.values()),
        )
        project_id = f"{project['id']}_B"
        project.update(
            {"id": project_id, "run_id": run_b, "source_event_id": event_id}
        )
        connection.execute(
            f"INSERT INTO source_projects ({', '.join(project)}) VALUES "
            f"({', '.join('?' for _ in project)})",
            list(project.values()),
        )
        source.update(
            {
                "run_id": run_b,
                "source_project_id": project_id,
                "proposed_duration_seconds": 8.0,
                "proposed_duration": "8s",
                "final_duration_seconds": None,
                "candidate_tier": "Standard",
                "short_clip_recovery": "not_applicable",
            }
        )
        connection.execute(
            f"INSERT INTO stock_candidates ({', '.join(source)}) VALUES "
            f"({', '.join('?' for _ in source)})",
            list(source.values()),
        )

    _set_durations(pipeline_run["database"], clip_id, proposed=1.5, run_id=run_a)

    # Shard B represents the long same-ID candidate from run_b.
    tree = ET.parse(corpus["shard_a"])
    project = _individual_projects(tree.getroot())[0]
    tree_b = ET.parse(corpus["shard_b"])
    root_b = tree_b.getroot()
    for event in root_b.findall("./library/event"):
        for node in list(event.findall("project")):
            if "Stock Compilation" not in (node.get("name") or ""):
                event.remove(node)
    event_b = root_b.find("./library/event")
    assert event_b is not None
    clone = ET.fromstring(ET.tostring(project))
    clone.set("name", project_name)
    event_b.append(clone)
    tree_b.write(corpus["shard_b"], encoding="utf-8", xml_declaration=True)
    corpus["manifest_b"].write_text(
        json.dumps(
            {
                "stockify_run_id": run_b,
                "stock_clip_ids": [clip_id],
                "projects": [
                    {
                        "event_name": "Other Run Event",
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

    report, catalog, _ = _run_prune(
        pipeline_run, corpus["input_root"], root / "final"
    )
    assert clip_id in catalog.review_short_prune_removed_ids(run_a)
    assert clip_id not in catalog.review_short_prune_removed_ids(run_b)
    assert any(
        item["stockify_run_id"] == run_a and item["removed_stock_clip_id"] == clip_id
        for item in report.removals
    )
    assert not any(item["stockify_run_id"] == run_b for item in report.removals)


def test_manifest_and_xml_rewrite(pipeline_run):
    root = pipeline_run["tmp_path"] / "prune-rewrite"
    corpus = _prepare_global_input(pipeline_run, root)
    clip_id, project_name = _first_clip(corpus["shard_a"])
    _set_durations(pipeline_run["database"], clip_id, proposed=1.25)
    before = corpus["shard_a"].read_bytes()
    output_root = root / "final"
    report, _, _ = _run_prune(pipeline_run, corpus["input_root"], output_root)
    assert report.shards_changed >= 1
    assert corpus["shard_a"].read_bytes() == before

    out_xml = output_root / "market-a" / corpus["shard_a"].name
    assert validate_fcpxml(ET.parse(out_xml).getroot()).passed
    names = {p.get("name") for p in _individual_projects(ET.parse(out_xml).getroot())}
    assert project_name not in names
    cleaned = json.loads(
        out_xml.with_name(f"{out_xml.stem}-shard-manifest.json").read_text()
    )
    assert clip_id not in cleaned["stock_clip_ids"]
    assert all(
        clip_id not in (project.get("stock_clip_ids") or [])
        for project in cleaned.get("projects") or []
    )
    assert cleaned["short_prune"]["reason"] == REASON


def test_db_provenance_and_reconcile_out_of_scope(pipeline_run):
    root = pipeline_run["tmp_path"] / "prune-recon"
    corpus = _prepare_global_input(pipeline_run, root)
    clip_id, _ = _first_clip(corpus["shard_a"])
    _set_durations(
        pipeline_run["database"],
        clip_id,
        proposed=0.8,
        short_clip_recovery="srt_rejected_expansion",
    )
    output_root = root / "final"
    report, catalog, _ = _run_prune(pipeline_run, corpus["input_root"], output_root)
    assert clip_id in catalog.review_short_prune_removed_ids(
        pipeline_run["result"].stockify_run_id
    )
    assert clip_id in catalog.all_pre_review_dedupe_removed_ids(
        pipeline_run["result"].stockify_run_id
    )
    with pipeline_run["database"].connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) AS n FROM stock_candidates WHERE stock_clip_id=?",
                (clip_id,),
            ).fetchone()["n"]
            == 1
        )
        row = connection.execute(
            """
            SELECT reason, effective_duration_seconds, min_duration_seconds,
                   input_xml, output_xml, short_clip_recovery
            FROM review_short_prune_removals
            WHERE removed_stock_clip_id=?
            """,
            (clip_id,),
        ).fetchone()
    assert row["reason"] == REASON
    assert row["effective_duration_seconds"] == 0.8
    assert row["min_duration_seconds"] == 3.0
    assert row["short_clip_recovery"] == "srt_rejected_expansion"
    assert row["input_xml"]
    assert row["output_xml"]

    reviewed = output_root / "market-b" / corpus["shard_b"].name
    recon = ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=reviewed,
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="observed-projects",
        report_path=None,
        allow_conflicts=False,
    )
    rejected = {
        row["stock_clip_id"]
        for row in recon.decisions
        if row["review_status"] == "rejected"
    }
    assert clip_id not in rejected
    with pipeline_run["database"].connect() as connection:
        status = connection.execute(
            "SELECT review_status FROM stock_candidates WHERE stock_clip_id=?",
            (clip_id,),
        ).fetchone()["review_status"]
    assert status == "pending"
    assert report.recovery_reason_breakdown.get("srt_rejected_expansion", 0) >= 1


def test_dry_run_no_xml_or_db_writes(pipeline_run):
    root = pipeline_run["tmp_path"] / "prune-dry"
    corpus = _prepare_global_input(pipeline_run, root)
    clip_id, _ = _first_clip(corpus["shard_a"])
    _set_durations(pipeline_run["database"], clip_id, proposed=1.1)
    output_root = root / "final"
    report, catalog, reports = _run_prune(
        pipeline_run, corpus["input_root"], output_root, dry_run=True
    )
    assert report.dry_run is True
    assert report.candidates_removed >= 1
    assert not output_root.exists() or not any(output_root.rglob("*.fcpxml"))
    assert catalog.review_short_prune_removed_ids(
        pipeline_run["result"].stockify_run_id
    ) == set()
    assert report.post_write_verification["remaining_short_candidates"] == 0
    assert (reports / "short-prune.json").is_file()


def test_overwrite_and_idempotent_rerun(pipeline_run):
    root = pipeline_run["tmp_path"] / "prune-idem"
    corpus = _prepare_global_input(pipeline_run, root)
    clip_id, _ = _first_clip(corpus["shard_a"])
    _set_durations(pipeline_run["database"], clip_id, proposed=2.2)
    output_root = root / "final"
    _run_prune(pipeline_run, corpus["input_root"], output_root)
    report2, _, _ = _run_prune(
        pipeline_run, corpus["input_root"], output_root, overwrite=True
    )
    with pipeline_run["database"].connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) AS n FROM review_short_prune_removals"
            ).fetchone()["n"]
            == 1
        )
    assert report2.shards_failed == 0
    assert report2.post_write_verification["remaining_short_candidates"] == 0


def test_partial_failure_does_not_corrupt_successful_shards(pipeline_run):
    root = pipeline_run["tmp_path"] / "prune-partial"
    corpus = _prepare_global_input(pipeline_run, root)
    clip_a, _ = _first_clip(corpus["shard_a"])
    clip_b, _ = _first_clip(corpus["shard_b"])
    _set_durations(pipeline_run["database"], clip_a, proposed=1.0)
    if clip_b != clip_a:
        _set_durations(pipeline_run["database"], clip_b, proposed=1.2)

    output_root = root / "final"
    # Block market-b writes by occupying the destination parent path as a file.
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "market-b").write_text("blocked", encoding="utf-8")

    report, catalog, _ = _run_prune(
        pipeline_run, corpus["input_root"], output_root, overwrite=True
    )
    assert report.shards_failed >= 1
    assert any("market-b" in item["relative_path"] for item in report.failures)
    # Successful shard still written.
    out_a = output_root / "market-a" / corpus["shard_a"].name
    assert out_a.is_file()
    # Only successful shard removals persisted.
    removed = catalog.review_short_prune_removed_ids(
        pipeline_run["result"].stockify_run_id
    )
    assert clip_a in removed
    if clip_b != clip_a:
        # market-b failed, so its prune row should not be recorded.
        assert clip_b not in removed or clip_b == clip_a
