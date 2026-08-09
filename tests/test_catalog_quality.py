from __future__ import annotations

from pathlib import Path

from vclip_pipeline.workflow.catalog_quality import (
    CatalogQualityService,
    is_generic_metadata,
)
from vclip_pipeline.workflow.enrichment import portable_frame_src
from vclip_pipeline.workflow.entities import EntityCatalog
from vclip_pipeline.workflow.models import NamedSubject, VisualAnalysis, VisualTag
from vclip_pipeline.workflow.taxonomy import VisualTaxonomy
from test_workflow_catalog import _ingest_one


def test_exact_alias_canonicalization():
    catalog = EntityCatalog.default()
    for raw in (
        "Salesforce Tower",
        "Salesforce Tower (San Francisco)",
    ):
        resolved = catalog.resolve_raw_name(raw, confidence="likely")
        assert resolved.canonical_entity_id == "ENTITY_SALESFORCE_TOWER"
        assert resolved.canonical_label == "Salesforce Tower"
        assert resolved.resolution_source == "alias_catalog"
        assert resolved.verified is False
        assert resolved.raw_name == raw

    for raw in (
        "San Francisco Ferry Building",
        "San Francisco Ferry Building (clock tower)",
        "San Francisco Embarcadero / Ferry Building",
    ):
        resolved = catalog.resolve_raw_name(raw)
        assert resolved.canonical_entity_id == "ENTITY_FERRY_BUILDING"
        assert resolved.canonical_label == "Ferry Building"


def test_unresolved_unknown_named_subject():
    catalog = EntityCatalog.default()
    resolved = catalog.resolve_raw_name("Downtown San Francisco skyline")
    assert resolved.canonical_entity_id is None
    assert resolved.canonical_label is None
    assert resolved.resolution_source is None
    assert resolved.raw_name == "Downtown San Francisco skyline"


def test_no_false_merge_of_different_landmarks():
    catalog = EntityCatalog.default()
    tower = catalog.resolve_raw_name("Salesforce Tower")
    ferry = catalog.resolve_raw_name("San Francisco Ferry Building")
    skyline = catalog.resolve_raw_name("San Francisco skyline")
    assert tower.canonical_entity_id != ferry.canonical_entity_id
    assert skyline.canonical_entity_id is None
    bay = catalog.resolve_raw_name("Bay Bridge")
    golden = catalog.resolve_raw_name("Golden Gate Bridge")
    assert bay.canonical_entity_id == "ENTITY_BAY_BRIDGE"
    assert golden.canonical_entity_id == "ENTITY_GOLDEN_GATE_BRIDGE"
    assert bay.canonical_entity_id != golden.canonical_entity_id


def test_named_subject_schema_migration_adds_canonical_columns(tmp_path: Path):
    from vclip_pipeline.db import Database
    from vclip_pipeline.workflow.catalog import WorkflowCatalog

    database = Database(tmp_path / "migrate.sqlite3")
    database.migrate()
    with database.transaction() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS workflow_schema (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE clip_named_subjects (
                id TEXT PRIMARY KEY,
                stockify_run_id TEXT NOT NULL,
                stock_clip_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                source TEXT NOT NULL,
                confidence TEXT,
                verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(stockify_run_id, stock_clip_id, subject, source)
            );
            INSERT INTO clip_named_subjects(
                id, stockify_run_id, stock_clip_id, subject, source,
                confidence, verified, created_at, updated_at
            ) VALUES (
                'S1', 'R', 'C', 'Salesforce Tower', 'vision',
                'likely', 0, 't', 't'
            );
            """
        )
    catalog = WorkflowCatalog(database)
    with catalog.database.connect() as connection:
        cols = {
            row[1] for row in connection.execute("PRAGMA table_info(clip_named_subjects)")
        }
        row = connection.execute(
            "SELECT raw_name, canonical_entity_id FROM clip_named_subjects WHERE id='S1'"
        ).fetchone()
    assert "raw_name" in cols
    assert "canonical_entity_id" in cols
    assert "canonical_label" in cols
    assert "resolution_source" in cols
    assert row["raw_name"] == "Salesforce Tower"


def test_taxonomy_version_bumped_for_coastal_semantics():
    taxonomy = VisualTaxonomy.from_path(
        Path(__file__).parents[1]
        / "src"
        / "vclip_pipeline"
        / "data"
        / "visual_taxonomy.json"
    )
    assert taxonomy.version == 2
    coastal = taxonomy.groups["scene"]["coastal"]["description"].casefold()
    waterfront = taxonomy.groups["subject"]["waterfront"]["description"].casefold()
    assert "ocean" in coastal
    assert "beach" in coastal
    assert "do not use" in coastal
    assert "bay" in coastal
    assert "marina" in waterfront or "bay" in waterfront


def test_generic_metadata_detection():
    assert is_generic_metadata(
        [
            {"tag": "city_urban", "tag_group": "scene"},
            {"tag": "establishing", "tag_group": "use"},
            {"tag": "golden_hour", "tag_group": "style"},
            {"tag": "clear_skies", "tag_group": "style"},
        ],
        [],
    )
    assert not is_generic_metadata(
        [
            {"tag": "city_urban", "tag_group": "scene"},
            {"tag": "waterfront", "tag_group": "subject"},
            {"tag": "golden_hour", "tag_group": "style"},
        ],
        [],
    )
    assert not is_generic_metadata(
        [{"tag": "city_urban", "tag_group": "scene"}],
        [{"subject": "Salesforce Tower"}],
    )


def test_html_host_portable_frame_paths(tmp_path: Path):
    html_path = tmp_path / "reports" / "review.html"
    html_path.parent.mkdir(parents=True)
    frame = tmp_path / "vclip-frame-cache" / "FRAMES_ABC" / "frame-01.jpg"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"jpeg")
    src = portable_frame_src(html_path, frame)
    assert not src.startswith("file:")
    assert "/work/" not in src
    assert src == "../vclip-frame-cache/FRAMES_ABC/frame-01.jpg"
    assert (html_path.parent / src).resolve() == frame.resolve()


def test_quality_report_counts_and_canonicalize_backfill(pipeline_run):
    catalog, target, stored, _exported = _ingest_one(pipeline_run)
    run_id = pipeline_run["result"].stockify_run_id
    clip_id = target["stock_clip_id"]

    catalog.upsert_visual_analysis(
        analysis_key="KEY_GENERIC",
        analysis_run_id=catalog.start_visual_run(
            provider="openai",
            model="gpt-5-mini",
            taxonomy_version=2,
            prompt_version="visual-taxonomy-v3",
            sampler_version="uniform-six-v1",
            config={},
        ),
        stockify_run_id=run_id,
        stock_clip_id=clip_id,
        export_id=stored["id"],
        export_sha256=stored["sha256"],
        provider="openai",
        model="gpt-5-mini",
        taxonomy_version=2,
        analysis=VisualAnalysis(
            caption="Golden-hour aerial over Mission Bay towers.",
            tags=(
                VisualTag("scene", "city_urban", "secondary", 0.7),
                VisualTag("use", "establishing", "primary", 0.8),
                VisualTag("style", "golden_hour", "primary", 0.9),
                VisualTag("style", "clear_skies", "context", 0.6),
            ),
            named_subjects=(
                NamedSubject(
                    name="Salesforce Tower (San Francisco)",
                    confidence="likely",
                ),
                NamedSubject(
                    name="San Francisco Ferry Building (clock tower)",
                    confidence="possible",
                ),
            ),
        ),
        evidence={},
    )

    quality = CatalogQualityService(catalog)
    before = quality.audit(run_id=run_id)
    assert before.total_enriched_clips >= 1
    assert before.clips_with_named_subject_suggestions >= 1
    assert before.clips_with_canonical_named_subjects == 0
    assert before.clips_with_unresolved_named_subjects >= 1
    assert before.average_tags_per_clip >= 4
    # Broad tags alone would be generic, but named-subject suggestions count.
    assert is_generic_metadata(
        [
            {"tag": "city_urban", "tag_group": "scene"},
            {"tag": "establishing", "tag_group": "use"},
            {"tag": "golden_hour", "tag_group": "style"},
            {"tag": "clear_skies", "tag_group": "style"},
        ],
        [],
    )

    result = quality.canonicalize_entities(run_id=run_id, dry_run=False)
    assert result["subjects_resolved"] >= 2

    after = quality.audit(run_id=run_id)
    assert after.clips_with_canonical_named_subjects >= 1
    assert after.canonical_entity_frequency.get("Salesforce Tower") == 1
    assert after.canonical_entity_frequency.get("Ferry Building") == 1
    assert after.clips_with_unresolved_named_subjects == 0
    assert after.clips_with_generic_metadata == []

    catalog.rebuild_search_index()
    document_hits = catalog.search("Salesforce Tower")
    assert any(row["stock_clip_id"] == clip_id for row in document_hits)
    caption_hits = catalog.search("Mission Bay")
    assert any(row["stock_clip_id"] == clip_id for row in caption_hits)


def test_search_index_includes_caption_tags_entities(pipeline_run):
    catalog, target, stored, _exported = _ingest_one(pipeline_run)
    run_id = pipeline_run["result"].stockify_run_id
    clip_id = target["stock_clip_id"]
    subject = EntityCatalog.default().canonicalize_subject(
        NamedSubject(name="San Francisco Ferry Building")
    )
    assert subject.canonical_entity_id == "ENTITY_FERRY_BUILDING"
    catalog.upsert_visual_analysis(
        analysis_key="KEY_SEARCH",
        analysis_run_id="RUNVIS",
        stockify_run_id=run_id,
        stock_clip_id=clip_id,
        export_id=stored["id"],
        export_sha256=stored["sha256"],
        provider="openai",
        model="gpt-5-mini",
        taxonomy_version=2,
        analysis=VisualAnalysis(
            caption="Warm light on the Embarcadero waterfront.",
            tags=(VisualTag("subject", "waterfront", "primary", 0.9),),
            named_subjects=(subject,),
        ),
        evidence={},
    )
    catalog.rebuild_search_index()
    with catalog.database.connect() as connection:
        document = connection.execute(
            """
            SELECT document_text FROM clip_search_documents
            WHERE stock_clip_id=?
            """,
            (clip_id,),
        ).fetchone()["document_text"]
    assert "Warm light on the Embarcadero waterfront." in document
    assert "waterfront" in document
    assert "Ferry Building" in document
    assert "San Francisco Ferry Building" in document
