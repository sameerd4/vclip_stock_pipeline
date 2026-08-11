"""Additive SQLite catalog tables for visual semantics and collections."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from ..db.connection import Database
from ..errors import VClipError
from ..util import json_dumps, json_loads, stable_id, utc_now
from .models import NamedSubject, ProviderUsage, VisualAnalysis, VisualTag
from .search import CatalogSearch
from .taxonomy import VisualTaxonomy


_WORKFLOW_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS workflow_schema (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS export_media_metadata (
    export_id TEXT PRIMARY KEY,
    width INTEGER,
    height INTEGER,
    codec_name TEXT,
    frame_rate REAL,
    probe_json TEXT NOT NULL DEFAULT '{}',
    probed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visual_analysis_runs (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    taxonomy_version INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    sampler_version TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','complete','failed')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_text TEXT
);

CREATE TABLE IF NOT EXISTS clip_visual_analysis (
    id TEXT PRIMARY KEY,
    analysis_key TEXT NOT NULL UNIQUE,
    analysis_run_id TEXT NOT NULL,
    stockify_run_id TEXT NOT NULL,
    stock_clip_id TEXT NOT NULL,
    export_id TEXT NOT NULL,
    export_sha256 TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    taxonomy_version INTEGER NOT NULL,
    caption TEXT NOT NULL,
    result_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('complete','failed','frames_only')),
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    total_tokens INTEGER,
    estimated_input_cost_usd REAL,
    estimated_output_cost_usd REAL,
    estimated_total_cost_usd REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_visual_clip
    ON clip_visual_analysis(stockify_run_id, stock_clip_id, updated_at);

CREATE TABLE IF NOT EXISTS clip_tags (
    id TEXT PRIMARY KEY,
    stockify_run_id TEXT NOT NULL,
    stock_clip_id TEXT NOT NULL,
    tag_group TEXT NOT NULL,
    tag TEXT NOT NULL,
    source TEXT NOT NULL,
    strength TEXT,
    score REAL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    human_override INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(stockify_run_id, stock_clip_id, tag_group, tag, source)
);

CREATE INDEX IF NOT EXISTS idx_clip_tags_lookup
    ON clip_tags(tag_group, tag, stockify_run_id, stock_clip_id);

CREATE TABLE IF NOT EXISTS clip_markets (
    id TEXT PRIMARY KEY,
    stockify_run_id TEXT NOT NULL,
    stock_clip_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    market_label TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(stockify_run_id, stock_clip_id, market_id, source)
);

CREATE INDEX IF NOT EXISTS idx_clip_markets_lookup
    ON clip_markets(market_id, stockify_run_id, stock_clip_id);

CREATE TABLE IF NOT EXISTS clip_named_subjects (
    id TEXT PRIMARY KEY,
    stockify_run_id TEXT NOT NULL,
    stock_clip_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    raw_name TEXT,
    canonical_entity_id TEXT,
    canonical_label TEXT,
    resolution_source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(stockify_run_id, stock_clip_id, subject, source)
);

CREATE TABLE IF NOT EXISTS clip_search_documents (
    stockify_run_id TEXT NOT NULL,
    stock_clip_id TEXT NOT NULL,
    document_text TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(stockify_run_id, stock_clip_id)
);

CREATE TABLE IF NOT EXISTS collection_definitions (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT,
    rule_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','active','archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_versions (
    id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','published','retired')),
    rule_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    UNIQUE(collection_id, version)
);

CREATE TABLE IF NOT EXISTS collection_version_clips (
    collection_version_id TEXT NOT NULL,
    stockify_run_id TEXT NOT NULL,
    stock_clip_id TEXT NOT NULL,
    export_id TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    score REAL,
    rationale_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(collection_version_id, stockify_run_id, stock_clip_id)
);

CREATE INDEX IF NOT EXISTS idx_collection_version_clips
    ON collection_version_clips(collection_version_id, sort_order);

CREATE TABLE IF NOT EXISTS review_dedupe_removals (
    id TEXT PRIMARY KEY,
    stockify_run_id TEXT NOT NULL,
    removed_stock_clip_id TEXT NOT NULL,
    kept_stock_clip_id TEXT NOT NULL,
    removed_project_name TEXT NOT NULL,
    kept_project_name TEXT NOT NULL,
    source_media TEXT,
    source_start TEXT,
    source_duration TEXT,
    reason TEXT NOT NULL,
    containment REAL,
    iou REAL,
    source_fcpxml TEXT,
    output_fcpxml TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(stockify_run_id, removed_stock_clip_id)
);

CREATE INDEX IF NOT EXISTS idx_review_dedupe_removals_run
    ON review_dedupe_removals(stockify_run_id, removed_stock_clip_id);

CREATE TABLE IF NOT EXISTS review_global_dedupe_removals (
    id TEXT PRIMARY KEY,
    stockify_run_id TEXT NOT NULL,
    removed_stock_clip_id TEXT NOT NULL,
    canonical_stock_clip_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    cluster_type TEXT NOT NULL,
    source_media TEXT,
    canonical_source_start TEXT,
    canonical_source_duration TEXT,
    reason TEXT NOT NULL,
    containment REAL,
    iou REAL,
    source_shard TEXT,
    removed_project_name TEXT,
    kept_project_name TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(stockify_run_id, removed_stock_clip_id)
);

CREATE INDEX IF NOT EXISTS idx_review_global_dedupe_removals_run
    ON review_global_dedupe_removals(stockify_run_id, removed_stock_clip_id);

CREATE TABLE IF NOT EXISTS review_short_prune_removals (
    id TEXT PRIMARY KEY,
    stockify_run_id TEXT NOT NULL,
    removed_stock_clip_id TEXT NOT NULL,
    removed_project_name TEXT,
    reason TEXT NOT NULL,
    effective_duration_seconds REAL NOT NULL,
    min_duration_seconds REAL NOT NULL,
    candidate_tier TEXT,
    short_clip_recovery TEXT,
    source_shard TEXT,
    input_xml TEXT,
    output_xml TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(stockify_run_id, removed_stock_clip_id)
);

CREATE INDEX IF NOT EXISTS idx_review_short_prune_removals_run
    ON review_short_prune_removals(stockify_run_id, removed_stock_clip_id);

CREATE TABLE IF NOT EXISTS review_location_recoveries (
    id TEXT PRIMARY KEY,
    stockify_run_id TEXT NOT NULL,
    stock_clip_id TEXT NOT NULL,
    original_event_name TEXT,
    new_event_name TEXT,
    original_project_name TEXT,
    new_project_name TEXT,
    source_media TEXT,
    srt_paths_json TEXT NOT NULL DEFAULT '[]',
    representative_lat REAL,
    representative_lon REAL,
    resolution_confidence TEXT NOT NULL,
    recovery_reason TEXT NOT NULL,
    source_shard TEXT,
    input_xml TEXT,
    output_xml TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(stockify_run_id, stock_clip_id)
);

CREATE INDEX IF NOT EXISTS idx_review_location_recoveries_run
    ON review_location_recoveries(stockify_run_id, stock_clip_id);

CREATE TABLE IF NOT EXISTS review_color_repairs (
    id TEXT PRIMARY KEY,
    stockify_run_id TEXT NOT NULL,
    stock_clip_id TEXT NOT NULL,
    source_media TEXT,
    camera_model TEXT,
    color_md TEXT,
    previous_camera_lut TEXT,
    new_camera_lut TEXT,
    previous_xml_lut_identity TEXT,
    new_xml_lut_identity TEXT,
    previous_effect_signature TEXT,
    new_effect_signature TEXT,
    repair_reason TEXT NOT NULL,
    source_shard TEXT,
    project_name TEXT,
    input_xml TEXT,
    output_xml TEXT,
    srt_path TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(stockify_run_id, stock_clip_id)
);

CREATE INDEX IF NOT EXISTS idx_review_color_repairs_run
    ON review_color_repairs(stockify_run_id, stock_clip_id);
"""


class WorkflowCatalog:
    """Persistence boundary for post-export enrichment and merchandising."""

    def __init__(
        self,
        database: Database,
        *,
        search: CatalogSearch | None = None,
    ) -> None:
        self.database = database
        self._fts_available = False
        self.search_engine = search or CatalogSearch()
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.database.transaction() as connection:
            connection.executescript(_WORKFLOW_SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO workflow_schema(version, applied_at) VALUES (1, ?)",
                (utc_now(),),
            )
            self._migrate_visual_usage_columns(connection)
            self._migrate_named_subject_canonical_columns(connection)
            self._migrate_review_dedupe_metric_columns(connection)
            try:
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS clip_search_fts USING fts5(
                        stockify_run_id UNINDEXED,
                        stock_clip_id UNINDEXED,
                        document_text
                    )
                    """
                )
                self._fts_available = True
            except sqlite3.OperationalError:
                self._fts_available = False

    @staticmethod
    def _migrate_visual_usage_columns(connection: sqlite3.Connection) -> None:
        """Add usage/cost columns to older clip_visual_analysis tables."""
        existing = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(clip_visual_analysis)")
        }
        columns = (
            ("input_tokens", "INTEGER"),
            ("cached_input_tokens", "INTEGER"),
            ("output_tokens", "INTEGER"),
            ("reasoning_tokens", "INTEGER"),
            ("total_tokens", "INTEGER"),
            ("estimated_input_cost_usd", "REAL"),
            ("estimated_output_cost_usd", "REAL"),
            ("estimated_total_cost_usd", "REAL"),
        )
        for name, sql_type in columns:
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE clip_visual_analysis ADD COLUMN {name} {sql_type}"
                )
        connection.execute(
            "INSERT OR IGNORE INTO workflow_schema(version, applied_at) VALUES (2, ?)",
            (utc_now(),),
        )

    @staticmethod
    def _migrate_named_subject_canonical_columns(connection: sqlite3.Connection) -> None:
        existing = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(clip_named_subjects)")
        }
        columns = (
            ("raw_name", "TEXT"),
            ("canonical_entity_id", "TEXT"),
            ("canonical_label", "TEXT"),
            ("resolution_source", "TEXT"),
        )
        for name, sql_type in columns:
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE clip_named_subjects ADD COLUMN {name} {sql_type}"
                )
        # Older rows stored the raw suggestion only in subject.
        connection.execute(
            """
            UPDATE clip_named_subjects
            SET raw_name=subject
            WHERE raw_name IS NULL OR trim(raw_name)=''
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_clip_named_subjects_entity
            ON clip_named_subjects(canonical_entity_id, stockify_run_id, stock_clip_id)
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO workflow_schema(version, applied_at) VALUES (3, ?)",
            (utc_now(),),
        )

    @staticmethod
    def _migrate_review_dedupe_metric_columns(connection: sqlite3.Connection) -> None:
        existing = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(review_dedupe_removals)")
        }
        if not existing:
            return
        for name, sql_type in (("containment", "REAL"), ("iou", "REAL")):
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE review_dedupe_removals ADD COLUMN {name} {sql_type}"
                )
        connection.execute(
            "INSERT OR IGNORE INTO workflow_schema(version, applied_at) VALUES (4, ?)",
            (utc_now(),),
        )

    def upsert_export_media(
        self,
        *,
        export_id: str,
        width: int | None,
        height: int | None,
        codec_name: str | None,
        frame_rate: float | None,
        probe: dict[str, Any],
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO export_media_metadata(
                    export_id, width, height, codec_name, frame_rate, probe_json, probed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(export_id) DO UPDATE SET
                    width=excluded.width,
                    height=excluded.height,
                    codec_name=excluded.codec_name,
                    frame_rate=excluded.frame_rate,
                    probe_json=excluded.probe_json,
                    probed_at=excluded.probed_at
                """,
                (
                    export_id,
                    width,
                    height,
                    codec_name,
                    frame_rate,
                    json_dumps(probe),
                    utc_now(),
                ),
            )

    def start_visual_run(
        self,
        *,
        provider: str,
        model: str,
        taxonomy_version: int,
        prompt_version: str,
        sampler_version: str,
        config: dict[str, Any],
    ) -> str:
        run_id = stable_id(
            "VISUALRUN",
            provider,
            model,
            taxonomy_version,
            prompt_version,
            sampler_version,
            utc_now(),
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO visual_analysis_runs(
                    id, provider, model, taxonomy_version, prompt_version,
                    sampler_version, config_json, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    run_id,
                    provider,
                    model,
                    taxonomy_version,
                    prompt_version,
                    sampler_version,
                    json_dumps(config),
                    utc_now(),
                ),
            )
        return run_id

    def finish_visual_run(self, run_id: str, *, error: str | None = None) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE visual_analysis_runs
                SET status=?, completed_at=?, error_text=?
                WHERE id=?
                """,
                ("failed" if error else "complete", utc_now(), error, run_id),
            )

    def has_analysis_key(self, analysis_key: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM clip_visual_analysis WHERE analysis_key=? AND status='complete'",
                (analysis_key,),
            ).fetchone()
        return row is not None

    def upsert_visual_analysis(
        self,
        *,
        analysis_key: str,
        analysis_run_id: str,
        stockify_run_id: str,
        stock_clip_id: str,
        export_id: str,
        export_sha256: str | None,
        provider: str,
        model: str,
        taxonomy_version: int,
        analysis: VisualAnalysis,
        evidence: dict[str, Any],
        usage: ProviderUsage | None = None,
        status: str = "complete",
    ) -> str:
        now = utc_now()
        analysis_id = stable_id("VISUAL", analysis_key)
        result = {
            "caption": analysis.caption,
            "tags": [
                {
                    "group": tag.group,
                    "tag": tag.tag,
                    "strength": tag.strength,
                    "score": tag.score,
                    "frame_hits": list(tag.frame_hits),
                    "rationale": tag.rationale,
                }
                for tag in analysis.tags
            ],
            "named_subjects": [
                {
                    "name": subject.name,
                    "raw_name": subject.raw_name,
                    "confidence": subject.confidence,
                    "verified": subject.verified,
                    "canonical_entity_id": subject.canonical_entity_id,
                    "canonical_label": subject.canonical_label,
                    "resolution_source": subject.resolution_source,
                }
                for subject in analysis.named_subjects
            ],
            "raw": analysis.raw,
            "usage": usage.as_dict() if usage is not None else None,
        }
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO clip_visual_analysis(
                    id, analysis_key, analysis_run_id, stockify_run_id,
                    stock_clip_id, export_id, export_sha256, provider, model,
                    taxonomy_version, caption, result_json, evidence_json,
                    status,
                    input_tokens, cached_input_tokens, output_tokens,
                    reasoning_tokens, total_tokens,
                    estimated_input_cost_usd, estimated_output_cost_usd,
                    estimated_total_cost_usd,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(analysis_key) DO UPDATE SET
                    analysis_run_id=excluded.analysis_run_id,
                    caption=excluded.caption,
                    result_json=excluded.result_json,
                    evidence_json=excluded.evidence_json,
                    status=excluded.status,
                    input_tokens=excluded.input_tokens,
                    cached_input_tokens=excluded.cached_input_tokens,
                    output_tokens=excluded.output_tokens,
                    reasoning_tokens=excluded.reasoning_tokens,
                    total_tokens=excluded.total_tokens,
                    estimated_input_cost_usd=excluded.estimated_input_cost_usd,
                    estimated_output_cost_usd=excluded.estimated_output_cost_usd,
                    estimated_total_cost_usd=excluded.estimated_total_cost_usd,
                    updated_at=excluded.updated_at
                """,
                (
                    analysis_id,
                    analysis_key,
                    analysis_run_id,
                    stockify_run_id,
                    stock_clip_id,
                    export_id,
                    export_sha256,
                    provider,
                    model,
                    taxonomy_version,
                    analysis.caption,
                    json_dumps(result),
                    json_dumps(evidence),
                    status,
                    None if usage is None else usage.input_tokens,
                    None if usage is None else usage.cached_input_tokens,
                    None if usage is None else usage.output_tokens,
                    None if usage is None else usage.reasoning_tokens,
                    None if usage is None else usage.total_tokens,
                    None if usage is None else usage.estimated_input_cost_usd,
                    None if usage is None else usage.estimated_output_cost_usd,
                    None if usage is None else usage.estimated_total_cost_usd,
                    now,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM clip_tags WHERE stockify_run_id=? AND stock_clip_id=? AND source='visual'",
                (stockify_run_id, stock_clip_id),
            )
            for tag in analysis.tags:
                self._upsert_tag(connection, stockify_run_id, stock_clip_id, tag, "visual")
            connection.execute(
                "DELETE FROM clip_named_subjects WHERE stockify_run_id=? AND stock_clip_id=? AND source='vision'",
                (stockify_run_id, stock_clip_id),
            )
            for subject in analysis.named_subjects:
                self._upsert_named_subject(
                    connection,
                    stockify_run_id,
                    stock_clip_id,
                    subject,
                    "vision",
                )
        return analysis_id

    def replace_named_subjects(
        self,
        *,
        stockify_run_id: str,
        stock_clip_id: str,
        subjects: list[NamedSubject],
        source: str = "vision",
    ) -> None:
        """Rewrite named subjects and patch result_json without touching usage/tags."""
        now = utc_now()
        named_payload = [
            {
                "name": subject.name,
                "raw_name": subject.raw_name,
                "confidence": subject.confidence,
                "verified": subject.verified,
                "canonical_entity_id": subject.canonical_entity_id,
                "canonical_label": subject.canonical_label,
                "resolution_source": subject.resolution_source,
            }
            for subject in subjects
        ]
        with self.database.transaction() as connection:
            connection.execute(
                """
                DELETE FROM clip_named_subjects
                WHERE stockify_run_id=? AND stock_clip_id=? AND source=?
                """,
                (stockify_run_id, stock_clip_id, source),
            )
            for subject in subjects:
                self._upsert_named_subject(
                    connection,
                    stockify_run_id,
                    stock_clip_id,
                    subject,
                    source,
                )
            row = connection.execute(
                """
                SELECT result_json
                FROM clip_visual_analysis
                WHERE stockify_run_id=? AND stock_clip_id=? AND status='complete'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (stockify_run_id, stock_clip_id),
            ).fetchone()
            if row is None:
                return
            payload = json_loads(row["result_json"], {})
            payload["named_subjects"] = named_payload
            connection.execute(
                """
                UPDATE clip_visual_analysis
                SET result_json=?, updated_at=?
                WHERE stockify_run_id=? AND stock_clip_id=? AND status='complete'
                """,
                (
                    json_dumps(payload),
                    now,
                    stockify_run_id,
                    stock_clip_id,
                ),
            )

    @staticmethod
    def _upsert_tag(
        connection: sqlite3.Connection,
        run_id: str,
        clip_id: str,
        tag: VisualTag,
        source: str,
    ) -> None:
        now = utc_now()
        tag_id = stable_id("TAG", run_id, clip_id, tag.group, tag.tag, source)
        connection.execute(
            """
            INSERT INTO clip_tags(
                id, stockify_run_id, stock_clip_id, tag_group, tag, source,
                strength, score, evidence_json, human_override, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(stockify_run_id, stock_clip_id, tag_group, tag, source)
            DO UPDATE SET
                strength=excluded.strength,
                score=excluded.score,
                evidence_json=excluded.evidence_json,
                updated_at=excluded.updated_at
            """,
            (
                tag_id,
                run_id,
                clip_id,
                tag.group,
                tag.tag,
                source,
                tag.strength,
                tag.score,
                json_dumps(
                    {
                        "frame_hits": list(tag.frame_hits),
                        "rationale": tag.rationale,
                    }
                ),
                now,
                now,
            ),
        )

    @staticmethod
    def _upsert_named_subject(
        connection: sqlite3.Connection,
        run_id: str,
        clip_id: str,
        subject: NamedSubject,
        source: str,
    ) -> None:
        now = utc_now()
        subject_id = stable_id("SUBJECT", run_id, clip_id, subject.name, source)
        connection.execute(
            """
            INSERT INTO clip_named_subjects(
                id, stockify_run_id, stock_clip_id, subject, source,
                confidence, verified, raw_name, canonical_entity_id,
                canonical_label, resolution_source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stockify_run_id, stock_clip_id, subject, source)
            DO UPDATE SET
                confidence=excluded.confidence,
                verified=excluded.verified,
                raw_name=excluded.raw_name,
                canonical_entity_id=excluded.canonical_entity_id,
                canonical_label=excluded.canonical_label,
                resolution_source=excluded.resolution_source,
                updated_at=excluded.updated_at
            """,
            (
                subject_id,
                run_id,
                clip_id,
                subject.name,
                source,
                subject.confidence,
                int(subject.verified),
                subject.raw_name,
                subject.canonical_entity_id,
                subject.canonical_label,
                subject.resolution_source,
                now,
                now,
            ),
        )

    def upsert_tag(
        self,
        *,
        run_id: str,
        clip_id: str,
        group: str,
        tag: str,
        source: str,
        strength: str = "primary",
        score: float | None = 1.0,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        tag_id = stable_id("TAG", run_id, clip_id, group, tag, source)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO clip_tags(
                    id, stockify_run_id, stock_clip_id, tag_group, tag, source,
                    strength, score, evidence_json, human_override, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(stockify_run_id, stock_clip_id, tag_group, tag, source)
                DO UPDATE SET
                    strength=excluded.strength,
                    score=excluded.score,
                    evidence_json=excluded.evidence_json,
                    updated_at=excluded.updated_at
                """,
                (
                    tag_id,
                    run_id,
                    clip_id,
                    group,
                    tag,
                    source,
                    strength,
                    score,
                    json_dumps(evidence or {}),
                    now,
                    now,
                ),
            )

    def upsert_market(
        self,
        *,
        run_id: str,
        clip_id: str,
        market_id: str,
        market_label: str,
        source: str = "location_rule",
        confidence: str = "high",
    ) -> None:
        now = utc_now()
        market_row_id = stable_id("MARKET", run_id, clip_id, market_id, source)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO clip_markets(
                    id, stockify_run_id, stock_clip_id, market_id, market_label,
                    source, confidence, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stockify_run_id, stock_clip_id, market_id, source)
                DO UPDATE SET
                    market_label=excluded.market_label,
                    confidence=excluded.confidence,
                    updated_at=excluded.updated_at
                """,
                (
                    market_row_id,
                    run_id,
                    clip_id,
                    market_id,
                    market_label,
                    source,
                    confidence,
                    now,
                    now,
                ),
            )

    def eligible_exports(
        self,
        *,
        run_id: str | None = None,
        include_pending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["c.eligibility_status='accepted'", "c.export_status='matched'"]
        parameters: list[Any] = []
        if not include_pending:
            conditions.append("c.review_status='approved'")
        if run_id:
            conditions.append("c.run_id=?")
            parameters.append(run_id)
        limit_sql = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            parameters.append(limit)
        query = f"""
            SELECT
                c.run_id AS stockify_run_id,
                c.stock_clip_id,
                c.source_media_id,
                c.session_id,
                c.source_name,
                c.source_project_id,
                c.generated_project_label,
                c.generated_clip_project_name,
                c.final_duration_seconds,
                c.proposed_duration_seconds,
                c.capture_time_json,
                c.time_of_day_json,
                c.location_json,
                e.id AS export_id,
                e.exported_filename,
                e.exported_path,
                e.sha256 AS export_sha256,
                e.duration_seconds AS export_duration_seconds,
                m.width,
                m.height,
                m.codec_name,
                m.frame_rate,
                s.public_label,
                s.city,
                s.neighborhood,
                s.state,
                s.country,
                s.time_of_day
            FROM stock_candidates c
            JOIN exports e
              ON e.stockify_run_id=c.run_id
             AND e.stock_clip_id=c.stock_clip_id
            LEFT JOIN export_media_metadata m ON m.export_id=e.id
            LEFT JOIN shoot_sessions s ON s.id=c.session_id
            WHERE {' AND '.join(conditions)}
            ORDER BY c.run_id, c.generated_event_name,
                     c.generated_project_label, c.clip_sequence
            {limit_sql}
        """
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for source, target in (
                ("capture_time_json", "capture_time"),
                ("time_of_day_json", "time_of_day_metadata"),
                ("location_json", "location"),
            ):
                item[target] = json_loads(item.pop(source, None), {})
            item["orientation"] = self._orientation(item.get("width"), item.get("height"))
            result.append(item)
        return result

    @staticmethod
    def _orientation(width: int | None, height: int | None) -> str | None:
        if not width or not height:
            return None
        if height > width:
            return "vertical"
        if width > height:
            return "landscape"
        return "square"

    def catalog_rows(
        self,
        *,
        run_id: str | None = None,
        markets: Iterable[str] = (),
        required_tags: Iterable[str] = (),
        orientation: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.eligible_exports(run_id=run_id)
        market_set = {value.casefold() for value in markets}
        tag_set = {value.casefold() for value in required_tags}
        result: list[dict[str, Any]] = []
        with self.database.connect() as connection:
            for row in rows:
                key = (row["stockify_run_id"], row["stock_clip_id"])
                market_rows = connection.execute(
                    """
                    SELECT market_id, market_label, source, confidence
                    FROM clip_markets
                    WHERE stockify_run_id=? AND stock_clip_id=?
                    ORDER BY market_id
                    """,
                    key,
                ).fetchall()
                tag_rows = connection.execute(
                    """
                    SELECT tag_group, tag, source, strength, score, evidence_json
                    FROM clip_tags
                    WHERE stockify_run_id=? AND stock_clip_id=?
                    ORDER BY tag_group, tag
                    """,
                    key,
                ).fetchall()
                analysis_row = connection.execute(
                    """
                    SELECT caption, result_json, evidence_json, provider, model, updated_at
                    FROM clip_visual_analysis
                    WHERE stockify_run_id=? AND stock_clip_id=? AND status='complete'
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    key,
                ).fetchone()
                subject_rows = connection.execute(
                    """
                    SELECT subject, source, confidence, verified,
                           raw_name, canonical_entity_id, canonical_label,
                           resolution_source
                    FROM clip_named_subjects
                    WHERE stockify_run_id=? AND stock_clip_id=?
                    ORDER BY verified DESC, subject
                    """,
                    key,
                ).fetchall()
                row_markets = [dict(item) for item in market_rows]
                row_tags = []
                for item in tag_rows:
                    decoded = dict(item)
                    decoded["evidence"] = json_loads(decoded.pop("evidence_json"), {})
                    row_tags.append(decoded)
                market_values = {
                    value.casefold()
                    for item in row_markets
                    for value in (str(item["market_id"]), str(item["market_label"]))
                }
                tag_values = {str(item["tag"]).casefold() for item in row_tags}
                if market_set and not market_set <= market_values:
                    continue
                if tag_set and not tag_set <= tag_values:
                    continue
                if orientation and row.get("orientation") != orientation:
                    continue
                enriched = dict(row)
                enriched["markets"] = row_markets
                enriched["tags"] = row_tags
                enriched["named_subjects"] = [dict(item) for item in subject_rows]
                if analysis_row:
                    enriched["caption"] = analysis_row["caption"]
                    enriched["visual_analysis"] = json_loads(
                        analysis_row["result_json"], {}
                    )
                    enriched["visual_evidence"] = json_loads(
                        analysis_row["evidence_json"], {}
                    )
                    enriched["visual_provider"] = analysis_row["provider"]
                    enriched["visual_model"] = analysis_row["model"]
                else:
                    enriched["caption"] = ""
                    enriched["visual_analysis"] = {}
                    enriched["visual_evidence"] = {}
                result.append(enriched)
        return result

    def rebuild_search_index(self) -> int:
        rows = self.catalog_rows()
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM clip_search_documents")
            if self._fts_available:
                connection.execute("DELETE FROM clip_search_fts")
            taxonomy = (
                self.search_engine.taxonomy
                if hasattr(self.search_engine, "taxonomy")
                else VisualTaxonomy.default()
            )
            for row in rows:
                tag_labels = []
                for item in row.get("tags", []):
                    tag_id = item.get("tag")
                    if not tag_id:
                        continue
                    label = taxonomy.label_for(str(tag_id))
                    if label:
                        tag_labels.append(label)
                text_values = [
                    row.get("public_label"),
                    row.get("city"),
                    row.get("neighborhood"),
                    row.get("state"),
                    row.get("country"),
                    row.get("generated_project_label"),
                    row.get("caption"),
                    row.get("orientation"),
                    row.get("time_of_day"),
                    *(item.get("market_label") for item in row.get("markets", [])),
                    *(item.get("market_id") for item in row.get("markets", [])),
                    *(item.get("tag") for item in row.get("tags", [])),
                    *tag_labels,
                    *(
                        item.get("tag_group")
                        for item in row.get("tags", [])
                        if item.get("tag_group")
                    ),
                    *(
                        item.get("raw_name") or item.get("subject")
                        for item in row.get("named_subjects", [])
                    ),
                    *(
                        item.get("canonical_label")
                        for item in row.get("named_subjects", [])
                        if item.get("canonical_label")
                    ),
                    *(
                        item.get("canonical_entity_id")
                        for item in row.get("named_subjects", [])
                        if item.get("canonical_entity_id")
                    ),
                ]
                document = " ".join(str(value) for value in text_values if value)
                connection.execute(
                    """
                    INSERT INTO clip_search_documents(
                        stockify_run_id, stock_clip_id, document_text, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        row["stockify_run_id"],
                        row["stock_clip_id"],
                        document,
                        now,
                    ),
                )
                if self._fts_available:
                    connection.execute(
                        "INSERT INTO clip_search_fts VALUES (?, ?, ?)",
                        (
                            row["stockify_run_id"],
                            row["stock_clip_id"],
                            document,
                        ),
                    )
        return len(rows)

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        explain: bool = False,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Evidence-aware search over enriched catalog rows.

        Query aliases and taxonomy labels are normalized before scoring. Ranking
        rewards canonical entities, controlled tags by strength, raw named
        subjects, then caption/location text. Multi-concept queries strongly
        prefer clips that match every concept.
        """
        rows = self.catalog_rows(run_id=run_id)
        return self.search_engine.rank(
            rows,
            query,
            limit=limit,
            explain=explain,
        )

    def save_collection_definition(
        self,
        *,
        slug: str,
        title: str,
        description: str | None,
        rule: dict[str, Any],
    ) -> str:
        collection_id = stable_id("COLLECTION", slug)
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO collection_definitions(
                    id, slug, title, description, rule_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    title=excluded.title,
                    description=excluded.description,
                    rule_json=excluded.rule_json,
                    status='active',
                    updated_at=excluded.updated_at
                """,
                (
                    collection_id,
                    slug,
                    title,
                    description,
                    json_dumps(rule),
                    now,
                    now,
                ),
            )
        return collection_id

    def publish_collection_version(
        self,
        *,
        collection_id: str,
        rule: dict[str, Any],
        metadata: dict[str, Any],
        clips: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM collection_versions WHERE collection_id=?",
                (collection_id,),
            ).fetchone()
            version = int(row[0])
            version_id = stable_id("COLLECTIONVERSION", collection_id, version)
            connection.execute(
                """
                INSERT INTO collection_versions(
                    id, collection_id, version, status, rule_json, metadata_json,
                    created_at, published_at
                ) VALUES (?, ?, ?, 'published', ?, ?, ?, ?)
                """,
                (
                    version_id,
                    collection_id,
                    version,
                    json_dumps(rule),
                    json_dumps(metadata),
                    now,
                    now,
                ),
            )
            for index, clip in enumerate(clips, start=1):
                connection.execute(
                    """
                    INSERT INTO collection_version_clips(
                        collection_version_id, stockify_run_id, stock_clip_id,
                        export_id, sort_order, score, rationale_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        clip["stockify_run_id"],
                        clip["stock_clip_id"],
                        clip["export_id"],
                        index,
                        clip.get("score"),
                        json_dumps(clip.get("rationale", {})),
                    ),
                )
        return {"collection_version_id": version_id, "version": version}

    def collection_version(self, slug: str, version: int | None = None) -> dict[str, Any]:
        with self.database.connect() as connection:
            definition = connection.execute(
                "SELECT * FROM collection_definitions WHERE slug=?",
                (slug,),
            ).fetchone()
            if definition is None:
                raise VClipError(f"Unknown collection: {slug}")
            if version is None:
                version_row = connection.execute(
                    """
                    SELECT * FROM collection_versions
                    WHERE collection_id=? AND status='published'
                    ORDER BY version DESC LIMIT 1
                    """,
                    (definition["id"],),
                ).fetchone()
            else:
                version_row = connection.execute(
                    """
                    SELECT * FROM collection_versions
                    WHERE collection_id=? AND version=?
                    """,
                    (definition["id"], version),
                ).fetchone()
            if version_row is None:
                raise VClipError(f"Collection {slug} has no requested published version.")
            clip_rows = connection.execute(
                """
                SELECT cv.*, e.exported_path, e.exported_filename, e.sha256,
                       e.duration_seconds
                FROM collection_version_clips cv
                JOIN exports e ON e.id=cv.export_id
                WHERE cv.collection_version_id=?
                ORDER BY cv.sort_order
                """,
                (version_row["id"],),
            ).fetchall()
        return {
            "definition": {
                **dict(definition),
                "rule": json_loads(definition["rule_json"], {}),
            },
            "version": {
                **dict(version_row),
                "rule": json_loads(version_row["rule_json"], {}),
                "metadata": json_loads(version_row["metadata_json"], {}),
            },
            "clips": [
                {
                    **dict(row),
                    "rationale": json_loads(row["rationale_json"], {}),
                }
                for row in clip_rows
            ],
        }

    def record_review_dedupe_removals(
        self,
        *,
        stockify_run_id: str,
        removals: Iterable[Any],
        source_fcpxml: str,
        output_fcpxml: str,
    ) -> int:
        """Persist XML-only dedupe suppressions so reconcile treats them as out-of-scope."""
        now = utc_now()
        count = 0
        with self.database.transaction() as connection:
            for item in removals:
                removal_id = stable_id(
                    "DEDUPE",
                    stockify_run_id,
                    getattr(item, "removed_stock_clip_id"),
                )
                connection.execute(
                    """
                    INSERT INTO review_dedupe_removals(
                        id, stockify_run_id, removed_stock_clip_id, kept_stock_clip_id,
                        removed_project_name, kept_project_name, source_media,
                        source_start, source_duration, reason, containment, iou,
                        source_fcpxml, output_fcpxml, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stockify_run_id, removed_stock_clip_id) DO UPDATE SET
                        kept_stock_clip_id=excluded.kept_stock_clip_id,
                        removed_project_name=excluded.removed_project_name,
                        kept_project_name=excluded.kept_project_name,
                        source_media=excluded.source_media,
                        source_start=excluded.source_start,
                        source_duration=excluded.source_duration,
                        reason=excluded.reason,
                        containment=excluded.containment,
                        iou=excluded.iou,
                        source_fcpxml=excluded.source_fcpxml,
                        output_fcpxml=excluded.output_fcpxml,
                        created_at=excluded.created_at
                    """,
                    (
                        removal_id,
                        stockify_run_id,
                        getattr(item, "removed_stock_clip_id"),
                        getattr(item, "kept_stock_clip_id"),
                        getattr(item, "removed_project_name"),
                        getattr(item, "kept_project_name"),
                        getattr(item, "source_media", None),
                        getattr(item, "source_start", None),
                        getattr(item, "source_duration", None),
                        getattr(item, "reason", "exact_source_range_duplicate"),
                        getattr(item, "containment", None),
                        getattr(item, "iou", None),
                        source_fcpxml,
                        output_fcpxml,
                        now,
                    ),
                )
                count += 1
        return count

    def review_dedupe_removed_ids(self, stockify_run_id: str) -> set[str]:
        with self.database.connect() as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT removed_stock_clip_id
                    FROM review_dedupe_removals
                    WHERE stockify_run_id=?
                    """,
                    (stockify_run_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                return set()
        return {str(row["removed_stock_clip_id"]) for row in rows}

    def record_review_global_dedupe_removals(
        self,
        *,
        removals: Iterable[Any],
    ) -> int:
        """Persist cross-shard global dedupe suppressions (idempotent upsert)."""
        now = utc_now()
        count = 0
        with self.database.transaction() as connection:
            for item in removals:
                removal_id = stable_id(
                    "GDEDUPE",
                    getattr(item, "stockify_run_id"),
                    getattr(item, "removed_stock_clip_id"),
                )
                provenance = getattr(item, "provenance", None)
                if provenance is None:
                    provenance = {}
                connection.execute(
                    """
                    INSERT INTO review_global_dedupe_removals(
                        id, stockify_run_id, removed_stock_clip_id,
                        canonical_stock_clip_id, cluster_id, cluster_type,
                        source_media, canonical_source_start, canonical_source_duration,
                        reason, containment, iou, source_shard,
                        removed_project_name, kept_project_name, provenance_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stockify_run_id, removed_stock_clip_id) DO UPDATE SET
                        canonical_stock_clip_id=excluded.canonical_stock_clip_id,
                        cluster_id=excluded.cluster_id,
                        cluster_type=excluded.cluster_type,
                        source_media=excluded.source_media,
                        canonical_source_start=excluded.canonical_source_start,
                        canonical_source_duration=excluded.canonical_source_duration,
                        reason=excluded.reason,
                        containment=excluded.containment,
                        iou=excluded.iou,
                        source_shard=excluded.source_shard,
                        removed_project_name=excluded.removed_project_name,
                        kept_project_name=excluded.kept_project_name,
                        provenance_json=excluded.provenance_json,
                        created_at=excluded.created_at
                    """,
                    (
                        removal_id,
                        getattr(item, "stockify_run_id"),
                        getattr(item, "removed_stock_clip_id"),
                        getattr(item, "canonical_stock_clip_id"),
                        getattr(item, "cluster_id"),
                        getattr(item, "cluster_type"),
                        getattr(item, "source_media", None),
                        getattr(item, "canonical_source_start", None),
                        getattr(item, "canonical_source_duration", None),
                        getattr(item, "reason"),
                        getattr(item, "containment", None),
                        getattr(item, "iou", None),
                        getattr(item, "source_shard", None),
                        getattr(item, "removed_project_name", None),
                        getattr(item, "kept_project_name", None),
                        json_dumps(provenance),
                        now,
                    ),
                )
                count += 1
        return count

    def review_global_dedupe_removed_ids(self, stockify_run_id: str) -> set[str]:
        with self.database.connect() as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT removed_stock_clip_id
                    FROM review_global_dedupe_removals
                    WHERE stockify_run_id=?
                    """,
                    (stockify_run_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                return set()
        return {str(row["removed_stock_clip_id"]) for row in rows}

    def record_review_short_prune_removals(
        self,
        *,
        removals: Iterable[Any],
    ) -> int:
        """Persist short-candidate prune suppressions (idempotent upsert)."""
        now = utc_now()
        count = 0
        with self.database.transaction() as connection:
            for item in removals:
                removal_id = stable_id(
                    "SPRUNE",
                    getattr(item, "stockify_run_id"),
                    getattr(item, "removed_stock_clip_id"),
                )
                provenance = getattr(item, "provenance", None) or {}
                connection.execute(
                    """
                    INSERT INTO review_short_prune_removals(
                        id, stockify_run_id, removed_stock_clip_id,
                        removed_project_name, reason, effective_duration_seconds,
                        min_duration_seconds, candidate_tier, short_clip_recovery,
                        source_shard, input_xml, output_xml, provenance_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stockify_run_id, removed_stock_clip_id) DO UPDATE SET
                        removed_project_name=excluded.removed_project_name,
                        reason=excluded.reason,
                        effective_duration_seconds=excluded.effective_duration_seconds,
                        min_duration_seconds=excluded.min_duration_seconds,
                        candidate_tier=excluded.candidate_tier,
                        short_clip_recovery=excluded.short_clip_recovery,
                        source_shard=excluded.source_shard,
                        input_xml=excluded.input_xml,
                        output_xml=excluded.output_xml,
                        provenance_json=excluded.provenance_json,
                        created_at=excluded.created_at
                    """,
                    (
                        removal_id,
                        getattr(item, "stockify_run_id"),
                        getattr(item, "removed_stock_clip_id"),
                        getattr(item, "removed_project_name", None),
                        getattr(item, "reason"),
                        getattr(item, "effective_duration_seconds"),
                        getattr(item, "min_duration_seconds"),
                        getattr(item, "candidate_tier", None),
                        getattr(item, "short_clip_recovery", None),
                        getattr(item, "source_shard", None),
                        getattr(item, "input_xml", None),
                        getattr(item, "output_xml", None),
                        json_dumps(provenance),
                        now,
                    ),
                )
                count += 1
        return count

    def review_short_prune_removed_ids(self, stockify_run_id: str) -> set[str]:
        with self.database.connect() as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT removed_stock_clip_id
                    FROM review_short_prune_removals
                    WHERE stockify_run_id=?
                    """,
                    (stockify_run_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                return set()
        return {str(row["removed_stock_clip_id"]) for row in rows}

    def all_pre_review_dedupe_removed_ids(self, stockify_run_id: str) -> set[str]:
        """Union of pre-review suppressions (dedupe + short prune)."""
        return (
            self.review_dedupe_removed_ids(stockify_run_id)
            | self.review_global_dedupe_removed_ids(stockify_run_id)
            | self.review_short_prune_removed_ids(stockify_run_id)
        )

    def record_review_location_recoveries(
        self,
        *,
        recoveries: Iterable[Any],
    ) -> int:
        """Persist review-shard location recovery provenance (idempotent upsert)."""
        now = utc_now()
        count = 0
        with self.database.transaction() as connection:
            for item in recoveries:
                recovery_id = stable_id(
                    "LRECV",
                    getattr(item, "stockify_run_id"),
                    getattr(item, "stock_clip_id"),
                )
                provenance = getattr(item, "provenance", None) or {}
                srt_paths = getattr(item, "srt_paths", None) or []
                connection.execute(
                    """
                    INSERT INTO review_location_recoveries(
                        id, stockify_run_id, stock_clip_id,
                        original_event_name, new_event_name,
                        original_project_name, new_project_name,
                        source_media, srt_paths_json,
                        representative_lat, representative_lon,
                        resolution_confidence, recovery_reason,
                        source_shard, input_xml, output_xml,
                        provenance_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stockify_run_id, stock_clip_id) DO UPDATE SET
                        original_event_name=excluded.original_event_name,
                        new_event_name=excluded.new_event_name,
                        original_project_name=excluded.original_project_name,
                        new_project_name=excluded.new_project_name,
                        source_media=excluded.source_media,
                        srt_paths_json=excluded.srt_paths_json,
                        representative_lat=excluded.representative_lat,
                        representative_lon=excluded.representative_lon,
                        resolution_confidence=excluded.resolution_confidence,
                        recovery_reason=excluded.recovery_reason,
                        source_shard=excluded.source_shard,
                        input_xml=excluded.input_xml,
                        output_xml=excluded.output_xml,
                        provenance_json=excluded.provenance_json,
                        created_at=excluded.created_at
                    """,
                    (
                        recovery_id,
                        getattr(item, "stockify_run_id"),
                        getattr(item, "stock_clip_id"),
                        getattr(item, "original_event_name", None),
                        getattr(item, "new_event_name", None),
                        getattr(item, "original_project_name", None),
                        getattr(item, "new_project_name", None),
                        getattr(item, "source_media", None),
                        json_dumps(list(srt_paths)),
                        getattr(item, "representative_lat", None),
                        getattr(item, "representative_lon", None),
                        getattr(item, "resolution_confidence"),
                        getattr(item, "recovery_reason"),
                        getattr(item, "source_shard", None),
                        getattr(item, "input_xml", None),
                        getattr(item, "output_xml", None),
                        json_dumps(provenance),
                        now,
                    ),
                )
                count += 1
        return count

    def review_location_recovered_ids(self, stockify_run_id: str) -> set[str]:
        with self.database.connect() as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT stock_clip_id
                    FROM review_location_recoveries
                    WHERE stockify_run_id=?
                    """,
                    (stockify_run_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                return set()
        return {str(row["stock_clip_id"]) for row in rows}

    def record_review_color_repairs(
        self,
        *,
        repairs: Iterable[Any],
    ) -> int:
        """Persist Camera LUT repair provenance (idempotent upsert)."""
        now = utc_now()
        count = 0
        with self.database.transaction() as connection:
            for item in repairs:
                repair_id = stable_id(
                    "CREPAIR",
                    getattr(item, "stockify_run_id"),
                    getattr(item, "stock_clip_id"),
                )
                provenance = getattr(item, "provenance", None) or {}
                connection.execute(
                    """
                    INSERT INTO review_color_repairs(
                        id, stockify_run_id, stock_clip_id,
                        source_media, camera_model, color_md,
                        previous_camera_lut, new_camera_lut,
                        previous_xml_lut_identity, new_xml_lut_identity,
                        previous_effect_signature, new_effect_signature,
                        repair_reason, source_shard, project_name,
                        input_xml, output_xml, srt_path,
                        provenance_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stockify_run_id, stock_clip_id) DO UPDATE SET
                        source_media=excluded.source_media,
                        camera_model=excluded.camera_model,
                        color_md=excluded.color_md,
                        previous_camera_lut=excluded.previous_camera_lut,
                        new_camera_lut=excluded.new_camera_lut,
                        previous_xml_lut_identity=excluded.previous_xml_lut_identity,
                        new_xml_lut_identity=excluded.new_xml_lut_identity,
                        previous_effect_signature=excluded.previous_effect_signature,
                        new_effect_signature=excluded.new_effect_signature,
                        repair_reason=excluded.repair_reason,
                        source_shard=excluded.source_shard,
                        project_name=excluded.project_name,
                        input_xml=excluded.input_xml,
                        output_xml=excluded.output_xml,
                        srt_path=excluded.srt_path,
                        provenance_json=excluded.provenance_json,
                        created_at=excluded.created_at
                    """,
                    (
                        repair_id,
                        getattr(item, "stockify_run_id"),
                        getattr(item, "stock_clip_id"),
                        getattr(item, "source_media", None),
                        getattr(item, "camera_model", None),
                        getattr(item, "color_md", None),
                        getattr(item, "previous_camera_lut", None),
                        getattr(item, "new_camera_lut", None),
                        getattr(item, "previous_xml_lut_identity", None),
                        getattr(item, "new_xml_lut_identity", None),
                        getattr(item, "previous_effect_signature", None),
                        getattr(item, "new_effect_signature", None),
                        getattr(item, "repair_reason"),
                        getattr(item, "source_shard", None),
                        getattr(item, "project_name", None),
                        getattr(item, "input_xml", None),
                        getattr(item, "output_xml", None),
                        getattr(item, "srt_path", None),
                        json_dumps(provenance),
                        now,
                    ),
                )
                count += 1
        return count

    def review_color_repaired_ids(self, stockify_run_id: str) -> set[str]:
        with self.database.connect() as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT stock_clip_id
                    FROM review_color_repairs
                    WHERE stockify_run_id=?
                    """,
                    (stockify_run_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                return set()
        return {str(row["stock_clip_id"]) for row in rows}
