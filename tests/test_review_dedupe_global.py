from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from vclip_pipeline.reconcile import ReconcileService
from vclip_pipeline.stockify.fcpxml import validate_fcpxml
from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.review_dedupe_global import ReviewGlobalDedupeService

from test_review_dedupe import (
    _clone_db_candidate,
    _inject_duplicate_project,
    _individual_projects,
    _project_clip_id,
)
from test_review_dedupe_batch import _prepare_corpus


def _prepare_global_input(pipeline_run, root: Path, *, shard_count: int = 2) -> dict:
    """Co-locate portable XMLs with manifests under one clean input root."""
    corpus = _prepare_corpus(pipeline_run, root)
    input_root = root / "review-shards-clean"
    input_root.mkdir(parents=True, exist_ok=True)

    pairs = [
        ("market-a", corpus["shard_a"], corpus["manifest_a"]),
        ("market-b", corpus["shard_b"], corpus["manifest_b"]),
    ]
    if shard_count >= 3:
        # Third shard starts as a copy of market-b (different clip IDs than market-a).
        market_c_portable = corpus["portable_root"] / "market-c"
        market_c_portable.mkdir(parents=True, exist_ok=True)
        xml_c_name = corpus["shard_b"].name.replace(".fcpxml", "-c.fcpxml")
        xml_c = market_c_portable / xml_c_name
        shutil.copy2(corpus["shard_b"], xml_c)
        manifest_c = corpus["manifest_root"] / "market-c" / f"{xml_c.stem}-shard-manifest.json"
        manifest_c.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(corpus["manifest_b"], manifest_c)
        pairs.append(("market-c", xml_c, manifest_c))

    result: dict = {"input_root": input_root}
    for market, xml_src, manifest_src in pairs:
        dest_dir = input_root / market
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_xml = dest_dir / Path(xml_src).name
        shutil.copy2(xml_src, dest_xml)
        dest_manifest = dest_xml.with_name(f"{dest_xml.stem}-shard-manifest.json")
        shutil.copy2(manifest_src, dest_manifest)
        key = market[-1]
        result[f"shard_{key}"] = dest_xml
        result[f"manifest_{key}"] = dest_manifest
        result[market] = dest_xml

    result["shard_a"] = result["shard_a"]
    result["shard_b"] = result["shard_b"]
    result["manifest_a"] = result["manifest_a"]
    result["manifest_b"] = result["manifest_b"]
    return result


def _append_manifest_project(
    manifest_path: Path,
    *,
    clip_id: str,
    project_name: str,
    event_name: str | None = None,
) -> None:
    manifest = json.loads(manifest_path.read_text())
    manifest["stock_clip_ids"] = list(
        dict.fromkeys([*manifest.get("stock_clip_ids", []), clip_id])
    )
    projects = list(manifest.get("projects") or [])
    projects.append(
        {
            "event_name": event_name or (projects[0].get("event_name") if projects else ""),
            "project_name": project_name,
            "project_uid": f"uid-{clip_id[-12:]}",
            "representation": "individual",
            "source_project_id": None,
            "stock_clip_ids": [clip_id],
        }
    )
    manifest["projects"] = projects
    manifest["project_count"] = len(projects)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _add_cross_shard_exact(
    pipeline_run,
    source_xml: Path,
    target_xml: Path,
    target_manifest: Path,
    suffix: str,
    *,
    segment_index: int,
    event_name: str | None = None,
) -> tuple[str, str, str]:
    source_tree = ET.parse(source_xml)
    source_project = _individual_projects(source_tree.getroot())[0]
    source_id = _project_clip_id(source_project)
    dup_name = f"{source_project.get('name')} — {suffix}"
    dup_id = f"{source_id}_{suffix}"
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=source_id,
        new_clip_id=dup_id,
        new_project_name=dup_name,
        short_clip_recovery="not_applicable",
        segment_index=segment_index,
    )
    target_tree = ET.parse(target_xml)
    target_root = target_tree.getroot()
    anchor = _individual_projects(target_root)[0]
    clone = _inject_duplicate_project(
        target_root,
        source_project=anchor,
        new_name=dup_name,
        new_clip_id=dup_id,
    )
    # Copy source range metadata identity is already via DB clone.
    if event_name is not None:
        for event in target_root.findall("./library/event"):
            if clone in list(event.findall("project")):
                event.set("name", event_name)
                break
    target_tree.write(target_xml, encoding="utf-8", xml_declaration=True)
    _append_manifest_project(
        target_manifest,
        clip_id=dup_id,
        project_name=dup_name,
        event_name=event_name,
    )
    return source_id, dup_id, dup_name


def _add_cross_shard_near(
    pipeline_run,
    source_xml: Path,
    target_xml: Path,
    target_manifest: Path,
    suffix: str,
    *,
    segment_index: int,
) -> tuple[str, str]:
    source_id, near_id, _ = _add_cross_shard_exact(
        pipeline_run,
        source_xml,
        target_xml,
        target_manifest,
        suffix,
        segment_index=segment_index,
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
            (near_id,),
        )
    return source_id, near_id


def _set_candidate_fields(database, clip_id: str, **fields) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key}=?" for key in fields)
    with database.transaction() as connection:
        connection.execute(
            f"UPDATE stock_candidates SET {assignments} WHERE stock_clip_id=?",
            [*fields.values(), clip_id],
        )


def _run_global(
    pipeline_run,
    input_root: Path,
    output_root: Path,
    *,
    near_policy: str = "none",
    dry_run: bool = False,
    overwrite: bool = True,
):
    catalog = WorkflowCatalog(pipeline_run["database"])
    reports = output_root.parent / "duplicate-reports"
    report = ReviewGlobalDedupeService(pipeline_run["repository"], catalog).run(
        input_root=input_root,
        output_root=output_root,
        report_path=reports / "global-dedupe.json",
        text_report_path=reports / "global-dedupe.txt",
        conflict_report_path=reports / "global-metadata-conflicts.json",
        near_policy=near_policy,
        dry_run=dry_run,
        overwrite=overwrite,
    )
    return report, catalog, reports


def test_exact_duplicate_across_two_shards(pipeline_run):
    root = pipeline_run["tmp_path"] / "global-2way"
    corpus = _prepare_global_input(pipeline_run, root)
    source_id, dup_id, dup_name = _add_cross_shard_exact(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_b"],
        corpus["manifest_b"],
        "G2",
        segment_index=9201,
    )
    before_a = corpus["shard_a"].read_bytes()
    report, catalog, _ = _run_global(
        pipeline_run, corpus["input_root"], root / "review-shards-canonical"
    )
    assert report.projects_removed == 1
    assert report.connected_clusters == 1
    assert report.exact_only_clusters == 1
    assert report.post_write_audit["remaining_exact_global_pairs"] == 0
    assert corpus["shard_a"].read_bytes() == before_a  # input untouched

    out_b = root / "review-shards-canonical" / "market-b" / corpus["shard_b"].name
    names = {p.get("name") for p in _individual_projects(ET.parse(out_b).getroot())}
    assert dup_name not in names
    cleaned = json.loads(
        out_b.with_name(f"{out_b.stem}-shard-manifest.json").read_text()
    )
    assert dup_id not in cleaned["stock_clip_ids"]
    assert source_id in catalog.review_global_dedupe_removed_ids(
        pipeline_run["result"].stockify_run_id
    ) or dup_id in catalog.review_global_dedupe_removed_ids(
        pipeline_run["result"].stockify_run_id
    )


def test_exact_duplicate_across_three_shards(pipeline_run):
    root = pipeline_run["tmp_path"] / "global-3way"
    corpus = _prepare_global_input(pipeline_run, root, shard_count=3)
    source_id, dup_b, _ = _add_cross_shard_exact(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_b"],
        corpus["manifest_b"],
        "G3B",
        segment_index=9202,
    )
    _, dup_c, _ = _add_cross_shard_exact(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_c"],
        corpus["manifest_c"],
        "G3C",
        segment_index=9203,
    )
    report, catalog, _ = _run_global(
        pipeline_run, corpus["input_root"], root / "canonical"
    )
    assert report.connected_clusters == 1
    assert report.projects_removed == 2
    removed = catalog.review_global_dedupe_removed_ids(
        pipeline_run["result"].stockify_run_id
    )
    assert len(removed & {source_id, dup_b, dup_c}) == 2
    assert report.post_write_audit["remaining_exact_global_pairs"] == 0


def test_near_duplicate_across_shards(pipeline_run):
    root = pipeline_run["tmp_path"] / "global-near"
    corpus = _prepare_global_input(pipeline_run, root)
    _add_cross_shard_near(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_b"],
        corpus["manifest_b"],
        "GNEAR",
        segment_index=9204,
    )
    report_none, _, _ = _run_global(
        pipeline_run,
        corpus["input_root"],
        root / "canonical-none",
        near_policy="none",
    )
    assert report_none.projects_removed == 0
    assert report_none.near_pair_relationships == 0

    report, _, _ = _run_global(
        pipeline_run,
        corpus["input_root"],
        root / "canonical-agg",
        near_policy="aggressive",
    )
    assert report.projects_removed == 1
    assert report.near_only_clusters == 1
    assert report.post_write_audit["remaining_aggressive_near_pairs"] == 0
    assert report.post_write_audit["remaining_exact_global_pairs"] == 0


def test_mixed_exact_near_connected_component(pipeline_run):
    root = pipeline_run["tmp_path"] / "global-mixed"
    corpus = _prepare_global_input(pipeline_run, root, shard_count=3)
    source_id, dup_b, _ = _add_cross_shard_exact(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_b"],
        corpus["manifest_b"],
        "MIXB",
        segment_index=9205,
    )
    # Make A/B exact at 0-10, then C near of A.
    with pipeline_run["database"].transaction() as connection:
        for clip_id in (source_id, dup_b):
            connection.execute(
                """
                UPDATE stock_candidates
                SET proposed_start='0s', proposed_duration='10s',
                    proposed_duration_seconds=10,
                    original_start='0s', original_duration='10s'
                WHERE stock_clip_id=?
                """,
                (clip_id,),
            )
    _, near_c = _add_cross_shard_near(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_c"],
        corpus["manifest_c"],
        "MIXC",
        segment_index=9206,
    )
    # Re-apply A/B exact ranges after near helper mutated source_id.
    with pipeline_run["database"].transaction() as connection:
        for clip_id in (source_id, dup_b):
            connection.execute(
                """
                UPDATE stock_candidates
                SET proposed_start='0s', proposed_duration='10s',
                    proposed_duration_seconds=10,
                    original_start='0s', original_duration='10s'
                WHERE stock_clip_id=?
                """,
                (clip_id,),
            )
        connection.execute(
            """
            UPDATE stock_candidates
            SET proposed_start='1/5s', proposed_duration='97/10s',
                proposed_duration_seconds=9.7,
                original_start='1/5s', original_duration='97/10s'
            WHERE stock_clip_id=?
            """,
            (near_c,),
        )

    report, catalog, _ = _run_global(
        pipeline_run,
        corpus["input_root"],
        root / "canonical",
        near_policy="aggressive",
    )
    assert report.connected_clusters == 1
    assert report.mixed_clusters == 1
    assert report.projects_removed == 2
    removed = catalog.review_global_dedupe_removed_ids(
        pipeline_run["result"].stockify_run_id
    )
    assert len(removed & {source_id, dup_b, near_c}) == 2


def test_same_shard_exact_not_part_of_global_pass(pipeline_run):
    root = pipeline_run["tmp_path"] / "global-same-shard"
    corpus = _prepare_global_input(pipeline_run, root)
    # Inject exact duplicate into the same shard only.
    tree = ET.parse(corpus["shard_a"])
    root_xml = tree.getroot()
    source = _individual_projects(root_xml)[0]
    source_id = _project_clip_id(source)
    dup_name = f"{source.get('name')} — SAME"
    dup_id = f"{source_id}_SAME"
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=source_id,
        new_clip_id=dup_id,
        new_project_name=dup_name,
        short_clip_recovery="not_applicable",
        segment_index=9207,
    )
    _inject_duplicate_project(
        root_xml, source_project=source, new_name=dup_name, new_clip_id=dup_id
    )
    tree.write(corpus["shard_a"], encoding="utf-8", xml_declaration=True)
    _append_manifest_project(
        corpus["manifest_a"], clip_id=dup_id, project_name=dup_name
    )

    report, catalog, _ = _run_global(
        pipeline_run, corpus["input_root"], root / "canonical"
    )
    assert report.projects_removed == 0
    assert report.connected_clusters == 0
    assert catalog.review_global_dedupe_removed_ids(
        pipeline_run["result"].stockify_run_id
    ) == set()
    # Both still present in mirrored output.
    out_a = root / "canonical" / "market-a" / corpus["shard_a"].name
    names = {p.get("name") for p in _individual_projects(ET.parse(out_a).getroot())}
    assert source.get("name") in names
    assert dup_name in names


def test_approved_matched_candidate_wins(pipeline_run):
    root = pipeline_run["tmp_path"] / "global-approved"
    corpus = _prepare_global_input(pipeline_run, root)
    source_id, dup_id, _ = _add_cross_shard_exact(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_b"],
        corpus["manifest_b"],
        "APPR",
        segment_index=9208,
    )
    # Later shard candidate is approved; should become canonical.
    _set_candidate_fields(
        pipeline_run["database"], dup_id, review_status="approved"
    )
    report, catalog, _ = _run_global(
        pipeline_run, corpus["input_root"], root / "canonical"
    )
    assert report.clusters[0].canonical_stock_clip_id == dup_id
    assert source_id in catalog.review_global_dedupe_removed_ids(
        pipeline_run["result"].stockify_run_id
    )
    assert "approved_review_status" in report.clusters[0].keeper_selection_reasons


def test_known_location_beats_unknown(pipeline_run):
    root = pipeline_run["tmp_path"] / "global-loc"
    corpus = _prepare_global_input(pipeline_run, root)
    source_id, dup_id, _ = _add_cross_shard_exact(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_b"],
        corpus["manifest_b"],
        "LOC",
        segment_index=9209,
    )
    # Drop shared session linkage on the source so session labels cannot mask the
    # unknown/known contrast under test.
    _set_candidate_fields(
        pipeline_run["database"],
        source_id,
        location_json=json.dumps({"public_label": "Unknown Location", "city": ""}),
        session_id=None,
    )
    _set_candidate_fields(
        pipeline_run["database"],
        dup_id,
        location_json=json.dumps(
            {"public_label": "South Lake Union, Seattle", "city": "Seattle"}
        ),
        session_id=None,
    )
    report, catalog, _ = _run_global(
        pipeline_run, corpus["input_root"], root / "canonical"
    )
    assert report.clusters[0].canonical_stock_clip_id == dup_id
    assert report.clusters[0].metadata_conflict_class == "known_plus_unknown"
    assert source_id in catalog.review_global_dedupe_removed_ids(
        pipeline_run["result"].stockify_run_id
    )


def test_structured_evidence_beats_misleading_event_title(pipeline_run):
    root = pipeline_run["tmp_path"] / "global-structured"
    corpus = _prepare_global_input(pipeline_run, root)
    source_id, dup_id, _ = _add_cross_shard_exact(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_b"],
        corpus["manifest_b"],
        "STRUCT",
        segment_index=9210,
        event_name="Carleton Park, Seattle — 2026-04-17",
    )
    # Both share the same structured Seattle location; event title alone must not
    # block collapse or invent a conflict when structured metadata agrees.
    for clip_id in (source_id, dup_id):
        _set_candidate_fields(
            pipeline_run["database"],
            clip_id,
            location_json=json.dumps(
                {"public_label": "South Lake Union, Seattle", "city": "Seattle"}
            ),
            capture_time_json=json.dumps({"capture_date": "2026-04-22"}),
        )
    report, _, reports = _run_global(
        pipeline_run, corpus["input_root"], root / "canonical"
    )
    assert report.projects_removed == 1
    assert report.clusters[0].metadata_conflict_class == "consistent"
    conflicts = json.loads((reports / "global-metadata-conflicts.json").read_text())
    assert conflicts["conflict_cluster_count"] == 0


def test_conflicting_known_metadata_logged_but_still_collapses(pipeline_run):
    root = pipeline_run["tmp_path"] / "global-conflict"
    corpus = _prepare_global_input(pipeline_run, root)
    source_id, dup_id, _ = _add_cross_shard_exact(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_b"],
        corpus["manifest_b"],
        "CONF",
        segment_index=9211,
        event_name="Carleton Park, Seattle — 2026-04-17",
    )
    _set_candidate_fields(
        pipeline_run["database"],
        source_id,
        location_json=json.dumps(
            {"public_label": "South Lake Union, Seattle", "city": "Seattle"}
        ),
        capture_time_json=json.dumps({"capture_date": "2026-04-22"}),
    )
    _set_candidate_fields(
        pipeline_run["database"],
        dup_id,
        location_json=json.dumps(
            {"public_label": "Carleton Park, Seattle", "city": "Seattle"}
        ),
        capture_time_json=json.dumps({"capture_date": "2026-04-17"}),
    )
    report, catalog, reports = _run_global(
        pipeline_run, corpus["input_root"], root / "canonical"
    )
    assert report.projects_removed == 1
    assert report.clusters[0].metadata_conflict_class == "conflicting_known_labels"
    assert report.metadata_conflict_clusters == 1
    conflicts = json.loads((reports / "global-metadata-conflicts.json").read_text())
    assert conflicts["conflict_cluster_count"] == 1
    assert conflicts["clusters"][0]["canonical_stock_clip_id"] in {source_id, dup_id}
    assert len(
        catalog.review_global_dedupe_removed_ids(pipeline_run["result"].stockify_run_id)
    ) == 1


def test_near_longer_clip_keeper_rule(pipeline_run):
    root = pipeline_run["tmp_path"] / "global-longer"
    corpus = _prepare_global_input(pipeline_run, root)
    source_id, near_id = _add_cross_shard_near(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_b"],
        corpus["manifest_b"],
        "LONG",
        segment_index=9212,
    )
    # source_id is longer (10s); near_id is 9.7s. Longer should win.
    report, catalog, _ = _run_global(
        pipeline_run,
        corpus["input_root"],
        root / "canonical",
        near_policy="aggressive",
    )
    assert report.clusters[0].canonical_stock_clip_id == source_id
    assert near_id in catalog.review_global_dedupe_removed_ids(
        pipeline_run["result"].stockify_run_id
    )


def test_deterministic_tie_breaking(pipeline_run):
    root = pipeline_run["tmp_path"] / "global-tie"
    corpus = _prepare_global_input(pipeline_run, root)
    source_id, dup_id, _ = _add_cross_shard_exact(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_b"],
        corpus["manifest_b"],
        "TIE",
        segment_index=9213,
    )
    report1, _, _ = _run_global(
        pipeline_run, corpus["input_root"], root / "canonical-1"
    )
    report2, _, _ = _run_global(
        pipeline_run, corpus["input_root"], root / "canonical-2", overwrite=True
    )
    assert (
        report1.clusters[0].canonical_stock_clip_id
        == report2.clusters[0].canonical_stock_clip_id
        == source_id
    )
    assert dup_id in report1.clusters[0].removed_stock_clip_ids


def test_xml_and_manifest_rewrite_across_shards(pipeline_run):
    root = pipeline_run["tmp_path"] / "global-rewrite"
    corpus = _prepare_global_input(pipeline_run, root, shard_count=3)
    _, dup_b, name_b = _add_cross_shard_exact(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_b"],
        corpus["manifest_b"],
        "WRB",
        segment_index=9214,
    )
    _, dup_c, name_c = _add_cross_shard_exact(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_c"],
        corpus["manifest_c"],
        "WRC",
        segment_index=9215,
    )
    output_root = root / "canonical"
    report, _, _ = _run_global(pipeline_run, corpus["input_root"], output_root)
    assert report.shards_changed >= 2
    for market, name, dup_id in (
        ("market-b", name_b, dup_b),
        ("market-c", name_c, dup_c),
    ):
        out_xml = output_root / market / corpus[f"shard_{market[-1]}"].name
        assert validate_fcpxml(ET.parse(out_xml).getroot()).passed
        names = {p.get("name") for p in _individual_projects(ET.parse(out_xml).getroot())}
        assert name not in names
        cleaned = json.loads(
            out_xml.with_name(f"{out_xml.stem}-shard-manifest.json").read_text()
        )
        assert dup_id not in cleaned["stock_clip_ids"]
        assert "global_dedupe" in cleaned


def test_db_provenance_and_reconcile_out_of_scope(pipeline_run):
    root = pipeline_run["tmp_path"] / "global-recon"
    corpus = _prepare_global_input(pipeline_run, root)
    source_id, dup_id, _ = _add_cross_shard_exact(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_b"],
        corpus["manifest_b"],
        "RECON",
        segment_index=9216,
    )
    output_root = root / "canonical"
    report, catalog, _ = _run_global(pipeline_run, corpus["input_root"], output_root)
    removed = catalog.review_global_dedupe_removed_ids(
        pipeline_run["result"].stockify_run_id
    )
    assert removed
    assert removed <= {source_id, dup_id}
    with pipeline_run["database"].connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) AS n FROM stock_candidates WHERE stock_clip_id=?",
                (dup_id,),
            ).fetchone()["n"]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) AS n FROM review_global_dedupe_removals"
            ).fetchone()["n"]
            == 1
        )

    removed_id = next(iter(removed))
    reviewed = output_root / "market-a" / corpus["shard_a"].name
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
    assert removed_id not in rejected
    with pipeline_run["database"].connect() as connection:
        status = connection.execute(
            "SELECT review_status FROM stock_candidates WHERE stock_clip_id=?",
            (removed_id,),
        ).fetchone()["review_status"]
    assert status == "pending"


def test_idempotent_rerun(pipeline_run):
    root = pipeline_run["tmp_path"] / "global-idem"
    corpus = _prepare_global_input(pipeline_run, root)
    _add_cross_shard_exact(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_b"],
        corpus["manifest_b"],
        "IDEM",
        segment_index=9217,
    )
    output_root = root / "canonical"
    _run_global(pipeline_run, corpus["input_root"], output_root)
    _run_global(pipeline_run, corpus["input_root"], output_root, overwrite=True)
    with pipeline_run["database"].connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) AS n FROM review_global_dedupe_removals"
            ).fetchone()["n"]
            == 1
        )


def test_dry_run_no_writes_and_hypothetical_audit(pipeline_run):
    root = pipeline_run["tmp_path"] / "global-dry"
    corpus = _prepare_global_input(pipeline_run, root)
    _add_cross_shard_exact(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_b"],
        corpus["manifest_b"],
        "DRY",
        segment_index=9218,
    )
    output_root = root / "canonical"
    report, catalog, reports = _run_global(
        pipeline_run,
        corpus["input_root"],
        output_root,
        dry_run=True,
    )
    assert report.dry_run is True
    assert report.projects_removed == 1
    assert not output_root.exists() or not any(output_root.rglob("*.fcpxml"))
    assert catalog.review_global_dedupe_removed_ids(
        pipeline_run["result"].stockify_run_id
    ) == set()
    assert report.post_write_audit["remaining_exact_global_pairs"] == 0
    assert (reports / "global-dedupe.json").is_file()


def test_post_write_aggressive_audit_zero_pairs(pipeline_run):
    root = pipeline_run["tmp_path"] / "global-audit"
    corpus = _prepare_global_input(pipeline_run, root, shard_count=3)
    source_id, dup_b, _ = _add_cross_shard_exact(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_b"],
        corpus["manifest_b"],
        "AUDX",
        segment_index=9219,
    )
    _, near_c = _add_cross_shard_near(
        pipeline_run,
        corpus["shard_a"],
        corpus["shard_c"],
        corpus["manifest_c"],
        "AUDN",
        segment_index=9220,
    )
    with pipeline_run["database"].transaction() as connection:
        for clip_id in (source_id, dup_b):
            connection.execute(
                """
                UPDATE stock_candidates
                SET proposed_start='0s', proposed_duration='10s',
                    proposed_duration_seconds=10,
                    original_start='0s', original_duration='10s'
                WHERE stock_clip_id=?
                """,
                (clip_id,),
            )
        connection.execute(
            """
            UPDATE stock_candidates
            SET proposed_start='1/5s', proposed_duration='97/10s',
                proposed_duration_seconds=9.7,
                original_start='1/5s', original_duration='97/10s'
            WHERE stock_clip_id=?
            """,
            (near_c,),
        )
    report, _, _ = _run_global(
        pipeline_run,
        corpus["input_root"],
        root / "canonical",
        near_policy="aggressive",
    )
    assert report.shards_failed == 0
    assert report.projects_removed >= 2
    assert report.post_write_audit["remaining_exact_global_pairs"] == 0
    assert report.post_write_audit["remaining_aggressive_near_pairs"] == 0
