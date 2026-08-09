from __future__ import annotations

from pathlib import Path

from vclip_pipeline.reconcile import ReconcileService
from vclip_pipeline.workflow.catalog import WorkflowCatalog
from vclip_pipeline.workflow.collections import CollectionService
from vclip_pipeline.workflow.export_ingest import ExportIngestService
from vclip_pipeline.workflow.models import ProviderUsage, VisualAnalysis, VisualTag


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
    road_results = catalog.search("roads", limit=10, explain=True)
    assert [row["stock_clip_id"] for row in road_results] == [target["stock_clip_id"]]
    assert road_results[0]["search_score"] >= 40.0
    assert any(
        item["kind"] == "exact_primary_tag" and item["label"] == "road"
        for item in road_results[0]["search_explain"]["contributions"]
    )

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


def test_visual_analysis_persists_provider_usage(pipeline_run):
    catalog, target, stored, _exported = _ingest_one(pipeline_run)
    usage = ProviderUsage(
        provider="openai",
        model="gpt-5-mini",
        input_tokens=1200,
        cached_input_tokens=300,
        output_tokens=450,
        reasoning_tokens=100,
        total_tokens=1650,
        estimated_input_cost_usd=0.0003,
        estimated_output_cost_usd=0.0009,
        estimated_total_cost_usd=0.0012,
    )
    catalog.upsert_visual_analysis(
        analysis_key="ANALYSIS_USAGE",
        analysis_run_id=catalog.start_visual_run(
            provider="openai",
            model="gpt-5-mini",
            taxonomy_version=1,
            prompt_version="visual-taxonomy-v2",
            sampler_version="test",
            config={},
        ),
        stockify_run_id=pipeline_run["result"].stockify_run_id,
        stock_clip_id=target["stock_clip_id"],
        export_id=stored["id"],
        export_sha256=stored["sha256"],
        provider="openai",
        model="gpt-5-mini",
        taxonomy_version=1,
        analysis=VisualAnalysis(
            caption="A waterfront road.",
            tags=(VisualTag("subject", "road", "primary", 0.9),),
        ),
        evidence={},
        usage=usage,
    )
    with catalog.database.connect() as connection:
        row = connection.execute(
            """
            SELECT provider, model, input_tokens, cached_input_tokens, output_tokens,
                   reasoning_tokens, total_tokens, estimated_input_cost_usd,
                   estimated_output_cost_usd, estimated_total_cost_usd, result_json
            FROM clip_visual_analysis
            WHERE analysis_key=?
            """,
            ("ANALYSIS_USAGE",),
        ).fetchone()
    assert row["provider"] == "openai"
    assert row["model"] == "gpt-5-mini"
    assert row["input_tokens"] == 1200
    assert row["cached_input_tokens"] == 300
    assert row["output_tokens"] == 450
    assert row["reasoning_tokens"] == 100
    assert row["total_tokens"] == 1650
    assert abs(row["estimated_total_cost_usd"] - 0.0012) < 1e-12
    payload = __import__("json").loads(row["result_json"])
    assert payload["usage"]["cached_input_tokens"] == 300
