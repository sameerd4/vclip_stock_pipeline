from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.review_dedupe import near_source_range_duplicate
from vclip_pipeline.workflow.review_dedupe_batch import ReviewDedupeBatchService
from vclip_pipeline.workflow.review_shard import ReviewShardService

from test_review_dedupe import (
    _clone_db_candidate,
    _inject_duplicate_project,
    _individual_projects,
    _markets_path,
    _project_clip_id,
)


def _prepare_corpus(pipeline_run, root: Path) -> dict[str, Path]:
    """Build portable + manifest roots with two shard directories."""
    manifest_root = root / "review-shards"
    portable_root = root / "review-shards-portable"
    shard_a_dir = manifest_root / "market-a"
    shard_b_dir = manifest_root / "market-b"
    shard_a_dir.mkdir(parents=True)
    shard_b_dir.mkdir(parents=True)

    # Two shards from the same stockify review XML (split by max_projects).
    ReviewShardService(pipeline_run["repository"]).run(
        review_xml=pipeline_run["output"],
        output_directory=shard_a_dir,
        markets_path=_markets_path(),
        group_by="none",
        representation="individual",
        max_projects=3,
        max_megabytes=None,
        include_scope_markers=True,
        include_compilations=False,
        overwrite=True,
        dry_run=False,
        report_path=None,
    )
    # Copy first shard into market-a, move remaining into market-b for multi-dir.
    shards = sorted(shard_a_dir.glob("*.fcpxml"))
    assert len(shards) >= 2
    first = shards[0]
    rest = shards[1:]
    for path in rest:
        target = shard_b_dir / path.name
        path.replace(target)
        manifest = path.with_name(f"{path.stem}-shard-manifest.json")
        if manifest.exists():
            manifest.replace(shard_b_dir / manifest.name)

    # Portable copies (simulating repaired media refs via a marker comment file).
    for src_dir, rel in ((shard_a_dir, "market-a"), (shard_b_dir, "market-b")):
        for xml in src_dir.glob("*.fcpxml"):
            dest_dir = portable_root / rel
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / xml.name
            # Portable XML only — manifests remain under manifest-root.
            shutil.copy2(xml, dest)

    def _pair(dir_name: str) -> tuple[Path, Path]:
        xml = sorted((portable_root / dir_name).glob("*.fcpxml"))[0]
        manifest = (manifest_root / dir_name / f"{xml.stem}-shard-manifest.json")
        assert manifest.is_file(), manifest
        return xml, manifest

    shard_a, manifest_a = _pair("market-a")
    shard_b, manifest_b = _pair("market-b")
    return {
        "manifest_root": manifest_root,
        "portable_root": portable_root,
        "shard_a": shard_a,
        "shard_b": shard_b,
        "manifest_a": manifest_a,
        "manifest_b": manifest_b,
    }


def _add_exact_duplicate(
    pipeline_run,
    xml_path: Path,
    manifest_path: Path,
    suffix: str,
    *,
    segment_index: int,
) -> str:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    source = _individual_projects(root)[0]
    source_id = _project_clip_id(source)
    dup_name = f"{source.get('name')} — {suffix}"
    dup_id = f"{source_id}_{suffix}"
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=source_id,
        new_clip_id=dup_id,
        new_project_name=dup_name,
        short_clip_recovery="not_applicable",
        segment_index=segment_index,
    )
    _inject_duplicate_project(
        root, source_project=source, new_name=dup_name, new_clip_id=dup_id
    )
    tree.write(xml_path, encoding="utf-8", xml_declaration=True)
    # Keep authoritative manifest in sync for scoping.
    manifest = json.loads(manifest_path.read_text())
    manifest["stock_clip_ids"] = list(
        dict.fromkeys([*manifest.get("stock_clip_ids", []), dup_id])
    )
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return dup_id


def _add_near_duplicate(
    pipeline_run,
    xml_path: Path,
    manifest_path: Path,
    suffix: str,
    *,
    segment_index: int,
) -> str:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    source = _individual_projects(root)[0]
    source_id = _project_clip_id(source)
    near_name = f"{source.get('name')} — {suffix}"
    near_id = f"{source_id}_{suffix}"
    _clone_db_candidate(
        pipeline_run["database"],
        source_clip_id=source_id,
        new_clip_id=near_id,
        new_project_name=near_name,
        short_clip_recovery="not_applicable",
        segment_index=segment_index,
    )
    # Ultra-near but not exact: 0s-10s vs 1/5s-97/10s (containment/IoU above thresholds).
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
    _inject_duplicate_project(
        root, source_project=source, new_name=near_name, new_clip_id=near_id
    )
    tree.write(xml_path, encoding="utf-8", xml_declaration=True)
    manifest = json.loads(manifest_path.read_text())
    manifest["stock_clip_ids"] = list(
        dict.fromkeys([*manifest.get("stock_clip_ids", []), near_id])
    )
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return near_id


def test_batch_portable_manifest_pairing_and_mirroring(pipeline_run):
    root = pipeline_run["tmp_path"] / "batch-corpus"
    corpus = _prepare_corpus(pipeline_run, root)
    dup_a = _add_exact_duplicate(
        pipeline_run,
        corpus["shard_a"],
        corpus["manifest_a"],
        "BATCHA",
        segment_index=9101,
    )
    _add_exact_duplicate(
        pipeline_run,
        corpus["shard_b"],
        corpus["manifest_b"],
        "BATCHB",
        segment_index=9102,
    )

    output_root = root / "review-shards-clean"
    report_path = root / "duplicate-reports" / "bulk-dedupe.json"
    text_path = root / "duplicate-reports" / "bulk-dedupe.txt"
    catalog = WorkflowCatalog(pipeline_run["database"])
    report = ReviewDedupeBatchService(pipeline_run["repository"], catalog).run(
        input_root=corpus["portable_root"],
        manifest_root=corpus["manifest_root"],
        output_root=output_root,
        report_path=report_path,
        text_report_path=text_path,
        overwrite=True,
    )

    assert report.shards_discovered >= 2
    assert report.shards_processed >= 2
    assert report.shards_failed == 0, report.failures
    assert report.projects_removed >= 2
    assert report.changed_shards >= 2
    assert {Path(s.input_xml).parent.name for s in report.shards} >= {"market-a", "market-b"}
    assert report.projects_after == report.projects_before - report.projects_removed
    assert report.percentage_reduction > 0

    # Output mirrors relative structure and uses portable inputs.
    out_a = output_root / "market-a" / corpus["shard_a"].name
    out_b = output_root / "market-b" / corpus["shard_b"].name
    assert out_a.is_file()
    assert out_b.is_file()
    assert corpus["shard_a"].read_bytes() != out_a.read_bytes()

    # Authoritative manifests were not mutated; cleaned manifests live next to output.
    original_manifest = json.loads(corpus["manifest_a"].read_text())
    assert dup_a in original_manifest["stock_clip_ids"]
    cleaned_manifest = json.loads(
        out_a.with_name(f"{out_a.stem}-shard-manifest.json").read_text()
    )
    assert dup_a not in cleaned_manifest["stock_clip_ids"]

    # Compilation wrappers preserved.
    names = {
        project.get("name", "")
        for project in ET.parse(out_a).getroot().findall("./library/event/project")
    }
    assert any("Stock Compilation" in name for name in names)

    # DB provenance recorded without deleting candidates.
    removed_ids = catalog.review_dedupe_removed_ids(
        pipeline_run["result"].stockify_run_id
    )
    assert dup_a in removed_ids
    with pipeline_run["database"].connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) AS n FROM stock_candidates WHERE stock_clip_id=?",
                (dup_a,),
            ).fetchone()["n"]
            == 1
        )

    payload = json.loads(report_path.read_text())
    assert payload["projects_removed"] == report.projects_removed
    assert text_path.is_file()


def test_batch_unchanged_shard_is_copied(pipeline_run):
    root = pipeline_run["tmp_path"] / "batch-unchanged"
    corpus = _prepare_corpus(pipeline_run, root)
    # Only mutate shard A; shard B remains unchanged.
    _add_exact_duplicate(
        pipeline_run,
        corpus["shard_a"],
        corpus["manifest_a"],
        "ONLYA",
        segment_index=9103,
    )
    output_root = root / "clean"
    report = ReviewDedupeBatchService(
        pipeline_run["repository"], WorkflowCatalog(pipeline_run["database"])
    ).run(
        input_root=corpus["portable_root"],
        manifest_root=corpus["manifest_root"],
        output_root=output_root,
        report_path=root / "bulk.json",
        text_report_path=root / "bulk.txt",
        overwrite=True,
    )
    assert report.unchanged_shards >= 1
    assert report.changed_shards >= 1
    out_b = output_root / "market-b" / corpus["shard_b"].name
    assert out_b.is_file()
    # Unchanged means zero removals for that shard, but output still written.
    unchanged = [s for s in report.shards if s.status == "unchanged"]
    assert unchanged
    assert all(item.projects_removed == 0 for item in unchanged)


def test_batch_idempotent_rerun(pipeline_run):
    root = pipeline_run["tmp_path"] / "batch-idem"
    corpus = _prepare_corpus(pipeline_run, root)
    dup_id = _add_exact_duplicate(
        pipeline_run,
        corpus["shard_a"],
        corpus["manifest_a"],
        "IDEM",
        segment_index=9104,
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
    )
    first = service.run(**kwargs)
    second = service.run(**kwargs)
    assert first.projects_removed == second.projects_removed
    assert first.projects_after == second.projects_after
    with pipeline_run["database"].connect() as connection:
        count = connection.execute(
            """
            SELECT COUNT(*) AS n FROM review_dedupe_removals
            WHERE removed_stock_clip_id=?
            """,
            (dup_id,),
        ).fetchone()["n"]
    assert count == 1


def test_batch_partial_failure_preserves_successes(pipeline_run):
    root = pipeline_run["tmp_path"] / "batch-fail"
    corpus = _prepare_corpus(pipeline_run, root)
    _add_exact_duplicate(
        pipeline_run,
        corpus["shard_a"],
        corpus["manifest_a"],
        "OKDUP",
        segment_index=9105,
    )
    # Corrupt shard B XML so processing fails after A succeeds.
    corpus["shard_b"].write_text("<not-valid-fcpxml>", encoding="utf-8")

    output_root = root / "clean"
    report = ReviewDedupeBatchService(
        pipeline_run["repository"], WorkflowCatalog(pipeline_run["database"])
    ).run(
        input_root=corpus["portable_root"],
        manifest_root=corpus["manifest_root"],
        output_root=output_root,
        report_path=root / "bulk.json",
        text_report_path=root / "bulk.txt",
        overwrite=True,
    )
    assert report.shards_failed >= 1
    assert report.shards_processed >= 1
    assert (output_root / "market-a" / corpus["shard_a"].name).is_file()
    assert not (output_root / "market-b" / corpus["shard_b"].name).exists()


def test_batch_post_dedupe_near_duplicate_audit(pipeline_run):
    root = pipeline_run["tmp_path"] / "batch-near"
    corpus = _prepare_corpus(pipeline_run, root)
    near_id = _add_near_duplicate(
        pipeline_run,
        corpus["shard_a"],
        corpus["manifest_a"],
        "ULTRANEAR",
        segment_index=9106,
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
    )
    audit = report.near_duplicate_audit
    assert audit["mode"] == "report_only"
    assert audit["thresholds"]["containment"] == 0.95
    assert audit["thresholds"]["iou"] == 0.92
    assert audit["near_duplicate_pairs"] >= 1
    assert any(
        near_id in (pair["left_stock_clip_id"], pair["right_stock_clip_id"])
        for pair in audit["pairs"]
    )
    out_a = root / "clean" / "market-a" / corpus["shard_a"].name
    out_ids = {
        _project_clip_id(project)
        for project in _individual_projects(ET.parse(out_a).getroot())
    }
    # Ultra-near remains in clean output (report-only; not auto-removed).
    assert near_id in out_ids


def test_near_helper_does_not_flag_exact_or_weak_overlap():
    from vclip_pipeline.workflow.review_dedupe import DedupeProject

    def project(clip_id: str, start: float, duration: float) -> DedupeProject:
        return DedupeProject(
            order=0,
            project_name=clip_id,
            project_uid=None,
            event_name="e",
            stock_clip_id=clip_id,
            stockify_run_id="R",
            source_project_id="P",
            representation="individual",
            media_identity="media:X",
            source_start_seconds=start,
            source_duration_seconds=duration,
            source_start=f"{start}s",
            source_duration=f"{duration}s",
            short_clip_recovery="not_applicable",
            element=ET.Element("project"),
        )

    exact_a = project("A", 0.0, 10.0)
    exact_b = project("B", 0.01, 10.0)
    assert near_source_range_duplicate(exact_a, exact_b) is False  # exact, not near

    near_a = project("A", 0.0, 10.0)
    near_b = project("B", 0.2, 9.7)
    assert near_source_range_duplicate(near_a, near_b) is True

    weak_a = project("A", 0.0, 10.0)
    weak_b = project("B", 5.0, 10.0)
    assert near_source_range_duplicate(weak_a, weak_b) is False
