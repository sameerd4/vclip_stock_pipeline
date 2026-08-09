from __future__ import annotations

from pathlib import Path

from vclip_pipeline.reconcile import ReconcileService
from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.collections import CollectionService
from vclip_pipeline.workflow.export_ingest import ExportIngestService
from vclip_pipeline.workflow.models import VisualAnalysis, VisualTag


def _ingest_one(pipeline_run):
    ReconcileService(pipeline_run["repository"]).run(
        reviewed_xml=pipeline_run["output"],
        run_id=pipeline_run["result"].stockify_run_id,
        authority="auto",
        scope="full-run",
        report_path=None,
        allow_conflicts=False,
    )
    candidates = pipeline_run["repository"].candidates_for_run(
        pipeline_run["result"].stockify_run_id,
        accepted_only=True,
        approved_only=True,
    )
    exports = pipeline_run["tmp_path"] / "catalog-exports"
    exports.mkdir()
    target = candidates[0]
    exported = exports / f"{target['expected_export_basename']}.mp4"
    exported.write_bytes(b"catalog-test")
    catalog = WorkflowCatalog(pipeline_run["database"])
    ExportIngestService(pipeline_run["repository"], catalog).run(
        exports_directory=exports,
        run_id=pipeline_run["result"].stockify_run_id,
        calculate_checksums=True,
        inspect_media=False,
        allow_missing=True,
        dry_run=False,
        report_path=None,
    )
    stored = pipeline_run["repository"].export_for_candidate(
        pipeline_run["result"].stockify_run_id,
        target["stock_clip_id"],
    )
    return catalog, target, stored, exported


def test_visual_tags_are_searchable_and_collection_is_snapshot(pipeline_run):
    catalog, target, stored, exported = _ingest_one(pipeline_run)
    catalog.upsert_market(
        run_id=pipeline_run["result"].stockify_run_id,
        clip_id=target["stock_clip_id"],
        market_id="seattle",
        market_label="Seattle",
    )
    catalog.upsert_visual_analysis(
        analysis_key="ANALYSIS_TEST",
        analysis_run_id=catalog.start_visual_run(
            provider="test",
            model="test",
            taxonomy_version=1,
            prompt_version="test",
            sampler_version="test",
            config={},
        ),
        stockify_run_id=pipeline_run["result"].stockify_run_id,
        stock_clip_id=target["stock_clip_id"],
        export_id=stored["id"],
        export_sha256=stored["sha256"],
        provider="test",
        model="test",
        taxonomy_version=1,
        analysis=VisualAnalysis(
            caption="Aerial view following a waterfront road in Seattle.",
            tags=(
                VisualTag("subject", "road", "primary", 0.95),
                VisualTag("subject", "waterfront", "secondary", 0.85),
            ),
        ),
        evidence={},
    )
    catalog.rebuild_search_index()
    results = catalog.search("waterfront", limit=10)
    assert [row["stock_clip_id"] for row in results] == [target["stock_clip_id"]]

    service = CollectionService(catalog)
    suggestion = service.suggest(
        title="Seattle Waterfront Roads",
        slug=None,
        description="A small road and waterfront set.",
        rule={
            "markets": ["seattle"],
            "required_tags": ["road"],
            "preferred_tags": ["waterfront"],
            "minimum_clips": 1,
            "maximum_clips": 8,
            "maximum_per_source_media": 2,
        },
    )
    published = service.publish(suggestion)
    output = pipeline_run["tmp_path"] / "collections"
    result = service.materialize(
        slug=published["slug"],
        output_directory=output,
        mode="copy",
        overwrite=False,
    )
    assert result["clip_count"] == 1
    materialized = Path(result["output_directory"])
    assert (materialized / "manifest.json").is_file()
    assert len(list((materialized / "clips").glob("*.mp4"))) == 1
    assert exported.is_file()
